"""Fixed-vs-dynamic worker matching is decided by bound tools, not wording.

A subtask is usually described more narrowly than the worker that should
run it ("extract the standings table" vs. a general web-research worker).
These tests pin the rule that closes that gap: a dynamic specialist is
created only when no existing worker holds a tool the subtask needs.
"""

from __future__ import annotations

import asyncio
import tempfile

import camel_service
import capabilities
from camel.tasks import Task


class _Assignment:
    def __init__(self, task_id, assignee_id):
        self.task_id = task_id
        self.assignee_id = assignee_id

    def model_copy(self, update=None):
        copy = _Assignment(self.task_id, self.assignee_id)
        for key, value in (update or {}).items():
            setattr(copy, key, value)
        return copy


class _AssignResult:
    def __init__(self, assignments):
        self.assignments = assignments

    def model_copy(self, update=None):
        return _AssignResult((update or {}).get("assignments", self.assignments))


class _Child:
    def __init__(self, node_id, description, worker):
        self.node_id = node_id
        self.description = description
        self.worker = worker


class _Workforce:
    """Only the surface the two patched assignment seams actually touch."""

    def __init__(self, children):
        self._children = children
        self.fallback_tasks = []

    async def _handle_task_assignment_fallbacks(self, tasks):
        self.fallback_tasks.append([task.id for task in tasks])
        return [_Assignment(task.id, "dynamic-worker-1") for task in tasks]


def _workforce(root: str, job_capabilities=None) -> _Workforce:
    """Build the real fixed workers, then wrap them as coordinator children."""
    built = {}

    class FakeWorkforce:
        def __init__(self, *args, **kwargs):
            self.coordinator_agent = kwargs["coordinator_agent"]
            self.task_agent = kwargs["task_agent"]
            self._children = []

        def add_single_agent_worker(self, description, worker=None, **kwargs):
            role = camel_service.WORKER_DISPLAY_NAMES.get(description, description)
            child = _Child(role.split()[0].lower(), description, worker)
            built[role] = child
            self._children.append(child)
            return self

    original = camel_service.Workforce
    camel_service.Workforce = FakeWorkforce
    try:
        camel_service.build_workforce(
            callback=object(),
            tracker=camel_service.UsageTracker(),
            workspace_root=root,
            job_capabilities=(
                frozenset(capabilities.ALL_CAPABILITIES)
                if job_capabilities is None
                else frozenset(job_capabilities)
            ),
            request_text="Use the supplied material to produce the requested output.",
        )
    finally:
        camel_service.Workforce = original
    return _Workforce(list(built.values()))


def _subtask(root: Task, content: str, required, task_id: str) -> Task:
    return Task(
        content=content,
        id=task_id,
        parent=root,
        additional_info=camel_service._contract_for_subtask(
            root, content, set(required), []
        ),
    )


def _assign(workforce, tasks, proposed):
    """Run the coordinator seam with `proposed` as the model's choice."""
    original = camel_service._ORIGINAL_CALL_COORDINATOR_FOR_ASSIGNMENT
    camel_service._ORIGINAL_CALL_COORDINATOR_FOR_ASSIGNMENT = (
        lambda self, tasks, invalid_ids=None: _AssignResult(
            [_Assignment(task.id, proposed[task.id]) for task in tasks]
        )
    )
    try:
        result = camel_service._patched_call_coordinator_for_assignment(
            workforce, tasks
        )
    finally:
        camel_service._ORIGINAL_CALL_COORDINATOR_FOR_ASSIGNMENT = original
    return {a.task_id: a.assignee_id for a in result.assignments}


def test_web_table_extraction_stays_with_the_research_agent() -> None:
    root = Task(
        content=(
            "trendyol süper lig güncel puan durumunu bulup excel olarak ver"
        ),
        id="root",
    )
    # The planner mentions the downstream Excel deliverable inside the
    # RESEARCH subtask. That wording must not make the research worker look
    # unfit for its own research task.
    research = _subtask(
        root,
        "Research and extract the current Trendyol Süper Lig standings table "
        "from a verifiable source and prepare the structured data for the "
        "Excel deliverable",
        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
        "research-task",
    )
    with tempfile.TemporaryDirectory() as tmp:
        workforce = _workforce(tmp)
        assigned = _assign(workforce, [research], {"research-task": "research"})
    assert assigned["research-task"] == "research"


def test_none_fit_research_task_falls_back_to_the_capable_fixed_worker() -> None:
    root = Task(content="find the current standings and export them", id="root")
    research = _subtask(
        root,
        "Extract the current league standings web table from the verified "
        "source and prepare structured evidence for the Excel deliverable",
        {capabilities.WEB_SEARCH},
        "research-task",
    )
    with tempfile.TemporaryDirectory() as tmp:
        workforce = _workforce(tmp)
        assignments = asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                workforce,
                [_Assignment("research-task", camel_service.NONE_FIT_SENTINEL)],
                [research],
                {child.node_id for child in workforce._children},
            )
        )
    assert [a.assignee_id for a in assignments] == ["research"]
    assert workforce.fallback_tasks == []


def test_research_and_artifact_split_across_the_two_fixed_workers() -> None:
    root = Task(content="find the standings and give me an excel", id="root")
    research = _subtask(
        root,
        "Research and verify the current standings from a live source",
        {capabilities.WEB_SEARCH},
        "research-task",
    )
    artifact = _subtask(
        root,
        "Create the requested XLSX from the prerequisite data_path",
        {capabilities.ARTIFACTS},
        "artifact-task",
    )
    with tempfile.TemporaryDirectory() as tmp:
        workforce = _workforce(tmp)
        assigned = _assign(
            workforce,
            [research, artifact],
            {"research-task": "research", "artifact-task": "document"},
        )
        # A miscast artifact assignment is repaired toward the worker that
        # actually holds the artifact tools, not bounced to a specialist.
        repaired = _assign(
            workforce,
            [artifact],
            {"artifact-task": "coding"},
        )
    assert assigned == {"research-task": "research", "artifact-task": "document"}
    assert repaired["artifact-task"] == "document"


def test_unsupported_engineering_task_still_creates_a_dynamic_worker() -> None:
    task = Task(
        content=(
            "Assess the Kubernetes production deployment topology and "
            "recommend a failover strategy for the cluster"
        ),
        id="infra-task",
    )
    with tempfile.TemporaryDirectory() as tmp:
        workforce = _workforce(tmp)
        assigned = _assign(workforce, [task], {"infra-task": "research"})
        assert assigned["infra-task"] == camel_service.NONE_FIT_SENTINEL

        assignments = asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                workforce,
                [_Assignment("infra-task", camel_service.NONE_FIT_SENTINEL)],
                [task],
                {child.node_id for child in workforce._children},
            )
        )
    assert [a.assignee_id for a in assignments] == ["dynamic-worker-1"]
    assert workforce.fallback_tasks == [["infra-task"]]


def test_missing_required_tools_still_reaches_none_fit() -> None:
    root = Task(content="produce the report file", id="root")
    artifact = _subtask(
        root,
        "Create the requested XLSX deliverable",
        {capabilities.ARTIFACTS},
        "artifact-task",
    )
    with tempfile.TemporaryDirectory() as tmp:
        # No worker is granted artifact tools in this job.
        workforce = _workforce(
            tmp, job_capabilities={capabilities.WEB_SEARCH, capabilities.FILES}
        )
        assigned = _assign(workforce, [artifact], {"artifact-task": "research"})

        assignments = asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                workforce,
                [_Assignment("artifact-task", camel_service.NONE_FIT_SENTINEL)],
                [artifact],
                {child.node_id for child in workforce._children},
            )
        )
    assert assigned["artifact-task"] == camel_service.NONE_FIT_SENTINEL
    assert [a.assignee_id for a in assignments] == ["dynamic-worker-1"]
    assert workforce.fallback_tasks == [["artifact-task"]]
