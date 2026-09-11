from __future__ import annotations

import json
from pathlib import Path

from .models import Scenario


ROOT = Path(__file__).resolve().parent


def load_catalog(path: Path | None = None) -> list[Scenario]:
    payload = json.loads((path or ROOT / "scenarios.json").read_text(encoding="utf-8"))
    scenarios: list[Scenario] = []
    for group in payload["groups"]:
        common = {k: v for k, v in group.items() if k != "cases"}
        for case in group["cases"]:
            merged = {**common, **case}
            repeat = int(merged.pop("prompt_repeat", 1))
            if repeat > 1:
                seed = merged.get("prompt", "Reliability context. ")
                merged["prompt"] = (seed * ((repeat // max(1, len(seed))) + 1))[:repeat]
            merged["tags"] = list(dict.fromkeys(common.get("tags", []) + case.get("tags", [])))
            scenarios.append(Scenario.from_dict(merged))
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Scenario ids must be unique")
    return scenarios


def select_scenarios(
    scenarios: list[Scenario], *, suite: str, ids: set[str] | None = None
) -> list[Scenario]:
    selected = scenarios if not ids else [s for s in scenarios if s.id in ids]
    if suite == "quick":
        selected = [s for s in selected if "quick" in s.tags and not s.destructive]
    elif suite == "deterministic":
        selected = [s for s in selected if s.kind != "live"]
    elif suite == "critical-live":
        selected = [s for s in selected if s.kind == "live" and "critical" in s.tags]
    elif suite == "live":
        selected = [s for s in selected if s.kind == "live" and not s.destructive]
    elif suite == "destructive-live":
        selected = [s for s in selected if s.kind == "live"]
    elif suite != "all":
        raise ValueError(f"Unknown suite: {suite}")
    return selected
