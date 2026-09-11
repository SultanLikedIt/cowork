from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Scenario:
    id: str
    name: str
    category: str
    kind: str
    prompt: str = ""
    mode: str = "multi"
    tags: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    destructive: bool = False
    operation: str = "run"
    turns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Scenario":
        return cls(**value)


@dataclass
class Finding:
    outcome: str
    code: str
    message: str
    severity: str = "P2"
    category: str = "invariant"


@dataclass
class RunResult:
    schema_version: str
    harness_version: str
    scenario_id: str
    scenario_name: str
    category: str
    repetition: int
    timestamp: str
    run_id: str
    job_id: str | None = None
    prompt: str | None = None
    prompt_reference: str | None = None
    prompt_character_count: int = 0
    strategy_selected: str | None = None
    routing_outcome: str | None = None
    durations: dict[str, float | None] = field(default_factory=dict)
    task_count: int = 0
    tasks: list[dict[str, Any]] = field(default_factory=list)
    fixed_agent_count: int | None = None
    dynamic_agent_count: int | None = None
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    quality: list[dict[str, Any]] = field(default_factory=list)
    model_calls_by_stage: dict[str, int] = field(default_factory=dict)
    tokens_by_stage: dict[str, dict[str, int]] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    duplicate_tool_calls: int = 0
    # Split from duplicate_tool_calls (which stays the raw, tool-agnostic
    # total for report.py's existing hot-spot list): how many of those
    # duplicates were dispatches of a tool whose real work is memoized
    # (workspace tools and model-facing web_search/web_read) versus one that has no
    # job-wide guarantee (internal search_web across agents, run_python,
    # ...) and therefore may genuinely re-execute. See evaluate.py's
    # MEMOIZED_TOOL_NAMES.
    duplicate_tool_calls_memoized: int = 0
    duplicate_tool_calls_unmemoized: int = 0
    # tool_dispatch_count: every logged_tool invocation, memoized or not
    #   (== len(tool_calls); kept as its own field for readability at call
    #   sites rather than re-deriving it).
    # tool_execution_count: dispatches that did real underlying work --
    #   every dispatch of a non-memoized tool, plus one per unique
    #   (tool, args) key for memoized tools (memoization guarantees a
    #   repeat of the same key never re-executes).
    # tool_cache_hit_count: dispatches that were memoized and did NOT do
    #   real work (== tool_dispatch_count - tool_execution_count, and also
    #   == duplicate_tool_calls_memoized).
    tool_dispatch_count: int = 0
    tool_execution_count: int = 0
    tool_cache_hit_count: int = 0
    no_progress_calls: int = 0
    file_accesses: list[dict[str, Any]] = field(default_factory=list)
    context_growth: list[int] = field(default_factory=list)
    final_status: str = "error"
    failure_stage: str | None = None
    failure_reason: str | None = None
    output_character_count: int = 0
    output_truncated: bool = False
    invariant_warnings: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    outcome: str = "FAIL"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, scenario: Scenario, repetition: int, run_id: str) -> "RunResult":
        return cls(
            schema_version="1.0",
            harness_version="1.0.0",
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            category=scenario.category,
            repetition=repetition,
            timestamp=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            prompt=scenario.prompt or None,
            prompt_character_count=len(scenario.prompt),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
