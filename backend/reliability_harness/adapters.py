from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import io
import csv
import zipfile
from pathlib import Path
from typing import Any

from .models import RunResult, Scenario


TERMINAL = {"done", "partial", "failed", "error", "cancelled", "interrupted"}
SECRET_KEYS = {"authorization", "api_key", "apikey", "password", "secret", "token"}


def _bounded(value: Any, limit: int) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.casefold() in SECRET_KEYS else _bounded(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_bounded(v, limit) for v in value[:50]]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[truncated {len(value) - limit} chars]"
    return value


def run_command(scenario: Scenario, result: RunResult, repo_root: Path, timeout: float) -> RunResult:
    command = [part.replace("{python}", sys.executable) for part in scenario.command]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        result.raw = {
            "command": command,
            "command_exit_code": completed.returncode,
            "stdout": completed.stdout[-20000:],
            "stderr": completed.stderr[-20000:],
        }
        unavailable = "Could not reach http://127.0.0.1:8000" in completed.stdout
        if unavailable:
            result.raw["skipped"] = "Local API server unavailable; upload smoke was not exercised"
        result.final_status = "done" if completed.returncode == 0 else "error"
        result.failure_stage = None if completed.returncode == 0 else "deterministic_test"
        result.failure_reason = completed.stderr[-4000:] or completed.stdout[-4000:] if completed.returncode else None
        result.output_character_count = len(completed.stdout)
    except subprocess.TimeoutExpired as exc:
        result.final_status = "error"
        result.failure_stage = "deterministic_test"
        result.failure_reason = f"Command timed out after {timeout}s"
        result.raw = {"command": command, "command_exit_code": 124, "stdout": (exc.stdout or "")[-20000:]}
    result.durations["total"] = time.monotonic() - started
    return result


def _request(base_url: str, method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc


def _fixture(spec: dict[str, Any]) -> tuple[str, bytes]:
    kind = spec.get("type", "txt")
    name = spec.get("name", f"fixture.{kind}")
    rows = int(spec.get("rows", 20))
    size = int(spec.get("characters", 1000))
    if kind in {"txt", "md", "yaml", "log", "py"}:
        prefix = {"md": "# Reliability fixture\n", "yaml": "fixture: true\nitems:\n", "log": "2026-01-01 INFO ready\n", "py": "def probe():\n    return 'ready'\n"}.get(kind, "Reliability fixture\n")
        return name, (prefix + ("bounded-content " * ((size // 16) + 1)))[:size].encode()
    if kind == "json":
        return name, json.dumps({"items": [{"id": i, "value": f"v{i}"} for i in range(rows)]}).encode()
    if kind == "csv":
        out = io.StringIO(newline="")
        writer = csv.writer(out); writer.writerow(["id", "group", "value"])
        writer.writerows((i, f"g{i % 5}", i * 3) for i in range(rows))
        return name, out.getvalue().encode()
    if kind == "xlsx":
        from openpyxl import Workbook
        stream = io.BytesIO(); workbook = Workbook(); sheet = workbook.active; sheet.title = "Primary"
        sheet.append(["id", "group", "value"])
        for i in range(rows): sheet.append([i, f"g{i % 5}", i * 3])
        if spec.get("sheets", 1) > 1:
            second = workbook.create_sheet("Secondary"); second.append(["id", "note"])
            for i in range(rows // 2): second.append([i, f"note-{i}"])
        workbook.save(stream); return name, stream.getvalue()
    if kind == "docx":
        from docx import Document
        stream = io.BytesIO(); document = Document(); document.add_heading("Reliability fixture", 0); document.add_paragraph("bounded content " * 50); document.save(stream); return name, stream.getvalue()
    if kind == "pdf":
        from reportlab.pdfgen.canvas import Canvas
        stream = io.BytesIO(); canvas = Canvas(stream); canvas.drawString(72, 750, "Reliability fixture bounded content"); canvas.save(); return name, stream.getvalue()
    if kind == "zero":
        return name, b""
    if kind == "malformed_csv":
        return name, b'"unterminated,column\n1,2\n'
    if kind == "malformed_xlsx":
        return name, b"PK\x03\x04not-an-xlsx"
    if kind == "zip":
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive: archive.writestr("inside.txt", "content")
        return name, stream.getvalue()
    return name, bytes((i % 251 for i in range(max(1, size))))


def _upload(base_url: str, name: str, content: bytes) -> str:
    boundary = f"----reliability{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(base_url.rstrip("/") + "/upload", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())["file_id"]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} /upload: {exc.read().decode(errors='replace')}") from exc


def _normalize_live(snapshot: dict[str, Any], result: RunResult, max_arg_chars: int) -> RunResult:
    usage = snapshot.get("usage") or {}
    result.job_id = snapshot.get("job_id")
    result.strategy_selected = usage.get("execution_strategy")
    result.routing_outcome = usage.get("routing_outcome")
    result.final_status = snapshot.get("status", "error")
    result.failure_reason = _bounded(snapshot.get("error"), 4000)
    result.failure_stage = "execution" if result.failure_reason else None
    result.durations["total"] = snapshot.get("duration_seconds") or usage.get("total_duration_seconds")
    by_stage = usage.get("by_stage") or {}
    result.model_calls_by_stage = {stage: int(data.get("model_call_count", 0)) for stage, data in by_stage.items()}
    result.tokens_by_stage = {
        stage: {key: int(data.get(key, 0)) for key in ("input_tokens", "output_tokens", "total_tokens")}
        for stage, data in by_stage.items()
    }
    result.input_tokens = int(usage.get("input_tokens", 0))
    result.output_tokens = int(usage.get("output_tokens", 0))
    result.total_tokens = int(usage.get("total_tokens", 0))
    result.tasks = []
    attempts = snapshot.get("task_attempts") or {}
    for task in snapshot.get("task_plan") or []:
        task_id = task.get("id")
        count = int(attempts.get(task_id, 0))
        result.tasks.append({
            "id": task_id,
            "text": task.get("description") or task.get("title"),
            "dependencies": task.get("dependencies") or [],
            "assigned_agent": task.get("assigned_agent"),
            "status": (snapshot.get("task_states") or {}).get(task_id),
            "attempts": count,
            "output_characters": len((snapshot.get("task_outputs") or {}).get(task_id, "")),
        })
        if count > 1:
            result.recoveries.extend({"task_id": task_id, "type": "retry_or_replan", "attempt": i} for i in range(2, count + 1))
    result.task_count = len(result.tasks)
    fixed_names = {"Research Agent", "Document Agent", "Coding Agent"}
    assignees = {t["assigned_agent"] for t in result.tasks if t.get("assigned_agent")}
    result.fixed_agent_count = len(assignees & fixed_names)
    result.dynamic_agent_count = len(assignees - fixed_names)
    result.tool_calls = [
        {**call, "args": _bounded(call.get("args", {}), max_arg_chars), "result": _bounded(call.get("result"), max_arg_chars)}
        for call in snapshot.get("tool_calls") or []
    ]
    file_tools = {"list_files", "inspect_file", "read_text_file", "search_text", "inspect_table", "read_table_rows", "search_table", "table_summary", "compare_tables"}
    result.file_accesses = [
        {"tool": call.get("tool_name"), "path": (call.get("args") or {}).get("relative_path"), "rows": (call.get("result") or {}).get("returned_rows", 0) if isinstance(call.get("result"), dict) else 0, "characters": len(call.get("result", "")) if isinstance(call.get("result"), str) else 0}
        for call in result.tool_calls if call.get("tool_name") in file_tools
    ]
    final_text = snapshot.get("result") or ""
    result.output_character_count = len(final_text)
    lowered = final_text.casefold()
    result.output_truncated = any(marker in lowered for marker in ("[truncated", "output limit", "maximum output"))
    nonterminal_tasks = [t["id"] for t in result.tasks if t.get("status") not in {"completed", "failed", "cancelled"}]
    if result.final_status in TERMINAL and nonterminal_tasks:
        result.invariant_warnings.append(f"Terminal job retained non-terminal tasks: {nonterminal_tasks}")
    result.raw = {"snapshot": _bounded(snapshot, max_arg_chars)}
    return result


def run_live(scenario: Scenario, result: RunResult, base_url: str, defaults: dict[str, Any]) -> RunResult:
    started = time.monotonic()
    body = {"topic": scenario.prompt, "mode": scenario.mode, "skill_ids": scenario.skills}
    try:
        file_ids = [_upload(base_url, *_fixture(spec)) for spec in scenario.attachments]
        if file_ids:
            body["file_ids"] = file_ids
        submitted = _request(base_url, "POST", "/run-pipeline", body)
        result.job_id = submitted["job_id"]
        if scenario.operation == "cancel":
            snapshot = _request(base_url, "POST", f"/jobs/{result.job_id}/cancel")
            return _normalize_live(snapshot, result, int(defaults["max_tool_arg_chars"]))
        deadline = time.monotonic() + float(defaults["job_timeout_seconds"])
        snapshot = {}
        while time.monotonic() < deadline:
            snapshot = _request(base_url, "GET", f"/jobs/{result.job_id}")
            if snapshot.get("status") in TERMINAL:
                break
            time.sleep(float(defaults["poll_interval_seconds"]))
        else:
            result.final_status = "working"
            result.failure_stage = "poll"
            result.failure_reason = f"Job did not terminate within {defaults['job_timeout_seconds']}s"
            result.durations["total"] = time.monotonic() - started
            return result
        prior_snapshots = [snapshot]
        for turn in scenario.turns:
            followup = _request(base_url, "POST", "/run-pipeline", {
                "topic": turn, "mode": scenario.mode, "skill_ids": scenario.skills,
                "workspace_id": submitted["workspace_id"], "conversation_id": submitted["conversation_id"],
            })
            result.job_id = followup["job_id"]
            deadline = time.monotonic() + float(defaults["job_timeout_seconds"])
            while time.monotonic() < deadline:
                snapshot = _request(base_url, "GET", f"/jobs/{result.job_id}")
                if snapshot.get("status") in TERMINAL:
                    break
                time.sleep(float(defaults["poll_interval_seconds"]))
            prior_snapshots.append(snapshot)
        normalized = _normalize_live(snapshot, result, int(defaults["max_tool_arg_chars"]))
        normalized.raw["turn_snapshots"] = _bounded(prior_snapshots, int(defaults["max_tool_arg_chars"]))
        return normalized
    except Exception as exc:
        result.final_status = "error"
        result.failure_stage = "submission_or_poll"
        result.failure_reason = repr(exc)
        result.raw = {"exception": repr(exc)}
        result.durations["total"] = time.monotonic() - started
        return result
