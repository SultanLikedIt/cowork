"""Structured initial plan: targets × capabilities, bounded timeout."""

from __future__ import annotations

from types import SimpleNamespace

import camel_service
import capabilities
import workforce_plan
from camel.tasks import Task


def test_partition_targets_round_robin() -> None:
    groups = workforce_plan.partition_targets(["A", "B", "C", "D", "E"], 3)
    assert groups == [["A", "D"], ["B", "E"], ["C"]]


def test_three_cities_excel_becomes_four_streams() -> None:
    names = ["İstanbul", "Ankara", "İzmir"]
    plan = workforce_plan.PlanResult(
        targets=[workforce_plan.PlanTarget(name=name) for name in names],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="hava durumu",
                capabilities=["web_search"],
                target_names=names,
            )
        ],
        source="llm",
    )
    streams = workforce_plan.expand_plan_for_coverage(
        "İstanbul, Ankara, İzmir hava durumunu excel olarak ver",
        plan,
        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
        max_subtasks=4,
    )
    research = [
        stream
        for stream in streams
        if stream.capabilities & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
    ]
    artifact = [
        stream
        for stream in streams
        if capabilities.ARTIFACTS in stream.capabilities
    ]
    assert len(streams) == 4
    assert len(research) == 3
    assert len(artifact) == 1
    assert [stream.targets for stream in research] == [
        ("İstanbul",),
        ("Ankara",),
        ("İzmir",),
    ]
    assert artifact[0].depends_on == (0, 1, 2)
    assert set(artifact[0].targets) == set(names)


def test_five_targets_pack_into_three_research_slots() -> None:
    names = [f"C{i}" for i in range(5)]
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
    streams = workforce_plan.expand_plan_for_coverage(
        "compare them as excel",
        plan,
        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
        max_subtasks=4,
    )
    research = [
        stream
        for stream in streams
        if stream.capabilities & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
    ]
    packed = [name for stream in research for name in stream.targets]
    assert len(streams) == 4
    assert len(research) == 3
    assert len(packed) == 5
    assert set(packed) == set(names)


def test_fallback_plan_without_agent() -> None:
    plan = workforce_plan.plan_request(
        "süper lig puan durumu",
        agent=None,
        required={capabilities.WEB_SEARCH},
    )
    assert plan.source == "fallback"
    assert len(plan.workstreams) == 1
    assert capabilities.WEB_SEARCH in capabilities.expand(
        plan.workstreams[0].capabilities
    )


def test_plan_request_timeout_and_token_budget_are_restored() -> None:
    class _Agent:
        step_timeout = 1000
        model_backend = SimpleNamespace(model_config_dict={"max_tokens": 12288})
        seen: dict = {}

        def reset(self) -> None:
            return None

        def step(self, *_args, **_kwargs):
            self.seen["timeout"] = self.step_timeout
            self.seen["max_tokens"] = self.model_backend.model_config_dict[
                "max_tokens"
            ]
            raise RuntimeError("planner unavailable")

    agent = _Agent()
    plan = workforce_plan.plan_request(
        "compare three cities",
        agent=agent,
        required={capabilities.WEB_SEARCH},
    )
    assert plan.source == "fallback"
    assert agent.seen["timeout"] == workforce_plan.PLANNING_STEP_TIMEOUT_SECONDS
    assert agent.seen["max_tokens"] == workforce_plan.PLANNING_MAX_TOKENS
    assert agent.step_timeout == 1000
    assert agent.model_backend.model_config_dict["max_tokens"] == 12288


def test_lookup_target_names_from_plan_compare_subjects() -> None:
    plan = workforce_plan.PlanResult(
        targets=[
            workforce_plan.PlanTarget(name="Türkiye"),
            workforce_plan.PlanTarget(name="Almanya"),
            workforce_plan.PlanTarget(name="Güney Kore"),
        ],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="karşılaştır",
                capabilities=["web_search"],
                target_names=["Türkiye", "Almanya", "Güney Kore"],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == [
        "Türkiye",
        "Almanya",
        "Güney Kore",
    ]


def test_lookup_target_names_empty_for_single_subject_plan() -> None:
    plan = workforce_plan.PlanResult(
        targets=[],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="Akdeniz diyeti nedir",
                capabilities=["web_search", "artifacts"],
                target_names=[],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == []


def test_lookup_target_names_empty_for_fx_rates_board_plan() -> None:
    """Same-day FX rates stay one subject — empty targets → single SEARCH."""
    plan = workforce_plan.PlanResult(
        targets=[],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="güncel dolar euro pound kurları",
                capabilities=["web_search"],
                target_names=[],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == []
    assert "dolar euro pound" in workforce_plan.PLAN_SYSTEM_PROMPT.casefold() or (
        "usd eur gbp" in workforce_plan.PLAN_SYSTEM_PROMPT.casefold()
    )


def test_plan_prompt_splits_metals_from_fx_and_also() -> None:
    """Metals + AUD are different boards; prompt must not merge them."""
    prompt = workforce_plan.PLAN_SYSTEM_PROMPT.casefold()
    assert "avustralya" in prompt
    assert "ve ayrıca" in prompt or "and also" in prompt
    assert "precious metals" in prompt or "altın gümüş" in prompt
    plan = workforce_plan.PlanResult(
        targets=[
            workforce_plan.PlanTarget(name="altın gümüş"),
            workforce_plan.PlanTarget(name="Avustralya doları"),
        ],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="güncel fiyatlar",
                capabilities=["web_search"],
                target_names=["altın gümüş", "Avustralya doları"],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == [
        "altın gümüş",
        "Avustralya doları",
    ]


def test_lookup_target_names_from_json_text() -> None:
    raw = (
        '{"targets":[{"name":"A"},{"name":"B"},{"name":"C"}],'
        '"workstreams":[{"goal":"compare","capabilities":["web_search"],'
        '"target_names":["A","B","C"],"depends_on":[]}]}'
    )
    assert workforce_plan.lookup_target_names_from_plan(raw) == ["A", "B", "C"]


def test_planning_stage_is_bounded() -> None:
    assert workforce_plan.PLANNING_STEP_TIMEOUT_SECONDS == 25
    assert workforce_plan.PLANNING_MAX_TOKENS == 4096
    assert camel_service.PLANNING_STEP_TIMEOUT_SECONDS == 25


def test_initial_decompose_writes_target_contracts() -> None:
    names = ["İstanbul", "Ankara", "İzmir"]
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
        content="İstanbul, Ankara, İzmir hava durumunu excel olarak ver",
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "original_request": "İstanbul, Ankara, İzmir hava durumunu excel olarak ver",
                "required_capabilities": sorted(
                    capabilities.expand(
                        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
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

    assert len(result) == 4
    research = result[:3]
    artifact = result[3]
    assert [camel_service._workforce_contract(task)["targets"] for task in research] == [
        ["İstanbul"],
        ["Ankara"],
        ["İzmir"],
    ]
    assert capabilities.ARTIFACTS in camel_service._contract_capabilities(
        artifact, "task_capabilities"
    )
    assert camel_service._workforce_contract(artifact)["dependency_task_ids"] == [
        task.id for task in research
    ]
    assert all(task.id not in workforce._task_dependencies for task in result)
    # Repair must not fold "excel" from the original request onto Research.
    for task in research:
        assert capabilities.ARTIFACTS not in camel_service._contract_capabilities(
            task, "task_capabilities"
        )


def test_orchestration_telemetry_reports_plan_stage() -> None:
    workforce = SimpleNamespace(
        _last_plan_duration_seconds=0.25,
        _assignment_duration_seconds=0.04,
        _last_assignment_source="contract",
        _last_plan_output_tokens=128,
        _subtask_count=4,
        _targets_planned=["İstanbul", "Ankara", "İzmir"],
        _children=[
            SimpleNamespace(
                _web_controller_cfg={
                    "lookup_stats": {
                        "İstanbul": {"search": 1, "fetch": 1, "success": 1},
                        "Ankara": {"search": 1, "fetch": 1, "success": 0},
                    }
                }
            )
        ],
    )
    snap = camel_service.orchestration_telemetry(workforce)
    assert snap["planning_ms"] == 250
    assert snap["assignment_ms"] == 40
    assert snap["assignment_source"] == "contract"
    assert snap["planning_output_tokens"] == 128
    assert snap["subtask_count"] == 4
    assert snap["targets_planned"] == ["İstanbul", "Ankara", "İzmir"]
    assert snap["targets_covered"] == ["İstanbul"]
    assert snap["controller_search_count"] == 2
    assert snap["controller_by_target"]["Ankara"]["success"] == 0


def test_usable_incomplete_lookup_still_runs_quality() -> None:
    task = Task(
        id="root.1",
        content="weather",
        result="İstanbul 22C " * 10,
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "targets": ["İstanbul", "Ankara"],
                "targets_covered": ["İstanbul"],
                "task_capabilities": sorted(
                    capabilities.expand({capabilities.WEB_SEARCH})
                ),
            }
        },
    )
    seen = {}

    def capture(workforce, observed, *, for_failure, error_message=None):
        seen["called"] = True
        return camel_service.TaskAnalysisResult(
            reasoning="partial",
            recovery_strategy=None,
            quality_score=80,
            issues=[],
        )

    original = camel_service._ORIGINAL_ANALYZE_TASK
    camel_service._ORIGINAL_ANALYZE_TASK = capture
    try:
        out = camel_service._patched_analyze_task(
            SimpleNamespace(
                execution_strategy="workforce",
                task_agent=SimpleNamespace(model_backend=None),
            ),
            task,
            for_failure=False,
        )
    finally:
        camel_service._ORIGINAL_ANALYZE_TASK = original
    assert seen.get("called") is True
    assert out.recovery_strategy is None
    assert camel_service._DEGRADED_TASK_REASONS[task.id].startswith(
        "missing lookup targets"
    )


def test_complete_lookup_skips_quality() -> None:
    task = Task(
        id="root.2",
        content="weather",
        result="İstanbul 22C " * 10,
        additional_info={
            camel_service.WORKFORCE_INTENT_KEY: {
                "targets": ["İstanbul"],
                "targets_covered": ["İstanbul"],
                "task_capabilities": sorted(
                    capabilities.expand({capabilities.WEB_SEARCH})
                ),
            }
        },
    )
    seen = {}

    def capture(workforce, observed, *, for_failure, error_message=None):
        seen["called"] = True
        return camel_service.TaskAnalysisResult(
            reasoning="should not run",
            recovery_strategy=None,
            quality_score=40,
            issues=["nope"],
        )

    original = camel_service._ORIGINAL_ANALYZE_TASK
    camel_service._ORIGINAL_ANALYZE_TASK = capture
    try:
        out = camel_service._patched_analyze_task(
            SimpleNamespace(
                execution_strategy="workforce",
                task_agent=SimpleNamespace(model_backend=None),
            ),
            task,
            for_failure=False,
        )
    finally:
        camel_service._ORIGINAL_ANALYZE_TASK = original
    assert seen == {}
    assert out.quality_score == 100


def test_deck_renders_tables_instead_of_dropping_them() -> None:
    """A section that is a table must not become an empty slide.

    Live: "Sözleşme Yığını" and "Finansal Veriler" came out as a heading
    over 35 characters of nothing, while the .docx built from the same
    text carried both tables in full -- the deck skipped table blocks
    because the Word writer renders them from columns/rows.
    """
    import re

    from execution.langgraph_runtime.workforce import _html_deck_from_payload

    body = (
        "## Sozlesme Yigini\n\n"
        "| Tarih | Sozlesme | Kurum | Deger |\n"
        "|---|---|---|---|\n"
        "| Haziran 2023 | USSOCOM veri fuzyonu | ABD Ozel Harekat | 463 milyon |\n"
        "| Mayis 2024 | Maven IDIQ | DoD | 480 milyon |\n\n"
        "## Diger Bolum\n\n"
        "Duz metin burada yer alir ve yeterince uzundur.\n"
    )
    html = _html_deck_from_payload(
        title="Test", summary="", columns=[], rows=[], body=body
    )

    sections = re.findall(r"<section class='[^']*'>(.*?)</section>", html, re.S)
    table_slide = next(s for s in sections if "Sozlesme Yigini" in s)
    text = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", table_slide))

    # Every row reached the slide, as label/value pairs rather than pipes.
    assert "USSOCOM" in text and "463 milyon" in text
    assert "Maven IDIQ" in text and "480 milyon" in text
    assert "|" not in text
    assert len(text) > 150


def test_pptx_gets_the_full_body_like_html_does() -> None:
    """The deck needs the sections, and the summary has none.

    A model that returns a short summary alongside a long content built an
    eight-section HTML deck and a one-slide .pptx from the same answer,
    because the html branch read `body` and the pptx branch read `summary`.
    """
    import inspect

    from execution.langgraph_runtime import workforce

    src = inspect.getsource(workforce._write_artifact)
    pptx_branch = src[src.index('if kind == "pptx":') :]
    pptx_branch = pptx_branch[: pptx_branch.index("if kind == \"code\":")]
    assert "summary=body or summary" in pptx_branch
    assert "summary=summary or body" not in pptx_branch
