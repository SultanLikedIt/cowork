"""Regression test for orphaned task states at job finalization.

Observed live: a 3-way parallel Workforce run finalized and reported its
result while one subtask was still shown as RUNNING in the UI. The cause
is structural, not cosmetic -- a subtask can still be in flight when the
run ends (CAMEL's dispatch loop aborts on its own _get_returned_task
timeout, workforce.py:3906, and run_workforce_in_thread's teardown then
cancels the leftover worker coroutines), so no task_completed/task_failed
event is ever emitted for it. _apply_event therefore leaves task_states
at "running" forever, in the live snapshot and in the persisted row.

/cancel already reconciled this (cancel_job); the normal finalization
path did not. These tests exercise _run_job's finally block with the
Workforce execution stubbed out, so no model or network is involved.

    python test_task_state_finalization.py
"""

import asyncio
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import db  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from camel.tasks import Task  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def _make_job(job_id: str) -> None:
    """Create a job the way /run-pipeline does, minus the HTTP layer."""
    main._JOBS[job_id] = {
        "job_id": job_id,
        "topic": "t",
        "status": "queued",
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


def _run_with_states(
    job_id: str,
    states: dict,
    *,
    cancelled: bool = False,
    outputs: dict | None = None,
) -> dict:
    """Drive _run_job with the whole execution stubbed, then inspect state."""
    _make_job(job_id)
    job = main._JOBS[job_id]
    control = main._JOB_CONTROLS[job_id]
    control.cancelled = cancelled

    async def fake_run(*args, **kwargs):
        # Stand in for the Workforce run: seed exactly the task states (and
        # any partial outputs) the event stream would have left behind,
        # then return a finished root.
        job["task_states"].update(states)
        job["task_outputs"].update(outputs or {})
        root = Task(content="c", id=job_id)
        root.result = "done"
        root.state = "DONE"
        return root

    async def fake_synth(*a, **k):
        return "", False

    async def fake_replay(*a, **k):
        return None

    async def fake_intent(*args, **kwargs):
        import artifact_intent as ai

        return ai.ArtifactIntent(False, "none", "test")

    originals = (
        main.camel_service.run_workforce_in_thread,
        main.camel_service.synthesize_workforce_result,
        main.camel_service.replay_final_answer_as_stream,
        main.artifact_intent.classify_artifact_intent,
    )
    main.camel_service.run_workforce_in_thread = lambda *a, **k: None
    main.camel_service.synthesize_workforce_result = fake_synth
    main.camel_service.replay_final_answer_as_stream = fake_replay
    main.artifact_intent.classify_artifact_intent = fake_intent

    # to_thread would call the stub off-loop; patch it to run our coroutine.
    original_to_thread = asyncio.to_thread

    async def fake_to_thread(fn, *a, **k):
        if fn is main.camel_service.run_workforce_in_thread:
            return await fake_run()
        return await original_to_thread(fn, *a, **k)

    asyncio.to_thread = fake_to_thread
    try:
        asyncio.run(main._run_job(job_id, "content", "multi"))
    finally:
        asyncio.to_thread = original_to_thread
        (
            main.camel_service.run_workforce_in_thread,
            main.camel_service.synthesize_workforce_result,
            main.camel_service.replay_final_answer_as_stream,
            main.artifact_intent.classify_artifact_intent,
        ) = originals
    return job


def main_test() -> None:
    db.run_migrations()

    # --- The observed bug: one subtask never reached a terminal event ---
    job = _run_with_states(
        "job-orphan",
        {"t.1": "completed", "t.2": "running", "t.3": "queued"},
    )

    check(
        "a completed task is left alone",
        job["task_states"]["t.1"] == "completed",
        job["task_states"]["t.1"],
    )
    check(
        "a still-RUNNING task is reconciled to a terminal state",
        job["task_states"]["t.2"] == "failed",
        job["task_states"]["t.2"],
    )
    check(
        "a never-started QUEUED task is reconciled too",
        job["task_states"]["t.3"] == "failed",
        job["task_states"]["t.3"],
    )
    check(
        "no task remains non-terminal after finalization",
        not [s for s in job["task_states"].values() if s in {"queued", "running"}],
        str(job["task_states"]),
    )
    check(
        "the orphaned task gets an explanatory output",
        "did not finish" in job["task_outputs"].get("t.2", ""),
        job["task_outputs"].get("t.2", ""),
    )

    # --- Reconciliation must not invent failures on a clean run ---
    clean = _run_with_states("job-clean", {"t.1": "completed", "t.2": "completed"})
    check(
        "a fully-completed run is untouched",
        list(clean["task_states"].values()) == ["completed", "completed"],
        str(clean["task_states"]),
    )
    check(
        "a clean run gets no invented task output",
        not clean["task_outputs"],
        str(clean["task_outputs"]),
    )

    # --- A cancelled run reports cancelled, not failed ---
    cancelled = _run_with_states(
        "job-cancelled", {"t.1": "running"}, cancelled=True
    )
    check(
        "an orphan on a cancelled job is marked cancelled",
        cancelled["task_states"]["t.1"] == "cancelled",
        cancelled["task_states"]["t.1"],
    )

    # --- An existing task output must not be overwritten ---
    job_keep = _run_with_states(
        "job-keep",
        {"t.1": "running"},
        outputs={"t.1": "partial work produced before the stall"},
    )
    check(
        "an existing partial output is preserved, not replaced",
        job_keep["task_outputs"]["t.1"] == "partial work produced before the stall",
        job_keep["task_outputs"]["t.1"],
    )

    # Rehydrated jobs (backend restart) live in _JOBS with no _JobControl.
    # Stop used to 404 "Unknown job_id" even though GET /jobs/{id} worked.
    orphan_id = "rehydrated-interrupted-job"
    _make_job(orphan_id)
    main._JOBS[orphan_id]["status"] = "interrupted"
    main._JOBS[orphan_id]["error"] = (
        "Server restarted while this job was in progress; it did not complete."
    )
    del main._JOB_CONTROLS[orphan_id]
    snapshot = main.cancel_job(orphan_id)
    check(
        "cancel of a rehydrated interrupted job returns the snapshot, not 404",
        snapshot.job_id == orphan_id and snapshot.status == "interrupted",
        f"got {snapshot.status!r}",
    )
    check(
        "cancel of a rehydrated job does not rewrite it as cancelled",
        snapshot.status == "interrupted" and not snapshot.cancellation_requested,
    )

    unknown_404 = False
    try:
        main.cancel_job("definitely-not-a-job")
    except HTTPException as exc:
        unknown_404 = exc.status_code == 404 and "Unknown job_id" in str(exc.detail)
    check("cancel of a truly unknown job_id still 404s", unknown_404)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All task-state finalization checks passed")


if __name__ == "__main__":
    main_test()
