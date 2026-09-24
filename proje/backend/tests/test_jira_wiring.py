"""The seams between the Jira subsystem and the rest of the app.

Each piece is tested where it lives; this file asserts they are connected --
the failure where every unit passes and the feature never runs because one
kwarg was never threaded through.
"""

from __future__ import annotations

import inspect

import pytest

import artifact_intent
import capabilities


def test_the_classifier_can_report_jira_intent() -> None:
    assert artifact_intent.ArtifactIntentResult(reads_jira=True).reads_jira is True


def test_the_prompt_actually_asks_for_the_field() -> None:
    prompt = artifact_intent.INTENT_SYSTEM_PROMPT
    assert '"reads_jira": true | false' in prompt
    assert "reads_jira examples" in prompt
    # The distinctions that matter: asking ABOUT Jira, or writing FOR it.
    assert "jira nedir" in prompt
    assert "bug raporu yaz" in prompt


def test_intent_survives_persistence_round_trip() -> None:
    restored = artifact_intent.from_persisted(
        {
            "artifact_intent_source": "llm",
            "wants_artifact": False,
            "artifact_type": "none",
            "reads_jira": True,
        }
    )
    assert restored is not None and restored.reads_jira is True


def test_a_failed_classification_does_not_default_to_reading() -> None:
    assert artifact_intent.fallback_from_regex("PROJ-1 ne durumda").reads_jira is False


def test_intent_grants_the_capability() -> None:
    granted = capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x", reads_jira=True
    )
    assert capabilities.JIRA_READ in granted
    assert capabilities.JIRA_READ not in capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x"
    )


def test_the_capability_guidance_frames_issue_text_as_evidence() -> None:
    text = capabilities.REGISTRY[capabilities.JIRA_READ].guidance.lower()
    assert "never instructions" in text
    assert "never invent" in text


def test_an_unconfigured_jira_is_reported_through_the_missing_channel() -> None:
    source = inspect.getsource(capabilities.resolve_tools)
    assert "JIRA_READ in resolved" in source
    assert "missing.append((JIRA_READ" in source


def test_routing_grants_jira_on_configuration_not_on_reachability() -> None:
    import main

    source = inspect.getsource(main._routing_for_workforce)
    assert "jira_client.is_configured()" in source
    assert "reads_jira=reads_jira" in source
    assert "reads_jira" in main.UsageTelemetry.model_fields


def test_the_role_is_scheduled_and_never_answers_alone() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.JIRA_READ}),
        fast_path_role=None,
        wants_artifact=False,
    )
    assert roles == [workforce.ROLE_JIRA, workforce.ROLE_GENERAL]


def test_the_document_writer_answers_when_a_file_is_wanted() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.ARTIFACTS}),
        fast_path_role=None,
        wants_artifact=True,
        reads_jira=True,
    )
    assert roles == [workforce.ROLE_JIRA, workforce.ROLE_DOCUMENT]


def test_gatherers_still_get_an_answerer_in_combination() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset(),
        fast_path_role=None,
        wants_artifact=False,
        reads_email=True,
        reads_jira=True,
    )
    assert roles == [workforce.ROLE_MAIL, workforce.ROLE_JIRA, workforce.ROLE_GENERAL]


def test_the_role_has_a_worker_id_for_the_ui() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    assert workforce.WORKER_IDS[workforce.ROLE_JIRA] == "lg-jira"


def test_the_roster_lists_jira_exactly_when_routing_could_schedule_it(monkeypatch) -> None:
    """Same promise as the Mail Agent's roster entry: the panel shows who can
    actually work on a request, and routing grants JIRA_READ on configuration."""
    import main

    monkeypatch.setenv("JIRA_ENABLED", "true")
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "me@acme.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    assert "Jira Agent" in [w.name for w in main._worker_pool().workers]

    monkeypatch.setenv("JIRA_ENABLED", "false")
    names = [w.name for w in main._worker_pool().workers]
    assert "Jira Agent" not in names
    assert "Research Agent" in names


def test_the_roster_lists_the_social_agent_on_the_routing_condition(monkeypatch) -> None:
    import main
    from social import health as social_health

    def report(ok: bool):
        return lambda *a, **k: social_health.HealthReport(
            platform="reddit", installed=ok, authenticated=ok
        )

    monkeypatch.setattr(main.social_health, "check", report(True))
    assert "Social Agent" in [w.name for w in main._worker_pool().workers]
    monkeypatch.setattr(main.social_health, "check", report(False))
    assert "Social Agent" not in [w.name for w in main._worker_pool().workers]


def test_the_status_endpoint_exists() -> None:
    import jira_api

    paths = [route.path for route in jira_api.build_router().routes]
    assert "/integrations/jira/status" in paths
    assert "/integrations/jira/recheck" in paths
