"""SQLite access for scheduled tasks and their runs.

Same conventions as db.py -- fresh connection per call, the process-wide
write lock for writes, blocking calls meant for asyncio.to_thread.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Optional

import db

TASK_JSON_FIELDS = {
    "skill_ids_json": "skill_ids",
    "watch_json": "watch",
    "watch_state_json": "watch_state",
}
TASK_BOOL_FIELDS = ("deep_research", "include_previous_summary", "enabled")
UPDATABLE_TASK_COLUMNS = frozenset(
    {
        "name",
        "prompt",
        "mode",
        "deep_research",
        "skill_ids_json",
        "trigger_kind",
        "cron_expr",
        "run_at",
        "timezone",
        "watch_json",
        "watch_state_json",
        "include_previous_summary",
        "enabled",
        "paused_reason",
        "next_run_at",
        "last_run_at",
        "last_job_id",
        "last_status",
        "last_result_summary",
        "consecutive_failures",
        "updated_at",
    }
)
UPDATABLE_RUN_COLUMNS = frozenset(
    {
        "job_id",
        "conversation_id",
        "started_at",
        "finished_at",
        "status",
        "change_detected",
        "change_summary",
        "error",
        "seen_at",
    }
)
TERMINAL_RUN_STATUSES = frozenset(
    {
        "done",
        "partial",
        "failed",
        "error",
        "cancelled",
        "interrupted",
        "skipped_overlap",
        "missed",
        "no_change",
    }
)


def _task_from_row(row: Any) -> dict[str, Any]:
    task = dict(row)
    for column, key in TASK_JSON_FIELDS.items():
        raw = task.pop(column, None)
        default: Any = [] if key == "skill_ids" else ({} if key == "watch_state" else None)
        task[key] = json.loads(raw) if raw else default
    for key in TASK_BOOL_FIELDS:
        task[key] = bool(task.get(key))
    return task


def _run_from_row(row: Any) -> dict[str, Any]:
    run = dict(row)
    if run.get("change_detected") is not None:
        run["change_detected"] = bool(run["change_detected"])
    return run


def create_task(record: dict[str, Any]) -> None:
    columns = [
        "task_id",
        "workspace_id",
        "name",
        "prompt",
        "mode",
        "deep_research",
        "skill_ids_json",
        "trigger_kind",
        "cron_expr",
        "run_at",
        "timezone",
        "watch_json",
        "include_previous_summary",
        "enabled",
        "next_run_at",
        "created_at",
        "updated_at",
    ]
    values = [
        record["task_id"],
        record["workspace_id"],
        record["name"],
        record["prompt"],
        record.get("mode") or "multi",
        int(bool(record.get("deep_research"))),
        json.dumps(record.get("skill_ids") or []),
        record["trigger_kind"],
        record.get("cron_expr"),
        record.get("run_at"),
        record["timezone"],
        json.dumps(record["watch"], ensure_ascii=False) if record.get("watch") else None,
        int(bool(record.get("include_previous_summary", True))),
        int(bool(record.get("enabled", True))),
        record.get("next_run_at"),
        record["created_at"],
        record["updated_at"],
    ]
    placeholders = ", ".join("?" for _ in columns)
    with db._write_lock, db._connect() as conn:
        conn.execute(
            f"INSERT INTO scheduled_tasks ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _task_from_row(row) if row else None
    finally:
        conn.close()


def list_tasks(workspace_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Tasks newest first, each with ``unseen_runs`` and ``running``."""
    conn = db._connect()
    try:
        query = """
            SELECT t.*,
                (SELECT COUNT(*) FROM scheduled_runs r
                  WHERE r.task_id = t.task_id AND r.seen_at IS NULL
                    AND r.job_id IS NOT NULL
                    AND r.status IN ('done', 'partial', 'failed', 'error')) AS unseen_runs,
                (SELECT COUNT(*) FROM scheduled_runs r
                  WHERE r.task_id = t.task_id AND r.status = 'running') AS running_runs
            FROM scheduled_tasks t
        """
        params: tuple[Any, ...] = ()
        if workspace_id:
            query += " WHERE t.workspace_id = ?"
            params = (workspace_id,)
        query += " ORDER BY t.created_at DESC"
        tasks = []
        for row in conn.execute(query, params).fetchall():
            task = _task_from_row(row)
            task["unseen_runs"] = int(task.pop("unseen_runs") or 0)
            task["running"] = bool(task.pop("running_runs"))
            tasks.append(task)
        return tasks
    finally:
        conn.close()


def count_tasks(workspace_id: str) -> int:
    conn = db._connect()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM scheduled_tasks WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def update_task(task_id: str, fields: dict[str, Any]) -> bool:
    unknown = set(fields) - UPDATABLE_TASK_COLUMNS
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if not fields:
        return get_task(task_id) is not None
    assignments = ", ".join(f"{column} = ?" for column in fields)
    with db._write_lock, db._connect() as conn:
        cursor = conn.execute(
            f"UPDATE scheduled_tasks SET {assignments} WHERE task_id = ?",
            [*fields.values(), task_id],
        )
        return cursor.rowcount > 0


def delete_task(task_id: str) -> bool:
    with db._write_lock, db._connect() as conn:
        conn.execute("DELETE FROM scheduled_runs WHERE task_id = ?", (task_id,))
        cursor = conn.execute("DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,))
        return cursor.rowcount > 0


def claim_due(
    now: float,
    limit: int,
    advance: Callable[[dict[str, Any], float], Optional[float]],
) -> list[tuple[dict[str, Any], float]]:
    """Take up to ``limit`` due tasks, moving each one's next_run_at forward.

    Returns ``(task, fire_time)`` pairs. The move is a compare-and-set on the
    old next_run_at, so a task is claimed by exactly one caller even if two
    schedulers ever tick at once. ``advance(task, now)`` returns the new
    next_run_at (None when the task has no future run).
    """
    if limit <= 0:
        return []
    conn = db._connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    finally:
        conn.close()

    claimed: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        task = _task_from_row(row)
        fire_time = float(task["next_run_at"])
        new_next = advance(task, now)
        with db._write_lock, db._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks SET next_run_at = ?, updated_at = ?
                WHERE task_id = ? AND enabled = 1 AND next_run_at = ?
                """,
                (new_next, now, task["task_id"], fire_time),
            )
        if cursor.rowcount == 1:
            task["next_run_at"] = new_next
            claimed.append((task, fire_time))
    return claimed


def insert_run(record: dict[str, Any]) -> None:
    columns = [
        "run_id",
        "task_id",
        "job_id",
        "conversation_id",
        "trigger",
        "scheduled_for",
        "started_at",
        "finished_at",
        "status",
        "change_detected",
        "change_summary",
        "retry_of",
        "error",
    ]
    values = [record.get(column) for column in columns]
    if values[columns.index("change_detected")] is not None:
        values[columns.index("change_detected")] = int(bool(record["change_detected"]))
    with db._write_lock, db._connect() as conn:
        conn.execute(
            f"INSERT INTO scheduled_runs ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )


def update_run(run_id: str, fields: dict[str, Any]) -> None:
    unknown = set(fields) - UPDATABLE_RUN_COLUMNS
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if not fields:
        return
    values = [
        int(bool(value)) if column == "change_detected" and value is not None else value
        for column, value in fields.items()
    ]
    assignments = ", ".join(f"{column} = ?" for column in fields)
    with db._write_lock, db._connect() as conn:
        conn.execute(
            f"UPDATE scheduled_runs SET {assignments} WHERE run_id = ?",
            [*values, run_id],
        )


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _run_from_row(row) if row else None
    finally:
        conn.close()


def list_runs(task_id: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = db._connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_runs WHERE task_id = ?
            ORDER BY scheduled_for DESC, COALESCE(started_at, 0) DESC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        return [_run_from_row(row) for row in rows]
    finally:
        conn.close()


def running_runs(task_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scheduled_runs WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
        runs = [_run_from_row(row) for row in rows]
    finally:
        conn.close()
    if task_ids is None:
        return runs
    wanted = set(task_ids)
    return [run for run in runs if run["task_id"] in wanted]


def has_retry(run_id: str) -> bool:
    conn = db._connect()
    try:
        return (
            conn.execute(
                "SELECT 1 FROM scheduled_runs WHERE retry_of = ? LIMIT 1", (run_id,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def mark_runs_seen(task_id: str, now: float) -> int:
    with db._write_lock, db._connect() as conn:
        cursor = conn.execute(
            "UPDATE scheduled_runs SET seen_at = ? WHERE task_id = ? AND seen_at IS NULL",
            (now, task_id),
        )
        return cursor.rowcount


def run_for_job(job_id: str) -> Optional[dict[str, Any]]:
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_runs WHERE job_id = ? LIMIT 1", (job_id,)
        ).fetchone()
        return _run_from_row(row) if row else None
    finally:
        conn.close()
