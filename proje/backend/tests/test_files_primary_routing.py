"""FILES-primary vs ambient contract: planning, assignment, controller, completeness."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import camel_service
import capabilities
import workforce_plan
from camel.societies.workforce.utils import TaskAssignResult, TaskAssignment
from camel.tasks import Task


def _task(
    task_id: str,
    goal: str,
    caps,
    *,
    deps=None,
    targets=None,
    files_processed: bool = False,
) -> Task:
    root = Task(id="root", content=goal)
    additional_info = camel_service._contract_for_subtask(
        root, goal, caps, deps or [], targets or []
    )
    if files_processed:
        contract = dict(additional_info[camel_service.WORKFORCE_INTENT_KEY])
        contract["files_processed"] = True
        additional_info[camel_service.WORKFORCE_INTENT_KEY] = contract
    return Task(
        id=task_id,
        content=goal,
        parent=root,
        additional_info=additional_info,
    )


def test_planner_web_search_does_not_override_files_only_job() -> None:
    plan = workforce_plan.PlanResult(
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="summarize the upload",
                capabilities=["web_search"],
            )
        ],
        source="llm",
    )
    streams = workforce_plan.expand_plan_for_coverage(
        "bunu özetle",
        plan,
        {capabilities.FILES},
        max_subtasks=3,
    )
    assert len(streams) == 1
    assert streams[0].capabilities == frozenset({capabilities.FILES})
    assert not (
        streams[0].capabilities & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
    )


def test_web_plus_files_keeps_files_ambient_not_primary_stream() -> None:
    plan = workforce_plan.PlanResult(
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="research topic",
                capabilities=["web_search"],
            )
        ],
        source="llm",
    )
    streams = workforce_plan.expand_plan_for_coverage(
        "compare uploaded spreadsheet with live market data",
        plan,
        {capabilities.WEB_SEARCH, capabilities.FILES},
        max_subtasks=3,
    )
    web_streams = [
        stream
        for stream in streams
        if stream.capabilities & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
    ]
    files_primary = [
        stream for stream in streams if stream.capabilities == frozenset({capabilities.FILES})
    ]
    assert len(web_streams) == 1
    assert files_primary == []


def test_files_only_assignment_goes_to_document() -> None:
    task = _task("root.1", "Summarize the PDF", {capabilities.FILES})
    assert camel_service._role_for_subtask(task) == "Document Agent"


def test_unknown_capability_asks_coordinator_not_research() -> None:
    task = _task("root.1", "Do something vague", set())
    assert camel_service._role_for_subtask(task) is None


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


def test_files_only_contract_assignment_skips_coordinator() -> None:
    task = _task("root.1", "Analyze the CSV", {capabilities.FILES})
    workforce = _Workforce()
    original = camel_service._ORIGINAL_FIND_ASSIGNEE
    called: list = []

    async def coordinator(self, tasks):
        called.append(True)
        return TaskAssignResult(assignments=[])

    camel_service._ORIGINAL_FIND_ASSIGNEE = coordinator
    try:
        result = asyncio.run(
            camel_service._patched_find_assignee(workforce, [task])
        )
    finally:
        camel_service._ORIGINAL_FIND_ASSIGNEE = original

    assert called == []
    assert result.assignments[0].assignee_id == "document-1"
    assert workforce._last_assignment_source == "contract"


def test_web_controller_not_bound_for_files_only_task() -> None:
    class _Agent:
        astep = object()
        _web_controller_wrapped = False
        _answers_when_out_of_turns = False

    worker = SimpleNamespace(
        _web_controller_cfg={"web_tools": object()},
        _current_task=_task("root.1", "Read PDF", {capabilities.FILES}),
    )
    original = camel_service._ORIGINAL_GET_WORKER_AGENT

    async def fake_get_worker_agent(_self):
        return _Agent()

    camel_service._ORIGINAL_GET_WORKER_AGENT = fake_get_worker_agent
    try:
        agent = asyncio.run(camel_service._patched_get_worker_agent(worker))
    finally:
        camel_service._ORIGINAL_GET_WORKER_AGENT = original

    assert getattr(agent, "_web_controller_wrapped", False) is False
    assert getattr(agent, "_answers_when_out_of_turns", False) is True


def test_web_controller_bound_for_web_task_on_research_node() -> None:
    class _Agent:
        def __init__(self) -> None:
            self.astep = lambda *a, **k: None
            self._web_controller_wrapped = False
            self._answers_when_out_of_turns = False

    worker = SimpleNamespace(
        _web_controller_cfg={
            "web_tools": SimpleNamespace(
                search=lambda *a, **k: [],
                read=lambda *a, **k: "",
            ),
            "tracker": camel_service.UsageTracker(),
            "request_text": "weather",
            "progress": lambda *a, **k: None,
            "current_date": "2026-01-01",
            "wants_artifact": False,
        },
        _current_task=_task(
            "root.1",
            "Gather weather",
            {capabilities.WEB_SEARCH},
            targets=["İstanbul"],
        ),
    )
    original = camel_service._ORIGINAL_GET_WORKER_AGENT

    async def fake_get_worker_agent(_self):
        return _Agent()

    camel_service._ORIGINAL_GET_WORKER_AGENT = fake_get_worker_agent
    try:
        agent = asyncio.run(camel_service._patched_get_worker_agent(worker))
    finally:
        camel_service._ORIGINAL_GET_WORKER_AGENT = original

    assert getattr(agent, "_web_controller_wrapped", False) is True


def test_files_primary_incomplete_without_processed_marker() -> None:
    task = _task(
        "root.1",
        "Summarize upload",
        {capabilities.FILES},
        files_processed=False,
    )
    task.result = "Here is a summary " * 20
    assert camel_service._result_is_usable(task)
    assert camel_service._files_responsibility_complete(task) is False
    assert camel_service._responsibility_complete(task) is False


def test_files_primary_complete_after_tool_activity() -> None:
    task = _task("root.1", "Summarize upload", {capabilities.FILES})
    token = camel_service._ACTIVE_WORKFORCE_SUBTASK.set(task)
    try:
        camel_service._mark_files_processed(task)
    finally:
        camel_service._ACTIVE_WORKFORCE_SUBTASK.reset(token)
    task.result = "Summary grounded in the file " * 10
    assert camel_service._files_responsibility_complete(task) is True
    assert camel_service._responsibility_complete(task) is True


def test_repair_stamps_files_ambient_only_on_web_plus_files() -> None:
    root = Task(
        id="root",
        content="compare upload with web sources",
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": "compare upload with web sources",
                "required_capabilities": sorted(
                    capabilities.expand(
                        {capabilities.WEB_SEARCH, capabilities.FILES}
                    )
                ),
            }
        },
    )
    subtask = Task(id="root.1", content="Research live data", parent=root)
    repaired = camel_service._repair_initial_plan_coverage(
        SimpleNamespace(),
        root,
        [subtask],
    )
    caps = camel_service._contract_capabilities(
        repaired[0], "task_capabilities"
    )
    assert capabilities.FILES in caps

    files_root = Task(
        id="root2",
        content="bunu özetle",
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": "bunu özetle",
                "required_capabilities": sorted(
                    capabilities.expand({capabilities.FILES})
                ),
            }
        },
    )
    files_sub = Task(
        id="root2.1",
        content="Read uploaded PDF",
        parent=files_root,
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "task_capabilities": sorted(
                    capabilities.expand({capabilities.FILES})
                ),
            }
        },
    )
    files_repaired = camel_service._repair_initial_plan_coverage(
        SimpleNamespace(),
        files_root,
        [files_sub],
    )
    assert capabilities.FILES in camel_service._contract_capabilities(
        files_repaired[0], "task_capabilities"
    )


def test_initial_decompose_files_only_produces_document_stream() -> None:
    plan = workforce_plan.PlanResult(
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="summarize",
                capabilities=["web_search"],
            )
        ],
        source="llm",
    )
    original = workforce_plan.plan_request
    workforce_plan.plan_request = lambda *a, **k: plan
    root = Task(
        id="root",
        content="bunu özetle",
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": "bunu özetle",
                "required_capabilities": sorted(
                    capabilities.expand({capabilities.FILES})
                ),
            }
        },
    )
    workforce = SimpleNamespace(_task_dependencies={}, task_agent=object())
    try:
        result = camel_service._patched_decompose_task(workforce, root)
    finally:
        workforce_plan.plan_request = original

    assert len(result) == 1
    caps = camel_service._contract_capabilities(result[0], "task_capabilities")
    assert caps == {capabilities.FILES}
    assert camel_service._role_for_subtask(result[0]) == "Document Agent"


def test_web_plus_files_decompose_still_researches() -> None:
    names = ["Market A", "Market B"]
    plan = workforce_plan.PlanResult(
        targets=[workforce_plan.PlanTarget(name=name) for name in names],
        workstreams=[
            workforce_plan.PlanWorkstream(
                capabilities=["web_search"],
                target_names=names,
            )
        ],
        source="llm",
    )
    original = workforce_plan.plan_request
    workforce_plan.plan_request = lambda *a, **k: plan
    root = Task(
        id="root",
        content="Compare uploaded sheet with live prices for A and B",
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": "Compare uploaded sheet with live prices",
                "required_capabilities": sorted(
                    capabilities.expand(
                        {capabilities.WEB_SEARCH, capabilities.FILES}
                    )
                ),
            }
        },
    )
    workforce = SimpleNamespace(_task_dependencies={}, task_agent=object())
    try:
        result = camel_service._patched_decompose_task(workforce, root)
    finally:
        workforce_plan.plan_request = original

    web_tasks = [
        task
        for task in result
        if camel_service._contract_capabilities(task, "task_capabilities")
        & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
    ]
    assert len(web_tasks) >= 1
    assert not any(
        camel_service._contract_capabilities(task, "task_capabilities")
        == {capabilities.FILES}
        for task in result
    )
    for task in web_tasks:
        assert capabilities.FILES in camel_service._contract_capabilities(
            task, "task_capabilities"
        )


def _assert_files_primary_routes_document(request_text: str) -> None:
    plan = workforce_plan.PlanResult(
        workstreams=[workforce_plan.PlanWorkstream(goal=request_text)],
        source="llm",
    )
    streams = workforce_plan.expand_plan_for_coverage(
        request_text,
        plan,
        {capabilities.FILES},
        max_subtasks=2,
    )
    assert len(streams) == 1
    assert streams[0].capabilities == frozenset({capabilities.FILES})
    task = _task("root.1", streams[0].goal, streams[0].capabilities)
    assert camel_service._role_for_subtask(task) == "Document Agent"


def test_pdf_summarize_routes_document() -> None:
    _assert_files_primary_routes_document("bunu özetle")


def test_docx_analyze_routes_document() -> None:
    _assert_files_primary_routes_document("analyze the attached DOCX report")


def test_xlsx_compare_routes_document() -> None:
    _assert_files_primary_routes_document(
        "compare the two uploaded XLSX workbooks"
    )


def test_csv_summarize_routes_document() -> None:
    _assert_files_primary_routes_document("summarize the CSV export")
