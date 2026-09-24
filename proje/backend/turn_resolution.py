"""One job-start turn resolver: expand follow-up references and decide work.

Workforce threads only. Single stays a tools-free chatbot and never calls
this. This is not a replacement for UI mode, worker assignment, or tool
selection -- the model returns a self-contained request plus whether this
turn needs new external work (web, files, artifacts).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

# DeepSeek reasoning models spend most of a small budget on thinking tokens
# and then hit LengthFinishReasonError (no JSON) -- observed live: 512
# reserved for this call, 419-512 of it consumed by reasoning_tokens alone,
# zero content tokens left. Same fix and rationale as artifact_intent.py's
# INTENT_MAX_TOKENS: keep headroom for the label after reasoning.
RESOLVE_MAX_TOKENS = 2048
RESOLVE_TIMEOUT_SECONDS = 30
MAX_RECENT_TURNS_CHARS = 6_000
MAX_CATALOG_ITEMS = 10
MAX_PRIOR_MESSAGES = 4  # last ~2 turns, newest-first from the DB


class TurnResolutionResult(BaseModel):
    resolved_request: str = ""
    needs_external_work: bool = True
    bind_file_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class TurnResolution:
    resolved_request: str
    needs_external_work: bool
    bind_file_ids: tuple[str, ...]
    source: str

    def normalized(self, topic: str, catalog_ids: tuple[str, ...]) -> "TurnResolution":
        resolved = (self.resolved_request or "").strip() or (topic or "").strip()
        allowed = set(catalog_ids)
        bind = tuple(fid for fid in self.bind_file_ids if fid in allowed)
        return TurnResolution(
            resolved_request=resolved,
            needs_external_work=bool(self.needs_external_work),
            bind_file_ids=bind,
            source=self.source,
        )


RESOLVE_SYSTEM_PROMPT = (
    "You prepare ONE follow-up chat turn. Respond with JSON only: "
    '{"resolved_request": "...", "needs_external_work": true | false, '
    '"bind_file_ids": ["..."]}.\n\n'
    "resolved_request is a self-contained restatement of what the user wants "
    "NOW. Replace pronouns and references (it, this, that, the same, bunu, "
    "aynısını, önceki, ikinci kaynak, az önce yüklediğim) with the actual "
    "subject, place, names, and constraints from PRIOR TURNS. Keep the "
    "user's language. Do not answer the user.\n\n"
    "needs_external_work is true ONLY when this turn needs new web research, "
    "opening pages, reading, searching or comparing uploaded files (the FILE "
    "CATALOG or the WORKSPACE DOCUMENTS), or producing a new "
    "downloadable file. It is false when the latest assistant answer and "
    "prior turns already contain everything needed (shorten, translate, "
    "who were the top three, what was the result, restate).\n\n"
    "IMPORTANT: changing delivery format of prior work still needs workers "
    "(needs_external_work true) even when no new web search is required: "
    "'bunu pdf ver', 'bir de powerpoint olarak', 'chatte versene xlsx "
    "istemedim', 'excel yerine word'. The executor will reuse prior notes "
    "and skip fresh research; Direct/chat-only mode cannot write files or "
    "reliably recover stripped tables.\n\n"
    "bind_file_ids lists FILE CATALOG ids this turn must attach, copied "
    "exactly. Empty if no file is needed. Never invent ids.\n\n"
    "Examples:\n"
    '- After standings research, "ilk üçü kim?" -> false, resolved names '
    "the league/standings, bind [].\n"
    '- "aynısını MotoGP için araştır" -> true, resolved asks to research '
    "MotoGP the same way.\n"
    '- "bunu Excel olarak ver" after a table in chat -> true, resolved asks '
    "to create an Excel of that table.\n"
    '- "chatte versene ben xlsx istemedim" after an Excel answer -> true, '
    "resolved asks to show the same rates/table in chat text.\n"
    '- "bunu pdf ver" / "bir de powerpoint olarak ver" after research -> '
    "true, resolved asks for that format from prior findings.\n"
    '- "az önce yüklediğim dosyayı özetle" with a catalog id -> true, bind '
    "that id.\n"
    '- "aynı formatta X yaz" / "bu şablonu kullanarak Y hazırla" referring '
    "to a previously uploaded .docx -> true, resolved restates the new "
    "content to write, bind that file's catalog id.\n"
    "Output ONLY the JSON object."
)


def _library_line(names: Optional[list[str]], limit: int = 20) -> str:
    cleaned = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not cleaned:
        return "(none)"
    more = f" ... (+{len(cleaned) - limit})" if len(cleaned) > limit else ""
    return ", ".join(cleaned[:limit]) + more


def first_turn(topic: str) -> TurnResolution:
    return TurnResolution(
        resolved_request=(topic or "").strip(),
        needs_external_work=True,
        bind_file_ids=(),
        source="first_turn",
    ).normalized(topic, ())


def single_mode(topic: str) -> TurnResolution:
    return TurnResolution(
        resolved_request=(topic or "").strip(),
        needs_external_work=False,
        bind_file_ids=(),
        source="single_mode",
    ).normalized(topic, ())


def fallback(topic: str, catalog_ids: tuple[str, ...] = ()) -> TurnResolution:
    return TurnResolution(
        resolved_request=(topic or "").strip(),
        needs_external_work=True,
        bind_file_ids=(),
        source="fallback",
    ).normalized(topic, catalog_ids)


def _recover(response_text: Any) -> Optional[TurnResolutionResult]:
    raw = (response_text or "").strip() if isinstance(response_text, str) else ""
    if not raw:
        return None
    fenced = raw
    if fenced.startswith("```"):
        fenced = fenced.split("\n", 1)[-1]
        if fenced.endswith("```"):
            fenced = fenced[: -3]
        fenced = fenced.strip()
    start = fenced.find("{")
    if start < 0:
        return None
    try:
        decoded, _ = json.JSONDecoder().raw_decode(fenced[start:])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    try:
        return TurnResolutionResult.model_validate(decoded)
    except ValidationError:
        return None


def _match_bind_ids(
    raw_ids: list[str], catalog: list[dict[str, str]]
) -> tuple[str, ...]:
    by_id = {str(item.get("file_id") or ""): item for item in catalog}
    by_name = {
        str(item.get("filename") or "").casefold(): str(item.get("file_id") or "")
        for item in catalog
        if item.get("filename")
    }
    bound: list[str] = []
    for raw in raw_ids:
        token = str(raw or "").strip()
        if not token:
            continue
        if token in by_id and token not in bound:
            bound.append(token)
            continue
        mapped = by_name.get(token.casefold())
        if mapped and mapped not in bound:
            bound.append(mapped)
    return tuple(bound)


def format_recent_turns(messages_newest_first: list[dict[str, Any]]) -> str:
    """Newest-kept, then chronological, bounded transcript for the resolver."""
    lines: list[str] = []
    remaining = MAX_RECENT_TURNS_CHARS
    for message in messages_newest_first:
        role = "USER" if message.get("role") == "user" else "ASSISTANT"
        body = str(message.get("content") or "")
        entry = f"--- {role} ---\n{body}\n"
        if len(entry) > remaining:
            break
        lines.append(entry)
        remaining -= len(entry)
    lines.reverse()
    return "".join(lines).strip()


async def resolve_turn(
    topic: str,
    *,
    tracker: Any,
    prior_messages: list[dict[str, Any]],
    catalog: list[dict[str, str]],
    library_names: Optional[list[str]] = None,
) -> TurnResolution:
    """Expand follow-up references. Fail closed to Workforce, not Direct.

    ``library_names`` are the workspace's uploaded documents: a follow-up
    that looks something up in them ("sözleşmede depozito ne kadar") is new
    work, not a restatement of the last answer.
    """
    topic = topic or ""
    catalog = catalog[:MAX_CATALOG_ITEMS]
    catalog_ids = tuple(
        str(item.get("file_id") or "") for item in catalog if item.get("file_id")
    )
    if not prior_messages:
        return first_turn(topic)

    prior_messages = prior_messages[:MAX_PRIOR_MESSAGES]

    from execution.langgraph_runtime import openai_chat

    recent = format_recent_turns(prior_messages)
    catalog_json = json.dumps(catalog, ensure_ascii=False)
    try:
        reply = await asyncio.wait_for(
            openai_chat.complete_chat(
                [
                    {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"CURRENT MESSAGE:\n{topic}\n\n"
                            f"PRIOR TURNS (oldest first):\n{recent or '(none)'}\n\n"
                            f"FILE CATALOG:\n{catalog_json}\n\n"
                            f"WORKSPACE DOCUMENTS:\n{_library_line(library_names)}\n"
                        ),
                    },
                ],
                tracker=tracker,
                stage="turn_resolution",
                max_tokens=RESOLVE_MAX_TOKENS,
                temperature=0,
                response_format=TurnResolutionResult,
            ),
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - fallback must never fail the job
        stabilization_logger.warning(
            "Turn resolution failed; using fallback (external work)",
            exc_info=True,
        )
        return fallback(topic, catalog_ids)

    if isinstance(reply, TurnResolutionResult):
        result = reply
    else:
        result = _recover(reply)
    if result is None:
        stabilization_logger.warning(
            "Turn resolution returned no valid structured content; fallback"
        )
        return fallback(topic, catalog_ids)

    bind = _match_bind_ids(result.bind_file_ids, catalog)
    return TurnResolution(
        result.resolved_request,
        result.needs_external_work,
        bind,
        "llm",
    ).normalized(topic, catalog_ids)
