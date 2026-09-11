"""Deterministic 5-job Direct/Workforce concurrency and state-isolation test."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="job_concurrency_")

import db  # noqa: E402
import main  # noqa: E402
from camel.tasks import Task  # noqa: E402
from camel.tasks.task import TaskState  # noqa: E402


async def main_test():
    db.run_migrations()

    async def direct(content, tracker, task_id, queue, **kwargs):
        await asyncio.sleep(0.02)
        task = Task(content=content, id=task_id, state=TaskState.DONE, result=f"direct:{task_id}")
        return task

    def workforce(callback, tracker, recorder, content, task_id, *args):
        time.sleep(0.04)
        return Task(content=content, id=task_id, state=TaskState.DONE, result=f"workforce:{task_id}")

    async def synthesize(task, topic, tracker, skills=None, job_capabilities=None, **kwargs):
        return task.result, False

    async def replay(text, task_id, queue):
        await queue.put({"event_type": "token_delta", "task_id": task_id, "delta": text})

    originals = (
        main.camel_service.run_single_agent,
        main.camel_service.run_workforce_in_thread,
        main.camel_service.synthesize_workforce_result,
        main.camel_service.replay_final_answer_as_stream,
        main.artifact_intent.classify_artifact_intent,
    )
    main.camel_service.run_single_agent = direct
    main.camel_service.run_workforce_in_thread = workforce
    main.camel_service.synthesize_workforce_result = synthesize
    main.camel_service.replay_final_answer_as_stream = replay

    async def intent(topic, tracker, *, persisted=None):
        import artifact_intent as ai

        return ai.fallback_from_regex(topic)

    main.artifact_intent.classify_artifact_intent = intent
    try:
        requests = [
            main.RunPipelineRequest(topic="direct-a", mode="single"),
            main.RunPipelineRequest(topic="workforce-b", mode="multi"),
            main.RunPipelineRequest(topic="simple-c", mode="multi"),
            main.RunPipelineRequest(topic="workforce-d", mode="multi"),
            main.RunPipelineRequest(topic="direct-e", mode="single"),
        ]
        responses = await asyncio.gather(*(main.run_pipeline(req) for req in requests))
        background = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if background:
            await asyncio.gather(*background)
    finally:
        (
            main.camel_service.run_single_agent,
            main.camel_service.run_workforce_in_thread,
            main.camel_service.synthesize_workforce_result,
            main.camel_service.replay_final_answer_as_stream,
            main.artifact_intent.classify_artifact_intent,
        ) = originals

    ids = [response.job_id for response in responses]
    jobs = [main._JOBS[job_id] for job_id in ids]
    checks = {
        "five distinct concurrent jobs created": len(set(ids)) == 5,
        "all concurrent jobs terminal": all(job["status"] == "done" for job in jobs),
        "Single and Workforce overlapped safely": {job["usage"]["execution_strategy"] for job in jobs} == {"single", "workforce"},
        "every multi request stayed Workforce": all(
            job["usage"]["execution_strategy"] == "workforce"
            for job in jobs
            if job["mode"] == "multi"
        ),
        "results stay associated with their own job": all(job["job_id"] in job["result"] for job in jobs),
        "job controls remain per-job": len({id(main._JOB_CONTROLS[job_id]) for job_id in ids}) == 5,
        "stream state never crosses jobs": all(job.get("streaming_text", "") in {"", job["result"]} for job in jobs),
        "persisted reload contains every job": set(ids) <= {job["job_id"] for job in db.load_all_jobs()},
    }
    for name, passed in checks.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise SystemExit(f"Concurrent job checks failed: {failed}")
    print("\nAll deterministic concurrent job checks passed.")


if __name__ == "__main__":
    asyncio.run(main_test())
