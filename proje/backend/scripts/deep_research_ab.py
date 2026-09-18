"""A/B the deep-research Research worker: survey (angles) vs iter (report loop).

Runs the real LangGraph Research worker (``_run_research``, deep_research
on) against live search, fetch and model -- so it spends model tokens --
once per prompt in the given DEEP_RESEARCH_MODE, and records time, tokens,
pages, cited sources and the full answer. Must run where the backend's
model/search env resolves, i.e. inside the backend container:

    docker compose exec backend python scripts/deep_research_ab.py run --mode survey --label base
    docker compose exec backend python scripts/deep_research_ab.py run --mode iter --label iter
    docker compose exec backend python scripts/deep_research_ab.py compare base iter

Results go to backend/latency-results/deep-ab-<label>.json (gitignored).
Answers are saved in full so quality can be judged side by side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
RESULTS_DIR = BACKEND / "latency-results"

DEFAULT_PROMPTS = [
    "Türkiye'de elektrikli araç pazarının durumunu, avantajlarını ve önündeki "
    "engelleri karşılaştırmalı olarak araştır",
    "İHA/drone sektöründeki son teknolojik gelişmeleri araştır ve özetle.",
    "Avrupa Birliği Yapay Zeka Yasası'nın yürürlük takvimini, risk "
    "kategorilerini ve Türkiye'deki şirketlere etkilerini kapsamlı araştır",
]


def _sources(output: str) -> list[dict[str, str]]:
    marker = "RESEARCH_SOURCES:\n"
    if marker not in output:
        return []
    tail = output.split(marker, 1)[1].split("\n---\n", 1)[0].strip()
    try:
        return json.loads(tail)
    except json.JSONDecodeError:
        return []


async def _one(prompt: str) -> dict[str, Any]:
    from execution.langgraph_runtime import workforce
    from execution.langgraph_runtime.workforce import CancellableWorkforce
    from model_config import UsageTracker

    tracker = UsageTracker()
    with tempfile.TemporaryDirectory() as root:
        started = time.monotonic()
        output = await workforce._run_research(
            content=prompt,
            original_request=prompt,
            tracker=tracker,
            callback=None,
            workspace_root=root,
            tool_call_recorder=None,
            wants_artifact=False,
            handle=CancellableWorkforce(),
            unit_task_id="deep-ab",
            deep_research=True,
        )
        wall = time.monotonic() - started
    events = list(getattr(tracker, "_events", []))
    iterate_inputs = [
        int(e.get("input_tokens") or 0)
        for e in events
        if e.get("kind") == "llm" and e.get("name") == "web_iterate"
    ]
    answer = output.split("\n---\nRESEARCH_SOURCES:", 1)[0].strip()
    return {
        "prompt": prompt,
        "wall_s": round(wall, 1),
        "model_calls": tracker.model_call_count,
        "input_tokens": tracker.input_tokens,
        "output_tokens": tracker.output_tokens,
        "stages": dict(getattr(tracker, "_stages", {})),
        "iterate_input_tokens": iterate_inputs,
        "sources": _sources(output),
        "answer_chars": len(answer),
        "answer_sections": len(re.findall(r"(?m)^#{1,3} ", answer)),
        "answer": answer,
    }


def run(args: argparse.Namespace) -> int:
    os.environ["DEEP_RESEARCH_MODE"] = args.mode
    prompts = args.prompt or DEFAULT_PROMPTS
    results = []
    for prompt in prompts:
        print(f"[{args.mode}] {prompt[:70]}", flush=True)
        entry = asyncio.run(_one(prompt))
        entry["mode"] = args.mode
        results.append(entry)
        print(
            f"    wall={entry['wall_s']}s calls={entry['model_calls']} "
            f"in={entry['input_tokens']} out={entry['output_tokens']} "
            f"sources={len(entry['sources'])} answer={entry['answer_chars']} chars",
            flush=True,
        )
        if entry["iterate_input_tokens"]:
            print(f"    iterate input tokens per round: {entry['iterate_input_tokens']}")
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"deep-ab-{args.label}.json"
    path.write_text(
        json.dumps({"mode": args.mode, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0


def compare(args: argparse.Namespace) -> int:
    runs = [
        json.loads((RESULTS_DIR / f"deep-ab-{label}.json").read_text(encoding="utf-8"))
        for label in (args.a, args.b)
    ]
    keys = ("wall_s", "model_calls", "input_tokens", "output_tokens", "answer_chars", "answer_sections")
    for index, prompt_entry in enumerate(runs[0]["results"]):
        print(f"\n{prompt_entry['prompt'][:80]}")
        for label, run_data in zip((args.a, args.b), runs):
            entry = run_data["results"][index]
            stats = " ".join(f"{k}={entry[k]}" for k in keys)
            print(f"  {label:10} {stats} sources={len(entry['sources'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=("survey", "iter"), required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--prompt", action="append")
    r.set_defaults(func=run)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.set_defaults(func=compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
