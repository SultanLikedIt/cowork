"""A worker that runs out of turns mid-tool-call must still answer.

The confirmed live failure this pins ("Galatasaray son maçtaki ilk 11"):
the Research Agent searched, opened the right match page, and then hit
max_iteration while still emitting tool calls. CAMEL breaks out of that
loop with the tool-call response as the final one
(chat_agent.py:3285-3288), so the worker returned NO assistant text. Empty
text cannot parse as a TaskResult, so SingleAgentWorker fell back to
`content="Task processing failed", failed=True`, the task burned its one
recovery attempt reproducing the same thing, and the user saw only
"Task processing failed (assigned to worker: ...)" -- with the answer
sitting unused in the agent's own memory the whole time.

Raising the iteration ceiling does not fix this class of bug; it only
moves where the run stops. The fix is the same one the DIRECT path has
had since the 430K-token incident: when a step ends with no text, ask the
same conversation again with the tools unbound.

No model and no network: the agents here are stubs.

    python test_worker_out_of_turns.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402
from camel.messages import BaseMessage  # noqa: E402
from camel.responses import ChatAgentResponse  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def _response(content: str) -> ChatAgentResponse:
    return ChatAgentResponse(
        msgs=[
            BaseMessage.make_assistant_message(
                role_name="Research Agent", content=content
            )
        ],
        terminated=False,
        info={"usage": {"total_tokens": 10}},
    )


class _StubAgent:
    """A worker agent whose step returns whatever the test tells it to.

    `clone(with_memory=True)` returns a tool-free copy that answers from
    "memory", which is exactly the contract the real finalization relies on.
    """

    role_name = "Research Agent"

    def __init__(self, step_results, finalization="", clone_raises=False):
        self._step_results = list(step_results)
        self._finalization = finalization
        self._clone_raises = clone_raises
        self._internal_tools = {"web_search": object(), "web_read": object()}
        self.step_timeout = 180
        self.steps = 0
        self.clones = 0

    async def astep(self, *_args, **_kwargs):
        self.steps += 1
        return self._step_results.pop(0)

    def clone(self, with_memory: bool = False):
        self.clones += 1
        if self._clone_raises:
            raise RuntimeError("clone unavailable")
        assert with_memory, "the finalization turn needs the tool results"
        clone = _StubAgent([_response(self._finalization)])
        clone._parent = self
        return clone

    def remove_tools(self, names):
        for name in names:
            self._internal_tools.pop(name, None)


class _StubWorker:
    """Stands in for SingleAgentWorker: hands out one agent, records it."""

    def __init__(self, agent):
        self._agent = agent

    async def _original_get_worker_agent(self):
        return self._agent


def _wrapped_agent(agent):
    """Run the real patch against a stub worker."""
    worker = _StubWorker(agent)
    original = camel_service._ORIGINAL_GET_WORKER_AGENT
    camel_service._ORIGINAL_GET_WORKER_AGENT = (
        lambda self: self._original_get_worker_agent()
    )
    try:
        return asyncio.run(camel_service._patched_get_worker_agent(worker))
    finally:
        camel_service._ORIGINAL_GET_WORKER_AGENT = original


def test_empty_step_is_recovered_into_a_real_answer() -> None:
    lineup = "Galatasaray ilk 11: Muslera, Sallai, Abdülkerim, Sanchez, ..."
    agent = _StubAgent([_response("")], finalization=lineup)
    wrapped = _wrapped_agent(agent)

    response = asyncio.run(wrapped.astep("do the task"))
    text = camel_service._visible_response_text(response)

    check(
        "an empty worker step is replaced by an answer from its own memory",
        text == lineup,
        repr(text),
    )
    check(
        "the recovery asked the model exactly once more",
        agent.clones == 1,
        f"{agent.clones} clones",
    )
    check(
        "the answer would now parse as a TaskResult instead of failing",
        bool(camel_service._recover_task_result_content(text)),
        repr(text),
    )


def test_the_pooled_agent_keeps_its_tools() -> None:
    """The worker agent is reused by later tasks in the same job.

    AgentPool.return_agent only calls reset(), which does not restore
    tools -- so unbinding them on the agent itself would leave every
    subsequent task in the job unable to search or read anything. The
    finalization runs on a throwaway clone for exactly this reason.
    """
    agent = _StubAgent([_response("")], finalization="an answer")
    wrapped = _wrapped_agent(agent)
    asyncio.run(wrapped.astep("do the task"))

    check(
        "the reusable worker agent still has its web tools bound",
        set(agent._internal_tools) == {"web_search", "web_read"},
        sorted(agent._internal_tools),
    )


def test_a_normal_answer_is_passed_through_untouched() -> None:
    """The overwhelmingly common path must cost nothing: no extra call."""
    agent = _StubAgent([_response("Here is the lineup.")], finalization="unused")
    wrapped = _wrapped_agent(agent)

    response = asyncio.run(wrapped.astep("do the task"))

    check(
        "a step that produced text is returned as-is",
        camel_service._visible_response_text(response) == "Here is the lineup.",
    )
    check(
        "and no finalization call was made",
        agent.clones == 0,
        f"{agent.clones} clones",
    )


def test_tool_call_markup_does_not_count_as_an_answer() -> None:
    """CAMEL's accumulator can leave "[TOOL RESULT] ..." status lines in
    content. That is not an answer, and treating it as one would send the
    user protocol noise instead of triggering the recovery."""
    markup = "[TOOL RESULT] web_read → {...}\n[TOOL CALL] web_search → {}"
    agent = _StubAgent([_response(markup)], finalization="the real answer")
    wrapped = _wrapped_agent(agent)

    response = asyncio.run(wrapped.astep("do the task"))

    check(
        "a response carrying only tool markup triggers the recovery",
        camel_service._visible_response_text(response) == "the real answer",
        camel_service._visible_response_text(response),
    )


def test_a_failed_finalization_leaves_the_original_response() -> None:
    """Best-effort: if the extra call cannot run, behaviour is unchanged
    rather than worse -- the caller still gets CAMEL's own fallback."""
    agent = _StubAgent([_response("")], clone_raises=True)
    wrapped = _wrapped_agent(agent)

    response = asyncio.run(wrapped.astep("do the task"))

    check(
        "the original (empty) response is returned when recovery fails",
        camel_service._visible_response_text(response) == "",
    )


def test_usable_failed_taskresult_is_accepted() -> None:
    """A worker that found the match but not every requested field used to
    set failed=true; CAMEL then retried and hid the answer. Partial but
    substantive output must still be shown."""
    from camel.societies.workforce.utils import TaskResult

    payload = (
        '{"content": "Last match: Galatasaray 2-2 Corum FK on 14 Aug 2026. '
        'Starting 11 was not listed on the pages that were opened.", '
        '"failed": true}'
    )
    parsed = camel_service._patched_parse_structured_response(
        payload, TaskResult, fallback_values=None
    )
    check(
        "usable failed TaskResult is accepted as success",
        isinstance(parsed, TaskResult)
        and parsed.failed is False
        and "Corum FK" in parsed.content,
        repr(parsed),
    )


def test_the_wrapper_is_applied_once_per_agent() -> None:
    """Pooled agents come back repeatedly; wrapping each time would stack
    one finalization layer per task."""
    agent = _StubAgent([_response("ok"), _response("ok")])
    first = _wrapped_agent(agent)
    second = _wrapped_agent(agent)

    check(
        "an already-wrapped pooled agent is not wrapped again",
        first.astep is second.astep,
    )


def _main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All out-of-turns worker recovery checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
