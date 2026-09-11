from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .models import Finding, RunResult, Scenario


RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}

# Tool names whose real underlying work is memoized per exact (name, args)
# key for the life of a job -- see workspace.py's ReadOnlyWorkspaceTools
# and web_tools.py's job-shared search/read caches. A repeat dispatch with
# identical arguments returns the cached result instead of re-parsing/
# re-reading/searching, so a duplicate DISPATCH no longer means the expensive
# operation ran twice. Tools outside this set (internal search_web across
# different agents, run_python, ...) carry no job-wide guarantee: a duplicate
# dispatch can be a genuine second real execution.
#
# The harness talks to the backend only over its HTTP API (see
# adapters.py) rather than importing backend modules, so this is a
# deliberate hardcoded mirror of workspace.py's tool method names, not an
# import -- keep the two lists in sync by hand if workspace.py's tool set
# changes.
MEMOIZED_TOOL_NAMES = frozenset(
    {
        "list_files",
        "inspect_file",
        "read_text_file",
        "search_text",
        "inspect_table",
        "read_table_rows",
        "search_table",
        "table_summary",
        "compare_tables",
        "web_search",
        "web_read",
    }
)


def _add(result: RunResult, outcome: str, code: str, message: str, severity="P2", category="invariant"):
    result.findings.append(Finding(outcome, code, message, severity, category))


def _thresholds(defaults: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    merged = {key: dict(value) for key, value in defaults["thresholds"].items()}
    for key, value in scenario.thresholds.items():
        merged.setdefault(key, {}).update(value if isinstance(value, dict) else {"fail": value})
    return merged


def _check_limit(result: RunResult, name: str, value: float, limits: dict[str, Any]):
    if limits.get("fail") is not None and value > limits["fail"]:
        _add(result, "FAIL", f"threshold.{name}", f"{name}={value} exceeds fail={limits['fail']}", "P1", "resource")
    elif limits.get("warn") is not None and value > limits["warn"]:
        _add(result, "WARN", f"threshold.{name}", f"{name}={value} exceeds warn={limits['warn']}", "P2", "resource")


def evaluate(result: RunResult, scenario: Scenario, defaults: dict[str, Any]) -> RunResult:
    terminal = set(defaults["terminal_statuses"])
    if result.final_status not in terminal:
        _add(result, "FAIL", "terminal.job", f"Non-terminal final status: {result.final_status}", "P0", "lifecycle")
    if result.final_status == "done" and result.output_character_count <= 0 and scenario.kind == "live":
        _add(result, "FAIL", "output.empty", "Successful job has an empty final output", "P1", "output")
    if result.failure_reason and result.final_status not in {"error", "partial", "cancelled", "interrupted"}:
        _add(result, "WARN", "failure.hidden", "Failure reason present on a successful status", "P2")
    if result.output_truncated:
        _add(result, "WARN", "output.truncated", "Output-cap/truncation signal detected", "P1", "output")

    calls = [(c.get("tool_name"), json.dumps(c.get("args", {}), sort_keys=True, default=str)) for c in result.tool_calls]
    call_counts = Counter(calls)
    result.duplicate_tool_calls = sum(count - 1 for count in call_counts.values() if count > 1)
    result.duplicate_tool_calls_memoized = sum(
        count - 1
        for (tool_name, _args), count in call_counts.items()
        if count > 1 and tool_name in MEMOIZED_TOOL_NAMES
    )
    result.duplicate_tool_calls_unmemoized = (
        result.duplicate_tool_calls - result.duplicate_tool_calls_memoized
    )
    result.tool_dispatch_count = len(result.tool_calls)
    # Real work happens once per unique (tool, args) key for a memoized
    # tool; a non-memoized tool has no such guarantee, so every one of its
    # dispatches (duplicate or not) is counted as a real execution.
    result.tool_execution_count = len(call_counts) + result.duplicate_tool_calls_unmemoized
    result.tool_cache_hit_count = result.duplicate_tool_calls_memoized
    result.no_progress_calls = sum(
        1 for previous, current in zip(result.tool_calls, result.tool_calls[1:])
        if previous.get("tool_name") == current.get("tool_name")
        and previous.get("args") == current.get("args")
        and previous.get("result") == current.get("result")
        and previous.get("error") == current.get("error")
    )
    for warning in result.invariant_warnings:
        _add(result, "WARN", "invariant.warning", warning, "P2")

    values = {
        "duration_seconds": result.durations.get("total") or 0,
        "total_model_calls": sum(result.model_calls_by_stage.values()),
        "input_tokens": result.input_tokens,
        "total_tokens": result.total_tokens,
        # Raw count, kept for reporting (report.py's "Recovery-heavy" hot
        # spot list reads result.recoveries directly, not this value) but
        # deliberately NOT threshold-checked as an absolute number below --
        # see recovery_count_per_task.
        "recovery_count": len(result.recoveries),
        # Bounded-recovery semantics (MAX_TASK_ATTEMPTS=2 in camel_service.py)
        # allow AT MOST one recovery per task: an initial attempt plus one
        # retry, never more. A flat per-JOB threshold (the old
        # "recovery_count": {"warn": 2}) was therefore miscalibrated against
        # the app's own normal decomposition policy (2-4 independent
        # subtasks is the expected default range) -- a 3- or 4-subtask job
        # in which every subtask legitimately needed its one allowed retry
        # is not anomalous, it is the documented worst case, yet it already
        # exceeded "warn=2" before anything was actually wrong (observed:
        # live.workforce.skills, 4 tasks, 3 legitimate one-per-task
        # retries). Comparing per-task instead makes the threshold track
        # what the architecture actually bounds: at most 1.0 recoveries per
        # task is architecturally the ceiling, so fail is set just above
        # that (over-recovery would indicate a real bug in the bound
        # itself), and warn flags when a majority of tasks needed repair.
        "recovery_count_per_task": len(result.recoveries) / max(1, result.task_count or 1),
        "tool_call_count": len(result.tool_calls),
        # NOT threshold-checked directly below (report.py's "Duplicate/
        # tool-loop" hot-spot list reads result.duplicate_tool_calls
        # directly, so the raw tool-agnostic total is kept for that), but
        # its own blanket FAIL threshold is retired in favor of the two
        # split metrics right below -- see MEMOIZED_TOOL_NAMES. Treating a
        # memoized tool's duplicate dispatch as equally expensive as a
        # non-memoized one's was exactly the miscalibration the
        # memoization fix (this session) made incorrect: the underlying
        # work for a memoized duplicate no longer re-executes at all.
        "duplicate_tool_calls": result.duplicate_tool_calls,
        # Cheap by construction (a cached return, no re-parse/re-read) --
        # still worth a WARN as a dispatch-pattern signal (it may still
        # indicate a model looping, or a provider/streaming quirk
        # re-issuing calls -- see run_single_agent's DIRECT double-
        # dispatch investigation), but never a resource-cost FAIL, since
        # the expensive part provably did not happen twice.
        "duplicate_tool_calls_memoized": result.duplicate_tool_calls_memoized,
        # Not memoized anywhere -- a duplicate dispatch of one of these
        # tools (search_web across agents, run_python, ...) IS a second real execution
        # (a second network call, a second code run), so this keeps the
        # original, stricter fail-eligible threshold.
        "duplicate_tool_calls_unmemoized": result.duplicate_tool_calls_unmemoized,
        "file_rows": sum(int(f.get("rows", 0) or 0) for f in result.file_accesses),
        "file_characters": sum(int(f.get("characters", 0) or 0) for f in result.file_accesses),
    }
    if len(result.context_growth) >= 2 and result.context_growth[0] > 0:
        values["context_growth_ratio"] = max(result.context_growth) / result.context_growth[0]
    limits = _thresholds(defaults, scenario)
    for name, value in values.items():
        if name in limits:
            _check_limit(result, name, value, limits[name])

    expected = scenario.expected
    if expected.get("strategy") and result.strategy_selected not in expected["strategy"]:
        _add(result, "FAIL", "routing.strategy", f"Expected strategy {expected['strategy']}, got {result.strategy_selected}", "P1", "routing")
    if expected.get("status") and result.final_status not in expected["status"]:
        _add(result, "FAIL", "status.unexpected", f"Expected status {expected['status']}, got {result.final_status}", "P1", "lifecycle")
    if result.raw.get("command_exit_code", 0) != 0:
        _add(result, "FAIL", "command.failed", f"Regression command exited {result.raw['command_exit_code']}", "P1", result.category)
    if result.raw.get("skipped"):
        _add(result, "WARN", "validation.skipped", str(result.raw["skipped"]), "P2", result.category)
    result.outcome = max((f.outcome for f in result.findings), key=lambda x: RANK[x], default="PASS")
    return result
