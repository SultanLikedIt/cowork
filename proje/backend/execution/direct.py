"""Single-agent (DIRECT) execution path."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.societies.workforce.events import (
    TaskAssignedEvent,
    TaskCompletedEvent,
    TaskCreatedEvent,
    TaskStartedEvent,
    WorkerCreatedEvent,
)
from camel.tasks import Task
from camel.tasks.task import TaskState

import capabilities
import capability_routing
import runtime_context
from execution.caveats import NO_TOOLS_CAVEAT
from execution.ids import child_task_id
from model_config import UsageTracker
from skills import render_guidance as render_skill_guidance
from stream_protocol import StreamTextFilter
from stream_protocol import sanitize_text as _strip_protocol_markup
from tool_call_log import ToolCallRecorder
from workforce.events import TokenDeltaEvent

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

# DIRECT iteration budget (used by routing/golden regressions; not bound on
# the tool-less single chatbot path today, but kept for telemetry parity).
DIRECT_BASE_ITERATIONS = 3
DIRECT_ITERATIONS_PER_CAPABILITY = 3
DIRECT_MAX_ITERATIONS = 14


def direct_iteration_budget(job_capabilities) -> int:
    resolved = capabilities.expand(job_capabilities)
    return min(
        DIRECT_MAX_ITERATIONS,
        DIRECT_BASE_ITERATIONS + DIRECT_ITERATIONS_PER_CAPABILITY * len(resolved),
    )


SINGLE_AGENT_ROLE = "Assistant"
SINGLE_AGENT_WORKER_ID = "single-agent"

# --- DIRECT execution profiles --------------------------------------------
#
# A profile is a persona and an output convention. It is what remains of the
# fixed "Research Agent"/"Document Agent" workers after the thing that made
# them dangerous -- a toolset welded to a name -- was taken away and given to
# the capability layer.
#
# The distinction is worth being precise about, because it is the fix: a
# profile can never be the reason a run cannot finish. If the request needs to
# write a spreadsheet, the ARTIFACTS capability supplies the tools regardless
# of which profile is selected, so the worst a wrong profile does is give the
# answer a slightly odd tone. A wrong fixed worker used to cost the whole job.
#
# The display names are kept identical to the old worker roles so the UI, the
# stage telemetry, and existing job history all keep reading the same labels.
EXECUTION_PROFILES: dict[str, tuple[str, str]] = {
    capability_routing.PROFILE_GENERAL: (
        "Assistant",
        "You are a capable, direct assistant. Complete the given task "
        "thoroughly, using the tools available to you where they are the "
        "right way to get something done.",
    ),
    capability_routing.PROFILE_RESEARCH: (
        "Research Agent",
        "You are a research specialist. You gather, organize, and summarize "
        "the information needed to complete the task, preferring real "
        "lookups over memory for anything time-sensitive, specific, or "
        "verifiable. Say so explicitly when you remain uncertain after "
        "looking, rather than presenting a guess as a verified fact.",
    ),
    capability_routing.PROFILE_DOCUMENT: (
        "Document Agent",
        "You are a document specialist. You work with the material provided "
        "-- uploaded files and content already in the request -- and produce "
        "clear, well-structured output from it, in the same language as the "
        "input unless asked otherwise.",
    ),
    capability_routing.PROFILE_CODING: (
        "Coding Agent",
        "You are a coding and software-engineering specialist. Write, "
        "execute, debug, and modify code to complete the assigned task.",
    ),
    # Kept for backward compat with persisted jobs that used these profiles.
    capability_routing.PROFILE_REVIEW: (
        "Review Agent",
        "You are a review specialist. You critically review the content in "
        "front of you for accuracy, clarity, and completeness, and suggest "
        "or apply concrete improvements.",
    ),
    capability_routing.PROFILE_OUTLINE: (
        "Outline Agent",
        "You are an outline specialist. You organize ideas and content into "
        "clear, logical outlines and section breakdowns.",
    ),
}


def profile_identity(profile: Optional[str]) -> tuple[str, str]:
    """(display name, system prompt) for a DIRECT execution profile."""
    return EXECUTION_PROFILES.get(
        profile or capability_routing.PROFILE_GENERAL,
        EXECUTION_PROFILES[capability_routing.PROFILE_GENERAL],
    )


def unsupported_capability_message(names) -> str:
    """The answer a job gets when it needs something this system cannot do.

    Deliberately a real, final answer rather than an exception: the user asked
    for something, and "I cannot do this specific thing, here is what I can
    do" is a better outcome than a stack trace. The point is that it is
    produced BEFORE any execution, so an impossible request costs one routing
    call instead of an executor looping to its iteration cap discovering the
    same thing expensively.
    """
    wanted = ", ".join(sorted(names))
    available = ", ".join(capabilities.ALL_CAPABILITIES)
    return (
        f"This request needs a capability this system does not have: "
        f"{wanted}.\n\nWhat is available: {available}.\n\n"
        "Nothing was executed, because no execution here could have produced "
        "the requested result. Rephrase the request within the capabilities "
        "above, or ask for the part of it that is covered."
    )


async def record_streamed_usage(
    streaming_response: Any, tracker: UsageTracker, stage: str
) -> None:
    """Attribute a streamed call's usage, which the model wrapper cannot see.

    _UsageTrackingModel records usage off the object the backend returns, but
    with stream=True that object is a chunk stream with no `.usage` at all --
    so `record()` returned early and a DIRECT run logged ZERO calls and ZERO
    tokens (confirmed against the job store: every single-mode row has
    model_call_count=0, total_tokens=0, by_stage absent). Usage was not
    missing from the provider, only from our accounting.

    CAMEL keeps the provider's settled usage on the final response, which is
    the same accessor SingleAgentWorker._process_task uses for its own token
    count. Awaiting an ALREADY-CONSUMED AsyncStreamingChatAgentResponse costs
    no model call: `_ensure_latest_response` short-circuits on its
    `_consumed` flag and hands back the stored final response
    (chat_agent.py:315-375).

    Best-effort by design: telemetry must never be able to fail a job that
    has already produced its answer.
    """
    try:
        final_response = await streaming_response
    except Exception:  # noqa: BLE001 - accounting must not break a good run
        stabilization_logger.warning(
            "Could not settle streamed response for %s usage", stage, exc_info=True
        )
        return

    info = getattr(final_response, "info", None) or {}
    usage = info.get("usage") or info.get("token_usage")
    already_counted = (
        tracker.stage_snapshot().get(stage, {}).get("model_call_count", 0) > 0
    )
    if usage:
        # The streaming provider invocation was already counted when the
        # wrapped backend returned its stream. Settling only adds tokens.
        tracker.record(
            {"usage": usage}, stage=stage, count_call=not already_counted
        )
    elif not already_counted:
        tracker.record({}, stage=stage)
    else:
        stabilization_logger.warning(
            "Streamed %s response reported no token usage; call still counted",
            stage,
        )


# One extra model call, and only on the path where the alternative is an
# empty answer. Deliberately small: it summarizes work that already happened,
# so it needs no tools and little output.
FINALIZE_MAX_TOKENS = 2048
FINALIZE_TIMEOUT_SECONDS = 60


async def finalize_without_tools(
    agent: ChatAgent,
    tracker: UsageTracker,
    task_id: str,
    emit,
    bound: "capabilities.BoundTools",
) -> str:
    """Get a final answer out of an agent that ran out of turns mid-tool-call.

    Reuses the SAME agent, so its memory still holds every tool result -- the
    page it fetched, the artifact it wrote. Removing the tools first is what
    makes this terminate: with them bound the model would simply call another
    one and we would be back where we started.

    Best-effort. If it fails, a deterministic summary of the artifacts that
    genuinely exist is still better than an empty answer, because those files
    are real and the user can download them.
    """
    try:
        agent.remove_tools([name for name in getattr(agent, "_internal_tools", {})])
    except Exception:  # noqa: BLE001 - proceed tool-free-ish rather than fail
        stabilization_logger.warning("Could not unbind tools for finalization", exc_info=True)

    # No RUN CONTEXT is re-stated here: this reuses the SAME agent, so its
    # system message -- date, abilities, language -- is still in memory and
    # applies to this turn like any other. Re-sending it would only pay for
    # it twice.
    prompt = (
        "You have run out of tool-use turns. Do not call any tool. Using only "
        "what you already did and observed above, write the final answer for "
        "the user now: what you produced, the exact filenames of any files you "
        "created, and anything you could not complete. Be concise and concrete. "
        "Report only what specifically failed -- do not turn it into a general "
        "claim that you are unable to browse, create files, or run code."
    )
    text = ""
    try:
        agent.step_timeout = FINALIZE_TIMEOUT_SECONDS
        response = await agent.astep(prompt)
        # This agent was built with stream=True, so astep hands back an
        # AsyncStreamingChatAgentResponse -- a wrapper with NO `.msg` at all.
        # Reading response.msg here raises AttributeError, which the except
        # below would swallow, so the good answer would silently never appear
        # and every finalization would land on the deterministic fallback.
        # Iterating is also what streams the text to the UI as it arrives,
        # exactly like the main run does.
        if hasattr(response, "msg"):
            text = (
                _strip_protocol_markup(response.msg.content or "").strip()
                if response.msg
                else ""
            )
            if text:
                emit(TokenDeltaEvent(task_id=task_id, delta=text))
        else:
            visible = StreamTextFilter()
            async for partial in response:
                if partial.msg is None:
                    continue
                delta = visible.feed_cumulative(partial.msg.content or "")
                if delta:
                    emit(TokenDeltaEvent(task_id=task_id, delta=delta))
            tail = visible.flush()
            if tail:
                emit(TokenDeltaEvent(task_id=task_id, delta=tail))
            text = visible.visible_text.strip()
            # Attribute this call's tokens like every other one; without it
            # the finalization turn is free in the telemetry and real in the
            # bill.
            await record_streamed_usage(response, tracker, "single_agent")
    except Exception:  # noqa: BLE001 - fall through to the deterministic form
        stabilization_logger.warning("Finalization call failed", exc_info=True)
        text = ""

    if text:
        return text

    # Deterministic fallback, built from what is actually on disk. Never
    # claims anything that is not there -- see artifacts.scan_artifacts.
    return describe_produced_artifacts(bound)


def describe_produced_artifacts(bound: "capabilities.BoundTools") -> str:
    """A truthful last-resort answer listing the files that really exist."""
    tools = getattr(bound, "artifact_tools", None)
    records = list(tools.records()) if tools is not None else []
    if not records:
        return (
            "This run reached its tool-use limit before it could finish, and "
            "produced no files. Nothing was fabricated. Please retry with a "
            "narrower request."
        )
    lines = [
        "This run reached its tool-use limit before writing a summary, but the "
        "following files were created successfully and are ready to download:",
        "",
    ]
    lines.extend(f"- {record.filename} ({record.size_bytes} bytes)" for record in records)
    return "\n".join(lines)


async def run_single_agent(
    content: str,
    tracker: UsageTracker,
    task_id: str,
    event_queue: "asyncio.Queue[Dict[str, Any] | None]",
    skills: Optional[list] = None,
    workspace_root: Optional[str] = None,
    tool_call_recorder: Optional[ToolCallRecorder] = None,
    job_capabilities: Optional[frozenset] = None,
    profile: Optional[str] = None,
    request_topic: Optional[str] = None,
    tool_binding_source: str = "",
    on_tools_bound: Optional[Callable[[capabilities.BoundTools, str], None]] = None,
    enforce_artifact_grounding: bool = False,
) -> Task:
    """Streamed Single-mode chatbot: no tools, no web controller.

    Web search, fetch, and file production belong to Workforce. This path
    is one ChatAgent with tools=None. ``job_capabilities`` is ignored for
    binding so a leftover capability set cannot re-arm tools.
    """

    def _emit(event) -> None:
        event_queue.put_nowait(event.model_dump(mode="json"))

    unit_task_id = child_task_id(task_id)

    display_name, profile_prompt = profile_identity(profile)
    _emit(
        WorkerCreatedEvent(
            worker_id=SINGLE_AGENT_WORKER_ID,
            worker_type="single_agent",
            role=display_name,
        )
    )
    _emit(TaskCreatedEvent(task_id=unit_task_id, description=content))
    _emit(
        TaskAssignedEvent(
            task_id=unit_task_id, worker_id=SINGLE_AGENT_WORKER_ID, dependencies=[]
        )
    )
    _emit(TaskStartedEvent(task_id=unit_task_id, worker_id=SINGLE_AGENT_WORKER_ID))

    bound = capabilities.BoundTools(
        capabilities=frozenset(),
        tools=(),
        names=(),
        missing=(),
    )
    if on_tools_bound is not None:
        on_tools_bound(bound, tool_binding_source or "single_chatbot")
    answer_content = content

    capability_guidance = NO_TOOLS_CAVEAT
    capability_guidance += runtime_context.runtime_block(
        frozenset(), request_text=request_topic or content
    )

    # Resolve through camel_service so tests can patch ChatAgent/get_model there.
    import camel_service

    agent = camel_service.ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name=display_name,
            content=profile_prompt
            + render_skill_guidance(skills)
            + capability_guidance,
        ),
        model=camel_service.get_model(tracker, stream=True, stage="single_agent"),
        tools=None,
        max_iteration=None,
    )
    started = time.monotonic()

    # stream=True makes astep() return an AsyncStreamingChatAgentResponse:
    # awaiting it just hands back the wrapper (cheap); async-iterating it
    # is what actually drives the streamed model call. Each yielded
    # response's msg.content is the *cumulative* text so far (CAMEL
    # accumulates internally), not a delta, so we diff against what we've
    # already emitted to get the token_delta chunks.
    streaming_response = await agent.astep(answer_content)
    # The filter is the single boundary between provider text and anything
    # user-visible: it decides what is assistant text and what is tool-call
    # protocol before a delta exists, so no protocol character can be emitted
    # and later need retracting.
    visible = StreamTextFilter()
    async for partial in streaming_response:
        if partial.msg is None:
            continue
        delta = visible.feed_cumulative(partial.msg.content or "")
        if delta:
            _emit(TokenDeltaEvent(task_id=unit_task_id, delta=delta))
    tail = visible.flush()
    if tail:
        _emit(TokenDeltaEvent(task_id=unit_task_id, delta=tail))
    result_text = visible.visible_text

    await record_streamed_usage(streaming_response, tracker, "single_agent")

    # --- Finalization -----------------------------------------------------
    #
    # A run can end with tools executed and NO text, and it is not an edge
    # case: CAMEL's streaming loop breaks out as soon as iteration_count
    # reaches max_iteration while the model is still emitting tool calls
    # (chat_agent.py:4957-4979), leaving the tool-call response as the final
    # one. The agent did the work -- fetched the page, wrote the spreadsheet --
    # and then produced nothing to say about it.
    #
    # Untreated this is the exact live failure: a job that created a valid
    # XLSX, had it appear in the UI, and still reported failure because the
    # answer was empty. One tool-free continuation call converts that into a
    # correct outcome, and it is strictly cheaper than the alternative the
    # system used to take (fail the task, run recovery, redo the whole chain).
    if not result_text.strip():
        result_text = await finalize_without_tools(
            agent, tracker, unit_task_id, _emit, bound
        )

    duration = time.monotonic() - started

    task = Task(content=content, id=unit_task_id)
    task.result = result_text
    task.state = TaskState.DONE

    _emit(
        TaskCompletedEvent(
            task_id=unit_task_id,
            worker_id=SINGLE_AGENT_WORKER_ID,
            result_summary=result_text,
            processing_time_seconds=duration,
        )
    )
    return task
