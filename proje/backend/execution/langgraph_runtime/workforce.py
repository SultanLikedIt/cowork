"""LangGraph Workforce — supervisor + specialist nodes (Phase 4).

Drop-in for ``camel_service.run_workforce_in_thread``: same call signature,
thread + private event loop, ``on_workforce_ready`` + ``stop_gracefully``,
SSE lifecycle events via ``QueueWorkforceCallback``.

Does not stream ``token_delta`` during workers (Camel parity); ``main`` still
runs ``replay_final_answer_as_stream`` for typing UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Optional, Sequence, TypedDict
from urllib.parse import urlsplit

from langgraph.graph import END, START, StateGraph

import capabilities
import runtime_context
import untrusted_content
import web_controller
import web_fetch
from execution.ids import child_task_id
from execution.langgraph_runtime import events as lg_events
from execution.langgraph_runtime.openai_chat import complete_chat
from execution.result import execution_result
from model_config import UsageTracker
from skills import render_guidance as render_skill_guidance
from tool_call_log import ToolCallRecorder, logged_tool
from workforce.events import ActivityEvent

stabilization_logger = logging.getLogger("agent_webapp.stabilization")
# Which writer produced each PDF/DOCX (typed blocks vs markdown) and why.
# Own logger at INFO because the root logger is WARNING (see main.py):
#   docker logs proje-backend-1 | grep "document path"
document_logger = logging.getLogger("agent_webapp.document")
document_logger.setLevel(logging.INFO)

ROLE_RESEARCH = "Research Agent"
ROLE_DOCUMENT = "Document Agent"
ROLE_CODING = "Coding Agent"
ROLE_GENERAL = "Assistant"
ROLE_MAIL = "Mail Agent"
ROLE_SOCIAL = "Social Agent"
ROLE_JIRA = "Jira Agent"

WORKER_IDS = {
    ROLE_RESEARCH: "lg-research",
    ROLE_DOCUMENT: "lg-document",
    ROLE_CODING: "lg-coding",
    ROLE_GENERAL: "lg-assistant",
    ROLE_MAIL: "lg-mail",
    ROLE_SOCIAL: "lg-social",
    ROLE_JIRA: "lg-jira",
}

# Workers that gather evidence and never write the user-facing answer. Their
# output is notes for the next worker (quoted or summarized source material),
# so a job made only of these needs the Assistant appended to answer.
_GATHER_ONLY_ROLES = frozenset({ROLE_MAIL, ROLE_SOCIAL, ROLE_JIRA})


class CancellableWorkforce:
    """Handle stored on ``JobControl.workforce`` for cancel."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    def stop_gracefully(self) -> None:
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()


class WorkforceState(TypedDict, total=False):
    content: str
    original_request: str
    roles: list[str]
    role_index: int
    notes: str
    answer: str
    cancelled: bool


def _emit(callback: Any, event: dict[str, Any]) -> None:
    loop = getattr(callback, "_loop", None)
    queue = getattr(callback, "_queue", None)
    if loop is None or queue is None:
        return
    loop.call_soon_threadsafe(queue.put_nowait, event)


def _emit_activity(
    callback: Any, task_id: str, stage: str, detail: str = ""
) -> None:
    event = ActivityEvent(task_id=task_id, stage=stage, detail=detail)  # type: ignore[arg-type]
    _emit(callback, event.model_dump(mode="json"))


def _resolve_fast_path_role(fast_path_role: str) -> str:
    name = fast_path_role.strip()
    if name in WORKER_IDS:
        return name
    lower = name.casefold()
    if "research" in lower:
        return ROLE_RESEARCH
    if "document" in lower:
        return ROLE_DOCUMENT
    if "cod" in lower:
        return ROLE_CODING
    return ROLE_GENERAL


def _request_wants_local_open(text: str) -> bool:
    """True when the user wants a file opened on their OWN computer, not
    just delivered as a download -- see capabilities.LOCAL_COMPUTER and
    local_computer.py. Deliberately narrower than a bare "aç"/"open" (too
    common in unrelated asks) -- requires it to be paired with a clear
    "on my machine" signal."""
    return bool(_LOCAL_OPEN_RE.search(text or ""))


# Both halves need real word boundaries. Without them "m(ac)taki (mac)
# skorunu" satisfied verb-then-machine inside two ordinary Turkish words,
# and a request for a football score tried to open a file that was never
# asked for ("no_filename").
#
# Apple's Mac is spelled the same as Turkish "maç" typed without its
# diacritic, and in these requests the football sense is overwhelmingly
# the likely one -- so the Mac reading has to be marked, by an apostrophe
# ("mac'imde"), a possessive suffix, or the English "my mac".
_LOCAL_MACHINE = (
    r"(?:bilgisayar\w*|makine\w*|masa[üu]st[üu]\w*|"
    r"pc(?:'\w*|si|sinde|mde)?|"
    r"mac(?:'\w*|imde|imda|inde)|"
    r"lokal\w*|yerel\w*|"
    r"my\s+(?:computer|machine|desktop|pc|mac)|locally)"
)
_LOCAL_OPEN_VERB = r"(?:a[çc]|ba[şs]lat|open|launch)\w*"

_LOCAL_OPEN_RE = re.compile(
    r"(?is)"
    rf"\b{_LOCAL_OPEN_VERB}\b(?:\s+\w+){{0,4}}?\s*\b{_LOCAL_MACHINE}"
    r"|"
    rf"\b{_LOCAL_MACHINE}(?:\s+\w+){{0,4}}?\s*\b{_LOCAL_OPEN_VERB}\b"
)


def _deliverable_names(workspace_root: Optional[str], *, limit: int = 5) -> list[str]:
    """Names of the files this job produced, oldest first.

    Only the top level of artifacts/: nested directories are a bundle's
    internals (images/, a web bundle's assets) and opening those on the
    user's machine would be noise, not delivery.
    """
    if not workspace_root:
        return []
    from pathlib import Path

    artifacts = Path(workspace_root) / "artifacts"
    if not artifacts.is_dir():
        return []
    try:
        files = [
            p
            for p in artifacts.iterdir()
            if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
        ]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime)
    return [p.name for p in files[-limit:]]


def _open_local_file_step(
    *, workspace_root: Optional[str], conversation_id: Optional[str] = None
) -> str:
    """Deterministically call the LOCAL_COMPUTER capability's tool and turn
    the result into a plain confirmation/error message -- no model call
    involved, see the comment at this function's call site in run_next."""
    import local_computer

    if not local_computer.is_enabled():
        return (
            "Bilgisayarda dosya açma özelliği bu kurulumda kapalı "
            "(LOCAL_COMPUTER_ENABLED=false)."
        )
    if not local_computer.bridge_reachable():
        return (
            "Bilgisayarınızda çalışan yerel köprüye ulaşılamadı. Önce "
            "`python3 scripts/local_computer_bridge.py` komutunu kendi "
            "bilgisayarınızda çalıştırmanız gerekiyor, sonra tekrar deneyin."
        )
    fallback_roots: list[str] = []
    if conversation_id:
        try:
            import db

            fallback_roots = [
                root
                for root in db.workspace_roots_for_conversation(conversation_id)
                if root != workspace_root
            ]
        except Exception:  # noqa: BLE001
            stabilization_logger.exception(
                "failed to look up prior workspace roots for conversation %s",
                conversation_id,
            )
    tools = local_computer.LocalComputerTools(
        workspace_root=workspace_root, fallback_workspace_roots=fallback_roots
    )

    # Open every deliverable this job produced, not just the newest one.
    # A "pptx ve html hazırla, sonra aç" run writes two files and
    # open_local_file() with no name resolves to the most recent, so the
    # other one silently stayed closed.
    names = _deliverable_names(workspace_root)
    if len(names) > 1:
        opened: list[str] = []
        failures: list[str] = []
        for name in names:
            outcome = tools.open_local_file(name)
            if outcome.get("ok"):
                opened.append(str(outcome.get("opened") or name))
            else:
                failures.append(name)
        if opened:
            line = "Açıldı: " + ", ".join(opened)
            if failures:
                line += f" (açılamayan: {', '.join(failures)})"
            return line
        # Every named open failed -- fall through to the unnamed call,
        # which has its own resolution and error reporting.

    result = tools.open_local_file()
    if result.get("ok"):
        opened_one = result.get("opened") or "dosya"
        return f"Açıldı: {opened_one}"
    error = result.get("error") or "unknown_error"
    detail = str(result.get("detail") or "").strip()
    return f"Dosya açılamadı ({error}). {detail}".strip()


def _is_chat_redelivery_followup(text: str) -> bool:
    """True when the user wants prior facts in chat, not as a new file."""
    body = text or ""
    return bool(
        re.search(
            r"(?is)"
            r"(?:chatte?|sohbette|burada|yazı(?:yla)?|metin(?:le)?)\s*"
            r"(?:olarak\s+)?(?:ver|göster|yaz|anlat)"
            r"|"
            r"(?:xlsx|excel|dosya|pdf|pptx|powerpoint|word)\s*"
            r"(?:olarak\s+)?istemedim"
            r"|"
            r"(?:dosya|excel|xlsx)\s*(?:değil|degil|olmasın|olmasin)"
            r"|"
            r"(?:sadece|yalnızca|yalnizca)\s+(?:chat|sohbet|metin|yazı|yazi)",
            body,
        )
    )


def _is_bare_format_switch(text: str) -> bool:
    """True when the whole ask is just 'give that as PDF/Excel/…'."""
    body = (text or "").strip()
    if not body or len(body) > 120:
        return False
    # Drop polite filler so "lütfen pdf olarak verir misin" still matches.
    cleaned = re.sub(
        r"(?is)\b(?:"
        r"l[uü]tfen|please|bana|bir\s+de|art[iı]k|"
        r"ver(?:ir)?\s*m[iı]s[iı]n|yap(?:ar)?\s*m[iı]s[iı]n|"
        r"oluştur(?:ur)?\s*musun|olustur|"
        r"haz[iı]rla(?:r)?\s*m[iı]s[iı]n"
        r")\b",
        " ",
        body,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?!.")
    return bool(
        re.fullmatch(
            r"(?is)(?:bunu|şunu|sunu|this|it|aynı(?:sını|sini)?|onceki|önceki)?\s*"
            r"(?:pdf|pptx|ppt|powerpoint|power\s*point|word|docx|excel|xlsx|csv|"
            r"sunum|slayt|html|presentation)\s*"
            r"(?:olarak\s+)?(?:da\s+)?(?:ver|yap|oluştur|olustur|hazırla|hazirla)?",
            cleaned,
        )
    )


def _is_reformat_followup(text: str) -> bool:
    """True when the ask is 'turn that into a deck/file/chat', not new research.

    Fresh asks that both research and deliver ("…araştır… PowerPoint olarak
    da ver") must NOT match. Only anaphoric reformats of prior work
    ("bunu sunum olarak", "also as a powerpoint", "chatte ver") skip web
    research and reuse prior assistant notes.
    """
    body = text or ""
    if re.search(
        r"(?is)\b("
        r"araştır\w*|research|literature|literatür|incele\w*"
        r"|survey|overview|karşılaştır\w*|deep\s+research"
        r")\b",
        body,
    ):
        return False
    if _is_chat_redelivery_followup(body) or _is_bare_format_switch(body):
        return True
    return bool(
        re.search(
            r"(?is)"
            r"(?:\bbunu\b|\bşu(?:nu)?\b|\bthis\b|\bsame\b|\baynı(?:sını|sini)?\b|"
            r"\bbir\s+de\b|\balso\b|\bö(?:nceki|nceki)\b|\baz\s+önce(?:ki)?\b|"
            r"\bo\s+(?:tablo|sonuç|sonuc|cevap|dosya)\b|"
            r"\binstead\b|\byerine\b)"
            r".{0,100}"
            r"(?:sunum|pptx|powerpoint|slayt|html|word|docx|pdf|presentation|deck|"
            r"excel|xlsx|csv|tablo)"
            r"|"
            r"(?:as\s+a\s+)?(?:presentation|powerpoint|slide\s*deck|pdf|word|excel)"
            r"\s+(?:of\s+)?(?:that|this|it)\b",
            body,
        )
    )


def _has_prior_turn_material(content: str) -> bool:
    """True when prior assistant notes exist to reformat (not user-only history)."""
    text = content or ""
    return any(
        marker in text
        for marker in (
            "--- ASSISTANT ---",
            "[Summary of earlier conversation:]",
        )
    )


def _last_assistant_excerpt(content: str, *, limit: int = 12000) -> str:
    """Last assistant turn from conversation context, if present."""
    text = content or ""
    marker = "--- ASSISTANT ---"
    if marker not in text:
        return ""
    excerpt = text.rsplit(marker, 1)[-1].strip()
    for stop in ("--- USER ---", "=== USER-PROVIDED CONTEXT ==="):
        idx = excerpt.find(stop)
        if idx >= 0:
            excerpt = excerpt[:idx].strip()
    return excerpt[:limit]


def _request_needs_live_data(text: str) -> bool:
    """Whether answering honestly requires opening a page right now.

    "son maç", "bugün", "bu hafta sonu" style asks cannot be answered from
    model memory at all. Reuses the phase controller's own detectors so the
    role gate and the search ranking agree on what "latest" means.
    """
    body = (text or "").strip()
    if not body:
        return False
    return bool(
        web_controller.request_looks_last_occurrence(body)
        or web_controller.request_looks_time_sensitive(body)
    )


def _select_roles(
    *,
    job_capabilities: Optional[frozenset],
    fast_path_role: Optional[str],
    wants_artifact: bool,
    wants_sources: bool = False,
    skip_web_research: bool = False,
    needs_live_data: bool = False,
    reads_email: bool = False,
    reads_social: bool = False,
    reads_jira: bool = False,
) -> list[str]:
    """Pick workers for this job.

    File policy (user-facing contract):
    - ``wants_artifact=True`` → Document Agent is **always** scheduled so a
      real downloadable file is produced (even on specialist / fast-path
      Research-only routes — that was skipping Document and answering in
      chat only).
    - ``wants_artifact=False`` → Document Agent is **never** scheduled, even
      if ARTIFACTS somehow sits in job_capabilities. Chat-only asks must
      stay chat-only.
    """
    roles: list[str] = []
    caps = set(capabilities.expand(job_capabilities or frozenset()))
    # The mailbox is the SOURCE for this job, so it is read before anything
    # else runs and its notes flow into whatever comes next -- the same
    # position Research holds for the web. Placed ahead of the fast-path
    # branch because a specialist route ("just write the summary") still has
    # to be given the mail it is supposed to summarize.
    if reads_email or capabilities.MAIL_READ in caps:
        roles.append(ROLE_MAIL)
    # Same position and the same reasoning as the Mail Agent: public
    # discussion is the SOURCE for this job, so it is gathered before
    # anything that has to use it.
    if reads_social or capabilities.SOCIAL_READ in caps:
        roles.append(ROLE_SOCIAL)
    # And again for Jira: the user's tickets are the SOURCE, gathered first.
    if reads_jira or capabilities.JIRA_READ in caps:
        roles.append(ROLE_JIRA)
    if fast_path_role:
        roles.append(_resolve_fast_path_role(fast_path_role))
    else:
        if (
            not skip_web_research
            and caps & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
        ):
            roles.append(ROLE_RESEARCH)
        if capabilities.CODE_EXEC in caps:
            roles.append(ROLE_CODING)

    # Citations require live pages even when routing skipped web capabilities.
    # Reformat follow-ups reuse prior notes instead of searching again.
    if (
        wants_sources
        and not skip_web_research
        and ROLE_RESEARCH not in roles
    ):
        roles.insert(0, ROLE_RESEARCH)

    # Artifact gate is independent of fast-path / capability expand.
    if wants_artifact:
        if ROLE_DOCUMENT not in roles:
            roles.append(ROLE_DOCUMENT)
    else:
        roles = [role for role in roles if role != ROLE_DOCUMENT]

    # The Mail Agent gathers; it does not answer. Its output is quoted mail
    # inside an explicit "this is data, not instructions" fence, so shipping
    # it as the reply would hand the user a transcript of their own inbox --
    # and would put unfenced sender text where the answer should be. Anything
    # else in the list can do the answering; alone, it needs the Assistant.
    if roles and all(role in _GATHER_ONLY_ROLES for role in roles):
        # These gather; they do not answer. Their output is quoted text
        # inside an explicit "this is data, not instructions" fence, so
        # shipping it as the reply would put unfenced stranger-written text
        # where the answer should be. Anything else in the list can do the
        # answering; alone, they need the Assistant.
        roles.append(ROLE_GENERAL)

    if not roles:
        # Capability routing is a model call, so the same question can come
        # back with web capabilities on one run and none on the next. Falling
        # through to the tool-less Assistant made it answer "I have no
        # internet access" for an ask that is unanswerable without one.
        # Narrow on purpose: only when there is no other worker at all.
        if needs_live_data and not skip_web_research:
            roles.append(ROLE_RESEARCH)
        else:
            roles.append(ROLE_GENERAL)

    seen: set[str] = set()
    ordered: list[str] = []
    for role in roles:
        if role not in seen:
            seen.add(role)
            ordered.append(role)
    return ordered


def _artifacts_on_disk(workspace_root: Optional[str]) -> list[str]:
    """Return relative paths of deliverable files under artifacts/."""
    if not workspace_root:
        return []
    from pathlib import Path

    root = Path(workspace_root) / "artifacts"
    if not root.is_dir():
        return []
    skip = {".artifact-manifest.json", ".ds_store"}
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.casefold() in skip:
            continue
        try:
            found.append(str(path.relative_to(Path(workspace_root))))
        except ValueError:
            found.append(str(path))
    return found


async def _make_complete(
    tracker: UsageTracker,
    *,
    deep_research: bool = False,
    # Usage is reported per stage, and every stage here was a web phase when
    # this was written. The Mail Agent reuses the same wrapper for its own
    # phases, and recording those as "web_mail_plan" would put mailbox reads
    # in the web research column of the timing panel.
    stage_prefix: str = "web",
):
    async def complete(
        phase: str, system: str, user: str, schema: Optional[type]
    ) -> Any:
        if phase == "iterate":
            # Iterative deep research: each round's JSON carries the whole
            # rewritten report (~3k tokens by ITER_REPORT_MAX_CHARS) plus
            # think/action. At 2048 the JSON is cut mid-report and the
            # round is lost.
            max_tokens = 8192
        elif schema is not None:
            max_tokens = 2048
        elif deep_research and phase == "answer":
            # A deep run gathers dozens of pages across many angles, and the
            # brief has to give every part of the ask its own section. The
            # default leaves no room for that once the first section carries
            # a table.
            max_tokens = 12288
        elif phase == "answer":
            # The answer phase is not one step among many: when the brief is
            # already report-shaped the Document worker is skipped entirely
            # and this text IS the deliverable, so its ceiling is the
            # report's length. At 4096 a plain Workforce report stopped
            # mid-sentence inside section 5 and the server appended the
            # sources block to the wound. Every other phase keeps the
            # default -- they emit a plan, a query or a selection, not prose.
            max_tokens = 8192
        else:
            max_tokens = 4096
        return await complete_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tracker=tracker,
            stage=f"{stage_prefix}_{phase}",
            max_tokens=max_tokens,
            # Only the answer: when the brief is already report-shaped the
            # Document worker is skipped and this text is what the user
            # downloads, so being cut short is a broken deliverable rather
            # than a shorter plan.
            # And an iterate round: truncated JSON loses the whole report.
            retry_on_truncation=(
                (phase == "answer" and schema is None) or phase == "iterate"
            ),
            temperature=0 if schema is not None else None,
            response_format=schema,
        )

    return complete


async def _extract_lookup_targets(
    request_text: str,
    *,
    complete: Any,
) -> list[str]:
    """Named lookup subjects via the same rules as Camel workforce_plan.

    Empty list means one unscoped SEARCH (single subject / coupled design).
    """
    import workforce_plan

    text = (request_text or "").strip()
    if not text:
        return []
    try:
        parsed = await complete(
            "plan",
            workforce_plan.PLAN_SYSTEM_PROMPT,
            f"REQUEST:\n{text}",
            workforce_plan.PlanResult,
        )
    except Exception:  # noqa: BLE001
        stabilization_logger.warning(
            "LangGraph lookup-target plan failed; single search",
            exc_info=True,
        )
        return []
    return workforce_plan.lookup_target_names_from_plan(parsed)


def _lifecycle_start(
    callback: Any,
    *,
    role: str,
    worker_id: str,
    unit_task_id: str,
    description: str,
) -> None:
    _emit(
        callback,
        lg_events.worker_created(
            worker_id=worker_id, role=role, worker_type="langgraph_worker"
        ),
    )
    _emit(
        callback,
        lg_events.task_created(task_id=unit_task_id, description=description),
    )
    _emit(
        callback,
        lg_events.task_assigned(task_id=unit_task_id, worker_id=worker_id),
    )
    _emit(
        callback,
        lg_events.task_started(task_id=unit_task_id, worker_id=worker_id),
    )


def _lifecycle_done(
    callback: Any,
    *,
    worker_id: str,
    unit_task_id: str,
    result_summary: str,
    started: float,
) -> None:
    _emit(
        callback,
        lg_events.task_completed(
            task_id=unit_task_id,
            worker_id=worker_id,
            result_summary=result_summary,
            processing_time_seconds=time.monotonic() - started,
        ),
    )


def _answer_looks_insufficient(answer: str) -> bool:
    """Heuristic: model admits evidence does not contain the asked facts.

    Bare substring match -- every marker here must be a phrase that only
    shows up in an actual refusal/admission. Two markers were removed after
    real false positives: "açılan sayfa" also matches "açılan sayfalardan
    elde edilen bilgiler" (info FROM the opened pages -- a normal citation);
    "bu kanıt" also matches "bu kanıt özetinde yer almadığından" (a caveat
    about ONE missing row/column in an otherwise-good partial table, e.g.
    a gol krallığı table missing its Takım column and rows 8-10 -- real,
    substantial data, not a refusal). Both silently dropped Document Agent
    from a perfectly good, partially-complete research pass (see
    _research_notes_insufficient's caller in run_next)."""
    text = (answer or "").casefold()
    markers = (
        "kanıtlarda",
        "kanitlarda",
        "bulunmuyor",
        "bulunamadı",
        "bulunamadi",
        "bulunamamış",
        "bulunamamis",
        "bulunamamıştır",
        "bulunamamistir",
        "veremem",
        "veremiyorum",
        "sağlayamam",
        "saglayamam",
        "yeterli kanıt",
        "yeterli kanit",
        "evidence does not",
        "not in the evidence",
        "cannot provide",
        "could not find",
        "no information",
        "mevcut değil",
        "mevcut degil",
        "mevcut değildir",
        "elimizdeki",
        "içerik yok",
        "icerik yok",
        "içermemektedir",
        "icermemektedir",
        "içermiyor",
        "icermiyor",
        "sayfada yok",
        "karşılayamıyorum",
        "karsilayamiyorum",
        "karşılanam",
        "karsilanam",
        "farklı bir konu",
        "farkli bir konu",
        "does not answer",
        "herhangi bir bilgi",
        "herhangi bir veri",
        "bilgi içermemektedir",
        "bilgi icermemektedir",
        "güncel bir kaynak bulunamad",
        "guncel bir kaynak bulunamad",
        "kaynak bulunamad",
        "uydurma",
        "talep edilen",
        "istenilen kriter",
        "istenilen bilgi",
    )
    return any(m in text for m in markers)

def _web_lookup_text(original_request: str, content: str) -> str:
    """Text that SEARCH/SELECT may see — never conversation history.

    ``content`` from main is often topic + PRIOR MESSAGES. Feeding that into
    SEARCH makes the model chase an earlier turn (e.g. FX rates) instead of
    the current ask. Camel uses assigned/original_request only; match that.
    """
    primary = (original_request or "").strip()
    if primary:
        return primary
    raw = (content or "").strip()
    if not raw:
        return ""
    for marker in (
        "PRIOR MESSAGES IN THIS CONVERSATION",
        "=== USER-PROVIDED CONTEXT ===",
        "[Summary of earlier conversation:]",
        "\n--- USER ---\n",
        "\n--- ASSISTANT ---\n",
    ):
        idx = raw.find(marker)
        if idx >= 0:
            raw = raw[:idx].strip()
    return raw.strip()


_MIN_SUBSTANTIVE_CHARS = 300
# Research already wrote a usable brief — packaging LLM adds little quality
# and can hang for the full timeout. Skip it for simple single-file narrative.
_MIN_DIRECT_PDF_NOTES_CHARS = 500
# Hard cap: one recovery pass max. Extra passes burn search+fetch+answer
# tokens with diminishing returns.
_MAX_RECOVERY_PASSES = 1


def _substantive_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pages that actually carry usable body text or tabular preview."""
    good: list[dict[str, Any]] = []
    for item in evidence or []:
        if not item.get("ok"):
            continue
        text = str(item.get("text") or "").strip()
        if len(text) >= _MIN_SUBSTANTIVE_CHARS:
            good.append(item)
            continue
        if int(item.get("row_count") or item.get("record_count") or 0) > 0:
            good.append(item)
            continue
        if item.get("preview"):
            good.append(item)
    return good


def _opened_urls(evidence: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in evidence or []:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


_RESEARCH_SOURCES_MARKER = "\n---\nRESEARCH_SOURCES:\n"
_ARTIFACT_HANDOFF_MARKER = "\n---\nARTIFACT_HANDOFF:\n"
# Word-boundary-ish: require a real "sources" ask, not substrings like
# "resource" / bare "link" in unrelated words.
_SOURCES_REQUEST_RE = re.compile(
    r"(?:"
    r"\bkaynak(?:lar(?:ı|i)?|ça|ca)?\b|"
    r"\bkaynakça\b|\bkaynakca\b|"
    r"\bsources?\b|"
    r"\bcitations?\b|"
    r"\breferences?\b|"
    r"\bbibliography\b|"
    r"\bcite(?:\s+your)?\s+sources?\b|"
    r"\breferans(?:lar(?:ı|i)?)?\b|"
    r"\b(?:source|reference)\s+(?:links?|urls?|list)\b|"
    r"\blink(?:ler(?:i)?|leri)?\s*(?:ver|ekle|koy|listele)\b|"
    r"\b(?:ver|ekle|koy)\b.{0,40}\b(?:kaynak|source|referans)"
    r")",
    re.IGNORECASE,
)


def _append_source_entry(
    entries: list[dict[str, str]],
    seen: set[str],
    *,
    url: str,
    title: str = "",
) -> None:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")) or url in seen:
        return
    seen.add(url)
    entries.append({"url": url, "title": (title or "").strip()})


def _research_sources_from_evidence(
    evidence: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """URLs Research found — opened pages first, then any other candidate."""
    opened: list[dict[str, str]] = []
    found: list[dict[str, str]] = []
    seen_ok: set[str] = set()
    seen_any: set[str] = set()
    for item in evidence or []:
        url = str(item.get("url") or item.get("source_url") or "").strip()
        title = str(item.get("title") or "").strip()
        if item.get("ok"):
            _append_source_entry(opened, seen_ok, url=url, title=title)
        _append_source_entry(found, seen_any, url=url, title=title)
    return opened or found


def _opened_citation_sources(
    evidence: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Only pages that actually returned usable content — for Kaynaklar."""
    from web_controller import is_successful_fetch

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in evidence or []:
        if not item.get("ok"):
            continue
        if not is_successful_fetch(item):
            continue
        _append_source_entry(
            entries,
            seen,
            url=str(item.get("url") or item.get("source_url") or ""),
            title=str(item.get("title") or ""),
        )
    return entries


def _research_sources_from_controller(acquired: Any) -> list[dict[str, str]]:
    """Selected/opened search URLs — plus search hits when fetches fail.

    Used for recovery routing, not for final Kaynaklar (that needs
    ``_opened_citation_sources`` so unread PDFs are not cited as read).
    """
    selected: list[dict[str, str]] = []
    candidates: list[dict[str, str]] = []
    seen_selected: set[str] = set()
    seen_candidates: set[str] = set()
    stats_map = getattr(acquired, "target_stats", None) or {}
    for stats in stats_map.values():
        if not isinstance(stats, dict):
            continue
        for url in stats.get("selected_urls") or []:
            _append_source_entry(selected, seen_selected, url=str(url or ""))
        for item in stats.get("candidate_urls") or []:
            if isinstance(item, dict):
                _append_source_entry(
                    candidates,
                    seen_candidates,
                    url=str(item.get("url") or ""),
                    title=str(item.get("title") or ""),
                )
            else:
                _append_source_entry(
                    candidates, seen_candidates, url=str(item or "")
                )
    return selected or candidates


def _merge_source_lists(
    *groups: list[dict[str, str]],
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            _append_source_entry(
                merged,
                seen,
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
            )
    return merged


def _research_sources_block(
    evidence: list[dict[str, Any]],
    extra: Optional[list[dict[str, str]]] = None,
) -> str:
    """Emit citation URLs for Document/chat — opened pages only.

    Unread search hits / failed PDF links must not appear as Kaynaklar.
    ``extra`` is ignored for the citation list (kept for call-site compat).
    """
    del extra  # recovery may pass controller URLs; do not cite unread ones
    entries = _opened_citation_sources(evidence)
    return _RESEARCH_SOURCES_MARKER + json.dumps(entries, ensure_ascii=False)


_RESEARCH_INSUFFICIENT_RE = re.compile(
    r"(?is)(?:"
    r"hiçbir sayfa başarıyla açılamamış|"
    r"hiçbir veri[,\s].*ulaşılamamış|"
    r"yeterli kanıt bulunmamakt|"
    r"no usable page content|"
    r"evidence does not answer|"
    r"erişim sağlanamad|"
    r"güvenilir bir kaynak bulunamamış|"
    r"bilgi mevcut değil|"
    r"research failed|"
    # Polite "opened a page but the asked facts are missing" answers.
    r"ulaşılamamış|"
    r"ulaşılamadı|"
    r"bulunamamış|"
    r"bulunamadı|"
    r"bulunamamıştır|"
    r"mevcut değildir|"
    r"mevcut değil|"
    r"içermemektedir|"
    r"herhangi bir (?:bilgi|veri)|"
    r"veri(?:ler)?\s+(?:yok|eksik|bulunam|mevcut değil)|"
    r"istenilen?\s+(?:bilgi|veri|rakam|kriter).{0,60}(?:yok|bulunam|ulaşılam|karşılanam)|"
    r"talep edilen.{0,80}(?:karşılanam|bulunam|mevcut değil)|"
    r"could not (?:find|verify)|"
    r"not (?:found|available) (?:in|on) (?:the )?(?:opened|acquired)|"
    r"no (?:current|usable|relevant) (?:source|information|results)"
    r")"
)


_MD_TABLE_SEPARATOR_LINE_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _markdown_table_row_count(text: str) -> int:
    """Body (non-header, non-separator) rows across every markdown table in
    text -- a rough proxy for "how much real tabular data is here", used to
    tell a genuinely populated ranked list/table apart from a token grid."""
    rows = 0
    seen_separator = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if _MD_TABLE_SEPARATOR_LINE_RE.match(stripped):
            seen_separator = True
            continue
        if seen_separator:
            rows += 1
    return rows


def _notes_have_deliverable_substance(text: str) -> bool:
    """True when notes carry enough verified material for a Word/PDF body.

    Multi-target reports often admit one missing subject ("Güney Kore için
    veri bulunamadı") while still having figures for the others. Those must
    still become a file — the insufficient heuristics alone are too blunt.
    """
    body = (text or "").strip()
    if len(body) < 280:
        return False
    if not re.search(r"\d{2,}", body):
        return False
    headings = len(re.findall(r"(?m)^#{1,3}\s+\S", body))
    bullets = len(re.findall(r"(?m)^\s*[-*•]\s+\S", body))
    has_table = body.count("|") >= 4
    # A "not found" write-up with a tiny placeholder table is not a report.
    # Real multi-target briefs have several headings plus figures -- but a
    # single ranked table (e.g. a top-10 list) only ever has ONE heading no
    # matter how populated it is, so require that OR several real table
    # rows (a few missing cells/rows described with refusal-toned language,
    # e.g. "(Kanıtlarda bulunamadı)" placeholders for 3 of 10 rows, must not
    # by itself discard 7 real rows of names and figures).
    if _answer_looks_insufficient(body):
        return (headings >= 2 and (bullets >= 3 or has_table)) or (
            has_table and _markdown_table_row_count(body) >= 3
        )
    # Structured brief with real figures → package even if one target failed.
    if headings >= 2 or has_table or bullets >= 3:
        return True
    return len(body) >= max(_MIN_DIRECT_PDF_NOTES_CHARS, 900)


def _research_notes_insufficient(notes: str) -> bool:
    text = (notes or "").strip()
    if not text:
        return True
    # Partial multi-target briefs still deserve Document packaging.
    if _notes_have_deliverable_substance(text):
        return False
    if _RESEARCH_INSUFFICIENT_RE.search(text):
        return True
    # Same signals as chat-answer insufficiency (opened page, wrong facts).
    return _answer_looks_insufficient(text)


def _enrich_lookup_for_research(request_text: str) -> str:
    """General research nudge — not sector-specific."""
    text = (request_text or "").strip()
    if not text:
        return text
    return (
        f"{text}\n\n"
        "SEARCH HINT: Search for the facts in the request. Prefer HTML "
        "detail/article/bulletin pages whose snippets match the topic. "
        "Any reputable on-topic source is fine — do not insist on one "
        "official host. Avoid generic hubs, off-topic encyclopedia pages, "
        "and .pdf downloads as the first choice."
    )


# Older name used by tests.
def _enrich_lookup_for_stats(request_text: str) -> str:
    return _enrich_lookup_for_research(request_text)


def _split_research_sources(notes: str) -> tuple[str, list[dict[str, str]]]:
    """Strip machine blocks from notes; return cleaned text + parsed URLs.

    ``RESEARCH_SOURCES`` may appear *after* ``ARTIFACT_HANDOFF`` (Word/PDF
    path). Always extract the JSON array from the full blob first — never
    strip the handoff before reading sources, or Kaynaklar goes empty.
    """
    text = (notes or "").strip()
    out: list[dict[str, str]] = []
    marker = "RESEARCH_SOURCES:"
    idx = text.find(marker)
    if idx >= 0:
        after = text[idx + len(marker) :].lstrip()
        parsed: Any = None
        end = 0
        try:
            parsed, end = json.JSONDecoder().raw_decode(after)
        except json.JSONDecodeError:
            match = re.search(r"(\[[\s\S]*\])", after)
            if match:
                try:
                    parsed = json.loads(match.group(1))
                    end = match.end()
                except json.JSONDecodeError:
                    parsed = None
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if url:
                    out.append(
                        {
                            "url": url,
                            "title": str(item.get("title") or "").strip(),
                        }
                    )
        # Drop the machine block (and a leading --- separator if present).
        cut = idx
        sep = "\n---\n"
        if text[max(0, idx - len(sep)) : idx] == sep:
            cut = idx - len(sep)
        text = (text[:cut] + after[end:]).strip()

    if _ARTIFACT_HANDOFF_MARKER in text:
        text = text.split(_ARTIFACT_HANDOFF_MARKER, 1)[0].strip()
    return text, out


def _request_wants_sources(text: str) -> bool:
    return bool(_SOURCES_REQUEST_RE.search(text or ""))


_IMAGE_CDN_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"images\.pexels\.com|images\.unsplash\.com|cdn\.pixabay\.com|"
    r"upload\.wikimedia\.org|i\.imgur\.com|"
    r"(?:www\.)?shutterstock\.com|(?:www\.)?gettyimages\."
    r"|(?:www\.)?istockphoto\.com|(?:www\.)?adobe\.stock"
    r")$"
)
_IMAGE_PATH_EXT = re.compile(r"(?i)\.(?:jpe?g|png|gif|webp|svg)(?:\?|$)")


def _is_image_asset_url(url: str) -> bool:
    """True for photo CDN / stock-search URLs (not article sources)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if _IMAGE_CDN_HOST.search(host):
        return True
    path = (parsed.path or "").lower()
    if "/search/" in path and any(
        k in host for k in ("shutterstock", "unsplash", "pexels", "pixabay")
    ):
        return True
    return bool(_IMAGE_PATH_EXT.search(path))


def _urls_from_notes(notes: str) -> list[dict[str, str]]:
    """Best-effort URL harvest when RESEARCH_SOURCES block is missing."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s\]\)\"'<>]+", notes or ""):
        url = match.group(0).rstrip(".,;:")
        if url in seen or len(url) < 12 or _is_image_asset_url(url):
            continue
        seen.add(url)
        found.append({"url": url, "title": ""})
        if len(found) >= 12:
            break
    return found


# Cookie-wall and app-shell text that some pages leave inside <title>.
# This is scraped page chrome, not user language: the strings are literally
# what those banners say, and a title that contains one is truncated there.
_TITLE_CHROME_RE = re.compile(
    r"(?i)\s*(?:close this consent banner|consent banner|accept all cookies|"
    r"cookie (?:policy|settings|notice)|çerez (?:politikası|ayarları|bildirimi)|"
    r"kabul et|reklam engelleyici|skip to (?:main )?content|"
    r"javascript.{0,20}(?:enable|etkinle))"
)


# Title separators. Sites append their own name after one of these
# ("Article — Site Name"), so segments are examined individually.
_TITLE_SEPARATOR_RE = re.compile(r"\s+[|•·–—]\s+|\s+-\s+|\s*::\s*|\s*»\s*")

# Longest a shown title may be. Past this it is not a title, it is the page's
# navigation flattened into one line, and it breaks the sources list layout.
_MAX_TITLE_CHARS = 120

# Single words that are navigation, not content. A segment made only of
# these is dropped; one that merely contains them is left alone, because
# "Contact tracing in Turkey" is a real title and "Home Contact About" is not.
_NAV_WORDS = frozenset(
    {
        "home", "homepage", "menu", "search", "login", "log in", "sign in",
        "signin", "register", "subscribe", "newsletter", "contact", "about",
        "about us", "privacy", "terms", "cookies", "sitemap", "share",
        "anasayfa", "ana sayfa", "giriş", "giris", "kayıt", "kayit", "ara",
        "arama", "menü", "menu̇", "iletişim", "iletisim", "hakkında",
        "hakkinda", "gizlilik", "çerezler", "cerezler", "abone ol", "paylaş",
        "paylas",
    }
)


def _host_label(url: str) -> str:
    """"example.com" from a URL, without www. Empty when unparseable."""
    from urllib.parse import urlparse

    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _ascii_fold(value: str) -> str:
    """Lowercase ASCII skeleton of a string, for comparison only.

    Needed to recognise a Turkish site name as its own domain: "TÜİK" and
    "tuik.gov.tr" are the same organisation, but casefold() alone leaves
    "tüi̇k" (İ decomposes to i + combining dot) which matches nothing. NFKD
    strips the combining marks; ı is mapped explicitly because it has no
    decomposition of its own.
    """
    import unicodedata

    folded = unicodedata.normalize("NFKD", str(value or "").replace("ı", "i"))
    return "".join(
        ch for ch in folded if not unicodedata.combining(ch)
    ).casefold()


def _is_nav_segment(segment: str) -> bool:
    """Whether a title segment is pure navigation furniture."""
    # Separators survive the split ("Home - Contact" -> [home, -, contact]),
    # and a leftover "-" is not a nav word, which made every multi-segment
    # run of furniture look like content. Strip first, then drop what is
    # left empty.
    words = [
        stripped
        for word in re.split(r"[\s,/>|·•–—]+", segment.casefold())
        if (stripped := word.strip(" .:-"))
    ]
    if not words or len(words) > 6:
        return False
    return all(word in _NAV_WORDS for word in words)


def _clean_source_title(raw: str, url: str = "") -> str:
    """A page title fit to show, from one that was scraped.

    Real titles arrive carrying the page's own furniture: a live run listed
    "Kanuni Sultan Süleyman Dönemi | PDF <!---->Close this consent
    banner<!----> <!---->" as a source, which is the cookie wall's markup
    sitting in the <title>.

    Cutting at a list of banner phrases (``_TITLE_CHROME_RE``) only ever
    fixed the wordings that list knows, in the two languages it knows, and
    the open set of websites keeps producing new ones -- site navigation
    flattened into the element, a breadcrumb trail, the site name repeated.
    So the phrase cut is kept as one step and the rest is structural:

    * split on the separators sites use to append their own name;
    * drop segments that are nothing but navigation words;
    * drop a trailing segment that is just the site's domain, which is
      already shown next to the title in the sources list;
    * cap the length, and if nothing survives that looks like a title, say
      the domain rather than show the page's furniture.

    ``url`` is optional so existing single-argument calls keep working; it
    only enables the last two rules.
    """
    text = re.sub(r"<!--.*?-->", " ", str(raw or ""), flags=re.DOTALL)
    text = re.sub(r"<[^>]*>", " ", text)
    match = _TITLE_CHROME_RE.search(text)
    if match:
        text = text[: match.start()]
    text = " ".join(text.split()).strip(" |-–—·•")
    if not text:
        return ""

    host = _host_label(url)
    segments = [
        part.strip(" |-–—·•")
        for part in _TITLE_SEPARATOR_RE.split(text)
        if part and part.strip(" |-–—·•")
    ]
    if len(segments) > 1:
        # Every segment being navigation is not a failed split -- it is a
        # title that is entirely furniture ("Home | Contact | About"). Let
        # it empty out and fall through to the domain below.
        kept = [seg for seg in segments if not _is_nav_segment(seg)]
        # A trailing segment that is the site's own name duplicates the URL
        # shown beside it. Only trailing, and only when something else
        # remains -- "BBC News" as the whole title is the title.
        if len(kept) > 1 and host:
            tail = _ascii_fold(kept[-1]).replace(" ", "")
            stem = _ascii_fold(host.split(".")[0])
            if tail in {_ascii_fold(host), _ascii_fold(host).replace(".", ""), stem}:
                kept.pop()
        text = " - ".join(kept)

    if len(text) > _MAX_TITLE_CHARS:
        # Prefer a clean break at the last separator that still fits over a
        # hard slice through the middle of a word.
        head = text[:_MAX_TITLE_CHARS]
        cut = max(head.rfind(" - "), head.rfind(": "))
        text = (head[:cut] if cut > 40 else head.rsplit(" ", 1)[0]).rstrip(" ,;:-–—")

    text = text.strip(" |-–—·•")
    if not text or _is_nav_segment(text):
        return host
    return text


def _dedupe_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """One entry per page. The same URL reached the list twice when two
    angles opened it, and the reader saw the same link listed twice."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in sources or []:
        url = str(item.get("url") or "").strip()
        key = url.rstrip("/").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _format_sources_lines(sources: list[dict[str, str]]) -> list[str]:
    """Markdown bullets with title + full https URL for chat readability."""
    lines: list[str] = []
    for item in _dedupe_sources(sources):
        url = str(item.get("url") or "").strip()
        if not url or _is_image_asset_url(url):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        title = (
            _clean_source_title(item.get("title"), url)
            .replace("[", "(")
            .replace("]", ")")
        )
        if title and title != url:
            lines.append(f"- [{title}]({url})")
        else:
            lines.append(f"- {url}")
    return lines


def _format_sources_lines_plain(sources: list[dict[str, str]]) -> list[str]:
    """Plain bullets for Word/PDF — no markdown link syntax in the file body."""
    lines: list[str] = []
    for item in _dedupe_sources(sources):
        url = str(item.get("url") or "").strip()
        if not url or _is_image_asset_url(url):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        title = _clean_source_title(item.get("title"), url)
        if title and title != url:
            lines.append(f"- {title} ({url})")
        else:
            lines.append(f"- {url}")
    return lines



# The suffix (lar/ça/ca) is required, not optional: a bare "Kaynak: TÜİK"
# is an inline citation ("Source: TÜİK"), not a Kaynaklar/Kaynakça section
# header. Making it optional matched that inline form too -- live bug: a
# report's first line was "Kaynak: Kültür ve Turizm Bakanlığı / TÜİK
# (...)" and _strip_kaynaklar_section read that as the section header,
# discarding the entire body (everything from line 0 onward) and leaving
# only a freshly rebuilt Kaynaklar list with no file-download notice.
_KAYNAKLAR_SECTION = re.compile(
    r"(?is)\n*\s*kaynak(?:lar|ça|ca)\s*:?\s*\n[\s\S]*$"
)
_KAYNAKLAR_HEAD_LINE = re.compile(
    r"(?im)^\s*kaynak(?:lar|ça|ca)\s*:?\s*$"
)

_CAPABILITY_REFUSAL_RE = re.compile(
    r"(?is)(?:^|\n+)[^\n]{0,500}?(?:"
    r"PDF\s+dosyas[ıi]\s+olu[şs]turam[ıi]yorum|"
    r"PDF\s+olu[şs]turam[ıi]yorum|"
    r"dosya\s+olu[şs]turam[ıi]yorum|"
    r"PDF\s+dosyas[ıi]\s+olu[şs]turma|"
    r"olu[şs]turma\s+ve\s+g[öo]rsel\s+ekleme|"
    r"g[öo]rsel\s+ekleme\s+(?:i[sş]lemi|imk[aâ]n)|"
    r"sistemin\s+g[öo]revi\s+kapsam[ıi]nda\s+de[ğg]il|"
    r"g[öo]revi\s+kapsam[ıi]nda\s+de[ğg]ildir|"
    r"belge\s+d[üu]zenleyicide\s+kendiniz|"
    r"kendiniz\s+olu[şs]turabilirsiniz|"
    r"g[öo]rsel\s+ekleme\s+imk[aâ]n[ıi]m\s+bulunmamakt|"
    r"olu[şs]turma\s+(?:ve\s+g[öo]rsel\s+ekleme\s+)?imk[aâ]n|"
    r"kendi\s+(?:ara[cç]lar[ıi]n[ıi]zla|belgenizi)|"
    r"PDF['’]?ye\s+d[öo]n[üu][şs]t[üu]rebilir|"
    r"yap[ıi]land[ıi]r[ıi]lm[ıi][şs]\s+metni\s+(?:bir\s+)?rapor\s+tasla[ğg]|"
    r"a[şs]a[ğg][ıi]daki\s+yap[ıi]land[ıi]r[ıi]lm[ıi][şs]\s+metni|"
    r"rapor\s+i[cç]in\s+[öo]nerilen\s+yap[ıi]|"
    r"[öo]nerilen\s+yap[ıi]\s*:|"
    r"cannot\s+create\s+(?:a\s+)?(?:pdf|file|docx|word)|"
    r"I\s+cannot\s+create\s+(?:a\s+)?(?:pdf|file)|"
    r"unable\s+to\s+create\s+(?:a\s+)?(?:pdf|file)|"
    r"do\s+not\s+have\s+(?:the\s+)?(?:ability|capability)\s+to\s+create|"
    r"not\s+(?:part\s+of|within)\s+(?:this\s+)?(?:system'?s\s+)?(?:scope|task)|"
    r"use\s+the\s+(?:structured\s+)?text\s+below\s+as\s+a\s+(?:report\s+)?draft|"
    r"suggested\s+structure\s+for\s+(?:the\s+)?report|"
    r"create\s+(?:the\s+)?(?:document|pdf)\s+yourself"
    r")[^\n]*"
)


def _strip_capability_refusals(text: str) -> str:
    """Drop 'I cannot create PDF/files' lines from research/document prose.

    Research sometimes refuses deliverables even when Document Agent will
    write the file; those lines then leak into chat and into the PDF body.
    """
    value = (text or "").strip()
    if not value:
        return ""
    cleaned = _CAPABILITY_REFUSAL_RE.sub("\n", value)
    # Also drop whole paragraphs that still tell the user to DIY the file.
    kept: list[str] = []
    for para in re.split(r"\n{2,}", cleaned):
        blob = para.casefold()
        if any(
            marker in blob
            for marker in (
                "oluşturamıyorum",
                "olusturamiyorum",
                "görevi kapsamında değil",
                "gorevi kapsaminda degil",
                "kendiniz oluştur",
                "kendiniz olustur",
                "belge düzenleyicide",
                "belge duzenleyicide",
                "cannot create",
                "create the pdf yourself",
                "create the document yourself",
                "not within the scope",
                "not part of this system",
            )
        ):
            continue
        kept.append(para.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(p for p in kept if p)).strip()


# A worker that ended up without web tools claims the *system* cannot reach
# the internet. That is false — this backend searches and opens pages — and
# it reads to the user as a broken product rather than a run that misrouted.
_WEB_ACCESS_REFUSAL_RE = re.compile(
    r"(?is)(?:"
    r"internet(?:e|'e)?\s+eri[şs]im|"
    r"canl[ıi]\s+veri(?:lere)?\s+eri[şs]|"
    r"g[üu]ncel\s+(?:veri|bilgi|haber)\w*\s+eri[şs]|"
    r"eri[şs]imim(?:in)?\s+(?:yok|olmamas|bulunm)|"
    r"hi[çc]bir\s+ara[çc]\s+bulunmamakt|"
    r"ara[çc]\s+setim\s*\((?:\s*hi[çc]bir[^)]*)\)|"
    r"web'?de\s+arama\s+yapam|"
    r"cannot\s+access\s+(?:the\s+)?(?:internet|web|live|real[-\s]?time)|"
    r"do\s+not\s+have\s+access\s+to\s+(?:the\s+)?(?:internet|live|real[-\s]?time)|"
    r"no\s+tools?\s+(?:are\s+)?available"
    r")"
)

_WEB_ACCESS_REFUSAL_NOTICE = (
    "Bu istek için canlı web araması çalıştırılamadı, bu yüzden güncel "
    "bilgi getirilemedi. Lütfen tekrar deneyin."
)


def _looks_like_web_access_refusal(text: str) -> bool:
    """Whether the answer is mostly a false 'I have no internet' claim.

    Bounded to short answers on purpose: a real brief that merely notes one
    page was unreachable must keep its findings.
    """
    body = (text or "").strip()
    if not body or len(body) > 1200:
        return False
    return bool(_WEB_ACCESS_REFUSAL_RE.search(body))


# A brief that ends with "here is what your slides should say" is describing
# the deliverable instead of being it. Document turned that section into
# real slides, so a Cappadocia deck carried "6. PowerPoint Sunumu İçin
# Slayt İçerikleri", "Slayt 1 – Başlık", "Slayt 2 – Bölge Tanıtımı" as
# pages of its own -- a presentation about how to build the presentation.
# Same family as the "rapor için önerilen yapı" prose already stripped
# above; this one is a whole section, so it needs the section removed.
_SLIDE_PLAN_HEADING_RE = re.compile(
    r"(?im)^(#{1,6})\s*(?=[^\n]*\bslayt|[^\n]*\bslide)"
    r"(?=[^\n]*(?:i[çc]erik|haz[ıi]r\s*metin|plan[ıi]?|taslak|"
    r"content|outline|deck\s+text))[^\n]*$"
)


def _strip_slide_plan_section(text: str) -> str:
    """Drop a section that scripts the deliverable rather than filling it."""
    body = text or ""
    match = _SLIDE_PLAN_HEADING_RE.search(body)
    if not match:
        return body
    level = len(match.group(1))
    tail = body[match.end() :]
    # Ends where the next heading at the same or higher level begins; a
    # deeper one ("### Slayt 2") is part of what we are removing.
    nxt = re.search(rf"(?m)^#{{1,{level}}}\s+\S", tail)
    rest = tail[nxt.start() :] if nxt else ""
    head = body[: match.start()].rstrip()
    # The "---" rule that introduced the dropped section would dangle.
    head = re.sub(r"(?m)\n+\s*-{3,}\s*$", "", head).rstrip()
    return f"{head}\n\n{rest.lstrip()}".strip() if rest else head


def _strip_kaynaklar_section(value: str) -> str:
    """Remove a trailing Kaynaklar / Kaynakça block (colon optional)."""
    text = value or ""
    if _KAYNAKLAR_SECTION.search(text):
        return _KAYNAKLAR_SECTION.sub("", text).rstrip()
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if _KAYNAKLAR_HEAD_LINE.match(line.strip()) or re.match(
            r"(?im)^\s*kaynak(?:lar|ça|ca)\s*:", line.strip()
        ):
            return "\n".join(lines[:idx]).rstrip()
    return text.rstrip()


def _is_ranking_data_table(columns: Any) -> bool:
    """True when columns look like a user-requested ranking grid (not Konu/Detay)."""
    if not isinstance(columns, list) or not columns:
        return False
    joined = " ".join(str(c or "").casefold() for c in columns)
    return any(
        token in joined
        for token in (
            "sıra",
            "sira",
            "oyuncu",
            "player",
            "takım",
            "takim",
            "team",
            "gol",
            "goal",
            "puan",
            "rank",
        )
    )


_MD_TABLE_BLOCK = re.compile(
    r"(?ms)(?:^[ \t]*\|.+\|[ \t]*\n)+",
)


def _strip_markdown_tables(text: str) -> str:
    """Remove GFM pipe tables so Word does not render the same grid twice."""
    cleaned = _MD_TABLE_BLOCK.sub("\n", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


_FILE_NOTICE_TAIL = re.compile(
    r"(?is)\n*(?:---\s*\n+)?İndirilebilir dosya\(lar\) oluşturuldu:[\s\S]*$"
)


def _present_workforce_answer(text: str) -> str:
    """Chat-facing answer: strip model Kaynaklar; append Research URLs.

    File-handoff lines stay *after* Kaynaklar so the brief reads like OpenBot
    (title → table → sources → download), not sources buried under the file card.
    """
    had_marker = _RESEARCH_SOURCES_MARKER in (text or "")
    cleaned, sources = _split_research_sources(text)
    if not sources:
        sources = _urls_from_notes(cleaned)
    cleaned = _strip_capability_refusals(_strip_kaynaklar_section(cleaned))
    file_notice = ""
    match = _FILE_NOTICE_TAIL.search(cleaned)
    if match:
        file_notice = match.group(0).strip()
        cleaned = cleaned[: match.start()].rstrip()
    cleaned = _strip_capability_refusals(cleaned)
    # Never ship the "I have no internet access" claim as the answer: it is
    # untrue of this backend and hides that the run simply never searched.
    if not sources and _looks_like_web_access_refusal(cleaned):
        cleaned = _WEB_ACCESS_REFUSAL_NOTICE
    lines = _format_sources_lines(sources)
    if lines:
        out = f"{cleaned.rstrip()}\n\nKaynaklar:\n" + "\n".join(lines)
    elif had_marker and cleaned.strip():
        out = (
            f"{cleaned.rstrip()}\n\nKaynaklar:\n"
            "Doğrulanmış sayfa URL'si bulunamadı."
        )
    else:
        out = cleaned
    if file_notice:
        out = f"{out.rstrip()}\n\n{file_notice}"
    return out


def _inject_sources_into_payload(
    payload: dict[str, Any],
    sources: list[dict[str, str]],
    *,
    required: bool = True,
) -> None:
    """Put verified research URLs into the deliverable payload.

    When ``required`` is True, Research URLs win. Model-written Kaynaklar
    (domain-only names, invented lists) are always stripped and replaced
    with full ``https://`` addresses. Draft http(s) links are a fallback
    only when Research returned none. When False, do nothing.

    Sources always go in the prose block only — never as a fake last table
    row (that produced a Konu/Detay "Kaynaklar" grid at the end of Word).
    """
    if not required:
        return
    harvested: list[dict[str, str]] = []
    for key in ("summary", "content", "body"):
        harvested.extend(_urls_from_notes(str(payload.get(key) or "")))
    # Prefer Research evidence URLs; never leave domain-only model lists.
    merged = list(sources) if sources else harvested
    lines = _format_sources_lines_plain(merged)
    if lines:
        block = "Kaynaklar:\n" + "\n".join(lines)
    else:
        block = (
            "Kaynaklar:\n"
            "Doğrulanmış (açılmış) sayfa URL'si bulunamadı. "
            "Araştırma sırasında erişilen kanıt sayfası yok."
        )

    for key in ("summary", "content", "body"):
        value = str(payload.get(key) or "")
        if value:
            payload[key] = _strip_kaynaklar_section(value)

    for key in ("summary", "content", "body"):
        value = str(payload.get(key) or "")
        if value:
            payload[key] = re.sub(
                r"(?im)^\s*kaynakça\s*:?\s*.{0,120}(?:bulunmamaktadır|bulunamadı).*$",
                "",
                value,
            ).rstrip()

    summary = str(payload.get("summary") or "").strip()
    body = str(
        payload.get("content") or payload.get("body") or summary or ""
    ).strip()
    payload["summary"] = f"{summary}\n\n{block}".strip() if summary else block
    payload["content"] = (
        f"{body}\n\n{block}".strip() if body else payload["summary"]
    )
    payload["body"] = payload["content"]

    # Drop any model-written Kaynaklar row from data tables.
    rows = payload.get("rows")
    if isinstance(rows, list):
        payload["rows"] = [
            row
            for row in rows
            if not (
                isinstance(row, (list, tuple))
                and row
                and str(row[0] or "").strip().casefold()
                in {"kaynaklar", "kaynakça", "kaynakca", "sources", "kaynak"}
            )
        ]


def _sanitize_document_payload(
    payload: dict[str, Any], *, keep_bold: bool = False
) -> None:
    """Clean title/summary/body so downloads are not chat dumps.

    ``keep_bold`` is for the uploaded-template writer, the only one that
    renders **markers** as real bold runs. Stripping them here meant the
    model's emphasis never reached it however carefully it was asked for.
    """
    import document_export

    for key in ("summary", "content", "body"):
        if key in payload and payload.get(key) is not None:
            payload[key] = document_export.sanitize_report_prose(
                str(payload.get(key) or ""), keep_bold=keep_bold
            )
    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    body = str(
        payload.get("content") or payload.get("body") or summary or ""
    ).strip()
    # Always scan title + body together so a bad JSON "title" (thinking leak)
    # can be replaced by a real heading inside the narrative.
    blended = "\n".join(part for part in (title, summary, body) if part)
    payload["title"] = document_export.short_report_title(
        blended,
        fallback="Rapor",
    )


def _trim_evidence_for_answer(
    evidence: list[dict[str, Any]], *, max_items: int = 6, max_text: int = 2000
) -> list[dict[str, Any]]:
    """Keep ANSWER prompt small when many recovery pages accumulated.

    Pages that returned content come first. This used to keep the LAST
    ``max_items`` entries regardless of outcome, so a refine or recovery
    round whose fetches failed pushed the first round's good pages out of
    the prompt: the model then said the facts were missing, which fired
    another recovery round -- slower and worse. Failed entries still fill
    any room left, so the model can say which pages did not load.
    """
    items = list(evidence or [])
    usable = [item for item in items if item.get("ok")]
    if len(usable) > max_items:
        # Newest usable pages win, as before -- recovery rounds are the
        # more targeted searches.
        keep = {id(item) for item in usable[-max_items:]}
    else:
        failed = [item for item in items if not item.get("ok")]
        room = max_items - len(usable)
        keep = {id(item) for item in usable}
        keep.update(id(item) for item in (failed[-room:] if room else []))
    trimmed: list[dict[str, Any]] = []
    for item in (item for item in items if id(item) in keep):
        copy = dict(item)
        text = str(copy.get("text") or "")
        if len(text) > max_text:
            copy["text"] = text[:max_text]
        trimmed.append(copy)
    return trimmed


def _cheap_recovery_lookup(
    original_lookup: str,
    opened: list[str],
    evidence: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Template recovery brief — no extra LLM call for a new query.

    Includes short excerpts so SEARCH can name opponent/date/etc. the way
    Camel's search→read→refine loop did, without a second propose-query call.

    Must hold no URL at all: acquire_web_evidence takes any URL in its text
    as one the user typed and fetches it WITHOUT searching. Listing the
    opened pages here by URL turned every recovery pass into a re-fetch of
    pages already read (served from cache), then a second ANSWER over the
    same evidence. Opened pages are named by host only; the callers keep
    them out of SELECT with ``exclude_urls``.
    """
    hosts = list(
        dict.fromkeys(
            urlsplit(u).netloc.casefold() for u in opened if urlsplit(u).netloc
        )
    )
    opened_block = "\n".join(f"- {h}" for h in hosts[:8]) or "(none)"
    learned_lines: list[str] = []
    for item in (evidence or [])[-4:]:
        if not item.get("ok"):
            continue
        title = web_fetch._URL_RE.sub("", str(item.get("title") or "")).strip()
        text = web_fetch._URL_RE.sub(
            "", str(item.get("text") or "")
        ).strip().replace("\n", " ")
        if len(text) > 220:
            text = text[:220] + "…"
        learned_lines.append(f"- {title}: {text}")
    learned = "\n".join(learned_lines) or "(none)"
    return (
        f"{original_lookup}\n\n"
        "RECOVERY: Previous pages lacked the exact facts asked. "
        "Write a MORE SPECIFIC search using details below (names, dates, "
        "scores, versions) plus the exact ask. Open DIFFERENT "
        "detail/report/article HTML pages (not list/overview/homepage, "
        "not .pdf downloads). For ihracat/ithalat asks prefer a different "
        "on-topic bulletin/news page with published figures (association, "
        "TIM, TÜİK HTML, trade press) — not Wikipedia. Pages already "
        "opened on these sites were not enough; prefer other sources:\n"
        f"{opened_block}\n\n"
        f"LEARNED FROM OPENED PAGES:\n{learned}"
    )


def _research_handoff_output(prose: str, handoff: Optional[dict[str, Any]]) -> str:
    """The text a wants_artifact research pass hands to Document/chat.

    A handoff (pointer to an extracted data table) is only ever useful
    ALONGSIDE real narrative prose -- never on its own. Returning a bare
    handoff_json when prose is empty broke every downstream
    ARTIFACT_HANDOFF-marker-based split (sources insertion in _run_research's
    caller, Document Agent's prompt, _split_research_sources), which then
    treated raw internal JSON as if it were the visible answer (real bug: a
    Galata Kulesi PowerPoint request got a chat reply that was literally
    `{"status": "handoff", ...}` followed by the Kaynaklar list, and
    Document had only one narrow, tangential table to build a deck from)."""
    if handoff and prose:
        handoff_json = json.dumps(
            {"status": "handoff", "handoff": handoff, "evidence_summary": prose},
            ensure_ascii=False,
        )
        return f"{prose}\n\n---\nARTIFACT_HANDOFF:\n{handoff_json}"
    return prose or "No usable page content was acquired for this request."


async def _run_research(
    *,
    content: str,
    original_request: str,
    tracker: UsageTracker,
    callback: Any,
    workspace_root: Optional[str],
    tool_call_recorder: Optional[ToolCallRecorder],
    wants_artifact: bool,
    handle: CancellableWorkforce,
    unit_task_id: str,
    deep_research: bool = False,
) -> str:
    if handle.cancelled:
        return ""
    # CRITICAL: never put conversation-bloated ``content`` into SEARCH.
    lookup = _web_lookup_text(original_request, content)
    lookup = _enrich_lookup_for_research(lookup)
    answer_text = (original_request or lookup).strip()
    # Only run source-recovery when the user asked for citations — forcing
    # this for every PDF/Word job added multi-minute recovery searches.
    wants_sources = _request_wants_sources(original_request or content)
    worker_id = WORKER_IDS[ROLE_RESEARCH]
    _lifecycle_start(
        callback,
        role=ROLE_RESEARCH,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        description=lookup or content,
    )
    started = time.monotonic()

    def progress(stage: str, detail: str = "") -> None:
        _emit_activity(callback, unit_task_id, stage, detail)

    ctx = capabilities.ToolContext(
        workspace_root=workspace_root,
        tool_call_recorder=tool_call_recorder,
        progress=progress,
        grounding_text=lookup,
        expose_web_tools=False,
        binding_source="langgraph_research",
    )
    capabilities.resolve_tools(
        {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}, ctx
    )
    web = ctx._shared.get("web_tools")
    if web is None:
        text = "Web tools could not be bound for Research Agent."
        _lifecycle_done(
            callback,
            worker_id=worker_id,
            unit_task_id=unit_task_id,
            result_summary=text,
            started=started,
        )
        return text
    # capabilities.resolve_tools already wraps its OWN returned tool list
    # with the recorder, but that list is for model function-calling and is
    # discarded above -- this deterministic path calls the raw shared
    # instance's methods directly, which bypassed it entirely (the "Tool
    # activity" panel showed nothing for every LangGraph research run,
    # regardless of how many real searches/fetches happened).
    search_raw = web.search_raw
    read_page = web.read_page
    if tool_call_recorder is not None:
        search_raw = logged_tool(search_raw, tool_call_recorder)
        read_page = logged_tool(read_page, tool_call_recorder)

    complete = await _make_complete(tracker, deep_research=deep_research)
    date_line = runtime_context.now_local().strftime("%A, %d %B %Y")
    # Survey stays on for deep-research asks even when the deliverable is
    # PDF/Word — that is the Maven/report path. Speed is controlled by
    # tighter survey budgets when wants_artifact (see web_controller), not
    # by disabling survey entirely.
    # "deep" is the user's explicit opt-in (never phrase-triggered): the
    # same plan->search->read->coverage survey shape, but with
    # MAX_SEARCH_PHASES_DEEP / MAX_FETCHES_DEEP budgets (web_controller.py)
    # instead of the quick default, and it overrides wants_artifact's
    # tighter survey budget too -- an explicit ask for thoroughness should
    # not get quietly capped just because the deliverable is a file.
    if deep_research:
        research_depth = "deep"
        progress(
            "searching",
            "deep research: expanded search/fetch budget (opt-in)",
        )
    elif capabilities.detect_deep_research(original_request or lookup):
        research_depth = "survey"
    else:
        research_depth = "lookup"
    # Same target rules as Camel workforce_plan: independent lookup subjects
    # each get their own SEARCH. Empty list → single search (one subject).
    # Survey already plans angles — do not double-plan targets there.
    lookup_targets: list[str] = []
    if research_depth == "lookup":
        lookup_targets = await _extract_lookup_targets(
            original_request or lookup, complete=complete
        )
        if lookup_targets:
            progress(
                "searching",
                "targets: " + ", ".join(lookup_targets)[:160],
            )

    async def _acquire(
        lookup_text: str,
        *,
        allow_refine: bool = True,
        compact: bool = False,
        targets: Optional[list[str]] = None,
        exclude_urls: Optional[list[str]] = None,
    ) -> Any:
        return await web_controller.acquire_web_evidence(
            lookup_text,
            exclude_urls=exclude_urls,
            # Named, not positional -- so the recorded args carry "query"/
            # "url" instead of a bare positional tuple, which is what the
            # frontend's Tool activity panel looks for to show a preview.
            search=lambda query, max_results: search_raw(
                query=query, max_results=max_results
            ),
            fetch=lambda url: read_page(url=url),
            complete=complete,
            progress=progress,
            current_date=date_line,
            wants_artifact=wants_artifact,
            targets=targets,
            allow_refine=allow_refine and not compact,
            depth=research_depth,
            compact=compact,
            on_timing=(
                (
                    lambda name, seconds, detail: tracker.record_event(
                        name, name, seconds, detail=detail
                    )
                )
                if tracker is not None
                else None
            ),
        )

    # Set when the first acquisition ran the iterative deep-research loop:
    # its final report, every URL the loop opened, and the ones it cites.
    iter_run: dict[str, Any] = {}

    async def _answer(
        evidence: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Returns (answer text, the evidence subset actually shown to the
        model). Callers must build the citation list from that subset, not
        from the full ``evidence`` -- otherwise a page trimmed out of the
        prompt (see below) can still end up cited as a source for facts the
        model never actually saw, while the model itself (correctly, from
        its own narrower view) reports those facts as missing. This was a
        real, verified bug: a Turkey EV-market deep-research run cited a
        page stating TOGG sold 109,944 units, but the answer said sales
        figures could not be found -- the page was trimmed out of what the
        model read, yet still passed the (separate, untrimmed) citation
        filter."""
        report = str(iter_run.get("report") or "")
        if report:
            # Iterative deep research already read every page in full and
            # folded it into the report's cited findings. The answer gets
            # that report plus only the pages it cites (and pages no report
            # round saw: the loop's last round, recovery passes), each with
            # more room than the untargeted 40-page trim below allows.
            loop_urls = iter_run["urls"]
            cited_urls = iter_run["cited_urls"]
            compact = _trim_evidence_for_answer(
                [
                    item
                    for item in evidence
                    if str(item.get("url") or "") in cited_urls
                    or str(item.get("url") or "") not in loop_urls
                ],
                max_items=40,
                max_text=2500,
            )
        # Deep mode can gather dozens of pages -- the default 6-item/2000-char
        # trim would silently throw away most of that research at the final
        # synthesis step, discarding the whole point of the wider budget.
        elif deep_research:
            compact = _trim_evidence_for_answer(evidence, max_items=40, max_text=1200)
        else:
            compact = _trim_evidence_for_answer(evidence)
        ask = answer_text
        if wants_artifact:
            # Research must NOT refuse file creation — Document Agent writes
            # the Word/HTML next. Without this, notes become a refusal and
            # Document gets empty rows → ArtifactError.
            ask = (
                f"{answer_text}\n\n"
                "CRITICAL FOR THIS JOB: A Document Agent WILL create the "
                "real PDF/Word/PowerPoint file after you. Your only job is "
                "a thorough research brief from the EVIDENCE below — actual "
                "facts, figures, tables, and section prose that can be "
                "copied into the file. "
                "FORBIDDEN: saying you cannot create PDF/files/downloads; "
                "offering a 'suggested structure' / 'rapor için önerilen "
                "yapı' / 'taslak olarak kullanın' instead of real content; "
                "telling the user to convert text to PDF themselves. "
                "Write the report substance now (numbers + analysis from "
                "evidence), not instructions about how someone else should "
                "write it."
            )
            if _request_wants_structured_table(answer_text):
                ask = (
                    f"{ask}\n\n"
                    "STRUCTURED TABLE REQUIRED: The user named columns "
                    "(e.g. sıra/oyuncu/takım/gol). Put a GitHub-flavored "
                    "Markdown table in the brief with EVERY requested "
                    "column. Do not drop Takım/Team because one page omits "
                    "it — pull the club from another opened evidence page "
                    "when present. Empty cell only if no opened page has "
                    "that value; then note the gap in one short sentence "
                    "under the table."
                )
        if wants_sources:
            ask = (
                f"{ask}\n\n"
                "The user asked for sources. Cite facts from the evidence "
                "below only. Do NOT write a Kaynaklar list yourself and do "
                "NOT invent domain names — the system appends verified full "
                "https:// URLs of opened/selected pages at the end."
            )
        else:
            ask = (
                f"{ask}\n\n"
                "Do NOT invent a Kaynaklar/Sources section or domain-only "
                "citations (e.g. example.com). The system appends verified "
                "full https:// URLs from the evidence pages at the end."
            )
        if wants_artifact:
            handoff = web_controller._gated_handoff(compact, wants_artifact=True)
            prose = await web_controller.answer_from_evidence(
                ask,
                compact,
                complete=complete,
                current_date=date_line,
                depth=research_depth,
                report=report,
            )
            prose = _strip_capability_refusals(prose or "")
            return (_research_handoff_output(prose, handoff), compact)
        prose = await web_controller.answer_from_evidence(
            ask,
            compact,
            complete=complete,
            current_date=date_line,
            depth=research_depth,
            report=report,
        )
        return (
            (
                _strip_capability_refusals(prose)
                if prose
                else "No usable page content was acquired for this request."
            ),
            compact,
        )

    def _merge(into: list[dict[str, Any]], more: list[dict[str, Any]]) -> None:
        seen = {str(i.get("url") or "") for i in into}
        for item in more or []:
            url = str(item.get("url") or "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            into.append(item)

    merged: list[dict[str, Any]] = []
    controller_urls: list[dict[str, str]] = []
    output = ""
    # The evidence subset the model actually saw when it produced the
    # current `output` -- NOT all of `merged`. Citations are built from
    # this, not from `merged`, so a page trimmed out of the answer prompt
    # can never still show up as a source for facts the model never read.
    answer_evidence: list[dict[str, Any]] = []
    try:
        # First pass: allow refine so stats PDF jobs can pivot to a
        # figure-bearing page within the artifact search budget.
        # Named targets (from workforce_plan rules) drive per-subject SEARCH.
        acquired = await _acquire(
            lookup, allow_refine=True, targets=lookup_targets or None
        )
        if handle.cancelled:
            return ""
        if getattr(acquired, "report", ""):
            iter_run.update(
                report=acquired.report,
                urls={str(i.get("url") or "") for i in acquired.evidence or []},
                cited_urls={
                    str(i.get("url") or "") for i in acquired.report_evidence or []
                },
            )
            progress(
                "searching",
                f"deep research: report built over {acquired.iter_rounds} rounds, "
                f"{len(acquired.report_evidence)} pages cited",
            )
        _merge(merged, list(acquired.evidence or []))
        controller_urls = _merge_source_lists(
            controller_urls, _research_sources_from_controller(acquired)
        )

        recoveries = 0
        # Thin first pass → one cheap recovery (no nested refine).
        # Do NOT skip recovery just because search returned hit URLs —
        # unread/timed-out PDF links are not evidence. Artifact jobs used
        # to skip here and then Document wrote a "nothing opened" PDF after
        # burning minutes on failed fetches.
        if (
            not handle.cancelled
            and recoveries < _MAX_RECOVERY_PASSES
            and not _substantive_evidence(merged)
        ):
            progress("searching", "recovery: thin evidence")
            acquired_n = await _acquire(
                _cheap_recovery_lookup(
                    lookup, _opened_urls(merged), merged
                ),
                allow_refine=False,
                compact=True,
                exclude_urls=_opened_urls(merged),
            )
            recoveries += 1
            if not handle.cancelled:
                _merge(merged, list(acquired_n.evidence or []))
                controller_urls = _merge_source_lists(
                    controller_urls,
                    _research_sources_from_controller(acquired_n),
                )

        output, answer_evidence = await _answer(merged)

        # Extra pass when the answer admits the asked facts are missing —
        # including artifact/PDF jobs (previously skipped → empty "report" PDF).
        # Do not skip because a page merely repeats a place/entity name
        # (city listings, club fixtures are not the asked timetable/list).
        if (
            not handle.cancelled
            and recoveries < _MAX_RECOVERY_PASSES
            and _answer_looks_insufficient(output)
        ):
            progress("searching", "recovery: insufficient answer")
            acquired_n = await _acquire(
                _cheap_recovery_lookup(
                    lookup, _opened_urls(merged), merged
                ),
                allow_refine=False,
                compact=True,
                exclude_urls=_opened_urls(merged),
            )
            recoveries += 1
            if not handle.cancelled:
                _merge(merged, list(acquired_n.evidence or []))
                controller_urls = _merge_source_lists(
                    controller_urls,
                    _research_sources_from_controller(acquired_n),
                )
                output, answer_evidence = await _answer(merged)
        elif (
            wants_artifact
            and not handle.cancelled
            and recoveries < _MAX_RECOVERY_PASSES
            and not _opened_citation_sources(merged)
        ):
            progress("searching", "recovery: missing source URLs")
            acquired_n = await _acquire(
                _cheap_recovery_lookup(
                    lookup, _opened_urls(merged), merged
                ),
                allow_refine=False,
                compact=True,
                exclude_urls=_opened_urls(merged),
            )
            recoveries += 1
            if not handle.cancelled:
                _merge(merged, list(acquired_n.evidence or []))
                controller_urls = _merge_source_lists(
                    controller_urls,
                    _research_sources_from_controller(acquired_n),
                )
                if _substantive_evidence(merged):
                    output, answer_evidence = await _answer(merged)
    except Exception as exc:  # noqa: BLE001
        stabilization_logger.exception("LangGraph research node failed")
        output = f"Research failed: {type(exc).__name__}: {exc}"

    # answer_evidence (what the model actually read) is empty only if an
    # exception fired before any _answer() call completed -- output is then
    # an error string anyway, so falling back to the full merged set here
    # matches the old (pre-fix) behavior rather than citing nothing.
    sources_block = _research_sources_block(answer_evidence or merged, controller_urls)
    if output:
        # Put RESEARCH_SOURCES *before* ARTIFACT_HANDOFF so Document Agent
        # (and any strip-handoff-first parser) still sees the URL list.
        if _ARTIFACT_HANDOFF_MARKER in output:
            head, mid, tail = output.partition(_ARTIFACT_HANDOFF_MARKER)
            output = head.rstrip() + sources_block + mid + tail
        else:
            output = output.rstrip() + sources_block

    _lifecycle_done(
        callback,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        result_summary=output,
        started=started,
    )
    return output


_ROLE_PERSONAS = {
    ROLE_DOCUMENT: (
        "You are a document specialist. Produce clear, well-structured "
        "output from the material provided. Prefer concrete filenames and "
        "facts over vague claims."
    ),
    ROLE_CODING: (
        "You are a coding specialist. Write or explain code needed to "
        "complete the task. Be concrete. "
        "FORBIDDEN: claiming you saved, created, or attached a "
        "downloadable file, or inventing a download link/button/path "
        "(e.g. 'sandbox:/...', '[📥 file.xlsx](...)') — you have no file "
        "system or download mechanism here; any such claim is fabricated "
        "and the link will not work. If the task needs a real "
        "downloadable file, just write the complete code/content as "
        "plain text in your answer — a separate step packages it into "
        "an actual file. "
        "ALSO FORBIDDEN: reporting what the code printed. You have no "
        "execution here either, so an 'Output:' / 'Çıktı:' block, a "
        "results table, or a sentence like 'when we run this we get X' "
        "is a guess dressed as a run — and it has been wrong while the "
        "code beside it was right, which is worse than saying nothing. "
        "End with the code and, if useful, one line on what running it "
        "will show (\"prints the mean and standard deviation\"). Never "
        "the values themselves."
    ),
    ROLE_GENERAL: "You are a capable assistant. Complete the task thoroughly.",
}
_DEFAULT_ROLE_PERSONA = "You are a capable assistant. Complete the task thoroughly."


async def _run_mail(
    *,
    content: str,
    original_request: str,
    tracker: UsageTracker,
    callback: Any,
    workspace_root: Optional[str],
    tool_call_recorder: Optional[ToolCallRecorder],
    handle: CancellableWorkforce,
    unit_task_id: str,
    deep_research: bool = False,
) -> str:
    """Mail Agent: read the mailbox, return the evidence for the next worker.

    Structurally the same as ``_run_research``: a fixed phase sequence in
    mail_controller, not a tool the model may call. The return value is
    notes, never the user-facing answer -- see the ROLE_MAIL guard in
    _select_roles for why that distinction is enforced rather than assumed.
    """
    import mail_controller

    worker_id = WORKER_IDS[ROLE_MAIL]
    started = time.monotonic()
    _lifecycle_start(
        callback,
        role=ROLE_MAIL,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        description="Posta kutusunu oku",
    )
    _emit_activity(callback, unit_task_id, "searching", "posta kutusu taranıyor")

    # stage_prefix="mail": these phases cost tokens like web ones do, and the
    # usage panel should say which of the two spent them.
    complete = await _make_complete(
        tracker, deep_research=deep_research, stage_prefix="mail"
    )
    try:
        result = await mail_controller.acquire_mail_evidence(
            original_request or content,
            complete=complete,
            deep_research=deep_research,
        )
    except Exception as exc:  # noqa: BLE001 - a mail failure is an answer, not a crash
        stabilization_logger.exception("mail acquisition failed")
        _lifecycle_done(
            callback,
            worker_id=worker_id,
            unit_task_id=unit_task_id,
            result_summary="posta kutusu okunamadı",
            started=started,
        )
        return mail_controller.render_notes(
            mail_controller.MailResult(
                error=f"{type(exc).__name__}: {exc}", error_code="mail_failed"
            )
        )

    if tool_call_recorder is not None:
        # Recorded like any other real action, so the right panel and the
        # persisted tool log show that this job read mail, what it searched
        # for, and how much came back. A feature this sensitive should never
        # be invisible in the audit trail.
        try:
            tool_call_recorder.record(
                "read_mailbox",
                {
                    "folders": list(result.query.folders) if result.query else [],
                    "sender": result.query.sender if result.query else "",
                    "subject": result.query.subject if result.query else "",
                    "unread_only": bool(result.query.unread_only)
                    if result.query
                    else False,
                },
                result=(
                    f"{len(result.messages)} message(s) read of "
                    f"{result.searched} scanned"
                    if result.ok
                    else None
                ),
                error=None if result.ok else str(result.error_code or "mail_failed"),
            )
        except Exception:  # noqa: BLE001 - audit must not break the run
            stabilization_logger.warning("could not record read_mailbox", exc_info=True)

    if handle.cancelled:
        return mail_controller.render_notes(result)

    if result.ok and result.messages:
        _emit_activity(
            callback,
            unit_task_id,
            "reading",
            f"{len(result.messages)} e-posta okundu",
        )
        # Attachments last: the body alone already answers most requests, so
        # a download failure here must not cost the messages already read.
        try:
            result.attachments = await mail_controller.download_attachments(
                result, workspace_root
            )
        except Exception:  # noqa: BLE001
            stabilization_logger.warning("mail attachment download failed", exc_info=True)

    summary = (
        f"{len(result.messages)} e-posta"
        if result.ok
        else f"okunamadı ({result.error_code})"
    )
    _lifecycle_done(
        callback,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        result_summary=summary,
        started=started,
    )
    return mail_controller.render_notes(result, request_text=original_request)


async def _run_social(
    *,
    content: str,
    original_request: str,
    tracker: UsageTracker,
    callback: Any,
    tool_call_recorder: Optional[ToolCallRecorder],
    handle: CancellableWorkforce,
    unit_task_id: str,
    deep_research: bool = False,
) -> str:
    """Social Agent: read public discussion, return evidence for the next worker.

    Structurally identical to ``_run_mail``: a fixed phase sequence in
    social_controller, not a tool the model may call. The return value is
    notes, never the user-facing answer -- see the ROLE_SOCIAL guard in
    _select_roles. The difference from mail is who wrote the text: a mailbox
    holds messages from people who chose to write to this user, while a
    subreddit holds text from anyone at all, which is why the fence around
    it carries an unguessable per-block marker.
    """
    import social_controller

    worker_id = WORKER_IDS[ROLE_SOCIAL]
    started = time.monotonic()
    _lifecycle_start(
        callback,
        role=ROLE_SOCIAL,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        description="Reddit tartismalarini oku",
    )
    _emit_activity(callback, unit_task_id, "searching", "Reddit taraniyor")

    # stage_prefix="social": these phases cost tokens like the web and mail
    # ones do, and the usage panel should say which of the three spent them.
    complete = await _make_complete(
        tracker, deep_research=deep_research, stage_prefix="social"
    )
    try:
        result = await social_controller.acquire_social_evidence(
            original_request or content,
            complete=complete,
            deep_research=deep_research,
        )
    except Exception as exc:  # noqa: BLE001 - a failure is an answer, not a crash
        stabilization_logger.exception("social acquisition failed")
        _lifecycle_done(
            callback,
            worker_id=worker_id,
            unit_task_id=unit_task_id,
            result_summary="Reddit okunamadi",
            started=started,
        )
        return social_controller.render_notes(
            social_controller.SocialResult(
                error=f"{type(exc).__name__}: {exc}", error_code="social_failed"
            )
        )

    if tool_call_recorder is not None:
        # Recorded like any other real action: which query went out, how much
        # came back, and -- the part worth having in an audit trail -- whether
        # what came back tried to give the model instructions.
        try:
            query = result.query
            tool_call_recorder.record(
                "read_social",
                {
                    "platform": result.platform,
                    "mode": query.mode if query else "",
                    "query": query.query if query else "",
                    "subreddit": query.subreddit if query else "",
                },
                result=(
                    f"{len(result.records)} post(s) read of {result.searched} scanned"
                    if result.ok
                    else None
                ),
                error=None if result.ok else str(result.error_code or "social_failed"),
            )
        except Exception:  # noqa: BLE001 - audit must not break the run
            stabilization_logger.warning("could not record read_social", exc_info=True)

    if handle.cancelled:
        return social_controller.render_notes(result)

    if result.ok and result.records:
        _emit_activity(
            callback,
            unit_task_id,
            "reading",
            f"{len(result.records)} gonderi okundu",
        )

    # render_notes populates result.findings as a side effect, so the summary
    # below is built after it.
    notes = social_controller.render_notes(result, request_text=original_request)
    if result.ok:
        summary = f"{len(result.records)} gonderi"
        if result.findings:
            # Surfaced in the worker list, not just the log: "this run quoted
            # something that tried to steer it" is exactly the kind of thing
            # a user should be able to see without reading server logs.
            summary += " (yonlendirme denemesi iceren icerik var)"
    else:
        summary = f"okunamadi ({result.error_code})"
    _lifecycle_done(
        callback,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        result_summary=summary,
        started=started,
    )
    return notes


async def _run_jira(
    *,
    content: str,
    original_request: str,
    tracker: UsageTracker,
    callback: Any,
    tool_call_recorder: Optional[ToolCallRecorder],
    handle: CancellableWorkforce,
    unit_task_id: str,
    deep_research: bool = False,
) -> str:
    """Jira Agent: search and read the user's Jira, return evidence notes.

    Same structure as ``_run_social``: a fixed phase sequence in
    jira_controller, not a tool the model may call, and the return value is
    notes for the next worker -- never the user-facing answer (see
    _GATHER_ONLY_ROLES). Read-only: jira_client has no write method.
    """
    import jira_controller

    worker_id = WORKER_IDS[ROLE_JIRA]
    started = time.monotonic()
    _lifecycle_start(
        callback,
        role=ROLE_JIRA,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        description="Jira kayıtlarını oku",
    )
    _emit_activity(callback, unit_task_id, "searching", "Jira taranıyor")

    # stage_prefix="jira": its phases cost tokens like the web, mail and
    # social ones, and the usage panel should say which of them spent them.
    complete = await _make_complete(
        tracker, deep_research=deep_research, stage_prefix="jira"
    )
    try:
        result = await jira_controller.acquire_jira_evidence(
            original_request or content,
            complete=complete,
            deep_research=deep_research,
        )
    except Exception as exc:  # noqa: BLE001 - a failure is an answer, not a crash
        stabilization_logger.exception("jira acquisition failed")
        _lifecycle_done(
            callback,
            worker_id=worker_id,
            unit_task_id=unit_task_id,
            result_summary="Jira okunamadı",
            started=started,
        )
        return jira_controller.render_notes(
            jira_controller.JiraResult(
                error=f"{type(exc).__name__}: {exc}", error_code="jira_failed"
            )
        )

    if tool_call_recorder is not None:
        # Recorded like any other real action: which query went out and how
        # much came back. Reading company tickets should never be invisible
        # in the audit trail.
        try:
            tool_call_recorder.record(
                "read_jira",
                {
                    "jql": result.jql,
                    "issue_keys": list(result.query.issue_keys) if result.query else [],
                    "details": [issue.key for issue in result.details],
                },
                result=(
                    f"{len(result.issues)} issue(s) listed, {result.read_count} read"
                    if result.ok
                    else None
                ),
                error=None if result.ok else str(result.error_code or "jira_failed"),
            )
        except Exception:  # noqa: BLE001 - audit must not break the run
            stabilization_logger.warning("could not record read_jira", exc_info=True)

    if handle.cancelled:
        return jira_controller.render_notes(result)

    if result.ok and (result.issues or result.details):
        _emit_activity(
            callback,
            unit_task_id,
            "reading",
            f"{len(result.issues) or result.read_count} Jira kaydı okundu",
        )

    notes = jira_controller.render_notes(result)
    if result.ok:
        summary = f"{len(result.issues)} kayıt listelendi, {result.read_count} kayıt okundu"
        if result.findings:
            summary += " (yönlendirme denemesi içeren içerik var)"
    else:
        summary = f"okunamadı ({result.error_code})"
    _lifecycle_done(
        callback,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        result_summary=summary,
        started=started,
    )
    return notes


async def _run_llm_worker(
    *,
    role: str,
    content: str,
    original_request: str,
    prior_notes: str,
    tracker: UsageTracker,
    callback: Any,
    skills: Optional[list],
    handle: CancellableWorkforce,
    unit_task_id: str,
    system_extra: str = "",
    job_capabilities: Optional[frozenset] = None,
) -> str:
    if handle.cancelled:
        return ""
    worker_id = WORKER_IDS.get(role, WORKER_IDS[ROLE_GENERAL])
    _lifecycle_start(
        callback,
        role=role,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        description=content,
    )
    started = time.monotonic()

    persona = _ROLE_PERSONAS.get(role, _DEFAULT_ROLE_PERSONA)

    caps = job_capabilities or frozenset()
    system = (
        persona
        + render_skill_guidance(skills)
        + runtime_context.runtime_block(caps, request_text=original_request or content)
        + system_extra
    )
    user = content
    if prior_notes:
        user = (
            f"{content}\n\n---\nPrior worker notes (use as evidence, do not "
            f"ignore):\n{prior_notes}"
        )
    if original_request and original_request != content:
        user = f"Original request:\n{original_request}\n\nAssigned work:\n{user}"

    try:
        output = await complete_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tracker=tracker,
            stage=role.casefold().replace(" ", "_"),
            max_tokens=4096,
        )
        output = (output or "").strip()
    except Exception as exc:  # noqa: BLE001
        stabilization_logger.exception("LangGraph %s node failed", role)
        output = f"{role} failed: {type(exc).__name__}: {exc}"

    _lifecycle_done(
        callback,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        result_summary=output,
        started=started,
    )
    return output


_TABULAR_KINDS = frozenset({"xlsx", "csv", "json"})
_NARRATIVE_KINDS = frozenset({"pdf", "html", "txt", "md", "text"})
_OFFICE_KINDS = frozenset({"docx", "pptx"})
_SUPPORTED_KINDS = _TABULAR_KINDS | _NARRATIVE_KINDS | _OFFICE_KINDS | {"code"}
_MAX_DOC_NOTES_CHARS = 6_000
_MAX_SURVEY_DOC_NOTES_CHARS = 12_000
# User's explicit deep_research opt-in (main.py: RunPipelineRequest.deep_research)
# gathers far more evidence than the phrase-triggered survey default above, so a
# rich brief regularly runs well past 12k chars -- the old caps silently cut a
# multi-section report off mid-sentence right after its first section, which is
# what Document then wrote to the file (see the Aug 2026 Turkey-energy report
# bug: a 9.7k-char research brief got truncated to 6k chars before Document
# ever saw the rest).
_MAX_DEEP_DOC_NOTES_CHARS = 24_000
# Deep + several formats. Raised to the single-format deep allowance after
# measuring what a deep run actually gathers: eleven pages produced 103k
# characters of evidence and the writer was handed 12k of them, so 88% of
# the work the user waited ten minutes for never reached the document.
# Searching wider would only have grown the discarded share -- the binding
# constraint was here, not in the search budget.
_MAX_DEEP_MULTI_DOC_NOTES_CHARS = 24_000
# How much Document may WRITE. Feeding it a 24k-char brief while capping the
# reply at 4-6k tokens truncated the report from the other end: the last
# heading ended on a colon promising a list that never arrived. These are
# floors — the real ceiling is derived from the brief actually handed over
# (see _doc_token_budget), so a report is never cut short of its own
# research. _DOC_MAX_TOKENS_CEILING is only there to stop a runaway.
_DOC_MAX_TOKENS = 4_096
_DOC_MAX_TOKENS_PAGED = 6_144
_DOC_MAX_TOKENS_DEEP = 16_384
_DOC_MAX_TOKENS_CEILING = 32_768
# Typed blocks on a reasoning model. Its thinking tokens come out of the same
# max_tokens as the answer: live, deepseek-v4-flash spent all 4,710 of a
# blocks call on reasoning and returned no content at all, so the job fell
# back to the markdown writer and the new path never ran.
_DOC_BLOCKS_REASONING_HEADROOM = 6_000


def _doc_token_budget(
    notes: str,
    *,
    paged: bool,
    primary: str,
    deep_research: bool,
    source_text: str = "",
) -> int:
    """Output ceiling that can always carry the brief Document was given.

    Turkish tokenizes at roughly 3 chars/token, and a report adds headings
    and tables on top of the prose, so half the note length in tokens leaves
    real headroom rather than a hair's breadth.

    ``source_text`` covers the case where the length to be carried is not
    the research brief. Rewriting an uploaded document into its own
    template has no notes at all, so sizing from ``notes`` alone left the
    base budget for a job whose output must be at least as long as the
    file the user attached. Live: a 8.9k-char technical specification was
    cut mid-heading at "### MADDE 6: ORTA", and because the JSON was then
    unterminated the whole raw reply landed in the .docx as visible text.
    """
    budget = _DOC_MAX_TOKENS
    if paged and primary in {"pdf", "docx"}:
        budget = _DOC_MAX_TOKENS_PAGED
    if deep_research:
        budget = max(budget, _DOC_MAX_TOKENS_DEEP)
    fits_notes = len(notes or "") // 2
    # A rewrite reproduces its source rather than summarizing it, so the
    # whole source has to fit. One token per source character looks
    # extravagant for Turkish (~3 chars/token) and it is not: the model
    # reasons before it writes, and those thinking tokens come out of the
    # same budget. Halving this to "just the prose" was measured and cut a
    # rewrite off mid-word at "Temel ve Per" after 10 paragraphs.
    #
    # This only works paired with a wait long enough to spend it -- see
    # _document_llm_timeout. A budget the call cannot finish inside its own
    # timeout is worse than no budget at all: it times out into empty notes.
    fits_source = len(source_text or "")
    return min(_DOC_MAX_TOKENS_CEILING, max(budget, fits_notes, fits_source))
# A working single-page app's HTML/CSS/JS is not prose -- it cannot be
# summarized or cut mid-token without breaking it. Real bug: a Coding Agent
# wrote a complete 13.3k-char to-do app, but the 6k-char default cap sliced
# through the middle of its <style>/<script>, well before the closing ```
# fence -- _extract_html_document then found no complete document to recover
# and silently fell back to the static report-deck template.
_MAX_INTERACTIVE_APP_NOTES_CHARS = 30_000


def _document_notes_cap(
    *,
    deep_research: bool,
    request_text: str,
    kind_count: int,
    wants_interactive_app: bool = False,
) -> int:
    """How much of Research's brief Document is allowed to see.

    deep_research (the user's explicit opt-in) always wins over the
    phrase-triggered survey heuristic below it -- a request that opted in
    can gather far more evidence than that heuristic ever assumed, so it
    must not be capped to the heuristic's smaller budget.
    """
    if wants_interactive_app:
        return _MAX_INTERACTIVE_APP_NOTES_CHARS
    if deep_research:
        cap = _MAX_DEEP_DOC_NOTES_CHARS
    elif capabilities.detect_deep_research(request_text):
        cap = _MAX_SURVEY_DOC_NOTES_CHARS
    else:
        cap = _MAX_DOC_NOTES_CHARS
    # Multi-format jobs (docx+pptx+html) blow up when the model also tries to
    # regenerate the whole essay — keep notes tighter so the JSON call returns.
    #
    # But not to the same 4.5k in deep mode, which silently undid the whole
    # opt-in: a deep run gathered ~60k characters of evidence and then
    # handed the writer 4,500 of them, so asking for "docx and an HTML
    # deck" produced a SHORTER report than the same request without deep.
    # The clamp was tuned against the ordinary 6k budget and 45s timeout;
    # a deep run now writes with 16k tokens and up to 400s, so the
    # condition that motivated it no longer holds. Still tighter than a
    # single-format deep run, because the multi-format concern is real.
    if kind_count >= 2:
        cap = min(cap, _MAX_DEEP_MULTI_DOC_NOTES_CHARS if deep_research else 4_500)
    return cap


_MAX_ARTIFACT_KINDS = 3
# Document Agent often asks the model for a huge JSON body (docx+pptx+html).
# Cap hard: if the API hangs, fall back to research notes instead of burning
# another three minutes (we already saw 180s timeouts on healthy notes).
_DOCUMENT_LLM_TIMEOUT_SECONDS = 45.0


def _document_llm_extra_body() -> dict[str, Any]:
    """OpenRouter options for the Document call, from the environment.

    DOCUMENT_LLM_REASONING_EFFORT: low | medium | high -- how much the model
    thinks before writing. DOCUMENT_LLM_PROVIDER_SORT: throughput | latency |
    price -- which upstream provider OpenRouter routes to. Both unset means
    the request is exactly what it was before these existed.
    """
    import os

    body: dict[str, Any] = {}
    effort = os.environ.get("DOCUMENT_LLM_REASONING_EFFORT", "").strip().lower()
    if effort in {"low", "medium", "high"}:
        body["reasoning"] = {"effort": effort}
    sort = os.environ.get("DOCUMENT_LLM_PROVIDER_SORT", "").strip().lower()
    if sort in {"throughput", "latency", "price"}:
        body["provider"] = {"sort": sort}
    return body


def _document_llm_timeout(doc_max_tokens: int) -> float:
    """Wait in proportion to how much writing was actually asked for.

    The 45s cap and a 16k-token budget cannot both be honoured: generating
    that much takes minutes, so the call would time out every time and fall
    back to notes, making the larger budget pointless. Ordinary jobs keep
    the hard 45s; only a raised budget buys more time, and never unbounded.
    """
    # The threshold is the BASE budget, not the paged one. A paged report
    # or a template rewrite already asks for more writing than the base
    # job, and 45s does not cover it -- measured, the model needs about
    # 50s for a 6k-token document, so the flat cap failed it by a whisker
    # and the run silently fell back to notes.
    if doc_max_tokens <= _DOC_MAX_TOKENS:
        return _DOCUMENT_LLM_TIMEOUT_SECONDS
    # 60 tokens/sec was optimistic for this model: a 4.3k-token rewrite got
    # 72s, needed more, and timed out into an empty deliverable. Measured
    # closer to 30/sec, so budget the wait at that rate -- a raised budget
    # is pointless if the call can never finish inside its own timeout.
    # The ceiling has to clear what the budget actually costs, or the
    # budget is a bluff: a 9.8k-token template rewrite needs ~330s at this
    # model's measured 30 tokens/sec, so a 240s cap cut it off mid-run
    # about half the time and shipped a two-paragraph file. Waiting six
    # minutes and getting the document beats waiting four and getting
    # nothing.
    return min(400.0, max(_DOCUMENT_LLM_TIMEOUT_SECONDS, doc_max_tokens / 30))


def _notes_ready_for_direct_deliverable(
    notes: str,
    *,
    primary: str,
    kinds: list[str],
    keep_docx_table: bool,
) -> bool:
    """True when Research notes can become the file without a Document LLM."""
    if keep_docx_table or len(kinds) != 1:
        return False
    # PPTX: skip the Document outline LLM — it often returns 1–2 thin slides
    # and invents a bullet as the title. build_pptx structures the notes.
    if primary not in {"pdf", "docx", "html", "txt", "md", "text", "pptx"}:
        return False
    text = (notes or "").strip()
    min_chars = _MIN_DIRECT_PDF_NOTES_CHARS
    if primary == "pptx":
        min_chars = max(min_chars, 400)
    if len(text) < min_chars:
        return False
    if _research_notes_insufficient(text):
        return False
    return True


def _pptx_outline_too_thin(body: str) -> bool:
    """True when Document returned a near-empty deck body vs usable notes."""
    text = (body or "").strip()
    if len(text) < 280:
        return True
    headings = len(re.findall(r"(?m)^#{1,3}\s+\S", text))
    bullets = len(re.findall(r"(?m)^\s*[-*•]\s+\S", text))
    return headings < 2 and bullets < 6


def _infer_artifact_kind(artifact_type: str, request_text: str) -> str:
    """Resolve primary deliverable kind from intent + request wording."""
    kinds = _infer_artifact_kinds(artifact_type, request_text)
    return kinds[0] if kinds else "xlsx"


def _uploaded_kinds(workspace_root: Optional[str]) -> set[str]:
    """Formats present in this job's uploads, as artifact-kind names.

    Used to tell a reference from an order: naming a format the user also
    attached is usually a way of pointing at that file.
    """
    if not workspace_root:
        return set()
    from pathlib import Path

    sources = Path(workspace_root) / "sources"
    if not sources.is_dir():
        return set()
    mapping = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".xlsx": "xlsx",
        ".csv": "csv",
        ".json": "json",
        ".txt": "txt",
        ".html": "html",
        ".pptx": "pptx",
        ".zip": "code",
    }
    found: set[str] = set()
    try:
        for path in sources.rglob("*"):
            if path.is_file():
                kind = mapping.get(path.suffix.lower())
                if kind:
                    found.add(kind)
    except OSError:
        return set()
    return found


def _infer_artifact_kinds(
    artifact_type: str,
    request_text: str,
    *,
    attached_kinds: Optional[set[str]] = None,
) -> list[str]:
    """All explicitly requested formats (e.g. Word + HTML sunum).

    Camel Workforce can emit multiple files in one job; LangGraph used to
    pick a single kind, so ``word … ve html sunum`` silently dropped HTML.

    ``attached_kinds`` are the formats the user UPLOADED. A format named in
    the request is dropped from the extras when a file of that type is
    attached and the classifier chose a different primary, because the
    mention is then a reference to the attachment rather than an order for
    a second deliverable: "bu pdf'in içeriğini docx olarak ver" asked for
    one .docx and got a .docx plus a .pdf. That also silently disabled the
    uploaded-template path, which only runs for a single-format job.

    Evidence, not another phrase list: what the user actually attached is
    the thing that makes the mention ambiguous, so it is what resolves it.
    """
    kind = (artifact_type or "none").strip().lower()
    if kind in {"text"}:
        kind = "txt"

    text = (request_text or "").casefold()
    hints = (
        ("docx", ("docx", "word", "belge", "doküman", "dokuman", "döküman", "dokumanı")),
        ("html", ("html", "web sayfası", "web sayfasi", "landing")),
        ("pptx", ("pptx", "powerpoint", "power point", "power-point", "slayt", "ppt ")),
        ("pdf", ("pdf",)),
        ("csv", ("csv",)),
        ("json", ("json",)),
        ("xlsx", ("xlsx", "excel", "spreadsheet", "tablosu", "exsel")),
        ("txt", ("txt", "markdown", "md dosya")),
        ("code", ("zip", "code bundle", "kod paketi")),
    )
    # "sunum" alone (no format named) → HTML deck (matches Camel's html
    # sunum path). "PowerPoint sunumu" is the normal Turkish phrase for "a
    # PowerPoint presentation" -- sunum here describes what the PowerPoint
    # already is, not a request for a SECOND, separate deliverable. So this
    # default only applies when nothing else was matched at all; otherwise
    # "PowerPoint sunumu hazırla" would silently also build an unwanted HTML
    # deck alongside the .pptx (and did, before this fix).
    sunum_wants_html = "sunum" in text or "presentation" in text

    referenced = {
        k for k in (attached_kinds or set()) if k and k != kind
    }

    found: list[tuple[int, str]] = []
    for resolved, needles in hints:
        if resolved in referenced:
            continue
        for needle in needles:
            idx = text.find(needle)
            if idx >= 0:
                found.append((idx, resolved))
                break
    if sunum_wants_html and not found:
        idx = text.find("sunum")
        if idx < 0:
            idx = text.find("presentation")
        found.append((idx if idx >= 0 else 10_000, "html"))

    found.sort(key=lambda item: item[0])
    ordered: list[str] = []
    seen: set[str] = set()
    for _, resolved in found:
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)

    if kind in _SUPPORTED_KINDS and kind not in seen:
        ordered.insert(0, kind)
    elif kind in _SUPPORTED_KINDS:
        # Classifier primary stays first when it was also in the request.
        ordered = [kind] + [k for k in ordered if k != kind]

    if not ordered:
        ordered = [kind if kind in _SUPPORTED_KINDS else "xlsx"]

    # "3-4 sayfa / sayfa numarası" is a narrative report, not a spreadsheet.
    # Without this, unspecified artifact_type defaults to xlsx and Document
    # dies on empty rows while the research notes are a full essay.
    if _looks_like_paged_report(text):
        preferred = "pdf" if "pdf" in text else "docx"
        ordered = [preferred] + [k for k in ordered if k != preferred]
        # Spreadsheet is never the right primary for a paged prose report.
        ordered = [k for k in ordered if k != "xlsx"] or [preferred]

    return ordered[:_MAX_ARTIFACT_KINDS]


def _looks_like_paged_report(text: str) -> bool:
    body = text or ""
    return bool(
        re.search(
            r"(?is)"
            r"sayfa\s*numar"
            r"|\bsayfa\s*\d+\s*/\s*\d+"
            r"|\d+\s*[-–—]\s*\d+\s*sayfa"
            r"|en az\s+\d+\s*sayfa"
            r"|(?:pdf|word|docx).{0,40}sayfa"
            r"|sayfa.{0,20}(?:altında|altinda|footer|numara)",
            body,
        )
    )


def _find_uploaded_docx_template(workspace_root: Optional[str]) -> Optional[str]:
    """Workspace-relative path to an uploaded .docx in sources/, if any.

    First match wins if more than one -- a v1 simplification; a job's
    sources/ almost always holds one relevant upload (build_job_workspace
    copies only this-turn + turn_resolution-bound files, not the whole
    conversation)."""
    if not workspace_root:
        return None
    from pathlib import Path

    root = Path(workspace_root)
    sources_dir = root / "sources"
    if not sources_dir.is_dir():
        return None
    candidates = sorted(
        p
        for p in sources_dir.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".docx"
    )
    if not candidates:
        return None
    return candidates[0].relative_to(root).as_posix()


def _workspace_has_uploaded_sources(workspace_root: Optional[str]) -> bool:
    """True when this job's workspace has at least one uploaded source
    file. Used only to narrow the thin_notes carve-out in
    _run_document_worker -- never changes routing on the ordinary
    Research-then-Document path."""
    if not workspace_root:
        return False
    from pathlib import Path

    sources_dir = Path(workspace_root) / "sources"
    return sources_dir.is_dir() and any(p.is_file() for p in sources_dir.rglob("*"))


def _request_wants_structured_table(text: str) -> bool:
    """True when the user asked for a ranking/list with named columns."""
    body = text or ""
    if re.search(
        r"(?is)\b(?:sıra|sira)\b.{0,80}\b(?:oyuncu|player)\b"
        r"|\b(?:oyuncu|player)\b.{0,80}\b(?:takım|takim|team|club)\b"
        r"|\b(?:gol(?:ler)?|goals?)\b.{0,40}\b(?:sayısı|sayisi|count)\b"
        r"|\bilk\s*\d+\b|\btop\s*\d+\b"
        r"|\bgol\s*krall"
        r"|\bstandings?\b|\brankings?\b|\bleaderboard\b",
        body,
    ):
        return True
    # Explicit column list: "sıra, oyuncu, takım, gol"
    return bool(
        re.search(
            r"(?is)(?:sütun|sutun|kolon|column|alan).{0,40}:"
            r"|(?:sıra|sira)\s*[,|/]\s*(?:oyuncu|player)",
            body,
        )
    )


def _request_wants_interactive_app(text: str) -> bool:
    """True when an "html" request wants a real, working tool/app/game
    (interactivity: JS, inputs, buttons) rather than a static write-up of
    research findings.

    Without this check, EVERY "html"-only request goes through the same
    prose-to-deck shape (Document returns title/summary/content, never an
    html key -- see the "Do NOT emit a full HTML document" instruction in
    _run_document_worker) -- correct for "bir html rapor hazırla", but for
    "basit bir yapılacaklar listesi web uygulaması yap" it produces a
    decorative, non-functional page with zero <script>/<input> (real bug:
    tested live, got a page whose title was literally the model's own
    "here's the code" sentence, no todo-list behavior at all).

    Deliberately conservative: report words win when also present, since
    the deck path is the long-established default and must not regress
    for an actual report/sunum/analiz ask."""
    body = (text or "").casefold()
    if re.search(r"\b(?:rapor|sunum|özet|ozet|araştırma|arastirma|analiz)\b", body):
        return False
    return bool(
        re.search(
            r"web\s*(?:app|uygulama\w*)"
            r"|uygulama\w*\s+(?:yap|hazırla|hazirla|oluştur|olustur)"
            r"|hesap\s*makinesi"
            r"|oyun\w*\s+(?:yap|hazırla|hazirla|oluştur|olustur)"
            r"|\btodo\b|yapılacaklar\s+listesi|yapilacaklar\s+listesi"
            r"|kronometre|\bsayaç\b|\bsayac\b|\bquiz\b|\bwidget\b",
            body,
        )
    )


_TR_FOLD = str.maketrans(
    {
        "ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o", "ş": "s", "ü": "u",
        "Ç": "c", "Ğ": "g", "I": "i", "Ö": "o", "Ş": "s", "Ü": "u", "â": "a",
        "î": "i", "û": "u",
    }
)


def _fold_tr(text: str) -> str:
    """Casefold and strip Turkish diacritics so a pattern written in ASCII
    matches text typed either way ("aynı" and "ayni" alike)."""
    return (text or "").casefold().translate(_TR_FOLD)


def _request_wants_template_match(text: str) -> bool:
    """True when the user asks to reuse an uploaded document's own
    format/structure for new content ("aynı formatta/yapıda yaz").

    Deliberately loose on its own -- it is only ever consulted together
    with "an uploaded .docx exists in this job's workspace"
    (_find_uploaded_docx_template). That is what actually makes this safe:
    the phrase alone is ambiguous with "give me the same FILE format" (a
    prior-notes/turn_resolution concern, not this), but that ambiguity
    cannot fire a false positive here unless a real .docx template is also
    present in this job's sources/.

    Matched against diacritic-folded text. Turkish is routinely typed
    without diacritics, and the previous ASCII-blind pattern therefore
    missed every real request: "ayni olsun" does not match "aynı", so an
    attached specification was sent to web research instead, which went
    and wrote a report about an unrelated real company of a similar name.
    The one message that did work only did so because turn_resolution had
    already rewritten it with diacritics restored.

    Naming a format word is also not required. "her sey ayni olsun"
    (let everything be the same) and "bunu ... yazsana" (write this as)
    ask for exactly this and name no format at all."""
    body = _fold_tr(text or "")
    return bool(
        re.search(
            r"ayni\s+(?:format|yapi|sablon|stil)\w*"
            r"|bu\s+(?:format|yapi|sablon)\w*\s*(?:da|de|ta|te)\b"
            r"|(?:yukledi|yukledig|uploaded)\w*\s+(?:dosya|belge|sablon|template)\w*"
            r".{0,30}(?:format|yapi|structure|template)\w*"
            r"|bu\s+sablonu\s+kullan\w*|use\s+this\s+template"
            r"|same\s+(?:format|structure|template|layout)"
            r"|template'?\w*\s*gibi"
            # "everything the same" -- the ask with no format word in it.
            r"|\bayni\s+(?:olsun|olacak|olucak|kalsin|sekilde|yap)\w*"
            r"|her\s*sey\w*\s+ayni"
            r"|ayni\s+her\s*sey"
            r"|keep\s+(?:everything|the\s+same)|everything\s+the\s+same"
            # "write/adjust THIS (the attachment) as X" -- a reference to
            # the attachment plus a rewrite verb. Safe only because a real
            # uploaded .docx must also be present for this to be consulted.
            #
            # Two ways to refer to it, and both are needed: the bare
            # pronoun ("bunu doldur") and "bu" with a document noun ("bu
            # dokumani benim icin doldur", which the pronoun-only pattern
            # missed and sent to the ordinary writer -- it produced a
            # 113-character file saying no sources were found). The noun
            # list is what keeps "bu konuda bir rapor yaz" out.
            r"|\bbunu\b.{0,60}\b(?:yaz|duzenle|hazirla|doldur|uyarla|cevir|degistir)\w*"
            r"|\bbu\s+(?:dokuman|dokman|belge|dosya|sablon|form|metn|metin|rapor)\w*"
            r".{0,60}\b(?:yaz|duzenle|hazirla|doldur|uyarla|cevir|degistir)\w*"
            # "Fill it in" is a template ask on its own: you fill a form or
            # a template, not a report written from scratch.
            r"|\bdoldur\w*\b|\bfill\s+(?:\w+\s+){0,2}?(?:in|out)\b"
            r"|\brewrite\s+this\b|\bsame\s+as\s+this\b",
            body,
        )
    )


_FENCED_HTML_RE = re.compile(r"```(?:html|htm)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_html_document(text: str) -> str:
    """Pull a complete HTML document out of prose, for when the model
    wrote real working code but wrapped it in a markdown fence (or just
    started straight with it) instead of using the requested `html` JSON
    key -- a common model habit for code, instruction-following aside.

    Only ever called for _request_wants_interactive_app payloads (see
    _run_document_worker) -- an ordinary report's prose is never mistaken
    for the deliverable itself, since this is never invoked for that case."""
    body = (text or "").strip()
    if not body:
        return ""
    match = _FENCED_HTML_RE.search(body)
    candidate = match.group(1).strip() if match else body
    if re.match(r"(?is)^\s*<!doctype\s+html|^\s*<html[\s>]", candidate):
        return candidate
    return ""


def _markdown_table_from_grid(
    header: Optional[Sequence[Any]],
    rows: Optional[Sequence[Sequence[Any]]],
    *,
    max_rows: int = 12,
) -> str:
    """Build a compact markdown table from Document payload columns/rows."""
    cols = [str(h or "").strip() or f"C{i+1}" for i, h in enumerate(header or [])]
    matrix = [list(r) for r in (rows or []) if isinstance(r, (list, tuple))]
    if not cols or not matrix:
        return ""
    cols = cols[:8]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in matrix[:max_rows]:
        cells = [str(row[i] if i < len(row) else "").strip() for i in range(len(cols))]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _first_markdown_table(text: str, *, max_chars: int = 1800) -> str:
    """Return the first GitHub-style markdown table block, if any."""
    lines = (text or "").splitlines()
    start = -1
    for i, line in enumerate(lines):
        if "|" in line and re.search(r"\|.*\|", line):
            start = i
            break
    if start < 0:
        return ""
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if block:
                break
            continue
        if "|" not in line:
            break
        block.append(line.rstrip())
        if sum(len(x) + 1 for x in block) >= max_chars:
            break
    if len(block) < 2:
        return ""
    return "\n".join(block).strip()


def _compose_document_chat_answer(
    *,
    prior_notes: str,
    file_notice: str,
    title: str = "",
    kinds: Optional[Sequence[str]] = None,
    table_markdown: str = "",
) -> str:
    """Chat blurb when files were written.

    Narrative decks (docx/pptx/pdf/html): short title + 1–2 sentences so the
    file stays the deliverable. Tabular files (xlsx/csv/json): also keep the
    data table in chat so follow-ups ("chatte ver", "bunu pdf ver") can reuse
    the numbers without re-researching.
    """
    notes = _strip_capability_refusals(prior_notes or "").strip()
    notice = (file_notice or "").strip()
    cleaned, sources = _split_research_sources(notes)
    cleaned = _strip_capability_refusals(_strip_kaynaklar_section(cleaned)).strip()
    kind_set = {str(k or "").strip().lower() for k in (kinds or [])}
    tabular = bool(kind_set & {"xlsx", "csv", "json"})
    blurb = _chat_blurb_from_notes(
        cleaned, title=title, keep_tables=tabular
    )
    parts: list[str] = []
    if blurb:
        parts.append(blurb)
    if tabular:
        table = (table_markdown or "").strip() or _first_markdown_table(cleaned)
        if table and table not in (blurb or ""):
            parts.append(table)
    if sources:
        parts.append(
            "---\nRESEARCH_SOURCES:\n"
            + json.dumps(sources, ensure_ascii=False)
        )
    if notice:
        parts.append(notice)
    return "\n\n".join(parts) if parts else notice


def _chat_blurb_from_notes(
    notes: str, *, title: str = "", keep_tables: bool = False
) -> str:
    """Title + one short paragraph for artifact chat answers."""
    raw = notes or ""
    text = _strip_capability_refusals(
        raw if keep_tables else _strip_markdown_tables(raw)
    ).strip()
    title_line = (title or "").strip()
    if title_line.lower() in {"report", "rapor"}:
        title_line = ""

    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not title_line and paras:
        first = paras[0].lstrip("# ").strip()
        if first and len(first) <= 120 and not first.lower().startswith("http"):
            title_line = first
            paras = paras[1:]

    blurb = ""
    dangling = ""
    for para in paras:
        # A markdown heading is never the body prose we want. Stripping the
        # "#" first threw that away, so "## 1. Mevcut Durum (2026)" reached
        # the tests below and passed them (the "1." supplies the period they
        # look for) — the chat answer became a bare heading with nothing
        # under it while the real brief sat in the file.
        if para.lstrip().startswith("#"):
            continue
        candidate = para.lstrip("# ").strip()
        if len(candidate) < 40:
            continue
        if candidate.casefold().startswith("kaynak"):
            continue
        if "|" in candidate and candidate.count("|") >= 2:
            # Skip table blocks here; caller may append them separately.
            continue
        # Skip title / extended heading lines — we want the first body paragraph.
        if title_line and candidate.casefold().startswith(title_line.casefold()):
            continue
        # Unmarked section headings ("1. Ticarilesme Onundeki Engeller"):
        # numbered, with no sentence ending after the number itself. Testing
        # the whole line would pass on the numbering's own period.
        numbered = re.match(r"^\d+[.)]\s+(.*)$", candidate, re.S)
        if numbered and not re.search(r"[.!?](?:\s|$)", numbered.group(1)):
            continue
        # A paragraph ending in ":" introduces the table or list that comes
        # next — and tables are stripped above, so it lands in chat pointing
        # at nothing: "…aşağıdaki boyutlara ulaşmıştır:" then the sources.
        # Keep looking; fall back to it below only if nothing else exists.
        # Checked before the no-period rule, which would otherwise discard
        # such a lead-in as a heading and leave no fallback at all.
        if candidate.rstrip().endswith(":"):
            if not dangling:
                dangling = candidate
            continue
        if candidate.count(".") == 0 and len(candidate) < 100:
            continue
        blurb = candidate
        break
    blurb = blurb or dangling
    if blurb and len(blurb) > 420:
        cut = blurb[:417].rsplit(" ", 1)[0]
        blurb = (cut or blurb[:417]) + "…"

    if title_line and blurb:
        return f"{title_line}\n\n{blurb}"
    return title_line or blurb



def _image_data_uri(path: Any) -> str:
    """Encode a local image file as a data URI for offline HTML decks."""
    import base64
    import mimetypes
    from pathlib import Path

    file_path = Path(path)
    raw = file_path.read_bytes()
    mime = mimetypes.guess_type(str(file_path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _html_deck_from_payload(
    *,
    title: str,
    summary: str,
    columns: list[Any],
    rows: list[Any],
    body: str = "",
    images: Optional[list[Any]] = None,
) -> str:
    """Dark splash + slide deck HTML from doc payload — no second LLM call."""
    import html as html_lib
    from datetime import date as _date

    import document_export

    raw_title = (title or "").strip() or "Sunum"
    display_title = document_export.short_report_title(
        raw_title, fallback=raw_title
    )
    safe_title = html_lib.escape(display_title)
    prose = document_export.sanitize_report_prose(body or summary or "")
    teaser = ""
    for chunk in re.split(r"\n{2,}", prose):
        chunk = chunk.strip()
        if len(chunk) >= 40:
            teaser = chunk[:420]
            break

    _months = (
        "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
    )
    today = _date.today()
    prepared = f"{today.day} {_months[today.month - 1]} {today.year}"

    slides: list[str] = []
    structured = document_export.structure_prose(prose)
    section_buf: list[tuple[str, str]] = []
    current_title = ""
    current_paras: list[str] = []

    def flush_section() -> None:
        nonlocal current_title, current_paras
        body_text = "\n\n".join(p for p in current_paras if p.strip()).strip()
        if current_title or body_text:
            section_buf.append((current_title or "Bölüm", body_text))
        current_title = ""
        current_paras = []

    # Sections come from the same block parser the Word writer uses, not a
    # second heading detector written here.
    #
    # This one only recognised "#" headings and numbered lines, so a report
    # whose sections are plain short lines -- "Giriş", "Maven Smart System
    # (MSS) Nedir?", "Rekabet Analizi ve Kaybedilen İhaleler", which is
    # what the model actually writes -- had every one of them fall through
    # as body text. A 7.5k-character deep-research report came out as a
    # single content slide between a photo and the sources, while the .docx
    # built from the identical text carried real headings throughout.
    for kind, text, _level in document_export.iter_body_blocks(structured):
        if kind == "heading":
            flush_section()
            current_title = str(text).strip()
            continue
        if kind == "table":
            # Flattened into lines, not dropped. Skipping them left the two
            # sections that were tables -- "Sözleşme Yığını", "Finansal
            # Veriler" -- as a heading over 35 characters of nothing, while
            # the .docx built from the same text carried both tables in
            # full. A slide has no table, so each row becomes the same
            # "label: value" shape used for a markdown row elsewhere.
            matrix = text if isinstance(text, list) else []
            header = [str(c).strip() for c in (matrix[0] if matrix else [])]
            for row in matrix[1:]:
                cells = [str(c).strip() for c in row if str(c).strip()]
                if not cells:
                    continue
                if header and len(cells) == len(header) and len(cells) > 2:
                    current_paras.append(
                        "- "
                        + " · ".join(
                            f"{h}: {c}" if h else c for h, c in zip(header, cells)
                        )
                    )
                elif len(cells) == 2:
                    current_paras.append(f"- {cells[0]}: {cells[1]}")
                else:
                    current_paras.append("- " + " · ".join(cells))
            continue
        line = str(text).strip()
        if not line:
            continue
        current_paras.append(f"- {line}" if kind in {"bullet", "number"} else line)
    flush_section()

    subtitle = ""
    for sec_title, _sec_body in section_buf:
        candidate = (sec_title or "").strip()
        if (
            candidate
            and candidate.casefold() not in {"bölüm", "bolum", "özet", "ozet"}
            and not candidate.casefold().startswith("kaynak")
            and candidate.casefold() != display_title.casefold()
        ):
            subtitle = candidate
            break
    if not subtitle and teaser:
        subtitle = teaser.split(".")[0].strip()[:90]
    safe_subtitle = html_lib.escape(subtitle) if subtitle else ""
    tagline = teaser
    if subtitle and teaser.lower().startswith(subtitle.lower()):
        tagline = teaser[len(subtitle) :].lstrip(" .:—-")
    safe_tagline = html_lib.escape(tagline[:220]) if tagline else ""

    for sec_title, sec_body in section_buf[:10]:
        if sec_title.casefold().startswith("kaynak"):
            continue
        paras = [
            f"<p>{html_lib.escape(p.strip())}</p>"
            for p in re.split(r"\n{2,}", sec_body)
            if p.strip()
        ][:4]
        if not paras and not sec_title:
            continue
        slides.append(
            "<section class='slide'>"
            f"<h2>{html_lib.escape(sec_title)}</h2>"
            + "".join(paras)
            + "</section>"
        )

    headers = [str(c) for c in (columns or [])]
    header_keys = {h.strip().casefold() for h in headers}
    skip_synthetic = header_keys <= {
        "bölüm", "içerik", "bolum", "icerik", "section", "content"
    }
    if not skip_synthetic:
        for row in (rows or [])[:10]:
            if not isinstance(row, (list, tuple)):
                continue
            label0 = str(row[0] or "").strip().casefold() if row else ""
            if label0 in {"kaynaklar", "kaynakça", "kaynakca", "sources", "kaynak"}:
                continue
            cells = []
            for i, cell in enumerate(row):
                label = headers[i] if i < len(headers) else f"Alan {i + 1}"
                cells.append(
                    f"<div class='kv'><span>{html_lib.escape(label)}</span>"
                    f"<strong>{html_lib.escape(str(cell))}</strong></div>"
                )
            if cells:
                slides.append(
                    "<section class='slide'>" + "".join(cells) + "</section>"
                )

    source_lines: list[str] = []
    m = re.search(r"(?is)kaynaklar\s*:\s*(.+)$", prose)
    if m:
        source_lines = [
            ln.strip()
            for ln in m.group(1).splitlines()
            if ln.strip() and not ln.strip().casefold().startswith("kaynaklar")
        ]
    if not source_lines:
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            if str(row[0] or "").strip().casefold() in {
                "kaynaklar", "kaynakça", "kaynakca", "sources", "kaynak",
            }:
                source_lines = [
                    ln.strip()
                    for ln in str(row[1] or "").splitlines()
                    if ln.strip()
                ]
                break
    if source_lines:
        items = "".join(
            f"<li>{html_lib.escape(ln[:500])}</li>" for ln in source_lines[:12]
        )
        slides.append(
            "<section class='slide sources'>"
            "<h2>Kaynaklar</h2>"
            f"<ol>{items}</ol>"
            "</section>"
        )

    if not slides and prose:
        for chunk in re.split(r"\n{2,}", prose)[:6]:
            chunk = chunk.strip()
            if len(chunk) < 40:
                continue
            slides.append(
                "<section class='slide'><p>"
                + html_lib.escape(chunk[:1200])
                + "</p></section>"
            )

    figure_html = ""
    for img in (images or [])[:2]:
        path = getattr(img, "path", None)
        if path is None:
            continue
        try:
            from pathlib import Path as _Path

            if not _Path(path).is_file():
                continue
            uri = _image_data_uri(path)
        except (OSError, ValueError):
            continue
        credit = html_lib.escape(str(getattr(img, "attribution", "") or "")[:240])
        credit_html = f"<figcaption>{credit}</figcaption>" if credit else ""
        figure_html += (
            "<figure class='deck-photo'>"
            f"<img src=\"{uri}\" alt=\"\"/>"
            f"{credit_html}"
            "</figure>"
        )
    if figure_html:
        slides.insert(
            0,
            "<section class='slide photo'>"
            f"{figure_html}"
            "</section>",
        )

    slides_html = "\n".join(slides)
    hero_sub = f"<p class='kicker'>{safe_subtitle}</p>" if safe_subtitle else ""
    hero_tag = f"<p class='tagline'>{safe_tagline}</p>" if safe_tagline else ""
    return f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{safe_title}</title>
<style>
  :root {{
    --bg: #07111d; --fg: #e8eef6; --cyan: #3ecfff; --ember: #e45c38;
    --muted: #8b97a8; --card: #101c2c; --line: #1e3148;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: "Segoe UI", "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--fg);
  }}
  .hero {{
    min-height: 100vh;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; text-align: center;
    padding: 4rem 1.5rem 3.5rem;
    background:
      radial-gradient(720px 380px at 50% 18%, rgba(62,207,255,.12), transparent 62%),
      var(--bg);
  }}
  .mark {{
    width: 52px; height: 52px; margin-bottom: 1.75rem; color: var(--cyan);
    filter: drop-shadow(0 0 14px rgba(62,207,255,.45));
  }}
  .hero h1 {{
    margin: 0; max-width: 18ch;
    font-size: clamp(2rem, 5.2vw, 3.4rem);
    line-height: 1.12; font-weight: 750; letter-spacing: -.02em;
    color: var(--cyan);
    text-shadow: 0 0 28px rgba(62,207,255,.35);
  }}
  .kicker {{
    margin: 1.25rem 0 0; max-width: 36rem;
    font-size: clamp(1.05rem, 2.2vw, 1.45rem); font-weight: 700;
    color: var(--ember);
  }}
  .tagline {{
    margin: .85rem 0 0; max-width: 32rem;
    font-size: 1.02rem; color: var(--muted); line-height: 1.5;
  }}
  .meta {{
    margin: 1.5rem 0 0; font-size: .78rem; letter-spacing: .02em;
    color: #5d6b7c;
  }}
  .icons {{
    display: flex; gap: 1.75rem; margin-top: 2.75rem; color: var(--fg);
    opacity: .92;
  }}
  .icons svg {{ width: 28px; height: 28px; }}
  .deck {{ max-width: 860px; margin: 0 auto; padding: 1.5rem 1.25rem 4.5rem; }}
  .slide {{
    background: var(--card);
    border: 1px solid var(--line);
    border-left: 4px solid var(--cyan);
    border-radius: 14px;
    padding: 2.1rem 1.9rem;
    margin: 1.15rem 0;
    box-shadow: 0 12px 40px -24px rgba(0,0,0,.55);
  }}
  .slide h2 {{
    margin: 0 0 1rem; font-size: 1.32rem; color: var(--fg); font-weight: 650;
  }}
  .slide p {{ color: var(--muted); line-height: 1.7; font-size: 1.04rem; margin: 0 0 .85rem; }}
  .slide p:last-child {{ margin-bottom: 0; }}
  .kv {{
    display:flex; justify-content:space-between; gap:1rem;
    padding:.7rem 0; border-bottom:1px solid var(--line);
  }}
  .kv span {{ color: var(--muted); }}
  .kv strong {{ color: var(--fg); text-align:right; font-weight: 600; }}
  .sources ol {{ margin: 0; padding-left: 1.2rem; color: var(--muted); }}
  .sources li {{ margin: .45rem 0; line-height: 1.45; }}
  .deck-photo {{ margin: 0; }}
  .deck-photo img {{
    display: block; width: 100%; max-height: 420px; object-fit: cover;
    border-radius: 10px; border: 1px solid var(--line);
  }}
  .deck-photo figcaption {{
    margin-top: .65rem; font-size: .78rem; color: var(--muted); line-height: 1.4;
  }}
  @media print {{
    .hero {{ min-height: auto; break-after: page; }}
    .slide {{ break-after: page; }}
  }}
</style>
</head><body>
<header class="hero">
  <svg class="mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
    <circle cx="11" cy="11" r="7"/><path d="m20 20-3.4-3.4"/>
  </svg>
  <h1>{safe_title}</h1>
  {hero_sub}
  {hero_tag}
  <p class="meta">Hazırlanma Tarihi: {html_lib.escape(prepared)}</p>
  <div class="icons" aria-hidden="true">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 3c4 2 6 5 6 8 0 2-.5 3.5-1.5 5L12 21l-4.5-5C6.5 14.5 6 13 6 11c0-3 2-6 6-8z"/><circle cx="12" cy="11" r="2"/></svg>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="5" y="9" width="14" height="11" rx="3"/><path d="M12 9V5"/><circle cx="12" cy="3.6" r="1.2"/><circle cx="9" cy="14.5" r="1" fill="currentColor"/><circle cx="15" cy="14.5" r="1" fill="currentColor"/></svg>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M13 2 4 14h7l-1 8 10-14h-7l0-6z"/></svg>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/></svg>
  </div>
</header>
<main class="deck">
  {slides_html}
</main>
</body></html>
"""


def _html_deck_for(
    payload: dict[str, Any],
    *,
    title: str,
    summary: str,
    columns: list[Any],
    rows: list[Any],
    body: str = "",
    images: Optional[list[Any]] = None,
) -> str:
    """Deck HTML: from the typed blocks when they still describe `content`.

    Same rule the PDF/DOCX writers use (document_model.resolve_payload_document):
    if the write-up was replaced after parsing, the blocks no longer match
    and the markdown deck is built as before.
    """
    import document_model

    block_document = document_model.resolve_payload_document(payload)
    if block_document is not None:
        import document_html

        document_logger.info(
            "document html deck from %d typed blocks", len(block_document.blocks)
        )
        return document_html.render_deck(block_document, images=images)
    return _html_deck_from_payload(
        title=title,
        summary=summary,
        columns=columns,
        rows=rows,
        body=body,
        images=images,
    )


def _salvage_truncated_json(text: str) -> dict[str, Any]:
    """Recover the keys of a JSON object the model did not finish.

    A reply cut off by the output ceiling is not malformed, only
    unterminated -- everything before the cut is intact. Rejecting it
    wholesale sent the caller to a fallback that wrote the raw reply into
    the deliverable, so a user asking for their 8.9k-char specification
    rewritten got a .docx containing literal braces, JSON keys, and "\\n"
    escapes, ending mid-heading.

    Closes the open string and any open brackets, then parses. Returns {}
    if that still does not yield an object, so a genuinely malformed reply
    keeps failing rather than being half-guessed.
    """
    start = text.find("{")
    if start < 0:
        return {}
    body = text[start:]

    # Walk the text to learn where it stopped: inside a string, and how
    # many containers are still open. Rebuilt char by char rather than
    # sliced, so raw control characters can be escaped on the way past --
    # a model writing a long markdown document into a JSON string field
    # routinely emits real newlines instead of \\n, which is invalid JSON
    # and fails the same way a truncation does.
    out: list[str] = []
    in_string = False
    escaped = False
    stack: list[str] = []
    for index, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                if _quote_closes_string(body, index):
                    in_string = False
                else:
                    # A quote inside the text the model forgot to escape
                    # ('... örneğin "X ton" ...'): it cannot end the string,
                    # because what follows is not , } ] or :.
                    out.append('\\"')
                    continue
            elif ch in "\n\r\t":
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
                continue
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            # A trailing comma before a closer ("a", "b", ]) is invalid JSON.
            back = len(out) - 1
            while back >= 0 and out[back].isspace():
                back -= 1
            if back >= 0 and out[back] == ",":
                del out[back]
            if stack:
                stack.pop()
        out.append(ch)

    if escaped:
        # Cut between a backslash and the character it escapes.
        out.pop()
    repaired = "".join(out)
    if in_string:
        repaired += '"'
    repaired += "".join(reversed(stack))
    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _quote_closes_string(text: str, index: int) -> bool:
    """True when the '"' at ``index`` can end a JSON string.

    A closing quote is followed, after optional whitespace, by one of
    , } ] : -- or by the end of a (possibly truncated) reply.
    """
    for follower in text[index + 1 :]:
        if follower.isspace():
            continue
        return follower in ",}]:"
    return True


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0:
        return {}
    if end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return _salvage_truncated_json(text)


def _ensure_extension(filename: str, kind: str) -> str:
    from pathlib import Path

    stem = Path(filename or f"deliverable.{kind}").stem or "deliverable"
    ext = {
        "xlsx": ".xlsx",
        "csv": ".csv",
        "json": ".json",
        "pdf": ".pdf",
        "html": ".html",
        "docx": ".docx",
        "pptx": ".pptx",
        "txt": ".txt",
        "md": ".md",
        "text": ".txt",
        "code": ".zip",
    }.get(kind, f".{kind}")
    return f"{stem}{ext}"


def _ensure_office_table(
    columns: Any,
    rows: Any,
    *,
    title: str,
    summary: str,
    body: str,
) -> tuple[list[str], list[list[Any]]]:
    """A DOCX/PPTX/CSV table that must have ≥1 row — synthesize one from the narrative."""
    cols = [str(c) for c in columns] if isinstance(columns, list) and columns else []
    raw_rows = rows if isinstance(rows, list) else []
    cleaned: list[list[Any]] = []
    for row in raw_rows:
        if isinstance(row, (list, tuple)) and any(
            str(cell or "").strip() for cell in row
        ):
            cleaned.append(list(row))
        elif isinstance(row, dict) and row:
            if not cols:
                cols = [str(k) for k in row.keys()]
            cleaned.append([row.get(c) for c in cols])
    if cleaned:
        if not cols:
            width = max(len(r) for r in cleaned)
            cols = [f"Kolon {i + 1}" for i in range(width)]
        return cols, cleaned

    text = (summary or body or title or "Rapor").strip()
    parts = [
        p.strip()
        for p in re.split(r"\n{2,}", text)
        if p.strip()
    ]
    if len(parts) <= 1 and text:
        # Sentence-ish chunks so the Word table is not a single mega-cell.
        parts = [
            s.strip()
            for s in re.split(r"(?<=[.!?…])\s+", text)
            if len(s.strip()) > 40
        ] or [text]
    cols = ["Bölüm", "İçerik"]
    out_rows = [
        [f"{i + 1}", part[:2500]] for i, part in enumerate(parts[:24])
    ]
    if not out_rows:
        out_rows = [["1", title or "Rapor"]]
    return cols, out_rows


def _table_from_prose(body: str) -> tuple[list[str], list[list[str]]]:
    """The first real markdown table in the text, as (columns, rows).

    A spreadsheet asked for "excel olarak ver" came back with the columns
    "Bölüm | İçerik" and the report's paragraphs numbered down them -- with
    the actual figures sitting inside one cell as "| Yıl | Net Asgari
    Ücret |...". The data was there the whole time, written as markdown in
    the prose, while the sheet was built from the narrative instead.

    Returns ([], []) when there is no table, so the caller keeps whatever
    it had.
    """
    import document_export

    for kind, text, _level in document_export.iter_body_blocks(str(body or "")):
        if kind != "table" or not isinstance(text, list) or len(text) < 2:
            continue
        header = [str(c).strip() for c in text[0]]
        rows = [[str(c).strip() for c in row] for row in text[1:] if any(row)]
        if header and rows:
            return header, rows
    return [], []


def _write_artifact(
    artifact: Any,
    *,
    kind: str,
    filename: str,
    payload: dict[str, Any],
    images: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Dispatch to the matching ArtifactTools.create_* method."""
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    summary = str(payload.get("summary") or "").strip()
    title = str(payload.get("title") or summary or "Report").strip()
    body = str(payload.get("content") or payload.get("body") or summary or "").strip()
    html = str(payload.get("html") or "").strip()
    image_query = str(payload.get("image_query") or "").strip()
    filename = _ensure_extension(filename, kind)

    # Typed blocks, when the Document reply had them and `content` was not
    # replaced afterwards; otherwise "" and the markdown writers run.
    blocks_json = ""
    if kind in {"pdf", "docx"}:
        import document_model

        block_document = document_model.resolve_payload_document(payload)
        if block_document is not None:
            blocks_json = json.dumps(block_document.blocks_as_dicts(), ensure_ascii=False)
        document_logger.info(
            "document path=%s kind=%s file=%s",
            "blocks" if blocks_json else "markdown",
            kind,
            filename,
        )

    if kind in _OFFICE_KINDS:
        # DOCX narrative reports: keep real tables only. Do not invent a
        # Bölüm/İçerik grid from prose -- document_export structures the
        # summary into paragraphs instead.
        has_real_rows = isinstance(rows, list) and any(
            (isinstance(r, (list, tuple)) and any(str(c or "").strip() for c in r))
            or (isinstance(r, dict) and r)
            for r in rows
        )
        if kind == "docx" and not has_real_rows:
            columns, rows = [], []
        elif kind == "pptx" and not has_real_rows:
            # Narrative decks: section slides only — no fake Bölüm/İçerik table.
            columns, rows = [], []
        else:
            columns, rows = _ensure_office_table(
                columns, rows, title=title, summary=summary, body=body
            )
        # Prefer a long narrative summary for the prose block.
        # Word: never concatenate summary+body — that duplicated the report
        # (PDF already uses body or summary, so it stayed clean).
        if kind == "docx":
            summary = body or summary
        elif not summary and body:
            summary = body
        elif summary and body and body not in summary:
            summary = f"{summary}\n\n{body}"

    data_json = json.dumps(rows, ensure_ascii=False)
    columns_json = json.dumps(columns, ensure_ascii=False)

    if kind == "csv":
        if not rows:
            columns, rows = _ensure_office_table(
                columns, rows, title=title, summary=summary, body=body
            )
            data_json = json.dumps(rows, ensure_ascii=False)
            columns_json = json.dumps(columns, ensure_ascii=False)
        return artifact.create_csv_artifact(
            filename, data=data_json, columns=columns_json
        )
    if kind == "json":
        # Prefer object/array payload if provided; else wrap table.
        data = payload.get("data")
        if data is None:
            if columns and rows:
                data = [dict(zip(columns, row)) for row in rows]
            else:
                data = rows or {"title": title, "summary": summary or body}
        return artifact.create_json_artifact(
            filename, data=json.dumps(data, ensure_ascii=False)
        )
    if kind == "pdf":
        return artifact.create_pdf_artifact(
            filename,
            title=title,
            content=body or summary,
            image_query=image_query,
            blocks=blocks_json,
        )
    if kind in {"html"}:
        if not html:
            html = _html_deck_for(
                payload,
                title=title,
                summary=summary,
                columns=list(columns) if isinstance(columns, list) else [],
                rows=list(rows) if isinstance(rows, list) else [],
                body=body,
                images=list(images or []),
            )
        return artifact.create_html_artifact(filename, html=html)
    if kind in {"txt", "md", "text"}:
        # create_text_artifact structures wall-of-text into readable prose.
        return artifact.create_text_artifact(filename, content=body or summary)
    if kind == "docx":
        return artifact.create_docx_artifact(
            filename,
            title=title,
            summary=summary or body,
            data=data_json,
            columns=columns_json,
            image_query=image_query,
            blocks=blocks_json,
        )
    if kind == "pptx":
        return artifact.create_pptx_artifact(
            filename,
            title=title,
            # The full body, like pdf and html above -- not the summary
            # first. A model that returns a short summary alongside a long
            # content built an eight-section HTML deck and a one-slide
            # .pptx from the same answer, because html read `body` and this
            # read `summary`. The deck needs the sections; the summary has
            # none.
            summary=body or summary,
            data=data_json,
            columns=columns_json,
            image_query=image_query,
        )
    if kind == "code":
        # Best-effort: write a text/code bundle via text artifact if bundle
        # API needs a different shape; prefer create_text_artifact.
        return artifact.create_text_artifact(
            _ensure_extension(filename, "txt"), content=body or summary
        )
    # Default: xlsx
    #
    # Prefer a real table written in the prose over anything narrative. The
    # model often answers a spreadsheet ask with a markdown table inside
    # `content` and leaves columns/rows empty or filled with section
    # headings; the sheet then carried "Bölüm | İçerik" and the figures
    # were stranded inside a cell. document_export already knows how to
    # tell an invented narrative table from a real one.
    import document_export

    if not rows or document_export._is_synthetic_narrative_table(columns, rows):
        found_cols, found_rows = _table_from_prose(body or summary)
        if found_cols and found_rows:
            columns, rows = found_cols, found_rows
    if not rows:
        columns, rows = _ensure_office_table(
            columns, rows, title=title, summary=summary, body=body
        )
    data_json = json.dumps(rows, ensure_ascii=False)
    columns_json = json.dumps(columns, ensure_ascii=False)
    return artifact.create_xlsx_artifact(
        filename,
        data=data_json,
        columns=columns_json,
        sheet_name="Data",
    )


async def _run_document_worker(
    *,
    content: str,
    original_request: str,
    prior_notes: str,
    tracker: UsageTracker,
    callback: Any,
    skills: Optional[list],
    handle: CancellableWorkforce,
    unit_task_id: str,
    workspace_root: Optional[str],
    artifact_type: str,
    tool_call_recorder: Optional[ToolCallRecorder],
    deep_research: bool = False,
    research_ran: bool = True,
    reuses_uploaded_document: Optional[bool] = None,
) -> str:
    """Document node that writes one or more deliverables (docx + html, …)."""
    if handle.cancelled:
        return ""

    kinds = _infer_artifact_kinds(
        artifact_type,
        original_request or content,
        attached_kinds=_uploaded_kinds(workspace_root),
    )
    primary = kinds[0]

    if not workspace_root:
        return await _run_llm_worker(
            role=ROLE_DOCUMENT,
            content=content,
            original_request=original_request,
            prior_notes=prior_notes,
            tracker=tracker,
            callback=callback,
            skills=skills,
            handle=handle,
            unit_task_id=unit_task_id,
            job_capabilities=frozenset({capabilities.ARTIFACTS}),
            system_extra=(
                f"\nRequested format(s): {', '.join(kinds)}. No workspace "
                "available to write files; return the content in markdown only."
            ),
        )

    worker_id = WORKER_IDS[ROLE_DOCUMENT]
    _lifecycle_start(
        callback,
        role=ROLE_DOCUMENT,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        description=content,
    )
    started = time.monotonic()

    def progress(stage: str, detail: str = "") -> None:
        _emit_activity(callback, unit_task_id, stage, detail)

    progress("building", "create_" + "_".join(kinds) + "_artifact")

    from pathlib import Path

    from artifacts import ArtifactTools

    needs_table = any(k in _TABULAR_KINDS for k in kinds)
    needs_html = "html" in kinds
    needs_narrative = any(k in {"docx", "pdf", "pptx", "html"} for k in kinds)
    paged = _looks_like_paged_report(original_request or content)
    wants_data_table = _request_wants_structured_table(
        original_request or content
    )
    # Ranking/list Word asks need a real table in the .docx — not only prose.
    keep_docx_table = wants_data_table and primary in {"docx", "pdf"}
    wants_interactive_app = (
        needs_html
        and len(kinds) == 1
        and _request_wants_interactive_app(original_request or content)
    )
    template_path = (
        _find_uploaded_docx_template(workspace_root)
        if primary == "docx" and len(kinds) == 1
        else None
    )
    # The uploaded .docx is the hard gate either way -- neither signal can
    # claim a template that is not there.
    #
    # Which signal decides it is the LLM classifier, when it ran. The regex
    # below is the fallback for when it did not (call failed, timed out, or
    # the intent was never classified for this path). That ordering is the
    # point: the phrasings are unbounded and every pattern written for this
    # caught its own example and missed the next one -- "bunu doldur" was
    # added, then "bu dokumani doldur" missed; that was added, then "bana
    # gore yapar misin bunu" and "lutfen docx olarak ver" missed, and the
    # last of those went to web search and came back apologising that it
    # could not find the user's own attached file on the internet.
    if reuses_uploaded_document is None:
        reuse_signal = _request_wants_template_match(original_request or content)
    else:
        reuse_signal = bool(reuses_uploaded_document)
    wants_template_match = bool(template_path and reuse_signal)
    import document_model

    # Typed blocks only for narrative PDF/Word. Tables, template edits and
    # interactive apps keep their own shapes below.
    use_blocks = (
        document_model.blocks_enabled()
        and primary in {"pdf", "docx"}
        and not (needs_table or keep_docx_table or wants_template_match or wants_interactive_app)
    )
    if needs_table or keep_docx_table:
        if keep_docx_table:
            shape = (
                "Return ONLY JSON with keys: filename, title, summary, "
                "columns (string array), rows (array of arrays). "
                "summary: ONLY 1–3 short sentences (what the table shows + "
                "optional tie note). No Giriş/Yöntem/Değerlendirme chapters, "
                "no long essay, no markdown pipe tables in summary — the "
                "server renders columns/rows as the Word table. "
                "columns MUST match every column the user asked for "
                "(e.g. Sıra, Oyuncu, Takım, Gol). Fill Takım/Team when "
                "research notes name the club; empty string only if pages "
                "truly lack it. Match the exact league/competition named "
                f"(Süper Lig ≠ Premier League). filename ends with .{primary}."
            )
        else:
            shape = (
                "Return ONLY JSON with keys: filename, title, summary, columns "
                "(string array), rows (array of arrays aligned to columns). "
                "summary MUST hold the full report narrative (several "
                "paragraphs from the research notes). rows should be a compact "
                "key-points table (topic/detail), max ~12 rows — never leave "
                "rows empty; if unsure put section headings in rows."
            )
        if not keep_docx_table:
            shape += f" Primary filename must end with .{primary}."
    elif wants_template_match:
        # Edit the uploaded document instead of rewriting it.
        #
        # Rewriting cost a full regeneration every time: ~9000 output
        # tokens at this model's ~30 tokens/sec, about five minutes,
        # and the 101-paragraph source came back as 95 with bold on 84
        # runs instead of 33 and four clauses missing. A model asked to
        # reproduce 9800 characters does not reproduce them exactly, and
        # no amount of instruction fixes that.
        #
        # Naming only the paragraphs that change removes the cause. The
        # rest are never rewritten, so they cannot drift, and a rename
        # touches perhaps eight of them -- a few hundred tokens.
        shape = (
            "The user's own .docx is shown below as numbered "
            "paragraphs. Return ONLY JSON, in ONE of two shapes, and "
            "pick the shape from what the user actually asked for.\n"
            "\nA) They want THIS document adjusted -- renamed to their "
            "company, a clause reworded, details filled in. Return "
            "keys: filename, edits. edits maps the NUMBER of each "
            "paragraph you are changing to that paragraph's complete "
            "new text. Change as FEW paragraphs as the request needs: "
            "a paragraph you do not list keeps its exact original "
            "wording and formatting, which is the point, so never "
            "list one just to restate it. Do not renumber, reorder, "
            "merge, split or delete paragraphs. Keep each replacement "
            "the same KIND of line as the original -- if it began "
            "with a bold label (**Yapı Denetimi:**), keep that label "
            "bold -- and add no markdown the original did not have, "
            "since its list marker, indent and style are preserved "
            "for you already.\n"
            "\nB) They want DIFFERENT content written in this "
            "document's format -- typically because another file is "
            "also attached and they asked for its subject in this "
            "layout. Then this .docx is a FORMAT source, not the "
            "content: return keys filename, title, content, where "
            "content is the new body as markdown (## headings, - "
            "bullets with two spaces per nesting level, 1. numbered "
            "lines, **bold** labels). Do NOT return edits in this "
            "case, and do not carry over the sample document's own "
            "subject matter -- only its shape. The server applies "
            "this template's real fonts, sizes, heading styles and "
            "list formatting to what you write.\n"
            "\nIf both would work, prefer A: it changes less and "
            "keeps more. filename must end with .docx."
        )
    elif wants_interactive_app:
        shape = (
            "Return ONLY JSON with keys: filename, title, html. html MUST "
            "be a COMPLETE, self-contained, working HTML document (inline "
            "<style> and <script>, no external files or CDNs) that "
            "actually implements what the user asked for through real "
            "JavaScript interactivity (event listeners, DOM updates, "
            "working buttons/inputs) — not a description, a static mockup, "
            "or a code fence. Do not wrap the html value in markdown "
            "backticks. filename must end with .html."
        )
    elif needs_html and len(kinds) == 1:
        shape = (
            "Return ONLY JSON with keys: filename, title, summary, content "
            "(report body as plain text). Do NOT emit a full HTML document — "
            "the server builds the HTML deck from title/summary/content. "
            "filename must end with .html."
        )
    elif primary == "pptx":
        # PPTX-only: outline for slides — do not change DOCX/PDF narrative shape.
        shape = (
            "Return ONLY JSON with keys: filename, title, summary, content. "
            "title: a short topic name (e.g. 'Yapay Zeka'), NEVER a bullet "
            "line and NEVER the full user prompt. "
            "content MUST be a PowerPoint outline with 5–8 markdown ## "
            "section headings drawn from the research notes. Under each "
            "heading put 3–4 short '- ' bullets (≤100 chars, one idea). "
            "Cover the main findings from the notes — do not collapse the "
            "whole brief into one section. summary: one short sentence. "
            "Do not write a Kaynaklar section — the server appends sources. "
            "filename must end with .pptx."
        )
    elif use_blocks:
        shape = (
            "Return ONLY JSON with keys: filename, title, summary, blocks. "
            "summary: one or two sentences. blocks is the whole report as an "
            "ordered array; every element is exactly one of:\n"
            '{"type": "heading", "level": 1, "text": "..."} -- level 1 for a '
            "main section, 2 for a subsection, 3 rarely\n"
            '{"type": "paragraph", "text": "..."} -- ONE paragraph of 2-5 '
            "sentences; use several paragraph blocks, never a whole section "
            "in one\n"
            '{"type": "list", "ordered": false, "items": ["...", "..."]} -- '
            'ordered true for steps/rankings; a nested item is {"text": '
            '"...", "items": ["..."]}\n'
            '{"type": "table", "columns": ["..."], "rows": [["...", "..."]]}'
            " -- only for real comparable data from the notes, max 8 columns\n"
            '{"type": "quote", "text": "..."} -- a key finding or warning '
            "worth highlighting, at most two in the report\n"
            "Start EVERY section with a heading block. Inside text you may "
            "mark key terms or figures as **bold**; write no other markup "
            "(no #, no leading - or 1., no | tables) -- structure goes in "
            "the block type, not in the text.\n"
            "Write the report itself and nothing about making it: no block "
            "that suggests images, proposes a layout, or explains your "
            "approach. Do not repeat the title as the first block. "
            "Carry over every section, figure and caveat in the research "
            "notes: this is the full report, about as long as the notes, "
            "not a summary of them."
            f" filename must end with .{primary}."
        )
    else:
        shape = (
            "Return ONLY JSON with keys: filename, title, summary, content "
            "(full report body in light markdown). Start EVERY section of "
            "content with a '## ' heading -- one per topic, in the order "
            "you cover them -- and use '- ' for list items. That markup is "
            "not decoration: the server maps each ## onto a real Word "
            "heading and onto its own slide in the HTML deck, so a report "
            "written as plain lines becomes one wall of text and a "
            "single-slide presentation no matter how much it says.\n"
            "Write the report itself and nothing about making it. No "
            "section that suggests what images to add, proposes a "
            "layout, explains your approach, or addresses the reader "
            "about the document -- a live report ended with a chapter "
            "titled \"Görsel Önerileri (Sunum İçin)\", which is a note to "
            "the author sitting in the reader's deliverable. Every "
            "section must be part of the subject the user asked about."
        )
        shape += f". Primary filename must end with .{primary if primary != 'code' else 'txt'}."
    if needs_html and "html" not in shape:
        shape += (
            " Do NOT include an html key — the server builds the HTML "
            "presentation from title/summary/content."
        )
    if needs_narrative and not needs_table and not keep_docx_table:
        import image_fetch

        if image_fetch.request_wants_image(original_request or content or ""):
            # The user asked for pictures in so many words. Leaving the key
            # "optional" let the model omit it, and an absent image_query
            # is exactly what makes the pdf/docx branch below skip images
            # entirely -- so the ask was dropped with nothing logged, twice
            # in a row including when the user asked why.
            shape += (
                " REQUIRED key image_query: the user explicitly asked for "
                "images, so you MUST supply a short image search term for "
                "one topic photo, in the same language as the request "
                "(e.g. 'mısır tarlası'). Never omit it and never leave it "
                "empty."
            )
        else:
            shape += (
                " Optional key image_query: short image search term for one "
                "topic photo (e.g. 'Palantir Maven')."
            )

    notes_raw = (prior_notes or "").strip()
    notes, research_sources = _split_research_sources(notes_raw)
    notes = _strip_capability_refusals(notes)
    notes = _strip_slide_plan_section(notes)
    # Last line of defence before a deliverable. The social path now hands
    # over a written brief rather than quoted source, but this worker is
    # told to use its notes as evidence and not ignore them -- so if a
    # quoting marker ever reaches here again, it stops here instead of
    # being typeset into the user's PDF. That happened: an 11-page report
    # opened with three pages of forum comments and a <<<ALINTI_...>>>
    # marker.
    notes = untrusted_content.strip_fence_leakage(notes)
    if not research_sources:
        research_sources = _urls_from_notes(notes)
    wants_sources = _request_wants_sources(original_request or content)
    # Word/PDF/PPT (and any research-backed file): always end with real
    # https URLs from Research — user must not have to ask for links.
    if primary in {"pdf", "docx", "pptx", "html"} and (research_sources or notes.strip()):
        wants_sources = True
    notes_cap = _document_notes_cap(
        deep_research=deep_research,
        request_text=original_request or content,
        kind_count=len(kinds),
        wants_interactive_app=wants_interactive_app,
    )
    if len(notes) > notes_cap:
        notes = notes[:notes_cap] + "\n…[truncated]"

    source_rule = (
        " Do NOT write a Kaynaklar/Sources/Kaynakça section yourself and do "
        "NOT invent domain-only citations — the server appends a final "
        "Kaynaklar block with verified full https:// URLs from Research."
    )
    thin_notes = _research_notes_insufficient(notes) or (
        primary in {"pdf", "docx", "pptx", "html"} and not research_sources
    )
    if thin_notes and _workspace_has_uploaded_sources(workspace_root):
        # The user gave us a document, so there IS material -- the upload's
        # text is already in `content` (main.py's build_workspace_context).
        # Empty research notes say nothing about that.
        #
        # This used to also require that Research never ran, to tell "never
        # asked" from "asked and failed". That distinction cost a live
        # failure: a PDF attached with "bunu ÖMER İNŞAAT için yapar mısın,
        # docx de olur" had Research in its role list, so the guard did not
        # fire, the Document worker skipped its model call entirely, and
        # the user got a two-line "Kaynaklar: doğrulanmış sayfa
        # bulunamadı" over a document they had just handed us. Whether a
        # web search also happened is irrelevant when the source is in the
        # workspace.
        thin_notes = False
    if thin_notes and primary in {"pdf", "docx", "pptx", "html"}:
        source_rule += (
            " CRITICAL: Research notes say sources failed or lack the asked "
            "facts. Do NOT write a general-knowledge essay, sector overview, "
            "or invented ranking. Write a short deliverable that states the "
            "requested facts could not be verified from opened pages, what "
            "was missing, and stop. Empty list placeholders are OK; polished "
            "filler prose is forbidden."
        )

    system = (
        f"You prepare deliverable file(s): {', '.join(kinds)}. The server "
        "WILL write the real file(s) after you respond — you must NOT say "
        "you cannot create files, browse, or produce downloads. Never copy "
        "refusal lines from research notes (e.g. 'PDF oluşturamıyorum', "
        "'rapor için önerilen yapı', 'taslak olarak kullanın') into the "
        "deliverable. Write the ACTUAL report body with facts and figures "
        "from the notes — not meta-instructions telling the user how to "
        "build a PDF themselves. "
        + shape
        + " Use prior research notes as the source of truth; do not invent "
        "numbers. Keep columns/rows compact"
        + (" (max ~40 rows)." if keep_docx_table else " (max ~12 rows).")
        + " If a value is missing, use an empty string — do not invent clubs "
        "or scores. When the user asked for Takım/Team and notes name the "
        "club, you MUST include it."
        + (
            (
                " For Word/PDF/PowerPoint: put the FULL research narrative in "
                + (
                    "blocks, one heading block per section followed by paragraph "
                    "blocks of "
                    if use_blocks
                    else "summary/content as numbered sections (1. 2. 3. …), each with "
                )
                + "2–4 sentences from the notes. Prefer depth over a short "
                "overview. Cover technical, operational, and disputed/sensitive "
                "angles when the notes support them. Do not omit a section "
                "because notes are thin — write from notes plus clearly marked "
                "limits. Do not insert fake 'Sayfa N / M' lines in the body; "
                "the server adds real page numbers. "
                "CROSS-SECTION CONSISTENCY: when the notes give the same "
                "metric a different value at a different date in more than "
                "one place (e.g. a headline count in an early section vs. a "
                "comparison table later), use the most recent value in EVERY "
                "section/table of this file, not just the first place it "
                "appears — never let one of your own sections contradict "
                "another. Carry over the notes' own gap disclosures (a "
                "section the notes say could not be verified) instead of "
                "smoothing them into confident prose."
            )
            if (needs_narrative or paged or primary in {"pdf", "docx", "pptx"})
            and not thin_notes
            and not keep_docx_table
            else ""
        )
        + source_rule
        + " Write summary/content as clean report prose only: no chat "
        "openers (Elbette/Sure), no echoing the user prompt, "
        + ("" if use_blocks else "no markdown bold markers, ")
        + "no MACHINE blocks, no 'I cannot create PDF' lines. "
        + (
            " When research notes contain verified figures, write a REAL "
            "report (intro + findings with those numbers + short conclusion). "
            "Do NOT pad with 'suggested outline', 'how to structure a report', "
            "or filler sector essays that invent extra numbers."
            if not thin_notes and primary in {"pdf", "docx", "pptx", "html"}
            else ""
        )
        + render_skill_guidance(skills)
        + runtime_context.runtime_block(
            frozenset({capabilities.ARTIFACTS}),
            request_text=original_request or content,
        )
    )
    user = (
        f"Request:\n{original_request or content}\n\n"
        f"Assigned work:\n{content}\n\n"
        f"Target format(s): {', '.join(kinds)}\n\n"
        f"Research notes / evidence:\n{notes or '(none)'}\n"
    )
    if wants_template_match and template_path:
        import document_export

        _template_paras = document_export.template_paragraphs(
            Path(workspace_root or "") / template_path
        )
        if _template_paras:
            user += (
                "\n\nTHE USER'S UPLOADED DOCUMENT, numbered:\n"
                + "\n".join(
                    f"{i}: {t}" for i, t in enumerate(_template_paras) if t
                )
            )

    try:
        import asyncio

        doc_max_tokens = _doc_token_budget(
            notes,
            paged=paged,
            primary=primary,
            deep_research=deep_research,
            # The length to write from is the uploaded material, which
            # arrives in `content` (main.py's build_workspace_context) and
            # never in the research notes. Not only on the template path:
            # a PDF attached with "bunu ÖMER İNŞAAT için yapar mısın, docx
            # de olur" has no .docx to clone, so this was empty, the budget
            # fell to the base and the wait with it -- the model got 45s to
            # write a document out of a tender specification, timed out,
            # and the user received two lines over the file they had just
            # uploaded.
            source_text=content if _workspace_has_uploaded_sources(workspace_root) else "",
        )
        if use_blocks:
            # Block JSON carries ~15% more syntax than the same markdown, and
            # a reasoning model needs room to think before it writes any.
            doc_max_tokens = min(
                _DOC_MAX_TOKENS_CEILING,
                int(doc_max_tokens * 1.15) + _DOC_BLOCKS_REASONING_HEADROOM,
            )
        # Failed research: skip the slow Document LLM — write a short failure
        # file from notes so the user is not billed another minutes of wait.
        # Ready research brief: same skip — Research already wrote the report
        # body; a second LLM packaging call often hangs (full timeout) then
        # falls back to these same notes anyway.
        if thin_notes and primary in {"pdf", "docx", "pptx", "html"}:
            import document_export

            progress("building", "thin_research_short_failure")
            fail_body = (
                notes.strip()
                or (
                    "Açılan web sayfalarından istenen veriler doğrulanamadı. "
                    "Sayfalar zaman aşımına uğradı veya okunamadı; uydurma "
                    "rakam yazılmadı. Lütfen sorguyu daraltıp yeniden deneyin."
                )
            )
            raw = ""
            payload = {
                "filename": f"rapor.{primary}",
                "title": document_export.short_report_title(
                    original_request or content or "Rapor",
                    fallback="Araştırma sonucu",
                ),
                "summary": fail_body[:4000],
                "columns": [],
                "rows": [],
                "content": fail_body[:4000],
            }
        # With typed blocks on, PDF/DOCX always go through the Document LLM:
        # copying the notes straight in would leave no blocks to render.
        elif not use_blocks and _notes_ready_for_direct_deliverable(
            notes,
            primary=primary,
            kinds=kinds,
            keep_docx_table=keep_docx_table,
        ):
            import document_export

            progress("building", "direct_from_research_notes")
            body = notes.strip()
            raw = ""
            if primary == "pptx":
                deck_title = document_export.resolve_presentation_title(
                    original_request or content or "",
                    body=body,
                    fallback="Sunum",
                )
            else:
                deck_title = document_export.short_report_title(
                    original_request or content or body[:120],
                    fallback="Rapor",
                )
            payload = {
                "filename": f"rapor.{primary}",
                "title": deck_title,
                "summary": body[:4000],
                "columns": [],
                "rows": [],
                # No model call happens on this path -- the brief is copied
                # straight into the file -- so there is no token budget to
                # respect and the cap only lost content: a 21.7k-char report
                # reached the PDF cut mid-word inside its last section
                # ("Bilgi ve farkındalık eksikliği: Tüketi"), with the
                # sources block appended after the wound. The brief is
                # already bounded upstream by _MAX_DEEP_DOC_NOTES_CHARS.
                # Slides stay clipped: they have their own layout limits.
                "content": body if primary != "pptx" else body[:12000],
            }
        else:
            if use_blocks:
                document_logger.info(
                    "document path: calling Document LLM for typed blocks "
                    "(kind=%s, notes=%d chars, max_tokens=%d)",
                    primary,
                    len(notes),
                    doc_max_tokens,
                )
            try:
                raw = await asyncio.wait_for(
                    complete_chat(
                        [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        tracker=tracker,
                        stage="document_agent",
                        max_tokens=doc_max_tokens,
                        temperature=0,
                        response_format=None,
                        # A reply cut at the ceiling is re-asked once at double
                        # the room; an empty one is otherwise indistinguishable
                        # from a model that had nothing to say.
                        retry_on_truncation=True,
                        # Blocks are one large JSON document: ask the
                        # provider to guarantee it is well-formed.
                        json_mode=use_blocks,
                        extra_body=_document_llm_extra_body() or None,
                    ),
                    # The wait covers that retry, so it can actually finish.
                    timeout=_document_llm_timeout(
                        doc_max_tokens * 2 if use_blocks else doc_max_tokens
                    ),
                )
            except asyncio.TimeoutError:
                stabilization_logger.warning(
                    "Document Agent LLM timed out after %.0fs; writing from notes",
                    _document_llm_timeout(doc_max_tokens),
                )
                progress(
                    "building",
                    f"document_llm_timeout_{int(_DOCUMENT_LLM_TIMEOUT_SECONDS)}s",
                )
                raw = ""
            payload = _parse_json_object(raw or "")
            if not payload:
                # Model returned prose/refusal instead of JSON — still write
                # from research notes rather than failing the job.
                import document_export

                # Raw reply only when it is actually prose. _parse_json_object
                # now salvages truncated JSON, so anything still unparsed
                # here is either prose (usable) or malformed JSON (not) --
                # and writing the latter put literal braces, keys, and "\n"
                # escapes into a user's .docx. Falling back to the notes, or
                # to nothing, is recoverable; shipping that is not.
                salvage = (raw or "").strip()
                if salvage.startswith("{") or salvage.startswith("["):
                    # Keep the reply: without it an unparseable answer can only
                    # be guessed at after the fact.
                    saved_to = ""
                    try:
                        debug_dir = Path(workspace_root or "") / "debug"
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        debug_file = debug_dir / f"document_reply_{int(time.time())}.txt"
                        debug_file.write_text(salvage, encoding="utf-8")
                        saved_to = str(debug_file)
                    except OSError:
                        pass
                    stabilization_logger.warning(
                        "Document Agent returned unparseable JSON (%d chars); "
                        "writing from notes instead of the raw reply (saved: %s)",
                        len(salvage),
                        saved_to or "no",
                    )
                    salvage = ""
                fallback_body = notes or salvage[:8000]
                if not fallback_body and wants_template_match and template_path:
                    # A template rewrite has no research notes by
                    # definition, so the ordinary fallback is empty and the
                    # user got a two-paragraph file over their own
                    # document. The attachment's extracted text sits beside
                    # it in the workspace: writing that into the template
                    # returns their document, in their formatting, with the
                    # rename not applied -- visibly incomplete rather than
                    # silently blank.
                    extracted = Path(workspace_root or "") / (
                        template_path + ".extracted.txt"
                    )
                    try:
                        if extracted.is_file():
                            fallback_body = extracted.read_text(
                                encoding="utf-8", errors="replace"
                            ).strip()[:20000]
                    except OSError:
                        pass
                payload = {
                    "filename": f"rapor.{primary}",
                    "title": document_export.short_report_title(
                        notes or "Rapor", fallback="Rapor"
                    ),
                    "summary": fallback_body,
                    "columns": [],
                    "rows": [],
                    "content": fallback_body,
                }
        if wants_interactive_app and not str(payload.get("html") or "").strip():
            # Models routinely wrap real code in a markdown fence (or just
            # start straight with it) even when told to return an `html`
            # JSON key instead -- recover it here, before sanitize_report_prose
            # (which only touches summary/content/body, never html) gets a
            # chance to mangle the fence while cleaning up chat-style prose.
            for key in ("content", "body", "summary"):
                extracted = _extract_html_document(str(payload.get(key) or ""))
                if extracted:
                    payload["html"] = extracted
                    break
            else:
                # Document's OWN response sometimes just repeats the prior
                # worker's intro sentence and drops the code entirely (real
                # bug: Coding Agent wrote a complete, working to-do app —
                # visible in its own task output — but Document's JSON only
                # echoed "here is a to-do app" with no fence at all). If an
                # earlier role (e.g. Coding) already produced real HTML,
                # that is the actual deliverable -- use it directly rather
                # than asking Document to faithfully retype it.
                extracted = _extract_html_document(notes)
                if extracted:
                    payload["html"] = extracted
        filename = str(payload.get("filename") or f"deliverable.{primary}")
        _sanitize_document_payload(payload, keep_bold=wants_template_match)
        # Strip any leftover "cannot create PDF / suggested outline" prose
        # the Document LLM may have copied from research notes.
        for key in ("summary", "content", "body", "title"):
            if payload.get(key):
                payload[key] = _strip_capability_refusals(str(payload[key]))
        if use_blocks and payload.get("blocks") is not None:
            # Mirrors the blocks into `content` as markdown, so every step
            # below (length check, sources, HTML/PPTX extras) works unchanged.
            block_document = document_model.attach_blocks_to_payload(
                payload, text_filter=_strip_capability_refusals
            )
            if block_document is not None:
                progress("building", "typed_blocks")
                document_logger.info(
                    "document path: received %d typed blocks (%d chars as markdown)",
                    len(block_document.blocks),
                    len(str(payload.get("content") or "")),
                )
            else:
                document_logger.warning(
                    "document path: blocks were unusable -> markdown fallback"
                )
        elif use_blocks:
            if not (raw or "").strip():
                document_logger.warning(
                    "document path: Document LLM returned an EMPTY reply (output "
                    "budget used up or timed out; max_tokens=%d) -> markdown fallback",
                    doc_max_tokens,
                )
            else:
                document_logger.warning(
                    "document path: Document LLM reply had no blocks key -> markdown "
                    "fallback (reply=%d chars, starts=%r)",
                    len(raw),
                    raw[:80],
                )
        summary = str(payload.get("summary") or "").strip()
        title = str(payload.get("title") or summary or "Report").strip()
        body = str(
            payload.get("content") or payload.get("body") or summary or ""
        ).strip()
        if not summary and not body and notes:
            summary = notes
            body = notes
            payload["summary"] = summary
            payload["content"] = body
            _sanitize_document_payload(payload, keep_bold=wants_template_match)
            for key in ("summary", "content", "body", "title"):
                if payload.get(key):
                    payload[key] = _strip_capability_refusals(str(payload[key]))
            summary = str(payload.get("summary") or "").strip()
            title = str(payload.get("title") or summary or "Report").strip()
            body = str(
                payload.get("content") or payload.get("body") or summary or ""
            ).strip()

        # PPTX-only packaging: real topic title + prefer full research notes
        # over a thin Document outline (fixes empty/wrong decks).
        if primary == "pptx":
            import document_export

            notes_body = _strip_kaynaklar_section(
                _strip_capability_refusals(notes or "")
            ).strip()
            if notes_body and (
                _pptx_outline_too_thin(body) or len(notes_body) > len(body) + 200
            ):
                body = notes_body
                summary = notes_body[:600]
                payload["content"] = body
                payload["summary"] = summary
            payload["title"] = document_export.resolve_presentation_title(
                original_request or content or "",
                document_title=title or "",
                body=body or notes_body,
                fallback="Sunum",
            )
            title = str(payload["title"])
            # No fake Bölüm/İçerik table slides for narrative decks.
            payload["rows"] = []
            payload["columns"] = []
            columns, rows = [], []
        else:
            # Narrative reports hit the same failure the deck guard above
            # was written for, and had no equivalent. Document is meant to
            # format the brief, not to lose it: a 21k-char research report
            # with four asked-for sections came back as 5k of tables under
            # a stray subsection heading ("4.2 Ödeme Sistemi Parçalılığı"),
            # with the incentives and barriers sections simply gone.
            # Research already writes report-quality markdown, so when the
            # write-up drops most of it, the brief is the better document.
            if primary in {"pdf", "docx", "html"} and not wants_interactive_app:
                import document_export

                notes_body = _strip_kaynaklar_section(
                    _strip_capability_refusals(notes or "")
                ).strip()
                # How thin is "too thin" depends on whether there are typed
                # blocks to lose. Replacing them does not just swap text: it
                # breaks the fingerprint in resolve_payload_document, which
                # discards the blocks, which drops the PDF onto the markdown
                # writer -- reportlab, with no cover, no accent headings and
                # plain tables. The design is collateral damage from a
                # length comparison.
                #
                # Measured: a deep run's Document reply came back as 46
                # typed blocks, 10,614 characters of markdown, against
                # 18,134 characters of notes. 0.6 put the bar at 10,880, so
                # a perfectly good report lost by 266 characters and the
                # user got an undesigned PDF. The ratio also punishes deep
                # mode structurally -- more notes raise the bar a condensed
                # write-up has to clear, so the better the gathering, the
                # likelier the design is thrown away.
                #
                # With blocks present the test becomes "did the writer
                # actually lose the brief", not "is it shorter than the
                # notes": a low ratio AND an absolute floor. Without blocks
                # there is no design at stake, so the original 0.6 stands.
                if payload.get("blocks"):
                    thinness_floor = min(len(notes_body) * 0.35, 4_000)
                else:
                    thinness_floor = len(notes_body) * 0.6
                if notes_body and len(body) < thinness_floor:
                    if payload.get("blocks"):
                        document_logger.warning(
                            "document path: write-up too short (%d vs %d notes chars, "
                            "floor %d) -> research notes replace it, markdown fallback "
                            "(designed renderer lost)",
                            len(body),
                            len(notes_body),
                            int(thinness_floor),
                        )
                    body = notes_body
                    summary = notes_body[:600]
                    payload["content"] = body
                    payload["summary"] = summary
                    # The dropped title came from the same bad response.
                    payload["title"] = document_export.short_report_title(
                        notes_body, fallback=title or "Rapor"
                    )
                    title = str(payload["title"])
            columns = payload.get("columns") or []
            rows = payload.get("rows") or []
        # Long prose reports: skip synthetic key-points grids. Ranking /
        # column asks keep the real data table so Word renders it properly.
        if (paged or primary in {"pdf", "docx"}) and not keep_docx_table:
            payload["rows"] = []
            payload["columns"] = []
            columns, rows = [], []
        elif keep_docx_table and isinstance(rows, list) and rows:
            # Avoid a second table: strip markdown pipe grids from prose when
            # columns/rows already carry the ranking.
            for key in ("summary", "content", "body"):
                value = str(payload.get(key) or "")
                if value:
                    payload[key] = _strip_markdown_tables(value)
        # Always attach verified full https URLs (or an explicit empty notice)
        # for narrative deliverables and whenever Research found pages --
        # except a template-match body, which must stay exactly what the
        # user asked to be written into their own uploaded document (no
        # server-appended Kaynaklar list in someone else's legal template).
        if not wants_template_match and (research_sources or wants_sources or primary in {
            "pdf",
            "docx",
            "pptx",
            "html",
        }):
            _inject_sources_into_payload(
                payload, research_sources, required=True
            )
            _sanitize_document_payload(payload)
            summary = str(payload.get("summary") or "").strip()
            body = str(
                payload.get("content") or payload.get("body") or summary or ""
            ).strip()
            title = str(payload.get("title") or summary or "Report").strip()
            if primary == "pptx":
                import document_export

                payload["title"] = document_export.resolve_presentation_title(
                    original_request or content or "",
                    document_title=str(payload.get("title") or title or ""),
                    body=body or summary,
                    fallback="Sunum",
                )
                title = str(payload["title"])
                payload["rows"] = []
                payload["columns"] = []
                columns, rows = [], []
            elif (paged or primary in {"pdf", "docx"}) and not keep_docx_table:
                payload["rows"] = []
                payload["columns"] = []
                columns, rows = [], []
            else:
                columns = payload.get("columns") or []
                rows = payload.get("rows") or []
                if keep_docx_table and isinstance(rows, list) and rows:
                    for key in ("summary", "content", "body"):
                        value = str(payload.get(key) or "")
                        if value:
                            payload[key] = _strip_markdown_tables(value)
                    summary = str(payload.get("summary") or "").strip()
                    body = str(
                        payload.get("content")
                        or payload.get("body")
                        or summary
                        or ""
                    ).strip()

        artifact = ArtifactTools(
            Path(workspace_root),
            source_request=original_request or content,
        )
        if tool_call_recorder is not None:
            # Built directly rather than via capabilities.resolve_tools (the
            # only place that currently wraps tools with the recorder), so
            # every create_*_artifact call was invisible to the "Tool
            # activity" panel regardless of how many files got written.
            # Every verb that produces a file, not just create_: an
            # edit_docx_template run wrote the user's document and the
            # panel still said "No tool calls were recorded".
            for name in dir(artifact):
                if name.startswith(("create_", "edit_")):
                    setattr(
                        artifact, name, logged_tool(getattr(artifact, name), tool_call_recorder)
                    )
        image_query = str(payload.get("image_query") or "").strip()
        # PPTX/HTML may embed a topic photo. PDF/Word only when the user asked.
        # Never decorate a failed/thin research deliverable with a random stock photo.
        # Drop off-topic image queries (e.g. bare "Türkiye" → election maps).
        # Both sides folded. The model writes proper Turkish while users
        # routinely type without diacritics, so comparing raw text made
        # this drop good queries: asked for "misir uretilicegiligi ...
        # gorsellerle zenginlestir", the model's "mısır tarlası" matched
        # nothing in the request and the image was silently skipped --
        # twice, including when the user asked why no image came.
        ask_blob = _fold_tr(original_request or content or "")
        if image_query:
            q_tokens = [
                t
                for t in re.findall(r"[a-z0-9]{4,}", _fold_tr(image_query))
                if t not in {"turkiye", "turkey", "report", "rapor", "image", "photo"}
            ]
            if q_tokens and not any(t in ask_blob for t in q_tokens):
                image_query = ""
        wants_deck = any(k in {"pptx", "html"} for k in kinds)
        if thin_notes:
            shared_images = []
        elif wants_deck:
            shared_images = artifact._fetch_images(image_query, context=title)
        elif primary in {"pdf", "docx"} and not image_query:
            # No on-topic Commons query → skip rather than title-fallback
            # random maps (Rize/Laz settlement, election charts, etc.).
            shared_images = []
        else:
            shared_images = artifact._fetch_images(
                image_query, context=title, require_user_ask=True
            )
        created_paths: list[str] = []
        if wants_template_match and template_path:
            # Edit the uploaded .docx in place: the model named the
            # paragraphs that change, and every other one keeps its exact
            # wording and formatting because it is never rewritten.
            #
            # Falls back to the whole-body renderer when the model returned
            # prose instead of an edit list -- older behaviour, still
            # correct, just slower and less faithful.
            edits = payload.get("edits")
            created = None
            if isinstance(edits, dict) and edits:
                try:
                    created = artifact.edit_docx_template(
                        filename, template_path, edits
                    )
                except Exception as exc:  # noqa: BLE001
                    stabilization_logger.warning(
                        "edit_docx_template failed (%s); rewriting the body",
                        exc,
                    )
            if created is None:
                created = artifact.create_docx_from_template(
                    filename,
                    template_path,
                    title=title,
                    content=body,
                )
            path = created.get("path") or created.get("filename") or ""
            if path:
                created_paths.append(f"`{path}` [docx]")
        else:
            for kind in kinds:
                piece = dict(payload)
                if kind == "html" and not str(piece.get("html") or "").strip():
                    piece["html"] = _html_deck_for(
                        piece,
                        title=title,
                        summary=summary,
                        columns=list(columns) if isinstance(columns, list) else [],
                        rows=list(rows) if isinstance(rows, list) else [],
                        body=body,
                        images=shared_images,
                    )
                    piece["filename"] = str(
                        piece.get("html_filename")
                        or _ensure_extension(
                            Path(filename).stem + "-sunum", "html"
                        )
                    )
                elif kind != primary:
                    piece["filename"] = _ensure_extension(
                        Path(filename).stem, kind
                    )
                created = _write_artifact(
                    artifact,
                    kind=kind,
                    filename=str(piece.get("filename") or filename),
                    payload=piece,
                    images=shared_images,
                )
                path = created.get("path") or created.get("filename") or ""
                if path:
                    created_paths.append(f"`{path}` [{kind}]")

        progress("done", ", ".join(created_paths) or filename)
        file_notice = (
            "İndirilebilir dosya(lar) oluşturuldu: "
            + (", ".join(created_paths) if created_paths else filename)
            + "."
        )
        if "html" in kinds:
            file_notice += (
                " HTML sunumu tarayıcıda açmak için indirdikten sonra "
                "dosyaya çift tıklayın."
            )
        output = _compose_document_chat_answer(
            prior_notes=notes_raw or prior_notes or "",
            file_notice=file_notice,
            title=title,
            kinds=kinds,
            table_markdown=_markdown_table_from_grid(
                payload.get("columns"),
                payload.get("rows"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        stabilization_logger.exception("LangGraph document artifact failed")
        output = (
            "Document Agent could not write the file "
            f"({type(exc).__name__}: {exc}). Research notes were:\n"
            f"{(prior_notes or '')[:2000]}"
        )

    _lifecycle_done(
        callback,
        worker_id=worker_id,
        unit_task_id=unit_task_id,
        result_summary=output,
        started=started,
    )
    return output


def _build_graph(
    *,
    callback: Any,
    tracker: UsageTracker,
    tool_call_recorder: Optional[ToolCallRecorder],
    task_id: str,
    handle: CancellableWorkforce,
    workspace_root: Optional[str],
    skills: Optional[list],
    wants_artifact: bool,
    artifact_type: str,
    job_capabilities: Optional[frozenset] = None,
    deep_research: bool = False,
    conversation_id: Optional[str] = None,
    reuses_uploaded_document: Optional[bool] = None,
    # run_next below reads this. It was referenced there but never declared
    # here, so the ROLE_GENERAL branch raised "NameError: name
    # 'opens_on_user_computer' is not defined" and failed the whole job.
    # `and` short-circuits, so only jobs whose role is ROLE_GENERAL ever
    # evaluated it -- research jobs passed, and an attached document ("bu
    # pdf'i özetle") died instantly.
    opens_on_user_computer: Optional[bool] = None,
) -> Any:
    async def run_next(state: WorkforceState) -> dict[str, Any]:
        if handle.cancelled:
            return {"cancelled": True, "answer": state.get("answer") or ""}
        roles = list(state.get("roles") or [])
        index = int(state.get("role_index") or 0)
        if index >= len(roles):
            return {"answer": state.get("answer") or state.get("notes") or ""}

        role = roles[index]
        unit_id = f"{task_id}.{index}"
        content = state.get("content") or ""
        request = state.get("original_request") or content
        notes = state.get("notes") or ""

        if role == ROLE_GENERAL and (
            _request_wants_local_open(request)
            if opens_on_user_computer is None
            else bool(opens_on_user_computer)
        ):
            # Deterministic, no model tool-calling involved: _run_llm_worker
            # (below, for the ordinary ROLE_GENERAL case) is a plain text
            # completion with no bound tools in this LangGraph executor --
            # job_capabilities there only adds descriptive text to the
            # prompt (runtime_context.runtime_block), it does not let the
            # model actually invoke anything. "Open this on my computer" is
            # a real action, not prose, so it is performed directly here
            # and the model never enters the loop for this step.
            text = _open_local_file_step(
                workspace_root=workspace_root, conversation_id=conversation_id
            )
        elif role == ROLE_MAIL:
            text = await _run_mail(
                content=content,
                original_request=request,
                tracker=tracker,
                callback=callback,
                workspace_root=workspace_root,
                tool_call_recorder=tool_call_recorder,
                handle=handle,
                unit_task_id=unit_id,
                deep_research=deep_research,
            )
        elif role == ROLE_SOCIAL:
            text = await _run_social(
                content=content,
                original_request=request,
                tracker=tracker,
                callback=callback,
                tool_call_recorder=tool_call_recorder,
                handle=handle,
                unit_task_id=unit_id,
                deep_research=deep_research,
            )
        elif role == ROLE_JIRA:
            text = await _run_jira(
                content=content,
                original_request=request,
                tracker=tracker,
                callback=callback,
                tool_call_recorder=tool_call_recorder,
                handle=handle,
                unit_task_id=unit_id,
                deep_research=deep_research,
            )
        elif role == ROLE_RESEARCH:
            text = await _run_research(
                content=content,
                original_request=request,
                tracker=tracker,
                callback=callback,
                workspace_root=workspace_root,
                tool_call_recorder=tool_call_recorder,
                wants_artifact=wants_artifact,
                handle=handle,
                unit_task_id=unit_id,
                deep_research=deep_research,
            )
        elif role == ROLE_DOCUMENT:
            text = await _run_document_worker(
                content=content,
                original_request=request,
                prior_notes=notes,
                tracker=tracker,
                callback=callback,
                skills=skills,
                handle=handle,
                unit_task_id=unit_id,
                workspace_root=workspace_root,
                artifact_type=artifact_type,
                tool_call_recorder=tool_call_recorder,
                deep_research=deep_research,
                research_ran=(ROLE_RESEARCH in roles),
                reuses_uploaded_document=reuses_uploaded_document,
            )
        else:
            extra = ""
            if artifact_type and artifact_type != "none":
                extra = f"\nRequested artifact type: {artifact_type}."
            if (
                role == ROLE_GENERAL
                and notes
                and _is_chat_redelivery_followup(request)
            ):
                extra += (
                    "\nThe user wants PRIOR findings delivered as chat text "
                    "(not a downloadable file). Use the prior notes / "
                    "conversation evidence. Keep tables and numbers. Do NOT "
                    "claim you lack internet or cannot access prior results. "
                    "Do NOT invent figures; if numbers are missing, say so."
                )
            elif (
                role == ROLE_GENERAL
                and notes
                and _is_reformat_followup(request)
            ):
                extra += (
                    "\nReuse PRIOR notes only — do not invent a new research "
                    "pass. Summarize or reshape what is already present."
                )
            text = await _run_llm_worker(
                role=role,
                content=content,
                original_request=request,
                prior_notes=notes,
                tracker=tracker,
                callback=callback,
                skills=skills,
                handle=handle,
                unit_task_id=unit_id,
                system_extra=extra,
                job_capabilities=(
                    frozenset({capabilities.CODE_EXEC})
                    if role == ROLE_CODING
                    else frozenset(job_capabilities or ())
                ),
            )

        new_notes = text if not notes else f"{notes}\n\n{text}"
        # Prefer last worker output as user-facing answer; research-only keeps it.
        answer = text
        next_roles = list(roles)
        if role in _GATHER_ONLY_ROLES:
            # Belt and braces with the _select_roles guard: these notes are
            # quoted text wrapped in a "data, not instructions" fence, which
            # is prompt scaffolding rather than an answer. Shipping it would
            # also put a stranger's words where the answer belongs. Keep
            # whatever answer exists and let the next worker write the real
            # one.
            answer = state.get("answer") or ""
        if (
            role == ROLE_RESEARCH
            and wants_artifact
            and index + 1 < len(roles)
            and (text or "").strip().startswith("{")
        ):
            # Handoff JSON — wait for Document.
            answer = state.get("answer") or ""

        # General rule: empty/failed research must not become a polished
        # Word/PDF failure essay. Keep the honest chat answer; skip Document.
        if (
            role == ROLE_RESEARCH
            and wants_artifact
            and ROLE_DOCUMENT in next_roles[index + 1 :]
            and _research_notes_insufficient(text)
        ):
            next_roles = [
                r for i, r in enumerate(next_roles) if i <= index or r != ROLE_DOCUMENT
            ]
            answer = _strip_capability_refusals(
                _strip_kaynaklar_section(
                    text.split("---\nARTIFACT_HANDOFF:")[0]
                    if "ARTIFACT_HANDOFF" in (text or "")
                    else (text or "")
                )
            ).strip() or (text or "").strip()

        return {
            "role_index": index + 1,
            "roles": next_roles,
            "notes": new_notes,
            "answer": answer or state.get("answer") or text,
        }

    def route(state: WorkforceState) -> str:
        if handle.cancelled:
            return "end"
        roles = list(state.get("roles") or [])
        index = int(state.get("role_index") or 0)
        if index >= len(roles):
            return "end"
        return "run_next"

    graph = StateGraph(WorkforceState)
    graph.add_node("run_next", run_next)
    graph.add_edge(START, "run_next")
    graph.add_conditional_edges(
        "run_next",
        route,
        {"run_next": "run_next", "end": END},
    )
    return graph.compile()


async def _run_workforce_async(
    *,
    callback: Any,
    tracker: UsageTracker,
    tool_call_recorder: Optional[ToolCallRecorder],
    content: str,
    task_id: str,
    handle: CancellableWorkforce,
    workspace_root: Optional[str],
    fast_path_role: Optional[str],
    skills: Optional[list],
    job_capabilities: Optional[frozenset],
    original_request: Optional[str],
    wants_artifact: bool,
    artifact_type: str,
    deep_research: bool = False,
    conversation_id: Optional[str] = None,
    reuses_uploaded_document: Optional[bool] = None,
    opens_on_user_computer: Optional[bool] = None,
    reads_email: Optional[bool] = None,
    reads_social: Optional[bool] = None,
    reads_jira: Optional[bool] = None,
) -> Any:
    request = (original_request or content or "").strip()
    skip_web_research = _is_reformat_followup(request) and _has_prior_turn_material(
        content or ""
    )
    # "chatte ver / xlsx istemedim" must never schedule Document again.
    if _is_chat_redelivery_followup(request):
        wants_artifact = False
        artifact_type = "none"
    # "aç bunu bilgisayarımda" is a direct local action, not new research or
    # a new file -- force the deterministic fast-path in run_next
    # (_open_local_file_step) regardless of what artifact_intent classified
    # wants_artifact/job_capabilities as for this turn (a router model call
    # can easily re-classify a short followup as wanting another file, which
    # would otherwise schedule Research/Document and never even reach
    # ROLE_GENERAL where this fast-path lives).
    # The classifier decides; the pattern is the fallback for when it did
    # not run. "html ve pptx sunum verip acar misin lutfen" asked plainly
    # for the files to be opened and the pattern missed it -- it required
    # the word "bilgisayarımda" -- so the user got both files and no open.
    if opens_on_user_computer is None:
        wants_local_open = _request_wants_local_open(request)
    else:
        wants_local_open = bool(opens_on_user_computer)
    # BUT: "PDF hazırla ve bilgisayarımda aç" in ONE message still needs the
    # file actually created first -- wants_artifact was independently true
    # for THIS same turn (not just carried over from a prior one), so there
    # is a real file to build, not just an old one to find. Skipping straight
    # to the fast-path here produced a 0.1s job with nothing on disk (no
    # Research/Document ever ran) and a "no_filename" error, even though the
    # request explicitly asked for a PDF. Only take the pure-open fast-path
    # (skip Research/Document entirely) when there is nothing new to build.
    combined_create_and_open = wants_local_open and wants_artifact
    if wants_local_open and not combined_create_and_open:
        wants_artifact = False
        artifact_type = "none"
    # Keep Research scheduling on the user's explicit citation ask (or
    # normal WEB_* routing). Do not force wants_sources for every PDF —
    # that triggered long recovery loops.
    wants_sources = _request_wants_sources(request)
    roles = (
        [ROLE_GENERAL]
        if (wants_local_open and not combined_create_and_open)
        else _select_roles(
            job_capabilities=job_capabilities,
            fast_path_role=fast_path_role,
            wants_artifact=wants_artifact,
            wants_sources=wants_sources,
            skip_web_research=skip_web_research,
            needs_live_data=_request_needs_live_data(request),
            reads_email=bool(reads_email),
            reads_social=bool(reads_social),
            reads_jira=bool(reads_jira),
        )
    )
    seeded_notes = (
        _last_assistant_excerpt(content or "") if skip_web_research else ""
    )

    app = _build_graph(
        callback=callback,
        tracker=tracker,
        tool_call_recorder=tool_call_recorder,
        task_id=task_id,
        handle=handle,
        workspace_root=workspace_root,
        skills=skills,
        wants_artifact=wants_artifact,
        artifact_type=artifact_type or "none",
        job_capabilities=job_capabilities,
        deep_research=deep_research,
        conversation_id=conversation_id,
        reuses_uploaded_document=reuses_uploaded_document,
        opens_on_user_computer=opens_on_user_computer,
    )

    final_state = await app.ainvoke(
        {
            "content": content,
            "original_request": request,
            "roles": roles,
            "role_index": 0,
            "notes": seeded_notes,
            "answer": seeded_notes,
            "cancelled": False,
        }
    )

    final_answer = (final_state.get("answer") or final_state.get("notes") or "").strip()
    notes_for_doc = (final_state.get("notes") or final_answer or "").strip()

    # Safety net: only if Document never ran. Initial ``roles`` always lists
    # Document when wants_artifact — that must NOT count as "already ran".
    # When Research strips Document because notes lack the asked facts,
    # do not mint an empty report.pdf / xlsx. Partial multi-target briefs
    # with real figures are not "insufficient" and still get packaged.
    final_roles = list(final_state.get("roles") or [])
    document_already_ran = ROLE_DOCUMENT in final_roles
    skip_empty_doc = _research_notes_insufficient(notes_for_doc)
    if (
        wants_artifact
        and not handle.cancelled
        and not document_already_ran
        and not skip_empty_doc
    ):
        existing = _artifacts_on_disk(workspace_root)
        if not existing:
            doc_out = await _run_document_worker(
                content=content,
                original_request=request,
                prior_notes=notes_for_doc,
                tracker=tracker,
                callback=callback,
                skills=skills,
                handle=handle,
                unit_task_id=f"{child_task_id(task_id)}.document-ensure",
                workspace_root=workspace_root,
                artifact_type=artifact_type or "none",
                tool_call_recorder=tool_call_recorder,
                deep_research=deep_research,
                research_ran=(ROLE_RESEARCH in final_roles),
                reuses_uploaded_document=reuses_uploaded_document,
            )
            if (doc_out or "").strip():
                final_answer = doc_out.strip()
            if workspace_root and not _artifacts_on_disk(workspace_root):
                stabilization_logger.warning(
                    "wants_artifact=True but no files under %s/artifacts after "
                    "Document ensure pass",
                    workspace_root,
                )
    elif (
        final_answer.startswith("{")
        and '"handoff"' in final_answer
        and ROLE_DOCUMENT not in roles
        and not handle.cancelled
    ):
        # Chat-only: research left a handoff JSON blob — turn into prose.
        final_answer = await _run_llm_worker(
            role=ROLE_GENERAL,
            content=content,
            original_request=request,
            prior_notes=final_answer,
            tracker=tracker,
            callback=callback,
            skills=skills,
            handle=handle,
            unit_task_id=f"{child_task_id(task_id)}.synth",
            system_extra=(
                "\nTurn the prior research handoff/notes into a clear final "
                "answer for the user. Do not paste raw JSON."
            ),
        )

    if combined_create_and_open and not handle.cancelled:
        # The file was just created above in THIS job's own workspace --
        # open it now that it actually exists, appended after the normal
        # answer rather than replacing it.
        open_message = _open_local_file_step(
            workspace_root=workspace_root, conversation_id=conversation_id
        )
        final_answer = (
            f"{final_answer}\n\n{open_message}".strip()
            if final_answer
            else open_message
        )

    return execution_result(
        task_id=task_id,
        content=content,
        result=_present_workforce_answer(final_answer or ""),
        state="DONE" if not handle.cancelled else "FAILED",
    )


def run_workforce_in_thread(
    callback: Any,
    tracker: UsageTracker,
    tool_call_recorder: Optional[ToolCallRecorder],
    content: str,
    task_id: str,
    on_workforce_ready: Callable[[Any], None],
    workspace_root: Optional[str] = None,
    fast_path_role: Optional[str] = None,
    skills: Optional[list] = None,
    job_capabilities: Optional[frozenset] = None,
    original_request: Optional[str] = None,
    wants_artifact: bool = False,
    artifact_type: str = "none",
    deep_research: bool = False,
    conversation_id: Optional[str] = None,
    reuses_uploaded_document: Optional[bool] = None,
    opens_on_user_computer: Optional[bool] = None,
    reads_email: Optional[bool] = None,
    reads_social: Optional[bool] = None,
    reads_jira: Optional[bool] = None,
) -> Any:
    """Sync entrypoint matching ``camel_service.run_workforce_in_thread``."""
    handle = CancellableWorkforce()
    on_workforce_ready(handle)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(
            _run_workforce_async(
                callback=callback,
                tracker=tracker,
                tool_call_recorder=tool_call_recorder,
                content=content,
                task_id=task_id,
                handle=handle,
                workspace_root=workspace_root,
                fast_path_role=fast_path_role,
                skills=skills,
                job_capabilities=job_capabilities,
                original_request=original_request,
                wants_artifact=wants_artifact,
                artifact_type=artifact_type or "none",
                deep_research=deep_research,
                conversation_id=conversation_id,
                reuses_uploaded_document=reuses_uploaded_document,
                opens_on_user_computer=opens_on_user_computer,
                reads_email=reads_email,
                reads_social=reads_social,
                reads_jira=reads_jira,
            )
        )
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        finally:
            loop.close()
            asyncio.set_event_loop(None)
