"""Run one OpenBot turn using the existing web_controller research stack."""

from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator, Optional

import capabilities
import runtime_context
import web_controller
from ag_ui.core.events import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.core.types import Message, RunAgentInput
from execution.langgraph_runtime.openai_chat import complete_chat
from model_config import UsageTracker
from web_tools import WebTools

logger = logging.getLogger("agent_webapp.openbot")


def _message_text(message: Message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part.strip())
    return ""


def _latest_user_request(messages: list[Message]) -> str:
    for message in reversed(messages or []):
        role = getattr(message, "role", "")
        if role == "user":
            text = _message_text(message)
            if text:
                return text
    return ""


def _standing_role(messages: list[Message]) -> str:
    chunks: list[str] = []
    for message in messages or []:
        role = getattr(message, "role", "")
        if role not in {"system", "developer"}:
            continue
        text = _message_text(message)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


async def _make_complete(tracker: UsageTracker):
    async def complete(
        phase: str, system: str, user: str, schema: Optional[type]
    ) -> Any:
        return await complete_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tracker=tracker,
            stage=f"openbot_{phase}",
            max_tokens=2048 if schema is not None else 4096,
            temperature=0 if schema is not None else None,
            response_format=schema,
        )

    return complete


def _chunk_text(text: str, size: int = 48) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return [""]
    return [cleaned[i : i + size] for i in range(0, len(cleaned), size)]


async def run_openbot_turn(input_data: RunAgentInput) -> AsyncIterator[BaseEvent]:
    """Stream AG-UI events for one research-backed answer."""
    thread_id = input_data.thread_id
    run_id = input_data.run_id
    user_request = _latest_user_request(input_data.messages)
    role_hint = _standing_role(input_data.messages)

    yield RunStartedEvent(
        thread_id=thread_id,
        run_id=run_id,
        parent_run_id=input_data.parent_run_id,
        input=input_data,
    )

    if not user_request:
        yield RunErrorEvent(
            message="No user message in run input.",
            code="EMPTY_USER_MESSAGE",
        )
        return

    tracker = UsageTracker()
    web = WebTools()
    complete = await _make_complete(tracker)
    date_line = runtime_context.now_local().strftime("%A, %d %B %Y")
    lookup = user_request
    if role_hint:
        lookup = f"{role_hint}\n\nTask:\n{user_request}"

    try:
        yield StepStartedEvent(step_name="web_research")

        def progress(stage: str, detail: str = "") -> None:
            logger.info("openbot research %s %s", stage, detail)

        depth = (
            "survey"
            if capabilities.detect_deep_research(user_request)
            else "lookup"
        )
        result = await web_controller.run_web_controller(
            lookup,
            search=lambda query, max_results: web.search_raw(query, max_results),
            fetch=web.read_page,
            complete=complete,
            progress=progress,
            current_date=date_line,
            wants_artifact=False,
            depth=depth,
        )
        answer = (result.answer or "").strip()
        if not answer:
            if result.successful_fetches <= 0:
                answer = (
                    "Web research could not open usable sources for this request. "
                    "Try a narrower query or paste a direct https URL."
                )
            else:
                answer = "Research finished but no answer text was produced."
        else:
            # Same Kaynaklar presentation as Workforce chat answers.
            from execution.langgraph_runtime.workforce import (
                _format_sources_lines,
                _opened_citation_sources,
                _strip_kaynaklar_section,
            )

            answer = _strip_kaynaklar_section(answer)
            source_lines = _format_sources_lines(
                _opened_citation_sources(list(result.evidence or []))
            )
            if source_lines:
                answer = f"{answer.rstrip()}\n\nKaynaklar:\n" + "\n".join(
                    source_lines
                )

        yield StepFinishedEvent(step_name="web_research")

        message_id = str(uuid.uuid4())
        yield TextMessageStartEvent(message_id=message_id, role="assistant")
        for delta in _chunk_text(answer):
            if delta:
                yield TextMessageContentEvent(message_id=message_id, delta=delta)
        yield TextMessageEndEvent(message_id=message_id)

        yield RunFinishedEvent(
            thread_id=thread_id,
            run_id=run_id,
            outcome=RunFinishedSuccessOutcome(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenBot AG-UI run failed")
        yield RunErrorEvent(
            message=f"{type(exc).__name__}: {exc}",
            code="OPENBOT_RUN_FAILED",
        )
