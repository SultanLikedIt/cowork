"""Tests for capability resolution and merge_decision.

Covers capabilities.py structural resolution, apply_artifact_intent (canonical
ARTIFACTS union from intent), and capability_routing.merge_decision.

The retired LLM/text execution router is gone: Single vs Workforce is UI-selected.
Artifact intent LLM is the canonical source for ARTIFACTS at submission.
"""

import asyncio
import sys
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch

import artifact_intent

# This file's multilingual test corpus (Chinese, Turkish, German) can
# contain characters the console's active codepage cannot represent --
# e.g. Windows defaults to a locale-specific codepage (cp1254 for Turkish
# Windows) that has no Chinese characters at all, so printing a test name
# containing them raises UnicodeEncodeError and aborts the run before the
# assertions themselves are even the problem. Reconfiguring stdout to
# UTF-8 with a lossy fallback only changes how results are displayed, not
# any test data or assertion.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, ValueError):
    pass

import camel_service
import capabilities
import capability_routing
from capabilities import (
    ARTIFACTS,
    CODE_EXEC,
    FILES,
    WEB_FETCH,
    WEB_SEARCH,
)
from model_config import UsageTracker

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if not condition else ""))
    if not condition:
        failures.append(name)


@dataclass
class _Declared:
    execution: str = "direct"
    capabilities: list = field(default_factory=list)
    profile: str = "general"


def _result(execution="direct", caps=(), profile="general"):
    return _Declared(execution=execution, capabilities=list(caps), profile=profile)


def _intent_wants_artifact(topic: str) -> bool:
    """Test stand-in for artifact intent LLM (regex fallback equivalence)."""
    return capabilities.ARTIFACTS in capabilities.detect_artifact_request(topic)


def _effective_requirements(
    topic: str,
    has_workspace_files: bool,
    file_kinds=None,
    *,
    wants_artifact: bool | None = None,
) -> frozenset[str]:
    structural = capabilities.structural_requirements(
        topic, has_workspace_files, file_kinds
    )
    if wants_artifact is None:
        wants_artifact = _intent_wants_artifact(topic)
    return capabilities.apply_artifact_intent(
        structural,
        wants_artifact=wants_artifact,
        text=topic,
        has_workspace_files=has_workspace_files,
        workspace_file_kinds=file_kinds,
    )


def _route(
    topic,
    has_workspace_files,
    parsed_or_exception,
    file_kinds=None,
    *,
    wants_artifact: bool | None = None,
):
    """Merge declared capabilities with the structural + intent floor."""
    declared = []
    strategy = "direct"
    profile = "general"
    if parsed_or_exception is not None and not isinstance(
        parsed_or_exception, BaseException
    ):
        declared = list(getattr(parsed_or_exception, "capabilities", []) or [])
        strategy = getattr(parsed_or_exception, "execution", "direct") or "direct"
        profile = getattr(parsed_or_exception, "profile", "general") or "general"
    structural = _effective_requirements(
        topic,
        has_workspace_files,
        file_kinds,
        wants_artifact=wants_artifact,
    )
    decision = capability_routing.merge_decision(
        strategy,
        declared,
        structural,
        profile=profile,
        reason="test merge",
        outcome=capability_routing.ROUTING_SKIPPED,
        raw_declared=declared,
    )
    return decision, None, UsageTracker()


# ---------------------------------------------------------------------------
# 0. UI mode is the execution strategy; no model call
# ---------------------------------------------------------------------------


def test_production_routing_makes_no_model_call() -> None:
    """UI-selected strategy never constructs a routing agent."""
    import main

    class _Exploding:
        def __init__(self, *args, **kwargs):
            raise AssertionError("routing must not construct an agent")

    with patch.object(camel_service, "ChatAgent", _Exploding), patch.object(
        camel_service, "get_model", lambda *a, **k: None
    ):
        topic = "Go to https://example.com/t and give me an Excel file"
        single = main._routing_for_ui_mode(
            "single", topic, has_workspace_files=False, workspace_file_kinds=[]
        )
        check(
            "Single UI routing makes no model call",
            single.outcome == capability_routing.ROUTING_SKIPPED
            and single.strategy == "single",
            f"{single.outcome} {single.strategy}",
        )

    async def _workforce_with_intent():
        async def fake_classify(topic, tracker, persisted=None, **_kwargs):
            return artifact_intent.ArtifactIntent(True, "xlsx", "llm")

        with patch.object(
            artifact_intent,
            "classify_artifact_intent",
            fake_classify,
        ):
            return await main._routing_for_workforce(
                topic,
                has_workspace_files=False,
                workspace_file_kinds=[],
                tracker=UsageTracker(),
            )

    decision, intent = asyncio.run(_workforce_with_intent())
    check(
        "Workforce effective caps include fetch and artifacts from intent",
        decision.can_satisfy({WEB_FETCH, ARTIFACTS}),
        sorted(decision.capabilities),
    )
    check(
        "artifact intent is persisted at submission",
        intent.wants_artifact and intent.artifact_type == "xlsx",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )


def test_ui_mode_is_authoritative() -> None:
    import main

    topics = [
        "Go to https://x.example/p, pull the table, and give me an xlsx file",
        "Independently assess architecture, security, and operations, then "
        "synthesize the findings.",
        "merhaba, nasılsın?",
        "Explain how photosynthesis works",
    ]
    for topic in topics:
        single = main._routing_for_ui_mode(
            "single", topic, has_workspace_files=False, workspace_file_kinds=[]
        )
        check(
            f"Single stays chatbot: {topic[:44]!r}",
            single.strategy == "single" and not single.capabilities,
            f"{single.strategy} {sorted(single.capabilities)}",
        )

    async def _multi_strategy(topic: str):
        async def fake_classify(t, tracker, persisted=None, **_kwargs):
            wants = _intent_wants_artifact(t)
            return artifact_intent.ArtifactIntent(
                wants, "xlsx" if wants else "none", "llm"
            ).normalized()

        with patch.object(
            artifact_intent,
            "classify_artifact_intent",
            fake_classify,
        ):
            decision, _ = await main._routing_for_workforce(
                topic,
                has_workspace_files=False,
                workspace_file_kinds=[],
                tracker=UsageTracker(),
            )
            return decision

    for topic in topics:
        multi = asyncio.run(_multi_strategy(topic))
        check(
            f"Workforce stays workforce: {topic[:44]!r}",
            multi.strategy == "workforce",
            multi.strategy,
        )


def test_profiles_are_persona_only() -> None:
    """A profile must never change what a run can do."""
    cases = [
        ("Summarize the attached report", True, "document"),
        ("What is the current Bitcoin price?", False, "research"),
        ("Write code to parse JSON data", False, "coding"),
        ("Debug this function", False, "coding"),
        ("Explain how TCP works", False, "general"),
        ("Give me a literature review of RAG papers", False, "research"),
    ]
    for topic, has_files, expected in cases:
        resolved = capabilities.expand(
            capabilities.advisory_requirements(topic, has_files)
            | capabilities.structural_requirements(topic, has_files)
        )
        profile = capability_routing.infer_profile(topic, has_files, resolved)
        check(
            f"profile {expected!r} for {topic[:40]!r}",
            profile == expected,
            profile,
        )

    topic = "Review this draft, then give me the result as a CSV file"
    resolved = _effective_requirements(topic, False, wants_artifact=True)
    profile = capability_routing.infer_profile(topic, False, resolved)
    check(
        "a review profile still carries the artifact capability",
        profile == "review" and ARTIFACTS in resolved,
        f"{profile} {sorted(resolved)}",
    )


def test_shadow_mode_records_but_never_obeys() -> None:
    """The LLM router is gone; merge still honours declared capabilities."""
    decision, _agent, _ = _route(
        "Summarize the attached PDF",
        True,
        _result("workforce", [WEB_SEARCH]),
    )
    check(
        "merge keeps the declared strategy string",
        decision.strategy == "workforce",
        decision.strategy,
    )
    check(
        "FILES still arrives from the structural floor",
        FILES in decision.capabilities,
        sorted(decision.capabilities),
    )



def test_mentor_case_url_to_excel() -> None:
    """CASE 1 -- 'go to this URL, extract the table, give me the Excel file'.

    The original failure: routed to a Research Agent whose only tool was web
    SEARCH. It could neither fetch the named page nor write a file, so the
    job failed after doing the one part its tools allowed.

    What must now hold, regardless of what the router itself says:
      * web_fetch, because the request names a URL;
      * artifacts, because the deliverable is a file;
      * and the decision must not be a research-only context.
    """
    topic = (
        "https://namazvakitleri.diyanet.gov.tr/tr-TR/9206/ankara-icin-namaz-"
        "vakti bu sayfaya git, aylık Ankara namaz vakitlerini çek, excel'e "
        "aktar ve bana excel dosyasını ver"
    )

    # The worst realistic router answer: it sees "research" and nothing else.
    decision, _agent, _ = _route(
        topic, False, _result("direct", [WEB_SEARCH], "research")
    )

    check(
        "url-to-excel: web_fetch is required even when the router omits it",
        WEB_FETCH in decision.capabilities,
        sorted(decision.capabilities),
    )
    check(
        "url-to-excel: artifacts is required even when the router omits it",
        ARTIFACTS in decision.capabilities,
        sorted(decision.capabilities),
    )
    check(
        "url-to-excel: the decision satisfies the full requirement set",
        decision.can_satisfy({WEB_FETCH, ARTIFACTS}),
        sorted(decision.capabilities),
    )
    check(
        "url-to-excel: NOT a search-only context",
        decision.capabilities != {WEB_SEARCH},
        sorted(decision.capabilities),
    )
    # The profile may still be "research" -- that is fine and is the point of
    # separating persona from ability.
    check(
        "url-to-excel: a research PROFILE cannot remove the artifact ability",
        decision.profile == "research" and ARTIFACTS in decision.capabilities,
        f"profile={decision.profile} caps={sorted(decision.capabilities)}",
    )


def test_mentor_case_xlsx_to_searchable_web() -> None:
    """CASE 2 -- 'convert this XLSX to JSON and build an HTML/CSS/JS search page'.

    The original failure: routed to a Document Agent whose toolset is
    read-only. Reading rows was the only action available, and the
    deliverable was permanently out of reach, so it read rows for ~430K
    tokens.

    What must now hold: files (the upload), plus artifacts AND code_exec (the
    deliverable), so the run is not a read-only context.
    """
    topic = (
        "Bu excel dosyasındaki veriyi JSON'a çevir ve ekranda arama "
        "yapabileceğim bir HTML/CSS/JS sayfası oluştur"
    )

    # Worst realistic router answer: it sees an uploaded file and stops.
    decision, _agent, _ = _route(
        topic, True, _result("direct", [FILES], "document"), file_kinds=["xlsx"]
    )

    check(
        "xlsx-to-web: files is present",
        FILES in decision.capabilities,
        sorted(decision.capabilities),
    )
    check(
        "xlsx-to-web: artifacts is required even when the router omits it",
        ARTIFACTS in decision.capabilities,
        sorted(decision.capabilities),
    )
    check(
        "xlsx-to-web: code_exec is required for the transform",
        CODE_EXEC in decision.capabilities,
        sorted(decision.capabilities),
    )
    check(
        "xlsx-to-web: NOT a read-only document context",
        decision.capabilities != {FILES},
        sorted(decision.capabilities),
    )
    check(
        "xlsx-to-web: the decision satisfies the full requirement set",
        decision.can_satisfy({FILES, ARTIFACTS, CODE_EXEC}),
        sorted(decision.capabilities),
    )


def test_structural_floor_cannot_be_lowered() -> None:
    """The router may ADD capabilities; it may never remove a structural one.

    This asymmetry is the entire safety argument: an imperfect routing prompt
    degrades into "slightly over-equipped", never into "provably unable to
    finish".
    """
    topic = "Fetch https://example.com/data and save the table as a CSV file"

    for declared in ([], [WEB_SEARCH], [FILES], [CODE_EXEC]):
        decision, _agent, _ = _route(topic, False, _result("direct", declared))
        structural = capabilities.structural_requirements(topic, False)
        effective = _effective_requirements(topic, False)
        check(
            f"structural floor survives declared={declared}",
            decision.can_satisfy(effective),
            f"effective={sorted(effective)} got={sorted(decision.capabilities)}",
        )


def test_router_may_add_capabilities() -> None:
    topic = "Explain the difference between TCP and UDP"
    decision, _agent, _ = _route(topic, False, _result("direct", [WEB_SEARCH]))
    check(
        "a router-declared capability with no structural signal is honoured",
        WEB_SEARCH in decision.capabilities,
        sorted(decision.capabilities),
    )


# ---------------------------------------------------------------------------
# 2. Cheap things stay cheap
# ---------------------------------------------------------------------------


def test_simple_requests_stay_cheap() -> None:
    """Simple research and simple document summary must not gain capabilities.

    The counterweight to everything above: a resolver that over-equips every
    job would trade one failure mode for a more expensive one. These assert
    the floor detects NOTHING for ordinary requests.
    """
    cases = [
        ("What is the latest stable version of Python?", False, {WEB_SEARCH}),
        ("Bugün dolar kuru nedir?", False, {WEB_SEARCH}),
        ("Summarize the attached report", True, {FILES}),
        ("Yüklediğim belgeyi özetler misin?", True, {FILES}),
        ("Explain how photosynthesis works.", False, set()),
        ("merhaba, nasılsın?", False, set()),
        ("Rewrite this paragraph in a more formal tone.", False, set()),
    ]
    for topic, has_files, expected in cases:
        declared = list(expected)
        decision, _agent, _ = _route(topic, has_files, _result("direct", declared))
        check(
            f"stays cheap: {topic[:46]!r}",
            decision.capabilities == capabilities.expand(expected)
            and decision.strategy == "direct",
            f"got {sorted(decision.capabilities)} / {decision.strategy}",
        )
        check(
            f"no artifact capability leaked into: {topic[:46]!r}",
            ARTIFACTS not in decision.capabilities,
            sorted(decision.capabilities),
        )
        check(
            f"no code execution leaked into: {topic[:46]!r}",
            CODE_EXEC not in decision.capabilities,
            sorted(decision.capabilities),
        )


def test_format_mention_alone_is_not_a_deliverable() -> None:
    """Naming a format is not the same as asking for a file.

    Without this the detector would attach artifact tools to every request
    that says the word "JSON", which is how a resolver becomes noise.
    """
    not_deliverables = [
        "Explain how JSON differs from YAML",
        "What does the CSV format specify about quoting?",
        "Why is HTML not a programming language?",
        "Excel formüllerini açıklar mısın?",
    ]
    for topic in not_deliverables:
        found = capabilities.structural_requirements(topic, False)
        check(
            f"format mention alone adds no artifact capability: {topic[:44]!r}",
            ARTIFACTS not in found,
            sorted(found),
        )

def test_apply_artifact_intent_unions_artifacts() -> None:
    """Canonical intent union -- structural alone never adds ARTIFACTS."""
    deliverables = [
        "Give me the results as an Excel file",
        "Create a CSV of the top 20 rows and let me download it",
        "Export this data to JSON",
        "Bana excel dosyasını ver",
        "Sonuçları CSV olarak kaydet",
        "Summarize 5 AI papers as a Word doc",
    ]
    for topic in deliverables:
        structural = capabilities.structural_requirements(topic, False)
        check(
            f"structural alone has no artifact: {topic[:44]!r}",
            ARTIFACTS not in structural,
            sorted(structural),
        )
        effective = _effective_requirements(
            topic, False, wants_artifact=_intent_wants_artifact(topic)
        )
        if _intent_wants_artifact(topic):
            check(
                f"intent union adds artifact: {topic[:44]!r}",
                ARTIFACTS in effective,
                sorted(effective),
            )


def test_pdf_output_is_supported_and_inputs_are_not_outputs() -> None:
    """PDF is a real deliverable; an attached PDF is still only an input."""
    outputs = [
        "Give me a PDF report of the findings",
        "outline'a uygun kısa bir PDF rapor oluştur",
        "export this as pdf",
    ]
    for topic in outputs:
        check(
            f"PDF output is not rejected: {topic[:44]!r}",
            capabilities.detect_unsupported_deliverable(topic) is None,
            str(capabilities.detect_unsupported_deliverable(topic)),
        )
        check(
            f"PDF output is detected: {topic[:44]!r}",
            capabilities.detect_pdf_output(topic),
            topic,
        )
        found = capabilities.detect_artifact_request(topic)
        check(
            f"PDF output binds artifacts: {topic[:44]!r}",
            ARTIFACTS in found,
            sorted(found),
        )

    inputs = [
        "Summarize the attached PDF",
        "Yüklediğim pdf in 2. sayfasındaki tabloyu al",
    ]
    for topic in inputs:
        check(
            f"attached PDF is not an output request: {topic[:44]!r}",
            not capabilities.detect_pdf_output(topic),
            topic,
        )

    check(
        "legacy .doc is still unsupported",
        capabilities.detect_unsupported_deliverable("give me a .doc file") == "doc",
        str(capabilities.detect_unsupported_deliverable("give me a .doc file")),
    )


def test_runnable_project_requests_are_detected_as_artifacts() -> None:
    """Runnable-project deliverables are artifact-intent cases, not structural."""
    deliverables = [
        "Fetch the yearly data and give me a local API I can run",
        "Build me a REST API that serves this data",
        "Turn this into a small FastAPI service",
        "Write a Node.js server that exposes these rows as an endpoint",
        "Bana bu veriler için çalıştırabileceğim bir sunucu oluştur",
    ]
    for topic in deliverables:
        structural = capabilities.structural_requirements(topic, False)
        check(
            f"structural alone skips artifact: {topic[:44]!r}",
            ARTIFACTS not in structural,
            sorted(structural),
        )
        effective = _effective_requirements(topic, False, wants_artifact=True)
        check(
            f"intent union detects runnable project: {topic[:44]!r}",
            ARTIFACTS in effective,
            sorted(effective),
        )

    effective = _effective_requirements(
        "Build me a REST API that serves this data", False, wants_artifact=True
    )
    check(
        "a requested API also implies code_exec (code + data, not a document)",
        CODE_EXEC in effective,
        sorted(effective),
    )

    not_deliverables = [
        "Can you give me an update on the project?",
        "What's on the program for the conference?",
        "Proje güncellemesi hakkında bilgi verir misin?",
    ]
    for topic in not_deliverables:
        found = capabilities.structural_requirements(topic, False)
        check(
            f"a status update about a project is NOT an artifact request: {topic[:44]!r}",
            ARTIFACTS not in found,
            sorted(found),
        )


# ---------------------------------------------------------------------------
# 3. Orchestration: several tools is not several agents
# ---------------------------------------------------------------------------


def test_multi_tool_sequential_work_can_stay_direct() -> None:
    """A multi-capability sequential chain must be allowed to remain DIRECT.

    Nothing in the resolver may escalate to WORKFORCE merely because several
    capabilities are involved -- that would reinstate the old
    "capability count == agent count" rule the failures came from.
    """
    # (topic, has_files, what the router declares, what must end up present)
    chains = [
        (
            "Go to https://example.com/prices, pull the table, and give me an "
            "xlsx file",
            False,
            [],
            {WEB_FETCH, ARTIFACTS},
        ),
        (
            "Read the uploaded spreadsheet, convert it to JSON, and build an "
            "HTML page to search it",
            True,
            [],
            {FILES, ARTIFACTS, CODE_EXEC},
        ),
        (
            # Three ordered steps, but only ONE capability: this is the case
            # that must not be escalated just for having several verbs.
            "Look up the current EU AI Act timeline, then use it to build a "
            "compliance checklist, then rank the items by urgency",
            False,
            [WEB_SEARCH],
            {WEB_SEARCH},
        ),
    ]
    for topic, has_files, declared, required in chains:
        decision, _agent, _ = _route(topic, has_files, _result("direct", declared))
        check(
            f"sequential multi-capability work stays DIRECT: {topic[:44]!r}",
            decision.strategy == "direct",
            f"{decision.strategy} caps={sorted(decision.capabilities)}",
        )
        check(
            f"...and still gets every capability it needs: {topic[:44]!r}",
            decision.can_satisfy(required),
            f"required={sorted(required)} got={sorted(decision.capabilities)}",
        )


# ---------------------------------------------------------------------------
# 4. Failure behaviour
# ---------------------------------------------------------------------------


def test_routing_failure_stays_capable() -> None:
    """A missing declared set still keeps the structural floor."""
    topic = "Fetch https://example.com/table and give me an Excel file"
    decision, _agent, _ = _route(topic, False, asyncio.TimeoutError("boom"))
    check(
        "a failed declare still merges the structural floor",
        decision.can_satisfy({WEB_FETCH, ARTIFACTS}),
        sorted(decision.capabilities),
    )
    decision, _agent, _ = _route(topic, False, RuntimeError("bad backend"))
    check(
        "an error declare still merges the structural floor",
        decision.can_satisfy({WEB_FETCH, ARTIFACTS}),
        sorted(decision.capabilities),
    )


def test_unsupported_capability_is_reported_not_attempted() -> None:
    """A capability this system does not have must surface explicitly.

    The valid capabilities alongside it still resolve, so one hallucinated
    name never costs a job that was otherwise fine.
    """
    decision, _agent, _ = _route(
        "Render a 3D animation of the results",
        False,
        _result("direct", ["video_render", WEB_SEARCH]),
    )
    check(
        "an unknown capability is recorded as unsupported",
        "video_render" in decision.unsupported,
        str(decision.unsupported),
    )
    check(
        "an unknown capability is not silently treated as a real one",
        "video_render" not in decision.capabilities,
        sorted(decision.capabilities),
    )
    check(
        "valid capabilities alongside it still resolve",
        WEB_SEARCH in decision.capabilities,
        sorted(decision.capabilities),
    )
    message = camel_service.unsupported_capability_message(["video_render"])
    check(
        "the unsupported message names the missing capability",
        "video_render" in message,
        message[:120],
    )
    check(
        "the unsupported message states nothing was executed",
        "Nothing was executed" in message,
        message[:200],
    )


def test_empty_topic() -> None:
    decision, _agent, _ = _route("", False, _result("direct", []))
    check(
        "an empty topic still yields a skipped merge, not a crash",
        decision.outcome == capability_routing.ROUTING_SKIPPED
        and decision.strategy == "direct",
        str(decision),
    )


# ---------------------------------------------------------------------------
# 5. Deep research vs ordinary search
# ---------------------------------------------------------------------------


def test_survey_questions_still_need_the_web() -> None:
    survey_questions = [
        "Review the important 2024-2026 work on Physical AI / Vision-Language-"
        "Action systems, comparing approaches, datasets, results and "
        "limitations.",
        "Give me a literature review of retrieval-augmented generation.",
        "Vision-language modeller üzerine akademik makaleleri derle.",
    ]
    for topic in survey_questions:
        found = capabilities.structural_requirements(topic, False)
        check(
            f"survey question requires web_search: {topic[:44]!r}",
            WEB_SEARCH in found,
            sorted(found),
        )
        check(
            f"detect_deep_research true: {topic[:40]!r}",
            capabilities.detect_deep_research(topic) is True,
        )

    declined = (
        "Türkiye tekstil sektörünün güncel durumu hakkında kısa bir özet yaz, "
        "PDF olarak ver. Derin literatür taraması yapma."
    )
    check(
        "decline deep research stays lookup",
        capabilities.detect_deep_research(declined) is False,
        declined,
    )

    ordinary = [
        "What is the current price of Bitcoin?",
        "Who won the game last night?",
        "En son iPhone modeli hangisi?",
    ]
    for topic in ordinary:
        found = capabilities.structural_requirements(topic, False)
        check(
            f"ordinary lookup is not forced onto the web floor: {topic[:44]!r}",
            WEB_SEARCH not in found and WEB_FETCH not in found,
            sorted(found),
        )


# ---------------------------------------------------------------------------
# 6. Retired router lock-ins
# ---------------------------------------------------------------------------


def test_routing_call_is_bounded() -> None:
    check(
        "the execution router is gone",
        not hasattr(camel_service, "route_request")
        and not hasattr(camel_service, "ROUTING_TIMEOUT_SECONDS"),
    )


def test_json_recovery_from_unparsed_content() -> None:
    """Declared capabilities still merge without a routing model."""
    decision, _agent, _ = _route(
        "find the latest news", False, _result("direct", [WEB_SEARCH])
    )
    check(
        "declared web_search still expands to fetch",
        decision.strategy == "direct" and WEB_SEARCH in decision.capabilities,
        str(decision),
    )
    decision, _agent, _ = _route(
        "something complex", False, _result("workforce", [])
    )
    check(
        "declared workforce strategy is kept",
        decision.strategy == "workforce",
        str(decision),
    )


def test_prompt_semantics_lock_in() -> None:
    """Structural detection still encodes the load-bearing routing rules."""
    url_caps = capabilities.structural_requirements(
        "Go to https://example.com/t and extract the table", False
    )
    file_caps = _effective_requirements(
        "Give me the results as an Excel file", False, wants_artifact=True
    )
    check(
        "a named URL needs web_fetch",
        WEB_FETCH in url_caps,
        sorted(url_caps),
    )
    check(
        "a requested FILE needs artifacts via intent union",
        ARTIFACTS in file_caps,
        sorted(file_caps),
    )
    check(
        "the registry still lists every capability name",
        set(capabilities.ALL_CAPABILITIES)
        == {WEB_SEARCH, WEB_FETCH, FILES, ARTIFACTS, CODE_EXEC},
        capabilities.ALL_CAPABILITIES,
    )


def test_capability_expansion_rules() -> None:
    check(
        "unknown capability names are dropped by expand()",
        # web_search implies web_fetch (see WEB_SEARCH's CapabilitySpec):
        # discovering a page and never being able to retrieve it is a dead
        # end, so the expansion carries fetch along automatically.
        capabilities.expand(["web_search", "nonsense"]) == frozenset({WEB_SEARCH, WEB_FETCH}),
        sorted(capabilities.expand(["web_search", "nonsense"])),
    )
    check(
        "web_search implies web_fetch",
        capabilities.expand([WEB_SEARCH]) == frozenset({WEB_SEARCH, WEB_FETCH}),
        sorted(capabilities.expand([WEB_SEARCH])),
    )
    check(
        "artifacts requires a workspace",
        capabilities.requires_workspace([ARTIFACTS]),
    )
    check(
        "code_exec requires a workspace",
        capabilities.requires_workspace([CODE_EXEC]),
    )
    check(
        "plain search does not require a workspace",
        not capabilities.requires_workspace([WEB_SEARCH]),
    )
    check(
        "web_fetch (implied by search) does not require a workspace either",
        not capabilities.requires_workspace([WEB_SEARCH, WEB_FETCH]),
    )


# ---------------------------------------------------------------------------
# 7. Acquisition capability: web_search implies web_fetch
# ---------------------------------------------------------------------------
#
# Live failure this pins: "Go to <organization>'s site and give me the
# current rates as Excel" resolved to WEB_SEARCH + ARTIFACTS but never
# WEB_FETCH, because structural_requirements() only grants WEB_FETCH from a
# LITERAL URL in the text (see capabilities.py:491-494), and this phrasing
# names a source by reference, not by URL. Search located the exact official
# page; the executor then had no tool to retrieve it and fell back to
# secondary snippets. The fix is general: WEB_SEARCH now implies WEB_FETCH
# (capabilities.py's WEB_SEARCH CapabilitySpec), so any executor that can
# discover a page can also operate on it -- independent of whether the
# request also asks for a deliverable format. These tests use no real
# organization name, on purpose: the fix is about the search/fetch pairing,
# not about any one source.


def test_search_without_literal_url_gets_fetch_too() -> None:
    """Example 1/3: naming a source by reference (no literal URL, no
    requested output format) resolves to search + fetch, never search alone.
    """
    cases = [
        "Go to the vendor's official site and tell me the current rates",
        "Find the official documentation and summarize it",
        "Bu kurumun resmi sitesine git ve güncel verileri söyle",
    ]
    for topic in cases:
        resolved = capabilities.advisory_requirements(topic, False)
        check(
            f"search-without-URL still resolves web_fetch: {topic[:44]!r}",
            WEB_SEARCH in resolved and WEB_FETCH in resolved,
            sorted(resolved),
        )
        check(
            f"...with no artifact capability leaking in: {topic[:44]!r}",
            ARTIFACTS not in resolved,
            sorted(resolved),
        )


def test_search_plus_artifact_request_stays_orthogonal() -> None:
    """Acquisition (search+fetch) and deliverable (artifacts) union via intent."""
    topic = "Go to the vendor's official site and give me the current rates as Excel"
    resolved = _effective_requirements(topic, False, wants_artifact=True)
    check(
        "search+excel: web_search, web_fetch and artifacts are all present",
        {WEB_SEARCH, WEB_FETCH, ARTIFACTS} <= resolved,
        sorted(resolved),
    )


def test_search_plus_api_request_resolves_independently() -> None:
    """Example 4: a runnable-project deliverable on top of discovered data
    resolves web_search/web_fetch (acquisition) and artifacts/code_exec
    (deliverable) independently -- one is not gated on the other."""
    topic = "Find this organization's current data and create a local API"
    resolved = _effective_requirements(topic, False, wants_artifact=True)
    check(
        "search+API: acquisition capabilities are present",
        {WEB_SEARCH, WEB_FETCH} <= resolved,
        sorted(resolved),
    )
    check(
        "search+API: deliverable capabilities are present, resolved separately",
        {ARTIFACTS, CODE_EXEC} <= resolved,
        sorted(resolved),
    )


def test_literal_url_request_is_unaffected() -> None:
    """Example 5: a request naming a literal URL must still work exactly as
    before -- structural detection (not the new implication) is what grants
    web_fetch here, and that path is untouched."""
    topic = "Fetch https://example.com/data.html and tell me what's on it"
    structural = capabilities.structural_requirements(topic, False)
    check(
        "a literal URL still structurally requires web_fetch",
        WEB_FETCH in structural,
        sorted(structural),
    )
    decision, _agent, _ = _route(topic, False, _result("direct", []))
    check(
        "a literal-URL job is still satisfiable even if the router says nothing",
        decision.can_satisfy({WEB_FETCH}),
        sorted(decision.capabilities),
    )


def test_search_quota_exhaustion_does_not_remove_fetch() -> None:
    """Example 6: search's own per-agent call budget and web_read's tool
    binding are two independent mechanisms. Exhausting the search budget
    must not remove or disable the fetch tools that were bound alongside it
    -- they are a different capability with a different, separate budget
    (web_tools.MAX_WEB_READS_PER_JOB), not gated by search's call count.
    """
    workspace_root = tempfile.mkdtemp(prefix="search_fetch_budget_test_")

    def _stub_search(*args, **kwargs):
        # No network in this test -- only the budget wrapper's own
        # bookkeeping is under test, not a real search provider.
        return [{"result_id": 1, "title": "stub", "description": "", "url": "https://example.com/x"}]

    with patch.object(camel_service, "search_web", _stub_search):
        budgeted = camel_service._budgeted_search_tool()
        context = capabilities.ToolContext(
            workspace_root=workspace_root,
            search_tool_factory=lambda: budgeted,
        )
        bound = capabilities.resolve_tools({WEB_SEARCH}, context)

        tool_names = set(bound.names)
        check(
            "one WEB_SEARCH capability binds the whole acquisition pipeline",
            tool_names == {"web_search", "web_read", "transform_table"},
            sorted(tool_names),
        )

        # Exhaust the search budget. DISTINCT queries on purpose: an
        # identical repeated query is served from the memo cache and never
        # reaches the budget check at all (asserted separately below).
        for i in range(camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES):
            budgeted(f"query-{i}")
        exhausted = budgeted("a brand new query")
        check(
            "the search budget is actually exhausted after its call limit",
            exhausted.get("search_budget_exhausted") is True,
            str(exhausted),
        )

        # The fetch tools are separate objects with no dependency on the
        # search wrapper's internal call_count -- still present, still the
        # same bound methods, unaffected by search's exhaustion.
        still_bound = set(bound.names)
        check(
            "the acquisition tool remains bound after the search budget is exhausted",
            still_bound == {"web_search", "web_read", "transform_table"},
            sorted(still_bound),
        )

        # An already-answered query is still answered from cache even now,
        # rather than being told the budget is gone -- it costs nothing.
        repeat = budgeted("query-0")
        check(
            "an identical repeated query is served from cache, not refused",
            isinstance(repeat, list) and repeat and repeat[0]["title"] == "stub",
            str(repeat)[:160],
        )


def test_identical_search_does_no_duplicate_real_work() -> None:
    """The same query twice must hit the network once and charge the budget
    once -- CAMEL's streaming path can dispatch one logical call twice, and
    re-running a search returns the same links anyway."""
    executions: list = []

    def _stub_search(query, *args, **kwargs):
        executions.append(query)
        return [{"result_id": 1, "title": "stub", "description": "", "url": "https://example.com/x"}]

    with patch.object(camel_service, "search_web", _stub_search):
        budgeted = camel_service._budgeted_search_tool()
        first = budgeted("same query")
        second = budgeted("same query")

    check(
        "an identical search query executes real search work only once",
        len(executions) == 1,
        str(executions),
    )
    check(
        "the memoized repeat returns the same results",
        first == second,
        f"{first} vs {second}",
    )

    # ...and the budget was only charged once, so a duplicate dispatch does
    # not eat an agent's whole search allowance.
    with patch.object(camel_service, "search_web", _stub_search):
        executions.clear()
        budgeted = camel_service._budgeted_search_tool()
        for _ in range(camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES + 2):
            budgeted("one query")
        fresh = budgeted("a different query")
    check(
        "duplicate identical dispatches do not consume the search budget",
        isinstance(fresh, list) and len(executions) == 2,
        f"executions={executions} fresh={str(fresh)[:120]}",
    )


def test_search_is_logged_exactly_once_per_real_call() -> None:
    """Regression: search_web was wrapped by logged_tool twice -- once inside
    _budgeted_search_tool (so only real network calls are recorded) and again
    by resolve_tools -- so every real search produced two identical records
    and all search telemetry read double."""
    from tool_call_log import ToolCallRecorder

    recorder = ToolCallRecorder(job_id="test")

    def _stub_search(*args, **kwargs):
        return []

    with patch.object(camel_service, "search_web", _stub_search):
        context = capabilities.ToolContext(
            tool_call_recorder=recorder,
            search_tool_factory=lambda: camel_service._budgeted_search_tool(recorder),
        )
        bound = capabilities.resolve_tools({WEB_SEARCH}, context)
        search_tool = next(
            tool for tool, name in zip(bound.tools, bound.names)
            if name == "web_search"
        )
        search_tool(query="a query")

    records = recorder.snapshot()
    names = [call["tool_name"] for call in records]
    # One real operation is recorded, not one operation twice: the
    # model-facing web_search call itself dispatches the search directly now
    # (no separate internal search step to double-record), so the regression
    # this guards -- one real network call producing two log entries -- would
    # show up as len(records) == 2 for a single web_search call.
    check(
        "a real search is recorded exactly once, not twice",
        len(records) == 1 and names == ["web_search"],
        f"{len(records)} records: {names}",
    )

    # The pre-existing property this double-wrap fix must not break: a
    # budget-exhausted stub does no network work and is never recorded.
    with patch.object(camel_service, "search_web", _stub_search):
        recorder2 = ToolCallRecorder(job_id="test2")
        budgeted = camel_service._budgeted_search_tool(recorder2)
        for i in range(camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES + 2):
            budgeted(f"q{i}")
    check(
        "over-budget search stubs are still not recorded as real calls",
        len(recorder2.snapshot()) == camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES,
        str(len(recorder2.snapshot())),
    )


def test_no_progress_guard_stops_repeated_identical_failures() -> None:
    """A tool that keeps failing the same way with the same arguments must be
    stopped long before any per-run budget is exhausted, and the model must
    be told to do something else rather than handed another retryable-looking
    error string."""
    from tool_guard import MAX_IDENTICAL_FAILURES, NoProgressGuard

    attempts = {"count": 0}

    def flaky_tool(url: str) -> dict:
        """A tool docstring, so @wraps preservation is observable."""
        attempts["count"] += 1
        raise RuntimeError("deterministic boom")

    guard = NoProgressGuard()
    guarded = guard.wrap(flaky_tool)

    results = [
        _call_capturing(guarded, "https://example.com/x") for _ in range(20)
    ]
    check(
        "a deterministically failing call stops executing after a small bound",
        attempts["count"] == MAX_IDENTICAL_FAILURES,
        f"executed {attempts['count']} times",
    )
    check(
        "20 identical repeated failures cannot occur",
        attempts["count"] < 20,
        str(attempts["count"]),
    )
    blocked = results[-1]
    check(
        "the blocked call returns a clear no-progress instruction, not an exception",
        isinstance(blocked, dict) and blocked.get("no_progress") is True,
        str(blocked)[:200],
    )
    check(
        "the instruction tells the model to change approach",
        "different tool" in (blocked.get("instruction") or ""),
        str(blocked)[:250],
    )
    check(
        "the guard preserves the tool's name and docstring for CAMEL's schema",
        guarded.__name__ == "flaky_tool" and "tool docstring" in (guarded.__doc__ or ""),
        f"{guarded.__name__} / {guarded.__doc__}",
    )


def test_no_progress_guard_allows_recovery_and_distinct_calls() -> None:
    """The guard must not break the cases it is not for: a transient failure
    that then succeeds, a different error, and different arguments."""
    from tool_guard import NoProgressGuard

    state = {"calls": 0}

    def sometimes(url: str) -> str:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient blip")
        return f"ok:{url}"

    guard = NoProgressGuard()
    guarded = guard.wrap(sometimes)

    _call_capturing(guarded, "https://example.com/a")  # fails once
    recovered = _call_capturing(guarded, "https://example.com/a")
    check(
        "a transient failure followed by success still recovers",
        recovered == "ok:https://example.com/a",
        str(recovered),
    )

    # A different argument is a different call and is never blocked, even
    # while another key is failing.
    def always_fails(url: str) -> str:
        raise RuntimeError("boom")

    guard2 = NoProgressGuard()
    blocked_tool = guard2.wrap(always_fails)
    for _ in range(5):
        _call_capturing(blocked_tool, "https://example.com/stuck")
    other = _call_capturing(blocked_tool, "https://example.com/different")
    check(
        "a different argument is not blocked by another key's failures",
        isinstance(other, RuntimeError),
        str(other),
    )

    # A DIFFERENT error for the same arguments means something changed, so
    # the counter restarts rather than latching.
    errors = iter(["first", "second", "third", "fourth"])

    def varying(url: str) -> str:
        raise RuntimeError(next(errors))

    guard3 = NoProgressGuard()
    varying_tool = guard3.wrap(varying)
    outcomes = [_call_capturing(varying_tool, "https://example.com/y") for _ in range(4)]
    check(
        "distinct errors for the same arguments do not trip the guard",
        all(isinstance(outcome, RuntimeError) for outcome in outcomes),
        str(outcomes),
    )


def _call_capturing(func, *args, **kwargs):
    """Call `func`, returning either its result or the exception it raised."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the exception IS the observation
        return exc


def test_structured_extraction_is_part_of_web_fetch() -> None:
    """Whenever WEB_FETCH resolves (directly or via the search implication),
    web_read must be bound -- structured extraction is part of web_fetch,
    not a separate grant a caller could miss."""
    workspace_root = tempfile.mkdtemp(prefix="web_fetch_toolset_test_")
    context = capabilities.ToolContext(workspace_root=workspace_root)

    for capability_set, label in (
        ({WEB_FETCH}, "direct web_fetch grant"),
        ({WEB_SEARCH}, "web_fetch implied via web_search"),
    ):
        bound = capabilities.resolve_tools(capability_set, context)
        names = set(bound.names)
        check(
            f"structured extraction is available via web_read: {label}",
            names == {"web_search", "web_read", "transform_table"},
            sorted(names),
        )


def test_named_web_source_without_a_url_gets_web_capabilities() -> None:
    """The live failure: "tcmb'nin sitesinden ... al ve bana excel olarak
    ver" resolved to ARTIFACTS alone. There is no literal URL for find_urls,
    and no _SEARCH_RE keyword either -- the request named the SOURCE rather
    than saying the data was "güncel" -- so a request that names a website
    in as many words got no web capability at all.

    No organization name is asserted on anywhere here: the rule is about
    "named site + acquire from it", not about any particular site.
    """
    # (request, must include, must NOT include)
    cases = [
        # The live prompt, verbatim.
        (
            "tcmb'nin sitesinden 13 ağustos 2026 tarihinin kurlarını al ve "
            "bana excel olarak ver",
            {WEB_SEARCH, WEB_FETCH, ARTIFACTS},
            set(),
        ),
        # Official-site wording, Turkish.
        ("Kurumun resmi sitesinden verileri çek", {WEB_SEARCH, WEB_FETCH}, {ARTIFACTS}),
        # Historical date from a named site, no artifact asked for.
        (
            "O bankanın sitesinden 13 ağustos 2026 kurlarını al",
            {WEB_SEARCH, WEB_FETCH},
            {ARTIFACTS},
        ),
        # Turkish web page / address wording.
        (
            "Şirketin web sayfasından fiyat listesini indir",
            {WEB_SEARCH, WEB_FETCH},
            set(),
        ),
        # English equivalents.
        (
            "Go to the vendor's website and get the current prices",
            {WEB_SEARCH, WEB_FETCH},
            {ARTIFACTS},
        ),
        (
            "Download the rates table from their official site as an Excel file",
            {WEB_SEARCH, WEB_FETCH, ARTIFACTS},
            set(),
        ),
        (
            "Fetch yesterday's figures from the agency homepage",
            {WEB_SEARCH, WEB_FETCH},
            set(),
        ),
    ]
    for topic, required, forbidden in cases:
        wants = capabilities.ARTIFACTS in required
        found = _effective_requirements(topic, False, wants_artifact=wants)
        check(
            f"named web source resolves web capabilities: {topic[:42]!r}",
            required <= found,
            f"missing {sorted(required - found)} in {sorted(found)}",
        )
        if forbidden:
            check(
                f"...and nothing unrelated leaks in: {topic[:42]!r}",
                not (forbidden & found),
                sorted(found),
            )


def test_page_wording_is_a_web_source_unless_it_is_a_local_document() -> None:
    """"X'in sayfasına git ... al" still resolved to ARTIFACTS alone: bare
    "sayfa"/"page" was left out of the source pattern because it is also a
    page of an uploaded PDF. It is ambiguous, not unusable -- what
    disambiguates it is whether the request points at a local document."""
    web = [
        "diyanetin sayfasına git yıllık namaz vakitlerini al ve bana excel olarak ver",
        "Kurumun sayfasından yıllık verileri çek",
        "go to their page and get the table",
        "Open the agency page and download the yearly figures",
    ]
    for topic in web:
        found = capabilities.structural_requirements(topic, False)
        check(
            f"page wording resolves web capabilities: {topic[:42]!r}",
            {WEB_SEARCH, WEB_FETCH} <= found,
            sorted(found),
        )

    # The same word, about something the user already gave us.
    local = [
        "Bu dosyanın 3. sayfasını özetle",
        "yüklediğim pdf in 2. sayfasındaki tabloyu al",
        "Get the data from page 4 of the attached document",
        "Summarize page 2 of the uploaded spreadsheet",
    ]
    for topic in local:
        found = capabilities.structural_requirements(topic, True)
        check(
            f"a page OF A DOCUMENT is not a web source: {topic[:42]!r}",
            WEB_SEARCH not in found and WEB_FETCH not in found,
            sorted(found),
        )


def test_named_web_source_does_not_over_detect() -> None:
    """The other half of the conjunction: mentioning a site is not asking to
    read one, and asking to "get" something is not naming a source."""
    not_web = [
        # Names a site, acquires nothing from it.
        "How do I build a website?",
        "Explain how a web page is rendered by the browser",
        "Bir web sitesi nasıl yapılır?",
        # Acquires, but names no source.
        "Give me a summary of the attached report",
        "Bana bu verilerin özetini ver",
        # "sayfa" here is a page of an UPLOADED file, not a web page --
        # deliberately excluded from the source pattern for this reason.
        "Bu dosyanın 3. sayfasını özetle",
        # "kur" as the imperative of kurmak ("set up"), not an exchange rate.
        "Bir sunucu kur ve bana bir API oluştur",
        "Explain how photosynthesis works.",
        "merhaba, nasılsın?",
    ]
    for topic in not_web:
        found = capabilities.structural_requirements(topic, False)
        check(
            f"no false web capability: {topic[:44]!r}",
            WEB_SEARCH not in found and WEB_FETCH not in found,
            sorted(found),
        )


def test_literal_url_behaviour_is_unchanged() -> None:
    """A request WITH a URL must still resolve exactly as before: web_fetch
    structurally, with no search added -- the page is already identified."""
    topic = "Fetch https://example.com/data.html and give me an xlsx file"
    found = capabilities.structural_requirements(topic, False)
    check(
        "a literal URL still structurally requires web_fetch",
        WEB_FETCH in found,
        sorted(found),
    )
    check(
        "a literal URL does not additionally trigger the named-source rule",
        WEB_SEARCH not in found,
        sorted(found),
    )
    effective = _effective_requirements(topic, False, wants_artifact=True)
    check(
        "artifact comes from intent union, not structural",
        ARTIFACTS in effective,
        sorted(effective),
    )
    # A URL *and* site wording still must not add search: find_urls wins.
    both = capabilities.structural_requirements(
        "Go to their website at https://example.com/x and download the table", False
    )
    check(
        "a URL alongside site wording still resolves to fetch, not search",
        WEB_FETCH in both and WEB_SEARCH not in both,
        sorted(both),
    )


def test_named_web_source_does_not_change_routing_strategy() -> None:
    """Capability detection does not pick Single vs Workforce."""
    import main

    cases = [
        "tcmb'nin sitesinden kurları al ve bana excel olarak ver",
        "Go to the vendor's website and get the prices",
        "Independently research pricing, support and integrations from "
        "each vendor's official website, then combine the findings",
    ]
    for topic in cases:
        single = main._routing_for_ui_mode(
            "single", topic, has_workspace_files=False, workspace_file_kinds=[]
        )
        check(f"Single for {topic[:40]!r}", single.strategy == "single")

    async def _multi(topic: str):
        async def fake_classify(t, tracker, persisted=None, **_kwargs):
            wants = _intent_wants_artifact(t)
            return artifact_intent.ArtifactIntent(
                wants, "xlsx" if wants else "none", "llm"
            ).normalized()

        with patch.object(
            artifact_intent,
            "classify_artifact_intent",
            fake_classify,
        ):
            decision, _ = await main._routing_for_workforce(
                topic,
                has_workspace_files=False,
                workspace_file_kinds=[],
                tracker=UsageTracker(),
            )
            return decision

    for topic in cases:
        multi = asyncio.run(_multi(topic))
        check(f"Workforce for {topic[:40]!r}", multi.strategy == "workforce")


def test_routing_strategy_selection_is_unchanged_by_the_implication() -> None:
    """web_search still implies web_fetch regardless of UI mode."""
    cases = [
        "Go to the vendor's official site and tell me the current rates",
        "What is the latest stable version of Python?",
        "Independently research pricing, support, and integrations for "
        "three different vendors, then combine the findings",
    ]
    for topic in cases:
        resolved = capabilities.advisory_requirements(topic, False)
        check(
            f"web_fetch rides along wherever web_search does: {topic[:44]!r}",
            WEB_SEARCH not in resolved or WEB_FETCH in resolved,
            sorted(resolved),
        )


def main() -> None:
    test_production_routing_makes_no_model_call()
    test_deterministic_direct_is_the_default()
    test_deterministic_workforce_needs_strong_evidence()
    test_sequential_chain_vetoes_domain_enumeration()
    test_two_domains_is_not_enough()
    test_profiles_are_persona_only()
    test_shadow_mode_records_but_never_obeys()
    test_mentor_case_url_to_excel()
    test_mentor_case_xlsx_to_searchable_web()
    test_structural_floor_cannot_be_lowered()
    test_router_may_add_capabilities()
    test_simple_requests_stay_cheap()
    test_format_mention_alone_is_not_a_deliverable()
    test_apply_artifact_intent_unions_artifacts()
    test_pdf_output_is_supported_and_inputs_are_not_outputs()
    test_runnable_project_requests_are_detected_as_artifacts()
    test_multi_tool_sequential_work_can_stay_direct()
    test_genuinely_independent_work_can_become_workforce()
    test_routing_failure_stays_capable()
    test_unsupported_capability_is_reported_not_attempted()
    test_empty_topic()
    test_survey_questions_still_need_the_web()
    test_prompt_carries_only_topic_and_file_flag()
    test_routing_call_is_bounded()
    test_json_recovery_from_unparsed_content()
    test_prompt_semantics_lock_in()
    test_capability_expansion_rules()
    test_search_without_literal_url_gets_fetch_too()
    test_search_plus_artifact_request_stays_orthogonal()
    test_search_plus_api_request_resolves_independently()
    test_literal_url_request_is_unaffected()
    test_search_quota_exhaustion_does_not_remove_fetch()
    test_identical_search_does_no_duplicate_real_work()
    test_search_is_logged_exactly_once_per_real_call()
    test_no_progress_guard_stops_repeated_identical_failures()
    test_no_progress_guard_allows_recovery_and_distinct_calls()
    test_named_web_source_without_a_url_gets_web_capabilities()
    test_page_wording_is_a_web_source_unless_it_is_a_local_document()
    test_named_web_source_does_not_over_detect()
    test_literal_url_behaviour_is_unchanged()
    test_named_web_source_does_not_change_routing_strategy()
    test_structured_extraction_is_part_of_web_fetch()
    test_routing_strategy_selection_is_unchanged_by_the_implication()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All capability-routing checks passed")


if __name__ == "__main__":
    main()
