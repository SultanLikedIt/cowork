"""Bridges Workforce events from the job's thread into an asyncio.Queue.

The Workforce runs on its own thread with its own event loop (see
execution/langgraph_runtime/workforce.run_workforce_in_thread), so its events
are produced on a thread other than the one that owns ``queue``. asyncio.Queue
is not thread-safe across loops; every emit is marshaled onto the queue's
owning loop with call_soon_threadsafe, which is safe from any thread.

The events are plain dicts (execution/langgraph_runtime/events.py) plus
``ActivityEvent`` dumps: factual tool-execution statuses such as
searching/opening/building, never model reasoning. main.py drains the queue
and streams snapshots to the frontend over SSE.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict


class QueueWorkforceCallback:
    """The queue plus its owning loop; the Workforce's ``_emit`` reads both."""

    def __init__(
        self,
        queue: "asyncio.Queue[Dict[str, Any] | None]",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._queue = queue
        self._loop = loop
