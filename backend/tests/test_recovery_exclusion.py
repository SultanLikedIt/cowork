"""Recovery passes search for new pages instead of re-fetching old ones.

The recovery lookup used to list opened pages by URL; acquire_web_evidence
takes any URL in its text as user-typed and fetches it without searching,
so every recovery pass re-read cached pages and answered again.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import web_controller
import web_fetch

os.environ["RERANKER_ENABLED"] = "false"


async def _complete(phase: str, system: str, user: str, schema: Optional[type]) -> Any:
    if schema is web_controller.SearchQuery:
        return web_controller.SearchQuery(query="recovery query")
    if schema is web_controller.UrlSelection:
        return web_controller.UrlSelection(urls=[])
    return "answer"


def test_recovery_lookup_holds_no_url() -> None:
    from execution.langgraph_runtime.workforce import _cheap_recovery_lookup

    text = _cheap_recovery_lookup(
        "türkiye ev satışları",
        ["https://news.example/a/1", "https://stats.example/t"],
        [
            {
                "ok": True,
                "title": "Title https://leak.example/x",
                "text": "Body with a link https://other.example/page in it.",
            }
        ],
    )
    assert web_fetch.find_urls(text) == []
    assert "news.example" in text and "stats.example" in text


def test_excluded_urls_are_not_selected_again() -> None:
    fetched: list[str] = []

    def search(query: str, max_results: int = 10) -> dict[str, Any]:
        return {
            "ok": True,
            "results": [
                {"url": "https://old.example/article/read", "title": "old", "content": "x"},
                {"url": "https://new.example/article/fresh", "title": "new", "content": "x"},
            ],
        }

    def fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return {"ok": True, "source_url": url, "title": "t", "text": "page text " * 50}

    result = asyncio.run(
        web_controller.acquire_web_evidence(
            "recovery lookup text",
            search=search,
            fetch=fetch,
            complete=_complete,
            compact=True,
            allow_refine=False,
            exclude_urls=["https://old.example/article/read/"],
        )
    )
    assert result.search_count == 1, "recovery must search, not re-fetch"
    assert fetched == ["https://new.example/article/fresh"]
