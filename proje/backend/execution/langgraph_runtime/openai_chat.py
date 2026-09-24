"""OpenAI-compatible chat helper shared by LangGraph nodes.

Reads the MODEL_* env vars (defaults in model_config).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, AsyncIterator, Optional

from openai import AsyncOpenAI

logger = logging.getLogger("agent_webapp.stabilization")
# One line per non-streaming model call: how long it took and where the time
# went (reasoning vs answer tokens, which OpenRouter provider served it).
# INFO on its own logger because the root logger is WARNING (see main.py):
#   docker logs proje-backend-1 | grep "llm stage="
llm_logger = logging.getLogger("agent_webapp.llm")
llm_logger.setLevel(logging.INFO)

from model_config import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_NAME,
    MODEL_CALL_TIMEOUT_SECONDS,
    UsageTracker,
)


def _client() -> AsyncOpenAI:
    base_url = (os.environ.get("MODEL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("MODEL_API_KEY") or DEFAULT_API_KEY
    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=MODEL_CALL_TIMEOUT_SECONDS,
    )


def model_name() -> str:
    return os.environ.get("MODEL_NAME") or DEFAULT_MODEL_NAME


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    tracker: Optional[UsageTracker] = None,
    stage: str = "single_agent",
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
) -> AsyncIterator[str]:
    """Yield text deltas from a streamed chat completion; record usage."""
    kwargs: dict[str, Any] = {
        "model": model_name(),
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    client = _client()
    started = time.monotonic()
    stream = await client.chat.completions.create(**kwargs)
    usage_payload: dict[str, int] = {}
    async for chunk in stream:
        if getattr(chunk, "usage", None) is not None:
            u = chunk.usage
            usage_payload = {
                "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
            }
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0].delta, "content", None)
        if delta:
            yield delta

    if tracker is not None:
        elapsed = time.monotonic() - started
        if usage_payload:
            tracker.record(
                {"usage": usage_payload}, stage=stage, duration_seconds=elapsed
            )
        else:
            # Provider omitted usage; still count the call for telemetry parity.
            tracker.record({}, stage=stage, duration_seconds=elapsed)


def _finish_reason(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    return str(getattr(choices[0], "finish_reason", "") or "") if choices else ""


def _first_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return (getattr(choices[0].message, "content", "") or "").strip()


def _log_call(stage: str, response: Any, seconds: float, kwargs: dict[str, Any]) -> None:
    """The per-call timing line. Never raises: telemetry must not fail a job."""
    try:
        usage = getattr(response, "usage", None)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage is not None else 0
        details = getattr(usage, "completion_tokens_details", None) if usage is not None else None
        reasoning_tokens = getattr(details, "reasoning_tokens", None) if details is not None else None
        extra = getattr(response, "model_extra", None) or {}
        llm_logger.info(
            "llm stage=%s model=%s provider=%s seconds=%.1f output_tokens=%d "
            "reasoning_tokens=%s tok_per_s=%.0f finish=%s max_tokens=%s extra_body=%s",
            stage,
            kwargs.get("model"),
            extra.get("provider") or "?",
            seconds,
            output_tokens,
            reasoning_tokens if reasoning_tokens is not None else "?",
            output_tokens / seconds if seconds > 0 else 0.0,
            _finish_reason(response) or "?",
            kwargs.get("max_tokens"),
            kwargs.get("extra_body") or "-",
        )
    except Exception:  # noqa: BLE001
        pass


async def _create_with_retry(
    client: Any, kwargs: dict[str, Any], *, stage: str, attempts: int = 3
) -> Any:
    """One chat call, retried on transient transport failures.

    A deep run spends ten minutes and forty page reads before it writes
    anything, and all of that hung on a single answer call: one
    APIConnectionError and the user received "Research failed" plus a
    bare source list. The evidence was gathered, correct, and thrown away
    because nobody asked twice.

    Only transport-level failures are retried -- a connection reset, a
    timeout, a 5xx from the provider. A refusal, a bad request or an
    exhausted key is not going to answer differently the second time, so
    those still fail immediately and visibly.
    """
    import asyncio

    from openai import APIConnectionError, APITimeoutError, InternalServerError

    transient = (APIConnectionError, APITimeoutError, InternalServerError)
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await client.chat.completions.create(**kwargs)
        except transient as exc:  # noqa: PERF203
            last = exc
            if attempt == attempts:
                break
            delay = 2.0 * attempt
            logger.warning(
                "chat stage=%s transport failure (%s); retry %d/%d in %.0fs",
                stage,
                type(exc).__name__,
                attempt,
                attempts - 1,
                delay,
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last


async def complete_chat(
    messages: list[dict[str, str]],
    *,
    tracker: Optional[UsageTracker] = None,
    stage: str = "web_answer",
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    response_format: Optional[type] = None,
    retry_on_truncation: bool = False,
    json_mode: bool = False,
    extra_body: Optional[dict[str, Any]] = None,
    reasoning_fallback: bool = False,
) -> Any:
    """One non-streaming chat call. If ``response_format`` is a Pydantic model,
    ask for JSON and validate (or recover). Used by web_controller ``complete``.

    ``retry_on_truncation`` re-issues the call once at double the ceiling
    when the provider says it stopped because it ran out of room
    (finish_reason "length"). The signal was always on the response and
    never read, so a report that outgrew its budget was simply shipped cut
    -- one live answer stopped mid-sentence inside section 5 and the
    server appended its sources block to the wound. Worth it only where
    the text IS the deliverable; a plan or a query that hits the ceiling
    is a different problem.
    """
    import json

    import web_controller

    kwargs: dict[str, Any] = {
        "model": model_name(),
        "messages": list(messages),
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if response_format is not None:
        kwargs["response_format"] = {"type": "json_object"}
        # Nudge the model: many OpenAI-compatible servers need the word JSON.
        schema_hint = ""
        try:
            schema_hint = json.dumps(response_format.model_json_schema(), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            schema_hint = response_format.__name__
        kwargs["messages"] = [
            *messages[:-1],
            {
                "role": messages[-1]["role"],
                "content": (
                    f"{messages[-1]['content']}\n\n"
                    f"Respond with a JSON object matching this schema:\n{schema_hint}"
                ),
            },
        ]

    if extra_body:
        # Provider-specific request options (e.g. OpenRouter "reasoning" or
        # "provider" routing), sent as-is in the JSON body.
        kwargs["extra_body"] = dict(extra_body)

    if json_mode and response_format is None:
        # Same provider JSON mode as above, but the caller parses the text
        # itself. A model left to write a large JSON document freehand
        # occasionally emits an unescaped quote inside a string, and the
        # whole reply is lost to one character.
        kwargs["response_format"] = {"type": "json_object"}

    client = _client()
    started = time.monotonic()
    try:
        response = await _create_with_retry(client, kwargs, stage=stage)
    except Exception as exc:  # noqa: BLE001 - only the json_mode case is retried
        from openai import BadRequestError

        if not (json_mode and response_format is None and isinstance(exc, BadRequestError)):
            raise
        logger.warning(
            "chat stage=%s: provider rejected JSON mode (%s); retrying without it",
            stage,
            str(exc)[:160],
        )
        kwargs.pop("response_format", None)
        response = await _create_with_retry(client, kwargs, stage=stage)

    if retry_on_truncation and _finish_reason(response) == "length":
        logger.warning(
            "chat stage=%s was cut at max_tokens=%s; retrying once at %s",
            stage,
            max_tokens,
            max_tokens * 2,
        )
        kwargs["max_tokens"] = max_tokens * 2
        retried = await _create_with_retry(client, kwargs, stage=stage)
        # Keep the retry only if it actually got further; a provider that
        # ignores the larger ceiling must not cost us the first answer.
        if len(_first_text(retried)) > len(_first_text(response)):
            response = retried
    _log_call(stage, response, time.monotonic() - started, kwargs)

    if tracker is not None:
        # Covers the retry too when one happened -- the stage really did
        # cost that long, and hiding the retry would understate it.
        elapsed = time.monotonic() - started
        usage = getattr(response, "usage", None)
        if usage is not None:
            tracker.record(
                {
                    "usage": {
                        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                        "completion_tokens": int(
                            getattr(usage, "completion_tokens", 0) or 0
                        ),
                        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
                    }
                },
                stage=stage,
                duration_seconds=elapsed,
            )
        else:
            tracker.record({}, stage=stage, duration_seconds=elapsed)

    text = ""
    choices = getattr(response, "choices", None) or []
    if choices:
        text = (choices[0].message.content or "").strip()
    if not text and reasoning_fallback and choices and _finish_reason(response) == "stop":
        # Measured on OpenRouter: one upstream provider of deepseek-v4-flash
        # (OpenInference) returns the WHOLE answer in `reasoning` with
        # content empty and finish_reason "stop" -- the MODE/PLAN/code reply
        # was there, complete, and the caller saw "". Opt-in only: for a
        # caller whose reply is validated downstream (the Coding Agent parses
        # a fenced program and refuses anything else), never for free prose,
        # where a model's actual thinking could otherwise become the answer.
        message = choices[0].message
        extra = getattr(message, "model_extra", None) or {}
        reasoning = (
            getattr(message, "reasoning", None)
            or extra.get("reasoning")
            or getattr(message, "reasoning_content", None)
            or extra.get("reasoning_content")
            or ""
        )
        if isinstance(reasoning, str) and reasoning.strip():
            logger.warning(
                "chat stage=%s: content empty, using the reasoning field (%d chars)",
                stage,
                len(reasoning),
            )
            text = reasoning.strip()
    if not text:
        # A reasoning model that spends its whole budget thinking returns
        # content=None with finish_reason "length"; callers only ever saw "".
        usage = getattr(response, "usage", None)
        logger.warning(
            "chat stage=%s returned no content (finish_reason=%s, "
            "completion_tokens=%s, max_tokens=%s)",
            stage,
            _finish_reason(response) or "?",
            getattr(usage, "completion_tokens", None) if usage is not None else None,
            kwargs.get("max_tokens"),
        )

    if response_format is None:
        return text

    try:
        return response_format.model_validate_json(text)
    except Exception:  # noqa: BLE001
        recovered = web_controller.recover_model(text, response_format)
        if recovered is not None:
            return recovered
        return text
