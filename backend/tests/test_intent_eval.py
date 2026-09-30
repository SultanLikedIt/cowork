"""The intent regression runner, without a model.

The live run (reliability_harness.intent_eval) spends model calls and is run
by hand. What this file guards is everything that can break silently in
between: a malformed case, an expected agent list that the routing code
could never produce, and a classifier outage being scored as misrouting.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import capabilities
from reliability_harness import intent_eval as ie

_ALL_FALSE = {flag: (False if kind is bool else "none") for flag, kind in ie.FLAG_TYPES.items()}


def _intent(source: str = "llm", **flags):
    return SimpleNamespace(source=source, **{**_ALL_FALSE, **flags})


def _case(**overrides) -> dict:
    case = {"id": "x", "text": "güncel dolar", "expect": {"needs_web_research": True}}
    case.update(overrides)
    return case


# --- the case file -------------------------------------------------------------


def test_the_shipped_case_file_is_valid_and_broad() -> None:
    cases = ie.load_cases()
    assert len(cases) >= 50
    tags = {tag for case in cases for tag in case.tags}
    assert {"chat", "web", "file", "code", "upload", "mail", "email", "social", "jira", "github", "documents"} <= tags


def test_expected_agents_are_what_routing_produces_for_the_expected_flags() -> None:
    """If a case says 'these flags' and 'these agents', the routing code must
    turn exactly those flags into exactly those agents. Otherwise the case
    can never pass and a live failure would blame the classifier."""
    pytest.importorskip("langgraph")
    for case in ie.load_cases():
        if case.expect_roles is None:
            continue
        roles = ie.roles_for(_intent(**case.expect), case.text, case.attachments, case.library)
        assert roles == list(case.expect_roles), case.id


@pytest.mark.parametrize(
    "bad, message",
    [
        (_case(expect={"reads_githb": True}), "unknown flag"),
        (_case(expect={"reads_github": "yes"}), "must be bool"),
        (_case(expect={"artifact_type": "gif"}), "unknown artifact_type"),
        (_case(expect={}), "at least one flag"),
        (_case(text=" "), "empty text"),
        (_case(expect_roles=["Research"]), "unknown roles"),
    ],
)
def test_malformed_cases_are_refused(bad, message) -> None:
    with pytest.raises(ie.CaseError, match=message):
        ie.parse_cases([bad])


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(ie.CaseError, match="duplicate"):
        ie.parse_cases([_case(), _case()])


# --- scoring -------------------------------------------------------------------


def _classifier(*intents):
    queue = list(intents)

    async def classify(text, tracker, *, attachment_names=None, library_names=None):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return classify


def _evaluate(case_dict: dict, *intents, repeat: int = 1):
    cases = ie.parse_cases([case_dict])
    return asyncio.run(
        ie.evaluate(cases, repeat=repeat, concurrency=1, classify=_classifier(*intents), tracker_factory=object)
    )


def test_only_the_named_flags_are_checked() -> None:
    [result] = _evaluate(_case(), _intent(needs_web_research=True, reads_social=True))
    assert result.verdict == "PASS"


def test_a_wrong_flag_is_a_failure_that_says_which() -> None:
    [result] = _evaluate(_case(), _intent(needs_web_research=False))
    assert result.verdict == "FAIL"
    assert result.runs[0].mismatches == ["needs_web_research: expected True, got False"]
    assert ie.exit_code([result]) == 1


def test_a_regex_fallback_is_an_error_not_a_verdict() -> None:
    [result] = _evaluate(_case(), _intent(source="regex_fallback", needs_web_research=False))
    assert result.verdict == "ERROR" and result.failed == 0
    assert ie.exit_code([result]) == 2


def test_a_crashed_call_is_an_error() -> None:
    [result] = _evaluate(_case(), RuntimeError("model down"))
    assert result.verdict == "ERROR" and "model down" in result.runs[0].error


def test_mixed_runs_are_flaky() -> None:
    [result] = _evaluate(
        _case(), _intent(needs_web_research=True), _intent(needs_web_research=False), repeat=2
    )
    assert (result.verdict, result.passed) == ("FLAKY", 1)
    assert "FLAKY 1/2" in ie.render([result], repeat=2)


def test_wrong_agents_are_a_failure() -> None:
    pytest.importorskip("langgraph")
    case = _case(expect_roles=["Research Agent"])
    [result] = _evaluate(case, _intent(needs_web_research=True, reads_github=True))
    assert result.verdict == "FAIL"
    assert result.runs[0].mismatches[0].startswith("agents: expected ['Research Agent']")


def test_selection_by_id_and_tag() -> None:
    cases = ie.load_cases()
    assert [c.id for c in ie.select(cases, ids=["github.popular"], tags=[])] == ["github.popular"]
    assert all("jira" in c.tags for c in ie.select(cases, ids=[], tags=["jira"]))


# --- the routing fix the eval found --------------------------------------------


def test_a_github_link_alone_does_not_also_schedule_research() -> None:
    text = "https://github.com/astral-sh/uv ne işe yarıyor"
    base = capabilities.workforce_structural_requirements(text)
    assert capabilities.WEB_FETCH in base
    granted = capabilities.apply_artifact_intent(base, wants_artifact=False, text=text, reads_github=True)
    assert capabilities.GITHUB_READ in granted
    assert not granted & {capabilities.WEB_FETCH, capabilities.WEB_SEARCH}


def test_other_links_or_a_web_ask_keep_research() -> None:
    mixed = "https://github.com/a/b ve https://example.com/yazi karşılaştır"
    granted = capabilities.apply_artifact_intent(
        capabilities.workforce_structural_requirements(mixed), wants_artifact=False, text=mixed, reads_github=True
    )
    assert capabilities.WEB_FETCH in granted

    link = "https://github.com/a/b hakkında blog yazılarını da bul"
    granted = capabilities.apply_artifact_intent(
        capabilities.workforce_structural_requirements(link),
        wants_artifact=False,
        text=link,
        reads_github=True,
        needs_web_research=True,
    )
    assert capabilities.WEB_SEARCH in granted


def test_without_github_reading_the_link_goes_to_research() -> None:
    text = "https://github.com/astral-sh/uv ne işe yarıyor"
    granted = capabilities.apply_artifact_intent(
        capabilities.workforce_structural_requirements(text), wants_artifact=False, text=text
    )
    assert capabilities.WEB_FETCH in granted
