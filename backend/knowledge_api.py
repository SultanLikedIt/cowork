"""``/workspaces/{id}/documents`` and ``/workspaces/{id}/knowledge/search``.

The document library a workspace's Knowledge Agent searches, and a raw
retrieval probe with no model call: what keyword search found, what the
reranker thought of it, and how long each step took. The probe is what the
retrieval eval and anyone tuning MIN_RELEVANCE look at.

No authentication, like every other route in this app: anyone who can reach
the backend can read any workspace's library already, through the chats.
"""

from __future__ import annotations

from typing import Any, Callable


def library_entries(
    workspace_id: str,
    *,
    list_documents: Callable[[str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Library rows with their index state: indexed | pending | skipped |
    failed | missing (never indexed and not queued)."""
    import knowledge_index

    documents = list_documents(workspace_id)
    statuses = knowledge_index.file_statuses([item["file_id"] for item in documents])
    entries: list[dict[str, Any]] = []
    for item in documents:
        file_id = item["file_id"]
        status = statuses.get(file_id)
        if knowledge_index.is_pending(file_id):
            state, passages, reason = "pending", 0, None
        elif status is None:
            state, passages, reason = "missing", 0, None
        else:
            state, passages, reason = status.status, status.passage_count, status.reason
        entries.append(
            {
                "file_id": file_id,
                "filename": item["filename"],
                "added_at": item["added_at"],
                "status": state,
                "passage_count": passages,
                "reason": reason,
            }
        )
    return entries


def build_router(*, load_upload_record: Callable[[str], dict[str, Any]]) -> Any:
    import asyncio
    import time

    from fastapi import APIRouter, HTTPException, Query

    import db
    import knowledge_controller
    import knowledge_index
    import reranker

    router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["knowledge"])

    async def _require_workspace(workspace_id: str) -> None:
        if await asyncio.to_thread(db.get_workspace, workspace_id) is None:
            raise HTTPException(status_code=404, detail=f"Unknown workspace_id: {workspace_id}")

    @router.get("/documents")
    async def list_documents(workspace_id: str):
        await _require_workspace(workspace_id)
        entries = await asyncio.to_thread(
            library_entries, workspace_id, list_documents=db.list_workspace_documents
        )
        return {"workspace_id": workspace_id, "documents": entries}

    @router.delete("/documents/{file_id}")
    async def remove_document(workspace_id: str, file_id: str):
        """Take a file out of this workspace's library. The upload and any
        chat that used it are untouched; the passages go only when no other
        workspace still holds the file."""
        await _require_workspace(workspace_id)
        removed = await asyncio.to_thread(db.remove_workspace_document, workspace_id, file_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{file_id} is not in this library")
        orphans = await asyncio.to_thread(db.unreferenced_document_ids, [file_id])
        await asyncio.to_thread(knowledge_index.drop_files, orphans)
        return {"removed": True, "file_id": file_id}

    @router.post("/documents/{file_id}/reindex")
    async def reindex_document(workspace_id: str, file_id: str):
        await _require_workspace(workspace_id)
        library = await asyncio.to_thread(db.list_workspace_documents, workspace_id)
        if all(item["file_id"] != file_id for item in library):
            raise HTTPException(status_code=404, detail=f"{file_id} is not in this library")
        record = await asyncio.to_thread(load_upload_record, file_id)
        future = knowledge_index.submit(record)
        if future is None:
            raise HTTPException(status_code=409, detail="Document search is disabled (KNOWLEDGE_ENABLED)")
        status = await asyncio.wrap_future(future)
        return status.to_dict()

    @router.get("/knowledge/search")
    async def search_documents(
        workspace_id: str,
        q: str = Query(..., min_length=1, max_length=500),
        rerank: bool = True,
        limit: int = Query(default=knowledge_controller.CANDIDATES, ge=1, le=100),
    ):
        """Raw retrieval for ``q``: keyword hits, then reranker scores.

        Only ``q`` itself is searched -- no PLAN call, so no model cost.
        """
        await _require_workspace(workspace_id)
        library = await asyncio.to_thread(db.list_workspace_documents, workspace_id)
        file_ids = [item["file_id"] for item in library]
        started = time.monotonic()
        hits = await asyncio.to_thread(knowledge_index.search, file_ids, [q], limit=limit)
        search_seconds = time.monotonic() - started

        scores: list[Any] = [None] * len(hits)
        rank_seconds = 0.0
        reranked = False
        if rerank and hits and reranker.enabled():
            ranked = hits[: knowledge_controller.RANK_CANDIDATES]
            started = time.monotonic()
            result = await asyncio.to_thread(
                reranker.score_batched,
                q,
                [knowledge_controller.rank_window(hit, [q]) for hit in ranked],
                deadline=time.monotonic() + knowledge_controller.RANK_BUDGET_SECONDS,
            )
            rank_seconds = time.monotonic() - started
            if result is not None:
                reranked = True
                scores[: len(result)] = result
        return {
            "workspace_id": workspace_id,
            "query": q,
            "terms": knowledge_index.query_terms(q),
            "searched_files": len(file_ids),
            "reranked": reranked,
            "min_relevance": knowledge_controller.MIN_RELEVANCE,
            "timings": {
                "search": round(search_seconds, 3),
                "rank": round(rank_seconds, 3),
            },
            "hits": [
                {
                    "file_id": hit.file_id,
                    "filename": hit.filename,
                    "page_from": hit.page_from,
                    "page_to": hit.page_to,
                    "heading": hit.heading,
                    "keyword_score": round(hit.score, 5),
                    "rerank_score": None if value is None else round(float(value), 4),
                    "text": hit.text,
                }
                for hit, value in zip(hits, scores)
            ],
        }

    return router
