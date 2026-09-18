"""Email delivery of scheduled runs: when a run is queued for email, retries,
and the delivery setting on the API."""

from __future__ import annotations

import pytest

from scheduling import delivery, store
from tests_support.scheduling_harness import NOW, Harness, make_task, temp_db  # noqa: F401

RECIPIENTS = {"to": ["ali@example.com"], "cc": [], "on": "success", "attach_files": True}


def _only_run(task_id: str) -> dict:
    [run] = store.list_runs(task_id)
    return run


def test_successful_run_is_emailed_on_the_next_tick(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)
    h.tick()
    h.finish("job-1", "done")
    h.tick()

    run = _only_run(task_id)
    assert h.delivered == [(task_id, run["run_id"])]
    assert run["delivery_status"] == "sent"
    assert run["delivery_attempts"] == 1
    assert run["delivered_at"] == NOW


def test_task_without_delivery_sends_nothing(temp_db) -> None:
    h = Harness()
    task_id = make_task()
    h.tick()
    h.finish("job-1", "done")
    h.tick()
    assert h.delivered == []
    assert _only_run(task_id)["delivery_status"] is None


def test_failed_run_only_emailed_when_condition_is_always(temp_db) -> None:
    h = Harness()
    quiet = make_task(delivery=RECIPIENTS)
    loud = make_task(delivery={**RECIPIENTS, "on": "always"})
    h.tick()
    h.finish("job-1", "failed")
    h.finish("job-2", "failed")
    h.tick()

    assert _only_run(quiet)["delivery_status"] is None
    assert _only_run(loud)["delivery_status"] == "sent"


def test_cancelled_run_is_never_emailed(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery={**RECIPIENTS, "on": "always"})
    h.tick()
    h.finish("job-1", "cancelled")
    h.tick()
    assert _only_run(task_id)["delivery_status"] is None


def test_change_condition_on_a_watch(temp_db) -> None:
    h = Harness()
    task_id = make_task(
        delivery={**RECIPIENTS, "on": "change"},
        watch={"strategies": ["research_diff"], "urls": [], "criteria": ""},
        last_result_summary="Eski rapor",
    )
    h.diff_result = delivery_diff(False)
    h.tick()
    h.finish("job-1", "done", "Aynı rapor")
    h.tick()
    assert _only_run(task_id)["delivery_status"] is None

    h.now += 7 * 86400
    store.update_task(task_id, {"next_run_at": h.now - 5})
    h.diff_result = delivery_diff(True)
    h.tick()
    h.finish("job-2", "done", "Yeni yönetmelik yayımlandı")
    h.tick()
    latest = store.list_runs(task_id)[0]
    assert latest["job_id"] == "job-2"
    assert latest["delivery_status"] == "sent"


def delivery_diff(changed: bool):
    from scheduling import watch

    return watch.DiffResult(material_change=changed, summary="Yeni düzenleme" if changed else "Değişiklik yok")


def test_unreachable_outlook_is_retried_with_backoff(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)
    h.delivery_outcomes = [delivery.DeliveryOutcome(False, "bridge down")]
    h.tick()
    h.finish("job-1", "done")
    h.tick()

    run = _only_run(task_id)
    assert run["delivery_status"] == "pending"
    assert run["delivery_attempts"] == 1
    assert run["delivery_error"] == "bridge down"
    assert run["delivery_next_at"] == NOW + delivery.retry_delay(1)

    h.tick()  # not due yet
    assert len(h.delivered) == 1

    h.now = run["delivery_next_at"]
    h.tick()
    run = _only_run(task_id)
    assert run["delivery_status"] == "sent"
    assert run["delivery_attempts"] == 2


def test_non_retryable_failure_stops_at_once(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)
    h.delivery_outcomes = [delivery.DeliveryOutcome(False, "invalid token", retryable=False)]
    h.tick()
    h.finish("job-1", "done")
    h.tick()
    run = _only_run(task_id)
    assert run["delivery_status"] == "failed"
    assert run["delivery_next_at"] is None


def test_gives_up_after_max_attempts(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_DELIVERY_MAX_ATTEMPTS", "2")
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)
    h.delivery_outcomes = [delivery.DeliveryOutcome(False, "down"), delivery.DeliveryOutcome(False, "down")]
    h.tick()
    h.finish("job-1", "done")
    h.tick()
    h.now += 3600
    h.tick()
    run = _only_run(task_id)
    assert run["delivery_status"] == "failed"
    assert run["delivery_attempts"] == 2


def test_turning_email_off_fails_pending_delivery(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)
    h.delivery_outcomes = [delivery.DeliveryOutcome(False, "down")]
    h.tick()
    h.finish("job-1", "done")
    h.tick()
    store.update_task(task_id, {"delivery_json": None})
    h.now += 3600
    h.tick()
    assert _only_run(task_id)["delivery_status"] == "failed"
    assert len(h.delivered) == 1


def test_mailer_exception_counts_as_failed_attempt(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)

    async def boom(task, run):
        raise RuntimeError("COM exploded")

    h.scheduler._deliver = boom
    h.tick()
    h.finish("job-1", "done")
    h.tick()
    run = _only_run(task_id)
    assert run["delivery_status"] == "pending"
    assert "COM exploded" in run["delivery_error"]


def test_retry_delay_grows_and_caps() -> None:
    delays = [delivery.retry_delay(n) for n in range(1, 9)]
    assert delays == sorted(delays)
    assert delays[-1] == 3600


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"to": []}, "at least one"),
        ({"to": ["not-an-address"]}, "Not an email address"),
        ({"to": ["a@example.com"], "on": "sometimes"}, "Unknown delivery condition"),
    ],
)
def test_normalize_rejects(raw, message) -> None:
    with pytest.raises(ValueError, match=message):
        delivery.normalize(raw)


def test_normalize_dedupes_and_splits() -> None:
    value = delivery.normalize({"to": ["a@example.com; B@example.com", "b@example.com"], "cc": []})
    assert value == {"to": ["a@example.com", "B@example.com"], "cc": [], "on": "success", "attach_files": True}
    assert delivery.normalize(None) is None


def test_prompt_derived_delivery_is_recorded(temp_db) -> None:
    """A task with no Email setting, whose prompt names a recipient."""
    h = Harness()
    task_id = make_task(prompt="Haftalık özet hazırla ve ali@sirket.com adresine gönder")
    h.scheduler._resolve_delivery = lambda task, run: {
        "to": ["ali@sirket.com"],
        "cc": [],
        "on": "success",
        "attach_files": True,
        "source": "prompt",
    }
    h.tick()
    h.finish("job-1", "done")
    h.tick()

    run = _only_run(task_id)
    assert run["delivery_status"] == "sent"
    assert run["delivery_to"] == "ali@sirket.com"
    assert run["delivery_source"] == "prompt"


def test_task_setting_is_recorded_as_such(temp_db) -> None:
    h = Harness()
    task_id = make_task(delivery=RECIPIENTS)
    h.tick()
    h.finish("job-1", "done")
    h.tick()
    run = _only_run(task_id)
    assert (run["delivery_to"], run["delivery_source"]) == ("ali@example.com", "task")
