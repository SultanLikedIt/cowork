"""The seams between the social subsystem and the rest of the app.

Each piece is tested where it lives; what this file asserts is that they are
actually connected -- the failure mode where every unit passes and the
feature never runs, because one kwarg was never threaded through.
"""

from __future__ import annotations

import pytest

import artifact_intent
import capabilities


def test_the_classifier_can_report_social_intent() -> None:
    result = artifact_intent.ArtifactIntentResult(reads_social=True)
    assert result.reads_social is True


def test_the_prompt_actually_asks_for_the_field() -> None:
    """A field the model is never told about is a field always false.

    Two of the edits that added this were silent no-ops the first time, so
    the prompt is asserted rather than assumed.
    """
    prompt = artifact_intent.INTENT_SYSTEM_PROMPT
    assert '"reads_social": true | false' in prompt
    assert "reads_social examples" in prompt
    # The distinction that matters: a question mentioning Reddit is not the
    # same as wanting Reddit read.
    assert "reddit ne zaman kuruldu" in prompt


def test_intent_survives_persistence_round_trip() -> None:
    """The job record is the audit answer to "why did this read Reddit"."""
    restored = artifact_intent.from_persisted(
        {
            "artifact_intent_source": "llm",
            "wants_artifact": False,
            "artifact_type": "none",
            "reads_social": True,
        }
    )
    assert restored is not None and restored.reads_social is True


def test_a_failed_classification_does_not_default_to_reading() -> None:
    """Degrading toward spending requests against a rate-limited account is
    not a tradeoff a fallback gets to make."""
    fallback = artifact_intent.fallback_from_regex("reddit'te ne diyorlar")
    assert fallback.reads_social is False


def test_intent_grants_the_capability() -> None:
    granted = capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x", reads_social=True
    )
    assert capabilities.SOCIAL_READ in granted
    assert capabilities.SOCIAL_READ not in capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x"
    )


def test_the_capability_has_guidance_that_frames_reddit_as_opinion() -> None:
    spec = capabilities.REGISTRY[capabilities.SOCIAL_READ]
    text = spec.guidance.lower()
    assert "never as instructions" in text or "never instructions" in text
    # Reddit is opinion, not fact; a report that states a comment as a
    # verified claim is wrong in a way the user cannot see.
    assert "opinion" in text


def test_an_unusable_platform_is_reported_through_the_missing_channel() -> None:
    """Reported like every other unavailable capability, rather than being
    discovered mid-run inside the phase.

    The check lives inside resolve_tools alongside the other preflights, so
    this asserts on that seam rather than calling a function that does not
    exist on its own.
    """
    import inspect

    source = inspect.getsource(capabilities.resolve_tools)
    assert "SOCIAL_READ in resolved" in source
    assert "_social_health.check" in source
    assert "missing.append((SOCIAL_READ" in source


def test_the_camel_path_drops_the_kwarg_instead_of_crashing() -> None:
    """camel_service's entrypoint has no such parameter; forwarding it would
    turn an executor switch into a TypeError."""
    import inspect

    from execution import adapter

    source = inspect.getsource(adapter.run_workforce_in_thread)
    assert 'kwargs.pop("reads_social", None)' in source


def test_the_role_is_scheduled_and_never_answers_alone() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.SOCIAL_READ}),
        wants_artifact=False,
    )
    assert workforce.ROLE_SOCIAL in roles
    # It gathers; something else has to write the answer.
    assert roles[-1] == workforce.ROLE_GENERAL


def test_the_role_has_a_worker_id_for_the_ui() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    assert workforce.WORKER_IDS[workforce.ROLE_SOCIAL] == "lg-social"


def test_the_status_endpoint_exists() -> None:
    import social_api

    paths = [route.path for route in social_api.build_router().routes]
    assert "/integrations/social/status" in paths
