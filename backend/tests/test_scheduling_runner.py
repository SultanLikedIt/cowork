"""Scheduler loop: claiming, starting, settling, retries and watch tasks.

Runs against a real SQLite file with a fake clock, a fake submit (no model,
no web) and an in-memory job table standing in for main._JOBS.
"""

from __future__ import annotations

import asyncio

import db
from scheduling import store, watch
from tests_support.scheduling_harness import Harness, make_task as _task, temp_db  # noqa: F401

NOW = 1_790_000_000.0  # a fixed moment; only relative times matter here


def test_due_task_starts_a_job_and_advances(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)

    h.tick()

    assert len(h.submitted) == 1
    submitted = h.submitted[0]
    assert "sektör haberleri" in submitted["prompt"] and "{hafta}" not in submitted["prompt"]
    assert submitted["conversation_title"].startswith("📅 Haftalık bülten — ")
    assert submitted["context_text"] is None
    task = store.get_task(task_id)
    assert task["next_run_at"] > NOW
    assert task["last_job_id"] == "job-1"
    runs = store.list_runs(task_id)
    assert [run["status"] for run in runs] == ["running"]
    assert runs[0]["trigger"] == "schedule"


def test_not_due_and_disabled_tasks_are_left_alone(temp_db) -> None:
    h = Harness(NOW)
    _task(now=NOW, next_run_at=NOW + 3600)
    _task(now=NOW, enabled=False)
    h.tick()
    assert h.submitted == []


def test_settle_stores_summary_and_next_run_gets_context(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)
    h.tick()
    h.finish("job-1", "done", "Faizler sabit.\n\n**Kaynaklar**\n- https://example.com")
    h.tick()

    task = store.get_task(task_id)
    assert task["last_status"] == "done"
    assert task["last_result_summary"] == "Faizler sabit."
    assert store.list_runs(task_id)[0]["status"] == "done"

    h.now = task["next_run_at"] + 1
    h.tick()
    assert "ÖNCEKİ ÖZET:\nFaizler sabit." in h.submitted[1]["context_text"]


def test_overlapping_run_is_skipped(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)
    h.tick()
    run = asyncio.run(h.scheduler.run_now(task_id))
    assert run["status"] == "skipped_overlap"
    assert len(h.submitted) == 1


def test_run_far_past_catch_up_window_is_missed(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_CATCHUP_WINDOW_HOURS", "6")
    h = Harness(NOW)
    task_id = _task(now=NOW, next_run_at=NOW - 7 * 3600)
    h.tick()
    assert h.submitted == []
    assert store.list_runs(task_id)[0]["status"] == "missed"
    assert store.get_task(task_id)["next_run_at"] > NOW


def test_recent_miss_catches_up_once(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW, next_run_at=NOW - 3600)
    h.tick()
    assert len(h.submitted) == 1
    assert store.list_runs(task_id)[0]["trigger"] == "catch_up"


def test_concurrency_limit(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_CONCURRENT", "2")
    h = Harness(NOW)
    for _ in range(3):
        _task(now=NOW)
    h.tick()
    assert len(h.submitted) == 2
    h.finish("job-1")
    h.tick()
    assert len(h.submitted) == 3


def test_once_task_has_no_next_run(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW, trigger_kind="once", cron_expr=None, run_at=NOW - 5)
    h.tick()
    assert len(h.submitted) == 1
    assert store.get_task(task_id)["next_run_at"] is None


def test_repeated_failures_pause_the_task(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_CONSECUTIVE_FAILURES", "2")
    h = Harness(NOW)
    task_id = _task(now=NOW, cron_expr="*/5 * * * *")
    for attempt in range(2):
        task = store.get_task(task_id)
        h.now = max(h.now, task["next_run_at"]) + 1
        h.tick()
        h.finish(f"job-{attempt + 1}", "failed", "")
        h.tick()
    task = store.get_task(task_id)
    assert task["enabled"] is False
    assert task["consecutive_failures"] == 2
    assert "Paused automatically" in task["paused_reason"]


def test_success_resets_failures(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW, consecutive_failures=0)
    store.update_task(task_id, {"consecutive_failures": 1})
    h.tick()
    h.finish("job-1", "partial", "Kısmi rapor.")
    h.tick()
    assert store.get_task(task_id)["consecutive_failures"] == 0


def test_interrupted_run_is_retried_once(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)
    h.tick()
    h.finish("job-1", "interrupted", "")
    h.tick()
    assert len(h.submitted) == 2
    runs = {run["status"]: run for run in store.list_runs(task_id)}
    assert runs["running"]["trigger"] == "retry"
    assert runs["running"]["retry_of"] == runs["interrupted"]["run_id"]

    h.finish("job-2", "interrupted", "")
    h.tick()
    assert len(h.submitted) == 2
    assert store.get_task(task_id)["consecutive_failures"] == 1


def test_rejected_submission_is_an_error_run(temp_db) -> None:
    h = Harness(NOW)
    h.fail_submit = RuntimeError("unsupported")
    task_id = _task(now=NOW)
    h.tick()
    run = store.list_runs(task_id)[0]
    assert run["status"] == "error"
    assert "unsupported" in run["error"]
    assert store.get_task(task_id)["consecutive_failures"] == 1


def test_deleted_job_settles_as_error(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)
    h.tick()
    del h.jobs["job-1"]
    h.tick()
    assert store.list_runs(task_id)[0]["status"] == "error"


def test_research_diff_without_change_is_no_change_and_seen(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW, watch={"strategies": ["research_diff"], "urls": [], "criteria": "yeni yönetmelik"})
    store.update_task(task_id, {"last_result_summary": "Eski özet."})
    h.diff_result = watch.DiffResult(material_change=False, summary="Değişiklik yok.")
    h.tick()
    h.finish("job-1", "done", "Yeni rapor.")
    h.tick()

    assert h.diff_calls == [("Eski özet.", "Yeni rapor.", "yeni yönetmelik")]
    run = store.list_runs(task_id)[0]
    assert run["status"] == "no_change"
    assert run["change_detected"] is False
    assert run["seen_at"] is not None
    assert store.list_tasks("ws-1")[0]["unseen_runs"] == 0


def test_research_diff_with_change_is_unseen(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW, watch={"strategies": ["research_diff"], "urls": [], "criteria": ""})
    store.update_task(task_id, {"last_result_summary": "Eski özet."})
    h.diff_result = watch.DiffResult(material_change=True, summary="- Yeni tebliğ yayımlandı")
    h.tick()
    h.finish("job-1", "done", "Yeni rapor.")
    h.tick()
    run = store.list_runs(task_id)[0]
    assert run["status"] == "done"
    assert run["change_detected"] is True
    assert run["change_summary"] == "- Yeni tebliğ yayımlandı"
    assert store.list_tasks("ws-1")[0]["unseen_runs"] == 1


def test_url_hash_unchanged_skips_the_job(temp_db) -> None:
    h = Harness(NOW)
    url = "https://gazete.example/"
    task_id = _task(now=NOW, cron_expr="*/5 * * * *", watch={"strategies": ["url_hash"], "urls": [url], "criteria": ""})
    h.page_texts[url] = "Bugünkü  sayı"

    h.tick()  # baseline: runs the job and records the hash
    assert len(h.submitted) == 1
    assert store.get_task(task_id)["watch_state"] == {url: watch.content_hash("bugünkü sayı")}
    h.finish("job-1")
    h.tick()

    h.now = store.get_task(task_id)["next_run_at"] + 1
    h.tick()
    assert len(h.submitted) == 1
    assert store.list_runs(task_id)[0]["status"] == "no_change"

    h.page_texts[url] = "Yeni sayı: değişiklik"
    h.now = store.get_task(task_id)["next_run_at"] + 1
    h.tick()
    assert len(h.submitted) == 2
    assert store.list_runs(task_id)[0]["change_detected"] is True


def test_url_hash_all_pages_failing_is_an_error(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW, watch={"strategies": ["url_hash"], "urls": ["https://down.example/"], "criteria": ""})
    h.tick()  # page_texts is empty: fetch raises KeyError
    assert h.submitted == []
    run = store.list_runs(task_id)[0]
    assert run["status"] == "error"
    assert "None of the watched pages could be opened" in run["error"]


def test_mark_runs_seen(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)
    h.tick()
    h.finish("job-1")
    h.tick()
    assert store.list_tasks("ws-1")[0]["unseen_runs"] == 1
    store.mark_runs_seen(task_id, NOW)
    assert store.list_tasks("ws-1")[0]["unseen_runs"] == 0


def test_deleting_workspace_removes_tasks(temp_db) -> None:
    h = Harness(NOW)
    task_id = _task(now=NOW)
    h.tick()
    db.delete_workspace("ws-1")
    assert store.get_task(task_id) is None
    assert store.list_runs(task_id) == []
