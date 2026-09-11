"""Tests for the parallel-Workforce runtime bounds.

Context: with the bounded 2-4 decomposition now working, several dynamic
workers run at once. Against an inference backend that serializes
requests, they queue behind each other, so a timeout sized for one task
fires on work that was never stuck -- and a false timeout feeds
RETRY/REPLAN, which puts another full worker run into the same queue and
makes the remaining workers likelier to time out too.

WORKFORCE_TASK_TIMEOUT_SECONDS bounds that. These tests pin the
derivation (not the number), that it covers the serialized worst case for
a full parallel batch, and that it actually reaches CAMEL.

They also pin the iteration caps, which measurement says must NOT be
lowered as an "optimization" -- see test_iteration_caps_unchanged.

No model or network. Run directly:
    python test_worker_runtime_policy.py
"""

import asyncio
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def _worst_case_iterations() -> int:
    """The turns the SLOWEST worker may take, not the generic worker's.

    Search-capable workers get their own, higher ceiling. Deriving the
    timeout from the generic cap would understate it for precisely the
    jobs that come closest to it.
    """
    return max(
        camel_service.WORKER_MAX_ITERATIONS,
        camel_service.SEARCH_WORKER_MAX_ITERATIONS,
    )


def _expected(subtasks=None, iterations=None, per_call=None) -> float:
    return float(
        (subtasks or camel_service.MAX_INITIAL_DECOMPOSITION_SUBTASKS)
        * (iterations or _worst_case_iterations())
        * (per_call or camel_service.WORST_CASE_MODEL_CALL_SECONDS)
    )


def test_timeout_is_derived_not_hardcoded() -> None:
    """The bound must track its inputs, not coincidentally match a number."""

    check(
        "timeout equals subtasks x iterations x worst-case call",
        camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS == _expected(),
        f"{camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS} != {_expected()}",
    )

    # The whole point of deriving it: raising either cap must widen the
    # bound. If someone re-hardcodes it, these recomputations diverge and
    # the false-timeout/retry-storm loop comes back silently.
    check(
        "a larger decomposition cap would widen the bound",
        _expected(subtasks=camel_service.MAX_INITIAL_DECOMPOSITION_SUBTASKS + 1)
        > camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS,
    )
    check(
        "a larger iteration cap would widen the bound",
        _expected(iterations=_worst_case_iterations() + 1)
        > camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS,
    )
    check(
        "a slower backend would widen the bound",
        _expected(per_call=camel_service.WORST_CASE_MODEL_CALL_SECONDS + 5)
        > camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS,
    )


def test_timeout_covers_the_serialized_worst_case() -> None:
    """It must cover a whole parallel batch, not one task's own work."""

    one_worker_worst = (
        _worst_case_iterations() * camel_service.WORST_CASE_MODEL_CALL_SECONDS
    )
    check(
        "bound exceeds a single worker's worst case",
        camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS > one_worker_worst,
        f"{camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS} vs {one_worker_worst}",
    )
    check(
        "bound covers the full parallel batch queueing behind each other",
        camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS
        >= one_worker_worst * camel_service.MAX_INITIAL_DECOMPOSITION_SUBTASKS,
    )

    # Sanity against the slowest real run in the bench corpus (scenario F,
    # 19 calls / 306.2s). The bound has to sit comfortably above a real
    # observed job, or it would fire on work that genuinely completes.
    check(
        "bound is above the slowest measured real run (306.2s)",
        camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS > 306.2,
        f"{camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS}",
    )

    # Empirical input must stay empirical: 25 rounds up the 24.0s
    # worst single-call measurement. A value at or below the measured
    # sustained rate (16.1s) would mean the bound no longer covers what
    # we have actually seen.
    check(
        "worst-case call estimate still covers measured worst call (24.0s)",
        camel_service.WORST_CASE_MODEL_CALL_SECONDS >= 24,
        f"{camel_service.WORST_CASE_MODEL_CALL_SECONDS}",
    )


def test_timeout_reaches_camel() -> None:
    """A derived constant nothing passes to CAMEL would be decoration."""

    captured = {}

    class _FakeWorkforce:
        """Only what build_workforce touches after construction."""

        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            # Real Workforce.__init__ REBUILDS these two from the passed-in
            # agents (workforce.py:381-403, 448-469) and exposes them under
            # these names; build_workforce then applies the settings the
            # rebuild drops. Modelled here so that step has something to
            # act on -- see _configure_management_agent.
            self.coordinator_agent = kwargs.get("coordinator_agent")
            self.task_agent = kwargs.get("task_agent")
            self._children = []

        def add_single_agent_worker(self, description, worker=None, **kwargs):
            self._children.append(worker)
            return self

    original = camel_service.Workforce
    camel_service.Workforce = _FakeWorkforce
    try:
        camel_service.build_workforce(
            callback=object(),
            tracker=camel_service.UsageTracker(),
        )
    finally:
        camel_service.Workforce = original

    check(
        "build_workforce passes task_timeout_seconds to Workforce",
        "task_timeout_seconds" in captured,
        f"got kwargs {sorted(captured)}",
    )
    check(
        "the value passed is the derived bound",
        captured.get("task_timeout_seconds")
        == camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS,
        f"got {captured.get('task_timeout_seconds')}",
    )


def test_step_timeout_reaches_every_agent() -> None:
    """No agent may be left on ChatAgent's silent 180s step_timeout.

    This is a SECOND deadline, independent of the transport one and strictly
    outer to it: step_timeout wraps the whole astep in an asyncio.wait_for
    (chat_agent.py:3040-3051) while the HTTP timeout bounds one request
    inside it. Raising only the transport value changed nothing observable,
    because the 180s asyncio guard still fired first.

    The subtle part, and the reason this test asserts against the REAL
    creation path rather than the template: ChatAgent.clone
    (chat_agent.py:5491-5515) forwards max_iteration and
    tool_execution_timeout but silently DROPS step_timeout, so a dynamic
    worker cloned from a correctly-configured template still came back at
    180.0s. Testing the template alone would have passed while production
    stayed broken.
    """
    from camel.utils.constants import Constants

    default = Constants.TIMEOUT_THRESHOLD
    bound = camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS

    captured = {}
    fixed_workers = []

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
            fixed_workers.append(worker)
            return self

    original = camel_service.Workforce
    camel_service.Workforce = _FakeWorkforce
    try:
        camel_service.build_workforce(
            callback=object(), tracker=camel_service.UsageTracker()
        )
    finally:
        camel_service.Workforce = original

    check(
        "the step-timeout constant clears the orchestration bound",
        camel_service.WORKFORCE_STEP_TIMEOUT_SECONDS >= bound,
        f"{camel_service.WORKFORCE_STEP_TIMEOUT_SECONDS} < {bound}",
    )
    check(
        "the step-timeout constant is not the vendor default",
        camel_service.WORKFORCE_STEP_TIMEOUT_SECONDS != default,
        f"still {default}",
    )

    for name, agent in (
        ("coordinator", captured.get("coordinator_agent")),
        ("planner", captured.get("task_agent")),
    ):
        check(
            f"{name} is off the vendor default",
            agent is not None and agent.step_timeout != default,
            f"{name} step_timeout={getattr(agent, 'step_timeout', None)}",
        )

    for index, worker in enumerate(fixed_workers):
        agent = getattr(worker, "worker", None) or getattr(worker, "_agent", None)
        if agent is None:
            continue
        check(
            f"fixed worker {index} is off the vendor default",
            agent.step_timeout != default,
            f"step_timeout={agent.step_timeout}",
        )

    # The one that actually regressed: exercise _patched_create_new_agent,
    # which is how a dynamic worker is really built, instead of trusting the
    # template it clones from.
    class _TemplateHolder:
        new_worker_agent = captured.get("new_worker_agent")

    dynamic = asyncio.run(
        camel_service._patched_create_new_agent(
            _TemplateHolder(), "Security Agent", "You assess security."
        )
    )
    check(
        "a dynamic worker built through the real path is off the default",
        dynamic.step_timeout != default,
        f"step_timeout={dynamic.step_timeout} (clone drops the template's value)",
    )
    check(
        "a dynamic worker's step timeout clears the orchestration bound",
        dynamic.step_timeout >= bound,
        f"{dynamic.step_timeout} < {bound}",
    )
    # Guard the wiring that made this necessary: if a future CAMEL bump
    # starts forwarding step_timeout through clone, this stays true; if the
    # re-apply in _patched_create_new_agent is removed, it fails.
    template = captured.get("new_worker_agent")
    bare_clone = template.clone(with_memory=False)
    check(
        "the gap this guards against is real (bare clone drops it)",
        bare_clone.step_timeout == default or bare_clone.step_timeout >= bound,
        f"bare clone step_timeout={bare_clone.step_timeout}",
    )


def test_dynamic_worker_completion_budget() -> None:
    """Dynamic workers need a bigger completion budget than everyone else.

    Not for answer length -- for a failure mode. When a completion is cut by
    max_tokens while the model is mid-tool-call, the cut lands inside the
    `arguments` JSON string. CAMEL parses that with a bare, unguarded
    json.loads (chat_agent.py:3777, _handle_batch_response); the
    JSONDecodeError rides through _get_model_response's
    `except Exception: raise` and is caught only by
    Worker._process_single_task (worker.py:98-102), which fails the whole
    subtask with "JSONDecodeError: Unterminated string starting at ...".
    Observed live on three independent dynamic workers in one run.

    Measured against the real configured model with the actual failing
    subtask prompts: 4096 produced 3769 completion tokens (8% headroom),
    and an 8192 run produced 5752 -- which exceeds 4096 by 40%, so that
    call would have truncated at the old ceiling.

    finish_reason cannot be used to detect this: the provider returns
    "tool_calls", not "length", even when it truncates mid-argument.
    Verified directly. So the budget is the only lever.
    """
    # The largest completion actually measured on a real subtask prompt.
    MEASURED_MAX_COMPLETION_TOKENS = 5752
    # What every other stage uses (model_config.get_model's default).
    APP_DEFAULT_MAX_TOKENS = 4096

    budget = camel_service.DYNAMIC_WORKER_MAX_TOKENS

    check(
        "budget clears the largest measured completion",
        budget > MEASURED_MAX_COMPLETION_TOKENS,
        f"{budget} <= {MEASURED_MAX_COMPLETION_TOKENS}",
    )
    check(
        "budget keeps real headroom over it (>=20%)",
        budget >= MEASURED_MAX_COMPLETION_TOKENS * 1.2,
        f"{budget} < {MEASURED_MAX_COMPLETION_TOKENS * 1.2:.0f}",
    )
    # Pins WHY this constant exists: the previous ceiling is provably below
    # a real observed completion. If someone reverts it, this states the
    # consequence rather than just failing an equality.
    check(
        "the old default was genuinely insufficient (this is not cosmetic)",
        APP_DEFAULT_MAX_TOKENS < MEASURED_MAX_COMPLETION_TOKENS,
        f"{APP_DEFAULT_MAX_TOKENS} >= {MEASURED_MAX_COMPLETION_TOKENS}",
    )

    captured = {}
    fixed_workers = []

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
            fixed_workers.append(worker)
            return self

    original = camel_service.Workforce
    camel_service.Workforce = _FakeWorkforce
    try:
        camel_service.build_workforce(
            callback=object(), tracker=camel_service.UsageTracker()
        )
    finally:
        camel_service.Workforce = original

    def max_tokens_of(agent):
        backend = getattr(agent.model_backend, "current_model", agent.model_backend)
        backend = getattr(backend, "_backend", backend)
        return backend.model_config_dict.get("max_tokens")

    template = captured["new_worker_agent"]
    check(
        "the dynamic-worker template carries the budget",
        max_tokens_of(template) == budget,
        f"got {max_tokens_of(template)}",
    )
    # The clone is what actually runs. It shares the template's model
    # backend, so the budget follows it -- unlike step_timeout, which the
    # clone drops (see test_step_timeout_reaches_every_agent).
    check(
        "a cloned dynamic worker still carries the budget",
        max_tokens_of(template.clone(with_memory=False)) == budget,
        f"got {max_tokens_of(template.clone(with_memory=False))}",
    )

    # Scoped, not global: raising this everywhere would let every stage emit
    # far longer completions than it needs, and most management stages are
    # not the ones that overrun. The planner is the one exception --
    # reliability triage (rel-20260813T105420Z-2a702b3b) found its
    # decompose call saturating the app default just as routinely as
    # dynamic_worker did (see PLANNING_MAX_TOKENS's own comment for the
    # measurements), so it now carries the same kind of scoped raise this
    # stage's own budget does, sized off the other two already-vetted
    # values rather than an unverified new number.
    check(
        "coordinator keeps the app default",
        max_tokens_of(captured["coordinator_agent"]) == APP_DEFAULT_MAX_TOKENS,
        f"got {max_tokens_of(captured['coordinator_agent'])}",
    )
    check(
        "planner is raised to PLANNING_MAX_TOKENS, not left at the app default",
        max_tokens_of(captured["task_agent"]) == camel_service.PLANNING_MAX_TOKENS,
        f"got {max_tokens_of(captured['task_agent'])}",
    )
    check(
        "the planner's raised budget is bounded, not unboundedly large",
        camel_service.PLANNING_MAX_TOKENS
        in (camel_service.DYNAMIC_WORKER_MAX_TOKENS, camel_service.SYNTHESIS_MAX_TOKENS),
        camel_service.PLANNING_MAX_TOKENS,
    )
    # build_workforce passes the ChatAgent itself as `worker=`, so these are
    # agents, not SingleAgentWorker nodes. Asserting the count first stops
    # this from silently degrading into a loop over nothing.
    check(
        "the fixed workers were captured at all",
        len(fixed_workers) >= 4,
        f"captured {len(fixed_workers)}",
    )
    for index, agent in enumerate(fixed_workers):
        check(
            f"fixed worker {index} keeps the app default",
            max_tokens_of(agent) == APP_DEFAULT_MAX_TOKENS,
            f"got {max_tokens_of(agent)}",
        )


def test_iteration_caps_unchanged() -> None:
    """Lowering the iteration cap is NOT a safe optimization here.

    Measured, from bench_result_mixed_workforce.json: 8 worker calls over
    4 quality checks is ~2 turns per worker RUN, against a ceiling of 6.
    Workers do not approach the cap -- the amplification came from repeated
    RUNS, not iteration depth -- so lowering it would reduce nothing on a
    healthy run while truncating the pathological tail straight into the
    documented failure mode: hitting the cap mid-tool-call makes CAMEL
    return the tool-call response as final (chat_agent.py:3284-3288), so
    the task ends with no text, fails quality, and triggers the very
    recovery this work exists to avoid (see SEARCH_WORKER_MAX_ITERATIONS).
    """

    check("worker iteration ceiling still 6", camel_service.WORKER_MAX_ITERATIONS == 6)
    check(
        "search-worker ceiling counts reads, not just searches",
        camel_service.SEARCH_WORKER_MAX_ITERATIONS
        == camel_service.MAX_SEARCH_CALLS_PER_AGENT
        + camel_service.MAX_READ_CALLS_PER_AGENT
        + 2,
    )
    check(
        "per-agent search cap is the agent budget, not the job-wide backstop",
        camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES
        == camel_service.MAX_SEARCH_CALLS_PER_AGENT,
    )
    # A cap of 2 is not merely tight, it is structurally wrong for any
    # lookup whose subject the agent cannot name up front. Measured on the
    # live index: vague opening queries return only hub/landing pages, and
    # the query that reaches the answer can only be written after a read
    # reveals the specifics. That needs at least search -> read -> refined
    # search -> read, so a budget below 3 forbids the refinement outright
    # and the agent reports "arama kotası doldu" instead of an answer.
    check(
        "the search budget allows a refined query after a read, not just an opening one",
        camel_service.MAX_SEARCH_CALLS_PER_AGENT >= 3,
        camel_service.MAX_SEARCH_CALLS_PER_AGENT,
    )
    check(
        "the read budget allows both the hub page and the page that answers",
        camel_service.MAX_READ_CALLS_PER_AGENT >= 3,
        camel_service.MAX_READ_CALLS_PER_AGENT,
    )
    check(
        "per-agent search cap is tighter than the job-wide search backstop",
        camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES
        < camel_service.web_tools.MAX_WEB_SEARCHES_PER_JOB,
        (
            f"agent={camel_service.MAX_INTERNAL_DISCOVERY_SEARCHES} "
            f"job={camel_service.web_tools.MAX_WEB_SEARCHES_PER_JOB}"
        ),
    )
    # The live failure this pins: a search-then-read worker that spends every
    # tool call it is allowed must still have a turn left to write the answer.
    # Without it CAMEL returns the tool-call response as final, the task has no
    # text, and the user sees "Task processing failed" instead of a result.
    bounded_tool_budget = (
        camel_service.MAX_SEARCH_CALLS_PER_AGENT
        + camel_service.MAX_READ_CALLS_PER_AGENT
    )
    check(
        "search-worker ceiling leaves a turn to answer after search AND read budgets",
        camel_service.SEARCH_WORKER_MAX_ITERATIONS >= bounded_tool_budget + 1,
        (
            f"ceiling {camel_service.SEARCH_WORKER_MAX_ITERATIONS} "
            f"vs budget {bounded_tool_budget}"
        ),
    )
    # The generic ceiling governs workers WITHOUT search (build_workforce
    # picks between the two by has_search), so it must be measured against
    # the tool calls such a worker can actually make -- comparing it to the
    # web budgets it can never spend would only force it upward for no
    # reason.
    check(
        "the generic ceiling leaves a turn to answer after a non-web worker's tool calls",
        camel_service.WORKER_MAX_ITERATIONS
        >= camel_service.MAX_READ_CALLS_PER_AGENT + 1,
        f"ceiling {camel_service.WORKER_MAX_ITERATIONS}",
    )
    # A per-job thinking budget was tried and removed: a tool return is only
    # a string, so it cannot end the agent loop, and refused workers kept
    # calling tools to the ceiling anyway. The toolkit is gone, so the
    # constant must not quietly come back with it.
    check(
        "no thinking-call budget constant lingers",
        not hasattr(camel_service, "MAX_THINKING_CALLS_PER_AGENT"),
        "MAX_THINKING_CALLS_PER_AGENT is back -- was the toolkit re-added?",
    )


def main() -> None:
    test_timeout_is_derived_not_hardcoded()
    test_timeout_covers_the_serialized_worst_case()
    test_timeout_reaches_camel()
    test_step_timeout_reaches_every_agent()
    test_dynamic_worker_completion_budget()
    test_iteration_caps_unchanged()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All worker-runtime policy checks passed")


if __name__ == "__main__":
    main()
