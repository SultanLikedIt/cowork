"""Artifact-intent classifier: produce vs analyze, with regex fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import artifact_intent
import capabilities
from execution.langgraph_runtime import openai_chat
from model_config import UsageTracker

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        raise AssertionError(f"{name} -- {detail}")


class _Scripted:
    """Stands in for openai_chat.complete_chat: a parsed model, raw text, or an error."""

    parsed = None
    content = ""
    error = None

    @staticmethod
    async def complete_chat(messages, **kwargs):
        if _Scripted.error is not None:
            raise _Scripted.error
        if _Scripted.parsed is not None:
            return _Scripted.parsed
        return _Scripted.content


def _classify(topic: str, parsed=None, content="", error=None, persisted=None):
    _Scripted.parsed = parsed
    _Scripted.content = content
    _Scripted.error = error
    with patch.object(openai_chat, "complete_chat", _Scripted.complete_chat):
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


class _Flaky:
    """Fails (or answers with no JSON) until ``fail_times`` calls are used up."""

    calls = 0
    fail_times = 1
    failure: object = TimeoutError("step timed out")
    parsed = None

    @staticmethod
    async def complete_chat(messages, **kwargs):
        _Flaky.calls += 1
        if _Flaky.calls <= _Flaky.fail_times:
            if isinstance(_Flaky.failure, BaseException):
                raise _Flaky.failure
            return "no json here"
        return _Flaky.parsed


def _classify_flaky(topic: str, *, fail_times: int, failure=TimeoutError("t/o")):
    _Flaky.calls = 0
    _Flaky.fail_times = fail_times
    _Flaky.failure = failure
    _Flaky.parsed = artifact_intent.ArtifactIntentResult(
        wants_artifact=True, artifact_type="xlsx", needs_code_execution=True
    )
    with patch.object(openai_chat, "complete_chat", _Flaky.complete_chat):
        return asyncio.run(artifact_intent.classify_artifact_intent(topic, UsageTracker()))


def test_a_timed_out_classifier_is_retried_before_the_fallback() -> None:
    intent = _classify_flaky("faiz tablosu excel", fail_times=1)
    assert _Flaky.calls == 2
    assert intent.source == "llm" and intent.needs_code_execution is True


def test_a_reply_without_json_is_retried_too() -> None:
    intent = _classify_flaky("faiz tablosu excel", fail_times=1, failure=None)
    assert _Flaky.calls == 2 and intent.source == "llm"


def test_two_failures_reach_the_regex_fallback() -> None:
    intent = _classify_flaky("faiz tablosu excel", fail_times=2)
    assert _Flaky.calls == artifact_intent.INTENT_ATTEMPTS == 2
    assert intent.source == "regex_fallback"


def test_the_fallback_sends_arithmetic_on_given_numbers_to_code_not_the_web() -> None:
    intent = artifact_intent.fallback_from_regex(
        "aylık %3 faizle 100.000 TL 24 ayda ne olur, ay ay tablo olarak excel ver"
    )
    assert intent.needs_code_execution is True
    assert intent.needs_web_research is False
    assert intent.wants_artifact is True and intent.artifact_type == "xlsx"

    stats = artifact_intent.fallback_from_regex("12, 15, 9, 22 sayılarının ortalaması")
    assert stats.needs_code_execution is True and stats.needs_web_research is False


def test_the_fallback_still_searches_for_current_rates_and_plain_questions() -> None:
    live = artifact_intent.fallback_from_regex("güncel kredi faizleri 2026 ne kadar")
    assert live.needs_web_research is True and live.needs_code_execution is False

    no_numbers = artifact_intent.fallback_from_regex("bileşik faiz nedir")
    assert no_numbers.needs_web_research is True and no_numbers.needs_code_execution is False
