"""Self-tests for catalog expansion, thresholds, serialization, and variance."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from reliability_harness.catalog import ROOT, load_catalog, select_scenarios
from reliability_harness.evaluate import MEMOIZED_TOOL_NAMES, evaluate
from reliability_harness.models import RunResult, Scenario
from reliability_harness.report import aggregate, write_outputs
from reliability_harness.runner import load_defaults


failures: list[str] = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def main():
    catalog = load_catalog()
    defaults = load_defaults(ROOT / "defaults.json")
    check("catalog is comprehensive", len(catalog) >= 60)
    check("scenario ids are unique", len({s.id for s in catalog}) == len(catalog))
    check("all required categories exist", {"routing", "assignment", "workforce", "direct-specialist", "recovery", "files", "tools", "skills", "context", "lifecycle"} <= {s.category for s in catalog})
    check("quick suite is deterministic-only", all(s.kind != "live" for s in select_scenarios(catalog, suite="quick")))
    check("destructive live cases are gated out of normal live", not any(s.destructive for s in select_scenarios(catalog, suite="live")))
    check("long prompt expansion preserves exact character count", len(next(s for s in catalog if s.id == "live.routing.10k").prompt) == 10000)

    scenario = Scenario(id="self", name="self", category="tools", kind="live", thresholds={"tool_call_count": {"warn": 1, "fail": 2}}, expected={"status": ["done"]})
    result = RunResult.empty(scenario, 1, "self:1")
    result.final_status = "done"; result.output_character_count = 10
    result.tool_calls = [{"tool_name": "read", "args": {"x": 1}}, {"tool_name": "read", "args": {"x": 1}}, {"tool_name": "read", "args": {"x": 2}}]
    evaluate(result, scenario, defaults)
    check("scenario override triggers FAIL", result.outcome == "FAIL")
    check("duplicate calls are derived", result.duplicate_tool_calls == 1)

    passing = result.to_dict(); passing["outcome"] = "PASS"; passing["strategy_selected"] = "direct"; passing["task_count"] = 1; passing["durations"] = {"total": 1.0}; passing["total_tokens"] = 10; passing["findings"] = []
    second = json.loads(json.dumps(passing)); second["repetition"] = 2; second["durations"] = {"total": 3.0}; second["total_tokens"] = 30
    summary = aggregate([passing, second])
    check("aggregate calculates median duration", summary["scenarios"]["self"]["duration_seconds"]["median"] == 2.0)
    check("aggregate calculates pass rate", summary["scenarios"]["self"]["pass_rate"] == 1.0)
    output = Path(tempfile.mkdtemp(prefix="harness_selftest_"))
    paths = write_outputs(output, [passing, second], {"test": True}, ["test blind spot"])
    check("all four report artifacts are written", all(path.exists() for path in paths.values()))
    check("JSONL has one record per run", len(paths["results.jsonl"].read_text(encoding="utf-8").splitlines()) == 2)

    # -----------------------------------------------------------------
    # Memoized vs. non-memoized duplicate dispatch telemetry. The fix
    # this locks down: a duplicate dispatch of a workspace tool (see
    # workspace.py's ReadOnlyWorkspaceTools memoization) does not
    # re-execute the real work, so it must WARN, not FAIL, on its own --
    # while a duplicate dispatch of a tool with no job-wide guarantee
    # (internal search_web across agents, run_python, ...) can still represent a second
    # real execution and keeps the original strict fail-eligible bar.
    # -----------------------------------------------------------------
    check(
        "at least one real workspace tool name is covered by MEMOIZED_TOOL_NAMES",
        {"read_table_rows", "inspect_table", "read_text_file"} <= MEMOIZED_TOOL_NAMES,
    )
    check(
        "the model-facing web tools are job-memoized",
        {"web_search", "web_read"} <= MEMOIZED_TOOL_NAMES,
    )
    check(
        "internal search and code execution are not treated as job-memoized",
        "search_web" not in MEMOIZED_TOOL_NAMES and "run_python" not in MEMOIZED_TOOL_NAMES,
    )

    def _run(tool_calls):
        s = Scenario(id="dup-self", name="dup-self", category="tools", kind="live", expected={"status": ["done"]})
        r = RunResult.empty(s, 1, "dup-self:1")
        r.final_status = "done"
        r.output_character_count = 10
        r.tool_calls = tool_calls
        evaluate(r, s, defaults)
        return r

    memoized_dup = _run(
        [{"tool_name": "read_table_rows", "args": {"offset": 0}}] * 5
        + [{"tool_name": "inspect_table", "args": {"path": "a.csv"}}]
    )
    check(
        "5x duplicate dispatch of a memoized tool does not FAIL the run on its own",
        memoized_dup.outcome != "FAIL",
        memoized_dup.outcome,
    )
    check(
        "...but it still WARNs -- duplicate dispatch behavior is not hidden",
        memoized_dup.outcome == "WARN",
        memoized_dup.outcome,
    )
    check(
        "duplicate_tool_calls_memoized reflects the 4 extra dispatches",
        memoized_dup.duplicate_tool_calls_memoized == 4,
        memoized_dup.duplicate_tool_calls_memoized,
    )
    check(
        "duplicate_tool_calls_unmemoized is 0 (nothing non-memoized was duplicated)",
        memoized_dup.duplicate_tool_calls_unmemoized == 0,
        memoized_dup.duplicate_tool_calls_unmemoized,
    )
    check(
        "the raw duplicate_tool_calls total is unchanged (report.py still reads this)",
        memoized_dup.duplicate_tool_calls == 4,
        memoized_dup.duplicate_tool_calls,
    )
    check(
        "tool_dispatch_count counts every dispatch, memoized or not",
        memoized_dup.tool_dispatch_count == 6,
        memoized_dup.tool_dispatch_count,
    )
    check(
        "tool_execution_count reflects real work happening once per unique memoized call",
        memoized_dup.tool_execution_count == 2,
        memoized_dup.tool_execution_count,
    )
    check(
        "tool_cache_hit_count captures the 4 dispatches that were memoized returns",
        memoized_dup.tool_cache_hit_count == 4,
        memoized_dup.tool_cache_hit_count,
    )

    # fail=3 requires the duplicate COUNT (dispatches - 1) to exceed 3, so
    # 5 identical dispatches (4 duplicates) is the smallest batch that
    # actually crosses it -- matches the pre-existing threshold semantics
    # unchanged for non-memoized tools.
    unmemoized_dup = _run([{"tool_name": "search_web", "args": {"query": "x"}}] * 5)
    check(
        "5x duplicate dispatch of a NON-memoized tool still FAILs (real re-execution)",
        unmemoized_dup.outcome == "FAIL",
        unmemoized_dup.outcome,
    )
    check(
        "duplicate_tool_calls_unmemoized reflects all 4 extra real dispatches",
        unmemoized_dup.duplicate_tool_calls_unmemoized == 4,
        unmemoized_dup.duplicate_tool_calls_unmemoized,
    )
    check(
        "duplicate_tool_calls_memoized is 0 for an all-non-memoized batch",
        unmemoized_dup.duplicate_tool_calls_memoized == 0,
        unmemoized_dup.duplicate_tool_calls_memoized,
    )
    check(
        "tool_execution_count counts every non-memoized dispatch as real work (no memoization credit)",
        unmemoized_dup.tool_execution_count == 5,
        unmemoized_dup.tool_execution_count,
    )
    check(
        "tool_cache_hit_count is 0 -- nothing was memoized",
        unmemoized_dup.tool_cache_hit_count == 0,
        unmemoized_dup.tool_cache_hit_count,
    )

    mixed = _run(
        [{"tool_name": "read_table_rows", "args": {"offset": 0}}] * 3  # 2 memoized cache hits
        + [{"tool_name": "search_web", "args": {"query": "x"}}] * 5  # 4 real unmemoized re-executions
        + [{"tool_name": "table_summary", "args": {"path": "a.csv"}}]  # 1 unique memoized call
    )
    check(
        "mixed batch: dispatch count covers all 9 dispatches",
        mixed.tool_dispatch_count == 9,
        mixed.tool_dispatch_count,
    )
    check(
        "mixed batch: execution count is 2 unique memoized + 5 real unmemoized = 7",
        mixed.tool_execution_count == 7,
        mixed.tool_execution_count,
    )
    check(
        "mixed batch: cache hits are exactly the 2 memoized repeats",
        mixed.tool_cache_hit_count == 2,
        mixed.tool_cache_hit_count,
    )
    check(
        "mixed batch: still FAILs because of the unmemoized duplicate, not the memoized one",
        mixed.outcome == "FAIL" and mixed.duplicate_tool_calls_unmemoized == 4 and mixed.duplicate_tool_calls_memoized == 2,
        (mixed.outcome, mixed.duplicate_tool_calls_unmemoized, mixed.duplicate_tool_calls_memoized),
    )

    if failures:
        raise SystemExit(f"{len(failures)} harness self-test(s) failed: {failures}")
    print("\nAll reliability harness self-tests passed.")


if __name__ == "__main__":
    main()
