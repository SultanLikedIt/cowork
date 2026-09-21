"""FastAPI boundary: HTTP + SSE only. No CAMEL imports here.

Endpoints:
  POST /upload, /uploads   - file upload + text extraction (unchanged logic)
  POST /run-pipeline        - submit a task, runs the Workforce in the background
  GET  /jobs, /jobs/{id}    - job snapshots (REST hydration + results)
  GET  /jobs/{id}/events    - Server-Sent Events stream of live job snapshots
  POST /jobs/{id}/cancel    - cooperative cancellation
  POST /jobs/{id}/export/{docx|pdf} - final answer as a downloadable file
  GET  /workers, POST /workers - list / manually add session-scoped agents
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

# Windows consoles default to a codepage (e.g. cp1254 for Turkish locale)
# that can't encode most emoji/symbols. CAMEL's own workforce.py prints
# status markers like the U+274C X mark directly (see
# Workforce._handle_failed_task), and this crashes with UnicodeEncodeError
# uncaught inside the background task -- which then leaves the job's
# asyncio.Queue never drained, so it looks like an indefinite hang (main.py
# only learns about it via Workforce's own 600s "Timeout waiting for task
# return" warning) rather than a clean, visible failure. This has to be
# set before any of that code runs, so it's here at the top of the actual
# process entrypoint rather than in camel_service.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import artifacts as artifacts_module
import artifact_intent
import camel_service
import capabilities
import capability_routing
import db
from document_ingestion import DOCLING_EXTENSIONS, DocumentParseError, parse_document
import document_export
import outlook
import reranker
import social_api
from social import health as social_health
from execution.adapter import (
    run_single_agent as run_single_agent_executor,
    run_workforce_in_thread as run_workforce_in_thread_executor,
)
from execution.executor_config import agent_executor
from model_config import UsageTracker
import skills as skills_catalog
from scheduling import config as scheduling_config
from scheduling import cron as scheduling_cron
from scheduling import prompts as scheduling_prompts
from scheduling.delivery import DeliveryOutcome
from scheduling.api import build_router as build_scheduling_router
from scheduling.runner import Scheduler, SubmittedJob
from tool_call_log import ToolCallRecorder
import turn_resolution
from workforce_callback import QueueWorkforceCallback
from workspace import (
    MAX_BATCH_UPLOAD_BYTES,
    MAX_UPLOAD_SOURCES,
    WORKSPACE_TEXT_EXTENSIONS,
    WorkspaceSecurityError,
    _safe_child,
    build_job_workspace,
    ensure_job_workspace,
    inspect_zip,
    safe_filename,
)

timing_logger = logging.getLogger("agent_webapp.timing")
stabilization_logger = logging.getLogger("agent_webapp.stabilization")
# CAMEL's own logging.basicConfig(level=WARNING) sets the root logger's
# level, which would otherwise silently swallow these INFO-level records.
timing_logger.setLevel(logging.INFO)

# DATA_DIR defaults to this file's own directory (bare-metal/local dev is
# unchanged); in Docker, DATA_DIR=/data points everything below at a mounted
# volume instead, since these would otherwise live next to the source code
# the image COPYs in, which a volume mount would shadow. See db.py's
# DATA_DIR comment -- same env var, same reasoning, shared across all of
# this app's persistent state (DB, uploads, workspaces, logs).
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)

# Persist warnings/errors (e.g. upload failures, crashes) to disk -- without
# this, a crash's traceback only ever reaches the terminal running uvicorn
# and is lost the moment that terminal is closed or the process is killed.
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.WARNING)

upload_logger = logging.getLogger("agent_webapp.uploads")

app = FastAPI(title="agent-webapp backend")

_cors_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
if os.environ.get("OPENBOT_AGUI_ENABLED", "").strip().casefold() in {
    "1",
    "true",
    "yes",
    "on",
}:
    _cors_origins.extend(
        [
            "http://localhost:3010",
            "http://127.0.0.1:3010",
        ]
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

from openbot import mount_openbot_routes

mount_openbot_routes(app)

UPLOAD_DIR = DATA_DIR / "uploads"
WORKSPACE_DIR = DATA_DIR / "workspaces"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

DOCUMENT_EXTENSIONS = set(DOCLING_EXTENSIONS) | {".csv", ".txt"}
# Reliability triage (rel-20260813T105420Z-2a702b3b: live.file.md-json-yaml/
# logs-code/mixed-six) found a standalone upload of an ordinary .md or .log
# file was rejected with HTTP 400 "Unsupported file type", even though
# workspace.py's tool layer (read_text_file/search_text) already fully
# supports every extension in WORKSPACE_TEXT_EXTENSIONS once a file is
# inside a workspace -- this list had simply never been widened to match.
# Reusing that set directly (rather than hand-duplicating .md/.log/.json/
# .yaml/.yml here) means the two can never drift apart again: anything the
# workspace can actually read is, by construction, uploadable. Extraction
# needs no corresponding change -- _extract_text's fallback branch
# (`return path.read_text(...)`) already handles any non-pdf/docx/xlsx/csv
# extension generically, exactly as it already does for .txt today.
ALLOWED_EXTENSIONS = DOCUMENT_EXTENSIONS | WORKSPACE_TEXT_EXTENSIONS | {".zip"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_WORDS = 5000
# Document conversion is CPU-bound. Keep it off the event loop and bound the
# API lifecycle independently of the upload byte/page limits in the adapter.
EXTRACTION_TIMEOUT_SECONDS = max(
    1, int(os.getenv("DOCLING_PARSE_TIMEOUT_SECONDS", "60"))
)
CANCELLATION_NOTE = (
    "Cancellation is cooperative: no new tasks are scheduled and late "
    "events are ignored, but an already-running model call cannot always "
    "be forcibly terminated."
)
# "interrupted" is never set by _run_job itself -- it's assigned only at
# startup, to jobs a prior process left in "queued"/"running" when it died
# (see db.mark_incomplete_jobs_interrupted). There is no live workforce to
# resume one of these from, so it's a genuine terminal state, not a retry
# candidate.
TERMINAL_STATUSES = {
    "done", "partial", "failed", "error", "cancelled", "interrupted"
}
# Separate budget from workspace.MAX_CONTEXT_CHARS (file context): a workspace
# can accumulate an unbounded number of past jobs, so conversation history
# gets its own bounded slice of the prompt rather than competing with
# uploaded-file context for the same budget.
CONVERSATION_CONTEXT_MAX_CHARS = 20_000

_UPLOAD_STORE: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------
# File upload + Docling-backed document extraction
# --------------------------------------------------------------------------


def _extract_tabular(path: Path, ext: str) -> str:
    import pandas as pd

    frame = pd.read_csv(path)
    return frame.to_string(index=False)


def _extract_text(path: Path, ext: str) -> str:
    if ext == ".csv":
        return _extract_tabular(path, ext)
    return path.read_text(encoding="utf-8", errors="replace")


def _maybe_truncate(text: str) -> tuple[str, bool, int]:
    words = text.split()
    total = len(words)
    if total <= MAX_WORDS:
        return text, False, total
    truncated = " ".join(words[:MAX_WORDS])
    truncated += f"\n\n[... truncated, showing first {MAX_WORDS} of {total} words ...]"
    return truncated, True, total


def _metadata_path(file_id: str) -> Path:
    return UPLOAD_DIR / f"{file_id}.metadata.json"


def _extracted_path(file_id: str) -> Path:
    return UPLOAD_DIR / f"{file_id}.extracted.txt"


def _docling_path(file_id: str) -> Path:
    return UPLOAD_DIR / f"{file_id}.docling"


def _save_upload_record(record: dict[str, Any]) -> None:
    persisted = {key: value for key, value in record.items() if key != "extracted_text"}
    _metadata_path(record["file_id"]).write_text(
        json.dumps(persisted, ensure_ascii=False), encoding="utf-8"
    )
    _extracted_path(record["file_id"]).write_text(
        str(record.get("extracted_text") or ""), encoding="utf-8"
    )
    _UPLOAD_STORE[record["file_id"]] = record


def _load_upload_record(file_id: str) -> dict[str, Any]:
    if file_id in _UPLOAD_STORE:
        return _UPLOAD_STORE[file_id]

    metadata_path = _metadata_path(file_id)
    if metadata_path.exists():
        try:
            record = json.loads(metadata_path.read_text(encoding="utf-8"))
            record["extracted_text"] = (
                _extracted_path(file_id).read_text(encoding="utf-8")
                if _extracted_path(file_id).exists()
                else ""
            )
            if not Path(str(record.get("path", ""))).is_file():
                raise ValueError("uploaded source file is missing")
            _UPLOAD_STORE[file_id] = record
            return record
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Upload metadata is unavailable for file_id: {file_id}",
            ) from exc
    raise HTTPException(status_code=404, detail=f"Unknown file_id: {file_id}")


def _delete_new_upload(record: dict[str, Any]) -> None:
    file_id = str(record["file_id"])
    targets = [Path(str(record["path"])), _metadata_path(file_id), _extracted_path(file_id)]
    for target in targets:
        if target.parent.resolve() == UPLOAD_DIR.resolve() and target.exists():
            target.unlink()
    structured = _docling_path(file_id)
    if structured.parent.resolve() == UPLOAD_DIR.resolve() and structured.is_dir():
        import shutil

        shutil.rmtree(structured)
    _UPLOAD_STORE.pop(file_id, None)


class UploadResponse(BaseModel):
    file_id: str
    filename: str
    extracted_text: str
    truncated: bool
    word_count: int
    kind: str = "file"
    size_bytes: int = 0
    workspace_files: list[str] = Field(default_factory=list)
    ignored_file_count: int = 0


async def _store_upload(filename: str, content: bytes) -> UploadResponse:
    display_name = safe_filename(filename)
    ext = Path(display_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{ext or '(none)'}'. Allowed types: "
                f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(content)} bytes). Max size is {MAX_UPLOAD_BYTES} bytes (20MB).",
        )

    file_id = str(uuid.uuid4())
    saved_path = UPLOAD_DIR / f"{file_id}{ext}"
    docling_dir = _docling_path(file_id)
    docling_manifest: Optional[dict[str, Any]] = None
    saved_path.write_bytes(content)
    try:
        if ext == ".zip":
            inspection = inspect_zip(saved_path)
            text = ""
            workspace_files = [item.path for item in inspection.files]
            ignored = inspection.ignored_file_count
        else:
            # Run in a worker thread so one heavy/complex PDF can't block the
            # event loop (and therefore every other in-flight request) for
            # the whole extraction, and so a runaway parse can be timed out
            # instead of running unbounded.
            try:
                if ext in DOCLING_EXTENSIONS:
                    parsed = await asyncio.wait_for(
                        asyncio.to_thread(
                            parse_document,
                            saved_path,
                            docling_dir,
                            display_name=display_name,
                        ),
                        timeout=EXTRACTION_TIMEOUT_SECONDS,
                    )
                    text = parsed.text
                    docling_manifest = parsed.manifest
                else:
                    text = await asyncio.wait_for(
                        asyncio.to_thread(_extract_text, saved_path, ext),
                        timeout=EXTRACTION_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"extraction exceeded {EXTRACTION_TIMEOUT_SECONDS}s"
                ) from exc
            workspace_files = [display_name]
            ignored = 0
    except WorkspaceSecurityError as exc:
        saved_path.unlink()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DocumentParseError as exc:
        saved_path.unlink(missing_ok=True)
        if docling_dir.exists():
            import shutil

            shutil.rmtree(docling_dir)
        upload_logger.warning("Docling failed for %s: %s", display_name, exc)
        raise HTTPException(
            status_code=422,
            detail={
                "reason": exc.reason,
                "message": exc.message,
                "retryable": False,
            },
        ) from exc
    except TimeoutError as exc:
        saved_path.unlink()
        if docling_dir.exists():
            import shutil

            shutil.rmtree(docling_dir)
        upload_logger.warning("Extraction timed out for %s: %s", display_name, exc)
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "document_parse_timeout",
                "message": (
                    f"Document parsing exceeded {EXTRACTION_TIMEOUT_SECONDS} seconds."
                ),
                "retryable": False,
            },
        ) from exc
    except Exception as exc:
        saved_path.unlink()
        if docling_dir.exists():
            import shutil

            shutil.rmtree(docling_dir)
        upload_logger.warning("Extraction failed for %s: %s", display_name, exc, exc_info=True)
        raise HTTPException(
            status_code=422, detail=f"Failed to inspect uploaded file: {exc}"
        ) from exc

    display_text, truncated, word_count = _maybe_truncate(text)
    record = {
        "file_id": file_id,
        "filename": display_name,
        "path": str(saved_path),
        "kind": "zip" if ext == ".zip" else "file",
        "upload_size_bytes": len(content),
        "extracted_text": text,
        "workspace_files": workspace_files,
        "ignored_file_count": ignored,
    }
    if docling_manifest is not None:
        record["docling_dir"] = str(docling_dir)
        record["docling_manifest"] = docling_manifest
    _save_upload_record(record)
    return UploadResponse(
        file_id=file_id,
        filename=display_name,
        extracted_text=display_text,
        truncated=truncated,
        word_count=word_count,
        kind=record["kind"],
        size_bytes=len(content),
        workspace_files=workspace_files,
        ignored_file_count=ignored,
    )


async def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded")
    if len(files) > MAX_UPLOAD_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_UPLOAD_SOURCES} files may be uploaded at once",
        )

    uploads: list[tuple[str, bytes]] = []
    total = 0
    for file in files:
        filename = safe_filename(file.filename or "upload")
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext or '(none)'}': {filename}",
            )
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail=f"{filename} exceeds the 20MB individual limit"
            )
        total += len(content)
        if total > MAX_BATCH_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Combined uploads exceed the {MAX_BATCH_UPLOAD_BYTES}-byte batch limit",
            )
        uploads.append((filename, content))
    return uploads


@app.post("/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)):
    uploads = await _read_uploads([file])
    return await _store_upload(*uploads[0])


@app.post("/uploads", response_model=list[UploadResponse])
async def upload_files(files: list[UploadFile] = File(...)):
    uploads = await _read_uploads(files)
    created: list[dict[str, Any]] = []
    responses: list[UploadResponse] = []
    try:
        for filename, content in uploads:
            response = await _store_upload(filename, content)
            responses.append(response)
            created.append(_load_upload_record(response.file_id))
    except Exception:
        for record in created:
            _delete_new_upload(record)
        raise
    return responses


# --------------------------------------------------------------------------
# Jobs: submit, live status via SSE, cancellation
# --------------------------------------------------------------------------


class RunPipelineRequest(BaseModel):
    topic: str = "REST APIs"
    file_id: Optional[str] = None
    file_ids: list[str] = Field(default_factory=list, max_length=MAX_UPLOAD_SOURCES)
    context_text: Optional[str] = None
    # Manual choice only -- no auto-detection. "single" bypasses Workforce
    # entirely (one direct ChatAgent call, no decompose/assign/quality-check
    # overhead); "multi" is the existing 4-worker Workforce pipeline.
    mode: Literal["single", "multi"] = "single"
    # Opt-in only -- never auto-detected. Raises Research's survey budget
    # to MAX_SEARCH_PHASES_DEEP / MAX_FETCHES_DEEP (web_controller.py) for a
    # single ask: many more angles, many more pages, no attempt to keep this
    # fast. Distinct from the existing phrase-triggered "survey" depth
    # (capabilities.detect_deep_research) -- that one stays a quick default;
    # this is the user explicitly asking for as-thorough-as-possible.
    deep_research: bool = False
    # Optional by design: the frontend's "just type and go" flow must keep
    # working with no workspace selected -- see run_pipeline's
    # _resolve_workspace_id, which silently creates one when this is absent
    # (or references an id the DB doesn't know about) rather than rejecting
    # the request. Naming/switching workspaces is a secondary, non-blocking
    # UI affordance on top of this, not a prerequisite for it.
    workspace_id: Optional[str] = None
    # Null starts a new conversation in the resolved workspace. Supplying a
    # real id continues only that conversation.
    conversation_id: Optional[str] = None
    # Built-in Skill ids (see skills.BUILT_IN_SKILLS). Unknown ids are
    # dropped and the list is capped server-side by skills.resolve_skills,
    # so a stale or oversized selection degrades to a valid run rather than
    # a 422. max_length is a cheap DoS bound on the request body only; the
    # semantic cap is MAX_ACTIVE_SKILLS.
    skill_ids: list[str] = Field(default_factory=list, max_length=32)


class SkillSummary(BaseModel):
    skill_id: str
    name: str
    description: str


class SkillCatalogResponse(BaseModel):
    skills: list[SkillSummary]
    # The picker enforces this client-side too; sending it keeps the two
    # from drifting if the server-side cap changes.
    max_active: int


class RunPipelineResponse(BaseModel):
    job_id: str
    workspace_id: str
    conversation_id: str


class CreateWorkspaceRequest(BaseModel):
    name: Optional[str] = None


class UpdateWorkspaceRequest(BaseModel):
    name: Optional[str] = None


class WorkspaceInfo(BaseModel):
    workspace_id: str
    name: Optional[str] = None
    created_at: float
    last_active_at: float


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceInfo] = Field(default_factory=list)


class ConversationMessage(BaseModel):
    job_id: Optional[str] = None
    role: str
    content: str
    created_at: float


class WorkspaceDetailResponse(BaseModel):
    workspace: WorkspaceInfo
    messages: list[ConversationMessage] = Field(default_factory=list)


class AttachmentSummary(BaseModel):
    file_id: str
    filename: str
    kind: str
    file_count: int
    total_size_bytes: int
    ignored_file_count: int = 0


class WorkspaceFileSummary(BaseModel):
    path: str
    source_id: str
    source_filename: str
    size_bytes: int


class WorkspaceSummary(BaseModel):
    file_count: int
    total_size_bytes: int
    ignored_file_count: int = 0
    files: list[WorkspaceFileSummary] = Field(default_factory=list)
    limits: dict[str, int] = Field(default_factory=dict)


class UsageTelemetry(BaseModel):
    model_name: Optional[str] = None
    provider: Optional[str] = None
    model_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    usage_available: bool = False
    total_duration_seconds: Optional[float] = None
    # Per-stage breakdown (planning/assignment/research/document/review/
    # outline/quality/synthesis, plus dynamic_worker/manual_worker/
    # single_agent where applicable) -- see UsageTracker.stage_snapshot().
    # Empty for jobs run before this field existed (persisted rows just
    # don't have the key; model default fills it in as {}).
    # float, not int: the buckets carry token counts *and* duration_seconds,
    # and a fractional duration made this model reject the whole job status.
    by_stage: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Every timed step (model calls, tool calls, reranking), oldest first -- see _timeline.
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    # Which execution strategy actually ran this job -- "direct" (single
    # ChatAgent, no Workforce), "specialist" (one known fixed worker,
    # decompose+assign skipped), or "workforce" (full CAMEL decompose +
    # assign). Lives on `usage` (JSON blob, no migration needed to persist
    # it) rather than as a top-level JobStatusResponse field -- see
    # db.save_job's explicit column list, which a new top-level job dict key
    # would silently not survive a restart through. `mode` is unchanged and
    # still drives the UI's Single/Multi Agent badge -- see
    # capability_routing.py.
    # New jobs emit "single" or "workforce" from the UI choice. A Workforce
    # thread may still run strategy="direct" for a conversation-only
    # follow-up without changing job["mode"]. "specialist" remains accepted
    # for persisted legacy jobs.
    execution_strategy: Literal[
        "single", "workforce", "direct", "specialist"
    ] = "workforce"
    execution_strategy_role: Optional[str] = None
    # How the strategy above was arrived at. New jobs report "not_called"
    # because the explicit UI mode is authoritative; timeout/error values are
    # retained for persisted jobs created by the former execution router.
    routing_outcome: Optional[str] = None
    # What this run was given the ability to do, and which persona ran it.
    # Both are on `usage` for the same reason execution_strategy is: it is a
    # JSON blob, so recording them needs no migration and old rows just
    # default. These are the fields that make a bad routing decision
    # diagnosable after the fact -- "the job failed" versus "the job failed
    # and it never had the artifact capability" are completely different
    # investigations.
    capabilities: list[str] = Field(default_factory=list)
    # Keep product mode, runtime strategy, and binding provenance explicit.
    # New jobs intentionally use the same product term for mode and strategy.
    # New telemetry normalizes API mode="multi" to the product term
    # "workforce". "multi" remains accepted for persisted legacy jobs.
    ui_mode: Optional[Literal["single", "workforce", "multi"]] = None
    tool_binding_source: Optional[str] = None
    bound_capabilities: list[str] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)
    execution_profile: Optional[str] = None
    # Why this strategy was chosen (user-selected Single vs Workforce).
    strategy_reason: Optional[str] = None
    advisory_strategy: Optional[str] = None
    wants_artifact: Optional[bool] = None
    artifact_type: Optional[str] = None
    artifact_intent_source: Optional[str] = None
    needs_web_research: Optional[bool] = None
    reuses_uploaded_document: Optional[bool] = None
    opens_on_user_computer: Optional[bool] = None
    # "ve ali@sirket.com'a gonder" in the request itself; recipients are only
    # addresses the user typed (artifact_intent._verified_recipients).
    sends_email: Optional[bool] = None
    email_to: list[str] = Field(default_factory=list)
    # "gelen maillerime bak" -- this job read the user's own mailbox. Kept on
    # the job record because it is the audit answer to "why did a job open my
    # inbox": the classification that caused it is stored, not just its
    # effect. The messages themselves are never persisted here.
    reads_email: Optional[bool] = None
    # "reddit'te ne diyorlar" -- this job read public discussion. Same audit
    # reasoning as reads_email: the classification that caused it is what is
    # stored, never the quoted posts themselves.
    reads_social: Optional[bool] = None
    # Classified once when a Single run finishes; see _suggests_workforce.
    suggests_workforce: Optional[bool] = None
    planning_ms: Optional[int] = None
    assignment_ms: Optional[int] = None
    assignment_source: Optional[str] = None
    planning_output_tokens: Optional[int] = None
    subtask_count: Optional[int] = None
    targets_planned: list[str] = Field(default_factory=list)
    targets_covered: list[str] = Field(default_factory=list)
    controller_search_count: Optional[int] = None
    controller_fetch_count: Optional[int] = None
    controller_by_target: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Turn-resolution output. `topic` on the job stays the raw user text;
    # executors (web controller, artifact intent, synthesis) read this.
    resolved_request: Optional[str] = None
    turn_resolution_source: Optional[str] = None
    needs_external_work: Optional[bool] = None
    # Real tool activity, derived from the ground-truth recorder rather than
    # from anything a model claimed. `tool_errors` is the one that matters
    # operationally: a run with many tool errors is looping, and that was
    # previously invisible without reading the JSONL log by hand.
    tool_call_count: int = 0
    tool_error_count: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    artifact_count: int = 0
    # Why the job stopped, as one short token -- the field an operator reads
    # first. See main._terminal_reason.
    terminal_reason: Optional[str] = None


class TaskPlanEntry(BaseModel):
    id: str
    title: str
    description: str = ""
    assigned_agent: Optional[str] = None
    dependencies: list[str] = Field(default_factory=list)


class ActivityStatus(BaseModel):
    """Latest factual execution activity; never model reasoning."""

    task_id: str = ""
    stage: Literal[
        "searching", "opening", "browsing", "reading", "building", "done"
    ]
    detail: str = ""
    timestamp: Optional[str] = None


class ToolCallRecord(BaseModel):
    """One real tool invocation: what was actually sent and actually
    returned, independent of CAMEL's task-level events or what the calling
    agent's response text claims. See tool_call_log.py."""

    timestamp: str
    tool_name: str
    args: dict[str, Any]
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    duration_ms: Optional[float] = None


class JobStatusResponse(BaseModel):
    job_id: str
    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    topic: str
    status: str
    # Echoes back whichever mode this job actually ran with -- the single
    # source of truth for the frontend badge, independent of what was
    # requested (in case defaults or validation ever diverge).
    mode: Literal["single", "multi"] = "multi"
    # Same idea for the deep-research opt-in. The composer clears its toggle
    # on send (it is per-message, not sticky), so without this the running
    # job gave no way to tell whether it was the wide search or the normal
    # one -- a difference of 30 searches vs 3 and ten minutes of waiting.
    deep_research: bool = False
    # Derived (see _suggests_workforce): this Single run was asked for
    # something only Workforce can do, so the UI can offer the switch
    # instead of leaving the user to work out why the answer is thin.
    suggests_workforce: bool = False
    task_plan: list[TaskPlanEntry] = Field(default_factory=list)
    planning_fallback: bool = False
    planning_errors: list[str] = Field(default_factory=list)
    task_states: dict[str, str] = Field(default_factory=dict)
    task_outputs: dict[str, str] = Field(default_factory=dict)
    task_attempts: dict[str, int] = Field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: float
    # Wall-clock execution start (created_at is submission). Only set once
    # the job actually begins running; the live elapsed counter measures
    # from here so it matches duration_seconds when the run ends.
    started_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    attachments: list[AttachmentSummary] = Field(default_factory=list)
    workspace: Optional[WorkspaceSummary] = None
    usage: UsageTelemetry = Field(default_factory=UsageTelemetry)
    cancellation_requested: bool = False
    cancellation_note: Optional[str] = None
    # Which Skills this run actually executed with, resolved and capped at
    # submission. Persisted with the job so history and any future
    # scheduled re-run reproduce the same behaviour.
    skills: list[SkillSummary] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    # Real files this job produced, downloadable via
    # GET /jobs/{job_id}/artifacts/{path}.
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    # Accumulates as "token_delta" events arrive (see camel_service.py) so
    # the frontend can render the final answer as it's generated instead of
    # waiting for the job to reach a terminal status.
    streaming_text: str = ""
    # Updated by real tool steps (search/open/read/build), transported
    # in the normal job snapshot so polling and SSE have identical schemas.
    current_activity: Optional[ActivityStatus] = None


class ArtifactSummary(BaseModel):
    """One real file this job created, as listed to the frontend.

    Built by walking the job workspace's artifacts directory (see
    artifacts.scan_artifacts), never from what the model said it did -- an
    artifact the agent claims to have produced but that is not on disk simply
    does not appear here. That is the point: the UI shows files, not claims.
    """

    path: str
    filename: str
    kind: str
    size_bytes: int
    entry_point: Optional[str] = None
    # Relative to the job's artifact endpoint; the frontend prefixes it with
    # /jobs/{job_id}/artifacts/.
    download_path: str
    # Which create_*_artifact call produced this file, when known (real for a
    # bundle, from its manifest; best-effort by extension for a standalone
    # file -- see artifacts._PRODUCER_BY_KIND).
    producer: Optional[str] = None
    created_at: Optional[str] = None
    # Set only for a bundle (create_web_bundle / create_code_bundle): the
    # folder name, so the frontend can group its members under one entry
    # instead of showing them as unrelated files.
    bundle: Optional[str] = None
    files: Optional[list[dict[str, Any]]] = None
    # Set only alongside `bundle`: relative to /jobs/{job_id}/artifact-bundles/,
    # for downloading the whole bundle as one ZIP instead of just entry_point.
    zip_download_path: Optional[str] = None


class ExportRequest(BaseModel):
    # The exact final-answer text the Results view is displaying.
    content: str
    title: Optional[str] = None


class AddWorkerRequest(BaseModel):
    name: str
    description: Optional[str] = None


class WorkerSummary(BaseModel):
    name: str
    # The coordinator-facing sentence this agent is actually registered
    # with -- synthesized from the name when the user left it blank.
    description: str
    source: Literal["fixed", "manual"]


class WorkerPoolResponse(BaseModel):
    workers: list[WorkerSummary] = Field(default_factory=list)


def _worker_pool() -> WorkerPoolResponse:
    fixed = [
        WorkerSummary(name=role_name, description=description, source="fixed")
        for description, role_name, _ in camel_service.WORKER_ROLES
    ]
    # Shown only when this deployment can actually read mail. The roster is
    # a promise to the user about who can work on their request, so listing
    # a Mail Agent on an install with OUTLOOK_READ_ENABLED off would be
    # advertising a worker that can never be scheduled (see
    # _routing_for_workforce, which withholds MAIL_READ for the same reason).
    #
    # Appended here rather than added to camel_service.WORKER_ROLES on
    # purpose: that list is CAMEL's *assignment* roster -- its coordinator
    # reads those descriptions to route subtasks -- and there is no mail
    # worker on the CAMEL path at all. Putting it there would let the
    # coordinator hand tasks to an agent that does not exist.
    if outlook.read_enabled():
        fixed.append(
            WorkerSummary(
                name="Mail Agent",
                description=(
                    "Reads the Outlook mailbox on this computer (Inbox and "
                    "Sent Items) and passes what it finds to the other "
                    "workers. Read-only."
                ),
                source="fixed",
            )
        )
    manual = [
        WorkerSummary(
            name=record["name"], description=record["description"], source="manual"
        )
        for record in camel_service.list_manual_workers()
    ]
    return WorkerPoolResponse(workers=fixed + manual)


class _JobControl:
    __slots__ = (
        "workforce",
        "cancelled",
        "subscribers",
        "usage_tracker",
        "tool_call_recorder",
        "task",
    )

    def __init__(self, job_id: str) -> None:
        self.workforce: Any = None
        self.cancelled = False
        self.subscribers: list["asyncio.Queue[dict | None]"] = []
        self.usage_tracker = UsageTracker()
        self.tool_call_recorder = ToolCallRecorder(job_id=job_id)
        # A STRONG reference to the background asyncio.Task running this job.
        #
        # asyncio keeps only a weak reference to running tasks, so a task
        # nobody holds can be garbage-collected mid-flight -- the interpreter
        # documents this explicitly. A job that vanishes this way leaves its
        # row "running" forever with no error and no log line, which is the
        # worst possible failure shape: invisible.
        self.task: Any = None


_JOBS: dict[str, dict[str, Any]] = {}
_JOB_CONTROLS: dict[str, _JobControl] = {}
_JOB_INDEX: dict[str, dict[str, dict[str, Any]]] = {}  # job_id -> task_id -> task_plan entry
_WORKER_ROLES: dict[str, dict[str, str]] = {}  # job_id -> worker_id -> role
_TIMING: dict[str, dict[str, Any]] = {}  # job_id -> phase timestamps, for the log breakdown


@app.on_event("startup")
def _rehydrate_from_db() -> None:
    """Bring jobs and manually-added agents back after a restart.

    Deliberately does NOT create a _JobControl for any rehydrated job --
    there is no live workforce/usage_tracker/tool_call_recorder to attach,
    and _job_snapshot()/job_events() both check for that (control is None)
    to know a job is historical rather than still in-process.
    """
    db.run_migrations()

    interrupted = db.mark_incomplete_jobs_interrupted()
    if interrupted:
        logging.getLogger("agent_webapp.startup").warning(
            "Marked %d job(s) interrupted after restart: %s", len(interrupted), interrupted
        )

    for job in db.load_all_jobs():
        root = _resolve_workspace_root(job)
        if root:
            job["workspace_root"] = root
        _JOBS[job["job_id"]] = job

    camel_service.load_manual_workers(db.load_manual_workers())


@app.on_event("startup")
def _warm_reranker() -> None:
    # Off the request path: otherwise the first research round pays the weight load.
    if not reranker.enabled():
        return
    threading.Thread(
        target=reranker.warm_up, name="reranker-warmup", daemon=True
    ).start()


def _skill_summaries(job: dict[str, Any]) -> list[SkillSummary]:
    """Expand a job's stored skill ids into name/description for display.

    The job row stores ids only (see migration 0004). Resolving them here
    rather than at write time is what keeps a renamed or reworded catalog
    entry from leaving stale copies of itself in history, and drops any id
    that has since been retired.
    """
    return [
        SkillSummary(
            skill_id=skill.skill_id,
            name=skill.name,
            description=skill.description,
        )
        for skill in skills_catalog.resolve_skills(job.get("skills"))
    ]


# job_id -> (expires_at_monotonic, artifacts). See _job_artifacts.
_ARTIFACT_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
# Long enough to make the scan negligible during streaming, short enough that
# a file appearing mid-run shows up while the user is still watching.
_ARTIFACT_CACHE_TTL_SECONDS = 1.0


def _resolve_workspace_root(job: dict[str, Any]) -> Optional[str]:
    """Absolute job workspace path, including after a process restart.

    ``workspace_root`` is process-memory only (never a DB column). After
    restart, jobs rehydrated from SQLite have it missing even though the
    files still live under ``WORKSPACE_DIR / job_id``. Reconstruct from
    that convention so artifact downloads survive reopen / refresh.
    """
    root = job.get("workspace_root")
    if root:
        return str(root)
    nested = job.get("workspace")
    if isinstance(nested, dict) and nested.get("root"):
        return str(nested["root"])
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return None
    candidate = WORKSPACE_DIR / job_id
    if candidate.is_dir():
        return str(candidate)
    return None


def _job_artifacts(job: dict[str, Any]) -> list[dict[str, Any]]:
    """The job's real, on-disk artifacts.

    Read from the FILESYSTEM rather than tracked in the job dict, for the same
    reason tool_call_log exists: what the agent said it produced is not
    evidence. This also survives a restart for free, and means a fabricated
    "I created report.xlsx" simply produces an empty list.

    Memoized for a second because of where this is called from. _job_snapshot
    runs on EVERY event drained for a job (see _drain_events), and that
    includes every token_delta -- one per ~4 characters of streamed answer. An
    unguarded directory walk there would be thousands of stat storms per job,
    and GET /jobs would do one walk per job in the list. The TTL keeps the
    cost flat while still surfacing a file within a second of it being
    written.
    """
    root = _resolve_workspace_root(job)
    if root and not job.get("workspace_root"):
        job["workspace_root"] = root
    if not root:
        return []

    job_id = job.get("job_id") or ""
    now = time.monotonic()
    cached = _ARTIFACT_CACHE.get(job_id)
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        found = artifacts_module.scan_artifacts(root)
    except OSError:
        found = []
    for entry in found:
        # "artifacts/report.xlsx" -> "report.xlsx"; the endpoint re-anchors it
        # under the job's own artifacts directory.
        entry["download_path"] = entry["path"].split("/", 1)[-1]
        if entry.get("bundle"):
            # "artifacts/prayer-api" -> "prayer-api", for
            # /jobs/{job_id}/artifact-bundles/{bundle_path}.
            entry["zip_download_path"] = entry["bundle"].split("/", 1)[-1]
    if job_id:
        _ARTIFACT_CACHE[job_id] = (now + _ARTIFACT_CACHE_TTL_SECONDS, found)
        # Bounded: this is a cache, not a registry, and a long-lived process
        # must not accumulate one entry per job it has ever seen.
        if len(_ARTIFACT_CACHE) > 256:
            for stale_id, (expires, _) in list(_ARTIFACT_CACHE.items()):
                if expires <= now:
                    _ARTIFACT_CACHE.pop(stale_id, None)
    return found


async def _email_answer_from_prompt(job: dict[str, Any]) -> Optional[str]:
    """The user asked, in the request itself, for this answer to be mailed.

    Chat runs open the finished message in Outlook instead of sending it
    (OUTLOOK_AGENT_ACTION=send overrides): the person is at the computer, and
    a draft they press Send on cannot become a way to mail something out on
    the strength of text the run happened to read. Scheduled runs never come
    here -- see _deliver_scheduled_run.

    Returns one line to append to the answer, or None when no mail was asked
    for.
    """
    setting = _prompt_delivery(job)
    if setting is None:
        return None
    recipients = ", ".join(setting["to"])
    if not outlook.is_enabled():
        return (
            f"📧 Not: sonucu {recipients} adresine göndermek istediniz, ama e-posta "
            "gönderimi bu kurulumda kapalı (docs/outlook.md)."
        )
    notes: list[str] = []
    attachments = await _email_attachments(job, notes)
    title = document_export.short_report_title(
        str(job.get("topic") or "").strip(), fallback="agent-webapp"
    )
    action = outlook.agent_action()
    notes.append("Bu e-posta agent-webapp'te hazırlandı.")
    result = await asyncio.to_thread(
        outlook.send_mail,
        to=setting["to"],
        subject=title,
        html_body=outlook.render_email_html(title, str(job.get("result") or ""), notes=notes),
        attachments=attachments,
        action=action,
    )
    if not result.ok:
        return f"📧 E-posta hazırlanamadı ({recipients}): {result.message}"
    if action == "send":
        return f"📧 E-posta gönderildi: {recipients}"
    return (
        f"📧 E-posta taslağı Outlook'ta açıldı: {recipients} — göndermek için "
        "Outlook'taki Gönder düğmesine basın."
    )


def _ensure_requested_pdf(
    job: dict[str, Any], produced: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """If the user asked for a PDF and none exists, render one from the answer."""
    if job.get("mode") != "multi":
        return produced
    root = _resolve_workspace_root(job)
    if root:
        job["workspace_root"] = root
    if not root:
        return produced
    if any(str(item.get("kind") or "").lower() == "pdf" for item in produced):
        return produced
    usage = job.get("usage") or {}
    asked = str(usage.get("artifact_type") or "").lower() == "pdf"
    request_text = _job_resolved_request(job)
    if not asked and not capabilities.detect_pdf_output(request_text):
        return produced
    body = str(job.get("result") or "").strip()
    if not body or (
        body.startswith("{") and "verified_artifact_created" in body
    ):
        return produced
    try:
        artifacts_module.ensure_pdf_from_text(
            Path(root),
            title=str(job.get("topic") or "Rapor"),
            body=body,
        )
    except Exception:
        logging.getLogger("agent_webapp.jobs").exception(
            "job=%s failed to materialize requested PDF", job.get("job_id")
        )
        return produced
    job_id = str(job.get("job_id") or "")
    _ARTIFACT_CACHE.pop(job_id, None)
    return _job_artifacts(job)


def _terminal_reason(job: dict[str, Any], artifact_count: int) -> Optional[str]:
    """One short token saying why the job stopped.

    Deliberately coarse and deliberately derived, not stored: it answers the
    first question an operator asks ("what happened?") without needing them to
    correlate status, error text and artifact list by eye.
    """
    status = job.get("status")
    if status not in TERMINAL_STATUSES:
        return None
    if status == "cancelled":
        return "cancelled_by_user"
    if status == "done":
        return "completed"
    if status == "partial":
        return "partial_with_artifacts" if artifact_count else "partial_result"
    if status == "error":
        return "execution_error"
    if status == "failed":
        return "no_usable_result"
    return status


def _tool_activity(calls: list[dict[str, Any]]) -> tuple[int, int, dict[str, int]]:
    """Counts derived from the ground-truth tool log."""
    by_name: dict[str, int] = {}
    errors = 0
    for call in calls:
        name = str(call.get("tool_name") or "unknown")
        by_name[name] = by_name.get(name, 0) + 1
        if call.get("error"):
            errors += 1
    return len(calls), errors, by_name


MAX_TIMELINE_EVENTS = 500
_TOOL_DETAIL_KEYS = ("query", "url", "path", "q", "search_query")


def _timeline(tracker: Any, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model calls, tool calls and reranking as one oldest-first timing log."""
    events = list(tracker.timeline_snapshot())
    for call in tool_calls:
        duration_ms = call.get("duration_ms")
        if duration_ms is None:
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        detail = next((str(args[key]) for key in _TOOL_DETAIL_KEYS if args.get(key)), "")
        events.append(
            {
                "kind": "tool",
                "name": str(call.get("tool_name") or "tool"),
                "started_at": call.get("started_at") or call.get("timestamp"),
                "duration_ms": duration_ms,
                "detail": detail[:300],
                "ok": not call.get("error"),
            }
        )
    events.sort(key=lambda event: str(event.get("started_at") or ""))
    return events[-MAX_TIMELINE_EVENTS:]


def _suggests_workforce(job: dict[str, Any]) -> bool:
    """Whether this Single run was asked for something it cannot do.

    Single binds no tools at all (single.run_single_agent passes tools=())
    and its routing is deliberately neutered -- _routing_for_single resolves
    declared and structural capabilities as empty, so every Single job
    records profile "general" and no capabilities. There is therefore
    nothing in the run's own telemetry to read; the need has to be detected
    from the request, which is what these three already do elsewhere in the
    product. Display only: nothing about execution changes.
    """
    usage = job.get("usage") or {}
    if (usage.get("ui_mode") or job.get("mode")) != "single":
        return False
    stored = usage.get("suggests_workforce")
    if stored is not None:
        # Classified once when the run finished; see the call site.
        return bool(stored)
    ask = str(usage.get("resolved_request") or job.get("topic") or "").strip()
    if not ask:
        return False
    if capabilities.detect_artifact_request(ask):
        return True
    if capabilities.detect_web_source(ask):
        return True
    from execution.langgraph_runtime.workforce import _request_needs_live_data

    return _request_needs_live_data(ask)


def _job_snapshot(job_id: str) -> JobStatusResponse:
    job = dict(_JOBS[job_id])
    job["skills"] = _skill_summaries(job)
    job["artifacts"] = _job_artifacts(job)
    job["suggests_workforce"] = _suggests_workforce(job)
    control = _JOB_CONTROLS.get(job_id)
    if control is None:
        # Rehydrated from the DB after a restart: nothing in this process
        # ever ran this job, so there's no live tracker/recorder to merge --
        # the dict already carries the final persisted usage/tool_calls from
        # db.load_all_jobs(), which must be returned as-is rather than
        # overlaid with a fresh (all-zero) UsageTracker.
        return JobStatusResponse(**job)
    tracker_usage = control.usage_tracker.snapshot()
    tool_calls = control.tool_call_recorder.snapshot()
    call_count, error_count, by_name = _tool_activity(tool_calls)
    job["usage"] = {
        **job["usage"],
        **tracker_usage,
        "usage_available": tracker_usage["model_call_count"] > 0,
        "by_stage": control.usage_tracker.stage_snapshot(),
        "timeline": _timeline(control.usage_tracker, tool_calls),
        # Usage.total_duration_seconds was declared but never assigned, so
        # the Usage table's Duration row always fell through to
        # job.duration_seconds. Feed it from the same measurement rather
        # than leaving two fields that can disagree about one run.
        "total_duration_seconds": job.get("duration_seconds"),
        "tool_call_count": call_count,
        "tool_error_count": error_count,
        "tool_calls_by_name": by_name,
        "artifact_count": len(job["artifacts"]),
        "terminal_reason": _terminal_reason(job, len(job["artifacts"])),
        **camel_service.orchestration_telemetry(control.workforce),
    }
    job["tool_calls"] = tool_calls
    return JobStatusResponse(**job)


def _sse_pack(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# SSE carries frequent live snapshots, so fields that are useful in the
# authoritative GET /jobs response can be ruinously repetitive here. Keep
# the event stream operational: status, timings, activity, concise tool
# metadata and bounded answer progress. Raw tool results remain available in
# the job record/log and are never copied into every event.
MAX_SSE_TEXT_CHARS = 16_000
MAX_SSE_TASK_OUTPUT_CHARS = 800
MAX_SSE_TOOL_CALLS = 25
SSE_STREAM_EMIT_CHARS = 512


def _bounded_event_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _compact_tool_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    useful = {
        "query", "q", "search_query", "keyword", "need", "url", "want",
        "path", "from_path", "filename", "format",
    }
    compact: dict[str, Any] = {}
    for key, value in args.items():
        if key not in useful:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = _bounded_event_text(value, 240)
        else:
            compact[key] = _bounded_event_text(value, 240)
    return compact


def _event_snapshot(job_id: str) -> dict[str, Any]:
    """Compact live snapshot; never includes web/tool response bodies."""
    snapshot = _job_snapshot(job_id).model_dump(mode="json")
    snapshot["topic"] = _bounded_event_text(snapshot.get("topic"), 2_000)
    snapshot["streaming_text"] = _bounded_event_text(
        snapshot.get("streaming_text"), MAX_SSE_TEXT_CHARS
    )
    snapshot["result"] = (
        _bounded_event_text(snapshot.get("result"), MAX_SSE_TEXT_CHARS)
        if snapshot.get("result") is not None
        else None
    )
    snapshot["error"] = (
        _bounded_event_text(snapshot.get("error"), 1_000)
        if snapshot.get("error") is not None
        else None
    )
    snapshot["planning_errors"] = [
        _bounded_event_text(error, 500)
        for error in snapshot.get("planning_errors", [])[:10]
    ]
    compact_outputs: dict[str, str] = {}
    for task_id, output in snapshot.get("task_outputs", {}).items():
        state = snapshot.get("task_states", {}).get(task_id)
        if state == "failed":
            compact_outputs[str(task_id)] = _bounded_event_text(
                output, MAX_SSE_TASK_OUTPUT_CHARS
            )
        elif output:
            compact_outputs[str(task_id)] = "Completed; full output is available in job status."
    snapshot["task_outputs"] = compact_outputs
    for task in snapshot.get("task_plan", []):
        task["title"] = _bounded_event_text(task.get("title"), 300)
        task["description"] = _bounded_event_text(task.get("description"), 500)
    for artifact in snapshot.get("artifacts", []):
        # Bundle member manifests can be large and are not live progress.
        artifact["files"] = None
    compact_calls = []
    for call in snapshot.get("tool_calls", [])[-MAX_SSE_TOOL_CALLS:]:
        compact_calls.append(
            {
                "timestamp": call.get("timestamp"),
                "tool_name": _bounded_event_text(call.get("tool_name"), 120),
                "args": _compact_tool_args(call.get("args")),
                "result": None,
                "error": (
                    _bounded_event_text(call.get("error"), 500)
                    if call.get("error")
                    else None
                ),
                "started_at": call.get("started_at"),
                "duration_ms": call.get("duration_ms"),
            }
        )
    snapshot["tool_calls"] = compact_calls
    return snapshot


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _apply_event(job_id: str, event: dict[str, Any]) -> None:
    job = _JOBS[job_id]
    task_index = _JOB_INDEX[job_id]
    worker_roles = _WORKER_ROLES[job_id]
    timing = _TIMING[job_id]
    event_type = event.get("event_type")
    ts = _parse_ts(event.get("timestamp"))

    if event_type == "worker_created":
        # event["role"] falls back to the worker's `description` (see
        # camel_service.WORKER_DISPLAY_NAMES for why); translate our fixed
        # workers' and any manually added agent's long coordinator-facing
        # description back to a short display name. Workers CAMEL creates
        # dynamically match neither map and keep their own (already short)
        # coordinator-generated role.
        worker_roles[event["worker_id"]] = camel_service.display_name_for(
            event["role"]
        )

    elif event_type == "task_created":
        task_id = event["task_id"]
        if task_id not in task_index:
            entry = {
                "id": task_id,
                "title": (event.get("description") or task_id)[:120],
                "description": event.get("description") or "",
                "assigned_agent": None,
                "dependencies": [],
            }
            task_index[task_id] = entry
            job["task_plan"].append(entry)
        job["task_states"][task_id] = "queued"

        if event.get("parent_task_id") is None and task_id == job_id:
            timing["root_created"] = ts
        else:
            timing["tasks"][task_id] = {"created": ts, "attempts": []}

    elif event_type == "task_decomposed":
        timing["decomposed"] = ts
        if timing.get("root_created"):
            duration = (ts - timing["root_created"]).total_seconds()
            timing_logger.info(
                "[timing] job=%s phase=decomposition duration=%.2fs "
                "subtasks=%d planning_ms=%s",
                job_id,
                duration,
                len(event.get("subtask_ids") or []),
                camel_service.orchestration_telemetry(
                    _JOB_CONTROLS.get(job_id) and _JOB_CONTROLS[job_id].workforce
                ).get("planning_ms"),
            )

    elif event_type == "task_assigned":
        task_id = event["task_id"]
        entry = task_index.get(task_id)
        if entry is not None:
            role = worker_roles.get(event.get("worker_id"))
            if role:
                entry["assigned_agent"] = role
            if event.get("dependencies"):
                entry["dependencies"] = event["dependencies"]
        job["task_states"][task_id] = "queued"

    elif event_type == "task_started":
        task_id = event["task_id"]
        job["task_states"][task_id] = "running"
        job["task_attempts"][task_id] = job["task_attempts"].get(task_id, 0) + 1
        attempt = job["task_attempts"][task_id]
        task_timing = timing["tasks"].setdefault(task_id, {"created": None, "attempts": []})
        # One entry per dispatch, not a single overwritten started/completed
        # pair -- a task retried after a failed quality check gets a fresh
        # task_started with the SAME task_id, so a singleton would silently
        # replace attempt 1's timing with attempt 2's every time.
        task_timing["attempts"].append({"started": ts, "completed": None})
        task_timing["worker_id"] = event.get("worker_id")
        if timing.get("first_worker_started") is None:
            timing["first_worker_started"] = ts
            if ts and timing.get("root_created"):
                timing_logger.info(
                    "[timing] job=%s phase=time_to_first_worker duration=%.2fs",
                    job_id,
                    (ts - timing["root_created"]).total_seconds(),
                )
        if attempt == 1:
            if task_timing.get("created"):
                duration = (ts - task_timing["created"]).total_seconds()
                timing_logger.info(
                    "[timing] job=%s task=%s phase=assignment duration=%.2fs",
                    job_id, task_id, duration,
                )
        else:
            # CAMEL's RETRY/REPLAN recovery reposts to the SAME worker with
            # no fresh coordinator call when a prior assignee is on record
            # (Workforce._apply_recovery_strategy: "no coordinator call
            # needed") -- this gap is the previous attempt's execution +
            # quality-check + recovery-decision time, not assignment/
            # coordinator latency. Logging it under "assignment" (the old
            # behavior) is exactly what made assignment duration look like
            # it randomly varied 3s-55s: single-attempt tasks showed real,
            # small coordinator latency, multi-attempt tasks showed the
            # entire previous attempt folded in.
            previous = task_timing["attempts"][-2]
            prior_ref = previous.get("completed") or previous["started"]
            duration = (ts - prior_ref).total_seconds()
            timing["retry_gap_total"] = timing.get("retry_gap_total", 0.0) + duration
            timing_logger.info(
                "[timing] job=%s task=%s phase=retry_gap duration=%.2fs attempt=%d",
                job_id, task_id, duration, attempt,
            )
        timing_logger.info(
            "[timing] job=%s task=%s phase=attempt attempt=%d worker=%s",
            job_id,
            task_id,
            attempt,
            worker_roles.get(event.get("worker_id"), "unknown"),
        )

    elif event_type == "task_completed":
        task_id = event["task_id"]
        job["task_states"][task_id] = "completed"
        if event.get("result_summary"):
            job["task_outputs"][task_id] = event["result_summary"]
        task_timing = timing["tasks"].get(task_id, {})
        attempts = task_timing.get("attempts") or []
        last_attempt = attempts[-1] if attempts else None
        if last_attempt is not None:
            last_attempt["completed"] = ts
        duration = event.get("processing_time_seconds")
        if duration is None and last_attempt and last_attempt.get("started"):
            duration = (ts - last_attempt["started"]).total_seconds()
        if duration is not None:
            timing_logger.info(
                "[timing] job=%s task=%s phase=execution_and_quality_check duration=%.2fs",
                job_id, task_id, duration,
            )

    elif event_type == "task_failed":
        task_id = event["task_id"]
        job["task_states"][task_id] = "failed"
        job["task_outputs"][task_id] = event.get("error_message") or ""
        task_timing = timing["tasks"].get(task_id, {})
        attempts = task_timing.get("attempts") or []
        if ts and attempts and attempts[-1].get("started"):
            attempts[-1]["completed"] = ts
            timing_logger.info(
                "[timing] job=%s task=%s phase=failed_attempt duration=%.2fs attempt=%d",
                job_id,
                task_id,
                (ts - attempts[-1]["started"]).total_seconds(),
                job["task_attempts"].get(task_id, 1),
            )

    elif event_type == "token_delta":
        event_task_id = str(event.get("task_id") or "")
        # Every direct/workforce child id is rooted at this job id, while the
        # final replay uses the job id itself. A queue is already per-job, but
        # reject a foreign task id as a second association check so an
        # accidentally misrouted callback cannot append another run's text.
        if event_task_id == job_id or event_task_id.startswith(f"{job_id}."):
            job["streaming_text"] = (
                job.get("streaming_text", "") + event.get("delta", "")
            )
        else:
            logging.getLogger("agent_webapp.jobs").warning(
                "Dropped token_delta for foreign task job=%s task=%s",
                job_id,
                event_task_id,
            )

    elif event_type == "activity":
        job["current_activity"] = {
            "task_id": str(event.get("task_id") or ""),
            "stage": event.get("stage"),
            "detail": str(event.get("detail") or "")[:200],
            "timestamp": event.get("timestamp"),
        }


# Task-level DB writes happen only for these event types (one upsert per
# transition), not on every raw CAMEL event -- worker_created/task_decomposed
# carry no per-task state and firing a write on every token/usage tick would
# swamp the single write lock for no benefit.
_TASK_EVENT_TYPES = {"task_created", "task_assigned", "task_started", "task_completed", "task_failed"}


def _persist_task(job_id: str, task_id: str) -> None:
    job = _JOBS[job_id]
    entry = _JOB_INDEX[job_id].get(task_id)
    if entry is None:
        return
    db.upsert_job_task(
        job_id=job_id,
        task_id=task_id,
        title=entry.get("title", ""),
        description=entry.get("description", ""),
        assigned_agent=entry.get("assigned_agent"),
        dependencies=entry.get("dependencies", []),
        status=job["task_states"].get(task_id, "queued"),
        output=job["task_outputs"].get(task_id),
    )


async def _drain_events(job_id: str, queue: "asyncio.Queue[dict | None]") -> None:
    last_stream_emit_length = 0
    while True:
        event = await queue.get()
        if event is None:
            break
        _apply_event(job_id, event)
        if event.get("event_type") in _TASK_EVENT_TYPES:
            task_id = event.get("task_id")
            if task_id:
                await asyncio.to_thread(_persist_task, job_id, task_id)
        if event.get("event_type") == "token_delta":
            current_length = len(_JOBS[job_id].get("streaming_text") or "")
            if (
                last_stream_emit_length
                and current_length - last_stream_emit_length < SSE_STREAM_EMIT_CHARS
            ):
                continue
            last_stream_emit_length = current_length
        snapshot = _event_snapshot(job_id)
        for subscriber in list(_JOB_CONTROLS[job_id].subscribers):
            subscriber.put_nowait(snapshot)


def _log_job_timing_summary(job_id: str, total_duration: float) -> None:
    timing = _TIMING[job_id]
    decomposition = None
    if timing.get("root_created") and timing.get("decomposed"):
        decomposition = (timing["decomposed"] - timing["root_created"]).total_seconds()

    # assignment_total is attempt-1 latency only (the only attempt CAMEL's
    # coordinator is actually involved in -- see the task_started handler
    # above); execution_total covers every attempt with a recorded
    # completion. retry_gap_total (accumulated live, since a failed
    # quality-check attempt has no "completed" timestamp of its own to
    # recompute it from here) covers the execution+quality-check+recovery
    # time of every attempt that got retried.
    assignment_total = 0.0
    execution_total = 0.0
    for task_id, task_timing in timing["tasks"].items():
        attempts = task_timing.get("attempts") or []
        if attempts and task_timing.get("created"):
            assignment_total += (attempts[0]["started"] - task_timing["created"]).total_seconds()
        for attempt in attempts:
            if attempt.get("started") and attempt.get("completed"):
                execution_total += (attempt["completed"] - attempt["started"]).total_seconds()
    retry_gap_total = timing.get("retry_gap_total", 0.0)

    accounted = (decomposition or 0.0) + assignment_total + execution_total + retry_gap_total
    usage = _JOB_CONTROLS[job_id].usage_tracker.snapshot()
    orch = camel_service.orchestration_telemetry(_JOB_CONTROLS[job_id].workforce)
    timing_logger.info(
        "[timing] job=%s SUMMARY total=%.2fs decomposition=%s "
        "planning_ms=%s assignment_ms=%s assignment_source=%s "
        "assignment_sum=%.2fs execution_and_quality_check_sum=%.2fs "
        "retry_gap_sum=%.2fs other_overhead=%.2fs real_model_calls=%d "
        "total_tokens=%d planning_output_tokens=%s subtasks=%s "
        "targets_planned=%s targets_covered=%s",
        job_id,
        total_duration,
        f"{decomposition:.2f}s" if decomposition is not None else "n/a",
        orch.get("planning_ms"),
        orch.get("assignment_ms"),
        orch.get("assignment_source"),
        assignment_total,
        execution_total,
        retry_gap_total,
        max(0.0, total_duration - accounted),
        usage["model_call_count"],
        usage["total_tokens"],
        orch.get("planning_output_tokens"),
        orch.get("subtask_count"),
        orch.get("targets_planned") or [],
        orch.get("targets_covered") or [],
    )


async def _run_job(job_id: str, content: str, mode: Literal["single", "multi"]) -> None:
    job = _JOBS[job_id]
    control = _JOB_CONTROLS[job_id]
    started = time.monotonic()
    # The live timer used to count from created_at (submission), while the
    # final duration is measured from here (execution start). On a job that
    # waited in the queue the two disagree, so the elapsed counter visibly
    # jumped backwards the moment the run finished. Publishing the real
    # start lets the frontend count the same interval the backend reports.
    job["started_at"] = time.time()
    job["status"] = "running"
    is_partial = False
    await asyncio.to_thread(db.update_job_status, job_id, "running")

    queue: "asyncio.Queue[dict | None]" = asyncio.Queue()
    drain_task = asyncio.create_task(_drain_events(job_id, queue))

    # Product mode is authoritative for new jobs: Single runs one ChatAgent;
    # Workforce always runs CAMEL's orchestration. "direct"/"specialist" are
    # accepted only so persisted jobs created by older versions can finish.
    execution_strategy = job["usage"].get("execution_strategy") or (
        "single" if mode == "single" else "workforce"
    )
    execution_strategy_role = job["usage"].get("execution_strategy_role")
    # Resolved at submission and read back here, so a replayed or scheduled
    # run executes with exactly the capabilities its history records.
    job_capabilities = capabilities.expand(job["usage"].get("capabilities") or [])
    execution_profile = job["usage"].get("execution_profile")

    # When the real final-answer text actually exists, as opposed to when
    # this function finally returns control to `finally`. Those two moments
    # differ by however long replay_final_answer_as_stream's fake typing
    # effect takes (asyncio.sleep(0.015) per 4-char chunk -- 15-30+ real
    # seconds for a medium-length synthesized answer) plus queue drain and
    # DB persistence, none of which is the agent doing work. Left at the
    # `finally` block's plain `time.monotonic() - started`, the reported
    # duration measured a UI animation and disk I/O, not execution -- set
    # once, at the point below where the canonical answer text is fixed,
    # and used in `finally` instead of re-measuring there.
    result_ready_at: Optional[float] = None

    # Re-resolved from the job's own persisted ids rather than carried
    # through from the request, so a replayed or scheduled run applies
    # exactly what its history says it ran with. Skills are read ONLY here,
    # at execution: the router has already decided the strategy above, and
    # nothing between this line and the model call consults them.
    active_skills = skills_catalog.resolve_skills(job.get("skills"))
    resolved_request = _job_resolved_request(job)
    deep_research = bool(job.get("deep_research"))

    try:
        # Fail early and explicitly when the request genuinely needs a
        # capability this system does not have. No executor is started at
        # all: an impossible deliverable costs one routing call, not an
        # agent looping to its iteration cap discovering the same thing.
        # This is the *cheap* half of the anti-runaway work; the primary half
        # is correct capability selection above.
        unsupported = job.get("unsupported_capabilities") or []
        if unsupported:
            job["result"] = camel_service.unsupported_capability_message(unsupported)
            job["status"] = "failed"
            job["error"] = (
                "unsupported capability requested: " + ", ".join(sorted(unsupported))
            )
            return
        # Same principle, applied to the requested OUTPUT FORMAT: asking for
        # legacy binary .doc/.ppt cannot succeed no matter how well the
        # acquisition goes, so it must not cost a full web job first.
        # .pdf/.docx/.pptx ARE producible (create_pdf_artifact /
        # create_docx_artifact / create_pptx_artifact) and must NOT be
        # flagged here -- see capabilities._UNSUPPORTED_ARTIFACT_RE.
        # Inspect the resolved request, not combined `content` (which can
        # include attached filenames such as report.pdf) and not the raw
        # follow-up pronoun ("bunu pdf ver") before turn resolution.
        if mode == "multi":
            bad_format = capabilities.detect_unsupported_deliverable(
                resolved_request
            )
            if bad_format:
                job["result"] = capabilities.unsupported_deliverable_message(bad_format)
                job["status"] = "failed"
                job["error"] = (
                    f"unsupported deliverable format requested: {bad_format}"
                )
                return
        if execution_strategy in ("single", "direct"):
            # No Workforce at all -- run_single_agent emits its own events
            # directly onto the same queue _drain_events is already reading.
            # Its streaming IS the real model generation (stream=True, see
            # that function), not a replay of already-finished work, so the
            # moment it returns already is the moment the answer is ready --
            # no separate "replay" step exists on this path to exclude.
            def _record_direct_binding(bound, source: str) -> None:
                job["usage"]["bound_capabilities"] = sorted(bound.capabilities)
                job["usage"]["bound_tools"] = sorted(bound.names)
                job["usage"]["tool_binding_source"] = source

            job["usage"]["agent_executor"] = agent_executor()
            result_task = await run_single_agent_executor(
                content,
                control.usage_tracker,
                job_id,
                queue,
                skills=active_skills,
                workspace_root=job.get("workspace_root"),
                tool_call_recorder=control.tool_call_recorder,
                job_capabilities=job_capabilities,
                profile=execution_profile,
                request_topic=resolved_request,
                tool_binding_source=str(
                    job["usage"].get("tool_binding_source") or ""
                ),
                on_tools_bound=_record_direct_binding,
                enforce_artifact_grounding=(
                    str(job["usage"].get("tool_binding_source") or "").startswith(
                        "single_"
                    )
                ),
            )
            result_ready_at = time.monotonic()

            # Whether to offer "switch to Workforce", decided by the same
            # classifier every Workforce job already uses instead of by a
            # keyword list. The regex fallback below only recognised an
            # explicit time window, so "guncel dolar" -- which Single had
            # just refused for lack of live data -- was not marked as
            # needing any, and the offer never appeared.
            #
            # Runs AFTER the answer, so it adds nothing to the wait the
            # user actually feels, and its result is stored rather than
            # recomputed on every status poll.
            try:
                single_intent = await artifact_intent.classify_artifact_intent(
                    resolved_request or content,
                    control.usage_tracker,
                )
                job["usage"]["suggests_workforce"] = bool(
                    single_intent.needs_web_research
                    or single_intent.wants_artifact
                    or single_intent.needs_code_execution
                )
            except Exception:  # noqa: BLE001 - display hint, never fatal
                stabilization_logger.warning(
                    "single-mode workforce hint classification failed",
                    exc_info=True,
                )
        else:
            # SPECIALIST reuses the same Workforce build + dispatch loop as
            # WORKFORCE -- only the decompose+assign calls are skipped (see
            # camel_service._run_task_direct) when execution_strategy is
            # "specialist", via fast_path_role below. Runs on its own
            # thread with its own event loop, so CAMEL's internal
            # synchronous coordinator calls (decomposition/assignment/
            # quality-check -- see camel_service.run_workforce_in_thread for
            # exact line refs) can never block *this* event loop, which
            # every other request in the app shares. loop=asyncio.
            # get_running_loop() lets the callback marshal events back here
            # safely from that other thread.
            callback = QueueWorkforceCallback(queue, asyncio.get_running_loop())
            intent = artifact_intent.from_persisted(job.get("usage"))
            if intent is None:
                intent = await artifact_intent.classify_artifact_intent(
                    resolved_request,
                    control.usage_tracker,
                    persisted=job.get("usage"),
                )
                job["usage"]["wants_artifact"] = intent.wants_artifact
                job["usage"]["artifact_type"] = intent.artifact_type
                job["usage"]["artifact_intent_source"] = intent.source
                job["usage"]["needs_web_research"] = intent.needs_web_research
                job["usage"]["needs_code_execution"] = intent.needs_code_execution
                job["usage"]["reuses_uploaded_document"] = (
                    intent.reuses_uploaded_document
                )
                job["usage"]["opens_on_user_computer"] = (
                    intent.opens_on_user_computer
                )
                job["usage"]["sends_email"] = intent.sends_email
                job["usage"]["email_to"] = list(intent.email_to)
                job["usage"]["reads_email"] = intent.reads_email
                job["usage"]["reads_social"] = intent.reads_social
            wants_artifact = intent.wants_artifact
            artifact_type = intent.artifact_type
            job["usage"]["agent_executor"] = agent_executor()
            result_task = await asyncio.to_thread(
                run_workforce_in_thread_executor,
                callback,
                control.usage_tracker,
                control.tool_call_recorder,
                content,
                job_id,
                lambda workforce: setattr(control, "workforce", workforce),
                job.get("workspace_root"),
                execution_strategy_role if execution_strategy == "specialist" else None,
                active_skills,
                job_capabilities,
                resolved_request,
                wants_artifact,
                artifact_type,
                deep_research=deep_research,
                conversation_id=job.get("conversation_id"),
                reuses_uploaded_document=intent.reuses_uploaded_document,
                opens_on_user_computer=intent.opens_on_user_computer,
                reads_email=intent.reads_email,
                reads_social=intent.reads_social,
            )
            if not control.cancelled:
                synthesized, is_partial = await camel_service.synthesize_workforce_result(
                    result_task,
                    resolved_request,
                    control.usage_tracker,
                    skills=active_skills,
                    job_capabilities=job_capabilities,
                    workspace_root=job.get("workspace_root"),
                )
                if synthesized:
                    result_task.result = synthesized
                else:
                    # No synthesis to show: result_task.result is CAMEL's
                    # raw subtask string-join, whose "--- Subtask <uuid>.N
                    # Result ---" delimiters are internal scaffolding, not
                    # part of the answer. Clean it here, before the replay,
                    # so the streamed text and the stored/final text are
                    # the same string rather than diverging at completion.
                    result_task.result = camel_service.strip_subtask_scaffolding(
                        result_task.result or ""
                    )
                # The canonical text is fixed as of here -- everything from
                # this point on (the typing replay, then queue drain, then
                # DB writes in `finally`) is presentation/persistence, not
                # execution, so it must not inflate the reported duration.
                result_ready_at = time.monotonic()
                await camel_service.replay_final_answer_as_stream(
                    result_task.result or "", job_id, queue
                )
        job["result"] = result_task.result or ""
        # Artifacts are read from disk, so this is evidence of work the user
        # can actually use -- not a claim. It changes what a non-DONE run
        # means: a live job created a valid XLSX, showed it in the UI, and
        # still reported "failed" because the executor ran out of turns before
        # writing a summary. From the user's side that is not a failure, it is
        # an incomplete explanation of a delivered file.
        # Read fresh: the executor was writing files right up to this point,
        # so the 1-second snapshot memo is likely to predate the last one.
        _ARTIFACT_CACHE.pop(job_id, None)
        produced = _job_artifacts(job)
        produced = _ensure_requested_pdf(job, produced)
        if control.cancelled:
            job["status"] = "cancelled"
        elif mode == "multi" and is_partial:
            job["status"] = "partial"
        elif result_task.state.value == "DONE":
            job["status"] = "done"
        elif produced:
            # Downgraded to partial, never to done: the run genuinely did not
            # finish cleanly, and saying otherwise would hide a real problem.
            job["status"] = "partial"
            job["error"] = job.get("error") or (
                "The run did not finish cleanly, but the files below were "
                "created successfully and are ready to download."
            )
            logging.getLogger("agent_webapp.jobs").warning(
                "job=%s did not reach DONE but produced %d artifact(s); "
                "reporting partial",
                job_id,
                len(produced),
            )
        else:
            job["status"] = "failed"
        if (
            job["status"] in {"done", "partial"}
            and not control.cancelled
            and job.get("origin") != "schedule"
        ):
            try:
                notice = await _email_answer_from_prompt(job)
            except Exception:  # noqa: BLE001 - a mail problem must not fail the job
                logging.getLogger("agent_webapp.jobs").exception(
                    "job=%s could not prepare the email it was asked for", job_id
                )
                notice = "📧 E-posta hazırlanamadı; ayrıntı için sunucu günlüğüne bakın."
            if notice:
                # Appended after the typing replay, so it lands in the stored
                # answer and shows when the job completes.
                job["result"] = f"{job['result']}\n\n{notice}".strip()
    except Exception as exc:  # noqa: BLE001 - surfaced as a bounded job error
        if not control.cancelled:
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Use the moment the answer was actually finalized when we have
        # one (the normal case for every successful/partial/cancelled-
        # before-finalization run). Fall back to full wall-clock time for
        # anything that never reached that point -- an exception, or a
        # cancellation that landed before synthesis ran -- where "how long
        # did this take" has no more precise answer than "until it stopped".
        job["duration_seconds"] = (
            result_ready_at if result_ready_at is not None else time.monotonic()
        ) - started
        await queue.put(None)
        await drain_task
        # Every event has now been applied, so anything still non-terminal
        # never received a task_completed/task_failed at all. That happens
        # for real: a subtask can still be in flight when the run ends --
        # CAMEL's dispatch loop aborts on its own _get_returned_task
        # timeout (workforce.py:3906) with tasks outstanding, and
        # run_workforce_in_thread's teardown then cancels whatever worker
        # coroutines are left, so no terminal event is ever emitted for
        # them. Without this the task stays "running" forever in the
        # snapshot AND in the persisted row -- observed live as a subtask
        # still spinning in the UI after the job had finalized and
        # reported its result.
        #
        # /cancel already does exactly this reconciliation for the same
        # reason (see cancel_job); the normal finalization path simply
        # never got it. Deliberately does not touch job["status"] -- what
        # the run produced is synthesis's call, not this loop's.
        orphaned = [
            task_id
            for task_id, state in job["task_states"].items()
            if state in {"queued", "running"}
        ]
        for task_id in orphaned:
            job["task_states"][task_id] = "cancelled" if control.cancelled else "failed"
            if not job["task_outputs"].get(task_id):
                job["task_outputs"][task_id] = (
                    "This subtask did not finish before the run ended."
                )
            _persist_task(job_id, task_id)
        if orphaned:
            logging.getLogger("agent_webapp.jobs").warning(
                "job=%s finalized with %d non-terminal task(s): %s",
                job_id,
                len(orphaned),
                ", ".join(orphaned),
            )
        _log_job_timing_summary(job_id, job["duration_seconds"])
        # control.usage_tracker/tool_call_recorder hold the only authoritative
        # final numbers (see _job_snapshot) -- fold them into the stored job
        # dict now so the persisted row (and any snapshot taken later in this
        # same process) reflects real totals, not the all-zero usage dict the
        # job was created with.
        final_usage = control.usage_tracker.snapshot()
        final_tool_calls = control.tool_call_recorder.snapshot()
        call_count, error_count, by_name = _tool_activity(final_tool_calls)
        final_artifacts = _job_artifacts(job)
        job["usage"] = {
            **job["usage"],
            **final_usage,
            "usage_available": final_usage["model_call_count"] > 0,
            "by_stage": control.usage_tracker.stage_snapshot(),
            # Persisted in usage_json: job_tool_calls has no duration columns.
            "timeline": _timeline(control.usage_tracker, final_tool_calls),
            # Folded into the persisted row too, so history keeps the same
            # answers a live snapshot gives -- otherwise these fields would
            # silently become null for every job after a restart.
            "tool_call_count": call_count,
            "tool_error_count": error_count,
            "tool_calls_by_name": by_name,
            "artifact_count": len(final_artifacts),
            "terminal_reason": _terminal_reason(job, len(final_artifacts)),
            **camel_service.orchestration_telemetry(control.workforce),
        }
        await asyncio.to_thread(db.finalize_job, job, final_tool_calls)
        workspace_id = job.get("workspace_id")
        conversation_id = job.get("conversation_id")
        if workspace_id and conversation_id:
            reply = job["result"] if job["status"] in {"done", "partial"} and job["result"] else f"[{job['status']}] {job.get('error') or ''}".strip()
            await asyncio.to_thread(
                db.insert_message,
                workspace_id,
                conversation_id,
                job_id,
                "assistant",
                reply,
                time.time(),
            )
        # The run has finished writing, so the last cached artifact list may
        # predate its final file. Drop it so the terminal snapshot -- the one
        # the UI keeps -- is read fresh from disk.
        _ARTIFACT_CACHE.pop(job_id, None)
        final_snapshot = _job_snapshot(job_id).model_dump(mode="json")
        for subscriber in list(control.subscribers):
            subscriber.put_nowait(final_snapshot)
            subscriber.put_nowait(None)


def _job_task_finished(task: "asyncio.Task") -> None:
    """Guarantee a job reaches a terminal state even if _run_job itself dies.

    _run_job wraps its execution in try/except/finally, but the lines BEFORE
    that -- the job/control lookups, the first DB write -- are not covered, and
    neither is a CancelledError delivered at an await point outside the block.
    An exception there propagates into the Task and nowhere else: asyncio only
    surfaces it if someone retrieves it, so the job silently stays "running"
    and the UI spins forever with no error to show.

    This is the backstop for exactly that: retrieve the exception, log it, and
    force a terminal status so the failure is visible and the job is over.
    """
    if task.cancelled():
        exception: Optional[BaseException] = None
    else:
        exception = task.exception()
    if exception is None and not task.cancelled():
        return

    job_id = getattr(task, "_agent_job_id", None)
    job = _JOBS.get(job_id) if job_id else None
    logger = logging.getLogger("agent_webapp.jobs")
    if job is None:
        logger.error("Background job task failed with no job row: %r", exception)
        return
    if job.get("status") in TERMINAL_STATUSES:
        # The normal path already finalized; the exception happened during
        # teardown and has no bearing on the reported outcome.
        logger.warning("job=%s raised after finalizing: %r", job_id, exception)
        return

    if task.cancelled():
        job["status"] = "cancelled"
        job["error"] = job.get("error") or "The job was cancelled."
    else:
        job["status"] = "error"
        job["error"] = f"{type(exception).__name__}: {exception}"
    logger.error(
        "job=%s never reached a terminal state on its own; forcing %s",
        job_id,
        job["status"],
        exc_info=exception,
    )
    try:
        db.finalize_job(job, [])
    except Exception:  # noqa: BLE001 - the log above is the important part
        logger.exception("job=%s could not be persisted after forced failure", job_id)


def _requested_file_ids(req: RunPipelineRequest) -> list[str]:
    ids = list(req.file_ids)
    if req.file_id and req.file_id not in ids:
        ids.insert(0, req.file_id)
    if len(ids) > MAX_UPLOAD_SOURCES:
        raise HTTPException(
            status_code=400, detail=f"A job may reference at most {MAX_UPLOAD_SOURCES} uploads"
        )
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Duplicate file IDs are not allowed")
    return ids


def _job_resolved_request(job: dict[str, Any]) -> str:
    usage = job.get("usage") or {}
    resolved = str(usage.get("resolved_request") or "").strip()
    return resolved or str(job.get("topic") or "").strip()


def _user_message_content(topic: str, records: list[dict[str, Any]]) -> str:
    names = [
        str(record.get("filename") or "").strip()
        for record in records
        if str(record.get("filename") or "").strip()
    ]
    if not names:
        return topic
    return f"{topic}\n\n[attached: {', '.join(names)}]"


def _file_catalog(
    this_turn: list[dict[str, Any]], bag: list[dict[str, Any]]
) -> list[dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for item in list(this_turn) + list(bag):
        file_id = str(item.get("file_id") or "").strip()
        if not file_id or file_id in catalog:
            continue
        catalog[file_id] = {
            "file_id": file_id,
            "filename": str(item.get("filename") or file_id),
        }
        if len(catalog) >= MAX_UPLOAD_SOURCES:
            break
    return list(catalog.values())


def _merge_file_ids(this_turn: list[str], bind: tuple[str, ...] | list[str]) -> list[str]:
    merged: list[str] = []
    for file_id in list(this_turn) + list(bind):
        token = str(file_id or "").strip()
        if token and token not in merged:
            merged.append(token)
        if len(merged) >= MAX_UPLOAD_SOURCES:
            break
    return merged


def _load_upload_records_optional(file_ids: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for file_id in file_ids:
        try:
            records.append(_load_upload_record(file_id))
        except HTTPException:
            continue
    return records


def _ordered_upload_records(
    merged_ids: list[str], known: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(record.get("file_id") or ""): record for record in known}
    missing = [file_id for file_id in merged_ids if file_id not in by_id]
    for record in _load_upload_records_optional(missing):
        by_id[str(record.get("file_id") or "")] = record
    return [by_id[file_id] for file_id in merged_ids if file_id in by_id]


async def _resolve_turn_for_job(
    mode: Literal["single", "multi"],
    topic: str,
    *,
    tracker: UsageTracker,
    prior_messages: list[dict[str, Any]],
    catalog: list[dict[str, str]],
) -> turn_resolution.TurnResolution:
    if mode == "single":
        return turn_resolution.single_mode(topic)
    if not prior_messages:
        return turn_resolution.first_turn(topic)
    return await turn_resolution.resolve_turn(
        topic,
        tracker=tracker,
        prior_messages=prior_messages,
        catalog=catalog,
    )


def _combined_content(
    topic: str,
    explicit_context: Optional[str],
    workspace_context: Optional[str],
    conversation_context: Optional[str],
) -> str:
    sections = [topic]
    if explicit_context:
        sections.append("=== USER-PROVIDED CONTEXT ===\n" + explicit_context.strip())
    # Conversation history goes before the uploaded-file workspace context so
    # a long file dump can't push the (usually more decision-relevant) prior
    # Q&A entirely out of the model's attention.
    if conversation_context:
        sections.append(conversation_context)
    if workspace_context:
        sections.append(workspace_context)
    return "\n\n".join(sections)


def _resolve_workspace_id(requested: Optional[str]) -> str:
    """Never blocks task submission on workspace state: a missing id gets a
    fresh workspace, and an id the DB doesn't recognize (stale
    localStorage, a fresh DB, a typo) gets silently (re)created under that
    same id rather than rejected -- see RunPipelineRequest.workspace_id."""
    workspace_id = requested or str(uuid.uuid4())
    now = time.time()
    db.create_workspace(workspace_id, name=None, created_at=now)
    db.touch_workspace(workspace_id, now)
    return workspace_id


def _resolve_conversation_id(
    workspace_id: str,
    requested: Optional[str],
    title: str,
) -> str:
    """Create a new thread or validate a requested thread's workspace."""
    now = time.time()
    if requested:
        conversation = db.get_conversation(requested)
        if conversation is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown conversation_id: {requested}",
            )
        if conversation["workspace_id"] != workspace_id:
            raise HTTPException(
                status_code=400,
                detail="Conversation does not belong to the selected workspace.",
            )
        db.touch_conversation(requested, now)
        return requested

    conversation_id = str(uuid.uuid4())
    db.create_conversation(
        conversation_id,
        workspace_id,
        title.strip() or None,
        now,
    )
    return conversation_id


async def _build_conversation_context(
    conversation_id: str, tracker: UsageTracker
) -> Optional[str]:
    """Bounded, newest-first-kept prior history for one conversation, in
    the same truncate-and-say-so style as workspace.build_workspace_context
    for uploaded files -- except turns that would be truncated are folded
    into a running per-conversation summary (db.conversations.summary)
    instead of silently dropped, so a long conversation degrades to "recent
    turns verbatim + a dense summary of the rest" rather than losing older
    context entirely once it crosses the char budget."""
    recent = await asyncio.to_thread(db.load_recent_messages, conversation_id)
    if not recent:
        return None

    conversation = await asyncio.to_thread(db.get_conversation, conversation_id) or {}
    existing_summary = (conversation.get("summary") or "").strip()
    summarized_through = float(conversation.get("summarized_through_created_at") or 0)

    # recent is newest-first; walk it accumulating from the most recent
    # message backward so a budget cut always drops the OLDEST turns first
    # (a contiguous suffix of `recent`), then reverse back to chronological
    # order for the prompt.
    header = (
        "PRIOR MESSAGES IN THIS CONVERSATION (most recent tasks first "
        "below, but presented in chronological order):\n"
    )
    remaining = CONVERSATION_CONTEXT_MAX_CHARS - len(header)
    kept: list[str] = []
    dropped: list[dict[str, Any]] = []
    hit_limit = False
    for message in recent:
        role_label = "USER" if message["role"] == "user" else "ASSISTANT"
        entry = f"\n--- {role_label} ---\n{message['content']}\n"
        if hit_limit or len(entry) > remaining:
            hit_limit = True
            dropped.append(message)
            continue
        kept.append(entry)
        remaining -= len(entry)

    summary_text = existing_summary
    to_fold = [m for m in dropped if float(m["created_at"]) > summarized_through]
    if to_fold:
        # dropped is newest-first (same order as `recent`); fold in
        # chronological order so the summary reads as a timeline.
        entries = [
            f"--- {'USER' if m['role'] == 'user' else 'ASSISTANT'} ---\n{m['content']}"
            for m in reversed(to_fold)
        ]
        try:
            summary_text = await camel_service.summarize_conversation_history(
                existing_summary, entries, tracker
            )
            newest_folded = max(float(m["created_at"]) for m in to_fold)
            await asyncio.to_thread(
                db.save_conversation_summary, conversation_id, summary_text, newest_folded
            )
        except Exception:
            stabilization_logger.warning(
                "Conversation summarization failed for %s; older turns dropped "
                "with no summary this time",
                conversation_id,
                exc_info=True,
            )

    if not kept and not summary_text:
        return None

    kept.reverse()
    lines = [header]
    if summary_text:
        lines.append(f"\n[Summary of earlier conversation:]\n{summary_text}\n")
    lines.extend(kept)
    return "".join(lines)


def _routing_for_ui_mode(
    mode: Literal["single", "multi"],
    topic: str,
    *,
    has_workspace_files: bool,
    workspace_file_kinds: list[str],
) -> capability_routing.RoutingDecision:
    """Translate the user's explicit mode into one execution architecture.

    Single mode only. Workforce routing uses ``_routing_for_workforce`` so
    artifact intent LLM can union ARTIFACTS into the effective capability set.
    """
    if mode == "single":
        return capability_routing.merge_decision(
            "single",
            declared=frozenset(),
            structural=frozenset(),
            profile=capability_routing.infer_profile(
                topic, has_workspace_files, frozenset()
            ),
            reason="user selected Single Agent",
            outcome=capability_routing.ROUTING_SKIPPED,
        )

    raise ValueError("_routing_for_ui_mode is for single mode; use _routing_for_workforce")


async def _routing_for_workforce(
    topic: str,
    *,
    has_workspace_files: bool,
    workspace_file_kinds: list[str],
    tracker: UsageTracker,
    persisted: Optional[dict] = None,
    attachment_names: Optional[list[str]] = None,
) -> tuple[capability_routing.RoutingDecision, artifact_intent.ArtifactIntent]:
    """Structural floor (WEB/FILES/CODE) + canonical work intent → effective caps."""
    base = capabilities.expand(
        capabilities.workforce_structural_requirements(
            topic,
            has_workspace_files,
            workspace_file_kinds,
        )
    )
    intent = await artifact_intent.classify_artifact_intent(
        topic, tracker, persisted=persisted, attachment_names=attachment_names
    )
    # Reusing an uploaded document is never a programming job. The
    # classifier is not deterministic about needs_code_execution, and one
    # stray True was enough to change everything downstream: CODE_EXEC is
    # granted, infer_profile sees it and returns the coding persona, the
    # Coding Agent runs, and the user asking for their template filled in
    # got a Python script explaining how to fill it in -- after two
    # minutes. Cheap to rule out here, where both facts are known.
    needs_code = intent.needs_code_execution and not intent.reuses_uploaded_document
    # Granted only when the deployment actually allows reading. Without this
    # the capability would be handed out on the classifier's word alone, the
    # Mail Agent would be scheduled, and the run would spend a worker slot
    # discovering that OUTLOOK_READ_ENABLED is off -- a slower way to reach
    # the same "I cannot read your mailbox" that costs nothing here.
    reads_email = bool(intent.reads_email) and outlook.read_enabled()
    # Granted only when this deployment can actually read, for the same
    # reason as mail above: without the check the Social Agent is scheduled
    # on the classifier's word alone and the run spends a worker slot
    # discovering that rdt-cli is missing or the session cookie has expired
    # -- a slower route to the same "I cannot read Reddit" that costs
    # nothing here. The probe is cached, so this is not a subprocess per job.
    reads_social = bool(intent.reads_social) and social_health.check("reddit").ok
    effective = capabilities.apply_artifact_intent(
        base,
        wants_artifact=intent.wants_artifact,
        needs_web_research=intent.needs_web_research,
        needs_code_execution=needs_code,
        text=topic,
        has_workspace_files=has_workspace_files,
        workspace_file_kinds=workspace_file_kinds,
        reads_email=reads_email,
        reads_social=reads_social,
    )
    decision = capability_routing.merge_decision(
        "workforce",
        declared=effective,
        structural=frozenset(),
        profile=capability_routing.infer_profile(
            topic, has_workspace_files, effective
        ),
        reason="user selected Workforce",
        outcome=capability_routing.ROUTING_SKIPPED,
    )
    return decision, intent


@app.post("/run-pipeline", response_model=RunPipelineResponse)
async def run_pipeline(req: RunPipelineRequest):
    """Start a background job with an optional isolated upload workspace."""
    return await submit_job(req)


async def submit_job(
    req: RunPipelineRequest,
    *,
    conversation_title: Optional[str] = None,
    origin: Literal["user", "schedule"] = "user",
) -> RunPipelineResponse:
    """Everything /run-pipeline does, callable in-process.

    Scheduled tasks start their runs here (scheduling/runner.py) so they get
    exactly the turn resolution, intent classification and routing a message
    typed by a user gets. ``origin="schedule"`` only removes what cannot
    work without a person present: opening a file on their computer.
    """
    this_turn_ids = _requested_file_ids(req)
    this_turn_records = [_load_upload_record(file_id) for file_id in this_turn_ids]
    this_turn_bytes = sum(
        int(record.get("upload_size_bytes") or 0) for record in this_turn_records
    )
    if this_turn_bytes > MAX_BATCH_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Combined job sources exceed the {MAX_BATCH_UPLOAD_BYTES}-byte limit",
        )

    job_id = str(uuid.uuid4())
    # Created early (rather than at its usual spot alongside _JOBS[job_id])
    # so the routing call below -- which needs a real UsageTracker to
    # record its stage="routing" usage under -- shares the same tracker
    # _run_job will use for the rest of this job, instead of a throwaway
    # one whose numbers would never reach the job's persisted usage.
    control = _JobControl(job_id)
    workspace_id = await asyncio.to_thread(_resolve_workspace_id, req.workspace_id)
    conversation_id = await asyncio.to_thread(
        _resolve_conversation_id,
        workspace_id,
        req.conversation_id,
        conversation_title or req.topic,
    )
    # Fetched BEFORE this job's own user message is inserted below, so a job
    # never sees its own topic reflected back as "prior" context.
    conversation_context = await _build_conversation_context(
        conversation_id, control.usage_tracker
    )
    prior_messages = await asyncio.to_thread(db.load_recent_messages, conversation_id)
    bag = await asyncio.to_thread(db.list_conversation_files, conversation_id)
    catalog = _file_catalog(this_turn_records, bag)
    resolution = await _resolve_turn_for_job(
        req.mode,
        req.topic,
        tracker=control.usage_tracker,
        prior_messages=prior_messages,
        catalog=catalog,
    )
    merged_ids = _merge_file_ids(this_turn_ids, resolution.bind_file_ids)
    records = _ordered_upload_records(merged_ids, this_turn_records)
    total_upload_bytes = sum(
        int(record.get("upload_size_bytes") or 0) for record in records
    )
    if total_upload_bytes > MAX_BATCH_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Combined job sources exceed the {MAX_BATCH_UPLOAD_BYTES}-byte limit",
        )

    workspace_data: Optional[dict[str, Any]] = None
    workspace_context: Optional[str] = None
    if records:
        try:
            workspace_data, workspace_context = build_job_workspace(job_id, records, WORKSPACE_DIR)
        except WorkspaceSecurityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    content = _combined_content(
        resolution.resolved_request,
        req.context_text,
        workspace_context,
        conversation_context,
    )

    # Files on this turn or rebound from the thread bag need a worker that
    # can read them. Conversation-only Direct is text follow-ups only.
    needs_external_work = bool(
        resolution.needs_external_work or this_turn_ids or resolution.bind_file_ids
    )
    # Format/chat redeelivery of prior work must stay on Workforce so we can
    # reuse notes (and write PDF/PPTX) without a tool-less Direct refusal.
    is_reformat_followup = None
    is_chat_redelivery_followup = None
    wants_local_open = None
    try:
        from execution.langgraph_runtime.workforce import (
            _is_chat_redelivery_followup as is_chat_redelivery_followup,
            _is_reformat_followup as is_reformat_followup,
            _request_wants_local_open as wants_local_open,
        )
    except Exception:  # noqa: BLE001
        stabilization_logger.exception(
            "reformat follow-up detection failed; leaving turn routing as-is"
        )
    resolved_ask = resolution.resolved_request or req.topic or ""
    if (
        req.mode == "multi"
        and is_reformat_followup is not None
        and is_reformat_followup(resolved_ask)
    ):
        needs_external_work = True
    # "bunu bilgisayarımda aç" after a file already exists needs no new web
    # research or download, so turn_resolution's own needs_external_work
    # rule reads it as chat-only and routes to Direct -- but Direct has no
    # LOCAL_COMPUTER tool, so the model can only describe how to open the
    # file by hand instead of actually opening it (see
    # _request_wants_local_open's ROLE_GENERAL dispatch in workforce.py,
    # which never runs unless this turn reaches Workforce). Same override
    # shape as is_reformat_followup above, for the same reason.
    if (
        req.mode == "multi"
        and origin == "user"
        and wants_local_open is not None
        and wants_local_open(resolved_ask)
    ):
        needs_external_work = True
    file_kinds = [
        str(record.get("kind") or "") for record in records
    ] + [
        Path(str(record.get("filename") or "")).suffix.lstrip(".").lower()
        for record in records
    ]
    if req.mode == "multi" and not needs_external_work:
        routing = capability_routing.merge_decision(
            "direct",
            declared=frozenset(),
            structural=frozenset(),
            profile=capability_routing.infer_profile(
                resolution.resolved_request, False, frozenset()
            ),
            reason="turn_resolution: conversation-only follow-up",
            outcome=capability_routing.ROUTING_TURN_RESOLUTION,
        )
        binding_source = "conversation_followup"
        artifact_intent_result: Optional[artifact_intent.ArtifactIntent] = None
    elif req.mode == "single":
        routing = _routing_for_ui_mode(
            req.mode,
            resolution.resolved_request,
            has_workspace_files=bool(records),
            workspace_file_kinds=file_kinds,
        )
        binding_source = "single_chatbot"
        artifact_intent_result = None
    else:
        routing, artifact_intent_result = await _routing_for_workforce(
            resolution.resolved_request,
            has_workspace_files=bool(records),
            workspace_file_kinds=file_kinds,
            tracker=control.usage_tracker,
            attachment_names=[
                str(r.get("filename") or "") for r in records if isinstance(r, dict)
            ],
        )
        binding_source = "workforce_subtask_capabilities"
        # Chat redeelivery must not re-create Excel/PDF from a confused intent.
        if (
            artifact_intent_result is not None
            and is_chat_redelivery_followup is not None
            and is_chat_redelivery_followup(resolution.resolved_request or "")
        ):
            artifact_intent_result = artifact_intent.ArtifactIntent(
                wants_artifact=False,
                artifact_type="none",
                source="chat_redelivery_override",
                needs_web_research=False,
                needs_code_execution=False,
                reuses_uploaded_document=False,
            )
    # Nobody is at the computer when a scheduled run finishes, and the local
    # bridge would open the file on whichever machine hosts it.
    if (
        origin == "schedule"
        and artifact_intent_result is not None
        and artifact_intent_result.opens_on_user_computer
    ):
        artifact_intent_result = dataclasses.replace(
            artifact_intent_result, opens_on_user_computer=False
        )
    # A workspace used to exist only when there were uploads. A job that must
    # WRITE a file (or run code) needs the same isolated, 0o700 directory
    # even with nothing attached -- otherwise the artifact capability binds
    # to nothing and the run is back to describing a file it cannot create.
    if workspace_data is None and capabilities.requires_workspace(routing.capabilities):
        try:
            workspace_root_path = await asyncio.to_thread(
                ensure_job_workspace, job_id, WORKSPACE_DIR
            )
        except WorkspaceSecurityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        workspace_data = {
            "root": str(workspace_root_path),
            "summary": None,
            "attachments": [],
        }

    job = {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "topic": req.topic,
        "status": "queued",
        "mode": req.mode,
        "task_plan": [],
        "planning_fallback": False,
        "planning_errors": [],
        "task_states": {},
        "task_outputs": {},
        "task_attempts": {},
        "result": None,
        "error": None,
        "streaming_text": "",
        "current_activity": None,
        "created_at": time.time(),
        "started_at": None,
        "duration_seconds": None,
        "model_provider": os.environ.get("MODEL_PROVIDER"),
        "model_name": os.environ.get("MODEL_NAME"),
        "attachments": workspace_data["attachments"] if workspace_data else [],
        "workspace": workspace_data["summary"] if workspace_data else None,
        "workspace_root": workspace_data["root"] if workspace_data else None,
        "usage": UsageTelemetry(
            model_name=os.environ.get("MODEL_NAME"),
            provider=os.environ.get("MODEL_PROVIDER"),
            execution_strategy=routing.strategy,
            execution_strategy_role=routing.role,
            routing_outcome=routing.outcome,
            capabilities=sorted(routing.capabilities),
            ui_mode="single" if req.mode == "single" else "workforce",
            tool_binding_source=binding_source,
            bound_capabilities=sorted(routing.capabilities),
            execution_profile=routing.profile,
            strategy_reason=routing.reason,
            advisory_strategy=routing.advisory_strategy,
            resolved_request=resolution.resolved_request,
            turn_resolution_source=resolution.source,
            needs_external_work=needs_external_work,
            wants_artifact=(
                artifact_intent_result.wants_artifact
                if artifact_intent_result is not None
                else None
            ),
            artifact_type=(
                artifact_intent_result.artifact_type
                if artifact_intent_result is not None
                else None
            ),
            artifact_intent_source=(
                artifact_intent_result.source
                if artifact_intent_result is not None
                else None
            ),
            needs_web_research=(
                artifact_intent_result.needs_web_research
                if artifact_intent_result is not None
                else None
            ),
            reuses_uploaded_document=(
                artifact_intent_result.reuses_uploaded_document
                if artifact_intent_result is not None
                else None
            ),
            opens_on_user_computer=(
                artifact_intent_result.opens_on_user_computer
                if artifact_intent_result is not None
                else None
            ),
            sends_email=(
                artifact_intent_result.sends_email
                if artifact_intent_result is not None
                else None
            ),
            email_to=(
                list(artifact_intent_result.email_to)
                if artifact_intent_result is not None
                else []
            ),
            reads_email=(
                artifact_intent_result.reads_email
                if artifact_intent_result is not None
                else None
            ),
            reads_social=(
                artifact_intent_result.reads_social
                if artifact_intent_result is not None
                else None
            ),
        ).model_dump(),
        # Set only when the request needs something this system does not
        # implement. _run_job turns it into an immediate, explicit answer --
        # see camel_service.unsupported_capability_message for why an
        # impossible request must cost one routing call rather than an
        # executor's whole iteration budget.
        "unsupported_capabilities": list(routing.unsupported),
        # "user" or "schedule"; a scheduled run has nobody at the computer,
        # so it emails through the scheduler instead of opening a draft.
        "origin": origin,
        "cancellation_requested": False,
        "cancellation_note": None,
        # Resolved once, at submission, and never re-read from the request
        # afterwards: this list IS the record of what the run executed with.
        # Unknown ids are already dropped and the selection capped by
        # resolve_skills, so what lands here is exactly what will be applied.
        "skills": skills_catalog.skill_ids(
            skills_catalog.resolve_skills(req.skill_ids)
        ),
        # Same re-resolved-at-execution convention as "skills" above: read
        # from the job record inside _run_job, not carried from the request.
        "deep_research": bool(req.deep_research),
    }
    _JOBS[job_id] = job
    _JOB_CONTROLS[job_id] = control
    _JOB_INDEX[job_id] = {}
    _WORKER_ROLES[job_id] = {}
    _TIMING[job_id] = {
        "root_created": None,
        "decomposed": None,
        "first_worker_started": None,
        "tasks": {},
    }
    await asyncio.to_thread(db.save_job, job)
    await asyncio.to_thread(
        db.insert_message,
        workspace_id,
        conversation_id,
        job_id,
        "user",
        _user_message_content(req.topic, records),
        job["created_at"],
    )
    await asyncio.to_thread(
        db.add_conversation_files,
        conversation_id,
        [
            {
                "file_id": str(record.get("file_id") or ""),
                "filename": str(record.get("filename") or record.get("file_id") or ""),
            }
            for record in records
        ],
        job["created_at"],
    )

    control.task = asyncio.create_task(_run_job(job_id, content, req.mode))
    # The callback receives only the Task, so the job id rides along on it.
    control.task._agent_job_id = job_id
    control.task.add_done_callback(_job_task_finished)
    return RunPipelineResponse(
        job_id=job_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )


# Media types for the formats the artifact writers actually produce. Anything
# not listed is served as an octet-stream download rather than being guessed
# at -- a wrong Content-Type on a generated file is how a spreadsheet ends up
# rendering as garbage text in a browser tab instead of downloading.
ARTIFACT_MEDIA_TYPES = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
}


@app.get("/jobs/{job_id}/artifacts/{artifact_path:path}")
def download_artifact(job_id: str, artifact_path: str):
    """Download one file this job created.

    Path handling is deliberately paranoid, because this is the one endpoint
    that turns a user-supplied string into a filesystem read:

      * the job must exist and must have a workspace;
      * the path is resolved through workspace._safe_child against the job's
        own ``artifacts/`` directory, which rejects absolute paths, ``..``
        traversal, and symlinks;
      * the artifacts directory is a sibling of ``sources/``, so even a
        successful traversal of one level could not reach an upload.

    Serving from ``artifacts/`` only -- never the workspace root -- is what
    keeps uploaded sources unreadable through this route.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    root = _resolve_workspace_root(job)
    if root:
        job["workspace_root"] = root
    if not root:
        raise HTTPException(status_code=404, detail="This job has no artifacts")

    artifacts_root = Path(root) / artifacts_module.ARTIFACTS_DIRNAME
    if not artifacts_root.is_dir():
        raise HTTPException(status_code=404, detail="This job has no artifacts")
    try:
        target = _safe_child(artifacts_root, artifact_path, must_exist=True)
    except (WorkspaceSecurityError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not a file")

    media_type = ARTIFACT_MEDIA_TYPES.get(
        target.suffix.lower(), "application/octet-stream"
    )
    return FileResponse(
        path=target,
        media_type=media_type,
        # inline for html/css/js so a generated web bundle can be opened and
        # can load its own siblings; attachment for everything else, which is
        # what "give me the file" means.
        headers={
            "Content-Disposition": (
                "inline"
                if target.suffix.lower() in {".html", ".css", ".js", ".json"}
                else _content_disposition(target.name)
            ),
            # Generated pages are untrusted output. Same-origin framing plus
            # a restrictive CSP keeps a bundle from reaching anything else.
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/uploads/{file_id}")
def download_upload(file_id: str):
    """Serve back a file the user attached, so the chat can link to it.

    The transcript shows an attachment by filename only, which is no help
    days later when the question is what was actually in it. This is the
    read side of /upload.

    No path is taken from the caller: ``file_id`` selects a stored record
    and the bytes come from that record's own ``path``. It is still
    checked against UPLOAD_DIR before being served, so a malformed or
    hand-edited metadata file cannot turn this into an arbitrary read.
    """
    record = _load_upload_record(file_id)
    target = Path(str(record.get("path") or ""))
    try:
        inside_uploads = target.resolve().parent == UPLOAD_DIR.resolve()
    except OSError:
        inside_uploads = False
    if not inside_uploads or not target.is_file():
        raise HTTPException(status_code=404, detail="Uploaded file is unavailable")

    filename = str(record.get("filename") or target.name)
    return FileResponse(
        path=target,
        media_type=ARTIFACT_MEDIA_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        ),
        # Always an attachment. Unlike generated artifacts, an upload is a
        # file of unknown provenance -- rendering one inline on this origin
        # is what makes a booby-trapped .html or .svg another site's
        # problem instead of nobody's.
        headers={
            "Content-Disposition": _content_disposition(filename),
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/jobs/{job_id}/artifact-bundles/{bundle_path:path}")
def download_artifact_bundle(job_id: str, bundle_path: str):
    """Download a multi-file artifact bundle (create_web_bundle /
    create_code_bundle) as one ZIP, instead of only its entry-point file.

    `bundle_path` is the bundle's folder name under this job's artifacts/
    directory (the same value scan_artifacts reports as an entry's `bundle`
    field, with the leading "artifacts/" stripped -- see _job_artifacts).
    It is NOT an arbitrary filesystem path: artifacts.resolve_bundle_for_zip
    only accepts a direct child of artifacts/ that carries a real
    .artifact-manifest.json, which the model cannot write directly, so a
    client cannot use this route to archive anything other than a bundle
    this job actually produced. Every member is re-verified against disk
    (existence, containment, not a symlink) before being zipped.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    root = _resolve_workspace_root(job)
    if root:
        job["workspace_root"] = root
    if not root:
        raise HTTPException(status_code=404, detail="This job has no artifacts")

    try:
        bundle = artifacts_module.resolve_bundle_for_zip(root, bundle_path)
    except (artifacts_module.ArtifactError, WorkspaceSecurityError) as exc:
        # The error text names only the bundle's own relative path/manifest
        # state -- never a real filesystem path -- so it is safe to surface.
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    payload = artifacts_module.build_bundle_zip(bundle)
    zip_filename = f"{bundle.folder_name}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": _content_disposition(zip_filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse)
def cancel_job(job_id: str):
    """Cooperatively cancel a job via Workforce's own stop_gracefully().

    Rehydrated jobs have no live ``_JobControl`` (see ``_rehydrate_from_db``).
    Stop on those must still succeed: they are already terminal after the
    startup interrupt sweep, and a 404 made the UI look as if the job was
    unknown rather than already finished.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")

    control = _JOB_CONTROLS.get(job_id)
    if control is None:
        return _job_snapshot(job_id)

    if job["status"] not in TERMINAL_STATUSES:
        control.cancelled = True
        job["cancellation_requested"] = True
        job["cancellation_note"] = CANCELLATION_NOTE
        if control.workforce is not None:
            control.workforce.stop_gracefully()
        job["status"] = "cancelled"
        for task_id, state in list(job["task_states"].items()):
            if state in {"queued", "running"}:
                job["task_states"][task_id] = "cancelled"
        db.persist_cancellation(job)
    return _job_snapshot(job_id)


# ---------------------------------------------------------------------------
# Ad-hoc code execution (frontend Run button)
# ---------------------------------------------------------------------------

class ExecuteCodeRequest(BaseModel):
    code: str
    language: str = "python"
    workspace_id: Optional[str] = None


class ExecuteCodeResponse(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    duration_seconds: Optional[float] = None
    files_written: list[str] = []


@app.post("/execute", response_model=ExecuteCodeResponse)
async def execute_code(req: ExecuteCodeRequest):
    """Run a code snippet in the workspace sandbox and return output.

    Used by the frontend's Run button on code blocks.  Only Python is
    supported; other languages return an error immediately.
    """
    if req.language != "python":
        return ExecuteCodeResponse(
            ok=False,
            error=f"Only Python execution is supported (got '{req.language}').",
        )
    workspace_id = req.workspace_id or str(uuid.uuid4())
    ws_root = WORKSPACE_DIR / workspace_id
    ws_root.mkdir(parents=True, exist_ok=True)

    from code_exec import CodeExecutionTools

    executor = CodeExecutionTools(ws_root)
    result = await asyncio.to_thread(executor.run_python, req.code)
    return ExecuteCodeResponse(
        ok=result.get("ok", False),
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        error=result.get("error"),
        duration_seconds=result.get("duration_seconds"),
        files_written=result.get("files_written", []),
    )


EXPORT_MEDIA_TYPES = {
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    "pdf": "application/pdf",
}


def _export_slug(topic: str, extension: str) -> str:
    """Mirror the frontend's safeFilename() so exports name consistently."""
    stem = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-")[:64]
    return f"{stem or 'agent-result'}.{extension}"


def _content_disposition(filename: str) -> str:
    """Build a Content-Disposition value that survives non-ASCII topics.

    A topic in any non-Latin script slugifies to an empty stem and falls
    back to 'agent-result', but a partially-ASCII one can still leave
    non-ASCII bytes in the name -- which is not legal in a bare filename
    parameter. Emit both the ASCII fallback and the RFC 5987 form.
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "agent-result"
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


@app.post("/jobs/{job_id}/export/{fmt}")
def export_job(job_id: str, fmt: Literal["docx", "pdf"], req: ExportRequest):
    """Render a completed job's final answer as a .docx or .pdf download.

    The caller supplies the exact final-answer text shown in the Results
    view rather than the backend re-deriving it -- see document_export for
    why (the selection logic lives in the frontend and would otherwise be
    duplicated and liable to drift).
    """
    if job_id not in _JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")

    content = (req.content or "").strip()
    if not content:
        raise HTTPException(
            status_code=400, detail="This job has no final answer to export."
        )

    title = (req.title or _JOBS[job_id].get("topic") or "Agent result").strip()
    builder = (
        document_export.build_docx if fmt == "docx" else document_export.build_pdf
    )
    try:
        payload = builder(title, content)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean 500
        logging.getLogger("agent_webapp.export").exception("Export failed")
        raise HTTPException(
            status_code=500, detail=f"Could not generate the {fmt} file."
        ) from exc

    return Response(
        content=payload,
        media_type=EXPORT_MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": _content_disposition(
                _export_slug(title, fmt)
            )
        },
    )


@app.get("/skills", response_model=SkillCatalogResponse)
def list_skills():
    """The built-in Skill catalog. Product-owned and read-only in v1 --
    there is deliberately no POST counterpart to this endpoint, unlike
    /workers: users select from this list, they cannot author instructions
    that would reach a system prompt."""
    return SkillCatalogResponse(
        skills=[SkillSummary(**entry) for entry in skills_catalog.catalog_payload()],
        max_active=skills_catalog.MAX_ACTIVE_SKILLS,
    )


@app.get("/workers", response_model=WorkerPoolResponse)
def list_workers():
    """The agents available to any Multi Agent run this session."""
    return _worker_pool()


@app.post("/workers", response_model=WorkerPoolResponse, status_code=201)
def add_worker(req: AddWorkerRequest):
    """Manually register an agent for the rest of this session.

    The agent is stored in-memory and registered onto every subsequent Multi
    Agent Workforce at build time -- see camel_service.add_manual_worker for
    why it can't be attached to an already-running Workforce.
    """
    try:
        camel_service.add_manual_worker(req.name, req.description or "")
    except camel_service.ManualWorkerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _worker_pool()


def _workspace_info(row: dict[str, Any]) -> WorkspaceInfo:
    return WorkspaceInfo(**row)


@app.post("/workspaces", response_model=WorkspaceInfo, status_code=201)
def create_workspace(req: CreateWorkspaceRequest):
    """Explicitly create a named workspace -- a secondary, optional UI
    affordance (e.g. a sidebar picker). Task submission never requires
    calling this first: /run-pipeline auto-creates one silently when no
    workspace_id is given."""
    workspace_id = str(uuid.uuid4())
    now = time.time()
    name = (req.name or "").strip() or None
    db.create_workspace(workspace_id, name, now)
    return _workspace_info(db.get_workspace(workspace_id))


@app.get("/workspaces", response_model=WorkspaceListResponse)
def list_workspaces():
    """Newest-active-first, for a sidebar picker."""
    return WorkspaceListResponse(
        workspaces=[_workspace_info(row) for row in db.list_workspaces()]
    )


@app.get("/workspaces/{workspace_id}", response_model=WorkspaceDetailResponse)
def get_workspace(workspace_id: str):
    row = db.get_workspace(workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown workspace_id: {workspace_id}")
    messages = list(
        reversed(db.load_recent_workspace_messages(workspace_id, limit=1000))
    )
    return WorkspaceDetailResponse(
        workspace=_workspace_info(row),
        messages=[ConversationMessage(**m) for m in messages],
    )


@app.patch("/workspaces/{workspace_id}", response_model=WorkspaceInfo)
def update_workspace(workspace_id: str, req: UpdateWorkspaceRequest):
    if req.name is None:
        raise HTTPException(status_code=400, detail="name is required")
    cleaned = req.name.strip()
    if not db.rename_workspace(workspace_id, cleaned or None):
        raise HTTPException(status_code=404, detail=f"Unknown workspace_id: {workspace_id}")
    row = db.get_workspace(workspace_id)
    assert row is not None
    return _workspace_info(row)


def _purge_jobs_from_memory(job_ids: list[str]) -> None:
    for jid in job_ids:
        _JOBS.pop(jid, None)
        _JOB_INDEX.pop(jid, None)
        _WORKER_ROLES.pop(jid, None)
        _TIMING.pop(jid, None)
        _JOB_CONTROLS.pop(jid, None)
        _ARTIFACT_CACHE.pop(jid, None)


@app.delete("/workspaces/{workspace_id}")
def delete_workspace_endpoint(workspace_id: str):
    """Delete a workspace, its conversations, jobs, and on-disk job folders."""
    job_ids = db.delete_workspace(workspace_id)
    if job_ids is None:
        raise HTTPException(status_code=404, detail=f"Unknown workspace_id: {workspace_id}")
    _purge_jobs_from_memory(job_ids)
    for job_id in job_ids:
        root = WORKSPACE_DIR / job_id
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)
    return {
        "deleted": True,
        "workspace_id": workspace_id,
        "jobs_removed": len(job_ids),
    }


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    """Delete a conversation and all its jobs from memory, DB, and disk."""
    job_ids = db.delete_conversation(conversation_id)
    if job_ids is None:
        raise HTTPException(status_code=404, detail=f"Unknown conversation: {conversation_id}")
    _purge_jobs_from_memory(job_ids)
    for job_id in job_ids:
        root = WORKSPACE_DIR / job_id
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)
    return {
        "deleted": True,
        "conversation_id": conversation_id,
        "jobs_removed": len(job_ids),
    }


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str):
    if job_id not in _JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return _job_snapshot(job_id)


@app.get("/jobs", response_model=list[JobStatusResponse])
def list_jobs():
    """Return newest jobs first for startup hydration and the recent list."""
    ordered = sorted(_JOBS.values(), key=lambda item: item["created_at"], reverse=True)
    return [_job_snapshot(job["job_id"]) for job in ordered]


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    """Server-Sent Events stream of live job snapshots."""
    if job_id not in _JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")

    control = _JOB_CONTROLS.get(job_id)
    if control is None:
        # Rehydrated from the DB: this process never ran it, so it's always
        # already terminal (see db.mark_incomplete_jobs_interrupted) -- there
        # is no live queue to subscribe to, just the one final snapshot.
        async def stream_once():
            yield _sse_pack(_event_snapshot(job_id))

        return StreamingResponse(
            stream_once(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    subscriber: "asyncio.Queue[dict | None]" = asyncio.Queue()
    control.subscribers.append(subscriber)

    async def stream():
        try:
            yield _sse_pack(_event_snapshot(job_id))
            if _JOBS[job_id]["status"] in TERMINAL_STATUSES:
                return
            while True:
                snapshot = await subscriber.get()
                if snapshot is None:
                    break
                yield _sse_pack(snapshot)
        finally:
            control.subscribers.remove(subscriber)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# Scheduled tasks (backend/scheduling/)
# --------------------------------------------------------------------------


async def _submit_scheduled_run(
    *,
    task: dict[str, Any],
    prompt: str,
    context_text: Optional[str],
    conversation_title: str,
    file_ids: Optional[list[str]] = None,
) -> SubmittedJob:
    response = await submit_job(
        RunPipelineRequest(
            topic=prompt,
            mode=task.get("mode") or "multi",
            deep_research=bool(task.get("deep_research")),
            workspace_id=task["workspace_id"],
            skill_ids=list(task.get("skill_ids") or []),
            context_text=context_text,
            file_ids=_existing_upload_ids(file_ids or []),
        ),
        conversation_title=conversation_title,
        origin="schedule",
    )
    return SubmittedJob(job_id=response.job_id, conversation_id=response.conversation_id)


def _existing_upload_ids(file_ids: list[str]) -> list[str]:
    """Drop uploads that have since disappeared, so one missing attachment
    does not reject the whole scheduled run with a 404."""
    kept: list[str] = []
    for file_id in file_ids:
        try:
            _load_upload_record(file_id)
        except HTTPException:
            continue
        kept.append(file_id)
    return kept


def _describe_upload(file_id: str) -> Optional[dict[str, Any]]:
    try:
        record = _load_upload_record(file_id)
    except HTTPException:
        return None
    return {"file_id": file_id, "filename": record.get("filename") or file_id}


async def _collect_job_files(job_id: str) -> tuple[list[str], list[str]]:
    """A finished job's artifacts, registered as uploads for a chained run.

    Going through _store_upload means the next job reads them exactly as it
    would a file a user attached: Docling parse, workspace context, and the
    template path for a .docx. Bundles (folders) are skipped -- a project
    directory is not a document to read.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return [], ["The upstream job is no longer available, so its files were not passed on."]
    file_ids: list[str] = []
    notes: list[str] = []
    total_bytes = 0
    root = _resolve_workspace_root(job)
    for entry in _job_artifacts(job):
        name = str(entry.get("filename") or "")
        if entry.get("bundle"):
            notes.append(f"Skipped folder {entry.get('bundle')}: folders are not passed on.")
            continue
        if len(file_ids) >= MAX_UPLOAD_SOURCES:
            notes.append(f"Skipped {name}: at most {MAX_UPLOAD_SOURCES} files are passed on.")
            continue
        size = int(entry.get("size_bytes") or 0)
        if total_bytes + size > MAX_BATCH_UPLOAD_BYTES:
            notes.append(f"Skipped {name}: files passed on are limited to {MAX_BATCH_UPLOAD_BYTES // (1024 * 1024)} MB in total.")
            continue
        try:
            source = _safe_child(Path(str(root)), str(entry["path"]), must_exist=True)
            content = await asyncio.to_thread(source.read_bytes)
            stored = await _store_upload(name, content)
        except HTTPException as exc:
            detail = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
            notes.append(f"Skipped {name}: {detail}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the chain
            notes.append(f"Skipped {name}: {type(exc).__name__}: {exc}")
            continue
        file_ids.append(stored.file_id)
        total_bytes += size
    return file_ids, notes


_RUN_FAILURE_TEXT = {
    "failed": "Görev bu çalıştırmada başarısız oldu.",
    "error": "Görev bu çalıştırmada hata verdi.",
    "interrupted": "Görev yarıda kesildi.",
    "no_change": "İzlenen sayfalarda değişiklik bulunmadı; rapor üretilmedi.",
}


async def _email_attachments(
    job: dict[str, Any], notes: list[str]
) -> list[tuple[str, bytes]]:
    """A job's artifacts as mail attachments, within the size cap.

    Skipped files are named in ``notes`` so the body says what is missing
    rather than the mail arriving quietly incomplete.
    """
    attachments: list[tuple[str, bytes]] = []
    attached_bytes = 0
    root = _resolve_workspace_root(job)
    if not root:
        return attachments
    for entry in _job_artifacts(job):
        name = str(entry.get("filename") or "")
        if entry.get("bundle") or not name:
            continue
        # Checked here, not only in send_mail, so the note reaches the body.
        if attached_bytes + int(entry.get("size_bytes") or 0) > outlook.MAX_ATTACHMENT_BYTES:
            limit_mb = outlook.MAX_ATTACHMENT_BYTES // (1024 * 1024)
            notes.append(f"{name} eklenmedi: ekler toplam {limit_mb} MB ile sınırlı; dosya uygulamada duruyor.")
            continue
        try:
            source = _safe_child(Path(root), str(entry["path"]), must_exist=True)
            content = await asyncio.to_thread(source.read_bytes)
        except Exception as exc:  # noqa: BLE001 - send the rest without this file
            notes.append(f"{name} eklenemedi: {exc}")
            continue
        attachments.append((name, content))
        attached_bytes += len(content)
    return attachments


def _prompt_delivery(job: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Recipients the user named in the request text itself.

    artifact_intent decides "they asked for this to be emailed" and keeps only
    addresses that appear verbatim in that text, so a poisoned web page or
    uploaded document cannot add a recipient.
    """
    usage = (job or {}).get("usage") or {}
    if not usage.get("sends_email"):
        return None
    recipients = [str(item).strip() for item in (usage.get("email_to") or []) if str(item).strip()]
    if not recipients:
        return None
    return {
        "to": recipients,
        "cc": [],
        "on": "success",
        "attach_files": True,
        "source": "prompt",
    }


def _resolve_task_delivery(
    task: dict[str, Any], run: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """The delivery setting in force for one scheduled run.

    The task's own Email setting wins; otherwise the task prompt may ask for
    it ("... ve ali@sirket.com'a gonder"), which is how a task emails without
    anyone ticking the box. Injected into the Scheduler as resolve_delivery.
    """
    setting = task.get("delivery")
    if setting and setting.get("to"):
        return {**setting, "source": "task"}
    job = _JOBS.get(run["job_id"]) if run.get("job_id") else None
    return _prompt_delivery(job)


async def _deliver_scheduled_run(task: dict[str, Any], run: dict[str, Any]) -> DeliveryOutcome:
    """Email one settled run's result to that run's recipients.

    Recipients come from the task's delivery setting or from the task prompt
    itself -- in both cases text a person wrote. Nothing the run read on the
    way (web pages, uploads, an upstream report) can add one.
    """
    setting = _resolve_task_delivery(task, run) or {}
    if not setting.get("to"):
        return DeliveryOutcome(False, "This run has no email recipients.", retryable=False)
    job = _JOBS.get(run["job_id"]) if run.get("job_id") else None
    if run.get("job_id") and job is None:
        return DeliveryOutcome(False, "The run's job is no longer available.", retryable=False)

    timezone = task.get("timezone") or scheduling_config.default_timezone()
    when = datetime.fromtimestamp(float(run["scheduled_for"]), scheduling_cron.zone(timezone))
    title = f"{task['name']} — {scheduling_prompts.format_date(when)}"
    status = str(run.get("status") or "")
    result = str((job or {}).get("result") or "").strip()

    notes: list[str] = []
    attachments: list[tuple[str, bytes]] = []
    intro: Optional[str] = None
    if result and status in {"done", "partial", "no_change"}:
        body = result
        if run.get("change_detected") and run.get("change_summary"):
            intro = f"Değişiklik: {run['change_summary']}"
        if status == "partial":
            notes.append("Bu çalıştırma kısmen tamamlandı; sonuç eksik olabilir.")
        if setting.get("attach_files", True) and job is not None:
            attachments = await _email_attachments(job, notes)
    else:
        detail = run.get("error") or (job or {}).get("error")
        body = _RUN_FAILURE_TEXT.get(status, f"Görev tamamlanmadı ({status}).")
        if detail:
            body += f"\n\n{detail}"
    if setting.get("source") == "prompt":
        notes.append("Alıcılar görev metninde yazdığı için bu adrese gönderildi.")
    notes.append(f"Bu e-posta \"{task['name']}\" zamanlanmış görevi tarafından otomatik gönderildi.")

    html_body = outlook.render_email_html(title, body, intro=intro, notes=notes)
    result_send = await asyncio.to_thread(
        outlook.send_mail,
        to=setting.get("to") or [],
        cc=setting.get("cc") or [],
        subject=title,
        html_body=html_body,
        attachments=attachments,
    )
    return DeliveryOutcome(
        ok=result_send.ok,
        error=None if result_send.ok else result_send.message,
        retryable=result_send.retryable,
        notes=result_send.notes,
    )


_SCHEDULER = Scheduler(
    submit=_submit_scheduled_run,
    job_state=_JOBS.get,
    collect_files=_collect_job_files,
    deliver=_deliver_scheduled_run,
    resolve_delivery=_resolve_task_delivery,
)
app.include_router(
    build_scheduling_router(lambda: _SCHEDULER, describe_upload=_describe_upload)
)
app.include_router(outlook.build_router())
app.include_router(social_api.build_router())


@app.on_event("startup")
async def _start_scheduler() -> None:
    # Registered after _rehydrate_from_db, so migrations have run and
    # interrupted jobs are already loaded when the first tick settles runs.
    if scheduling_config.enabled() and scheduling_config.autostart():
        _SCHEDULER.start()


@app.on_event("shutdown")
async def _stop_scheduler() -> None:
    await _SCHEDULER.stop()
