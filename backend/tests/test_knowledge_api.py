"""Library + raw search routes, and the main.py hooks that feed the index."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import db
import knowledge_api
import knowledge_index as ki
import reranker

CONTRACT = (
    "# Kira Bedeli\n\nAylık kira bedeli 25.000 TL.\n\n"
    "# Fesih\n\nKiracı otuz gün önceden yazılı bildirimde bulunur."
)


@pytest.fixture()
def uploads() -> dict[str, dict[str, Any]]:
    return {
        "f1": {"file_id": "f1", "filename": "kira.md", "kind": "file", "extracted_text": CONTRACT},
        "f2": {"file_id": "f2", "filename": "arsiv.zip", "kind": "zip"},
        "f3": {"file_id": "f3", "filename": "yeni.md", "kind": "file", "extracted_text": "henüz yok"},
    }


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uploads):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(ki, "DB_PATH", tmp_path / "knowledge.db")
    db.run_migrations()
    now = time.time()
    db.create_workspace("ws", "W", now)
    db.create_workspace("other", "O", now)
    db.add_workspace_documents(
        "ws",
        [{"file_id": fid, "filename": uploads[fid]["filename"]} for fid in ("f1", "f2", "f3")],
        now,
    )
    db.add_workspace_documents("other", [{"file_id": "f1", "filename": "kira.md"}], now)
    ki.index_upload(uploads["f1"])
    ki.index_upload(uploads["f2"])

    def load(file_id: str) -> dict[str, Any]:
        if file_id not in uploads:
            raise HTTPException(status_code=404, detail="gone")
        return uploads[file_id]

    app = FastAPI()
    app.include_router(knowledge_api.build_router(load_upload_record=load))
    return TestClient(app)


def test_the_library_lists_every_file_with_its_index_state(client: TestClient) -> None:
    body = client.get("/workspaces/ws/documents").json()
    states = {doc["filename"]: doc["status"] for doc in body["documents"]}
    assert states == {"kira.md": "indexed", "arsiv.zip": "skipped", "yeni.md": "missing"}
    kira = next(doc for doc in body["documents"] if doc["file_id"] == "f1")
    assert kira["passage_count"] == 2


def test_unknown_workspace_is_404(client: TestClient) -> None:
    assert client.get("/workspaces/nope/documents").status_code == 404
    assert client.get("/workspaces/nope/knowledge/search", params={"q": "kira"}).status_code == 404


def test_reindex_indexes_a_missing_file(client: TestClient) -> None:
    response = client.post("/workspaces/ws/documents/f3/reindex")
    assert response.status_code == 200
    assert response.json()["status"] == "indexed"
    assert client.post("/workspaces/ws/documents/nope/reindex").status_code == 404


def test_removing_keeps_passages_another_workspace_still_uses(client: TestClient) -> None:
    assert client.delete("/workspaces/ws/documents/f1").status_code == 200
    assert "f1" in ki.file_statuses(["f1"])  # "other" still holds it
    assert client.delete("/workspaces/other/documents/f1").status_code == 200
    assert ki.file_statuses(["f1"]) == {}
    assert client.delete("/workspaces/ws/documents/f1").status_code == 404


def test_raw_search_without_reranker(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    body = client.get("/workspaces/ws/knowledge/search", params={"q": "fesih bildirimi"}).json()
    assert body["reranked"] is False
    assert body["terms"] == ["fesih", "bildiri", "bildi"]
    assert body["hits"][0]["heading"] == "Fesih"
    assert body["hits"][0]["rerank_score"] is None


def test_raw_search_reports_reranker_scores(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setattr(
        reranker, "score_batched", lambda q, passages, deadline=None: [0.8] * len(passages)
    )
    body = client.get("/workspaces/ws/knowledge/search", params={"q": "kira bedeli"}).json()
    assert body["reranked"] is True
    assert all(hit["rerank_score"] == 0.8 for hit in body["hits"])


def test_search_is_limited_to_the_workspace_library(client: TestClient) -> None:
    db.create_workspace("empty", "E", time.time())
    body = client.get("/workspaces/empty/knowledge/search", params={"q": "kira"}).json()
    assert body["hits"] == [] and body["searched_files"] == 0


# -- main.py hooks -------------------------------------------------------------


def test_an_upload_is_indexed_in_the_background(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    monkeypatch.setattr(ki, "DB_PATH", tmp_path / "knowledge.db")
    response = asyncio.run(main._store_upload("notlar.md", CONTRACT.encode("utf-8")))
    for future in ki.pending([response.file_id]):
        future.result(timeout=10)
    status = ki.file_statuses([response.file_id])[response.file_id]
    assert status.status == ki.STATUS_INDEXED


def test_missing_library_files_are_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uploads) -> None:
    import main

    monkeypatch.setattr(ki, "DB_PATH", tmp_path / "knowledge.db")
    ki.index_upload(uploads["f1"])

    def load(file_id: str) -> dict[str, Any]:
        if file_id not in uploads:
            raise HTTPException(status_code=404, detail="gone")
        return uploads[file_id]

    monkeypatch.setattr(main, "_load_upload_record", load)
    # f1 is current, f3 was never indexed, "gone" has no upload on disk.
    assert main._index_missing_documents(["f1", "f3", "gone"]) == 1
    for future in ki.pending(["f3"]):
        future.result(timeout=10)
    assert ki.file_statuses(["f3"])["f3"].status == ki.STATUS_INDEXED


def test_uploading_straight_into_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(ki, "DB_PATH", tmp_path / "knowledge.db")
    db.run_migrations()
    db.create_workspace("ws", "W", time.time())
    client = TestClient(main.app)

    response = client.post(
        "/workspaces/ws/documents",
        files=[("files", ("notlar.md", CONTRACT.encode("utf-8"), "text/markdown"))],
    )
    assert response.status_code == 201
    [doc] = response.json()["documents"]
    assert doc["filename"] == "notlar.md"
    assert doc["status"] in {"pending", "indexed"}
    for future in ki.pending([doc["file_id"]]):
        future.result(timeout=10)
    listed = client.get("/workspaces/ws/documents").json()["documents"]
    assert listed[0]["status"] == "indexed"

    missing = client.post(
        "/workspaces/nope/documents",
        files=[("files", ("a.md", b"x", "text/markdown"))],
    )
    assert missing.status_code == 404
