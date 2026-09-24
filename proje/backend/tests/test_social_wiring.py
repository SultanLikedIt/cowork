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


def test_the_role_is_scheduled_and_never_answers_alone() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.SOCIAL_READ}),
        fast_path_role=None,
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


def test_an_unaddressed_email_request_is_remembered_not_silently_dropped() -> None:
    """Live failure: "uygar017@outlook adresine mail olarak gonder" produced
    an 11-page PDF, sent nothing, and said nothing. Refusing an address the
    user did not type is correct; doing it silently is not."""
    claimed = artifact_intent.ArtifactIntent(
        wants_artifact=True,
        artifact_type="pdf",
        source="llm",
        sends_email=True,
        email_to=(),
    ).normalized()
    assert claimed.sends_email is False
    assert claimed.email_unaddressed is True


def test_a_real_address_does_not_raise_the_flag() -> None:
    intent = artifact_intent.ArtifactIntent(
        wants_artifact=True,
        artifact_type="pdf",
        source="llm",
        sends_email=True,
        email_to=("a@b.com",),
    ).normalized()
    assert intent.sends_email is True
    assert intent.email_unaddressed is False


def test_the_malformed_address_from_the_live_run_finds_no_recipient() -> None:
    text = "raporu uygar017@outlook adresine mail olarak gonder"
    assert artifact_intent.addresses_in_text(text) == []
    assert artifact_intent._verified_recipients(["uygar017@outlook"], text) == []
