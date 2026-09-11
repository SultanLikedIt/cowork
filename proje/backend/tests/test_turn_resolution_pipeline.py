"""File bag + Direct-in-Workforce follow-up through run_pipeline.

Isolated DATA_DIR when this file is the first importer; under pytest it
reuses whichever DB the suite already opened and only needs unique ids.

    python test_turn_resolution_pipeline.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

# Never inherit DATA_DIR: run inside the container it points at /data, and
# this file's fixtures ("merhaba"/"selam", the Süper Lig standings, the
# notes.pdf follow-ups) then land in the live app database and surface in
# the user's chat history. conftest.py already forces isolation under
# pytest; this keeps the standalone `python test_turn_resolution_pipeline.py`
# path just as safe.
if os.environ.get("PYTEST_USE_REAL_DATA_DIR") != "1":
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="turn_resolution_pipe_")

import camel_service  # noqa: E402
import capability_routing  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
import turn_resolution  # noqa: E402
from camel.tasks.task import Task, TaskState  # noqa: E402

failures: list[str] = []


# main binds the execution facade at import time, so these tests patch
# main.run_single_agent_executor / main.run_workforce_in_thread_executor
# rather than camel_service directly. Patching camel_service made the tests
# quietly executor-dependent: with AGENT_EXECUTOR=langgraph -- the setting the
# app actually ships with -- the adapter routes past camel_service, the fake
# never ran, and the assertions failed on an empty `seen` dict. What these
# tests measure (does a follow-up go Direct instead of starting Workforce)
# is a routing decision made before either executor is chosen, so the seam
# they hold has to be the one main really calls.


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        raise AssertionError(f"{name} -- {detail}")


def _new_conversation() -> tuple[str, str]:
    workspace_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    now = time.time()
    db.create_workspace(workspace_id, name=None, created_at=now)
    db.create_conversation(conversation_id, workspace_id, "Thread", now)
    return workspace_id, conversation_id


def _seed_upload(filename: str = "notes.pdf") -> dict:
    file_id = str(uuid.uuid4())
    path = Path(os.environ["DATA_DIR"]) / "uploads" / f"{file_id}-{filename}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 fake")
    record = {
        "file_id": file_id,
        "filename": filename,
        "path": str(path),
        "kind": "file",
        "upload_size_bytes": path.stat().st_size,
        "extracted_text": "hello from pdf",
        "workspace_files": [filename],
        "ignored_file_count": 0,
    }
    main._UPLOAD_STORE[file_id] = record
    return record


async def _drain() -> None:
    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


async def _fake_run_single_agent(
    content,
    tracker,
    task_id,
    queue,
    request_topic=None,
    tool_binding_source="",
    **kwargs,
):
    _fake_run_single_agent.seen = {
        "content": content,
        "request_topic": request_topic,
        "tool_binding_source": tool_binding_source,
    }
    return Task(
        content=content,
        id=task_id,
        state=TaskState.DONE,
        result="mocked follow-up",
    )


def _fake_workforce(*args, **kwargs):
    _fake_workforce.called = True
    _fake_workforce.args = args
    _fake_workforce.kwargs = kwargs
    original = kwargs.get("original_request")
    if original is None and len(args) >= 11:
        original = args[10]
    _fake_workforce.original_request = original
    return Task(
        content=args[3] if len(args) > 3 else "",
        id=args[4] if len(args) > 4 else "t",
        state=TaskState.DONE,
        result="mocked workforce",
    )


async def _fake_synth(task, topic, tracker, **kwargs):
    _fake_synth.topic = topic
    return task.result, False


async def _fake_replay(text, task_id, queue):
    await queue.put({"event_type": "token_delta", "task_id": task_id, "delta": text})


async def _fake_intent(topic, tracker, persisted=None, attachment_names=None):
    import artifact_intent as ai

    return ai.fallback_from_regex(topic)


def test_file_bag_drops_oldest() -> None:
    db.run_migrations()
    workspace_id, conversation_id = _new_conversation()
    for index in range(11):
        db.add_conversation_files(
            conversation_id,
            [{"file_id": f"f{index}", "filename": f"{index}.pdf"}],
            time.time() + index,
        )
    rows = db.list_conversation_files(conversation_id)
    ids = [row["file_id"] for row in rows]
    check("bag cap is 10", len(ids) == 10, ids)
    check("oldest dropped", "f0" not in ids, ids)
    check("newest kept", "f10" in ids, ids)


def test_user_message_annotation() -> None:
    check(
        "no files leaves topic alone",
        main._user_message_content("özetle", []) == "özetle",
    )
    check(
        "filenames are recorded on the user message",
        main._user_message_content(
            "özetle", [{"filename": "a.pdf"}, {"filename": "b.xlsx"}]
        )
        == "özetle\n\n[attached: a.pdf, b.xlsx]",
    )


def test_first_turn_persists_bag_and_annotates() -> None:
    db.run_migrations()
    record = _seed_upload("report.pdf")
    _fake_workforce.called = False

    async def _run() -> None:
        with patch.object(
            main, "run_workforce_in_thread_executor", _fake_workforce
        ), patch.object(
            camel_service, "synthesize_workforce_result", _fake_synth
        ), patch.object(
            camel_service, "replay_final_answer_as_stream", _fake_replay
        ), patch.object(
            main.artifact_intent, "classify_artifact_intent", _fake_intent
        ), patch.object(
            main.turn_resolution, "resolve_turn", side_effect=AssertionError("no LLM")
        ):
            response = await main.run_pipeline(
                main.RunPipelineRequest(
                    topic="bu raporu özetle",
                    mode="multi",
                    file_ids=[record["file_id"]],
                )
            )
            await _drain()
            return response

    response = asyncio.run(_run())
    job = main._JOBS[response.job_id]
    bag = db.list_conversation_files(response.conversation_id)
    messages = db.load_recent_messages(response.conversation_id)
    user_text = next(m["content"] for m in messages if m["role"] == "user")
    check("first Workforce turn stays workforce", job["usage"]["execution_strategy"] == "workforce")
    check("first turn skips resolver LLM", job["usage"]["turn_resolution_source"] == "first_turn")
    check("topic stays raw", job["topic"] == "bu raporu özetle")
    check("bag stored the upload", bag[0]["file_id"] == record["file_id"], bag)
    check("user message names the file", "[attached: report.pdf]" in user_text, user_text)
    check("resolved equals topic on first turn", job["usage"]["resolved_request"] == "bu raporu özetle")


def test_direct_follow_up_in_workforce_thread() -> None:
    db.run_migrations()
    workspace_id, conversation_id = _new_conversation()
    now = time.time()
    db.insert_message(
        workspace_id, conversation_id, None, "user",
        "Süper Lig 2026 puan durumu", now,
    )
    db.insert_message(
        workspace_id, conversation_id, None, "assistant",
        "Galatasaray, Fenerbahçe, Beşiktaş", now + 1,
    )
    resolved = "Süper Lig 2026 puan durumu: ilk üçü kim?"
    _fake_run_single_agent.seen = {}
    _fake_workforce.called = False

    async def fake_resolve(topic, *, tracker, prior_messages, catalog):
        check("resolver sees prior turns", bool(prior_messages))
        return turn_resolution.TurnResolution(
            resolved, False, (), "llm"
        )

    async def _run() -> None:
        with patch.object(
            main.turn_resolution, "resolve_turn", fake_resolve
        ), patch.object(
            main, "run_single_agent_executor", _fake_run_single_agent
        ), patch.object(
            main, "run_workforce_in_thread_executor", _fake_workforce
        ):
            response = await main.run_pipeline(
                main.RunPipelineRequest(
                    topic="ilk üçü kim?",
                    mode="multi",
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                )
            )
            await _drain()
            return response

    response = asyncio.run(_run())
    job = main._JOBS[response.job_id]
    usage = job["usage"]
    check("composer mode stays multi", job["mode"] == "multi")
    check("follow-up runs Direct", usage["execution_strategy"] == "direct")
    check(
        "routing outcome is turn_resolution",
        usage["routing_outcome"] == capability_routing.ROUTING_TURN_RESOLUTION,
        usage["routing_outcome"],
    )
    check(
        "binding source is conversation_followup",
        usage["tool_binding_source"] == "conversation_followup",
    )
    check("Workforce was not started", _fake_workforce.called is False)
    check(
        "single-agent request_topic is resolved",
        _fake_run_single_agent.seen.get("request_topic") == resolved,
        _fake_run_single_agent.seen,
    )
    check(
        "content leads with resolved request",
        str(_fake_run_single_agent.seen.get("content") or "").startswith(resolved),
        _fake_run_single_agent.seen.get("content"),
    )


def test_single_never_calls_resolver_or_upgrades() -> None:
    db.run_migrations()
    workspace_id, conversation_id = _new_conversation()
    now = time.time()
    db.insert_message(
        workspace_id, conversation_id, None, "user", "merhaba", now
    )
    db.insert_message(
        workspace_id, conversation_id, None, "assistant", "selam", now + 1
    )
    _fake_run_single_agent.seen = {}

    async def boom(*args, **kwargs):
        raise AssertionError("resolve_turn must not run for Single")

    async def _run() -> None:
        with patch.object(
            main.turn_resolution, "resolve_turn", boom
        ), patch.object(
            main, "run_single_agent_executor", _fake_run_single_agent
        ):
            response = await main.run_pipeline(
                main.RunPipelineRequest(
                    topic="bunu kısalt",
                    mode="single",
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                )
            )
            await _drain()
            return response

    response = asyncio.run(_run())
    job = main._JOBS[response.job_id]
    check("Single stays single", job["usage"]["execution_strategy"] == "single")
    check("Single source recorded", job["usage"]["turn_resolution_source"] == "single_mode")
    check("Single does not upgrade ui_mode", job["usage"]["ui_mode"] == "single")


def test_rebind_from_bag_keeps_workforce() -> None:
    db.run_migrations()
    workspace_id, conversation_id = _new_conversation()
    record = _seed_upload("notes.pdf")
    now = time.time()
    db.add_conversation_files(
        conversation_id,
        [{"file_id": record["file_id"], "filename": record["filename"]}],
        now,
    )
    db.insert_message(
        workspace_id, conversation_id, None, "user",
        f"özetle\n\n[attached: {record['filename']}]", now,
    )
    db.insert_message(
        workspace_id, conversation_id, None, "assistant", "kısa özet", now + 1
    )
    resolved = f"{record['filename']} dosyasını önceki özetle karşılaştır"
    _fake_workforce.called = False

    async def fake_resolve(topic, *, tracker, prior_messages, catalog):
        ids = [item["file_id"] for item in catalog]
        check("catalog includes bag file", record["file_id"] in ids, catalog)
        return turn_resolution.TurnResolution(
            resolved, True, (record["file_id"],), "llm"
        )

    async def _run() -> None:
        with patch.object(
            main.turn_resolution, "resolve_turn", fake_resolve
        ), patch.object(
            main, "run_workforce_in_thread_executor", _fake_workforce
        ), patch.object(
            camel_service, "synthesize_workforce_result", _fake_synth
        ), patch.object(
            camel_service, "replay_final_answer_as_stream", _fake_replay
        ), patch.object(
            main.artifact_intent, "classify_artifact_intent", _fake_intent
        ):
            response = await main.run_pipeline(
                main.RunPipelineRequest(
                    topic="öncekiyle karşılaştır",
                    mode="multi",
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                )
            )
            await _drain()
            return response

    response = asyncio.run(_run())
    job = main._JOBS[response.job_id]
    check("file rebind stays Workforce", job["usage"]["execution_strategy"] == "workforce")
    check("no file_ids still attached the bag file", bool(job.get("attachments")), job.get("attachments"))
    check(
        "original_request is resolved",
        _fake_workforce.original_request == resolved,
        _fake_workforce.original_request,
    )
    check("synthesis saw resolved request", _fake_synth.topic == resolved, _fake_synth.topic)
    check("raw topic unchanged", job["topic"] == "öncekiyle karşılaştır")


def main_suite() -> None:
    test_file_bag_drops_oldest()
    test_user_message_annotation()
    test_first_turn_persists_bag_and_annotates()
    test_direct_follow_up_in_workforce_thread()
    test_single_never_calls_resolver_or_upgrades()
    test_rebind_from_bag_keeps_workforce()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All turn-resolution pipeline checks passed")


if __name__ == "__main__":
    main_suite()
