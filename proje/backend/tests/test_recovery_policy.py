"""Tests for the strategy-aware quality/recovery policy and the bounded
planning path (camel_service._patched_analyze_task / _patched_decompose_task
/ _patched_get_node_info).

No model or network access: the underlying CAMEL call is stubbed, so these
exercise the real policy branches rather than model behavior. Run directly:
    python test_recovery_policy.py
"""

import asyncio

import camel_service
import workforce_plan
from camel.societies.workforce.utils import RecoveryStrategy, TaskAnalysisResult
from camel.tasks import Task


class _FakeWorkforce:
    """Only the attributes the patched methods actually read."""

    def __init__(self, execution_strategy="workforce"):
        self.execution_strategy = execution_strategy
        self.task_agent = object()  # no tracked model -> stage override no-ops
        self.coordinator_agent = object()


def _task(result: str, failure_count: int = 0, content: str = "do the thing") -> Task:
    t = Task(content=content, id="t1")
    t.result = result
    t.failure_count = failure_count
    return t


def _run_analyze(workforce, task, analysis, *, for_failure=False):
    """Call the patched _analyze_task with the vendor call stubbed out."""
    original = camel_service._ORIGINAL_ANALYZE_TASK
    seen = {}

    def fake_original(self, task, *, for_failure, error_message=None):
        # Capture what the vendor layer would have been handed, so the
        # context-bounding behavior is observable.
        seen["content"] = task.content
        seen["result"] = task.result
        return analysis

    camel_service._ORIGINAL_ANALYZE_TASK = fake_original
    try:
        out = camel_service._patched_analyze_task(
            workforce, task, for_failure=for_failure, error_message=None
        )
    finally:
        camel_service._ORIGINAL_ANALYZE_TASK = original
    return out, seen


def main() -> None:
    failures = []

    def check(name, condition, detail=""):
        print(f"[{'PASS' if condition else 'FAIL'}] {name}"
              + (f" -- {detail}" if not condition else ""))
        if not condition:
            failures.append(name)

    good_result = "A" * 800  # comfortably above FAST_PATH_USABLE_RESULT_CHARS
    escalation = TaskAnalysisResult(
        reasoning="weak",
        quality_score=40,
        recovery_strategy=RecoveryStrategy.REASSIGN,
        issues=["not detailed enough"],
    )

    # --- SPECIALIST: usable result must be accepted, never escalated ---
    out, _ = _run_analyze(
        _FakeWorkforce("specialist"), _task(good_result), escalation
    )
    check(
        "specialist + usable result -> no recovery (no escalation)",
        out.recovery_strategy is None and out.quality_score >= 70,
        f"{out.recovery_strategy=} {out.quality_score=}",
    )

    # --- DIRECT: same policy ---
    out, _ = _run_analyze(_FakeWorkforce("direct"), _task(good_result), escalation)
    check(
        "direct + usable result -> no recovery",
        out.recovery_strategy is None,
        str(out.recovery_strategy),
    )

    # --- SPECIALIST: unusable result gets exactly ONE repair, as RETRY ---
    out, _ = _run_analyze(_FakeWorkforce("specialist"), _task("tiny"), escalation)
    check(
        "specialist + unusable result -> one bounded repair, downgraded to retry",
        out.recovery_strategy == RecoveryStrategy.RETRY
        and out.modified_task_content is None,
        str(out.recovery_strategy),
    )

    # --- SPECIALIST: repair budget spent -> stop, do not escalate ---
    out, _ = _run_analyze(
        _FakeWorkforce("specialist"),
        _task("tiny", failure_count=camel_service.FAST_PATH_MAX_REPAIRS),
        escalation,
    )
    check(
        "specialist + repair budget exhausted -> stop, no escalation",
        out.recovery_strategy is None,
        str(out.recovery_strategy),
    )

    # --- The predetermined second quality call is skipped entirely ------
    #
    # Once a fast-path job has spent its one repair, every branch accepts
    # the result regardless of the quality model's verdict -- so making the
    # call at all is pure cost. This is the second of the two "quality"
    # calls on a specialist research job; skipping it must not change the
    # outcome, only the call count.
    for strategy in ("specialist", "direct"):
        out, seen = _run_analyze(
            _FakeWorkforce(strategy),
            _task("tiny", failure_count=camel_service.FAST_PATH_MAX_REPAIRS),
            escalation,
        )
        check(
            f"{strategy} + budget spent -> quality model is NOT called again",
            seen == {},
            f"vendor analyze was called with {seen!r}",
        )
        check(
            f"{strategy} + budget spent -> result accepted as sufficient",
            out.quality_sufficient and out.recovery_strategy is None,
            f"{out.quality_score=} {out.recovery_strategy=}",
        )

    # A fast-path job on its FIRST quality check must still be evaluated --
    # the short-circuit only applies after the repair budget is spent.
    out, seen = _run_analyze(
        _FakeWorkforce("specialist"), _task("tiny", failure_count=0), escalation
    )
    check(
        "specialist first quality check still calls the quality model",
        seen != {},
        "vendor analyze was skipped on the first check",
    )

    # Failure analysis is a different decision and keeps its model call.
    out, seen = _run_analyze(
        _FakeWorkforce("specialist"),
        _task("tiny", failure_count=camel_service.FAST_PATH_MAX_REPAIRS),
        escalation,
        for_failure=True,
    )
    check(
        "specialist failure analysis is never short-circuited",
        seen != {},
        "failure analysis was skipped",
    )

    # WORKFORCE keeps every quality call it makes today.
    out, seen = _run_analyze(
        _FakeWorkforce("workforce"),
        _task("tiny", failure_count=camel_service.FAST_PATH_MAX_REPAIRS),
        escalation,
    )
    check(
        "workforce still runs its quality check after a repair",
        seen != {},
        "workforce quality check was skipped",
    )

    # --- CAMEL's quality sentinel must not count as a usable result ---
    out, _ = _run_analyze(
        _FakeWorkforce("specialist"),
        _task("Quality insufficient (score: 20). Issues: " + "x" * 500),
        escalation,
    )
    check(
        "specialist + prior quality-failure sentinel is not 'usable'",
        out.recovery_strategy == RecoveryStrategy.RETRY,
        str(out.recovery_strategy),
    )

    # --- WORKFORCE: recovery is restricted to same-task repair ----------
    #
    # RETRY and REPLAN repost the SAME Task object (same id, same
    # accumulated failure_count), so every attempt counts toward the one
    # MAX_TASK_ATTEMPTS ceiling. Everything else either manufactures a
    # fresh-budget unit of work (decompose/create_worker) or spends a
    # coordinator call for no new capability (reassign), so it is
    # downgraded to a plain retry.
    passthrough = [
        (
            "retry",
            TaskAnalysisResult(
                reasoning="transient",
                quality_score=40,
                recovery_strategy=RecoveryStrategy.RETRY,
                issues=["flaky"],
            ),
        ),
        (
            "replan",
            TaskAnalysisResult(
                reasoning="wording",
                quality_score=40,
                recovery_strategy=RecoveryStrategy.REPLAN,
                modified_task_content="clearer wording",
                issues=["ambiguous"],
            ),
        ),
    ]
    for label, analysis in passthrough:
        out, _ = _run_analyze(
            _FakeWorkforce("workforce"), _task("tiny"), analysis
        )
        check(
            f"workforce {label} passes through unchanged",
            out.recovery_strategy == analysis.recovery_strategy,
            str(out.recovery_strategy),
        )
    # REPLAN is the only strategy allowed to rewrite the task's content.
    out, _ = _run_analyze(
        _FakeWorkforce("workforce"), _task("tiny"), passthrough[1][1]
    )
    check(
        "workforce replan keeps its modified_task_content",
        out.modified_task_content == "clearer wording",
        str(out.modified_task_content),
    )

    for label, strategy_value in [
        ("reassign", RecoveryStrategy.REASSIGN),
        ("decompose", RecoveryStrategy.DECOMPOSE),
        ("create_worker", RecoveryStrategy.CREATE_WORKER),
    ]:
        analysis = TaskAnalysisResult(
            reasoning="escalate",
            quality_score=40,
            recovery_strategy=strategy_value,
            modified_task_content="a rewritten task",
            issues=["weak"],
        )
        out, _ = _run_analyze(
            _FakeWorkforce("workforce"), _task("tiny"), analysis
        )
        check(
            f"workforce {label} is downgraded to retry",
            out.recovery_strategy == RecoveryStrategy.RETRY,
            str(out.recovery_strategy),
        )
        check(
            f"workforce {label} downgrade clears modified_task_content",
            out.modified_task_content is None,
            str(out.modified_task_content),
        )

    # Failure analysis (timeouts, execution errors) is bounded the same way
    # -- that is the path a create_worker escalation actually arrives on.
    out, _ = _run_analyze(
        _FakeWorkforce("workforce"),
        _task("tiny"),
        TaskAnalysisResult(
            reasoning="no worker fits",
            recovery_strategy=RecoveryStrategy.CREATE_WORKER,
            issues=["timeout"],
        ),
        for_failure=True,
    )
    check(
        "workforce failure-analysis create_worker is downgraded to retry",
        out.recovery_strategy == RecoveryStrategy.RETRY,
        str(out.recovery_strategy),
    )

    # A sufficient result must not be given a recovery strategy it never had.
    out, _ = _run_analyze(
        _FakeWorkforce("workforce"),
        _task(good_result),
        TaskAnalysisResult(reasoning="fine", quality_score=91, recovery_strategy=None),
    )
    check(
        "workforce sufficient result stays recovery-free",
        out.recovery_strategy is None and out.quality_score == 91,
        f"{out.recovery_strategy=} {out.quality_score=}",
    )

    # The restriction is WORKFORCE-only: the fast paths keep their own
    # policy, which already declines escalation for its own reasons.
    for fast in ("specialist", "direct"):
        out, _ = _run_analyze(
            _FakeWorkforce(fast), _task(good_result), escalation
        )
        check(
            f"{fast} policy is unaffected by the workforce restriction",
            out.recovery_strategy is None and out.quality_score >= 70,
            f"{out.recovery_strategy=} {out.quality_score=}",
        )

    # --- Quality checks still RUN (not disabled) and still score ---
    sufficient = TaskAnalysisResult(
        reasoning="good", quality_score=88, recovery_strategy=None
    )
    out, seen = _run_analyze(
        _FakeWorkforce("specialist"), _task(good_result), sufficient
    )
    check(
        "quality check still runs and its passing score is preserved",
        out.quality_score == 88 and out.recovery_strategy is None,
        f"{out.quality_score=}",
    )

    # --- Context bounding: management layer sees a truncated view ---
    huge = "B" * 50_000
    _, seen = _run_analyze(
        _FakeWorkforce("workforce"),
        _task(huge, content=huge),
        sufficient,
    )
    check(
        "analysis sees bounded task content",
        len(seen["content"]) < 50_000
        and len(seen["content"]) <= camel_service.MAX_ANALYSIS_CONTENT_CHARS + 80,
        f"len={len(seen['content'])}",
    )
    check(
        "analysis sees bounded task result",
        len(seen["result"]) <= camel_service.MAX_ANALYSIS_RESULT_CHARS + 80,
        f"len={len(seen['result'])}",
    )

    # --- ...and the real task is restored afterwards ---
    t = _task(huge, content=huge)
    _run_analyze(_FakeWorkforce("workforce"), t, sufficient)
    check(
        "original task content/result restored after analysis",
        t.content == huge and t.result == huge,
        f"content={len(t.content)} result={len(t.result)}",
    )

    # --- Decomposition: initial plan is structured; XML clip is non-initial ---
    original_plan = workforce_plan.plan_request
    seen_plan = {}

    def fake_plan(request, *, agent=None, required=None):
        seen_plan["request"] = request
        return original_plan(request, agent=None, required=required)

    workforce_plan.plan_request = fake_plan
    try:
        original_request = "kısa istek"
        root = Task(
            content=huge,
            id="r-initial",
            additional_info={
                camel_service.WORKFORCE_INTENT_KEY: {
                    "original_request": original_request,
                    "required_capabilities": ["web_search"],
                }
            },
        )
        initial_subtasks = camel_service._patched_decompose_task(
            _FakeWorkforce(), root
        )
    finally:
        workforce_plan.plan_request = original_plan

    check(
        "initial planner receives original_request, not the history blob",
        seen_plan.get("request") == original_request,
        repr(seen_plan.get("request"))[:80],
    )
    check(
        "initial structured plan stays within MAX_INITIAL_DECOMPOSITION_SUBTASKS",
        len(initial_subtasks) <= camel_service.MAX_INITIAL_DECOMPOSITION_SUBTASKS,
        f"got {len(initial_subtasks)}",
    )
    check(
        "initial plan leaves root task content untruncated",
        root.content == huge,
        f"len={len(root.content)}",
    )

    original_decompose = camel_service._ORIGINAL_DECOMPOSE_TASK
    seen_decompose = {}

    def fake_decompose(self, task):
        seen_decompose["content"] = task.content
        return [Task(content=f"sub{i}", id=f"s{i}") for i in range(12)]

    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    try:
        t = _task("r", content=huge)
        later = _FakeWorkforce()
        later._initial_decomposition_done = True
        subtasks = camel_service._patched_decompose_task(later, t)
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original_decompose

    check(
        "non-initial decomposition clipped to MAX_SUBTASKS_PER_DECOMPOSITION",
        len(subtasks) == camel_service.MAX_SUBTASKS_PER_DECOMPOSITION,
        f"got {len(subtasks)}",
    )
    check(
        "non-initial planner sees bounded content",
        len(seen_decompose["content"])
        <= camel_service.MAX_PLANNER_CONTENT_CHARS + 80,
        f"len={len(seen_decompose['content'])}",
    )
    check(
        "original content restored after non-initial decomposition",
        t.content == huge,
        f"len={len(t.content)}",
    )

    # --- Capability info is always explicit for the coordinator ---
    class _W:
        def __init__(self, tools):
            self.tool_dict = tools

    class _Node:
        def __init__(self, tools):
            self.worker = _W(tools)

    original_info = camel_service._ORIGINAL_GET_NODE_INFO
    camel_service._ORIGINAL_GET_NODE_INFO = lambda self, node: ""
    try:
        searcher = camel_service._patched_get_node_info(
            None, _Node({"web_search": object(), "web_read": object()})
        )
        toolless = camel_service._patched_get_node_info(None, _Node({}))
    finally:
        camel_service._ORIGINAL_GET_NODE_INFO = original_info

    check(
        "search-capable worker advertises the capability",
        "CAN acquire web information" in searcher,
        searcher,
    )
    check(
        "tool-less worker explicitly advertises the LACK of it "
        "(CAMEL returns an empty string here)",
        "CANNOT search the web" in toolless,
        toolless,
    )

    # --- Assignment-time NONE_FIT still creates a dynamic worker --------
    #
    # The recovery restriction above must NOT reach this path. A task the
    # coordinator marks NONE_FIT is handled during ASSIGNMENT, through
    # _patched_handle_assignment_retry_and_fallback ->
    # _handle_task_assignment_fallbacks, and never goes through
    # _analyze_task at all -- so dynamic worker creation for a genuinely
    # out-of-domain task remains available.
    class _FakeAssignment:
        def __init__(self, task_id, assignee_id):
            self.task_id = task_id
            self.assignee_id = assignee_id

        # The patched handler rebuilds a repaired assignment the same way
        # CAMEL's pydantic TaskAssignment would.
        def model_copy(self, update=None):
            copy = _FakeAssignment(self.task_id, self.assignee_id)
            for key, value in (update or {}).items():
                setattr(copy, key, value)
            return copy

    class _AssignmentWorkforce:
        def __init__(self):
            self.fallback_tasks = []

        async def _handle_task_assignment_fallbacks(self, tasks):
            self.fallback_tasks.append([t.id for t in tasks])
            return [_FakeAssignment(t.id, "dynamic-worker-1") for t in tasks]

    original_retry_path = camel_service._ORIGINAL_HANDLE_ASSIGNMENT_RETRY_AND_FALLBACK
    retry_path_calls = []

    async def _spy_original(self, invalid, tasks, valid_ids):
        retry_path_calls.append([a.assignee_id for a in invalid])
        return []

    camel_service._ORIGINAL_HANDLE_ASSIGNMENT_RETRY_AND_FALLBACK = _spy_original
    try:
        wf = _AssignmentWorkforce()
        sentinel_task = Task(content="translate this", id="t-nf")
        assignments = asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                wf,
                [_FakeAssignment("t-nf", camel_service.NONE_FIT_SENTINEL)],
                [sentinel_task],
                {"worker-a"},
            )
        )
        check(
            "NONE_FIT still routes to dynamic worker creation",
            wf.fallback_tasks == [["t-nf"]],
            str(wf.fallback_tasks),
        )
        check(
            "NONE_FIT produces a real assignment",
            [a.assignee_id for a in assignments] == ["dynamic-worker-1"],
            str([a.assignee_id for a in assignments]),
        )
        check(
            "NONE_FIT skips the retry-with-feedback path",
            retry_path_calls == [],
            str(retry_path_calls),
        )

        # An id that is merely mangled -- the roster's angle brackets came
        # along with it, or the case changed -- names exactly one real
        # worker, so it is repaired locally. This is the case CAMEL's
        # retry-with-feedback existed to rescue; doing it here costs no
        # model call and cannot come back with a different answer.
        for label, mangled in (
            ("angle brackets", "<worker-a>"),
            ("case", "WORKER-A"),
            ("padding", "  worker-a "),
        ):
            retry_path_calls.clear()
            wf_fix = _AssignmentWorkforce()
            repaired = asyncio.run(
                camel_service._patched_handle_assignment_retry_and_fallback(
                    wf_fix,
                    [_FakeAssignment("t-r", mangled)],
                    [Task(content="x", id="t-r")],
                    {"worker-a"},
                )
            )
            check(
                f"mangled assignee id ({label}) is repaired with no retry call",
                [a.assignee_id for a in repaired] == ["worker-a"]
                and retry_path_calls == []
                and wf_fix.fallback_tasks == [],
                f"{[a.assignee_id for a in repaired]=} {retry_path_calls=} "
                f"{wf_fix.fallback_tasks=}",
            )

        # An id that names nothing is treated as an abstention: straight to
        # worker creation, no second coordinator call. CHANGED BEHAVIOUR --
        # this used to be delegated to CAMEL's retry-with-feedback. An
        # instrumented run showed that retry re-asking about three tasks
        # and all three still ending up on dynamic workers, and the prompt
        # this deployment ships already offers an explicit NONE_FIT option,
        # so an unrepairable id is a malformed abstention rather than a
        # near-miss worth a model call.
        retry_path_calls.clear()
        wf2 = _AssignmentWorkforce()
        asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                wf2,
                [_FakeAssignment("t-x", "not-a-real-worker")],
                [Task(content="x", id="t-x")],
                {"worker-a"},
            )
        )
        check(
            "an unrepairable worker id goes straight to worker creation, no retry",
            retry_path_calls == [] and wf2.fallback_tasks == [["t-x"]],
            f"{retry_path_calls=} {wf2.fallback_tasks=}",
        )

        # Ambiguity is never guessed: a candidate that could name either of
        # two workers is an abstention, not a coin flip.
        retry_path_calls.clear()
        wf3 = _AssignmentWorkforce()
        asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                wf3,
                [_FakeAssignment("t-a", "worker")],
                [Task(content="x", id="t-a")],
                {"worker-a", "worker-b"},
            )
        )
        check(
            "an ambiguous id is not guessed at",
            retry_path_calls == [] and wf3.fallback_tasks == [["t-a"]],
            f"{retry_path_calls=} {wf3.fallback_tasks=}",
        )

        # The one case the retry is still right for: CAMEL passes an empty
        # invalid list when the coordinator returned NO assignments at all.
        # That is a transport/formatting failure with no judgment to
        # respect, so the original retry path is preserved untouched.
        retry_path_calls.clear()
        wf4 = _AssignmentWorkforce()
        asyncio.run(
            camel_service._patched_handle_assignment_retry_and_fallback(
                wf4, [], [Task(content="x", id="t-e")], {"worker-a"}
            )
        )
        check(
            "an empty coordinator response still uses the original retry path",
            retry_path_calls == [[]] and wf4.fallback_tasks == [],
            f"{retry_path_calls=} {wf4.fallback_tasks=}",
        )
    finally:
        camel_service._ORIGINAL_HANDLE_ASSIGNMENT_RETRY_AND_FALLBACK = original_retry_path

    print(f"\n{'FAILED -- see above' if failures else 'All recovery-policy tests passed'}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
