"""/scheduled-tasks routes, mounted on a bare app with a fake scheduler."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
from scheduling import api, store

IST = "Europe/Istanbul"
NOW = datetime(2026, 9, 17, 10, 0, tzinfo=ZoneInfo(IST)).timestamp()  # Thursday


class FakeScheduler:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run_now(self, task_id: str) -> dict[str, Any]:
        self.ran.append(task_id)
        return {"run_id": "run-1", "task_id": task_id, "status": "running"}


@pytest.fixture()
def client(monkeypatch):
    original_dir, original_path = db.DATA_DIR, db.DB_PATH
    tmp = Path(tempfile.mkdtemp(prefix="scheduling_api_test_"))
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "app.db"
    db.run_migrations()
    db.create_workspace("ws-1", "Test", time.time())
    monkeypatch.setenv("SCHEDULING_ENABLED", "true")

    scheduler = FakeScheduler()
    parse_replies: list[Any] = []

    async def fake_complete(messages, **kwargs):
        return parse_replies.pop(0)

    uploads = {"file-1": {"filename": "sablon.docx"}}

    app = FastAPI()
    app.include_router(
        api.build_router(
            lambda: scheduler,
            clock=lambda: NOW,
            complete=fake_complete,
            describe_upload=lambda file_id: uploads.get(file_id),
        )
    )
    test_client = TestClient(app)
    test_client.scheduler = scheduler
    test_client.parse_replies = parse_replies
    try:
        yield test_client
    finally:
        db.DATA_DIR, db.DB_PATH = original_dir, original_path


def _create(client: TestClient, **overrides: Any):
    body = {
        "workspace_id": "ws-1",
        "name": "Haftalık bülten",
        "prompt": "{hafta} sektör haberlerini araştır, PDF hazırla",
        "cron_expr": "0 9 * * 1",
    }
    body.update(overrides)
    return client.post("/scheduled-tasks", json=body)


def test_create_and_list(client: TestClient) -> None:
    response = _create(client, skill_ids=["exec_brief", "not-a-skill"])
    assert response.status_code == 201, response.text
    task = response.json()
    expected_next = datetime(2026, 9, 21, 9, 0, tzinfo=ZoneInfo(IST)).timestamp()
    assert task["next_run_at"] == expected_next
    assert task["upcoming_runs"][0] == expected_next
    assert task["skill_ids"] == ["exec_brief"]
    assert task["timezone"] == IST
    assert "watch_state" not in task

    listed = client.get("/scheduled-tasks", params={"workspace_id": "ws-1"}).json()
    assert [t["task_id"] for t in listed["tasks"]] == [task["task_id"]]
    assert listed["unseen_runs"] == 0
    assert "tarih" in listed["variables"]


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"cron_expr": "not cron"}, 422),
        ({"cron_expr": "*/10 * * * *"}, 422),  # under the 60 min minimum
        ({"cron_expr": "0 */6 * * *", "deep_research": True}, 422),  # deep: once a day
        ({"cron_expr": "0 9 31 2 *"}, 422),  # never fires
        ({"timezone": "Mars/Olympus"}, 422),
        ({"workspace_id": "missing"}, 404),
        ({"trigger_kind": "once", "cron_expr": None, "run_at": NOW - 3600}, 422),
        ({"watch": {"strategies": ["url_hash"], "urls": []}}, 422),
        ({"watch": {"strategies": ["url_hash"], "urls": ["ftp://x.example/"]}}, 422),
    ],
)
def test_create_validation(client: TestClient, overrides: dict[str, Any], status: int) -> None:
    assert _create(client, **overrides).status_code == status


def test_task_limit_per_workspace(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULE_MAX_TASKS_PER_WORKSPACE", "1")
    assert _create(client).status_code == 201
    assert _create(client).status_code == 422


def test_once_task(client: TestClient) -> None:
    run_at = NOW + 3600
    task = _create(client, trigger_kind="once", cron_expr=None, run_at=run_at).json()
    assert task["trigger_kind"] == "once"
    assert task["next_run_at"] == run_at
    assert task["upcoming_runs"] == [run_at]


def test_watch_task(client: TestClient) -> None:
    task = _create(
        client,
        watch={"strategies": ["url_hash", "research_diff"], "urls": [" https://gazete.example/ "], "criteria": "inşaat"},
    ).json()
    assert task["watch"] == {
        "strategies": ["url_hash", "research_diff"],
        "urls": ["https://gazete.example/"],
        "criteria": "inşaat",
    }


def test_patch_reschedules_and_clears_watch(client: TestClient) -> None:
    task = _create(client, watch={"strategies": ["research_diff"], "urls": [], "criteria": ""}).json()
    response = client.patch(
        f"/scheduled-tasks/{task['task_id']}",
        json={"cron_expr": "30 8 * * 1-5", "name": "Günlük", "watch": None},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["name"] == "Günlük"
    assert updated["watch"] is None
    assert updated["next_run_at"] == datetime(2026, 9, 18, 8, 30, tzinfo=ZoneInfo(IST)).timestamp()


def test_pause_and_resume(client: TestClient) -> None:
    task = _create(client).json()
    store.update_task(task["task_id"], {"consecutive_failures": 3, "paused_reason": "x"})
    paused = client.post(f"/scheduled-tasks/{task['task_id']}/pause").json()
    assert paused["enabled"] is False
    assert paused["next_run_at"] is None
    assert paused["upcoming_runs"] == []
    resumed = client.post(f"/scheduled-tasks/{task['task_id']}/resume").json()
    assert resumed["enabled"] is True
    assert resumed["next_run_at"] is not None
    assert resumed["consecutive_failures"] == 0
    assert resumed["paused_reason"] is None


def test_run_now_runs_and_delete(client: TestClient) -> None:
    task = _create(client).json()
    response = client.post(f"/scheduled-tasks/{task['task_id']}/run-now")
    assert response.status_code == 200
    assert client.scheduler.ran == [task["task_id"]]
    assert client.get(f"/scheduled-tasks/{task['task_id']}/runs").json() == {"runs": []}
    assert client.delete(f"/scheduled-tasks/{task['task_id']}").status_code == 200
    assert client.get(f"/scheduled-tasks/{task['task_id']}").status_code == 404


def test_run_now_when_disabled(client: TestClient, monkeypatch) -> None:
    task = _create(client).json()
    monkeypatch.setenv("SCHEDULING_ENABLED", "false")
    assert client.post(f"/scheduled-tasks/{task['task_id']}/run-now").status_code == 503


def test_preview(client: TestClient) -> None:
    body = client.post("/scheduled-tasks/preview", json={"cron_expr": "0 9 * * 1"}).json()
    assert len(body["upcoming_runs"]) == 5
    assert body["upcoming_runs"][1] - body["upcoming_runs"][0] == 7 * 86400


def test_parse_schedule_cron(client: TestClient) -> None:
    client.parse_replies.append(
        api.ParsedSchedule(trigger_kind="cron", cron_expr="0 9 * * 1", description="Every Monday at 09:00")
    )
    body = client.post("/scheduled-tasks/parse-schedule", json={"text": "her pazartesi sabah"}).json()
    assert body["cron_expr"] == "0 9 * * 1"
    assert body["description"] == "Every Monday at 09:00"
    assert len(body["upcoming_runs"]) == 5


def test_parse_schedule_once(client: TestClient) -> None:
    client.parse_replies.append(api.ParsedSchedule(trigger_kind="once", run_at_local="2026-09-18T08:30"))
    body = client.post("/scheduled-tasks/parse-schedule", json={"text": "yarın 08:30"}).json()
    assert body["trigger_kind"] == "once"
    assert body["run_at"] == datetime(2026, 9, 18, 8, 30, tzinfo=ZoneInfo(IST)).timestamp()


def test_parse_schedule_unclear(client: TestClient) -> None:
    client.parse_replies.append(api.ParsedSchedule(trigger_kind="unclear"))
    assert client.post("/scheduled-tasks/parse-schedule", json={"text": "merhaba"}).status_code == 422


# -- chains --------------------------------------------------------------------


def _follow(client: TestClient, upstream_id: str, **overrides: Any):
    body = {
        "trigger_kind": "after_task",
        "cron_expr": None,
        "upstream_task_id": upstream_id,
        "name": "Takip",
        "prompt": "Önceki raporu özetle",
    }
    body.update(overrides)
    return _create(client, **body)


def test_create_follower(client: TestClient) -> None:
    upstream = _create(client, name="İzleme").json()
    response = _follow(client, upstream["task_id"], condition="change", pass_upstream_files=False)
    assert response.status_code == 201, response.text
    follower = response.json()
    assert follower["trigger_kind"] == "after_task"
    assert follower["next_run_at"] is None
    assert follower["upcoming_runs"] == []
    assert follower["condition"] == "change"
    assert follower["upstream_name"] == "İzleme"
    assert follower["pass_upstream_files"] is False

    listed = {t["task_id"]: t for t in client.get("/scheduled-tasks").json()["tasks"]}
    assert listed[upstream["task_id"]]["downstream_task_ids"] == [follower["task_id"]]
    assert client.get(f"/scheduled-tasks/{upstream['task_id']}").json()["downstream_task_ids"] == [
        follower["task_id"]
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"upstream_task_id": None},
        {"upstream_task_id": "missing"},
    ],
)
def test_follower_needs_a_real_upstream(client: TestClient, overrides: dict[str, Any]) -> None:
    body = {
        "trigger_kind": "after_task",
        "cron_expr": None,
        "name": "Takip",
        "prompt": "x",
        **overrides,
    }
    assert _create(client, **body).status_code == 422


def test_patch_rejects_cycle(client: TestClient) -> None:
    a = _create(client, name="A").json()
    b = _follow(client, a["task_id"], name="B").json()
    response = client.patch(
        f"/scheduled-tasks/{a['task_id']}",
        json={"trigger_kind": "after_task", "upstream_task_id": b["task_id"]},
    )
    assert response.status_code == 422
    assert "loop" in response.json()["detail"]


def test_patch_follower_back_to_cron(client: TestClient) -> None:
    a = _create(client, name="A").json()
    b = _follow(client, a["task_id"]).json()
    updated = client.patch(
        f"/scheduled-tasks/{b['task_id']}",
        json={"trigger_kind": "cron", "cron_expr": "0 9 * * 1"},
    ).json()
    assert updated["upstream_task_id"] is None
    assert updated["condition"] is None
    assert updated["next_run_at"] is not None


def test_resume_orphaned_follower_is_refused(client: TestClient) -> None:
    a = _create(client, name="A").json()
    b = _follow(client, a["task_id"]).json()
    client.post(f"/scheduled-tasks/{b['task_id']}/pause")
    client.delete(f"/scheduled-tasks/{a['task_id']}")
    assert client.get(f"/scheduled-tasks/{b['task_id']}").json()["upstream_task_id"] is None
    assert client.post(f"/scheduled-tasks/{b['task_id']}/resume").status_code == 422


def test_attached_files(client: TestClient) -> None:
    task = _create(client, attached_file_ids=["file-1"]).json()
    assert task["attached_files"] == [{"file_id": "file-1", "filename": "sablon.docx"}]
    assert _create(client, attached_file_ids=["nope"]).status_code == 404
    cleared = client.patch(f"/scheduled-tasks/{task['task_id']}", json={"attached_file_ids": []}).json()
    assert cleared["attached_files"] == []


def test_preview_after_task(client: TestClient) -> None:
    body = client.post("/scheduled-tasks/preview", json={"trigger_kind": "after_task"}).json()
    assert body["upcoming_runs"] == []



def test_delivery_setting(client: TestClient) -> None:
    task = _create(
        client,
        delivery={"to": ["ali@example.com", "ALI@example.com"], "cc": ["ekip@example.com"], "on": "change"},
    ).json()
    assert task["delivery"] == {
        "to": ["ali@example.com"],
        "cc": ["ekip@example.com"],
        "on": "change",
        "attach_files": True,
    }

    renamed = client.patch(f"/scheduled-tasks/{task['task_id']}", json={"name": "Yeni ad"}).json()
    assert renamed["delivery"]["to"] == ["ali@example.com"]

    cleared = client.patch(f"/scheduled-tasks/{task['task_id']}", json={"delivery": None}).json()
    assert cleared["delivery"] is None


@pytest.mark.parametrize(
    "delivery",
    [{"to": []}, {"to": ["bozuk-adres"]}, {"to": ["a@example.com"], "cc": ["x"]}],
)
def test_delivery_validation(client: TestClient, delivery: dict[str, Any]) -> None:
    assert _create(client, delivery=delivery).status_code == 422
