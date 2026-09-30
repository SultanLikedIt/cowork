from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .catalog import ROOT, load_catalog, select_scenarios
from .report import aggregate, write_outputs
from .runner import execute, load_defaults


REPO_ROOT = ROOT.parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "backend" / "reliability-results"
BLIND_SPOTS = [
    "Deterministic command scenarios validate policy and bounded behavior with stubs; they do not predict provider latency or nondeterministic model quality.",
    "Router/planning/assignment/worker-creation wall-clock phase durations are logged by the app but are not all exposed in the job API, so unavailable fields remain null.",
    "Quality scores and recovery types are only partially exposed; repeated task attempts are reported as retry_or_replan when the exact type is unavailable.",
    "Context size per model turn is not exposed by the current API; stage input tokens are captured, but quadratic retained-result growth can only be inferred.",
    "Disconnect/reconnect is exercised through polling persistence rather than transport-level SSE packet fault injection.",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reliability-harness", description="Backend reliability/stress harness")
    parser.add_argument("--catalog", type=Path, default=ROOT / "scenarios.json")
    parser.add_argument("--config", type=Path, default=ROOT / "defaults.json")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--suite", choices=["quick", "deterministic", "live", "critical-live", "destructive-live", "all"], default="quick")
    run.add_argument("--scenario", action="append", default=[])
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--base-url", default="http://127.0.0.1:8000")
    run.add_argument("--output", type=Path)
    run.add_argument("--allow-destructive-live", action="store_true")
    sub.add_parser("list")
    estimate = sub.add_parser("estimate")
    estimate.add_argument("--suite", choices=["live", "critical-live", "destructive-live"], default="live")
    estimate.add_argument("--repeat", type=int, default=1)
    compare = sub.add_parser("compare")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", type=Path)
    return parser


def _load_result_file(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["results"] if isinstance(payload, dict) and "results" in payload else payload


def _compare(baseline, candidate):
    a, b = aggregate(baseline), aggregate(candidate)
    shared = sorted(set(a["scenarios"]) & set(b["scenarios"]))
    rows = []
    for sid in shared:
        av, bv = a["scenarios"][sid], b["scenarios"][sid]
        am = (av.get("duration_seconds") or {}).get("median")
        bm = (bv.get("duration_seconds") or {}).get("median")
        at = (av.get("total_tokens") or {}).get("median")
        bt = (bv.get("total_tokens") or {}).get("median")
        rows.append({"scenario_id": sid, "pass_rate_delta": bv["pass_rate"] - av["pass_rate"], "duration_median_delta": None if am is None or bm is None else bm-am, "token_median_delta": None if at is None or bt is None else bt-at})
    return {"baseline": a, "candidate": b, "shared_scenarios": len(shared), "deltas": rows}


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    scenarios = load_catalog(args.catalog)
    if args.command == "list":
        for scenario in scenarios:
            print(f"{scenario.id:38} {scenario.category:12} {scenario.kind:8} {','.join(scenario.tags)}")
        print(f"\n{len(scenarios)} scenarios by category: {dict(Counter(s.category for s in scenarios))}")
        return 0
    if args.command == "estimate":
        selected = select_scenarios(scenarios, suite=args.suite)
        calls = sum(int(s.expected.get("estimated_model_calls", 8)) for s in selected) * args.repeat
        input_tokens = calls * 6000
        output_tokens = calls * 1000
        print(json.dumps({"suite": args.suite, "scenarios": len(selected), "repetitions": args.repeat, "runs": len(selected)*args.repeat, "estimated_model_calls": calls, "planning_envelope_input_tokens": input_tokens, "planning_envelope_output_tokens": output_tokens, "estimated_cost_usd": "provider/model-rate dependent", "cost_formula": f"({input_tokens} * input_rate_per_token) + ({output_tokens} * output_rate_per_token)", "executed": False}, indent=2))
        return 0
    if args.command == "compare":
        comparison = _compare(_load_result_file(args.baseline), _load_result_file(args.candidate))
        text = json.dumps(comparison, indent=2)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        print(text)
        return 0
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.suite == "destructive-live" and not args.allow_destructive_live:
        raise SystemExit("Refusing destructive live suite without --allow-destructive-live")
    selected = select_scenarios(scenarios, suite=args.suite, ids=set(args.scenario) or None)
    if args.scenario and len(selected) != len(set(args.scenario)):
        found = {s.id for s in selected}
        raise SystemExit(f"Unknown or suite-excluded scenarios: {sorted(set(args.scenario)-found)}")
    defaults = load_defaults(args.config)
    results, metadata = execute(selected, repetitions=args.repeat, repo_root=REPO_ROOT, base_url=args.base_url, defaults=defaults)
    output = args.output or DEFAULT_OUTPUT_ROOT / metadata["batch_id"]
    paths = write_outputs(output, results, metadata, BLIND_SPOTS)
    summary = aggregate(results)
    print(f"Overall={summary['overall']} outcomes={summary['outcomes']} report={paths['report.md']}")
    return 1 if summary["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
