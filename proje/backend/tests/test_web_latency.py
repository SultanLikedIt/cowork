"""Latency behavior of the web path: concurrent search, fetch early exit,
and answer-evidence trimming that does not drop the pages that loaded."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest import mock

import agent_search_client
import web_controller
import web_search
from execution.langgraph_runtime import workforce


def _hit(url: str) -> dict[str, Any]:
    return {"result_id": 1, "title": url, "description": "x", "url": url, "content": "x"}


def test_search_requests_run_concurrently() -> None:
    def slow_searx(query, *, categories, language, time_range, limit):
        time.sleep(0.4)
        return [_hit(f"https://searx.example/{categories}")]

    def slow_agent(query, *, categories, limit):
        time.sleep(0.4)
        return [_hit(f"https://agent.example/{categories}")]

    with (
        mock.patch.object(agent_search_client, "is_enabled", return_value=True),
        mock.patch.object(web_search, "_searx_raw", side_effect=slow_searx),
        mock.patch.object(web_search, "_agent_search_raw", side_effect=slow_agent),
        mock.patch.object(web_search, "AGENT_SEARCH_GENERAL_MODE", "general"),
    ):
        started = time.monotonic()
        out = web_search.search_web("q", number_of_result_pages=10, merge_news=True)
        elapsed = time.monotonic() - started

    # Four 0.4s requests: sequential was ~1.6s.
    assert elapsed < 1.0
    assert {hit["url"] for hit in out} == {
        "https://searx.example/news",
        "https://searx.example/general",
        "https://agent.example/news",
        "https://agent.example/general",
    }


def test_search_does_not_wait_for_slow_agent_search() -> None:
    def fast_searx(query, *, categories, language, time_range, limit):
        return [_hit("https://searx.example/")]

    def stalled_agent(query, *, categories, limit):
        time.sleep(2.0)
        return [_hit("https://agent.example/")]

    with (
        mock.patch.object(agent_search_client, "is_enabled", return_value=True),
        mock.patch.object(web_search, "_searx_raw", side_effect=fast_searx),
        mock.patch.object(web_search, "_agent_search_raw", side_effect=stalled_agent),
        mock.patch.object(web_search, "AGENT_SEARCH_GRACE_SECONDS", 0.1),
        mock.patch.object(web_search, "AGENT_SEARCH_GENERAL_MODE", "general"),
    ):
        started = time.monotonic()
        out = web_search.search_web("q", merge_news=False)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert [hit["url"] for hit in out] == ["https://searx.example/"]


def test_searxng_failure_does_not_fail_the_search() -> None:
    def broken_searx(query, *, categories, language, time_range, limit):
        raise RuntimeError("boom")

    with (
        mock.patch.object(agent_search_client, "is_enabled", return_value=True),
        mock.patch.object(web_search, "_searx_raw", side_effect=broken_searx),
        mock.patch.object(
            web_search, "_agent_search_raw", return_value=[_hit("https://agent.example/")]
        ),
        mock.patch.object(web_search, "AGENT_SEARCH_GENERAL_MODE", "general"),
    ):
        out = web_search.search_web("q", merge_news=False)

    assert [hit["url"] for hit in out] == ["https://agent.example/"]


def test_general_agent_search_uses_configured_mode() -> None:
    with (
        mock.patch.object(agent_search_client, "search", return_value=[]) as search,
        mock.patch.object(web_search, "AGENT_SEARCH_GENERAL_MODE", "reference"),
    ):
        web_search._agent_search_raw("q", categories="general", limit=5)
        web_search._agent_search_raw("q", categories="news", limit=5)

    assert search.call_args_list[0].kwargs["mode"] == "reference"
    # Other categories keep the client's own category -> strategy mapping.
    assert search.call_args_list[1].kwargs["mode"] is None


def test_general_search_skips_agent_search_when_mode_off() -> None:
    with (
        mock.patch.object(agent_search_client, "is_enabled", return_value=True),
        mock.patch.object(web_search, "_searx_raw", return_value=[_hit("https://s.example/")]),
        mock.patch.object(web_search, "_agent_search_raw", return_value=[]) as agent,
        mock.patch.object(web_search, "AGENT_SEARCH_GENERAL_MODE", "off"),
    ):
        web_search.search_web("q", merge_news=True)

    # Only the news pack reaches the sidecar.
    assert [call.kwargs["categories"] for call in agent.call_args_list] == ["news"]


def _page(text: str = "body") -> dict[str, Any]:
    return {"ok": True, "text": text, "source_url": ""}


def test_fetch_round_stops_waiting_once_enough_pages_loaded() -> None:
    delays = {
        "https://a.example/1": 0.0,
        "https://b.example/2": 0.05,
        "https://slow.example/3": 3.0,
    }

    def fetch(url: str) -> dict[str, Any]:
        time.sleep(delays[url])
        return _page()

    result = web_controller.ControllerResult()

    async def timed_round() -> float:
        # Timed inside the loop: asyncio.run itself waits for the abandoned
        # page's worker thread at shutdown, which the workforce loop does not.
        started = time.monotonic()
        await web_controller._fetch_urls(
            result, list(delays), fetch=fetch, progress=None, max_fetches=6
        )
        return time.monotonic() - started

    with mock.patch.object(web_controller, "FETCH_SETTLE_GRACE_SECONDS", 0.2):
        elapsed = asyncio.run(timed_round())

    assert elapsed < 1.5
    assert result.fetch_count == 3
    assert result.successful_fetches == 2
    assert [item["url"] for item in result.evidence] == list(delays)
    skipped = result.evidence[2]
    assert skipped["ok"] is False
    assert skipped["error_reason"] == "fetch_abandoned"


def test_fetch_round_waits_for_all_pages_when_none_loaded_yet() -> None:
    def fetch(url: str) -> dict[str, Any]:
        if url.endswith("/fail"):
            return {"ok": False, "error_reason": "http_error"}
        time.sleep(0.3)
        return _page()

    result = web_controller.ControllerResult()
    asyncio.run(
        web_controller._fetch_urls(
            result,
            ["https://a.example/fail", "https://b.example/ok"],
            fetch=fetch,
            progress=None,
            max_fetches=6,
        )
    )

    # A failure alone starts no grace period: the slower page still counts.
    assert result.successful_fetches == 1
    assert result.evidence[1]["ok"] is True


def test_answer_trim_keeps_loaded_pages_over_later_failures() -> None:
    good = [{"url": f"https://good.example/{i}", "ok": True, "text": "fact"} for i in range(2)]
    failed = [
        {"url": f"https://bad.example/{i}", "ok": False, "text": "", "error_reason": "timeout"}
        for i in range(6)
    ]

    trimmed = workforce._trim_evidence_for_answer(good + failed, max_items=6)

    urls = [item["url"] for item in trimmed]
    assert len(trimmed) == 6
    assert urls[:2] == ["https://good.example/0", "https://good.example/1"]
    # Remaining room goes to the most recent failures.
    assert urls[2:] == [f"https://bad.example/{i}" for i in range(2, 6)]


def test_answer_trim_keeps_newest_pages_when_all_loaded() -> None:
    pages = [{"url": f"https://p.example/{i}", "ok": True, "text": "x" * 50} for i in range(8)]

    trimmed = workforce._trim_evidence_for_answer(pages, max_items=6, max_text=10)

    assert [item["url"] for item in trimmed] == [f"https://p.example/{i}" for i in range(2, 8)]
    assert all(len(item["text"]) == 10 for item in trimmed)
