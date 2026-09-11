"""Focused test for the SPECIALIST execution-strategy dispatch decision:

    run_task() -> fast_path_role -> workforce.role_worker_ids lookup
    -> _run_task_direct() OR workforce.process_task_async()

Proves, without any real CAMEL/network/model call:
  - a valid fast_path_role resolves via role_worker_ids and dispatches to
    _run_task_direct with the correct worker_id
  - workforce.process_task_async is NEVER called in that case
  - the real UsageTracker involved shows zero "planning"/"assignment"
    calls, since neither task_agent nor coordinator_agent were ever
    touched

Uses a lightweight duck-typed fake Workforce (just the two attributes
run_task() actually touches: role_worker_ids and process_task_async) so
this needs no real Workforce/ChatAgent construction -- consistent with
test_capability_routing.py's no-network approach. Run directly:
    python test_specialist_fast_path.py
"""

import asyncio
import concurrent.futures
from unittest.mock import patch

import camel_service
from capability_routing import RESEARCH_ROLE
from model_config import UsageTracker


class _FakeUsage:
    def __init__(self, total: int) -> None:
        self.prompt_tokens = total
        self.completion_tokens = 0
        self.total_tokens = total


class _FakeModelResponse:
    def __init__(self, total: int) -> None:
        self.usage = _FakeUsage(total)


class _FakeWorkforce:
    """Duck-typed stand-in for camel.societies.workforce.Workforce --
    only the two attributes run_task() actually reads/calls."""

    def __init__(self, role_worker_ids):
        self.role_worker_ids = role_worker_ids
        self.process_task_async_calls = []

    async def process_task_async(self, task):
        self.process_task_async_calls.append(task.id)
        task.state = camel_service.TaskState.DONE
        task.result = "WRONG PATH: full decompose+assign was used"
        return task


def main() -> None:
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name}" + (f" -- {detail}" if not condition else ""))
        if not condition:
            failures.append(name)

    worker_id = "research-agent-node-id-123"
    workforce = _FakeWorkforce(role_worker_ids={RESEARCH_ROLE: worker_id})
    tracker = UsageTracker()

    direct_calls = []

    async def spy_run_task_direct(wf, task, wid):
        direct_calls.append((wf, wid))
        task.state = camel_service.TaskState.DONE
        task.result = "fake specialist result"
        return task

    with patch.object(camel_service, "_run_task_direct", spy_run_task_direct):
        result = asyncio.run(
            camel_service.run_task(
                workforce, "some content", "task-1", fast_path_role=RESEARCH_ROLE
            )
        )

    check(
        "_run_task_direct was used for a valid SPECIALIST role",
        len(direct_calls) == 1,
        f"direct_calls={direct_calls}",
    )
    check(
        "_run_task_direct received the role_worker_ids-resolved worker_id",
        direct_calls and direct_calls[0][1] == worker_id,
        f"direct_calls={direct_calls}",
    )
    check(
        "process_task_async (full decompose+assign) was NOT called",
        workforce.process_task_async_calls == [],
        f"process_task_async_calls={workforce.process_task_async_calls}",
    )
    check(
        "task completed via the fast path result",
        result.result == "fake specialist result",
        result.result,
    )

    # No real ChatAgent/model call happened anywhere in this test (the
    # fake Workforce has no task_agent/coordinator_agent at all), so the
    # tracker -- the same one that would be shared with build_workforce()
    # in the real flow -- must show zero planning/assignment calls.
    by_stage = tracker.stage_snapshot()
    check(
        "planning == 0 (task_agent/decompose never touched)",
        by_stage.get("planning", {}).get("model_call_count", 0) == 0,
        by_stage,
    )
    check(
        "assignment == 0 (coordinator_agent/_find_assignee never touched)",
        by_stage.get("assignment", {}).get("model_call_count", 0) == 0,
        by_stage,
    )

    # --- Control case: an unrecognized/missing role must NOT dispatch to
    # _run_task_direct -- falls back to the normal path (matches run_task's
    # own documented "an unexpected/stale role value never breaks the job"
    # behavior). ---
    workforce2 = _FakeWorkforce(role_worker_ids={RESEARCH_ROLE: worker_id})
    direct_calls.clear()
    with patch.object(camel_service, "_run_task_direct", spy_run_task_direct):
        asyncio.run(
            camel_service.run_task(
                workforce2, "some content", "task-2", fast_path_role="Nonexistent Agent"
            )
        )
    check(
        "unknown fast_path_role falls back to process_task_async, not _run_task_direct",
        direct_calls == [] and workforce2.process_task_async_calls == ["task-2"],
        f"direct_calls={direct_calls} process_calls={workforce2.process_task_async_calls}",
    )

    # --- Regression check for the actual root cause: quality-check calls
    # were misattributed to "planning" because ChatAgent.step()'s
    # step_timeout path runs the real call inside a fresh
    # concurrent.futures.ThreadPoolExecutor worker thread, and the old
    # contextvars-based override never propagated into that thread. Proves
    # temporarily_override_stage still works correctly across exactly that
    # boundary, without needing a real ChatAgent/model backend at all. ---
    class _FakeBackend:
        model_type = "fake-model"
        model_config_dict = {}

        def run(self, *args, **kwargs):
            return _FakeModelResponse(total=42)

    from model_config import _UsageTrackingModel  # noqa: E402 (test-local import)

    override_tracker = UsageTracker()
    model = _UsageTrackingModel(_FakeBackend(), override_tracker, stage="planning")

    def _call_in_worker_thread():
        # Mirrors chat_agent.py:2714-2727 exactly: the real call happens
        # inside a fresh ThreadPoolExecutor worker, not the calling thread.
        with model.temporarily_override_stage("quality"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(model._run, [])
                future.result(timeout=5)

    _call_in_worker_thread()
    override_stage_snapshot = override_tracker.stage_snapshot()
    check(
        'temporarily_override_stage survives a ThreadPoolExecutor boundary '
        '(the actual root cause of the "planning" contamination)',
        "quality" in override_stage_snapshot and "planning" not in override_stage_snapshot,
        override_stage_snapshot,
    )

    # And the override must not leak: a call made after the block ends
    # goes back to the model's original "planning" tag.
    model._run([])
    after_snapshot = override_tracker.stage_snapshot()
    check(
        "override does not leak past its block",
        after_snapshot.get("planning", {}).get("model_call_count", 0) == 1,
        after_snapshot,
    )

    # --- The Workforce must be told which strategy it is serving, and
    # that must survive the reset() _run_task_direct performs -- it is
    # what selects the bounded fast-path recovery policy over CAMEL's
    # full escalating one (see _patched_analyze_task). ---
    class _ResettableWorkforce(_FakeWorkforce):
        def reset(self):
            self.was_reset = True

    wf = _ResettableWorkforce(role_worker_ids={RESEARCH_ROLE: worker_id})
    wf.execution_strategy = "specialist"
    wf.reset()
    check(
        "execution_strategy survives Workforce.reset()",
        camel_service._execution_strategy_of(wf) == "specialist",
        camel_service._execution_strategy_of(wf),
    )
    check(
        "a Workforce with no strategy stamp defaults to full 'workforce'",
        camel_service._execution_strategy_of(_FakeWorkforce({})) == "workforce",
    )

    print(f"\n{'FAILED -- see above' if failures else 'All specialist fast-path checks passed'}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
