"""Task chains: conditions, queueing, input hand-off and chain guards."""

from __future__ import annotations

import asyncio

import db
import pytest
from scheduling import chains, store, watch
from tests_support.scheduling_harness import NOW, Harness, make_task, temp_db  # noqa: F401


def _follower(upstream_id: str, **overrides) -> str:
    fields = {
        "name": "Takip görevi",
        "prompt": "Önceki raporu özetle",
        "trigger_kind": "after_task",
        "cron_expr": None,
        "next_run_at": None,
        "upstream_task_id": upstream_id,
        "condition": {"on": overrides.pop("on", "success")},
    }
    fields.update(overrides)
    return make_task(**fields)


def _runs(task_id: str) -> list[dict]:
    return store.list_runs(task_id)


# -- conditions -------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "status", "change", "watch_task", "has_result", "expected"),
    [
        ("success", "done", None, False, True, True),
        ("success", "partial", None, False, True, True),
        ("success", "failed", None, False, False, False),
        ("success", "no_change", False, True, True, True),  # research_diff wrote a report
        ("success", "no_change", False, True, False, False),  # url_hash skipped the job
        ("change", "done", True, True, True, True),
        ("change", "done", False, True, True, False),
        ("change", "done", None, False, True, True),  # not a watch: every report is new
        ("change", "failed", None, False, False, False),
        ("always", "failed", None, False, False, True),
        ("always", "error", None, False, False, True),
        ("always", "skipped_overlap", None, False, False, False),
        ("always", "missed", None, False, False, False),
    ],
)
def test_condition_table(condition, status, change, watch_task, has_result, expected) -> None:
    assert (
        chains.condition_met(
            condition, status, change, upstream_is_watch=watch_task, has_result=has_result
        )
        is expected
    )


# -- queueing and hand-off ----------------------------------------------------


def test_success_chain_passes_report_and_starts_follower(temp_db) -> None:
    h = Harness()
    upstream = make_task(name="Sektör taraması")
    follower = _follower(upstream)

    h.tick()
    assert [item["task"]["task_id"] for item in h.submitted] == [upstream]

    h.finish("job-1", "done", "Çelik fiyatları %4 arttı.\n\n**Kaynaklar**\n- https://example.com")
    h.tick()

    [started] = h.submitted_for(follower)
    assert "Sektör taraması" in started["context_text"]
    assert "Çelik fiyatları %4 arttı." in started["context_text"]
    assert "example.com" not in started["context_text"]
    [run] = _runs(follower)
    assert run["status"] == "running"
    assert run["trigger"] == "chained"
    assert run["parent_run_id"] == _runs(upstream)[0]["run_id"]
    assert run["input"]["upstream_name"] == "Sektör taraması"


def test_failed_upstream_skips_success_follower(temp_db) -> None:
    h = Harness()
    upstream = make_task()
    follower = _follower(upstream)
    h.tick()
    h.finish("job-1", "failed", "")
    h.tick()

    assert h.submitted_for(follower) == []
    [run] = _runs(follower)
    assert run["status"] == "skipped_condition"
    assert "did not succeed" in run["error"]
    assert run["seen_at"] is not None


def test_always_follower_runs_after_failure(temp_db) -> None:
    h = Harness()
    upstream = make_task()
    follower = _follower(upstream, on="always")
    h.tick()
    h.finish("job-1", "failed", "")
    h.tick()
    assert len(h.submitted_for(follower)) == 1
    assert h.submitted_for(follower)[0]["context_text"] is None


def test_change_condition_with_url_watch(temp_db) -> None:
    h = Harness()
    url = "https://gazete.example/"
    h.page_texts[url] = "sayı 1"
    upstream = make_task(cron_expr="*/5 * * * *", watch={"strategies": ["url_hash"], "urls": [url], "criteria": ""})
    follower = _follower(upstream, on="change")

    h.tick()  # baseline: job runs, but a first look is not a change
    h.finish("job-1")
    h.tick()
    assert h.submitted_for(follower) == []
    assert _runs(follower)[0]["status"] == "skipped_condition"

    h.now = store.get_task(upstream)["next_run_at"] + 1
    h.tick()  # unchanged page: no job at all, follower skipped again
    assert len(h.submitted) == 1
    assert [run["status"] for run in _runs(follower)] == ["skipped_condition", "skipped_condition"]
    assert "no change" in _runs(follower)[0]["error"]

    h.page_texts[url] = "sayı 2"
    h.now = store.get_task(upstream)["next_run_at"] + 1
    h.tick()
    h.finish("job-2", "done", "Yeni tebliğ yayımlandı.")
    h.tick()
    [started] = h.submitted_for(follower)
    assert "Yeni tebliğ yayımlandı." in started["context_text"]


def test_research_diff_change_condition(temp_db) -> None:
    h = Harness()
    upstream = make_task(watch={"strategies": ["research_diff"], "urls": [], "criteria": ""})
    store.update_task(upstream, {"last_result_summary": "Eski"})
    follower = _follower(upstream, on="change")
    h.diff_result = watch.DiffResult(material_change=True, summary="- yeni kanun")
    h.tick()
    h.finish("job-1", "done", "Yeni rapor")
    h.tick()
    assert len(h.submitted_for(follower)) == 1


def test_queued_runs_go_before_due_cron_tasks(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_CONCURRENT", "1")
    h = Harness()
    upstream = make_task()
    follower = _follower(upstream)
    h.tick()
    other = make_task(name="Başka görev")  # due now, competing for the one slot

    h.finish("job-1")
    h.tick()
    assert [item["task"]["task_id"] for item in h.submitted] == [upstream, follower]
    assert _runs(other) == []

    h.finish("job-2")
    h.tick()
    assert h.submitted[-1]["task"]["task_id"] == other


def test_queued_run_waits_while_its_task_is_running(temp_db) -> None:
    h = Harness()
    upstream = make_task(cron_expr="*/5 * * * *")
    follower = _follower(upstream)
    h.tick()
    h.finish("job-1")
    h.tick()  # follower job-2 running
    h.now = store.get_task(upstream)["next_run_at"] + 1
    h.tick()  # upstream job-3
    h.finish("job-3")
    h.tick()  # follower queued again, but job-2 is still running
    statuses = sorted(run["status"] for run in _runs(follower))
    assert statuses == ["queued", "running"]
    h.finish("job-2")
    h.tick()
    assert len(h.submitted_for(follower)) == 2


def test_upstream_run_triggers_each_follower_once(temp_db) -> None:
    h = Harness()
    upstream = make_task()
    follower = _follower(upstream)
    h.tick()
    h.finish("job-1")
    h.tick()
    task = store.get_task(upstream)
    [run] = _runs(upstream)
    # A second settle of the same upstream run (e.g. two reconciles).
    asyncio.run(h.scheduler._trigger_downstream(task, run, "done", "Rapor"))
    assert len(_runs(follower)) == 1


def test_fan_out(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_CONCURRENT", "3")
    h = Harness()
    upstream = make_task()
    first = _follower(upstream, name="PDF")
    second = _follower(upstream, name="Sunum")
    h.tick()
    h.finish("job-1")
    h.tick()
    assert len(h.submitted_for(first)) == 1
    assert len(h.submitted_for(second)) == 1


def test_files_are_handed_on_with_attached_files(temp_db) -> None:
    h = Harness()
    upstream = make_task()
    follower = _follower(
        upstream,
        attached_files=[{"file_id": "template-1", "filename": "sablon.docx"}],
    )
    no_files = _follower(upstream, name="Dosyasız", pass_upstream_files=False)
    h.job_files["job-1"] = (["up-a", "up-b"], ["Skipped big.pdf: too large"])
    h.tick()
    h.finish("job-1")
    h.tick()

    [started] = h.submitted_for(follower)
    assert started["file_ids"] == ["template-1", "up-a", "up-b"]
    assert _runs(follower)[0]["input"]["notes"] == ["Skipped big.pdf: too large"]
    assert h.submitted_for(no_files)[0]["file_ids"] == []
    assert h.collected == ["job-1"]  # collected once for both followers


def test_attached_files_on_a_plain_scheduled_task(temp_db) -> None:
    h = Harness()
    make_task(attached_files=[{"file_id": "f-1", "filename": "veri.xlsx"}])
    h.tick()
    assert h.submitted[0]["file_ids"] == ["f-1"]


def test_daily_chain_cap(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_CHAINED_RUNS_PER_DAY", "1")
    h = Harness()
    upstream = make_task(cron_expr="*/5 * * * *")
    follower = _follower(upstream)
    h.tick()
    h.finish("job-1")
    h.tick()  # follower job-2
    h.finish("job-2")
    h.now = store.get_task(upstream)["next_run_at"] + 1
    h.tick()  # upstream job-3
    h.finish("job-3")
    h.tick()
    latest = _runs(follower)[0]
    assert latest["status"] == "skipped_condition"
    assert "limit 1" in latest["error"]


def test_disabled_follower_is_not_triggered(temp_db) -> None:
    h = Harness()
    upstream = make_task()
    follower = _follower(upstream, enabled=False)
    h.tick()
    h.finish("job-1")
    h.tick()
    assert _runs(follower) == []


def test_deleting_upstream_pauses_follower(temp_db) -> None:
    h = Harness()
    upstream = make_task(next_run_at=NOW + 3600)
    follower = _follower(upstream)
    store.delete_task(upstream)
    h.tick()
    task = store.get_task(follower)
    assert task["enabled"] is False
    assert "deleted" in task["paused_reason"]


def test_run_now_on_follower_uses_latest_upstream_result(temp_db) -> None:
    h = Harness()
    upstream = make_task()
    follower = _follower(upstream, enabled=False)  # not triggered by the chain
    h.tick()
    h.finish("job-1", "done", "Son başarılı rapor")
    h.tick()

    h.run_now(follower)
    [started] = h.submitted_for(follower)
    assert "Son başarılı rapor" in started["context_text"]
    assert _runs(follower)[0]["trigger"] == "manual"


def test_run_now_on_follower_without_upstream_result(temp_db) -> None:
    h = Harness()
    upstream = make_task(next_run_at=NOW + 3600)
    follower = _follower(upstream)
    run = h.run_now(follower)
    assert run["status"] == "running"
    assert h.submitted_for(follower)[0]["context_text"] is None


# -- link validation ------------------------------------------------------------


def test_validate_rejects_cycles(temp_db) -> None:
    a = make_task(name="A")
    b = _follower(a, name="B")
    c = _follower(b, name="C")
    with pytest.raises(chains.ChainError, match="loop"):
        chains.validate_upstream(a, c, "ws-1")
    with pytest.raises(chains.ChainError, match="loop"):
        chains.validate_upstream(a, a, "ws-1")


def test_validate_rejects_too_deep(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_CHAIN_DEPTH", "2")
    a = make_task(name="A")
    b = _follower(a, name="B")
    c = _follower(b, name="C")
    with pytest.raises(chains.ChainError, match="at most 2"):
        chains.validate_upstream(None, c, "ws-1")
    # Moving a task that already has a follower counts that follower too.
    d = make_task(name="D")
    _follower(d, name="E")
    with pytest.raises(chains.ChainError, match="at most 2"):
        chains.validate_upstream(d, b, "ws-1")


def test_validate_rejects_other_workspace(temp_db) -> None:
    db.create_workspace("ws-2", "Other", NOW)
    other = make_task(workspace_id="ws-2")
    with pytest.raises(chains.ChainError, match="same workspace"):
        chains.validate_upstream(None, other, "ws-1")
