"""Builds the CAMEL model backend from environment variables.

The only knobs are MODEL_BASE_URL, MODEL_NAME, and MODEL_API_KEY. Because we
always talk to an OpenAI-compatible chat-completions endpoint, pointing this
at Ollama, a real vLLM/LiteLLM gateway, or any other compatible provider is a
matter of changing these three environment variables -- no code changes.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from camel.models import ModelFactory
from camel.models.base_model import BaseModelBackend
from camel.types import ModelPlatformType
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL_NAME = "qwen2.5:3b"
DEFAULT_API_KEY = "ollama"

# Per-HTTP-request timeout for every model call.
#
# Passed explicitly because the vendor default is both undocumented and
# too low for this app: OpenAICompatibleModel falls back to
# `float(os.environ.get("MODEL_TIMEOUT", 180))` (openai_compatible_model
# .py:113), so every call silently carried a 180s transport deadline.
#
# That inverted the app's own policy. camel_service derives
# WORKFORCE_TASK_TIMEOUT_SECONDS (600s) as the deliberate bound on how
# long work may take when several dynamic workers run at once against an
# inference backend that serializes requests -- but a 180s transport
# deadline fires first, so queueing behind sibling workers was being
# converted into a hard APITimeoutError. The agent layer does not soften
# it either: ChatAgent._get_model_response retries RateLimitError only
# and re-raises everything else (chat_agent.py:3422-3426), so the
# exception propagates out of the worker's step and Worker
# ._process_single_task marks the whole subtask FAILED with the exception
# text as its result (worker.py:100-103) -- surfaced in the UI as
# "Task timed out". Observed live: with 3 dynamic workers, the last one
# queued (Database) blew 180s while its two siblings completed.
#
# Set equal to the orchestration bound so the derived, tested policy is
# the one that governs and the transport never preempts it. No new
# magic number: this deliberately reuses that bound rather than
# introducing a second, independently-guessed deadline. Calls that need
# to fail faster than the batch already carry their own, tighter asyncio
# step_timeout (routing 30s, synthesis 60s), which fires regardless of
# this value.
MODEL_CALL_TIMEOUT_SECONDS = 600.0

# The model's real CONTEXT window, in tokens -- which is not the same thing
# as the per-response output cap, though CAMEL treats them as one.
#
# BaseModelBackend.token_limit (base_model.py) returns
# `model_config_dict.get("max_tokens") or model_type.token_limit`, i.e. it
# reports the OUTPUT cap as the context window whenever max_tokens is set,
# which it always is here. Every agent therefore believed its context
# window was 4096 tokens (or 8192 for dynamic workers), and one thing in
# particular keys off that number: ChatAgent.summarize_threshold defaults
# to 50, so a step whose context exceeds HALF the reported limit first
# makes an extra "summarize the conversation" model call, rewrites its own
# memory with the summary, and only then makes the real call.
#
# Measured, one logical step, same agent:
#     input 1211 tokens (< 2048)  -> 1 backend call
#     input 3013 tokens (> 2048)  -> 2 backend calls, 6176 input tokens
# with `Token count (3013) exceed threshold (2048). Triggering
# summarization.` in CAMEL's log. Every management call over 2048 tokens in
# an instrumented Workforce run doubled; every call under it did not.
#
# That is the single root cause behind planning=2, the doubled quality
# calls, and the doubled recovery-assignment calls. Reporting the true
# context window fixes all of them at once and leaves CAMEL's compression
# in place for the case it was written for -- a context genuinely close to
# overflowing -- instead of firing on every ordinary orchestration prompt.
#
# Deliberately env-tunable and conservative: 32k is the smallest window in
# common use among current chat models, and the app already bounds every
# prompt it builds well below it (see camel_service's MAX_PLANNER_CONTENT_
# CHARS / MAX_ANALYSIS_* / MAX_SYNTHESIS_INPUT_CHARS). Lower it if you
# deploy a genuinely smaller model; compression then resumes at 50% of
# whatever you set, exactly as CAMEL intends.
MODEL_CONTEXT_TOKENS = int(os.environ.get("MODEL_CONTEXT_TOKENS") or 32_768)

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

    CAMEL's own Workforce only ever records a task's *total* token count
    (see SingleAgentWorker -- it stores ``{"total_tokens": ...}`` and
    nothing else), and it has no event for the decomposition/assignment/
    quality-check calls the coordinator and task agent make. Wrapping our
    own model instances is the only way to see the real per-call input/
    output token split and the true number of model calls a job makes.

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


class _UsageTrackingModel(BaseModelBackend):
    """Transparent wrapper that records every real call the backend makes."""

    def __init__(
        self,
        backend: BaseModelBackend,
        tracker: UsageTracker,
        stage: Optional[str] = None,
    ) -> None:
        self._backend = backend
        self._tracker = tracker
        self._stage = stage
        super().__init__(
            model_type=backend.model_type,
            model_config_dict=backend.model_config_dict,
        )

    @property
    def token_counter(self):
        return self._backend.token_counter

    @property
    def token_limit(self) -> int:
        """The context window, not the output cap.

        Overrides the inherited property rather than delegating to the
        wrapped backend, whose value is `max_tokens` -- see
        MODEL_CONTEXT_TOKENS for why that conflation is what made every
        oversized management prompt cost two model calls. This is the one
        place the correction has to live: ChatAgent reads
        `self.model_backend.token_limit` directly when deciding whether to
        compress, so setting ChatAgent's own `token_limit` argument would
        not reach it.
        """
        return MODEL_CONTEXT_TOKENS

    def _run(self, messages, response_format=None, tools=None):
        started = time.monotonic()
        response = self._backend.run(messages, response_format, tools)
        self._tracker.record(
            response, stage=self._stage, duration_seconds=self._elapsed(started)
        )
        return response

    async def _arun(self, messages, response_format=None, tools=None):
        started = time.monotonic()
        response = await self._backend.arun(messages, response_format, tools)
        self._tracker.record(
            response, stage=self._stage, duration_seconds=self._elapsed(started)
        )
        return response

    def _elapsed(self, started: float) -> float:
        # A streamed call returns before generation ends, so its wall time would be a lie.
        if self.model_config_dict.get("stream"):
            return 0.0
        return time.monotonic() - started

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def temporarily_override_stage(self, stage: str) -> "_StageOverride":
        """Context manager: retag every call this instance makes for the
        duration of the block (e.g. Workforce._analyze_task() reuses
        task_agent -- tagged "planning" -- for quality/failure analysis
        too; see camel_service._patched_analyze_task).

        Plain instance-attribute mutation, NOT contextvars/threading.local.
        ChatAgent.step()'s step_timeout path runs the actual call inside a
        fresh concurrent.futures.ThreadPoolExecutor worker thread
        (chat_agent.py:2714-2727), and neither contextvars nor
        threading.local values propagate into a thread spawned that way --
        confirmed empirically; an earlier contextvars-based version of this
        silently never took effect for any sync .step() call guarded by a
        step_timeout, which is exactly the quality-check path. A plain
        object attribute has no such boundary: _run/_arun read self._stage
        fresh on every call, from whichever thread is executing them, so
        the caller's mutation (made on the calling thread, before the
        worker thread is even spawned) is already visible by the time the
        worker thread reads it. Safe without a lock because the call site
        (ChatAgent.step()) blocks synchronously on future.result(...)
        until the override is no longer needed -- there is no window where
        a second, unrelated call on this same instance could observe the
        overridden value.
        """
        return _StageOverride(self, stage)


class _StageOverride:
    def __init__(self, model: "_UsageTrackingModel", stage: str) -> None:
        self._model = model
        self._stage = stage
        self._previous: Optional[str] = None

    def __enter__(self) -> "_StageOverride":
        self._previous = self._model._stage
        self._model._stage = self._stage
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._model._stage = self._previous


def get_model(
    tracker: Optional[UsageTracker] = None,
    stream: bool = False,
    stage: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> BaseModelBackend:
    """Create the CAMEL model backend for the configured endpoint.

    If a UsageTracker is given, every real call this backend makes is
    recorded into it (call count + actual provider-reported token usage).

    `stage` tags every call this particular model instance makes for the
    tracker's per-stage breakdown (e.g. "planning", "research") -- see
    UsageTracker.stage_snapshot(). It's a static label fixed at model
    construction time, so it's only accurate as long as one model instance
    is used for exactly one logical stage; the returned
    _UsageTrackingModel's temporarily_override_stage() exists for the one
    case where that's not true (see camel_service._patched_analyze_task).

    `temperature`/`max_tokens` override the default 4096-token, provider-
    default-temperature config for call sites that need a small, near-
    deterministic response -- e.g. artifact-intent classification, which
    wants temperature=0 and a handful of tokens, not the same budget a
    worker's free-text answer needs.

    If stream is True, ChatAgent.astep() on an agent built with this model
    returns an AsyncStreamingChatAgentResponse instead of a ChatAgentResponse
    (see camel_service.run_single_agent). Note: _UsageTrackingModel.record()
    expects a single response object with a `.usage` attribute; in stream
    mode the raw provider call returns a chunk stream instead, so usage
    tracking is silently skipped for streamed calls.
    """
    base_url = (os.environ.get("MODEL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model_name = os.environ.get("MODEL_NAME") or DEFAULT_MODEL_NAME
    api_key = os.environ.get("MODEL_API_KEY") or DEFAULT_API_KEY

    model_config_dict: dict[str, Any] = {
        "max_tokens": max_tokens if max_tokens is not None else 4096,
        "stream": stream,
    }
    if temperature is not None:
        model_config_dict["temperature"] = temperature

    backend = ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
        model_type=model_name,
        url=base_url,
        api_key=api_key,
        model_config_dict=model_config_dict,
        # Explicit: see MODEL_CALL_TIMEOUT_SECONDS for why the vendor's
        # silent 180s default was preempting the app's own bound.
        timeout=MODEL_CALL_TIMEOUT_SECONDS,
    )
    if tracker is None:
        return backend
    return _UsageTrackingModel(backend, tracker, stage=stage)
