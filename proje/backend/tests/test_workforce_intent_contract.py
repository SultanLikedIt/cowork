"""Regression tests for Workforce intent, coverage, and handoff contracts."""

from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace

import camel_service
import capabilities
from camel.societies.workforce.utils import TaskAssignment
from camel.tasks import Task


DIYANET = (
    "diyanetten ankaranın namaz vakitlerini bulup excel olarak verir misin"
)
SUPER_LIG = "süper lig güncel puan durumunu bulup tablo halinde yazar mısın"
YAYLA = "yayla gıdanın freeze dried ürünlerini fiyatlarıyla bul"


def _root(original: str, required) -> Task:
    return Task(
        id="root",
        content=original,
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": original,
                "assigned_subtask_goal": original,
                "required_capabilities": sorted(capabilities.expand(required)),
                "task_capabilities": [],
                "dependency_task_ids": [],
            }
        },
    )


def test_diyanet_plan_is_repaired_with_dependent_artifact_task() -> None:
    root = _root(DIYANET, {capabilities.ARTIFACTS})
    research = Task(
        id="root.0",
        content="Obtain and verify Ankara daily prayer times from Diyanet",
        parent=root,
        additional_info=root.additional_info,
    )
    root.subtasks = [research]
    workforce = SimpleNamespace(_task_dependencies={})

    repaired = camel_service._repair_initial_plan_coverage(
        workforce, root, [research]
    )

    assert len(repaired) == 2
    artifact = repaired[1]
    contract = camel_service._workforce_contract(artifact)
    assert capabilities.ARTIFACTS in capabilities.expand(
        contract["task_capabilities"]
    )
    assert contract["dependency_task_ids"] == [research.id]
    # CAMEL AUTO_DECOMPOSE uses presence in _task_dependencies as the
    # "already assigned" sentinel. Coverage repair must keep the DAG on
    # the intent contract and leave that map for _post_ready_tasks.
    assert artifact.id not in workforce._task_dependencies
    assert research.id not in workforce._task_dependencies
    assert "data_path" in artifact.content
    assert "do not repeat web research" in artifact.content


def test_root_contract_keeps_raw_original_separate_from_combined_content() -> None:
    captured: dict[str, Task] = {}

    class Workforce:
        original_request = SUPER_LIG
        requested_capabilities = frozenset({capabilities.WEB_SEARCH})
        role_worker_ids = {}

        async def process_task_async(self, task):
            captured["task"] = task
            return task

    asyncio.run(
        camel_service.run_task(
            Workforce(),
            f"{SUPER_LIG}\n\nPRIOR CONVERSATION:\nunrelated context",
            "job",
        )
    )
    contract = camel_service._workforce_contract(captured["task"])
    assert contract["original_request"] == SUPER_LIG
    assert "PRIOR CONVERSATION" not in contract["original_request"]


def test_coverage_repair_runs_at_the_real_decomposition_boundary() -> None:
    root = _root(
        DIYANET, {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
    )
    workforce = SimpleNamespace(_task_dependencies={}, task_agent=None)
    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_calls = []

    def decompose(observed_workforce, observed_root):
        camel_calls.append(observed_root.id)
        return []

    camel_service._ORIGINAL_DECOMPOSE_TASK = decompose
    try:
        result = camel_service._patched_decompose_task(workforce, root)
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    assert camel_calls == []
    assert len(result) == 2
    assert capabilities.ARTIFACTS in camel_service._contract_capabilities(
        result[1], "task_capabilities"
    )
    assert camel_service._workforce_contract(result[1])["dependency_task_ids"] == [
        result[0].id
    ]
    assert result[0].id not in workforce._task_dependencies
    assert result[1].id not in workforce._task_dependencies


def test_assignment_cannot_drop_artifact_handoff_dependency() -> None:
    root = _root(DIYANET, {capabilities.ARTIFACTS})
    research = Task(id="root.0", content="Research", parent=root)
    artifact = Task(
        id="root.coverage-artifacts",
        content="Create XLSX",
        parent=root,
        additional_info=camel_service._contract_for_subtask(
            root,
            "Create XLSX",
            {capabilities.ARTIFACTS},
            [research.id],
        ),
    )
    assignment = TaskAssignment(
        task_id=artifact.id,
        assignee_id="NONE_FIT",
        dependencies=[],
    )
    repaired = camel_service._preserve_contract_dependencies(
        [assignment], [research, artifact]
    )
    assert repaired[0].dependencies == [research.id]


def test_artifact_dynamic_worker_gets_artifact_tools_not_web() -> None:
    with tempfile.TemporaryDirectory() as root:
        bound = capabilities.resolve_tools(
            {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
            capabilities.ToolContext(workspace_root=root),
        )
    granted = camel_service.capabilities_for_subtask(
        "Create the requested XLSX from prerequisite data_path",
        set(bound.names),
        required_capabilities={capabilities.ARTIFACTS},
    )
    assert "create_xlsx_artifact" in granted
    assert "web_search" not in granted
    assert "web_read" not in granted
    assert "transform_table" not in granted


def test_yayla_research_only_plan_stays_one_task() -> None:
    root = _root(YAYLA, {capabilities.WEB_SEARCH})
    research = Task(
        id="root.0",
        content="Research verified Yayla freeze-dried products and prices",
        parent=root,
        additional_info=root.additional_info,
    )
    root.subtasks = [research]
    workforce = SimpleNamespace(_task_dependencies={})
    repaired = camel_service._repair_initial_plan_coverage(
        workforce, root, [research]
    )
    assert repaired == [research]


def test_quality_evaluation_receives_original_and_subtask_context() -> None:
    root = _root(SUPER_LIG, {capabilities.WEB_SEARCH})
    task = Task(
        id="root.0",
        content="find league standings",
        result="2023 standings",
        parent=root,
        additional_info=camel_service._contract_for_subtask(
            root,
            "find league standings",
            {capabilities.WEB_SEARCH},
            [],
        ),
    )
    seen: dict[str, str] = {}
    original = camel_service._ORIGINAL_ANALYZE_TASK

    def capture(workforce, observed, *, for_failure, error_message=None):
        seen["content"] = observed.content
        return camel_service.TaskAnalysisResult(
            reasoning="captured",
            recovery_strategy=None,
            quality_score=80,
            issues=[],
        )

    workforce = SimpleNamespace(
        execution_strategy="workforce",
        task_agent=SimpleNamespace(model_backend=None),
    )
    camel_service._ORIGINAL_ANALYZE_TASK = capture
    try:
        camel_service._patched_analyze_task(
            workforce, task, for_failure=False
        )
    finally:
        camel_service._ORIGINAL_ANALYZE_TASK = original

    assert SUPER_LIG in seen["content"]
    assert "find league standings" in seen["content"]
    assert "currentness" in seen["content"]
