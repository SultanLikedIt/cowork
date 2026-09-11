"""One job-start classifier: did the user ask for a downloadable file?

Workforce only. Single is a tools-free chatbot and never calls this.
This module is not a replacement for strategy selection.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, ValidationError

import capabilities

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

# DeepSeek reasoning models spend most of a small budget on thinking tokens
# and then hit LengthFinishReasonError (no JSON). Keep headroom for the label.
INTENT_MAX_TOKENS = 2048
INTENT_TIMEOUT_SECONDS = 30

ArtifactType = Literal[
    "xlsx", "csv", "json", "docx", "pptx", "html", "pdf", "code", "none"
]
_ARTIFACT_TYPES = frozenset(
    {"xlsx", "csv", "json", "docx", "pptx", "html", "pdf", "code", "none"}
)


class ArtifactIntentResult(BaseModel):
    wants_artifact: bool = False
    artifact_type: ArtifactType = "none"
    needs_web_research: bool = False
    needs_code_execution: bool = False
    reuses_uploaded_document: bool = False
    opens_on_user_computer: bool = False


@dataclass(frozen=True)
class ArtifactIntent:
    wants_artifact: bool
    artifact_type: str
    source: str
    needs_web_research: bool = False
    needs_code_execution: bool = False
    # "Use the document I attached as the document" -- rewrite it, fill it
    # in, restyle it -- as opposed to writing something new that merely
    # cites it. Asked here rather than pattern-matched downstream because
    # the phrasings are unbounded: "bunu doldur", "bu sartnameyi sirketim
    # icin uyarla", "bana gore yapar misin bunu", "lutfen docx olarak ver"
    # are all the same intent and share no keyword. Every regex written for
    # it caught the example that prompted it and missed the next one.
    reuses_uploaded_document: bool = False
    # "aç", "açar mısın", "bilgisayarımda aç" -- the same request in a
    # dozen shapes. Pattern-matched before, which meant it only fired on
    # the shapes someone had already thought of: a user who asked for a
    # deck "verip açar mısın" got the file and no open, because the
    # pattern wanted the word "bilgisayarımda".
    opens_on_user_computer: bool = False

    def normalized(self) -> "ArtifactIntent":
        kind = self.artifact_type if self.artifact_type in _ARTIFACT_TYPES else "none"
        wants = bool(self.wants_artifact) and kind != "none"
        return ArtifactIntent(
            wants_artifact=wants,
            artifact_type=kind,
            source=self.source,
            needs_web_research=bool(self.needs_web_research),
            needs_code_execution=bool(self.needs_code_execution),
            reuses_uploaded_document=bool(self.reuses_uploaded_document),
            opens_on_user_computer=bool(self.opens_on_user_computer),
        )


INTENT_SYSTEM_PROMPT = (
    "You classify ONE user request (any language, including typos) for "
    "workforce routing. Respond with JSON only: "
    '{"wants_artifact": true | false, '
    '"artifact_type": "xlsx" | "csv" | "json" | "docx" | "pptx" | "html" | '
    '"pdf" | "code" | "none", '
    '"needs_web_research": true | false, '
    '"needs_code_execution": true | false, '
    '"reuses_uploaded_document": true | false, '
    '"opens_on_user_computer": true | false}.\n\n'
    "wants_artifact is true ONLY when the user wants a new file created and "
    "given to them (Excel/xlsx, CSV, JSON, Word/docx, PowerPoint/pptx, PDF, HTML "
    "page, or a runnable code/project bundle). "
    "Generic 'dosya/file' without a specific writable format still counts "
    "when they ask you to deliver output as a file.\n"
    "wants_artifact is false when they only want chat text, analyze an "
    "existing/attached file, or summarize without asking for a new download.\n\n"
    "needs_web_research is true when answering requires looking up facts "
    "outside the chat or uploaded files (web search): research, history, "
    "current data, comparisons of real-world entities, named organizations, "
    "people, cities, products, prices, news, or 'find/araştır/bul' tasks. "
    "It is true even when they also want a file at the end. "
    "needs_web_research is false only for pure chat, attached-file-only "
    "analysis, or code/math on given data with no external lookup.\n\n"
    "needs_code_execution is true when the task requires writing, running, "
    "debugging, or modifying code -- including writing functions/scripts, "
    "implementing algorithms, building software, fixing bugs, configuring "
    "Docker/CI/CD, data transformation via code, or any programming task. "
    "It is true even when the code is presented as chat text rather than a "
    "downloadable file. "
    "needs_code_execution is false for pure prose, research, document "
    "creation, or questions that need no programming. Embedding a "
    "royalty-free photo in a PDF/Word/PPT is NOT code execution -- that is "
    "document creation (needs_code_execution false).\n\n"
    "reuses_uploaded_document is true when an ATTACHED FILE is listed "
    "below and the user wants THAT document itself produced again -- "
    "rewritten, filled in, adapted, restyled, renamed to their company, "
    "or simply handed back in a downloadable format. The giveaway is that "
    "the content they want already exists in the attachment; you are not "
    "researching or composing it. This stays true across follow-up turns "
    "in the same conversation, where the request may be as short as "
    "'lutfen docx olarak ver' or 'bana gore yap' and never name the file. "
    "It is FALSE when the attachment is only material to read from -- "
    "summarize it, answer questions about it, extract a table, compare it "
    "with something else -- and false when no file is attached at all.\n"
    "When reuses_uploaded_document is true, needs_web_research is almost "
    "always false: the source is the attachment, not the internet.\n\n"
    "opens_on_user_computer is true when the user asks for the file to "
    "be OPENED on their own machine once it exists -- 'aç', 'açar mısın', "
    "'bilgisayarımda aç', 'open it', 'sonra aç' -- however they phrase "
    "it, and whether or not they name the computer. Asking only for the "
    "file to be created or downloaded is NOT this. It is false when no "
    "file is produced at all.\n\n"
    "Examples (values in the order listed above):\n"
    '- "Cree-moi un fichier Excel des classements" -> true, xlsx, true, false, false\n'
    '- "Analyse ce fichier Excel" -> false, none, false, false, false\n'
    '- "bunu exsel olarak ver" -> true, xlsx, true, false, false\n'
    '- "Excel dosyasini analiz et" -> false, none, false, false, false\n'
    '- "5 makale ozetle" -> false, none, true, false, false\n'
    '- "TUSAS tarihini arastirip dosya olarak ver" -> true, docx, true, false, false\n'
    '- "PDF rapor olustur" -> true, pdf, true, false, false\n'
    '- "Zeytin ozeti yaz, gorsel ekle, PDF ver" -> true, pdf, true, false, false\n'
    '- "What is 2+2?" -> false, none, false, false, false\n'
    '- "Write a fibonacci function in Python" -> false, none, false, true, false\n'
    '- "Debug this code and fix the error" -> false, none, false, true, false\n'
    '- "Build a REST API with Flask" -> false, none, false, true, false\n'
    '- "Dockerfile olustur" -> false, none, false, true, false\n'
    '- "Bu CSV dosyasini JSON a donustur" -> false, none, false, true, false\n'
    "\nWith an attached document -- none of these share a keyword:\n"
    '- [sartname.docx] "Yukledigim vekaletnameyle ayni formatta kira sozlesmesi yaz"'
    " -> true, docx, false, false, true\n"
    '- [sartname.docx] "bunu omer insaat as olarak yazsana, her sey ayni olsun"'
    " -> true, docx, false, false, true\n"
    '- [sartname.docx] "bu dokumani benim icin doldur"'
    " -> true, docx, false, false, true\n"
    '- [sartname.docx] "bana gore yapar misin bunu"'
    " -> true, docx, false, false, true\n"
    '- [sartname.docx] "lutfen docx olarak ver"'
    " -> true, docx, false, false, true\n"
    '- [sartname.docx] "bu sartnameyi sirketim icin uyarla"'
    " -> true, docx, false, false, true\n"
    '- [rapor.pdf] "bu belgeyi ozetle"'
    " -> false, none, false, false, false\n"
    '- [veriler.xlsx] "bu dosyadaki en yuksek 5 degeri soyle"'
    " -> false, none, false, false, false\n"
    '- [rapor.pdf] "bu raporun konusuyla ilgili guncel haberleri arastir"'
    " -> false, none, true, false, false\n"
    "\nopens_on_user_computer examples (key named, since it is independent of the values above):\n"
    '- "sunum hazirla ve bilgisayarimda ac" -> opens_on_user_computer true\n'
    '- "html ve pptx sunum verip acar misin lutfen" -> opens_on_user_computer true\n'
    '- "raporu pdf ver sonra ac" -> opens_on_user_computer true\n'
    '- "bana bir docx hazirla" -> opens_on_user_computer false\n'
    '- "bu belgeyi ozetle" -> opens_on_user_computer false\n'
    "Output ONLY the JSON object."
)


def _recover_intent(response_text: Any) -> Optional[ArtifactIntentResult]:
    raw = (response_text or "").strip() if isinstance(response_text, str) else ""
    if not raw:
        return None
    fenced = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced).strip()
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
        return ArtifactIntentResult.model_validate(decoded)
    except ValidationError:
        return None


def fallback_from_regex(topic: str) -> ArtifactIntent:
    """Minimal fallback when the classifier call fails entirely."""
    wants = capabilities.ARTIFACTS in capabilities.detect_artifact_request(topic)
    body = (topic or "").lower()
    mapping = (
        ("pptx", "pptx"),
        ("powerpoint", "pptx"),
        ("power point", "pptx"),
        ("power-point", "pptx"),
        ("docx", "docx"),
        ("word", "docx"),
        ("xlsx", "xlsx"),
        ("excel", "xlsx"),
        ("spreadsheet", "xlsx"),
        ("csv", "csv"),
        ("geojson", "json"),
        ("json", "json"),
        ("html", "html"),
        ("web app", "html"),
        ("webapp", "html"),
        ("code", "code"),
        ("api", "code"),
        ("script", "code"),
    )
    kind = "xlsx" if wants else "none"
    for token, name in mapping:
        if token in body:
            kind = name
            break
    _CODE_RE = re.compile(
        r"\b(write code|implement|debug|fix (?:the |this |a )?bug|refactor|"
        r"unit test|dockerfile|ci.?cd|docker|deploy|build|compile|"
        r"kod yaz|hata düzelt|fonksiyon|function|script|algorithm)\b",
        re.IGNORECASE,
    )
    needs_code = bool(_CODE_RE.search(body))
    # Degraded path: prefer external lookup over skipping Research entirely.
    needs_web = True
    return ArtifactIntent(
        wants_artifact=wants,
        artifact_type=kind,
        source="regex_fallback",
        needs_web_research=needs_web,
        needs_code_execution=needs_code,
    ).normalized()


def from_persisted(usage: Optional[dict]) -> Optional[ArtifactIntent]:
    if not isinstance(usage, dict):
        return None
    # Job usage is seeded with wants_artifact=None before classification.
    # That must not look like a replay -- only a prior classify() writes
    # artifact_intent_source.
    source = str(usage.get("artifact_intent_source") or "").strip()
    if source not in {"llm", "regex_fallback", "regex_override"}:
        return None
    if "wants_artifact" not in usage:
        return None
    return ArtifactIntent(
        wants_artifact=bool(usage.get("wants_artifact")),
        artifact_type=str(usage.get("artifact_type") or "none"),
        source=source,
        needs_web_research=bool(usage.get("needs_web_research")),
        needs_code_execution=bool(usage.get("needs_code_execution")),
        reuses_uploaded_document=bool(usage.get("reuses_uploaded_document")),
        opens_on_user_computer=bool(usage.get("opens_on_user_computer")),
    ).normalized()


async def classify_artifact_intent(
    topic: str,
    tracker: Any,
    *,
    persisted: Optional[dict] = None,
    attachment_names: Optional[list[str]] = None,
) -> ArtifactIntent:
    """``attachment_names`` are the files in play for this turn.

    Without them reuses_uploaded_document cannot be decided at all: "lutfen
    docx olarak ver" is a request to reproduce the attachment when one is
    attached, and a request to write something new when nothing is. The
    same sentence, opposite answers.
    """
    existing = from_persisted(persisted)
    if existing is not None:
        return existing

    from camel.agents import ChatAgent
    from camel.messages import BaseMessage
    from model_config import get_model

    agent = ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Artifact intent",
            content=INTENT_SYSTEM_PROMPT,
        ),
        model=get_model(
            tracker,
            stage="artifact_intent",
            temperature=0,
            max_tokens=INTENT_MAX_TOKENS,
        ),
        max_iteration=1,
        retry_attempts=1,
        step_timeout=INTENT_TIMEOUT_SECONDS,
    )
    try:
        names = [str(n).strip() for n in (attachment_names or []) if str(n).strip()]
        attached_line = (
            "ATTACHED FILES: " + ", ".join(names[:10])
            if names
            else "ATTACHED FILES: (none)"
        )
        response = await agent.astep(
            f"{attached_line}\nREQUEST: {topic}",
            response_format=ArtifactIntentResult,
        )
    except Exception:  # noqa: BLE001 - fallback must never fail the job
        stabilization_logger.warning(
            "Artifact intent failed; using regex fallback",
            exc_info=True,
        )
        return fallback_from_regex(topic)

    parsed = response.msg.parsed if response.msg else None
    if isinstance(parsed, ArtifactIntentResult):
        result = parsed
    elif isinstance(parsed, dict):
        try:
            result = ArtifactIntentResult.model_validate(parsed)
        except ValidationError:
            result = None
    else:
        result = _recover_intent(response.msg.content if response.msg else None)
    if result is None:
        stabilization_logger.warning(
            "Artifact intent returned no valid structured content; regex fallback"
        )
        return fallback_from_regex(topic)
    intent = ArtifactIntent(
        result.wants_artifact,
        result.artifact_type,
        "llm",
        needs_web_research=result.needs_web_research,
        needs_code_execution=result.needs_code_execution,
        reuses_uploaded_document=result.reuses_uploaded_document,
        opens_on_user_computer=result.opens_on_user_computer,
    ).normalized()
    return intent
