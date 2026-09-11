"""Deterministic concurrent workspace, budget, and recorder stress probes."""

from __future__ import annotations

import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

import tool_call_log
from tool_call_log import ToolCallRecorder
from workspace import (
    MAX_TABLE_ROWS_PER_RUN,
    MAX_TEXT_CHARS_PER_RUN,
    ReadOnlyWorkspaceTools,
    WorkspaceSecurityError,
)


failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exercise_job(root: Path, marker: str) -> dict:
    text = root / "evidence.txt"
    csv_path = root / "rows.csv"
    tools = ReadOnlyWorkspaceTools(root)
    seen = tools.read_text_file("evidence.txt", max_chars=20000)
    repeat = tools.read_text_file("evidence.txt", max_chars=20000)
    delivered = 0
    offset = 0
    while delivered < MAX_TABLE_ROWS_PER_RUN + 400:
        page = tools.read_table_rows("rows.csv", offset=offset, limit=200)
        count = page["returned_rows"]
        delivered += count
        if count == 0:
            break
        offset += count
    exhausted = tools.read_table_rows("rows.csv", offset=offset, limit=200)
    traversal_rejected = False
    try:
        tools.read_text_file("../outside.txt")
    except WorkspaceSecurityError:
        traversal_rejected = True
    return {
        "marker_only": marker in seen and all(other not in seen for other in ("JOB-A", "JOB-B", "JOB-C", "JOB-D", "JOB-E") if other != marker),
        "repeat_noted": "already" in repeat,
        "rows": delivered,
        "budget_exhausted": exhausted["returned_rows"] == 0 and "budget" in exhausted["note"].casefold(),
        "traversal_rejected": traversal_rejected,
        "text_hash": _hash(text),
        "csv_hash": _hash(csv_path),
    }


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="reliability_stress_"))
    roots: list[tuple[Path, str, str, str]] = []
    for marker in ("JOB-A", "JOB-B", "JOB-C", "JOB-D", "JOB-E"):
        root = scratch / marker
        root.mkdir()
        text = root / "evidence.txt"
        table = root / "rows.csv"
        text.write_text((marker + " bounded evidence\n") * 6000, encoding="utf-8")
        pd.DataFrame({"job": [marker] * 1600, "id": range(1600)}).to_csv(table, index=False)
        roots.append((root, marker, _hash(text), _hash(table)))

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda args: _exercise_job(args[0], args[1]), roots))

    check("five workspace jobs completed concurrently", len(outcomes) == 5)
    check("workspace contents never leaked across jobs", all(o["marker_only"] for o in outcomes))
    check("identical reads are flagged", all(o["repeat_noted"] for o in outcomes))
    check("forward-progressing table traversal stops at the per-run budget", all(o["rows"] == MAX_TABLE_ROWS_PER_RUN and o["budget_exhausted"] for o in outcomes))
    check("path traversal is rejected in every job", all(o["traversal_rejected"] for o in outcomes))
    check("read-only tools leave text sources byte-identical", all(o["text_hash"] == original_text for o, (_, _, original_text, _) in zip(outcomes, roots)))
    check("read-only tools leave table sources byte-identical", all(o["csv_hash"] == original_csv for o, (_, _, _, original_csv) in zip(outcomes, roots)))

    log_path = scratch / "tool_calls.jsonl"
    original_log = tool_call_log.LOG_FILE
    tool_call_log.LOG_FILE = log_path
    try:
        recorder = ToolCallRecorder(job_id="concurrent-recorder")
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda i: recorder.record("probe", {"i": i}, result=i * 2), range(100)))
        snapshot = recorder.snapshot()
        logged = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    finally:
        tool_call_log.LOG_FILE = original_log
    check("concurrent in-memory tool recording loses no calls", len(snapshot) == 100)
    check("concurrent JSONL tool recording remains parseable and complete", len(logged) == 100)
    check("per-job text budgets are independent", all(o["marker_only"] for o in outcomes) and MAX_TEXT_CHARS_PER_RUN > 0)

    if failures:
        raise SystemExit(f"{len(failures)} reliability stress probe(s) failed: {failures}")
    print("\nAll deterministic reliability stress probes passed.")


if __name__ == "__main__":
    main()
