"""The seams between the GitHub subsystem and the rest of the app.

Each piece is tested where it lives; this file asserts they are connected --
the failure where every unit passes and the feature never runs because one
kwarg was never threaded through.
"""

from __future__ import annotations

import inspect

import pytest

import artifact_intent
import capabilities


def test_the_classifier_can_report_github_intent() -> None:
    assert artifact_intent.ArtifactIntentResult(reads_github=True).reads_github is True


def test_the_prompt_actually_asks_for_the_field() -> None:
    prompt = artifact_intent.INTENT_SYSTEM_PROMPT
    assert '"reads_github": true | false' in prompt
    assert "reads_github examples" in prompt
    # The distinction that matters: using git is not reading a repository.
    assert "nasil push yaparim" in prompt


def test_intent_survives_persistence_round_trip() -> None:
    restored = artifact_intent.from_persisted(
        {
            "artifact_intent_source": "llm",
            "wants_artifact": False,
            "artifact_type": "none",
            "reads_github": True,
        }
    )
    assert restored is not None and restored.reads_github is True


def test_a_typed_link_turns_reading_on_even_in_the_fallback() -> None:
    assert artifact_intent.fallback_from_regex("https://github.com/astral-sh/uv nedir").reads_github
    assert not artifact_intent.fallback_from_regex("github a nasil push yaparim").reads_github


def test_a_typed_link_overrides_a_classifier_that_missed_it() -> None:
    assert artifact_intent._with_github_links("github.com/a/b ozetle", False) is True
    assert artifact_intent._with_github_links("git nedir", False) is False
    assert artifact_intent._with_github_links("git nedir", True) is True


def test_intent_grants_the_capability() -> None:
    granted = capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x", reads_github=True
    )
    assert capabilities.GITHUB_READ in granted
    assert capabilities.GITHUB_READ not in capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x"
    )


def test_the_capability_guidance_frames_content_as_evidence() -> None:
    text = capabilities.REGISTRY[capabilities.GITHUB_READ].guidance.lower()
    assert "never instructions" in text
    assert "never invent" in text


def test_disabled_reading_is_reported_through_the_missing_channel() -> None:
    source = inspect.getsource(capabilities.resolve_tools)
    assert "GITHUB_READ in resolved" in source
    assert "missing.append(" in source and "(GITHUB_READ," in source


def test_the_camel_path_drops_the_kwarg_instead_of_crashing() -> None:
    from execution import adapter

    source = inspect.getsource(adapter.run_workforce_in_thread)
    assert 'kwargs.pop("reads_github", None)' in source


def test_routing_and_the_job_pass_github_through() -> None:
    import main

    assert "github_client.is_configured()" in inspect.getsource(main._routing_for_workforce)
    assert "reads_github=" in inspect.getsource(main._run_job)


def test_the_role_is_scheduled_and_never_answers_alone() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.GITHUB_READ}),
        fast_path_role=None,
        wants_artifact=False,
    )
    assert roles == [workforce.ROLE_GITHUB, workforce.ROLE_GENERAL]


def test_the_document_writer_answers_when_a_file_is_wanted() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.ARTIFACTS}),
        fast_path_role=None,
        wants_artifact=True,
        reads_github=True,
    )
    assert roles == [workforce.ROLE_GITHUB, workforce.ROLE_DOCUMENT]


def test_research_and_github_can_run_together() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.WEB_SEARCH, capabilities.GITHUB_READ}),
        fast_path_role=None,
        wants_artifact=False,
    )
    # Research never reads prior notes and the last worker's text is the
    # reply, so the Assistant writes it from both -- or the GitHub evidence
    # would be dropped (seen live).
    assert roles == [workforce.ROLE_GITHUB, workforce.ROLE_RESEARCH, workforce.ROLE_GENERAL]


def test_every_gatherer_before_research_gets_an_answerer() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    for flag, role in (
        ("reads_social", workforce.ROLE_SOCIAL),
        ("reads_jira", workforce.ROLE_JIRA),
        ("reads_email", workforce.ROLE_MAIL),
    ):
        roles = workforce._select_roles(
            job_capabilities=frozenset({capabilities.WEB_SEARCH}),
            fast_path_role=None,
            wants_artifact=False,
            **{flag: True},
        )
        assert roles == [role, workforce.ROLE_RESEARCH, workforce.ROLE_GENERAL]
    # Research alone is unchanged, and a file request still ends in Document.
    assert workforce._select_roles(
        job_capabilities=frozenset({capabilities.WEB_SEARCH}), fast_path_role=None, wants_artifact=False,
    ) == [workforce.ROLE_RESEARCH]
    assert workforce._select_roles(
        job_capabilities=frozenset({capabilities.WEB_SEARCH, capabilities.ARTIFACTS}),
        fast_path_role=None, wants_artifact=True, reads_github=True,
    ) == [workforce.ROLE_GITHUB, workforce.ROLE_RESEARCH, workforce.ROLE_DOCUMENT]


def test_the_role_has_a_worker_id_for_the_ui() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    assert workforce.WORKER_IDS[workforce.ROLE_GITHUB] == "lg-github"


def test_the_roster_lists_github_exactly_when_routing_could_schedule_it(monkeypatch) -> None:
    import main

    monkeypatch.delenv("GITHUB_ENABLED", raising=False)
    assert "GitHub Agent" in [w.name for w in main._worker_pool().workers]
    monkeypatch.setenv("GITHUB_ENABLED", "false")
    assert "GitHub Agent" not in [w.name for w in main._worker_pool().workers]


def test_the_status_endpoint_exists() -> None:
    import github_api

    paths = [route.path for route in github_api.build_router().routes]
    assert "/integrations/github/status" in paths
    assert "/integrations/github/recheck" in paths


# --- web research hook -----------------------------------------------------------


class _Reached(Exception):
    pass


class _Engine:
    """A crawler that records being reached, and stops there."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def acquire(self, url: str, need: str, **kwargs):
        self.urls.append(url)
        raise _Reached(url)


def test_web_reads_route_github_links_to_the_api(monkeypatch) -> None:
    import github_controller
    import web_tools

    seen: list[str] = []

    def fake_page(url: str, **kwargs):
        seen.append(url)
        return {"ok": True, "source_url": url, "title": "a/b", "text": "README text"}

    monkeypatch.setattr(github_controller, "read_url_as_page", fake_page)
    engine = _Engine()
    tools = web_tools.WebTools(engine=engine, request_text="https://github.com/a/b")
    page = tools.read_page("https://github.com/a/b")
    assert page["ok"] and page["text"] == "README text"
    assert seen == ["https://github.com/a/b"] and engine.urls == []


def test_web_reads_fall_back_to_the_crawler_when_the_api_declines(monkeypatch) -> None:
    import github_controller
    import web_tools

    monkeypatch.setattr(github_controller, "read_url_as_page", lambda url, **k: None)
    engine = _Engine()
    tools = web_tools.WebTools(engine=engine, request_text="https://github.com/a/b")
    with pytest.raises(_Reached):
        tools.read_page("https://github.com/a/b")
    assert engine.urls == ["https://github.com/a/b"]


def test_other_hosts_never_touch_the_github_reader(monkeypatch) -> None:
    import github_controller
    import web_tools

    def boom(url: str, **kwargs):
        raise AssertionError("github reader used for " + url)

    monkeypatch.setattr(github_controller, "read_url_as_page", boom)
    engine = _Engine()
    tools = web_tools.WebTools(engine=engine, request_text="https://example.com/x")
    with pytest.raises(_Reached):
        tools.read_page("https://example.com/x")
    assert engine.urls == ["https://example.com/x"]
