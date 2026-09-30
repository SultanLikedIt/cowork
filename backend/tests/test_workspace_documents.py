"""Workspace document library: migration backfill, membership, deletion."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import db


@pytest.fixture()
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    return tmp_path


def _insert_job(job_id: str, workspace_id: str, conversation_id: str, attachments: list, ts: float) -> None:
    with db._write_lock, db._connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, workspace_id, conversation_id, topic, status, mode,
                planning_fallback, planning_errors_json, created_at,
                attachments_json, usage_json, skills_json
            ) VALUES (?, ?, ?, 'topic', 'done', 'multi', 0, '[]', ?, ?, '{}', '[]')
            """,
            (job_id, workspace_id, conversation_id, ts, json.dumps(attachments)),
        )


def test_migration_backfills_the_library_from_history(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bring the schema to 0011, write history, then let 0012 run over it.
    older = fresh_db / "older-migrations"
    older.mkdir()
    for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) <= 11:
            shutil.copy(path, older / path.name)
    real_dir = db.MIGRATIONS_DIR
    monkeypatch.setattr(db, "MIGRATIONS_DIR", older)
    db.run_migrations()

    now = time.time()
    db.create_workspace("ws", "W", now)
    db.create_conversation("c1", "ws", "one", now)
    db.create_conversation("c2", "ws", "two", now)
    # Attached to a job, then pushed out of the ten-file conversation bag.
    _insert_job("j1", "ws", "c1", [{"file_id": "old", "filename": "old.pdf"}], now)
    # In the bag only.
    db.add_conversation_files("c2", [{"file_id": "bag", "filename": "bag.docx"}], now + 1)
    # In both, and malformed attachment JSON on another job.
    _insert_job("j2", "ws", "c2", [{"file_id": "bag", "filename": "bag.docx"}, "junk"], now + 2)
    _insert_job("j3", "ws", "c2", [], now + 3)
    with db._write_lock, db._connect() as conn:
        conn.execute("UPDATE jobs SET attachments_json = 'not json' WHERE job_id = 'j3'")
    # A job whose workspace is gone (jobs have no FK to workspaces) must not
    # put a library row in for it.
    _insert_job("j4", "ghost", None, [{"file_id": "ghost-file", "filename": "g.pdf"}], now)

    monkeypatch.setattr(db, "MIGRATIONS_DIR", real_dir)
    db.run_migrations()

    library = db.list_workspace_documents("ws")
    assert [(item["file_id"], item["filename"]) for item in library] == [
        ("old", "old.pdf"),
        ("bag", "bag.docx"),
    ]
    assert set(db.all_workspace_document_ids()) == {"old", "bag"}


def test_library_is_idempotent_and_never_drops_files(fresh_db: Path) -> None:
    db.run_migrations()
    now = time.time()
    db.create_workspace("ws", "W", now)
    files = [{"file_id": f"f{i}", "filename": f"{i}.pdf"} for i in range(25)]
    db.add_workspace_documents("ws", files, now)
    db.add_workspace_documents("ws", files[:3], now + 5)
    library = db.list_workspace_documents("ws")
    assert len(library) == 25
    # The first insert's timestamp is kept.
    assert library[0]["added_at"] == now


def test_removing_a_document_and_finding_orphans(fresh_db: Path) -> None:
    db.run_migrations()
    now = time.time()
    db.create_workspace("a", "A", now)
    db.create_workspace("b", "B", now)
    db.add_workspace_documents("a", [{"file_id": "shared"}, {"file_id": "only-a"}], now)
    db.add_workspace_documents("b", [{"file_id": "shared"}], now)

    assert db.remove_workspace_document("a", "only-a") is True
    assert db.remove_workspace_document("a", "only-a") is False
    assert db.unreferenced_document_ids(["shared", "only-a", "never"]) == ["only-a", "never"]


def test_deleting_a_workspace_removes_its_library(fresh_db: Path) -> None:
    db.run_migrations()
    now = time.time()
    db.create_workspace("a", "A", now)
    db.create_workspace("b", "B", now)
    db.add_workspace_documents("a", [{"file_id": "shared"}, {"file_id": "only-a"}], now)
    db.add_workspace_documents("b", [{"file_id": "shared"}], now)

    assert db.delete_workspace("a") == []
    assert db.list_workspace_documents("a") == []
    assert db.unreferenced_document_ids(["shared", "only-a"]) == ["only-a"]


def test_deleting_a_conversation_keeps_its_files_in_the_library(fresh_db: Path) -> None:
    db.run_migrations()
    now = time.time()
    db.create_workspace("ws", "W", now)
    db.create_conversation("c1", "ws", "chat", now)
    db.add_conversation_files("c1", [{"file_id": "f1", "filename": "a.pdf"}], now)
    db.add_workspace_documents("ws", [{"file_id": "f1", "filename": "a.pdf"}], now)

    db.delete_conversation("c1")
    assert db.list_conversation_files("c1") == []
    assert [item["file_id"] for item in db.list_workspace_documents("ws")] == ["f1"]
