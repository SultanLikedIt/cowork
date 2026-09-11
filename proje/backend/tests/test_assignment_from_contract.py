"""Deterministic assignment from subtask contracts, not coordinator prose."""

from __future__ import annotations

import asyncio

import camel_service
import capabilities
from camel.societies.workforce.utils import TaskAssignment, TaskAssignResult
from camel.tasks import Task


def _task(task_id: str, goal: str, caps, *, deps=None, targets=None) -> Task:
    root = Task(id="root", content=goal)
    return Task(
        id=task_id,
        content=goal,
        parent=root,
        additional_info=camel_service._contract_for_subtask(
            root, goal, caps, deps or [], targets or []
        ),
    )


class _Workforce:
    role_worker_ids = {
        "Research Agent": "research-1",
        "Document Agent": "document-1",
    }
    _task_dependencies: dict = {}

    def __init__(self) -> None:
        self._task_dependencies = {}
        self.fallbacks: list[str] = []

    def _update_task_dependencies_from_assignments(self, assignments, tasks):
        for assignment in assignments:
            self._task_dependencies[assignment.task_id] = list(
                assignment.dependencies or []
            )

    async def _handle_task_assignment_fallbacks(self, tasks):
        self.fallbacks.extend(task.id for task in tasks)
        return [
            TaskAssignment(
                task_id=task.id,
                assignee_id=f"dynamic-{task.id}",
                dependencies=[],
            )
            for task in tasks
        ]


def test_research_and_document_skip_the_coordinator() -> None:
    research = _task(
        "root.1",
        "Gather Istanbul weather",
        {capabilities.WEB_SEARCH},
        targets=["İstanbul"],
    )
    artifact = _task(
        "root.2",
        "Create the Excel file",
        {capabilities.ARTIFACTS},
        deps=["root.1"],
        targets=["İstanbul"],
    )
    workforce = _Workforce()
    original = camel_service._ORIGINAL_FIND_ASSIGNEE
    called = []

    async def coordinator(self, tasks):
        called.append([task.id for task in tasks])
        return TaskAssignResult(assignments=[])

    camel_service._ORIGINAL_FIND_ASSIGNEE = coordinator
    try:
        result = asyncio.run(
            camel_service._patched_find_assignee(workforce, [research, artifact])
        )
    finally:
        camel_service._ORIGINAL_FIND_ASSIGNEE = original

    by_id = {item.task_id: item for item in result.assignments}
    assert called == []
    assert workforce.fallbacks == []
    assert by_id["root.1"].assignee_id == "research-1"
    assert by_id["root.2"].assignee_id == "document-1"
    assert by_id["root.2"].dependencies == ["root.1"]
    assert workforce._last_assignment_source == "contract"


def test_technical_domain_creates_a_specialist_without_coordinator() -> None:
    task = Task(
        id="t-api",
        content=(
            "Research and summarize current best practices, common production "
            "risks, and mitigation strategies for API reliability."
        ),
    )
    workforce = _Workforce()
    original = camel_service._ORIGINAL_FIND_ASSIGNEE
    called = []

    async def coordinator(self, tasks):
        called.append(True)
        return TaskAssignResult(assignments=[])

    camel_service._ORIGINAL_FIND_ASSIGNEE = coordinator
    try:
        result = asyncio.run(camel_service._patched_find_assignee(workforce, [task]))
    finally:
        camel_service._ORIGINAL_FIND_ASSIGNEE = original

    assert called == []
    assert workforce.fallbacks == ["t-api"]
    assert result.assignments[0].assignee_id == "dynamic-t-api"


def test_web_and_artifact_on_one_task_asks_the_coordinator() -> None:
    mixed = _task(
        "root.1",
        "Research and write the spreadsheet",
        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
    )
    workforce = _Workforce()
    original = camel_service._ORIGINAL_FIND_ASSIGNEE
    called = []

    async def coordinator(self, tasks):
        called.append([task.id for task in tasks])
        return TaskAssignResult(
            assignments=[
                TaskAssignment(
                    task_id=tasks[0].id,
                    assignee_id="research-1",
                    dependencies=[],
                )
            ]
        )

    camel_service._ORIGINAL_FIND_ASSIGNEE = coordinator
    try:
        asyncio.run(camel_service._patched_find_assignee(workforce, [mixed]))
    finally:
        camel_service._ORIGINAL_FIND_ASSIGNEE = original

    assert called == [["root.1"]]
    assert workforce._last_assignment_source == "coordinator"
