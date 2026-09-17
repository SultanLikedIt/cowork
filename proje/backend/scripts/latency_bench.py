"""Latency measurement for the Workforce web-research path.

Two measurements, both meant to be run before and after a change and diffed:

``search`` -- times the discovery layer in isolation (no model calls, no
cost): each backend web_search.search_web() makes up to four HTTP requests
(SearXNG general/news, AgentSearch general/news), and this reports how long
each one and the whole call take. Must run where SEARXNG_BASE_URL and
AGENT_SEARCH_BASE_URL resolve, i.e. inside the backend container:

    docker compose exec backend python scripts/latency_bench.py search --label before

``jobs`` -- submits real Workforce jobs to a running backend over HTTP and
breaks each one down from the job's own timing log (usage.timeline): model
calls per stage, web search / page read tool time, wall-clock from submit to
terminal status, and time until the first answer text appears. Spends model
tokens. Runs from the host:

    python scripts/latency_bench.py jobs --base-url http://127.0.0.1:8001 --label before

Results go to backend/latency-results/<mode>-<label>.json (gitignored).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

RESULTS_DIR = Path(__file__).resolve().parent.parent / "latency-results"

DEFAULT_SEARCH_QUERIES = [
    "güncel dolar kuru",
    "Galatasaray son maç sonucu",
    "Türkiye enflasyon oranı 2026",
    "elektrikli araç pazarı Türkiye satış rakamları",
    "python asyncio gather timeout",
]

# Chat-only research asks (no file) so the numbers isolate the web path:
# no Document worker, no artifact rendering.
DEFAULT_JOB_PROMPTS = [
    "güncel dolar kuru ne kadar",
    "Galatasaray'ın oynadığı son maçın skoru neydi",
    "Türkiye'de açıklanan son yıllık enflasyon oranı kaç",
    "Türkiye'de elektrikli araç pazarının durumunu, avantajlarını ve önündeki engelleri karşılaştırmalı olarak araştır",
]

TERMINAL = {"done", "partial", "failed", "error", "cancelled", "interrupted"}


def _write(mode: str, label: str, payload: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{mode}-{label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _timed(func: Callable[[], Any]) -> tuple[float, Any]:
    started = time.monotonic()
    value = func()
    return time.monotonic() - started, value


def _hits(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return sum(1 for item in value if isinstance(item, dict) and not item.get("error"))


def run_search(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import agent_search_client
    import web_search

    queries = args.query or DEFAULT_SEARCH_QUERIES
    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeat):
        for query in queries:
            news = web_search._query_wants_news_blend(query)
            row: dict[str, Any] = {"query": query, "repeat": repeat, "news_blend": news}
            # Components first, each against a cold path of its own; the
            # full call runs last so its number includes whatever caching
            # the sidecar does -- read it as the realistic end-to-end cost.
            parts: dict[str, Callable[[], Any]] = {
                "searxng_general": lambda: web_search._searx_raw(
                    query, categories="general", language=None, time_range=None, limit=10
                ),
            }
            if news:
                parts["searxng_news"] = lambda: web_search._searx_raw(
                    query, categories="news", language=None, time_range=None, limit=10
                )
            if agent_search_client.is_enabled():
                # Through web_search's own wrapper, so the configured
                # AGENT_SEARCH_GENERAL_MODE is what gets timed.
                if web_search.AGENT_SEARCH_GENERAL_MODE not in web_search._AGENT_SEARCH_MODE_OFF:
                    parts["agent_general"] = lambda: web_search._agent_search_raw(
                        query, categories="general", limit=10
                    )
                if news:
                    parts["agent_news"] = lambda: web_search._agent_search_raw(
                        query, categories="news", limit=10
                    )
            for name, func in parts.items():
                seconds, value = _timed(func)
                row[name] = {"seconds": round(seconds, 3), "hits": _hits(value)}
            seconds, value = _timed(
                lambda: web_search.search_web(query, number_of_result_pages=10)
            )
            row["search_web"] = {"seconds": round(seconds, 3), "hits": _hits(value)}
            rows.append(row)
            print(
                f"{query[:45]:45} "
                + " ".join(
                    f"{name}={row[name]['seconds']:.2f}s/{row[name]['hits']}"
                    for name in list(parts) + ["search_web"]
                ),
                flush=True,
            )

    summary: dict[str, dict[str, float]] = {}
    for name in ("searxng_general", "searxng_news", "agent_general", "agent_news", "search_web"):
        values = [row[name]["seconds"] for row in rows if name in row]
        if values:
            summary[name] = {
                "n": len(values),
                "mean_s": round(statistics.mean(values), 3),
                "median_s": round(statistics.median(values), 3),
                "max_s": round(max(values), 3),
            }
    print("\nsummary:")
    for name, stats in summary.items():
        print(f"  {name:16} n={stats['n']} mean={stats['mean_s']}s median={stats['median_s']}s max={stats['max_s']}s")
    path = _write(
        "search",
        args.label,
        {
            "label": args.label,
            "agent_search_enabled": agent_search_client.is_enabled(),
            "rows": rows,
            "summary": summary,
        },
    )
    print(f"\nwrote {path}")
    return 0


def _breakdown(job: dict[str, Any]) -> dict[str, Any]:
    usage = job.get("usage") or {}
    timeline = usage.get("timeline") or []
    by_name: dict[str, dict[str, float]] = defaultdict(
        lambda: {"count": 0, "total_s": 0.0, "max_s": 0.0}
    )
    for event in timeline:
        key = f"{event.get('kind')}:{event.get('name')}"
        seconds = float(event.get("duration_ms") or 0) / 1000
        bucket = by_name[key]
        bucket["count"] += 1
        bucket["total_s"] = round(bucket["total_s"] + seconds, 3)
        bucket["max_s"] = round(max(bucket["max_s"], seconds), 3)
    return dict(sorted(by_name.items(), key=lambda item: -item[1]["total_s"]))


def run_jobs(args: argparse.Namespace) -> int:
    import requests

    base = args.base_url.rstrip("/")
    workspace = requests.post(
        f"{base}/workspaces", json={"name": "latency-bench"}, timeout=30
    )
    workspace.raise_for_status()
    workspace_id = workspace.json()["workspace_id"]

    prompts = args.prompt or DEFAULT_JOB_PROMPTS
    results: list[dict[str, Any]] = []
    for repeat in range(args.repeat):
        for prompt in prompts:
            submitted = time.monotonic()
            response = requests.post(
                f"{base}/run-pipeline",
                json={
                    "topic": prompt,
                    "mode": "multi",
                    "workspace_id": workspace_id,
                    "deep_research": args.deep,
                },
                timeout=120,
            )
            response.raise_for_status()
            job_id = response.json()["job_id"]
            accepted_s = time.monotonic() - submitted
            first_text_s = None
            job: dict[str, Any] = {}
            while True:
                job = requests.get(f"{base}/jobs/{job_id}", timeout=30).json()
                if first_text_s is None and (job.get("streaming_text") or job.get("result")):
                    first_text_s = time.monotonic() - submitted
                if job.get("status") in TERMINAL:
                    break
                if time.monotonic() - submitted > args.timeout:
                    print(f"  timeout waiting for {job_id}", flush=True)
                    break
                time.sleep(args.poll)
            wall_s = time.monotonic() - submitted
            usage = job.get("usage") or {}
            entry = {
                "prompt": prompt,
                "repeat": repeat,
                "job_id": job_id,
                "status": job.get("status"),
                "accepted_s": round(accepted_s, 2),
                "first_text_s": round(first_text_s, 2) if first_text_s is not None else None,
                "wall_s": round(wall_s, 2),
                "duration_seconds": job.get("duration_seconds"),
                "model_calls": usage.get("model_call_count"),
                "total_tokens": usage.get("total_tokens"),
                "result_chars": len(job.get("result") or ""),
                "breakdown": _breakdown(job),
            }
            results.append(entry)
            print(
                f"{prompt[:50]:50} status={entry['status']} wall={entry['wall_s']}s "
                f"exec={entry['duration_seconds'] and round(entry['duration_seconds'], 1)}s "
                f"first_text={entry['first_text_s']}s calls={entry['model_calls']}",
                flush=True,
            )
            for key, stats in list(entry["breakdown"].items())[:8]:
                print(f"    {key:28} n={stats['count']:<3} total={stats['total_s']:.1f}s max={stats['max_s']:.1f}s")

    path = _write(
        "jobs",
        args.label,
        {"label": args.label, "base_url": base, "deep": args.deep, "results": results},
    )
    print(f"\nwrote {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    search = sub.add_parser("search", help="time the discovery layer only (no model calls)")
    search.add_argument("--label", default="run")
    search.add_argument("--repeat", type=int, default=1)
    search.add_argument("--query", action="append", help="override the default query set")
    search.set_defaults(func=run_search)

    jobs = sub.add_parser("jobs", help="submit real Workforce jobs and break down their timing")
    jobs.add_argument("--label", default="run")
    jobs.add_argument("--base-url", default="http://127.0.0.1:8001")
    jobs.add_argument("--repeat", type=int, default=1)
    jobs.add_argument("--prompt", action="append", help="override the default prompt set")
    jobs.add_argument("--deep", action="store_true")
    jobs.add_argument("--poll", type=float, default=0.5)
    jobs.add_argument("--timeout", type=float, default=900)
    jobs.set_defaults(func=run_jobs)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
