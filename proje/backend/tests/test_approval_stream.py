"""The browser sees the approval card on the live stream, not only after a reload.

Live bug: the job was marked "done" before the Jira / Confluence proposal was
prepared. The last typing-replay events were drained while the proposal step
awaited, so a snapshot with status "done" -- and no card, no notice line --
reached the browser first. The UI stops listening at the first terminal
status (App.jsx), so the final snapshot that carried the card was never read.
"""

from __future__ import annotations

import asyncio
import time

import db
import main
from execution.result import execution_result
from workforce.events import TokenDeltaEvent

db.run_migrations()

TERMINAL = {"done", "partial", "failed", "error", "cancelled"}


def _make_job(job_id: str) -> dict:
    main._JOBS[job_id] = {
        "job_id": job_id,
        "topic": "KAN-2'ye yorum ekle: ok",
        "status": "queued",
        "mode": "multi",
        "result": "",
        "error": None,
        "created_at": 0.0,
        "started_at": None,
        "duration_seconds": None,
        "usage": {
            "execution_strategy": "workforce",
            "jira_write_action": "add_comment",
            "resolved_request": "KAN-2'ye yorum ekle: ok",
        },
        "task_states": {},
        "task_outputs": {},
        "task_attempts": {},
        "task_plan": [],
        "cancellation_requested": False,
        "cancellation_note": None,
        "workspace_id": None,
        "conversation_id": None,
        "workspace_root": None,
        "origin": "user",
    }
    main._JOB_CONTROLS[job_id] = main._JobControl(job_id)
    main._JOB_INDEX[job_id] = {}
    main._WORKER_ROLES[job_id] = {}
    main._TIMING[job_id] = {"tasks": {}, "root_created": None, "first_worker_started": None}
    db.save_job(main._JOBS[job_id])
    return main._JOBS[job_id]


def test_first_terminal_snapshot_already_carries_the_card(monkeypatch) -> None:
    job_id = "stream-approval-1"
    _make_job(job_id)

    def fake_workforce(*args, **kwargs):
        return execution_result(task_id=job_id, content="c", result="Onayınıza hazır.")

    async def fake_replay(text, task_id, queue):
        # Like the real replay's last chunk: queued, then no await before
        # _run_job moves on -- so it is drained during the proposal step.
        queue.put_nowait(TokenDeltaEvent(task_id=task_id, delta=text).model_dump(mode="json"))

    async def fake_intent(*args, **kwargs):
        import artifact_intent as ai

        return ai.ArtifactIntent(False, "none", "test")

    async def fake_propose(job, *, tracker=None, complete=None):
        await asyncio.sleep(0.2)  # the proposal's model call
        await asyncio.to_thread(
            db.insert_integration_action,
            "stream-action-1", job["job_id"], "jira", "add_comment",
            {"issue_key": "KAN-2", "body": "ok"}, time.time(),
        )
        return "🔧 Onayınızı bekliyor — KAN-2 kaydına yorum."

    async def no_proposal(job, *, tracker=None, complete=None):
        return None

    monkeypatch.setattr(main, "run_workforce_in_thread_executor", fake_workforce)
    monkeypatch.setattr(main, "replay_final_answer_as_stream", fake_replay)
    monkeypatch.setattr(main.artifact_intent, "classify_artifact_intent", fake_intent)
    monkeypatch.setattr(main.jira_actions, "propose_for_job", fake_propose)
    monkeypatch.setattr(main.confluence_actions, "propose_for_job", no_proposal)

    async def scenario() -> list[dict]:
        subscriber: asyncio.Queue = asyncio.Queue()
        main._JOB_CONTROLS[job_id].subscribers.append(subscriber)
        await main._run_job(job_id, "content", "multi")
        seen = []
        while not subscriber.empty():
            item = subscriber.get_nowait()
            if item is None:
                break
            seen.append(item)
        return seen

    snapshots = asyncio.run(scenario())
    first_terminal = next(s for s in snapshots if s["status"] in TERMINAL)
    assert first_terminal["status"] == "done"
    assert [a["action_id"] for a in first_terminal["integration_actions"]] == ["stream-action-1"]
    assert "Onayınızı bekliyor" in first_terminal["result"]
