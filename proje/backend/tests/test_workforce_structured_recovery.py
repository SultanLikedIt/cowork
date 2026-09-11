"""Recovery of verified structured research evidence into a task handoff."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import camel_service
import capabilities
from camel.tasks import Task
from camel.tasks.task import TaskState


def test_verified_table_recovery_completes_research_and_unlocks_document() -> None:
    calls = []

    def web_read(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("recovery must not rediscover the web")

    web_read._verified_structured_evidence = [
        {
            "captured_at": 110.0,
            "need": "current league standings",
            "data_path": "artifacts/extracted/current-standings.json",
            "row_count": 18,
            "columns": ["position", "team", "played", "points"],
            "source_url": "https://sports.example/standings",
            "title": "Current standings",
            "provenance": ["https://sports.example/standings"],
            "selected_table_index": 1,
        }
    ]

    root = Task(content="Find current standings and create XLSX", id="root")
    research = Task(
        content="Research and extract the current standings",
        id="research",
        parent=root,
    )
    document = Task(
        content="Create XLSX from the research data_path",
        id="document",
        parent=root,
    )
    research.assigned_worker_id = "research-worker"
    research.state = TaskState.FAILED

    class Workforce:
        def __init__(self):
            self._children = [
                SimpleNamespace(
                    node_id="research-worker",
                    worker=SimpleNamespace(
                        tool_dict={
                            "web_read": SimpleNamespace(func=web_read)
                        }
                    ),
                )
            ]
            self._task_start_times = {"research": 100.0}
            self.completed = []
            self.ready = []

        async def _handle_completed_task(self, task):
            self.completed.append(task.id)
            if task.id == "research":
                self.ready.append(document.id)

    workforce = Workforce()
    halted = asyncio.run(
        camel_service._patched_handle_failed_task(workforce, research)
    )

    handoff = json.loads(research.result)
    assert halted is False
    assert research.state == TaskState.DONE
    assert workforce.completed == ["research"]
    assert workforce.ready == ["document"]
    assert calls == []
    assert handoff == {
        "status": "verified_structured_evidence_ready",
        "data_path": "artifacts/extracted/current-standings.json",
        "row_count": 18,
        "columns": ["position", "team", "played", "points"],
        "source_url": "https://sports.example/standings",
        "provenance": ["https://sports.example/standings"],
        "title": "Current standings",
        "selected_table_index": 1,
        "instruction": (
            "Consume data_path directly. Do not repeat web research and do "
            "not read or retype the rows."
        ),
    }


def test_evidence_from_a_previous_task_is_not_reused() -> None:
    def web_read():
        return None

    web_read._verified_structured_evidence = [
        {
            "captured_at": 50.0,
            "data_path": "artifacts/extracted/stale.json",
            "row_count": 10,
            "columns": ["value"],
            "source_url": "https://example.test/stale",
        }
    ]
    task = Task(content="Research a different subject", id="new-task")
    task.assigned_worker_id = "research-worker"
    workforce = SimpleNamespace(
        _children=[
            SimpleNamespace(
                node_id="research-worker",
                worker=SimpleNamespace(
                    tool_dict={
                        "web_read": SimpleNamespace(func=web_read)
                    }
                ),
            )
        ],
        _task_start_times={"new-task": 100.0},
    )

    assert camel_service._worker_structured_evidence(workforce, task) is None


def _workforce_with_budget_checker(exhausted: bool | None) -> SimpleNamespace:
    """A worker whose bound web_read tool exposes _job_budget_exhausted.

    ``exhausted=None`` mimics a worker with no web_read tool at all (e.g.
    Document/Review/Outline Agent) -- tool_dict has no "web_read" key.
    """
    tool_dict: dict = {}
    if exhausted is not None:
        def web_read():
            return None

        web_read._job_budget_exhausted = lambda: exhausted
        tool_dict["web_read"] = SimpleNamespace(func=web_read)

    return SimpleNamespace(
        _children=[
            SimpleNamespace(
                node_id="worker-1",
                worker=SimpleNamespace(tool_dict=tool_dict),
            )
        ],
    )


def test_web_budget_exhausted_check_reads_the_workers_bound_tool() -> None:
    task = Task(content="Research something", id="t")
    task.assigned_worker_id = "worker-1"

    exhausted_wf = _workforce_with_budget_checker(True)
    assert camel_service._worker_web_budget_exhausted(exhausted_wf, task) is True

    fresh_wf = _workforce_with_budget_checker(False)
    assert camel_service._worker_web_budget_exhausted(fresh_wf, task) is False

    no_web_tool_wf = _workforce_with_budget_checker(None)
    assert camel_service._worker_web_budget_exhausted(no_web_tool_wf, task) is False


def test_quality_recovery_accepts_verified_path_without_research_retry() -> None:
    def web_read():
        raise AssertionError("quality recovery must not call the web tool")

    web_read._verified_structured_evidence = [
        {
            "captured_at": 110.0,
            "need": "current values",
            "data_path": "artifacts/extracted/current.json",
            "row_count": 4,
            "columns": ["name", "value"],
            "source_url": "https://example.test/current",
            "provenance": ["https://example.test/current"],
        }
    ]
    task = Task(content="Research the current values", id="research")
    task.assigned_worker_id = "research-worker"
    # Empty prose: the table IS the result. A written answer would be kept.
    task.result = ""
    workforce = SimpleNamespace(
        _children=[
            SimpleNamespace(
                node_id="research-worker",
                worker=SimpleNamespace(
                    tool_dict={
                        "web_read": SimpleNamespace(func=web_read)
                    }
                ),
            )
        ],
        _task_start_times={"research": 100.0},
    )

    analysis = camel_service._patched_analyze_task(
        workforce, task, for_failure=False
    )

    assert analysis.quality_sufficient is True
    assert analysis.recovery_strategy is None
    assert json.loads(task.result)["data_path"] == (
        "artifacts/extracted/current.json"
    )


def _fixture_table_evidence() -> dict:
    """The beIN Sports widget that closed a transfer-news job."""
    return {
        "captured_at": 110.0,
        "need": "https://beinsports.com.tr/takim/galatasaray",
        "data_path": "artifacts/extracted/takim-galatasaray-tables.json",
        "row_count": 20,
        "columns": ["key", "title", "position"],
        "row_keys": [
            "awayTeam",
            "homeTeam",
            "league",
            "matchDate",
            "liteMatchStatus",
        ],
        "source_url": "https://beinsports.com.tr/takim/galatasaray",
        "title": "Galatasaray Haberleri - Transfer ve Son Dakika | beIN SPORTS",
        "provenance": [],
    }


def _research_workforce_with(evidence: dict) -> SimpleNamespace:
    def web_read():
        return None

    web_read._verified_structured_evidence = [evidence]
    return SimpleNamespace(
        _children=[
            SimpleNamespace(
                node_id="research-worker",
                worker=SimpleNamespace(
                    tool_dict={"web_read": SimpleNamespace(func=web_read)}
                ),
            )
        ],
        _task_start_times={"research": 100.0},
    )


def test_a_fixture_table_does_not_answer_a_transfer_news_task() -> None:
    task = Task(
        content="Find current Galatasaray transfer news",
        id="research",
    )
    task.assigned_worker_id = "research-worker"
    workforce = _research_workforce_with(_fixture_table_evidence())

    assert camel_service._worker_structured_evidence(workforce, task) is None


def test_page_title_alone_does_not_make_a_table_relevant() -> None:
    """The page said Transfer; the table was a match calendar."""
    task = Task(content="Galatasaray transfer haberleri", id="research")
    task.assigned_worker_id = "research-worker"
    evidence = _fixture_table_evidence()
    workforce = _research_workforce_with(evidence)
    assert camel_service._structured_evidence_matches_task(task, evidence) is False
    assert camel_service._worker_structured_evidence(workforce, task) is None


def test_usable_prose_is_not_replaced_by_an_incidental_table() -> None:
    task = Task(content="Find current Galatasaray transfer news", id="research")
    task.assigned_worker_id = "research-worker"
    task.result = (
        "Latest transfer reports: Noa Lang joined; Harder and Rashford "
        "are being linked. Source: fanatik.com.tr."
    )
    workforce = _research_workforce_with(_fixture_table_evidence())

    analysis = camel_service._patched_analyze_task(
        workforce, task, for_failure=False
    )

    assert analysis.quality_sufficient is True
    assert "Noa Lang" in task.result
    assert "verified_structured_evidence_ready" not in task.result


def _document_worker_tool_dict(evidence: dict | None) -> dict:
    def create_xlsx_artifact():
        return None

    if evidence is not None:
        create_xlsx_artifact._verified_artifact_evidence = [evidence]

    return {"create_xlsx_artifact": SimpleNamespace(func=create_xlsx_artifact)}


def test_verified_artifact_recovery_completes_a_failed_document_task() -> None:
    async def run() -> None:
        task = Task(content="Create the requested XLSX from data_path", id="document")
        task.assigned_worker_id = "document-worker"
        task.result = "Task processing failed"

        class Workforce:
            def __init__(self) -> None:
                self._children = [
                    SimpleNamespace(
                        node_id="document-worker",
                        worker=SimpleNamespace(
                            tool_dict=_document_worker_tool_dict(
                                {
                                    "captured_at": 110.0,
                                    "path": "artifacts/ankara-namaz-vakitleri.xlsx",
                                    "download_path": "ankara-namaz-vakitleri.xlsx",
                                    "row_count": 365,
                                    "column_count": 8,
                                    "size_bytes": 15234,
                                    "producer": "create_xlsx_artifact",
                                }
                            )
                        ),
                    )
                ]
                self._task_start_times = {"document": 100.0}
                self.completed: list[str] = []

            async def _handle_completed_task(self, task):
                self.completed.append(task.id)

        workforce = Workforce()
        handled = await camel_service._patched_handle_failed_task(workforce, task)

        assert handled is False
        assert task.state == TaskState.DONE
        assert workforce.completed == ["document"]
        payload = json.loads(task.result)
        assert payload["status"] == "verified_artifact_created"
        assert payload["path"] == "artifacts/ankara-namaz-vakitleri.xlsx"

    asyncio.run(run())


def test_artifact_evidence_from_before_the_task_started_is_not_reused() -> None:
    async def run() -> None:
        task = Task(content="Create a different file", id="document")
        task.assigned_worker_id = "document-worker"
        task.result = "Task processing failed"

        workforce = SimpleNamespace(
            _children=[
                SimpleNamespace(
                    node_id="document-worker",
                    worker=SimpleNamespace(
                        tool_dict=_document_worker_tool_dict(
                            {
                                "captured_at": 50.0,  # before start (100.0)
                                "path": "artifacts/stale.xlsx",
                                "download_path": "stale.xlsx",
                                "row_count": 1,
                                "column_count": 1,
                                "size_bytes": 100,
                                "producer": "create_xlsx_artifact",
                            }
                        )
                    ),
                )
            ],
            _task_start_times={"document": 100.0},
        )

        calls = []

        async def original(_self, _task):
            calls.append("original")
            return True

        original_ref = camel_service._ORIGINAL_HANDLE_FAILED_TASK
        camel_service._ORIGINAL_HANDLE_FAILED_TASK = original
        try:
            handled = await camel_service._patched_handle_failed_task(workforce, task)
        finally:
            camel_service._ORIGINAL_HANDLE_FAILED_TASK = original_ref

        assert calls == ["original"]
        assert handled is True

    asyncio.run(run())


def test_worker_with_no_artifact_tool_gets_no_artifact_evidence() -> None:
    task = Task(content="Research something", id="research")
    task.assigned_worker_id = "research-worker"
    workforce = SimpleNamespace(
        _children=[
            SimpleNamespace(
                node_id="research-worker",
                worker=SimpleNamespace(tool_dict={}),
            )
        ],
        _task_start_times={"research": 100.0},
    )

    assert camel_service._worker_artifact_evidence(workforce, task) is None


def test_internal_handoff_payloads_are_recognized() -> None:
    research_handoff = camel_service._structured_research_handoff(
        {
            "data_path": "artifacts/extracted/standings.json",
            "row_count": 18,
            "columns": ["team", "points"],
            "source_url": "https://sports.example/standings",
        }
    )
    artifact_handoff = camel_service._artifact_research_handoff(
        {"path": "artifacts/report.xlsx"}
    )
    assert camel_service._is_internal_handoff_payload(research_handoff) is True
    assert camel_service._is_internal_handoff_payload(artifact_handoff) is True


def test_ordinary_text_and_json_are_not_internal_handoffs() -> None:
    assert camel_service._is_internal_handoff_payload("") is False
    assert camel_service._is_internal_handoff_payload("The answer is 42.") is False
    assert camel_service._is_internal_handoff_payload("not json {") is False
    # A dict-shaped answer that just isn't one of our two handoff kinds
    # (e.g. a worker legitimately answering with JSON content) must still
    # go through the normal single-leaf skip path.
    assert (
        camel_service._is_internal_handoff_payload('{"status": "done", "x": 1}')
        is False
    )


def test_single_leaf_internal_handoff_is_not_skipped_to_the_user() -> None:
    """synthesize_workforce_result must not hand raw handoff JSON to a user.

    Reproduces the confirmed failure: a single-subtask job (e.g. "find the
    current standings, write it as a table") whose only leaf's result is a
    verified_structured_evidence_ready handoff -- left behind by that
    leaf's own quality-check recovery (_worker_structured_evidence) -- must
    not skip synthesis the way an ordinary one-leaf answer would. This only
    checks the routing decision (skip vs. call synthesis), not the
    synthesis model call itself, which needs a live model and is out of
    scope for this test.
    """
    handoff = camel_service._structured_research_handoff(
        {
            "data_path": "artifacts/extracted/standings.json",
            "row_count": 18,
            "columns": ["team", "points"],
            "source_url": "https://sports.example/standings",
        }
    )

    async def run() -> None:
        leaf = Task(content="Find the standings", id="leaf")
        leaf.result = handoff
        leaf.state = TaskState.DONE
        root = Task(content="root", id="root")
        root.subtasks = [leaf]
        root.state = TaskState.DONE

        called = {"synthesis": False}

        class FakeAgent:
            def __init__(self, *_a, **_k):
                pass

            async def astep(self, _prompt):
                called["synthesis"] = True
                msg = SimpleNamespace(content="A real table answer.")
                return SimpleNamespace(msgs=[msg], msg=msg)

        original_agent = camel_service.ChatAgent
        camel_service.ChatAgent = FakeAgent
        try:
            answer, is_partial = await camel_service.synthesize_workforce_result(
                root, "find the standings", camel_service.UsageTracker()
            )
        finally:
            camel_service.ChatAgent = original_agent

        assert called["synthesis"] is True
        assert answer != handoff

    asyncio.run(run())


def test_synthesis_prefers_prose_over_tables_by_default() -> None:
    """ROWS PREVIEW should not force a markdown table for ordinary queries."""
    handoff = camel_service._structured_research_handoff(
        {
            "data_path": "artifacts/extracted/transfers.json",
            "row_count": 2,
            "columns": ["player", "from", "to", "fee"],
            "source_url": "https://sporx.example/galatasaray",
        }
    )

    async def run() -> None:
        leaf = Task(content="Find current transfer news", id="leaf")
        leaf.result = handoff
        leaf.state = TaskState.DONE
        root = Task(content="root", id="root")
        root.subtasks = [leaf]
        root.state = TaskState.DONE

        captured = {"prompt": ""}

        class FakeAgent:
            def __init__(self, *_a, **_k):
                pass

            async def astep(self, prompt):
                captured["prompt"] = prompt
                msg = SimpleNamespace(content="Prose answer.")
                return SimpleNamespace(msgs=[msg], msg=msg)

        original_agent = camel_service.ChatAgent
        try:
            with tempfile.TemporaryDirectory() as root_dir:
                data_dir = Path(root_dir) / "artifacts" / "extracted"
                data_dir.mkdir(parents=True, exist_ok=True)
                (data_dir / "transfers.json").write_text(
                    json.dumps(
                        [
                            {"player": "A", "from": "X", "to": "Galatasaray", "fee": "-"},
                            {"player": "B", "from": "Galatasaray", "to": "Y", "fee": "5M"},
                        ],
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                camel_service.ChatAgent = FakeAgent
                answer, is_partial = await camel_service.synthesize_workforce_result(
                    root,
                    "güncel galatasaray transfer haberlerini bul",
                    camel_service.UsageTracker(),
                    workspace_root=root_dir,
                )
        finally:
            camel_service.ChatAgent = original_agent

        assert answer == "Prose answer."
        assert is_partial is False
        assert "Prefer concise prose or bullet points by default." in captured["prompt"]
        assert "Render a table only when the user explicitly asked for tabular output" in captured["prompt"]
        assert "Created files are listed by the product UI" in captured["prompt"]

    asyncio.run(run())


def test_clean_user_facing_answer_strips_artifact_markdown_links() -> None:
    cleaned = camel_service._clean_user_facing_answer(
        "Dosya hazır: [ankara.xlsx](artifacts/ankara.xlsx)\n\n"
        "Kaynak: [TFF](https://www.tff.org)"
    )
    assert "artifacts/" not in cleaned
    assert "](" not in cleaned or "https://www.tff.org" in cleaned
    assert "ankara.xlsx" not in cleaned
    assert "[TFF](https://www.tff.org)" in cleaned


def test_clean_user_facing_answer_strips_extracted_data_paths() -> None:
    cleaned = camel_service._clean_user_facing_answer(
        "Veri `artifacts/extracted/standings.json` konumunda."
    )
    assert "artifacts/extracted" not in cleaned
    assert "standings.json" not in cleaned


def test_synthesis_runs_when_named_lookup_targets_are_missing() -> None:
    leaf = Task(content="İstanbul and Ankara weather", id="leaf")
    leaf.result = "İstanbul today is 22C. " * 4
    leaf.state = TaskState.DONE
    leaf.additional_info = {
        camel_service.WORKFORCE_INTENT_KEY: {
            "targets": ["İstanbul", "Ankara"],
            "targets_covered": ["İstanbul"],
            "task_capabilities": sorted(
                capabilities.expand({capabilities.WEB_SEARCH})
            ),
        }
    }
    root = Task(content="root", id="root")
    root.subtasks = [leaf]
    root.state = TaskState.DONE
    called = {"synthesis": False, "prompt": ""}

    class FakeAgent:
        def __init__(self, *_a, **_k):
            pass

        async def astep(self, prompt):
            called["synthesis"] = True
            called["prompt"] = prompt
            msg = SimpleNamespace(
                content="İstanbul 22C. Ankara was not covered."
            )
            return SimpleNamespace(msgs=[msg], msg=msg)

    original = camel_service.ChatAgent
    camel_service.ChatAgent = FakeAgent
    try:
        answer, is_partial = asyncio.run(
            camel_service.synthesize_workforce_result(
                root,
                "İstanbul, Ankara hava durumu",
                camel_service.UsageTracker(),
            )
        )
    finally:
        camel_service.ChatAgent = original

    assert called["synthesis"] is True
    assert is_partial is True
    assert "Ankara" in called["prompt"]
    assert "İstanbul" in answer
