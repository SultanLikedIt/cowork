"""Repeatable end-to-end benchmark for the execution-strategy scenarios.

Drives the REAL /run-pipeline handler (routing, strategy dispatch, CAMEL
execution, synthesis, telemetry) against the real configured model, in an
isolated temp DATA_DIR so it never touches the dev database. This is the
harness used to produce the before/after numbers in the efficiency work --
it is a measurement tool, not a unit test (it needs a live model endpoint).

    python bench_workforce.py                # all scenarios
    python bench_workforce.py C D            # only scenarios C and D
    python bench_workforce.py --label before # tag the output file

Writes a JSON report next to the script (bench_result_<label>.json) so two
runs can be diffed field by field.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_bench_"))

import db  # noqa: E402
import main  # noqa: E402

SAMPLE_DOC = (
    "QUARTERLY ENGINEERING REPORT - Q2\n\n"
    "1. Reliability: p99 latency fell from 840ms to 310ms after the caching "
    "layer rollout. Error budget consumption dropped to 12% for the quarter.\n\n"
    "2. Delivery: 14 of 16 planned features shipped. The two deferred items "
    "(multi-region failover, audit-log export) moved to Q3 due to a vendor "
    "dependency.\n\n"
    "3. Cost: infrastructure spend decreased 18% quarter-over-quarter, driven "
    "mainly by rightsizing the batch-processing fleet.\n\n"
    "4. Risks: on-call load remains concentrated on three engineers. "
    "Recommendation is to expand the rotation before Q3 feature work begins.\n"
)

# name -> (topic, mode, needs_upload)
SCENARIOS = {
    "A": ("Explain attention in simple terms.", "single", False),
    "B": ("Explain attention in simple terms.", "multi", False),
    "C": (
        "Research the current stable PostgreSQL version and summarize its main "
        "features with sources.",
        "multi",
        False,
    ),
    "D": (
        "En güncel PostgreSQL sürümünü araştır ve ana özelliklerini kaynaklarla özetle.",
        "multi",
        False,
    ),
    "E": ("Summarize the key points of the attached document.", "multi", True),
    "F": (
        "Analyze the attached document and compare it with current information "
        "from the web.",
        "multi",
        True,
    ),
    "G": (
        "Produce a competitive briefing on managed Postgres offerings: research "
        "current pricing and features for at least two vendors, then write a "
        "structured comparison and a separate reviewed executive summary.",
        "multi",
        False,
    ),
}


async def _upload_sample() -> str:
    """Register a sample text document through the real upload path."""
    import io

    from fastapi import UploadFile

    upload = UploadFile(
        filename="q2_engineering_report.txt",
        file=io.BytesIO(SAMPLE_DOC.encode("utf-8")),
    )
    result = await main.upload_file(upload)
    return result.file_id


async def _run_scenario(name: str) -> dict:
    topic, mode, needs_upload = SCENARIOS[name]
    file_ids = [await _upload_sample()] if needs_upload else []

    req = main.RunPipelineRequest(topic=topic, mode=mode, file_ids=file_ids)
    started = time.monotonic()
    response = await main.run_pipeline(req)

    # run_pipeline schedules _run_job via asyncio.create_task; wait it out.
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        job = main._JOBS[response.job_id]
        if job["status"] in {"done", "partial", "failed", "error", "cancelled"}:
            break
        await asyncio.sleep(0.25)
    elapsed = time.monotonic() - started

    job = main._JOBS[response.job_id]
    usage = job["usage"]
    by_stage = usage.get("by_stage") or {}
    tool_calls = main._JOB_CONTROLS[response.job_id].tool_call_recorder.snapshot()
    searches = [c for c in tool_calls if c.get("tool_name") == "search_web"]

    return {
        "scenario": name,
        "topic": topic,
        "mode": mode,
        "status": job["status"],
        "runtime_s": round(elapsed, 1),
        "strategy": usage.get("execution_strategy"),
        "role": usage.get("execution_strategy_role"),
        "routing_outcome": usage.get("routing_outcome"),
        "total_calls": usage.get("model_call_count"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "by_stage": {
            k: {"calls": v["model_call_count"], "tokens": v["total_tokens"]}
            for k, v in sorted(by_stage.items())
        },
        "task_count": len(job.get("task_plan") or []),
        "real_search_calls": len(searches),
        "result_chars": len(job.get("result") or ""),
        "result_preview": (job.get("result") or "")[:240],
        "error": job.get("error"),
    }


def _print_row(r: dict) -> None:
    print(f"\n{'=' * 78}")
    print(f"[{r['scenario']}] {r['topic'][:66]}")
    print(f"{'=' * 78}")
    print(
        f"  status={r['status']}  strategy={r['strategy']}"
        f"{'/' + r['role'] if r['role'] else ''}  runtime={r['runtime_s']}s"
    )
    print(
        f"  calls={r['total_calls']}  tokens={r['total_tokens']} "
        f"(in={r['input_tokens']} out={r['output_tokens']})  "
        f"tasks={r['task_count']}  searches={r['real_search_calls']}"
    )
    for stage, v in r["by_stage"].items():
        print(f"    {stage:<22} calls={v['calls']:<3} tokens={v['tokens']}")
    if r["error"]:
        print(f"  ERROR: {r['error']}")


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", default=None)
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    names = args.scenarios or list(SCENARIOS)
    db.run_migrations()

    results = []
    for name in names:
        if name not in SCENARIOS:
            print(f"skipping unknown scenario {name!r}")
            continue
        try:
            r = await _run_scenario(name)
        except Exception as exc:  # noqa: BLE001 - bench must survive one bad run
            r = {"scenario": name, "status": "BENCH_ERROR", "error": repr(exc)}
            print(f"\n[{name}] BENCH ERROR: {exc!r}")
        else:
            _print_row(r)
        results.append(r)

    out = Path(__file__).with_name(f"bench_result_{args.label}.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n\nwrote {out}")

    print(f"\n{'SUMMARY':=^78}")
    print(f"{'sc':<4}{'status':<9}{'strategy':<13}{'calls':<7}{'tokens':<9}{'secs':<7}")
    for r in results:
        print(
            f"{r['scenario']:<4}{str(r.get('status'))[:8]:<9}"
            f"{str(r.get('strategy'))[:12]:<13}{str(r.get('total_calls')):<7}"
            f"{str(r.get('total_tokens')):<9}{str(r.get('runtime_s')):<7}"
        )


if __name__ == "__main__":
    asyncio.run(_main())
