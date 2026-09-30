"""LangGraph Single path — streamed chatbot, no tools.

Emits the lifecycle events and returns the result shape ``main._run_job``
expects (execution/result.py).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

import capabilities
import capability_routing
import runtime_context
from execution.caveats import NO_TOOLS_CAVEAT
from execution.ids import child_task_id
from execution.langgraph_runtime import events as lg_events
from execution.langgraph_runtime.openai_chat import stream_chat
from execution.result import execution_result
from model_config import UsageTracker
from skills import render_guidance as render_skill_guidance
from tool_call_log import ToolCallRecorder

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

SINGLE_AGENT_WORKER_ID = "single-agent"

# Display names match the Workforce roles so the UI and job history read the
# same labels.
_PROFILES: dict[str, tuple[str, str]] = {
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


def _profile_identity(profile: Optional[str]) -> tuple[str, str]:
    return _PROFILES.get(
        profile or capability_routing.PROFILE_GENERAL,
        _PROFILES[capability_routing.PROFILE_GENERAL],
    )


class SingleAgentState(TypedDict):
    content: str
    system_prompt: str
    unit_task_id: str
    answer: str


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
) -> Any:
    """Streamed Single-mode chatbot via LangGraph (tools=None parity)."""

    def _emit(event: dict[str, Any]) -> None:
        event_queue.put_nowait(event)

    unit_task_id = child_task_id(task_id)
    display_name, profile_prompt = _profile_identity(profile)

    _emit(
        lg_events.worker_created(
            worker_id=SINGLE_AGENT_WORKER_ID,
            role=display_name,
            worker_type="single_agent",
        )
    )
    _emit(lg_events.task_created(task_id=unit_task_id, description=content))
    _emit(
        lg_events.task_assigned(
            task_id=unit_task_id, worker_id=SINGLE_AGENT_WORKER_ID
        )
    )
    _emit(
        lg_events.task_started(task_id=unit_task_id, worker_id=SINGLE_AGENT_WORKER_ID)
    )

    bound = capabilities.BoundTools(
        capabilities=frozenset(),
        tools=(),
        names=(),
        missing=(),
    )
    if on_tools_bound is not None:
        on_tools_bound(bound, tool_binding_source or "single_chatbot")

    capability_guidance = NO_TOOLS_CAVEAT
    capability_guidance += runtime_context.runtime_block(
        frozenset(), request_text=request_topic or content
    )
    system_prompt = (
        profile_prompt + render_skill_guidance(skills) + capability_guidance
    )

    started = time.monotonic()
    answer_parts: list[str] = []

    async def generate_node(state: SingleAgentState) -> dict[str, str]:
        messages = [
            {"role": "system", "content": state["system_prompt"]},
            {"role": "user", "content": state["content"]},
        ]
        async for delta in stream_chat(
            messages, tracker=tracker, stage="single_agent"
        ):
            if delta:
                answer_parts.append(delta)
                _emit(
                    lg_events.token_delta(task_id=state["unit_task_id"], delta=delta)
                )
        return {"answer": "".join(answer_parts)}

    graph = StateGraph(SingleAgentState)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    app = graph.compile()

    try:
        final_state = await app.ainvoke(
            {
                "content": content,
                "system_prompt": system_prompt,
                "unit_task_id": unit_task_id,
                "answer": "",
            }
        )
        result_text = (final_state.get("answer") or "".join(answer_parts)).strip()
    except Exception:
        stabilization_logger.exception("LangGraph single-agent generate failed")
        raise

    duration = time.monotonic() - started
    _emit(
        lg_events.task_completed(
            task_id=unit_task_id,
            worker_id=SINGLE_AGENT_WORKER_ID,
            result_summary=result_text,
            processing_time_seconds=duration,
        )
    )
    return execution_result(
        task_id=unit_task_id,
        content=content,
        result=result_text,
        state="DONE",
    )
