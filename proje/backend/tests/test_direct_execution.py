"""Focused test for the DIRECT execution path's task identity and telemetry.

Two regressions, both specific to run_single_agent (the DIRECT strategy --
all of mode="single", plus any mode="multi" job the router fast-paths to
"direct"):

  1. It named its one unit of work with `task_id == job_id`, which both
     main._apply_event and the frontend's excludeRootTask treat as the inert
     Workforce ROOT task. The task therefore got no `created` timestamp
     (its assignment-latency line was skipped) and was filtered out of the
     plan entirely. The SPECIALIST fast path already avoided this by naming
     its task like a subtask; DIRECT now uses the same _child_task_id.

  2. Its model is built with stream=True, and _UsageTrackingModel.record()
     reads usage off the returned object -- which for a stream has none. So
     a completed DIRECT run reported model_call_count=0 / total_tokens=0.

No network, no model backend, no real Workforce: ChatAgent/get_model are
patched, and the streaming response is a fake that mimics the two
properties run_single_agent depends on (async-iterable, and awaitable
AFTER consumption without re-driving the stream). Run directly:
    python test_direct_execution.py
"""

import asyncio
import os
import tempfile
from unittest.mock import patch

# Isolated DB/log directory, set BEFORE importing main/db so their
# module-level DATA_DIR reads pick it up.
_tmp_dir = tempfile.mkdtemp(prefix="agent_webapp_test_")
os.environ["DATA_DIR"] = _tmp_dir

import camel_service  # noqa: E402
import main  # noqa: E402
from model_config import UsageTracker  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


class _FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakePartial:
    def __init__(self, content: str) -> None:
        self.msg = _FakeMsg(content)


class _FakeFinal:
    def __init__(self, info: dict) -> None:
        self.info = info


class _FakeStream:
    """Mimics AsyncStreamingChatAgentResponse.

    Critically it records how often it is awaited and how often the
    underlying chunk sequence is driven, so the test can prove the usage
    lookup does not cost a second model call.
    """

    def __init__(self, chunks: list[str], usage: dict) -> None:
        self._chunks = chunks
        self._usage = usage
        self.stream_drives = 0
        self.awaits = 0

    def __aiter__(self):
        self.stream_drives += 1

        async def _gen():
            for chunk in self._chunks:
                yield _FakePartial(chunk)

        return _gen()

    def __await__(self):
        async def _final():
            self.awaits += 1
            # Already consumed: replay the settled response, do NOT re-drive
            # the generator (this is exactly what CAMEL's own _consumed flag
            # guarantees -- chat_agent.py:315-375).
            return _FakeFinal({"usage": self._usage})

        return _final().__await__()


class _FakeChatAgent:
    stream: "_FakeStream | None" = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def astep(self, content, response_format=None):
        return _FakeChatAgent.stream


async def _run() -> None:
    job_id = "11111111-2222-3333-4444-555555555555"
    expected_task_id = f"{job_id}.0"

    queue: "asyncio.Queue" = asyncio.Queue()
    tracker = UsageTracker()
    stream = _FakeStream(
        ["Hel", "Hello wo", "Hello world"],
        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    )
    _FakeChatAgent.stream = stream

    with patch.object(camel_service, "ChatAgent", _FakeChatAgent), patch.object(
        camel_service, "get_model", lambda *a, **k: None
    ):
        task = await camel_service.run_single_agent("hi", tracker, job_id, queue)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    # --- 1. Task identity -------------------------------------------------
    check(
        "returned Task uses the child id, not the job id",
        task.id == expected_task_id,
        f"got {task.id!r}, want {expected_task_id!r}",
    )
    check("returned Task carries the streamed text", task.result == "Hello world", repr(task.result))

    task_events = [e for e in events if e.get("task_id")]
    check("every emitted event carries a task_id", len(task_events) == len(events) - 1, f"{len(task_events)}/{len(events)}")
    offenders = [e["event_type"] for e in task_events if e["task_id"] == job_id]
    check(
        "no emitted event reuses the job id as its task id",
        not offenders,
        f"offending events: {offenders}",
    )
    check(
        "all task events use the single child id",
        {e["task_id"] for e in task_events} == {expected_task_id},
        str({e["task_id"] for e in task_events}),
    )

    emitted_types = [e["event_type"] for e in events]
    for required in (
        "worker_created",
        "task_created",
        "task_assigned",
        "task_started",
        "task_completed",
    ):
        check(f"event semantics preserved: {required} still emitted", required in emitted_types)
    check("token_delta events still emitted", "token_delta" in emitted_types)
    check(
        "streamed deltas reconstruct the answer",
        "".join(e["delta"] for e in events if e["event_type"] == "token_delta") == "Hello world",
    )

    # --- 2. Telemetry -----------------------------------------------------
    snapshot = tracker.snapshot()
    stages = tracker.stage_snapshot()
    check(
        "DIRECT run records exactly one model call",
        snapshot["model_call_count"] == 1,
        str(snapshot),
    )
    check(
        "DIRECT run records real provider tokens",
        (snapshot["input_tokens"], snapshot["output_tokens"], snapshot["total_tokens"])
        == (11, 7, 18),
        str(snapshot),
    )
    check(
        "usage is attributed to the single_agent stage",
        stages.get("single_agent", {}).get("model_call_count") == 1
        and stages["single_agent"]["total_tokens"] == 18,
        str(stages),
    )
    check(
        "usage lookup added no second model call",
        stream.stream_drives == 1 and stream.awaits == 1,
        f"stream_drives={stream.stream_drives} awaits={stream.awaits}",
    )

    # --- 3. Backend timing no longer mistakes the task for the root -------
    main._JOBS[job_id] = {
        "job_id": job_id,
        "task_plan": [],
        "task_states": {},
        "task_outputs": {},
        "task_attempts": {},
        "streaming_text": "",
    }
    main._JOB_INDEX[job_id] = {}
    main._WORKER_ROLES[job_id] = {}
    main._TIMING[job_id] = {
        "root_created": None,
        "decomposed": None,
        "first_worker_started": None,
        "tasks": {},
    }
    for event in events:
        main._apply_event(job_id, event)

    timing = main._TIMING[job_id]
    check(
        "the DIRECT task is tracked as a real task, not as root_created",
        timing["root_created"] is None and expected_task_id in timing["tasks"],
        f"root_created={timing['root_created']} tasks={list(timing['tasks'])}",
    )
    check(
        "the DIRECT task has a created timestamp (assignment latency measurable)",
        timing["tasks"][expected_task_id].get("created") is not None,
    )
    check(
        "the DIRECT task recorded an execution attempt",
        len(timing["tasks"][expected_task_id].get("attempts") or []) == 1,
    )

    job = main._JOBS[job_id]
    check(
        "the DIRECT task appears in the job's plan (survives excludeRootTask)",
        [entry["id"] for entry in job["task_plan"]] == [expected_task_id],
        str(job["task_plan"]),
    )
    check(
        "the DIRECT task reached completed state",
        job["task_states"].get(expected_task_id) == "completed",
        str(job["task_states"]),
    )
    check(
        "streaming text accumulated on the job",
        job["streaming_text"] == "Hello world",
        repr(job["streaming_text"]),
    )


def main_() -> None:
    missing_usage = UsageTracker()
    missing_usage.record(object(), stage="provider_call")
    check(
        "provider invocation counts even when token usage is absent",
        missing_usage.snapshot()["model_call_count"] == 1
        and missing_usage.snapshot()["total_tokens"] == 0,
        str(missing_usage.snapshot()),
    )
    asyncio.run(_run())
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All DIRECT execution identity/telemetry checks passed")


if __name__ == "__main__":
    main_()
