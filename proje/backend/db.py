"""SQLite persistence: jobs, per-task/tool-call detail, and manually-added
agents survive a backend restart. Everything else (live CAMEL `workforce`
objects, SSE subscriber queues, in-flight cancellation flags) stays
process-memory-only -- see main._JobControl -- because there is nothing to
rehydrate a live agent run from a database row. A job left `queued`/
`running` when the process dies is swept to `interrupted` at the next
startup (see mark_incomplete_jobs_interrupted) rather than resumed.

Concurrency: sqlite3 connections are opened fresh per call (cheap) with WAL
journaling so readers don't block a writer; a single process-wide lock
serializes writes since this app has no need for finer-grained locking at
its scale. Every call here is blocking and must be run via
asyncio.to_thread from async code.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

# DATA_DIR defaults to this file's own directory, so bare-metal/local dev
# behavior is unchanged. In Docker, DATA_DIR=/data points this at a mounted
# volume instead -- required because the DB otherwise lives next to the
# source code the image COPYs in, which a volume mount would shadow. See
# tool_call_log.py and main.py for the other persistent paths (uploads,
# workspaces, logs) that share this same directory for the same reason.
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_migrations() -> None:
    conn = _connect()
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.stem.split("_")[0])
            if version <= current:
                continue
            with _write_lock:
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def save_job(job: dict[str, Any]) -> None:
    """Insert a newly-created job row (status is normally 'queued')."""
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, workspace_id, conversation_id, topic, status, mode, planning_fallback,
                planning_errors_json, result, error, created_at,
                duration_seconds, model_provider, model_name,
                attachments_json, workspace_json, usage_json,
                cancellation_requested, cancellation_note, skills_json,
                deep_research
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["job_id"],
                job.get("workspace_id"),
                job.get("conversation_id"),
                job["topic"],
                job["status"],
                job["mode"],
                int(job.get("planning_fallback", False)),
                json.dumps(job.get("planning_errors", [])),
                job.get("result"),
                job.get("error"),
                job["created_at"],
                job.get("duration_seconds"),
                job.get("model_provider"),
                job.get("model_name"),
                json.dumps(job.get("attachments", [])),
                json.dumps(job.get("workspace")) if job.get("workspace") else None,
                json.dumps(job.get("usage", {})),
                int(job.get("cancellation_requested", False)),
                job.get("cancellation_note"),
                # Selected Skills are fixed at submission and never change
                # for the life of the run, so they are written once here and
                # never touched by finalize_job.
                json.dumps(job.get("skills", [])),
                # Fixed at submission like skills above, never revised by
                # finalize_job.
                int(bool(job.get("deep_research", False))),
            ),
        )


def update_job_status(job_id: str, status: str) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))


def persist_cancellation(job: dict[str, Any]) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, cancellation_requested = ?, cancellation_note = ?
            WHERE job_id = ?
            """,
            (
                job["status"],
                int(job.get("cancellation_requested", False)),
                job.get("cancellation_note"),
                job["job_id"],
            ),
        )
        for task_id, status in job.get("task_states", {}).items():
            conn.execute(
                "UPDATE job_tasks SET status = ? WHERE job_id = ? AND task_id = ?",
                (status, job["job_id"], task_id),
            )


def upsert_job_task(
    job_id: str,
    task_id: str,
    title: str,
    description: str,
    assigned_agent: Optional[str],
    dependencies: list[str],
    status: str,
    output: Optional[str],
) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO job_tasks (
                job_id, task_id, title, description, assigned_agent,
                dependencies_json, status, output
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, task_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                assigned_agent = excluded.assigned_agent,
                dependencies_json = excluded.dependencies_json,
                status = excluded.status,
                output = excluded.output
            """,
            (
                job_id,
                task_id,
                title,
                description,
                assigned_agent,
                json.dumps(dependencies),
                status,
                output,
            ),
        )


def finalize_job(
    job: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> None:
    """Persist a job's terminal state: final status/result/usage plus its
    complete tool-call log. Called once, from _run_job's finally block."""
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, result = ?, error = ?, duration_seconds = ?,
                usage_json = ?, cancellation_requested = ?, cancellation_note = ?
            WHERE job_id = ?
            """,
            (
                job["status"],
                job.get("result"),
                job.get("error"),
                job.get("duration_seconds"),
                json.dumps(job.get("usage", {})),
                int(job.get("cancellation_requested", False)),
                job.get("cancellation_note"),
                job["job_id"],
            ),
        )
        if tool_calls:
            conn.executemany(
                """
                INSERT INTO job_tool_calls (job_id, timestamp, tool_name, args_json, result_json, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        job["job_id"],
                        call["timestamp"],
                        call["tool_name"],
                        json.dumps(call.get("args", {})),
                        json.dumps(call["result"]) if call.get("result") is not None else None,
                        call.get("error"),
                    )
                    for call in tool_calls
                ],
            )


def mark_incomplete_jobs_interrupted() -> list[str]:
    """Sweep any job left 'queued'/'running' from a prior process that died
    mid-run. Returns the affected job_ids. Must run once at startup, before
    any new job is accepted, since there is no live workforce to resume."""
    note = "Server restarted while this job was in progress; it did not complete."
    with _write_lock, _connect() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('queued', 'running')"
        ).fetchall()
        job_ids = [row["job_id"] for row in rows]
        if job_ids:
            conn.executemany(
                "UPDATE jobs SET status = 'interrupted', error = ? WHERE job_id = ?",
                [(note, job_id) for job_id in job_ids],
            )
            conn.executemany(
                """
                UPDATE job_tasks SET status = 'cancelled'
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                [(job_id,) for job_id in job_ids],
            )
        return job_ids


def load_all_jobs() -> list[dict[str, Any]]:
    """Full rehydration for startup: every job plus its tasks and tool
    calls, shaped to match the in-memory job dict main.py already builds."""
    conn = _connect()
    try:
        job_rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        jobs: list[dict[str, Any]] = []
        for row in job_rows:
            job_id = row["job_id"]
            task_rows = conn.execute(
                "SELECT * FROM job_tasks WHERE job_id = ?", (job_id,)
            ).fetchall()
            tool_call_rows = conn.execute(
                "SELECT * FROM job_tool_calls WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()

            task_plan = [
                {
                    "id": t["task_id"],
                    "title": t["title"],
                    "description": t["description"],
                    "assigned_agent": t["assigned_agent"],
                    "dependencies": json.loads(t["dependencies_json"]),
                }
                for t in task_rows
            ]
            jobs.append(
                {
                    "job_id": job_id,
                    "workspace_id": row["workspace_id"],
                    "conversation_id": row["conversation_id"],
                    "topic": row["topic"],
                    "status": row["status"],
                    "mode": row["mode"],
                    "task_plan": task_plan,
                    "planning_fallback": bool(row["planning_fallback"]),
                    "planning_errors": json.loads(row["planning_errors_json"]),
                    "task_states": {t["task_id"]: t["status"] for t in task_rows},
                    "task_outputs": {
                        t["task_id"]: t["output"] for t in task_rows if t["output"]
                    },
                    "result": row["result"],
                    "error": row["error"],
                    "created_at": row["created_at"],
                    "duration_seconds": row["duration_seconds"],
                    "model_provider": row["model_provider"],
                    "model_name": row["model_name"],
                    "attachments": json.loads(row["attachments_json"]),
                    "workspace": json.loads(row["workspace_json"]) if row["workspace_json"] else None,
                    "usage": json.loads(row["usage_json"]),
                    "cancellation_requested": bool(row["cancellation_requested"]),
                    "cancellation_note": row["cancellation_note"],
                    "skills": json.loads(row["skills_json"] or "[]"),
                    "deep_research": bool(row["deep_research"]),
                    "tool_calls": [
                        {
                            "timestamp": t["timestamp"],
                            "tool_name": t["tool_name"],
                            "args": json.loads(t["args_json"]),
                            "result": json.loads(t["result_json"]) if t["result_json"] is not None else None,
                            "error": t["error"],
                        }
                        for t in tool_call_rows
                    ],
                }
            )
        return jobs
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Manually-added agents
# --------------------------------------------------------------------------


def insert_manual_worker(record: dict[str, str], created_at: float) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_workers (name, description, description_provided, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                record["name"],
                record["description"],
                int(record["description_provided"] == "true"),
                created_at,
            ),
        )


def load_manual_workers() -> list[dict[str, str]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, description, description_provided FROM manual_workers ORDER BY id"
        ).fetchall()
        return [
            {
                "name": row["name"],
                "description": row["description"],
                "description_provided": "true" if row["description_provided"] else "false",
            }
            for row in rows
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Workspaces + conversations + message history
# --------------------------------------------------------------------------


def create_workspace(workspace_id: str, name: Optional[str], created_at: float) -> None:
    """Idempotent: a client can retry/replay a stale workspace_id (e.g. from
    localStorage after a DB reset) without a 409 -- see the UX guardrail that
    workspace creation must never block task submission."""
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (workspace_id, name, created_at, last_active_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO NOTHING
            """,
            (workspace_id, name, created_at, created_at),
        )


def touch_workspace(workspace_id: str, ts: float) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            "UPDATE workspaces SET last_active_at = ? WHERE workspace_id = ?",
            (ts, workspace_id),
        )


def workspace_exists(workspace_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_workspace(workspace_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT workspace_id, name, created_at, last_active_at FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_workspaces() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT workspace_id, name, created_at, last_active_at FROM workspaces ORDER BY last_active_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def rename_workspace(workspace_id: str, name: str) -> bool:
    with _write_lock, _connect() as conn:
        cursor = conn.execute(
            "UPDATE workspaces SET name = ? WHERE workspace_id = ?", (name, workspace_id)
        )
        return cursor.rowcount > 0


def delete_workspace(workspace_id: str) -> Optional[list[str]]:
    """Delete a workspace and its jobs/conversations/messages.

    Returns the deleted job ids when the workspace existed, otherwise None.
    Jobs have no FK to workspaces, so they must be removed explicitly.
    """
    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT workspace_id FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return None
        job_rows = conn.execute(
            "SELECT job_id FROM jobs WHERE workspace_id = ?", (workspace_id,)
        ).fetchall()
        job_ids = [str(item["job_id"]) for item in job_rows]
        for job_id in job_ids:
            conn.execute("DELETE FROM job_tool_calls WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM job_tasks WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE workspace_id = ?", (workspace_id,))
        conn.execute("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))
        return job_ids


def create_conversation(
    conversation_id: str,
    workspace_id: str,
    title: Optional[str],
    created_at: float,
) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                conversation_id, workspace_id, title, created_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, workspace_id, title, created_at, created_at),
        )


def get_conversation(conversation_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT conversation_id, workspace_id, title, created_at, last_active_at,
                   summary, summarized_through_created_at
            FROM conversations WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def workspace_roots_for_conversation(conversation_id: str) -> list[str]:
    """Every past job's on-disk workspace directory in this conversation,
    most recent first.

    Each job gets its OWN, freshly-created workspace directory keyed by
    job_id -- never shared across jobs even within one conversation (see
    main.py's ensure_job_workspace/build_job_workspace and
    _resolve_workspace_root's "WORKSPACE_DIR / job_id" convention, mirrored
    here via this module's own DATA_DIR since both resolve the same
    DATA_DIR env var). So a follow-up like "open the file I just made"
    starts with an empty workspace of its own and must look back at prior
    jobs in the conversation to find what they produced. See
    local_computer.py."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE conversation_id = ? ORDER BY created_at DESC",
            (conversation_id,),
        ).fetchall()
    finally:
        conn.close()
    workspaces_dir = DATA_DIR / "workspaces"
    roots: list[str] = []
    for row in rows:
        candidate = workspaces_dir / str(row["job_id"])
        if candidate.is_dir():
            roots.append(str(candidate))
    return roots


def save_conversation_summary(
    conversation_id: str, summary: str, summarized_through_created_at: float
) -> None:
    """Persist the running summary of a conversation's older turns.

    summarized_through_created_at is the created_at of the newest message
    already folded into `summary`, so the next summarization pass knows
    where to resume (see main._build_conversation_context).
    """
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            UPDATE conversations
            SET summary = ?, summarized_through_created_at = ?
            WHERE conversation_id = ?
            """,
            (summary, summarized_through_created_at, conversation_id),
        )


def delete_conversation(conversation_id: str) -> Optional[list[str]]:
    """Delete a conversation, its messages/files, and its jobs.

    Returns the deleted job ids when the conversation existed, otherwise None.
    Jobs must be removed explicitly: their FK only SET NULLs conversation_id,
    and the sidebar lists conversations by grouping jobs -- orphaned jobs
    would reappear after refresh.
    """
    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        job_rows = conn.execute(
            "SELECT job_id FROM jobs WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        job_ids = [str(item["job_id"]) for item in job_rows]
        for job_id in job_ids:
            conn.execute("DELETE FROM job_tool_calls WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM job_tasks WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversation_files WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
        return job_ids


def touch_conversation(conversation_id: str, ts: float) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            "UPDATE conversations SET last_active_at = ? WHERE conversation_id = ?",
            (ts, conversation_id),
        )


# Keep in lockstep with workspace.MAX_UPLOAD_SOURCES. Follow-up turns re-bind
# from this bag; dropping the oldest files is how a long thread stays bounded.
CONVERSATION_FILE_BAG_MAX = 10


def list_conversation_files(conversation_id: str) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT file_id, filename, added_at
            FROM conversation_files
            WHERE conversation_id = ?
            ORDER BY added_at ASC
            """,
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def add_conversation_files(
    conversation_id: str,
    files: list[dict[str, Any]],
    added_at: float,
) -> None:
    """Idempotent insert; drops the oldest files when the bag is full."""
    if not files:
        return
    with _write_lock, _connect() as conn:
        for item in files:
            file_id = str(item.get("file_id") or "").strip()
            filename = str(item.get("filename") or "").strip() or file_id
            if not file_id:
                continue
            conn.execute(
                """
                INSERT INTO conversation_files (
                    conversation_id, file_id, filename, added_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id, file_id) DO NOTHING
                """,
                (conversation_id, file_id, filename, added_at),
            )
        extra = conn.execute(
            """
            SELECT file_id FROM conversation_files
            WHERE conversation_id = ?
            ORDER BY added_at ASC
            """,
            (conversation_id,),
        ).fetchall()
        overflow = len(extra) - CONVERSATION_FILE_BAG_MAX
        if overflow > 0:
            drop = [row["file_id"] for row in extra[:overflow]]
            conn.executemany(
                """
                DELETE FROM conversation_files
                WHERE conversation_id = ? AND file_id = ?
                """,
                [(conversation_id, file_id) for file_id in drop],
            )


def insert_message(
    workspace_id: str,
    conversation_id: str,
    job_id: Optional[str],
    role: str,
    content: str,
    created_at: float,
) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (
                workspace_id, conversation_id, job_id, role, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workspace_id, conversation_id, job_id, role, content, created_at),
        )


def load_recent_messages(conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first, capped at `limit` rows -- a generous ceiling so the
    caller's char-budget truncation (not this query) is what actually bounds
    context size; this just stops a pathologically long-lived workspace from
    pulling its entire history into memory before truncation gets a chance."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT job_id, role, content, created_at FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def load_recent_workspace_messages(
    workspace_id: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Compatibility read for the existing workspace-detail endpoint.

    Runtime prompt context must use ``load_recent_messages`` so sibling
    conversations never bleed together.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT job_id, role, content, created_at FROM messages
            WHERE workspace_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (workspace_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
