"""Regression tests for two _run_job polish items:

1. Duration must freeze when the canonical final-answer text is actually
   ready, not when `finally` gets around to measuring it. Between those two
   moments sits replay_final_answer_as_stream's fake typing effect
   (asyncio.sleep(0.015) per 4-char chunk -- 15-30+ real seconds for a
   medium-length answer) plus queue drain and DB persistence.
   None of that is the agent doing work, so it must not inflate the
   reported "how long did this take" number.

2. The text stored as the canonical answer (job["result"], what
   getFinalAnswer()/document export/conversation-context replay all read)
   must be EXACTLY the same string that was streamed to the user as the
   typing effect -- not a value computed from a different snapshot of
   result_task.result, which would let the UI show one answer while a
   reopened conversation or an export shows another.

No model or network: Workforce execution is stubbed, with real (small)
sleeps standing in for "real work" vs "presentation delay" so the timing
assertions exercise actual wall-clock behavior rather than mocked time.

    python test_final_result_and_duration.py
"""

import asyncio
import os
import tempfile
import time

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import db  # noqa: E402
import main  # noqa: E402
from execution.result import execution_result  # noqa: E402

# Module-level (not just inside main_test()) so pytest -- which discovers and
# runs the test_* functions below directly, never calling main_test() -- also
# gets the jobs table before any of them touch db.update_job_status. Idempotent
# (db.run_migrations gates on PRAGMA user_version), so main_test()'s own call
# stays harmless when this file runs as a script.
db.run_migrations()

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        raise AssertionError(f"{name} -- {detail}")


def _make_job(job_id: str, mode: str = "multi") -> None:
    main._JOBS[job_id] = {
        "job_id": job_id,
        "topic": "t",
        "status": "queued",
        "mode": mode,
        "result": "",
        "error": None,
        "created_at": 0.0,
        "started_at": None,
        "duration_seconds": None,
        "usage": {},
        "task_states": {},
        "task_outputs": {},
        "task_attempts": {},
        "task_plan": [],
        "cancellation_requested": False,
        "cancellation_note": None,
        "workspace_id": None,
        "conversation_id": None,
        "workspace_root": None,
    }
    main._JOB_CONTROLS[job_id] = main._JobControl(job_id)
    main._JOB_INDEX[job_id] = {}
    main._WORKER_ROLES[job_id] = {}
    main._TIMING[job_id] = {"tasks": {}, "root_created": None, "first_worker_started": None}
    # job_tasks carries a foreign key to jobs, and the real submit path always
    # writes the job row before _run_job persists a single task. A job that
    # exists only in main._JOBS is not a job this app can ever produce, so the
    # fixture builds both halves -- otherwise the first task write dies on
    # "FOREIGN KEY constraint failed" instead of testing what it meant to.
    db.save_job(main._JOBS[job_id])


REPLAY_DELAY = 0.3
WORK_DELAY = 0.02


def _run_workforce_job(
    job_id: str,
    *,
    execution_strategy: str = "workforce",
    raw_result: str = "answer body",
    cancelled: bool = False,
    raise_error: bool = False,
) -> tuple[dict, dict]:
    """Drive _run_job through the workforce/specialist branch.

    Returns (job dict, captured) where captured["replayed_text"] is exactly
    what was handed to replay_final_answer_as_stream -- the thing that must
    equal job["result"] afterwards.
    """
    _make_job(job_id)
    job = main._JOBS[job_id]
    job["usage"]["execution_strategy"] = execution_strategy
    control = main._JOB_CONTROLS[job_id]
    control.cancelled = cancelled
    captured: dict = {}

    async def fake_run_workforce(*args, **kwargs):
        # Real (small) sleep: this runs inside asyncio.to_thread's actual
        # worker thread via the fake_to_thread shim below, so time.sleep
        # here genuinely elapses wall-clock time, standing in for real
        # orchestration work.
        time.sleep(WORK_DELAY)
        if raise_error:
            raise RuntimeError("boom")
        return execution_result(task_id=job_id, content="c", result=raw_result)

    async def fake_replay(text, task_id, queue):
        captured["replayed_text"] = text
        await asyncio.sleep(REPLAY_DELAY)

    async def fake_intent(*args, **kwargs):
        import artifact_intent as ai

        return ai.ArtifactIntent(False, "none", "test")

    def workforce_sentinel(*args, **kwargs):
        raise AssertionError("replaced by fake_to_thread")

    originals = (
        main.run_workforce_in_thread_executor,
        main.replay_final_answer_as_stream,
        main.artifact_intent.classify_artifact_intent,
    )
    main.run_workforce_in_thread_executor = workforce_sentinel
    main.replay_final_answer_as_stream = fake_replay
    main.artifact_intent.classify_artifact_intent = fake_intent

    original_to_thread = asyncio.to_thread

    async def fake_to_thread(fn, *a, **k):
        if fn is workforce_sentinel:
            return await fake_run_workforce()
        return await original_to_thread(fn, *a, **k)

    asyncio.to_thread = fake_to_thread
    wall_start = time.monotonic()
    try:
        asyncio.run(main._run_job(job_id, "content", "multi"))
    finally:
        asyncio.to_thread = original_to_thread
        (
            main.run_workforce_in_thread_executor,
            main.replay_final_answer_as_stream,
            main.artifact_intent.classify_artifact_intent,
        ) = originals
    captured["wall_elapsed"] = time.monotonic() - wall_start
    return job, captured


def _run_direct_job(job_id: str, *, answer: str = "direct answer") -> dict:
    _make_job(job_id)
    job = main._JOBS[job_id]
    job["usage"]["execution_strategy"] = "direct"

    async def fake_single_agent(content, tracker, task_id, queue, **kwargs):
        time.sleep(WORK_DELAY)
        return execution_result(task_id=task_id, content="c", result=answer)

    original = main.run_single_agent_executor
    main.run_single_agent_executor = fake_single_agent
    try:
        asyncio.run(main._run_job(job_id, "content", "single"))
    finally:
        main.run_single_agent_executor = original
    return job


def test_duration_excludes_replay_delay() -> None:
    job, captured = _run_workforce_job("job-duration")

    check(
        "job completed successfully in the stub",
        job["status"] == "done",
        job["status"],
    )
    check(
        "duration is close to the real work time, not the full run",
        job["duration_seconds"] < REPLAY_DELAY / 2,
        f"duration={job['duration_seconds']} replay_delay={REPLAY_DELAY}",
    )
    check(
        "the run actually took longer than the reported duration",
        captured["wall_elapsed"] > job["duration_seconds"] + REPLAY_DELAY / 2,
        f"wall={captured['wall_elapsed']} duration={job['duration_seconds']}",
    )
    check(
        "duration is not negative or implausibly small",
        job["duration_seconds"] >= WORK_DELAY / 2,
        job["duration_seconds"],
    )


def test_duration_falls_back_on_error() -> None:
    # No result_ready_at is ever set on this path (the exception fires
    # inside the stubbed workforce run, before synthesis). There is no
    # "answer was ready" moment, so duration must fall back to full
    # elapsed time rather than silently reporting ~0.
    job, captured = _run_workforce_job("job-duration-error", raise_error=True)
    check("job reported as error", job["status"] == "error", job["status"])
    check(
        "duration falls back to full wall time on error, not ~0",
        job["duration_seconds"] >= WORK_DELAY / 2,
        f"duration={job['duration_seconds']}",
    )


def test_duration_falls_back_when_cancelled_before_synthesis() -> None:
    # control.cancelled=True from the start skips the `if not
    # control.cancelled:` block entirely -- synthesis and replay never run,
    # so result_ready_at is never set. Duration must still be a real
    # (non-zero, non-frozen) measurement of how long the run actually took.
    job, captured = _run_workforce_job("job-duration-cancelled", cancelled=True)
    check("job reported as cancelled", job["status"] == "cancelled", job["status"])
    check(
        "duration falls back to full wall time when cancelled pre-synthesis",
        job["duration_seconds"] >= WORK_DELAY / 2,
        f"duration={job['duration_seconds']}",
    )


def test_direct_mode_duration_is_not_frozen_early() -> None:
    # DIRECT has no replay step -- run_single_agent's own streaming IS the
    # real generation. result_ready_at is set right after it returns, which
    # should equal (not undershoot) the real work time.
    job = _run_direct_job("job-direct-duration")
    check("direct job completed", job["status"] == "done", job["status"])
    check(
        "direct-mode duration reflects the real work time",
        job["duration_seconds"] >= WORK_DELAY / 2,
        f"duration={job['duration_seconds']}",
    )


def test_canonical_result_matches_what_was_streamed() -> None:
    """job["result"] must be exactly the text the user was shown streaming.

    This is the propagation invariant: whatever text
    replay_final_answer_as_stream sent to the frontend as the typing
    effect is the same text getFinalAnswer()/document export/conversation-
    context replay will read afterwards as job["result"]. If a future
    change computed job["result"] from a different value than what was
    replayed, the UI's settled view (or an export, or a follow-up's
    context) could show different text than what the user just watched
    being typed out.
    """
    job, captured = _run_workforce_job(
        "job-canonical", raw_result="the final answer"
    )
    check(
        "the final text was actually replayed",
        captured.get("replayed_text") == "the final answer",
        captured.get("replayed_text"),
    )
    check(
        "job.result exactly matches the replayed text",
        job["result"] == captured.get("replayed_text"),
        f"result={job['result']!r} replayed={captured.get('replayed_text')!r}",
    )


def main_test() -> None:
    db.run_migrations()

    test_duration_excludes_replay_delay()
    test_duration_falls_back_on_error()
    test_duration_falls_back_when_cancelled_before_synthesis()
    test_direct_mode_duration_is_not_frozen_early()
    test_canonical_result_matches_what_was_streamed()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All final-result/duration checks passed")


if __name__ == "__main__":
    main_test()
