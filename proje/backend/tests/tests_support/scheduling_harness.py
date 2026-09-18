"""Shared scheduler test harness: a temp database, a fake clock, a fake
submit (no model, no web) and an in-memory job table standing in for
main._JOBS."""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

import db
from scheduling import delivery, runner, store, watch

IST = "Europe/Istanbul"
NOW = 1_790_000_000.0  # a fixed moment; only relative times matter


@pytest.fixture()
def temp_db():
    original_dir, original_path = db.DATA_DIR, db.DB_PATH
    tmp = Path(tempfile.mkdtemp(prefix="scheduling_db_test_"))
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "app.db"
    db.run_migrations()
    db.create_workspace("ws-1", "Test", time.time())
    try:
        yield
    finally:
        db.DATA_DIR, db.DB_PATH = original_dir, original_path


class Harness:
    def __init__(self, now: float = NOW) -> None:
        self.now = now
        self.jobs: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.fail_submit: Optional[Exception] = None
        self.page_texts: dict[str, str] = {}
        self.diff_result: Optional[watch.DiffResult] = None
        self.diff_calls: list[tuple[str, str, str]] = []
        # job_id -> (file_ids, notes) returned when a chain collects its files
        self.job_files: dict[str, tuple[list[str], list[str]]] = {}
        self.collected: list[str] = []
        # Outcomes handed out by the fake mailer, in order; then success.
        self.delivery_outcomes: list[delivery.DeliveryOutcome] = []
        self.delivered: list[tuple[str, str]] = []  # (task_id, run_id)

        async def submit(**kwargs: Any) -> runner.SubmittedJob:
            if self.fail_submit is not None:
                raise self.fail_submit
            job_id = f"job-{len(self.submitted) + 1}"
            self.submitted.append({**kwargs, "job_id": job_id})
            self.jobs[job_id] = {"status": "running", "result": None}
            return runner.SubmittedJob(job_id=job_id, conversation_id=f"conv-{job_id}")

        async def diff(prev: str, new: str, criteria: str) -> Optional[watch.DiffResult]:
            self.diff_calls.append((prev, new, criteria))
            return self.diff_result

        def fetch(url: str) -> str:
            return self.page_texts[url]

        async def collect_files(job_id: str) -> tuple[list[str], list[str]]:
            self.collected.append(job_id)
            return self.job_files.get(job_id, ([], []))

        async def deliver(task: dict[str, Any], run: dict[str, Any]) -> delivery.DeliveryOutcome:
            self.delivered.append((task["task_id"], run["run_id"]))
            if self.delivery_outcomes:
                return self.delivery_outcomes.pop(0)
            return delivery.DeliveryOutcome(True)

        self.scheduler = runner.Scheduler(
            submit=submit,
            job_state=self.jobs.get,
            clock=lambda: self.now,
            fetch_text=fetch,
            diff=diff,
            collect_files=collect_files,
            deliver=deliver,
        )

    def tick(self) -> None:
        asyncio.run(self.scheduler.tick())

    def run_now(self, task_id: str) -> dict[str, Any]:
        return asyncio.run(self.scheduler.run_now(task_id))

    def finish(self, job_id: str, status: str = "done", result: str = "Rapor metni.") -> None:
        self.jobs[job_id] = {
            "status": status,
            "result": result,
            "error": "boom" if status in {"failed", "error"} else None,
        }

    def submitted_for(self, task_id: str) -> list[dict[str, Any]]:
        return [item for item in self.submitted if item["task"]["task_id"] == task_id]


def make_task(**overrides: Any) -> str:
    now = overrides.pop("now", NOW)
    record = {
        "task_id": str(uuid.uuid4()),
        "workspace_id": "ws-1",
        "name": "Haftalık bülten",
        "prompt": "{hafta} sektör haberleri",
        "mode": "multi",
        "trigger_kind": "cron",
        "cron_expr": "0 9 * * 1",
        "timezone": IST,
        "next_run_at": now - 5,
        "created_at": now - 1000,
        "updated_at": now - 1000,
    }
    record.update(overrides)
    store.create_task(record)
    return record["task_id"]
