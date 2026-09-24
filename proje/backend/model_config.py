"""Model endpoint settings and per-job usage accounting.

The only knobs are MODEL_BASE_URL, MODEL_NAME, and MODEL_API_KEY. Because we
always talk to an OpenAI-compatible chat-completions endpoint, pointing this
at Ollama, a real vLLM/LiteLLM gateway, or any other compatible provider is a
matter of changing these three environment variables -- no code changes.
The calls themselves are made by execution/langgraph_runtime/openai_chat.py.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL_NAME = "qwen2.5:3b"
DEFAULT_API_KEY = "ollama"

# Per-HTTP-request timeout for every model call. Deliberately generous: a
# call queued behind sibling workers on a backend that serializes requests
# must not be turned into a hard APITimeoutError by the transport. Calls
# that need to fail faster carry their own, tighter asyncio timeout
# (intent 30s, turn resolution, the Coding Agent's own ceiling), which fires
# regardless of this value.
MODEL_CALL_TIMEOUT_SECONDS = 600.0

# Per-job cap on timed steps kept for the UI timing log (oldest dropped first).
MAX_TIMELINE_EVENTS = 500


def _empty_stage_bucket() -> dict[str, float]:
    return {
        "model_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        # Wall-clock spent inside the provider call. Without this, "why did
        # this job take six minutes?" could only be answered by inferring
        # generation time from token counts -- the stage breakdown showed
        # tokens but no time, so the slow stage was a guess.
        "duration_seconds": 0.0,
    }


class UsageTracker:
    """Counts real model calls and raw provider-reported token usage.

    Every call made through openai_chat records into the job's tracker, so
    this is the real per-call input/output token split and the true number
    of model calls a job makes.

    Also keeps an optional per-stage breakdown alongside the aggregate
    (see `record`'s `stage` argument and `stage_snapshot`). The aggregate
    fields/`snapshot()` are unchanged so existing callers stay compatible.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.model_call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self._stages: dict[str, dict[str, int]] = {}
        self._events: list[dict[str, Any]] = []

    def record(
        self,
        response: Any,
        stage: Optional[str] = None,
        *,
        count_call: bool = True,
        duration_seconds: float = 0.0,
    ) -> None:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")

        def _get(key: str) -> int:
            value = getattr(usage, key, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(key)
            return int(value or 0)

        prompt_tokens = _get("prompt_tokens")
        completion_tokens = _get("completion_tokens")
        total_tokens = _get("total_tokens") or (prompt_tokens + completion_tokens)

        with self._lock:
            if count_call:
                self.model_call_count += 1
            self.input_tokens += prompt_tokens
            self.output_tokens += completion_tokens
            self.total_tokens += total_tokens
            if stage:
                bucket = self._stages.setdefault(stage, _empty_stage_bucket())
                if count_call:
                    bucket["model_call_count"] += 1
                bucket["input_tokens"] += prompt_tokens
                bucket["output_tokens"] += completion_tokens
                bucket["total_tokens"] += total_tokens
                bucket["duration_seconds"] += float(duration_seconds or 0.0)
            if duration_seconds:
                self._append_event_locked(
                    "llm",
                    stage or "model",
                    float(duration_seconds),
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                )

    def record_event(
        self,
        kind: str,
        name: str,
        duration_seconds: float,
        *,
        detail: str = "",
        ok: bool = True,
    ) -> None:
        """A timed non-model step (e.g. reranking) for the job's timing log."""
        with self._lock:
            self._append_event_locked(
                kind, name, float(duration_seconds), detail=detail, ok=ok
            )

    def _append_event_locked(
        self,
        kind: str,
        name: str,
        duration_seconds: float,
        *,
        detail: str = "",
        ok: bool = True,
        **extra: Any,
    ) -> None:
        seconds = max(0.0, duration_seconds)
        finished = datetime.now(timezone.utc)
        self._events.append(
            {
                "kind": kind,
                "name": name,
                "started_at": (finished - timedelta(seconds=seconds)).isoformat(),
                "duration_ms": round(seconds * 1000, 1),
                "detail": str(detail or "")[:300],
                "ok": ok,
                **extra,
            }
        )
        if len(self._events) > MAX_TIMELINE_EVENTS:
            del self._events[: len(self._events) - MAX_TIMELINE_EVENTS]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "model_call_count": self.model_call_count,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            }

    def stage_snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {stage: dict(bucket) for stage, bucket in self._stages.items()}

    def timeline_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]
