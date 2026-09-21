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
    sends_email: bool = False
    email_to: list[str] = []
    reads_email: bool = False
    reads_social: bool = False


# Addresses are taken from the REQUEST TEXT only, never from anything the run
# reads later (web pages, uploads, an upstream task's report). A classifier
# that could be talked into a recipient by a fetched page would turn one
# poisoned search result into mail sent from the user's own mailbox. See
# addresses_in_text: an address the user did not type is dropped even when
# the model returns it.
_EMAIL_RE = re.compile(r"[^@\s;,<>\"()\[\]]+@[^@\s;,<>\"()\[\]]+\.[A-Za-z]{2,}")
MAX_EMAIL_RECIPIENTS = 10


def addresses_in_text(text: str) -> list[str]:
    """Email addresses literally present in ``text``, in order, deduped."""
    found: list[str] = []
    for match in _EMAIL_RE.findall(text or ""):
        address = match.strip(" .,;:'\"")
        if address and address.casefold() not in {item.casefold() for item in found}:
            found.append(address)
    return found


def _verified_recipients(claimed: list[str], text: str) -> list[str]:
    """Only the claimed addresses the user actually wrote."""
    typed = {address.casefold(): address for address in addresses_in_text(text)}
    kept: list[str] = []
    for address in claimed or []:
        actual = typed.get(str(address).strip().casefold())
        if actual and actual not in kept:
            kept.append(actual)
        elif actual is None:
            stabilization_logger.warning(
                "Artifact intent proposed an email recipient that is not in the "
                "request text; dropped"
            )
    return kept[:MAX_EMAIL_RECIPIENTS]


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
    # "ve ali@sirket.com'a gonder", "sonucu bana mail at" -- the user asking
    # for the result to be emailed, without ticking anything in the UI.
    # Recipients are only ever addresses they typed themselves
    # (_verified_recipients); a scheduled task's setting wins over this.
    sends_email: bool = False
    email_to: tuple[str, ...] = ()
    # "gelen maillerime bak", "X'ten gelen faturayı özetle" -- the user
    # pointing the job at their OWN mailbox as a source. Strictly separate
    # from sends_email: one reads, one writes, and a request can easily be
    # one without the other. Asked of the classifier rather than matched on
    # "mail", because half the requests containing that word ("mail şablonu
    # yaz") are not about the mailbox at all, and a false positive here
    # means reading someone's inbox they never asked about.
    reads_email: bool = False
    # "reddit'te bunun hakkinda ne diyorlar" -- the user pointing the job at
    # public discussion as a SOURCE, as opposed to an ordinary web question
    # that merely happens to mention a platform. Asked of the classifier for
    # the same reason as reads_email: the word is a bad signal on its own.
    reads_social: bool = False

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
            # No verified recipient means nothing can be sent, whatever the
            # classifier said.
            sends_email=bool(self.sends_email) and bool(self.email_to),
            email_to=tuple(self.email_to),
            reads_email=bool(self.reads_email),
            reads_social=bool(self.reads_social),
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
    '"opens_on_user_computer": true | false, '
    '"sends_email": true | false, '
    '"email_to": ["address", ...], '
    '"reads_email": true | false, '
    '"reads_social": true | false}.\n\n'
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
    "sends_email is true when the user asks for the result to be sent by "
    "email -- 'gonder', 'mail at', 'yolla', 'ilet', 'e-postala', 'send it "
    "to', 'email me the report' -- however they phrase it. email_to lists "
    "the recipient addresses EXACTLY as they appear IN THE REQUEST TEXT. "
    "Never invent, correct, or complete an address, and never take one from "
    "anywhere except the request text itself. If they ask for mail but name "
    "no address, sends_email is true and email_to is empty.\n\n"
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
    "\nsends_email / email_to examples (keys named, independent of the values above):\n"
    '- "haftalik raporu hazirla ve ali@sirket.com adresine gonder" -> '
    'sends_email true, email_to ["ali@sirket.com"]\n'
    '- "sonucu bana mail at" -> sends_email true, email_to []\n'
    '- "pdf yap, ekip@firma.com ve mudur@firma.com a yolla" -> '
    'sends_email true, email_to ["ekip@firma.com", "mudur@firma.com"]\n'
    '- "email pazarlama stratejilerini arastir" -> sends_email false, email_to []\n'
    '- "bu raporu ozetle" -> sends_email false, email_to []\n'
    "\nreads_email is true when the user wants you to LOOK AT THEIR OWN "
    "MAILBOX -- read, search, check, summarize or find something in the "
    "mail they received or sent. It is about incoming/existing mail, not "
    "about sending. It is false when the request merely CONTAINS the word "
    "mail/e-posta but asks you to write, send, or research something: "
    "writing an email text, sending a result, or researching email "
    "marketing are all reads_email false. sends_email and reads_email are "
    "independent -- a request can be both ('gelen teklifleri ozetle ve "
    "ali@x.com a gonder'), either, or neither.\n"
    "\nreads_email examples (key named, independent of the values above):\n"
    '- "gelen maillerime bak" -> reads_email true\n'
    '- "bugun gelen okunmamis mailleri ozetle" -> reads_email true\n'
    '- "ahmet ten gelen son maili ozetle" -> reads_email true\n'
    '- "gecen hafta gelen faturalari excel yap" -> reads_email true, '
    "wants_artifact true, xlsx\n"
    '- "mudurume gonderdigim son maili bul" -> reads_email true\n'
    '- "posta kutumdaki teklifleri karsilastir ve pdf ver" -> reads_email '
    "true, wants_artifact true, pdf\n"
    '- "musteriye tesekkur maili yaz" -> reads_email false\n'
    '- "mail sablonu hazirla" -> reads_email false\n'
    '- "raporu hazirla ve bana mail at" -> reads_email false, sends_email true\n'
    '- "email pazarlamasi hakkinda arastirma yap" -> reads_email false\n'
    "\nreads_social is true when the user wants what PEOPLE ARE SAYING on "
    "Reddit -- opinions, experiences, complaints, discussion -- as the "
    "SOURCE for the answer. It is false for an ordinary factual question "
    "that merely mentions the platform, and false when they want a website "
    "read instead (that is needs_web_research).\n"
    "\nreads_social examples (key named, independent of the values above):\n"
    '- "reddit te bu telefon hakkinda ne diyorlar" -> reads_social true\n'
    '- "r/LocalLLaMA da neler konusuluyor" -> reads_social true\n'
    '- "kullanici yorumlarina gore en iyi model hangisi" -> reads_social true\n'
    '- "reddit kullanicilarinin sikayetlerini pdf yap" -> reads_social true, '
    "wants_artifact true, artifact_type pdf\n"
    '- "reddit ne zaman kuruldu" -> reads_social false, needs_web_research '
    "true\n"
    '- "sosyal medya stratejisi yaz" -> reads_social false\n'
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
    recipients = addresses_in_text(topic or "")
    # Only with an address AND a sending verb: "mail adresimi degistir" and
    # "email pazarlamasi arastir" are not requests to send anything.
    sends_email = bool(recipients) and bool(
        re.search(
            r"\b(g[oö]nder|yolla|ilet|at|mail|e-?posta|e-?postala|send|email|forward)\b",
            body,
            re.IGNORECASE,
        )
    )
    # Degraded path: prefer external lookup over skipping Research entirely.
    needs_web = True
    return ArtifactIntent(
        wants_artifact=wants,
        artifact_type=kind,
        source="regex_fallback",
        needs_web_research=needs_web,
        needs_code_execution=needs_code,
        sends_email=sends_email,
        email_to=tuple(recipients),
        # Left false on purpose. Every other field here degrades toward
        # doing MORE work; this one would degrade toward opening the user's
        # mailbox because a classifier call failed, which is not a tradeoff
        # a fallback gets to make.
        reads_email=False,
        # Left false for the same reason as reads_email above: a failed
        # classifier call must not degrade toward spending upstream requests
        # against a rate-limited account.
        reads_social=False,
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
        sends_email=bool(usage.get("sends_email")),
        email_to=tuple(usage.get("email_to") or ()),
        reads_email=bool(usage.get("reads_email")),
        reads_social=bool(usage.get("reads_social")),
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
        sends_email=result.sends_email,
        email_to=tuple(_verified_recipients(result.email_to, topic)),
        reads_email=result.reads_email,
        reads_social=result.reads_social,
    ).normalized()
    return intent
