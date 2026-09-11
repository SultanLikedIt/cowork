"""SSE lifecycle events for LangGraph paths (Camel-free dicts).

Shapes match CAMEL WorkforceEventBase + workforce.events.TokenDeltaEvent so
``main._apply_event`` needs no special cases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_created(*, worker_id: str, role: str, worker_type: str = "single_agent") -> dict[str, Any]:
    return {
        "event_type": "worker_created",
        "worker_id": worker_id,
        "worker_type": worker_type,
        "role": role,
        "timestamp": _ts(),
    }


def task_created(*, task_id: str, description: str) -> dict[str, Any]:
    return {
        "event_type": "task_created",
        "task_id": task_id,
        "description": description,
        "timestamp": _ts(),
    }


def task_assigned(*, task_id: str, worker_id: str) -> dict[str, Any]:
    return {
        "event_type": "task_assigned",
        "task_id": task_id,
        "worker_id": worker_id,
        "dependencies": [],
        "timestamp": _ts(),
    }


def task_started(*, task_id: str, worker_id: str) -> dict[str, Any]:
    return {
        "event_type": "task_started",
        "task_id": task_id,
        "worker_id": worker_id,
        "timestamp": _ts(),
    }


def task_completed(
    *,
    task_id: str,
    worker_id: str,
    result_summary: str,
    processing_time_seconds: float,
) -> dict[str, Any]:
    return {
        "event_type": "task_completed",
        "task_id": task_id,
        "worker_id": worker_id,
        "result_summary": result_summary,
        "processing_time_seconds": processing_time_seconds,
        "timestamp": _ts(),
    }


def token_delta(*, task_id: str, delta: str) -> dict[str, Any]:
    return {
        "event_type": "token_delta",
        "task_id": task_id,
        "delta": delta,
        "timestamp": _ts(),
    }
