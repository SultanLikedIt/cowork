"""Ground-truth tool-call logging, separate from CAMEL's task-level events.

CAMEL's WorkforceCallback (see workforce_callback.py) only bridges
task/worker lifecycle events -- it has no per-tool-call detail, so there is
currently no way to verify whether a worker's tool actually ran, what was
sent to it, or what it actually returned. An agent's response text can
describe search results that were never actually fetched. This module wraps
a tool function so every real invocation is recorded with its raw
arguments and raw return value (or exception), independent of whatever the
agent's own response claims.

Records are kept both in-memory (attached to the job, via ToolCallRecorder)
and appended to a JSONL log file on disk, so they survive past the job's
lifetime and can be grepped/tailed directly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

# See db.py's DATA_DIR comment -- same reasoning, same env var, so both
# land under one mounted volume in Docker instead of two.
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "tool_calls.jsonl"
_FILE_APPEND_LOCK = threading.Lock()


class ToolCallRecorder:
    """Accumulates real tool-call records for one job."""

    def __init__(self, job_id: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self.job_id = job_id
        self.calls: list[dict[str, Any]] = []

    def record(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any = None,
        error: Optional[str] = None,
        *,
        duration_ms: Optional[float] = None,
        started_at: Optional[str] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": self.job_id,
            "tool_name": tool_name,
            "args": args,
            "result": result,
            "error": error,
            "started_at": started_at,
            "duration_ms": duration_ms,
        }
        with self._lock:
            self.calls.append(entry)
        _append_to_log_file(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.calls)


def _append_to_log_file(entry: dict[str, Any]) -> None:
    # Recorder instances are per job, so their instance locks cannot
    # serialize writes made by concurrent jobs. Keep each JSONL record in
    # one process-wide critical section to prevent interleaving/loss.
    with _FILE_APPEND_LOCK:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def is_logged(func: Callable) -> bool:
    """Whether `func` is already wrapped by logged_tool.

    Exists because two call sites legitimately want to wrap: capabilities.
    resolve_tools() wraps every bound tool uniformly, while
    camel_service._budgeted_search_tool() wraps search_web *before* its
    budget check so that only calls which really hit the network are
    recorded. Both ran, so every real search produced TWO identical records
    -- doubling search counts in the tool-call log and in anything derived
    from it. Marking the wrapper lets the outer site skip an already-wrapped
    tool and keeps this module's promise exact: one record == one real call.
    """
    return bool(getattr(func, "_is_logged_tool", False))


def logged_tool(func: Callable, recorder: ToolCallRecorder) -> Callable:
    """Wrap a bound tool method so every call is recorded via `recorder`,
    regardless of what the calling agent's response text later claims.

    Idempotent: wrapping an already-wrapped tool returns it unchanged, so a
    double wrap cannot double-count (see is_logged).
    """
    if is_logged(func):
        return func

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call_args = kwargs if kwargs else {"args": args}
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except Exception as exc:  # re-raise after recording
            recorder.record(
                func.__name__,
                call_args,
                error=repr(exc),
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                started_at=started_at,
            )
            raise
        recorder.record(
            func.__name__,
            call_args,
            result=result,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            started_at=started_at,
        )
        return result

    # @wraps copies __dict__ from `func`, so set the marker afterwards --
    # otherwise a wrapped-but-unmarked tool would still be re-wrappable.
    wrapper._is_logged_tool = True
    return wrapper
