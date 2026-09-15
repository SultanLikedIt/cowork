"""Per-step timing: model calls, tool calls and reranking land on one timeline."""

from __future__ import annotations

import time

import main
from model_config import UsageTracker
from tool_call_log import ToolCallRecorder, logged_tool


def test_timed_model_call_is_on_the_timeline() -> None:
    tracker = UsageTracker()
    tracker.record(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        stage="web_answer",
        duration_seconds=1.5,
    )
    events = tracker.timeline_snapshot()
    assert len(events) == 1
    event = events[0]
    assert (event["kind"], event["name"]) == ("llm", "web_answer")
    assert event["duration_ms"] == 1500.0
    assert (event["input_tokens"], event["output_tokens"]) == (10, 5)


def test_untimed_model_call_is_not_on_the_timeline() -> None:
    tracker = UsageTracker()
    tracker.record({}, stage="planning")
    assert tracker.timeline_snapshot() == []


def test_timeline_is_capped() -> None:
    tracker = UsageTracker()
    for index in range(main.MAX_TIMELINE_EVENTS + 25):
        tracker.record_event("rerank", "rerank", 0.01, detail=str(index))
    events = tracker.timeline_snapshot()
    assert len(events) == main.MAX_TIMELINE_EVENTS
    assert events[-1]["detail"] == str(main.MAX_TIMELINE_EVENTS + 24)


def test_logged_tool_records_duration() -> None:
    recorder = ToolCallRecorder(job_id="timing-tool")

    def read_page(url: str) -> dict:
        time.sleep(0.02)
        return {"ok": True}

    logged_tool(read_page, recorder)(url="https://a.example/page")
    call = recorder.snapshot()[0]
    assert call["duration_ms"] >= 15
    assert call["started_at"] <= call["timestamp"]


def test_failed_tool_call_still_records_duration() -> None:
    recorder = ToolCallRecorder(job_id="timing-tool-error")

    def read_page(url: str) -> dict:
        raise RuntimeError("boom")

    try:
        logged_tool(read_page, recorder)(url="https://a.example/page")
    except RuntimeError:
        pass
    call = recorder.snapshot()[0]
    assert call["error"] and call["duration_ms"] is not None


def test_timeline_merges_every_kind_oldest_first() -> None:
    tracker = UsageTracker()
    recorder = ToolCallRecorder(job_id="timing-merge")

    def search_raw(query: str, max_results: int = 10) -> dict:
        return {"results": []}

    logged_tool(search_raw, recorder)(query="postgres 18", max_results=10)
    tracker.record_event("rerank", "rerank", 0.4, detail="10 candidates, picked 3")
    tracker.record(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        stage="web_answer",
        duration_seconds=2.0,
    )
    events = main._timeline(tracker, recorder.snapshot())
    kinds = {(event["kind"], event["name"]) for event in events}
    assert kinds == {("tool", "search_raw"), ("rerank", "rerank"), ("llm", "web_answer")}
    tool = next(event for event in events if event["kind"] == "tool")
    assert tool["detail"] == "postgres 18" and tool["ok"] is True
    starts = [event["started_at"] for event in events]
    assert starts == sorted(starts)


def test_untimed_legacy_tool_calls_are_skipped() -> None:
    legacy = [{"timestamp": "2026-01-01T00:00:00+00:00", "tool_name": "read_page", "args": {}}]
    assert main._timeline(UsageTracker(), legacy) == []
