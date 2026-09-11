"""Regression for the dynamic worker's tool binding vs. what it is told.

Live failure this pins: a dynamic Security worker made two web-search tool
calls that never resolved, burned its turn budget, and returned an
unusable result. The tools were bound correctly the whole time -- the
worker was simply never told their names. DYNAMIC_WORKER_TOOL_HONESTY_
CAVEAT described the toolset in prose ("web search, code execution")
and named none of the callables, so the model had to guess the identifier.

That guess is not a soft failure. CAMEL resolves a tool call with a bare
`self._internal_tools[func_name]` (chat_agent.py:3855) placed OUTSIDE the
surrounding try/except, so an unbound name raises an uncaught KeyError
that propagates out of the agent step and is caught only by the worker
wrapper (worker.py:100-103), which fails the whole task with the
exception text as its result.

The core assertion is end-to-end and deterministic: take the names the
worker is ADVERTISED in its system message, resolve each against what the
cloned agent actually has bound, and invoke the search tool by that
advertised name -- with the network stubbed, so no model and no HTTP.

    python test_dynamic_worker_tools.py
"""

import asyncio
import os
import re
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402
import capabilities  # noqa: E402
from camel.tasks import Task  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


# A real, empty directory: the workspace-backed capabilities (code_exec,
# artifacts, files) refuse to bind without one, so a template built with no
# workspace would carry search only and every execution assertion below would
# be vacuously testing nothing.
_WORKSPACE = tempfile.mkdtemp(prefix="dynamic_worker_ws_")

# The capability set a job would have to have been routed with for a dynamic
# worker to be offered execution tools at all. Passing it explicitly is the
# point: after the capability refactor the template's toolset comes from the
# JOB's capabilities, not from a hardcoded toolkit list, so a test that omits
# it is testing a job that legitimately has no execution capability.
_JOB_CAPABILITIES = frozenset({capabilities.CODE_EXEC, capabilities.ARTIFACTS})


def _build_template(search_stub):
    """Build the real dynamic-worker template with search stubbed out.

    camel_service.search_web is resolved inside _budgeted_search_tool() while
    build_workforce runs, so patching the module attribute first means the
    bound tool wraps the stub -- same wrapper chain, no network.
    """
    captured = {}

    class _FakeWorkforce:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            # Real Workforce.__init__ REBUILDS these two from the passed-in
            # agents (workforce.py:381-403, 448-469) and exposes them under
            # these names; build_workforce then applies the settings the
            # rebuild drops. Modelled here so that step has something to
            # act on -- see _configure_management_agent.
            self.coordinator_agent = kwargs.get("coordinator_agent")
            self.task_agent = kwargs.get("task_agent")

        def add_single_agent_worker(self, description, worker=None, **kwargs):
            return self

    original_search = camel_service.search_web
    original_workforce = camel_service.Workforce
    camel_service.search_web = search_stub
    camel_service.Workforce = _FakeWorkforce
    try:
        camel_service.build_workforce(
            callback=object(),
            tracker=camel_service.UsageTracker(),
            workspace_root=_WORKSPACE,
            job_capabilities=_JOB_CAPABILITIES,
        )
    finally:
        camel_service.search_web = original_search
        camel_service.Workforce = original_workforce
    return captured["new_worker_agent"]


def _advertised_names(roster: str) -> list[str]:
    """The names the worker's own system message tells it to call."""
    match = re.search(r"named exactly: (.+?)\. Call them", roster, re.DOTALL)
    return [n.strip() for n in match.group(1).split(",")] if match else []


def main() -> None:
    calls: list[tuple] = []

    def fake_search_web(query, *args, **kwargs):
        calls.append((query, args, kwargs))
        # Empty on purpose: web_search then returns zero candidates and
        # invents no pages, so invoking the advertised tool below exercises
        # the real pipeline end-to-end with zero network access.
        return []

    fake_search_web.__name__ = "search_web"
    fake_search_web.__doc__ = "Search the web for a query."

    template = _build_template(fake_search_web)

    # A dynamic worker is a CLONE of this template (workforce.py:3866), so
    # every assertion below is made against the clone, not the template.
    worker = template.clone(with_memory=False)
    bound = set(worker._internal_tools)

    # --- What the worker is told matches what it has -------------------
    roster = camel_service._dynamic_worker_tool_roster(template)
    advertised = _advertised_names(roster)

    check("the roster is non-empty", bool(advertised), roster)
    check(
        "every advertised name is actually bound on the worker",
        set(advertised) <= bound,
        f"advertised-but-unbound: {sorted(set(advertised) - bound)}",
    )
    check(
        "every bound tool is advertised to the worker",
        bound <= set(advertised),
        f"bound-but-unadvertised: {sorted(bound - set(advertised))}",
    )
    check(
        "the web tools are not advertised — the controller owns search/read",
        "web_search" not in advertised and "web_read" not in advertised,
        str(advertised),
    )

    # The prose that produced the wrong guess must not come back.
    caveat = camel_service.DYNAMIC_WORKER_TOOL_HONESTY_CAVEAT
    check(
        "the caveat no longer names tools only in prose",
        "web search, code execution" not in caveat,
        caveat[:120],
    )

    # --- The exposed schema is what the model actually sees ------------
    schema_names = {
        t.get_openai_tool_schema()["function"]["name"]
        for t in worker._internal_tools.values()
    }
    check(
        "registry names and exposed schema names agree",
        schema_names == bound,
        f"schema={sorted(schema_names)} registry={sorted(bound)}",
    )
    check(
        "advertised names match the schema the model is given",
        set(advertised) == schema_names,
        f"advertised={sorted(advertised)} schema={sorted(schema_names)}",
    )

    # --- The core proof: advertised names resolve on the clone ----------
    # Mirrors CAMEL's own resolution (chat_agent.py:3855) exactly, so a
    # name the worker is told to use but cannot resolve fails here the
    # same way it would fail live -- with a KeyError.
    check(
        "dynamic workers do not get model-facing web tools",
        "web_search" not in worker._internal_tools
        and "web_read" not in worker._internal_tools,
        sorted(worker._internal_tools),
    )

    # Every advertised name must be callable -- an unbound one aborts the
    # whole task, so none may be advertised blind.
    unresolvable = [n for n in advertised if n not in worker._internal_tools]
    check(
        "every advertised name resolves on the worker",
        not unresolvable,
        f"unresolvable: {unresolvable}",
    )

    # --- The wrapper chain must not have renamed anything --------------
    # _search_tool wraps the real function in a closure; if @wraps were
    # dropped the exposed name would become the wrapper's and the model
    # would be handed an identifier that cannot resolve.
    check(
        "no tool leaked a wrapper's name",
        not ({"bounded", "bounded_search", "wrapper"} & bound),
        sorted(bound),
    )

    # --- ThinkingToolkit must stay unbound -----------------------------
    # Its 7 tools are gated into a 7-step chain (plan -> hypothesize ->
    # think -> contemplate -> critique -> synthesize -> reflect) whose every
    # gate response instructs the model to call another tool. That is one
    # step longer than WORKER_MAX_ITERATIONS, so a worker entering the chain
    # is still emitting tool calls when the ceiling hits and the loop breaks
    # at the cap -- measured live as 18 dynamic-worker calls for 3 workers,
    # exactly 3 x 6, on subtasks needing no tools at all. Budgeting it did
    # not help: a tool return is only a string and cannot end the loop.
    thinking = {
        "plan",
        "hypothesize",
        "think",
        "contemplate",
        "critique",
        "synthesize",
        "reflect",
    }
    check(
        "no thinking-toolkit tool is bound to a dynamic worker",
        not (thinking & bound),
        f"re-bound: {sorted(thinking & bound)}",
    )
    check(
        "no thinking-toolkit tool is advertised to a dynamic worker",
        not (thinking & set(advertised)),
        f"advertised: {sorted(thinking & set(advertised))}",
    )
    check(
        "the real capabilities survived the removal",
        {"execute_code", "execute_command"} <= bound,
        sorted(bound),
    )
    check(
        "the job's artifact capability reached the template too",
        "create_xlsx_artifact" in bound,
        sorted(bound),
    )

    # --- execution tooling is scoped, but still present -----------------
    # Report-writing subtasks were calling the execution tool to print prose
    # -- once with a 16,466-char `code` argument, which overran the
    # completion budget and truncated the tool-call JSON mid-string. The
    # scope statement lives in the tool's own schema (not only the system
    # message) because the schema is re-sent every turn, so it is in front
    # of the model at the moment it picks a tool.
    #
    # These assert guidance, never enforcement: the capability must stay.
    exec_desc = worker._internal_tools["execute_code"].get_function_description()

    check(
        "execute_code is bound and callable",
        callable(worker._internal_tools["execute_code"].func),
    )
    check(
        "execute_code's description mentions code execution",
        "code" in exec_desc.lower(),
        exec_desc,
    )

    # The system message must carry the same judgment, and must NOT have
    # lost the anti-fabrication rule it exists for.
    caveat = camel_service.DYNAMIC_WORKER_TOOL_HONESTY_CAVEAT
    check(
        "the caveat tells the worker to judge whether a tool is needed",
        "First decide whether the task needs one at all" in caveat,
    )
    check(
        "the caveat says producing text is not a reason to run code",
        "producing text is not a reason to execute code" in caveat,
    )
    check(
        "the anti-fabrication rule is still intact",
        "you MUST actually call the relevant tool" in caveat
        and "never acceptable" in caveat,
        "the honesty guarantee was weakened",
    )
    check(
        "the caveat does not send dynamic workers into a ReAct web loop",
        "call web_search(query)" not in caveat
        and "web_read(url)" not in caveat
        and "Scrapy" not in caveat
        and "You do not have web search" in caveat,
        caveat[-200:],
    )

    # Each job builds its own capability instances, so a second job must get
    # an identical schema rather than one that has accumulated text. This
    # used to be a real hazard when the description was mutated in place on
    # a possibly-shared toolkit schema; the tool's description now comes from
    # its docstring, so this asserts the property still holds.
    second = _build_template(fake_search_web)
    second_desc = second._internal_tools["execute_code"].get_function_description()
    check(
        "the tool schema is identical across jobs (no accumulation)",
        second_desc == exec_desc,
        f"len(job1)={len(exec_desc)} len(job2)={len(second_desc)}",
    )

    test_task_based_tool_selection(template)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All dynamic-worker tool-binding checks passed")


def _agent_for(template, task_text, role="Some Agent", sys_msg="You do work."):
    """Build a dynamic worker the way the real fallback path does."""

    class _TemplateHolder:
        new_worker_agent = template

    holder = _TemplateHolder()
    holder._dynamic_worker_task_content = task_text
    return asyncio.run(
        camel_service._patched_create_new_agent(holder, role, sys_msg)
    )


def _advertised_from_agent(agent):
    msg = getattr(agent, "_original_system_message", None) or agent.system_message
    return set(_advertised_names(msg.content))


@pytest.fixture
def template():
    """Dynamic-worker template from build_workforce with search stubbed."""

    def fake_search_web(query, *args, **kwargs):
        return []

    fake_search_web.__name__ = "web_search"
    fake_search_web.__doc__ = "Search the web for a query."
    return _build_template(fake_search_web)


def test_task_based_tool_selection(template) -> None:
    """Execution tools are chosen per SUBTASK, never per role.

    The failure being prevented: a report-writing subtask called
    execute_code to print prose with a 16,466-character `code` argument,
    which overran the completion budget and truncated the tool-call JSON.

    The rule is asymmetric on purpose -- withholding a needed tool is
    recoverable (the honesty caveat makes the worker say it could not
    compute the result), while wrongly granting it reproduces the failure.
    So execution wins ambiguity.
    """
    # --- the pure decision function, independent of any agent ----------
    analysis_tasks = [
        "Conduct a production readiness assessment for the FastAPI service's "
        "security. Provide a written assessment with findings and "
        "recommendations.",
        "Assess PostgreSQL schema design, indexing and replication.",
        "Review the deployment and DevOps posture and summarize the findings.",
        "Explain the tradeoffs between the two caching strategies.",
    ]
    execution_tasks = [
        "Run the project test suite and report which tests fail.",
        "Calculate the p99 latency from the attached measurements.",
        "Write a script that converts the CSV export into JSON.",
        "Debug why the migration fails and reproduce the error.",
    ]
    for text in analysis_tasks:
        check(
            f"analysis task withholds execution: {text[:44]!r}",
            camel_service.subtask_needs_execution_tools(text) is False,
        )
    for text in execution_tasks:
        check(
            f"execution task keeps them: {text[:44]!r}",
            camel_service.subtask_needs_execution_tools(text) is True,
        )

    # Ambiguity and absence must preserve capability, not remove it.
    check(
        "an unrecognised subtask keeps the execution tools",
        camel_service.subtask_needs_execution_tools("Handle the widget for Q3.")
        is True,
    )
    check(
        "empty task text keeps the execution tools",
        camel_service.subtask_needs_execution_tools("") is True,
    )
    check(
        "a task with BOTH signals keeps them (execution wins)",
        camel_service.subtask_needs_execution_tools(
            "Analyze the logs and compute the error rate."
        )
        is True,
    )

    # --- nothing about the decision may depend on the role -------------
    prose = "Write a security assessment with findings."
    compute = "Compute the checksum of each artifact."
    roles = ("Security Agent", "Coding Agent", "Data Analyst", "Research Agent")
    prose_tools = {r: sorted(_agent_for(template, prose, role=r)._internal_tools) for r in roles}
    compute_tools = {
        r: sorted(_agent_for(template, compute, role=r)._internal_tools) for r in roles
    }
    check(
        "the same subtask yields the same tools for every role (no role mapping)",
        len({tuple(v) for v in prose_tools.values()}) == 1
        and len({tuple(v) for v in compute_tools.values()}) == 1,
        f"prose={prose_tools} compute={compute_tools}",
    )
    check(
        "the same ROLE yields different tools for different subtasks",
        prose_tools["Coding Agent"] != compute_tools["Coding Agent"],
        f"{prose_tools['Coding Agent']} vs {compute_tools['Coding Agent']}",
    )

    # --- what actually gets bound, through the real creation path ------
    analysis_agent = _agent_for(template, analysis_tasks[0])
    execution_agent = _agent_for(template, execution_tasks[0])
    analysis_bound = set(analysis_agent._internal_tools)
    execution_bound = set(execution_agent._internal_tools)

    check(
        "an analysis worker gets no execution tools",
        not (set(camel_service.EXECUTION_TOOL_NAMES) & analysis_bound),
        sorted(analysis_bound),
    )
    check(
        "an execution worker still gets them (capability preserved)",
        set(camel_service.EXECUTION_TOOL_NAMES) <= execution_bound,
        sorted(execution_bound),
    )
    check(
        "web tools stay off dynamic workers either way",
        "web_search" not in analysis_bound
        and "web_read" not in analysis_bound
        and "web_search" not in execution_bound
        and "web_read" not in execution_bound,
        f"analysis={sorted(analysis_bound)} execution={sorted(execution_bound)}",
    )

    # The roster must name exactly what is bound. Advertising a tool that
    # was pruned is worse than advertising none: CAMEL resolves tool names
    # with a bare dict lookup, so an unbound name is an uncaught KeyError
    # that fails the entire subtask.
    for label, agent in (("analysis", analysis_agent), ("execution", execution_agent)):
        bound = set(agent._internal_tools)
        advertised = _advertised_from_agent(agent)
        check(
            f"{label} worker's roster matches its real bindings exactly",
            bound == advertised,
            f"bound={sorted(bound)} advertised={sorted(advertised)}",
        )

    # --- the stash must not leak between workers -----------------------
    original = camel_service._ORIGINAL_CREATE_WORKER_NODE_FOR_TASK
    seen = []

    async def _record(self, task):
        seen.append(getattr(self, "_dynamic_worker_task_content", None))
        return "node"

    class _Holder:
        pass

    holder = _Holder()
    camel_service._ORIGINAL_CREATE_WORKER_NODE_FOR_TASK = _record
    try:
        asyncio.run(
            camel_service._patched_create_worker_node_for_task(
                holder, Task(content="Run the migration.", id="t1")
            )
        )
        check(
            "the subtask is visible during creation",
            seen[-1] == "Run the migration.",
            repr(seen[-1]),
        )
        check(
            "the subtask is cleared after creation",
            holder._dynamic_worker_task_content == "",
            repr(holder._dynamic_worker_task_content),
        )

        async def _boom(self, task):
            raise RuntimeError("creation failed")

        camel_service._ORIGINAL_CREATE_WORKER_NODE_FOR_TASK = _boom
        try:
            asyncio.run(
                camel_service._patched_create_worker_node_for_task(
                    holder, Task(content="Assess security.", id="t2")
                )
            )
        except RuntimeError:
            pass
        check(
            "the subtask is cleared even when creation raises",
            holder._dynamic_worker_task_content == "",
            repr(holder._dynamic_worker_task_content),
        )
    finally:
        camel_service._ORIGINAL_CREATE_WORKER_NODE_FOR_TASK = original

    check(
        "the task-recording patch is actually installed",
        camel_service.Workforce._create_worker_node_for_task
        is camel_service._patched_create_worker_node_for_task,
    )

    # --- unrelated policy must be untouched ----------------------------
    def max_tokens_of(agent):
        backend = getattr(agent.model_backend, "current_model", agent.model_backend)
        backend = getattr(backend, "_backend", backend)
        return backend.model_config_dict.get("max_tokens")

    for label, agent in (("analysis", analysis_agent), ("execution", execution_agent)):
        check(
            f"{label} worker keeps the 8192 completion budget",
            max_tokens_of(agent) == camel_service.DYNAMIC_WORKER_MAX_TOKENS,
            f"got {max_tokens_of(agent)}",
        )
        check(
            f"{label} worker keeps its step timeout",
            agent.step_timeout == camel_service.WORKFORCE_STEP_TIMEOUT_SECONDS,
            f"got {agent.step_timeout}",
        )


if __name__ == "__main__":
    main()
