from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _triplet(values):
    values = [float(v) for v in values if v is not None]
    return None if not values else {"min": min(values), "median": statistics.median(values), "max": max(values)}


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = defaultdict(list)
    for result in results:
        grouped[result["scenario_id"]].append(result)
    scenarios = {}
    for scenario_id, runs in grouped.items():
        strategies = Counter(r.get("strategy_selected") for r in runs)
        task_counts = Counter(r.get("task_count") for r in runs)
        assignments = Counter(tuple(t.get("assigned_agent") for t in r.get("tasks", [])) for r in runs)
        tools = Counter(tuple(c.get("tool_name") for c in r.get("tool_calls", [])) for r in runs)
        scenarios[scenario_id] = {
            "runs": len(runs),
            "pass_rate": sum(r["outcome"] == "PASS" for r in runs) / len(runs),
            "outcomes": dict(Counter(r["outcome"] for r in runs)),
            "strategy_variance": dict(strategies),
            "task_count_variance": {str(k): v for k, v in task_counts.items()},
            "assignment_variants": len(assignments),
            "tool_call_variants": len(tools),
            "duration_seconds": _triplet(r.get("durations", {}).get("total") for r in runs),
            "total_tokens": _triplet(r.get("total_tokens") for r in runs),
            "recovery_frequency": sum(bool(r.get("recoveries")) for r in runs) / len(runs),
        }
    return {
        "run_count": len(results),
        "scenario_count": len(grouped),
        "outcomes": dict(Counter(r["outcome"] for r in results)),
        "overall": max((r["outcome"] for r in results), key={"PASS": 0, "WARN": 1, "FAIL": 2}.get, default="PASS"),
        "scenarios": scenarios,
    }


def markdown(results: list[dict[str, Any]], summary: dict[str, Any], blind_spots: list[str]) -> str:
    lines = ["# Backend Reliability Report", "", f"Overall: **{summary['overall']}**", "", f"Runs: {summary['run_count']} across {summary['scenario_count']} scenarios. Outcomes: {summary['outcomes']}.", ""]
    findings = [(r, f) for r in results for f in r.get("findings", []) if f["outcome"] != "PASS"]
    lines += ["## Failures and warnings", ""]
    if not findings:
        lines.append("No threshold or invariant findings.")
    else:
        for r, finding in sorted(findings, key=lambda x: (x[1]["outcome"] != "FAIL", x[1]["severity"], x[0]["scenario_id"])):
            lines.append(f"- {finding['outcome']} {finding['severity']} `{r['scenario_id']}` [{finding['category']}]: {finding['message']}")
    lines += ["", "## Hot spots", ""]
    rankings = [
        ("Slowest", lambda r: r.get("durations", {}).get("total") or 0, "s"),
        ("Highest-token", lambda r: r.get("total_tokens") or 0, " tokens"),
        ("Most model-call-heavy", lambda r: sum(r.get("model_calls_by_stage", {}).values()), " calls"),
        ("Recovery-heavy", lambda r: len(r.get("recoveries", [])), " recoveries"),
        ("Duplicate/tool-loop", lambda r: r.get("duplicate_tool_calls", 0), " duplicates"),
    ]
    for title, key, suffix in rankings:
        top = sorted(results, key=key, reverse=True)[:5]
        lines.append(f"### {title}")
        lines.append("")
        lines.extend(f"- `{r['scenario_id']}`: {key(r):.2f}{suffix}" if isinstance(key(r), float) else f"- `{r['scenario_id']}`: {key(r)}{suffix}" for r in top)
        lines.append("")
    lines += ["## Variance", "", "| Scenario | Pass rate | Strategies | Task counts | Assignment variants | Tool variants |", "|---|---:|---|---|---:|---:|"]
    for sid, data in summary["scenarios"].items():
        lines.append(f"| `{sid}` | {data['pass_rate']:.0%} | {data['strategy_variance']} | {data['task_count_variance']} | {data['assignment_variants']} | {data['tool_call_variants']} |")
    lines += ["", "## File robustness", ""]
    file_runs = [r for r in results if r["category"] == "files"]
    lines.append(f"{len(file_runs)} file scenarios ran: {dict(Counter(r['outcome'] for r in file_runs))}.")
    lines += ["", "## Known blind spots", ""] + [f"- {item}" for item in blind_spots] + [""]
    return "\n".join(lines)


def write_outputs(output_dir: Path, results: list[dict[str, Any]], metadata: dict[str, Any], blind_spots: list[str]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = aggregate(results)
    (output_dir / "results.json").write_text(json.dumps({"metadata": metadata, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    with (output_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    (output_dir / "aggregate.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "report.md").write_text(markdown(results, summary, blind_spots), encoding="utf-8")
    return {name: output_dir / name for name in ("results.json", "results.jsonl", "aggregate.json", "report.md")}
