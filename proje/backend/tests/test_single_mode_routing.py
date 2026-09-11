"""Proves explicit Single mode never calls a routing LLM.

Requirement: mode == "single" must go straight to run_single_agent --
no execution router, and no "routing" usage stage.

Uses an isolated temp DATA_DIR (own sqlite db/logs) so this never touches
the real dev database, and mocks camel_service.run_single_agent (returns a
real, cheaply-constructed Task -- no network/model access) so this needs
no server and no model backend. Run directly:
    python test_single_mode_routing.py
"""

import asyncio
import os
import tempfile
from unittest.mock import patch

# Isolated DB/log directory, set BEFORE importing main/db so their
# module-level DATA_DIR reads pick it up.
_tmp_dir = tempfile.mkdtemp(prefix="agent_webapp_test_")
os.environ["DATA_DIR"] = _tmp_dir

import db  # noqa: E402
import main  # noqa: E402
import camel_service  # noqa: E402
import capabilities  # noqa: E402
from camel.tasks.task import Task, TaskState  # noqa: E402


async def _fake_run_single_agent(
    content,
    tracker,
    task_id,
    queue,
    skills=None,
    workspace_root=None,
    tool_call_recorder=None,
    job_capabilities=None,
    profile=None,
    request_topic=None,
    tool_binding_source="",
    on_tools_bound=None,
    **kwargs,
):
    if on_tools_bound is not None:
        bound = capabilities.resolve_tools(
            job_capabilities,
            capabilities.ToolContext(workspace_root=workspace_root),
        )
        on_tools_bound(bound, tool_binding_source)
    return Task(content=content, id=task_id, state=TaskState.DONE, result="mocked single-agent answer")


async def _run() -> None:
    db.run_migrations()

    with patch.object(camel_service, "run_single_agent", _fake_run_single_agent):
        req = main.RunPipelineRequest(topic="What is the capital of France?", mode="single")
        response = await main.run_pipeline(req)

        # Let the background _run_job task (created via asyncio.create_task
        # inside run_pipeline) actually finish before asserting on job state.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

        job = main._JOBS[response.job_id]
        usage = job["usage"]

        checks = [
            ("job completed via Single", job["status"] == "done"),
            ("execution_strategy == 'single'", usage.get("execution_strategy") == "single"),
            (
                "plain Single request has no inferred tool capabilities",
                not (usage.get("bound_capabilities") or []),
            ),
            (
                "binding source is explicit",
                usage.get("tool_binding_source") == "single_chatbot",
            ),
            (
                "plain Single request binds no tools",
                not (usage.get("bound_tools") or []),
            ),
            (
                "no 'routing' stage recorded",
                "routing" not in (usage.get("by_stage") or {}),
            ),
        ]

        failures = [name for name, ok in checks if not ok]
        for name, ok in checks:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}")

        if failures:
            raise SystemExit(1)
        print("\nAll single-mode routing checks passed (no execution router).")


if __name__ == "__main__":
    asyncio.run(_run())
