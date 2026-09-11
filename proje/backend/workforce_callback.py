"""Bridges CAMEL Workforce's own lifecycle events into an asyncio.Queue.

This implements CAMEL's built-in ``WorkforceCallback`` interface (see
``camel.societies.workforce.workforce_callback``). Lifecycle payloads are
direct ``model_dump`` results from CAMEL. The one application event accepted
in addition is ``ActivityEvent``: a factual tool-execution status such as
searching/opening/building, never model reasoning. main.py drains the same
queue for both and streams snapshots to the frontend over SSE.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from camel.societies.workforce.events import (
    AllTasksCompletedEvent,
    TaskAssignedEvent,
    TaskCompletedEvent,
    TaskCreatedEvent,
    TaskDecomposedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
    WorkerCreatedEvent,
    WorkerDeletedEvent,
)
from camel.societies.workforce.workforce_callback import WorkforceCallback


class QueueWorkforceCallback(WorkforceCallback):
    """Pushes every real CAMEL workforce event onto an asyncio.Queue.

    Multi Agent jobs now run their whole Workforce on a dedicated thread
    with its own event loop (see camel_service.run_workforce_in_thread) so
    CAMEL's internal synchronous coordinator calls can't block the main
    FastAPI event loop. That means these callback methods can fire from a
    thread other than the one that owns ``queue``, and asyncio.Queue is not
    thread-safe across loops -- calling put_nowait() directly from the
    "wrong" thread could corrupt its internal state. ``loop`` is the queue's
    owning event loop (captured before the job's thread is spawned); each
    emit is marshaled onto it via call_soon_threadsafe, which is documented
    as safe to call from any thread, including the loop's own.
    """

    def __init__(
        self,
        queue: "asyncio.Queue[Dict[str, Any] | None]",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._queue = queue
        self._loop = loop

    def _emit(self, event) -> None:
        payload = event.model_dump(mode="json")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def log_task_created(self, event: TaskCreatedEvent) -> None:
        self._emit(event)

    def log_task_decomposed(self, event: TaskDecomposedEvent) -> None:
        self._emit(event)

    def log_task_assigned(self, event: TaskAssignedEvent) -> None:
        self._emit(event)

    def log_task_started(self, event: TaskStartedEvent) -> None:
        self._emit(event)

    def log_task_completed(self, event: TaskCompletedEvent) -> None:
        self._emit(event)

    def log_task_failed(self, event: TaskFailedEvent) -> None:
        self._emit(event)

    def log_worker_created(self, event: WorkerCreatedEvent) -> None:
        self._emit(event)

    def log_worker_deleted(self, event: WorkerDeletedEvent) -> None:
        self._emit(event)

    def log_all_tasks_completed(self, event: AllTasksCompletedEvent) -> None:
        self._emit(event)

    def log_activity(self, event: Any) -> None:
        """Bridge an application ActivityEvent from the Workforce thread."""
        self._emit(event)
