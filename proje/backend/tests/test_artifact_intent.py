"""Artifact-intent classifier: produce vs analyze, with regex fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import artifact_intent
import capabilities
from model_config import UsageTracker

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        raise AssertionError(f"{name} -- {detail}")


class _Msg:
    def __init__(self, parsed=None, content=""):
        self.parsed = parsed
        self.content = content


class _Response:
    def __init__(self, parsed=None, content=""):
        self.msg = _Msg(parsed, content)


class _ScriptedAgent:
    parsed = None
    content = ""
    error = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def astep(self, prompt, response_format=None):
        if _ScriptedAgent.error is not None:
            raise _ScriptedAgent.error
        return _Response(_ScriptedAgent.parsed, _ScriptedAgent.content)


def _classify(topic: str, parsed=None, content="", error=None, persisted=None):
    _ScriptedAgent.parsed = parsed
    _ScriptedAgent.content = content
    _ScriptedAgent.error = error
    with patch.object(artifact_intent, "ChatAgent", _ScriptedAgent, create=True), patch(
        "camel.agents.ChatAgent", _ScriptedAgent
    ), patch("model_config.get_model", lambda *a, **k: None):
        return asyncio.run(
            artifact_intent.classify_artifact_intent(
                topic, UsageTracker(), persisted=persisted
            )
        )


def test_create_vs_analyze() -> None:
    cases = [
        ("Create an Excel file of the standings", True, "xlsx"),
        ("Crée-moi un fichier Excel des classements", True, "xlsx"),
        ("bunu excel olarak ver", True, "xlsx"),
        ("Analyse ce fichier Excel", False, "none"),
        ("Excel dosyasını analiz et", False, "none"),
        ("5 makale özetle", False, "none"),
        ("Count the rows in this spreadsheet", False, "none"),
    ]
    for topic, wants, kind in cases:
        result = artifact_intent.ArtifactIntentResult(
            wants_artifact=wants, artifact_type=kind
        )
        intent = _classify(topic, parsed=result)
        check(
            f"llm {topic[:40]!r}",
            intent.wants_artifact is wants and intent.artifact_type == kind,
            f"{intent.wants_artifact} {intent.artifact_type}",
        )
        check(f"...source llm: {topic[:40]!r}", intent.source == "llm")


def test_typo_exsel_is_xlsx() -> None:
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=True, artifact_type="xlsx"
    )
    intent = _classify("bunu exsel olarak ver", parsed=result)
    check(
        "typo exsel -> xlsx",
        intent.wants_artifact is True and intent.artifact_type == "xlsx",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )


def test_none_forces_wants_false() -> None:
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=True, artifact_type="none"
    )
    intent = _classify("hello", parsed=result)
    check(
        "artifact_type none forces wants_artifact false",
        intent.wants_artifact is False and intent.artifact_type == "none",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )


def test_timeout_falls_back_to_regex() -> None:
    intent = _classify(
        "Give me this as an Excel file",
        error=TimeoutError("step timed out"),
    )
    check("timeout uses regex fallback", intent.source == "regex_fallback")
    check(
        "timeout regex still sees excel",
        intent.wants_artifact is True and intent.artifact_type == "xlsx",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )
    check(
        "regex uses detect_artifact_request",
        capabilities.ARTIFACTS
        in capabilities.detect_artifact_request("Give me this as an Excel file"),
    )


def test_replay_skips_classifier() -> None:
    intent = _classify(
        "ignored",
        error=AssertionError("must not call the model on replay"),
        persisted={
            "wants_artifact": True,
            "artifact_type": "csv",
            "artifact_intent_source": "llm",
        },
    )
    check(
        "persisted intent is reused",
        intent.wants_artifact is True
        and intent.artifact_type == "csv"
        and intent.source == "llm",
        intent,
    )


def test_seed_usage_does_not_skip_classifier() -> None:
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=True, artifact_type="xlsx"
    )
    intent = _classify(
        "Give me this as an Excel file",
        parsed=result,
        persisted={"wants_artifact": None, "artifact_type": None},
    )
    check("seed usage still classifies", intent.source == "llm")
    check(
        "seed usage is xlsx",
        intent.wants_artifact is True and intent.artifact_type == "xlsx",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )


def test_invalid_payload_falls_back() -> None:
    intent = _classify("make me a csv", parsed=None, content="not json")
    check("invalid payload uses regex", intent.source == "regex_fallback")
    check("csv regex", intent.wants_artifact is True and intent.artifact_type == "csv")


def test_llm_trusts_false_on_excel_produce() -> None:
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=False, artifact_type="none", needs_web_research=True
    )
    intent = _classify("süper lig puan durumunu excel olarak ver", parsed=result)
    check("no regex override", intent.source == "llm")
    check(
        "llm false negative stays false",
        intent.wants_artifact is False and intent.artifact_type == "none",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )


def test_research_plus_file_sets_web_and_artifact() -> None:
    topic = "TUSAŞ'ın tarihini araştırıp dosya olarak ver"
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=True, artifact_type="docx", needs_web_research=True
    )
    intent = _classify(topic, parsed=result)
    structural = capabilities.workforce_structural_requirements(topic, False)
    effective = capabilities.apply_artifact_intent(
        structural,
        wants_artifact=intent.wants_artifact,
        needs_web_research=intent.needs_web_research,
        text=topic,
    )
    check("intent wants file", intent.wants_artifact is True)
    check("intent wants web", intent.needs_web_research is True)
    check(
        "effective includes web and artifacts",
        capabilities.WEB_SEARCH in effective and capabilities.ARTIFACTS in effective,
        sorted(effective),
    )
    streams = __import__("workforce_plan").expand_plan_for_coverage(
        topic,
        __import__("workforce_plan").fallback_plan(topic, effective),
        effective,
        max_subtasks=4,
    )
    research = [
        s
        for s in streams
        if s.capabilities & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
    ]
    artifact = [
        s for s in streams if capabilities.ARTIFACTS in s.capabilities
    ]
    check("plan has research stream", len(research) == 1, len(research))
    check("plan has artifact stream", len(artifact) == 1, len(artifact))
    check("artifact depends on research", artifact[0].depends_on == (0,))


def test_llm_none_on_excel_analyze_is_kept() -> None:
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=False, artifact_type="none"
    )
    intent = _classify("Excel dosyasını analiz et", parsed=result)
    check("analyze stays llm", intent.source == "llm")
    check(
        "analyze stays none",
        intent.wants_artifact is False and intent.artifact_type == "none",
        f"{intent.wants_artifact} {intent.artifact_type}",
    )


def test_llm_word_doc_unions_artifacts_capability() -> None:
    """Word doc phrasing that regex misses is covered by intent → effective caps."""
    topic = "Summarize 5 AI papers as a Word doc"
    structural = capabilities.workforce_structural_requirements(topic, False)
    check(
        "workforce structural alone has no artifacts for Word doc shorthand",
        capabilities.ARTIFACTS not in structural,
        sorted(structural),
    )
    result = artifact_intent.ArtifactIntentResult(
        wants_artifact=True, artifact_type="docx", needs_web_research=True
    )
    intent = _classify(topic, parsed=result)
    effective = capabilities.apply_artifact_intent(
        structural,
        wants_artifact=intent.wants_artifact,
        needs_web_research=intent.needs_web_research,
        text=topic,
    )
    check(
        "intent union adds ARTIFACTS for Word doc",
        capabilities.ARTIFACTS in effective,
        sorted(effective),
    )
    check(
        "artifact type is docx",
        intent.artifact_type == "docx",
        intent.artifact_type,
    )


def main() -> None:
    test_create_vs_analyze()
    test_typo_exsel_is_xlsx()
    test_none_forces_wants_false()
    test_timeout_falls_back_to_regex()
    test_replay_skips_classifier()
    test_seed_usage_does_not_skip_classifier()
    test_invalid_payload_falls_back()
    test_llm_trusts_false_on_excel_produce()
    test_research_plus_file_sets_web_and_artifact()
    test_llm_none_on_excel_analyze_is_kept()
    test_llm_word_doc_unions_artifacts_capability()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All artifact-intent checks passed")


if __name__ == "__main__":
    main()
