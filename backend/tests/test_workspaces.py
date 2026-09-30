"""Workspace rename/delete persistence."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import db


def test_rename_and_delete_workspace() -> None:
    original_dir = db.DATA_DIR
    original_path = db.DB_PATH
    tmp = Path(tempfile.mkdtemp(prefix="workspace_db_test_"))
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "app.db"
    try:
        db.run_migrations()
        now = time.time()
        ws = "ws-test-1"
        db.create_workspace(ws, "Alpha", now)
        assert db.rename_workspace(ws, "Beta") is True
        row = db.get_workspace(ws)
        assert row is not None
        assert row["name"] == "Beta"

        conv = "conv-1"
        db.create_conversation(conv, ws, "Hello", now)
        with db._write_lock, db._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, workspace_id, conversation_id, topic, status, mode,
                    planning_fallback, planning_errors_json, created_at,
                    attachments_json, usage_json, skills_json
                ) VALUES (?, ?, ?, ?, ?, ?, 0, '[]', ?, '[]', '{}', '[]')
                """,
                ("job-1", ws, conv, "topic", "done", "single", now),
            )

        removed = db.delete_workspace(ws)
        assert removed == ["job-1"]
        assert db.get_workspace(ws) is None
        assert db.get_conversation(conv) is None
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

        ws2 = "ws-test-2"
        db.create_workspace(ws2, "Gamma", now)
        conv2 = "conv-2"
        db.create_conversation(conv2, ws2, "Bye", now)
        with db._write_lock, db._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, workspace_id, conversation_id, topic, status, mode,
                    planning_fallback, planning_errors_json, created_at,
                    attachments_json, usage_json, skills_json
                ) VALUES (?, ?, ?, ?, ?, ?, 0, '[]', ?, '[]', '{}', '[]')
                """,
                ("job-2", ws2, conv2, "topic-2", "done", "single", now),
            )
        removed_jobs = db.delete_conversation(conv2)
        assert removed_jobs == ["job-2"]
        assert db.get_conversation(conv2) is None
        with db._connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE job_id = ?", ("job-2",)
                ).fetchone()[0]
                == 0
            )
    finally:
        db.DATA_DIR = original_dir
        db.DB_PATH = original_path


if __name__ == "__main__":
    test_rename_and_delete_workspace()
    print("ok")
