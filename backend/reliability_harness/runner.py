from __future__ import annotations

import json
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import run_command, run_live
from .evaluate import evaluate
from .models import RunResult, Scenario


def load_defaults(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def execute(
    scenarios: list[Scenario], *, repetitions: int, repo_root: Path,
    base_url: str, defaults: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    batch_id = f"rel-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        for repetition in range(1, repetitions + 1):
            run_id = f"{batch_id}:{scenario.id}:{repetition}"
            result = RunResult.empty(scenario, repetition, run_id)
            if scenario.kind == "command":
                run_command(scenario, result, repo_root, float(scenario.thresholds.get("command_timeout", {}).get("fail", 180)))
            elif scenario.kind == "live":
                run_live(scenario, result, base_url, defaults)
            else:
                result.failure_reason = f"Unsupported scenario kind: {scenario.kind}"
            evaluate(result, scenario, defaults)
            results.append(result.to_dict())
            print(f"[{result.outcome}] {scenario.id} repetition={repetition} status={result.final_status}", flush=True)
    metadata = {
        "batch_id": batch_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "base_url": base_url,
        "repetitions": repetitions,
    }
    return results, metadata
