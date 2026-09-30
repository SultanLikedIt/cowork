"""The seams between the Confluence subsystem and the rest of the app."""

from __future__ import annotations

import inspect

import pytest

import artifact_intent
import capabilities


def test_the_classifier_can_report_confluence_intent() -> None:
    assert artifact_intent.ArtifactIntentResult(reads_confluence=True).reads_confluence is True


def test_the_prompt_actually_asks_for_the_field() -> None:
    prompt = artifact_intent.INTENT_SYSTEM_PROMPT
    assert '"reads_confluence": true | false' in prompt
    assert "reads_confluence examples" in prompt
    assert "confluence nedir" in prompt


def test_intent_survives_persistence_round_trip() -> None:
    restored = artifact_intent.from_persisted(
        {
            "artifact_intent_source": "llm",
            "wants_artifact": False,
            "artifact_type": "none",
            "reads_confluence": True,
        }
    )
    assert restored is not None and restored.reads_confluence is True


def test_a_failed_classification_does_not_default_to_reading(monkeypatch) -> None:
    monkeypatch.delenv("CONFLUENCE_ENABLED", raising=False)
    assert artifact_intent.fallback_from_regex("izin politikası ne diyor").reads_confluence is False


def test_a_link_to_this_confluence_counts_even_in_the_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CONFLUENCE_ENABLED", "true")
    monkeypatch.setenv("CONFLUENCE_SPACES", "ENG")
    monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "me@acme.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    intent = artifact_intent.fallback_from_regex(
        "https://acme.atlassian.net/wiki/spaces/ENG/pages/1/X özetle"
    )
    assert intent.reads_confluence is True


def test_intent_grants_the_capability() -> None:
    granted = capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x", reads_confluence=True
    )
    assert capabilities.CONFLUENCE_READ in granted
    assert capabilities.CONFLUENCE_READ not in capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x"
    )


def test_the_capability_guidance_frames_page_text_as_evidence() -> None:
    text = capabilities.REGISTRY[capabilities.CONFLUENCE_READ].guidance.lower()
    assert "never instructions" in text
    assert "never invent" in text


def test_missing_configuration_is_reported_through_the_missing_channel() -> None:
    source = inspect.getsource(capabilities.resolve_tools)
    assert "CONFLUENCE_READ in resolved" in source


def test_routing_and_the_job_pass_confluence_through() -> None:
    import main

    assert "confluence_client.is_configured()" in inspect.getsource(main._routing_for_workforce)
    assert "reads_confluence=" in inspect.getsource(main._run_job)


def test_the_role_is_scheduled_and_never_answers_alone() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.CONFLUENCE_READ}),
        fast_path_role=None,
        wants_artifact=False,
    )
    assert roles == [workforce.ROLE_CONFLUENCE, workforce.ROLE_GENERAL]


def test_the_status_endpoint_is_mounted() -> None:
    import main

    paths = {route.path for route in main.app.routes}
    assert "/integrations/confluence/status" in paths
    assert "/integrations/confluence/recheck" in paths
