"""Decomposition → assignment → first worker post must actually happen.

Live Multi jobs completed structured planning (subtasks and targets present)
then finalized with queued children: assignment_ms=0, assignment_source=None,
no task_started. Cause: planning wrote child IDs into Workforce._task_dependencies,
which AUTO_DECOMPOSE treats as “already assigned” (workforce.py:3943-3955), so
_post_ready_tasks skipped _find_assignee and then KeyError'd on _assignees.

This drives the real CAMEL methods after our patches: handle_decompose_append_task
→ _post_ready_tasks → _post_task. No model, no worker loop.
"""

from __future__ import annotations

import asyncio
from collections import deque

import camel_service
import capabilities
import workforce_plan
from camel.societies.workforce.utils import TaskAssignResult
from camel.societies.workforce.workforce import Workforce, WorkforceMode
from camel.tasks import Task


class _Recorder:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.decomposed: list[str] = []
        self.assigned: list[tuple[str, str]] = []
        self.started: list[tuple[str, str]] = []

    def log_task_created(self, event) -> None:
        self.created.append(event.task_id)

    def log_task_decomposed(self, event) -> None:
        self.decomposed.append(event.parent_task_id)

    def log_task_assigned(self, event) -> None:
        self.assigned.append((event.task_id, event.worker_id))

    def log_task_started(self, event) -> None:
        self.started.append((event.task_id, event.worker_id))


class _Channel:
    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []

    async def get_task_by_id(self, task_id: str):
        raise KeyError(task_id)

    async def post_task(self, task, publisher_id, assignee_id) -> None:
        self.posted.append((task.id, assignee_id))


class _LifecycleWorkforce:
    """Minimal Workforce that still runs CAMEL's real dispatch methods."""

    handle_decompose_append_task = Workforce.handle_decompose_append_task
    _post_ready_tasks = Workforce._post_ready_tasks
    _post_task = Workforce._post_task
    _increment_in_flight_tasks = Workforce._increment_in_flight_tasks
    _update_task_dependencies_from_assignments = (
        Workforce._update_task_dependencies_from_assignments
    )
    _update_dependencies_for_decomposition = (
        Workforce._update_dependencies_for_decomposition
    )
    _decompose_task = Workforce._decompose_task

    def __init__(self) -> None:
        self.node_id = "lifecycle-workforce"
        self.mode = WorkforceMode.AUTO_DECOMPOSE
        self._pending_tasks: deque = deque()
        self._task_dependencies: dict = {}
        self._assignees: dict = {}
        self._completed_tasks: list = []
        self._children: list = []
        self._in_flight_tasks = 0
        self._task_start_times: dict = {}
        self._task = None
        self._callbacks = [_Recorder()]
        self._channel = _Channel()
        self.task_agent = None
        self.role_worker_ids = {
            "Research Agent": "research-1",
            "Document Agent": "document-1",
        }
        self.find_assignee_calls: list[list[str]] = []

    def reset(self) -> None:
        self._task = None
        self._pending_tasks.clear()
        self._task_dependencies.clear()
        self._assignees.clear()
        self._completed_tasks = []
        self._in_flight_tasks = 0
        self._task_start_times.clear()

    async def _find_assignee(self, tasks):
        self.find_assignee_calls.append([task.id for task in tasks])
        return await camel_service._patched_find_assignee(self, tasks)


def _player_plan(*_args, **_kwargs):
    names = ["Gabriel Sara", "Mauro Icardi"]
    return workforce_plan.PlanResult(
        targets=[workforce_plan.PlanTarget(name=name) for name in names],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="research both players",
                capabilities=["web_search"],
                target_names=names,
            )
        ],
        source="llm",
    )


def test_structured_plan_reaches_contract_assignment_and_execution() -> None:
    request = (
        "Gabriel Sara ve Mauro Icardi hakkında araştırma yapıp excel olarak ver"
    )
    root = Task(
        id="job-1",
        content=request,
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": request,
                "required_capabilities": sorted(
                    capabilities.expand(
                        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
                    )
                ),
            }
        },
    )
    workforce = _LifecycleWorkforce()
    coordinator_calls: list[list[str]] = []
    original_plan = workforce_plan.plan_request
    original_assignee = camel_service._ORIGINAL_FIND_ASSIGNEE

    async def coordinator(self, tasks):
        coordinator_calls.append([task.id for task in tasks])
        return TaskAssignResult(assignments=[])

    workforce_plan.plan_request = _player_plan
    camel_service._ORIGINAL_FIND_ASSIGNEE = coordinator
    try:
        subtasks = asyncio.run(workforce.handle_decompose_append_task(root))
        child_ids = [task.id for task in subtasks]
        assert len(subtasks) == 3
        assert all(task_id not in workforce._task_dependencies for task_id in child_ids)
        assert list(workforce._pending_tasks) == list(subtasks)

        asyncio.run(workforce._post_ready_tasks())
    finally:
        workforce_plan.plan_request = original_plan
        camel_service._ORIGINAL_FIND_ASSIGNEE = original_assignee

    recorder = workforce._callbacks[0]
    research = [
        task
        for task in subtasks
        if capabilities.ARTIFACTS
        not in camel_service._contract_capabilities(task, "task_capabilities")
    ]
    artifact = [
        task
        for task in subtasks
        if capabilities.ARTIFACTS
        in camel_service._contract_capabilities(task, "task_capabilities")
    ]
    assert len(research) == 2
    assert len(artifact) == 1
    assert coordinator_calls == []
    assert workforce.find_assignee_calls == [child_ids]
    assert workforce._last_assignment_source == "contract"
    assert set(workforce._assignees) == set(child_ids)
    assert workforce._assignees[research[0].id] == "research-1"
    assert workforce._assignees[research[1].id] == "research-1"
    assert workforce._assignees[artifact[0].id] == "document-1"
    assert workforce._task_dependencies[artifact[0].id] == [
        task.id for task in research
    ]
    started_ids = [task_id for task_id, _ in recorder.started]
    posted_ids = [task_id for task_id, _ in workforce._channel.posted]
    assert started_ids == [task.id for task in research]
    assert posted_ids == started_ids
    assert artifact[0].id not in started_ids
    snap = camel_service.orchestration_telemetry(workforce)
    assert snap["assignment_source"] == "contract"
    assert snap["assignment_ms"] >= 0
    assert snap["subtask_count"] == 3
    assert snap["targets_planned"] == ["Gabriel Sara", "Mauro Icardi"]
