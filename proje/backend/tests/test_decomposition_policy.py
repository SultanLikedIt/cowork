"""Regression tests for bounded Workforce decomposition (_patched_decompose_task).

Initial decomposition uses workforce_plan (JSON), not CAMEL XML. These tests lock
down the deterministic caps and crash guards on the decompose patch itself.

    python test_decomposition_policy.py
"""

import os
import sys
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402
from camel.societies.workforce import Workforce  # noqa: E402
from camel.tasks import Task  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


CAP = camel_service.MAX_INITIAL_DECOMPOSITION_SUBTASKS
GENERAL_CAP = camel_service.MAX_SUBTASKS_PER_DECOMPOSITION


class _FakeWorkforce:
    """Stands in for the Workforce instance the patch stores its flag on."""

    def __init__(self):
        self._task_dependencies = {}
        self.task_agent = None


def _camel_decompose(workforce, produced):
    """Non-initial XML decompose that yields `produced` subtasks."""
    workforce._initial_decomposition_done = True

    def fake_decompose(self, task, _n=produced):
        return [Task(content=f"sub{i}", id=f"s{i}") for i in range(_n)]

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    try:
        return camel_service._patched_decompose_task(
            workforce, Task(content="x", id="r")
        )
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original


def test_hard_bound_in_code() -> None:
    """The 2-4 policy is enforced deterministically, not just requested."""
    import capabilities
    import workforce_plan

    check("initial-decomposition cap is 4, matching the policy", CAP == 4, f"got {CAP}")

    plan = workforce_plan.PlanResult(
        targets=[workforce_plan.PlanTarget(name=f"T{i}") for i in range(5)],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="research all of them",
                capabilities=["web_search"],
                target_names=[f"T{i}" for i in range(5)],
            )
        ],
        source="llm",
    )
    streams = workforce_plan.expand_plan_for_coverage(
        "compare them as excel",
        plan,
        {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
        max_subtasks=CAP,
    )
    packed_targets = {
        name
        for stream in streams
        if stream.capabilities & {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
        for name in stream.targets
    }
    check(
        "initial packing never exceeds the 4-task cap",
        len(streams) <= CAP,
        f"got {len(streams)}",
    )
    check(
        "clipping to 4 does not drop named targets",
        packed_targets == {f"T{i}" for i in range(5)},
        packed_targets,
    )

    for produced in (1, 2, 3, 4, 5, 8, 12):
        out = _camel_decompose(_FakeWorkforce(), produced)
        check(
            f"non-initial decomposition of {produced} subtasks -> {min(produced, GENERAL_CAP)} kept",
            len(out) == min(produced, GENERAL_CAP),
            f"got {len(out)}",
        )


def test_cap_is_scoped_to_initial_decomposition() -> None:
    """The tighter cap must not leak onto any other decomposition.

    Recovery's DECOMPOSE (workforce.py:1646) is the only other caller and
    is currently unreachable (downgraded to RETRY/REPLAN), but the scoping
    is what keeps it that way if it is ever re-enabled -- the bounded
    initial policy says nothing about recovery, so it must not silently
    govern it.
    """

    check(
        "general backstop restored to its original value",
        GENERAL_CAP == 5,
        f"got {GENERAL_CAP}",
    )
    check(
        "the two caps are genuinely separate constants",
        CAP != GENERAL_CAP,
        f"both are {CAP}",
    )

    workforce = _FakeWorkforce()
    camel_calls = []

    def fake_decompose(self, task):
        camel_calls.append(task.id)
        return [Task(content=f"sub{i}", id=f"s{i}") for i in range(8)]

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    try:
        first = camel_service._patched_decompose_task(
            workforce, Task(content="x", id="r")
        )
        second = camel_service._patched_decompose_task(
            workforce, Task(content="x", id="r2")
        )
        third = camel_service._patched_decompose_task(
            workforce, Task(content="x", id="r3")
        )
        fresh = camel_service._patched_decompose_task(
            _FakeWorkforce(), Task(content="x", id="fresh")
        )
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    check(
        "initial decomposition does not call CAMEL XML decompose",
        camel_calls == ["r2", "r3"],
        camel_calls,
    )
    check(
        "first decomposition on a workforce uses the structured plan cap",
        len(first) <= CAP,
        f"got {len(first)}",
    )
    check(
        "a later decomposition falls back to the general backstop",
        len(second) == GENERAL_CAP,
        f"got {len(second)}",
    )
    check(
        "every later decomposition keeps the general backstop",
        len(third) == GENERAL_CAP,
        f"got {len(third)}",
    )
    check(
        "a new workforce instance starts again on the structured plan path",
        len(fresh) <= CAP and "fresh" not in camel_calls,
        f"got {len(fresh)} calls={camel_calls}",
    )
    five = _camel_decompose(_FakeWorkforce(), 5)
    check(
        "a 5-subtask non-initial decomposition is left intact",
        len(five) == 5,
        f"got {len(five)}",
    )


# ---------------------------------------------------------------------------
# None/empty/truncated planning response must never crash the job
# (rel-20260813T105420Z-2a702b3b: workforce.2-independent/4-independent/
# dynamic/capability-boundary all crashed with
# "AttributeError: 'NoneType' object has no attribute 'content'" from
# vendor Task._decompose_non_streaming (task.py:506,
# `task_parser(response.msg.content, ...)`, no guard on response.msg being
# None) after task_agent's decompose call saturated its completion budget.
# _patched_decompose_task now catches that failure at the seam it already
# wraps and falls back to [] -- the exact same return CAMEL's own caller
# (handle_decompose_append_task, workforce.py:2235-2242) already treats as
# "run the original task as one unit" when a planner legitimately declines
# to split. No new fallback mechanism, no recovery/assignment change.
#
# The catch is narrow, not blanket: _is_missing_planning_response_error
# only matches an AttributeError raised by the interpreter itself (not a
# hand-constructed one -- AttributeError(str) alone does not set
# .name/.obj) for `.msg` or `.content` on a genuinely None object. Every
# trigger below uses a REAL attribute access that produces that exact
# shape, not a manually-raised exception, so these tests exercise
# _is_missing_planning_response_error's actual matching logic rather than
# just its docstring.
# ---------------------------------------------------------------------------


def _trigger_none_msg_content_error():
    """The exact confirmed crash: response.msg is None, .content fails."""
    response_msg = None
    return response_msg.content


def _trigger_none_response_msg_error():
    """The sibling shape: response itself is None, .msg fails instead."""
    response = None
    return response.msg


def _trigger_unrelated_attribute_error_on_real_object():
    """A genuinely different bug: an AttributeError on a non-None object.
    Must never be mistaken for the planning-response failure."""

    class _SomeOtherCamelObject:
        pass

    return _SomeOtherCamelObject().some_unrelated_attribute


def _trigger_unrelated_attribute_name_on_none():
    """Same None-object shape, but a different attribute name -- e.g. a
    hypothetical future vendor change that fails on `.strip()` instead of
    `.msg`/`.content`. Must also not be swallowed: the failure signature
    this guard targets is specifically about .msg/.content, not "any
    attribute access on None"."""
    value = None
    return value.strip()


def test_none_msg_content_error_falls_back() -> None:
    """The exact confirmed crash shape falls back to []."""

    def fake_decompose(self, task):
        return _trigger_none_msg_content_error()

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    workforce = _FakeWorkforce()
    workforce._initial_decomposition_done = True
    task = Task(content="Run two independent workstreams; synthesize.", id="r")
    try:
        result = camel_service._patched_decompose_task(workforce, task)
    except AttributeError as exc:
        check("response.msg.content == None falls back, does not crash", False, repr(exc))
        return
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    check(
        "response.msg.content == None falls back to the bounded single-task result",
        result == [],
        result,
    )


def test_none_response_msg_error_falls_back() -> None:
    """The sibling shape (response itself None) also falls back."""

    def fake_decompose(self, task):
        return _trigger_none_response_msg_error()

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    workforce = _FakeWorkforce()
    workforce._initial_decomposition_done = True
    task = Task(content="Design a system and review it.", id="r2")
    try:
        result = camel_service._patched_decompose_task(workforce, task)
    except AttributeError as exc:
        check("response.msg == None (response itself None) falls back", False, repr(exc))
        return
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    check(
        "response.msg == None (response itself None) falls back to []",
        result == [],
        result,
    )


def test_unrelated_attribute_error_on_real_object_is_reraised() -> None:
    """An AttributeError on a genuinely non-None object is a different bug
    and must propagate, not be silently treated as a planning failure."""

    def fake_decompose(self, task):
        return _trigger_unrelated_attribute_error_on_real_object()

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    task = Task(content="x", id="r5")
    workforce = _FakeWorkforce()
    workforce._initial_decomposition_done = True
    raised = None
    try:
        camel_service._patched_decompose_task(workforce, task)
    except AttributeError as exc:
        raised = exc
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    check(
        "an unrelated AttributeError on a real object is re-raised, not swallowed",
        raised is not None and "some_unrelated_attribute" in str(raised),
        repr(raised),
    )


def test_unrelated_attribute_name_on_none_is_reraised() -> None:
    """Even when the object IS None, an unrelated attribute name (not
    .msg/.content) is still a different bug and must propagate."""

    def fake_decompose(self, task):
        return _trigger_unrelated_attribute_name_on_none()

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose
    task = Task(content="x", id="r6")
    workforce = _FakeWorkforce()
    workforce._initial_decomposition_done = True
    raised = None
    try:
        camel_service._patched_decompose_task(workforce, task)
    except AttributeError as exc:
        raised = exc
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    check(
        "a None object's unrelated attribute error (not .msg/.content) is re-raised",
        raised is not None and getattr(raised, "name", None) == "strip",
        repr(raised),
    )


def test_none_response_restores_task_content() -> None:
    """The content-swap `finally` must still run when decompose raises,
    for both the caught (fallback) and re-raised (unrelated) cases."""

    def fake_decompose_matching(self, task):
        return _trigger_none_msg_content_error()

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose_matching
    original_content = "x" * (camel_service.MAX_PLANNER_CONTENT_CHARS + 5_000)
    task = Task(content=original_content, id="r3")
    workforce = _FakeWorkforce()
    workforce._initial_decomposition_done = True
    try:
        camel_service._patched_decompose_task(workforce, task)
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original
    check(
        "task.content is restored after the caught (matching) crash",
        task.content == original_content,
        f"len(task.content)={len(task.content)}",
    )

    def fake_decompose_unrelated(self, task):
        return _trigger_unrelated_attribute_error_on_real_object()

    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose_unrelated
    task2 = Task(content=original_content, id="r3b")
    workforce2 = _FakeWorkforce()
    workforce2._initial_decomposition_done = True
    try:
        camel_service._patched_decompose_task(workforce2, task2)
    except AttributeError:
        pass
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original
    check(
        "task.content is restored even when the error is re-raised",
        task2.content == original_content,
        f"len(task2.content)={len(task2.content)}",
    )


def test_crash_fallback_matches_legitimate_no_split_shape() -> None:
    """The crash fallback's return value ([]) is not a new mechanism -- it
    is byte-identical to what a legitimate "planner declined to split"
    decomposition already returns, so it reaches the exact same, already-
    existing "run as one task" path with no new code on the caller side."""
    legitimate_no_split = _camel_decompose(_FakeWorkforce(), 0)
    check(
        "a legitimate zero-subtask decomposition already returns []",
        legitimate_no_split == [],
        legitimate_no_split,
    )

    def fake_decompose_crash(self, task):
        return _trigger_none_msg_content_error()

    original = camel_service._ORIGINAL_DECOMPOSE_TASK
    camel_service._ORIGINAL_DECOMPOSE_TASK = fake_decompose_crash
    workforce = _FakeWorkforce()
    workforce._initial_decomposition_done = True
    try:
        crash_fallback = camel_service._patched_decompose_task(
            workforce, Task(content="x", id="r4")
        )
    finally:
        camel_service._ORIGINAL_DECOMPOSE_TASK = original

    check(
        "the crash fallback's shape matches the legitimate no-split case exactly",
        crash_fallback == legitimate_no_split == [],
        (crash_fallback, legitimate_no_split),
    )


def test_initial_plan_crash_falls_back_to_empty() -> None:
    """A crash in the structured planner must not abort the job."""

    def boom(_workforce, _root):
        raise RuntimeError("plan exploded")

    original = camel_service._materialize_initial_plan
    camel_service._materialize_initial_plan = boom
    try:
        result = camel_service._patched_decompose_task(
            _FakeWorkforce(), Task(content="x", id="r-plan")
        )
    finally:
        camel_service._materialize_initial_plan = original
    check("initial plan crash falls back to []", result == [], result)


def test_is_missing_planning_response_error_matcher_directly() -> None:
    """Unit-level coverage of the matcher itself, independent of the
    decompose call site."""
    try:
        _trigger_none_msg_content_error()
    except AttributeError as exc:
        check(
            "matcher accepts response.msg.content on None",
            camel_service._is_missing_planning_response_error(exc),
        )

    try:
        _trigger_none_response_msg_error()
    except AttributeError as exc:
        check(
            "matcher accepts response.msg on None response",
            camel_service._is_missing_planning_response_error(exc),
        )

    try:
        _trigger_unrelated_attribute_error_on_real_object()
    except AttributeError as exc:
        check(
            "matcher rejects an AttributeError on a non-None object",
            not camel_service._is_missing_planning_response_error(exc),
        )

    try:
        _trigger_unrelated_attribute_name_on_none()
    except AttributeError as exc:
        check(
            "matcher rejects a None object's unrelated attribute name",
            not camel_service._is_missing_planning_response_error(exc),
        )

    check(
        "matcher rejects a hand-constructed AttributeError with no .name/.obj",
        not camel_service._is_missing_planning_response_error(
            AttributeError("'NoneType' object has no attribute 'content'")
        ),
    )


def test_valid_decomposition_unaffected_by_the_guard() -> None:
    """The new try/except must not change behavior for a normal, successful
    decomposition -- same assertions test_hard_bound_in_code already makes,
    repeated here explicitly as a guard against the exception handling
    itself introducing a regression."""
    for produced in (1, 2, 4):
        out = _camel_decompose(_FakeWorkforce(), produced)
        check(
            f"normal {produced}-subtask decomposition is unaffected by the crash guard",
            len(out) == produced,
            f"got {len(out)}",
        )


def test_no_recovery_fanout_change() -> None:
    """The decompose guard must not touch recovery/assignment mechanics."""
    check(
        "MAX_TASK_ATTEMPTS is unchanged (no recovery fan-out change)",
        camel_service.MAX_TASK_ATTEMPTS == 2,
        camel_service.MAX_TASK_ATTEMPTS,
    )
    check(
        "the recovery-strategy patch is untouched by this fix",
        Workforce._apply_recovery_strategy is camel_service._patched_apply_recovery_strategy,
    )
    check(
        "the assignment retry/fallback patch is untouched by this fix",
        Workforce._handle_assignment_retry_and_fallback
        is camel_service._patched_handle_assignment_retry_and_fallback,
    )


def test_planning_max_tokens_raised_and_bounded() -> None:
    """max_tokens for the planning stage was raised from the app-wide 4096
    default, using the same already-vetted value (8192) this codebase
    already uses for two other stages with the identical failure
    signature (a reasoning-heavy model consuming the whole completion
    budget before emitting real content) -- not an arbitrary new number."""
    check(
        "PLANNING_MAX_TOKENS is raised above the 4096 default that saturated",
        camel_service.PLANNING_MAX_TOKENS > 4096,
        camel_service.PLANNING_MAX_TOKENS,
    )
    check(
        "PLANNING_MAX_TOKENS reuses the same vetted ceiling as the other "
        "reasoning-overhead-affected stages, not a new unverified number",
        camel_service.PLANNING_MAX_TOKENS
        in (camel_service.DYNAMIC_WORKER_MAX_TOKENS, camel_service.SYNTHESIS_MAX_TOKENS),
        camel_service.PLANNING_MAX_TOKENS,
    )

    captured: dict = {}

    class _CapturingWorkforce:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)
            self.coordinator_agent = kwargs.get("coordinator_agent")
            self.task_agent = kwargs.get("task_agent")

        def add_single_agent_worker(self, description, worker=None, **kwargs):
            return self

    original_workforce = camel_service.Workforce
    camel_service.Workforce = _CapturingWorkforce
    try:
        camel_service.build_workforce(
            callback=object(), tracker=camel_service.UsageTracker()
        )
    finally:
        camel_service.Workforce = original_workforce

    task_agent = captured["task_agent"]
    configured_max_tokens = task_agent.model_backend.model_config_dict.get("max_tokens")
    check(
        "task_agent is actually constructed with the raised planning max_tokens",
        configured_max_tokens == camel_service.PLANNING_MAX_TOKENS,
        configured_max_tokens,
    )


# ---------------------------------------------------------------------------
# Opt-in live check -- needs a real model endpoint, excluded by default.
# ---------------------------------------------------------------------------

def main() -> None:
    test_hard_bound_in_code()
    test_cap_is_scoped_to_initial_decomposition()
    test_none_msg_content_error_falls_back()
    test_none_response_msg_error_falls_back()
    test_unrelated_attribute_error_on_real_object_is_reraised()
    test_unrelated_attribute_name_on_none_is_reraised()
    test_none_response_restores_task_content()
    test_crash_fallback_matches_legitimate_no_split_shape()
    test_initial_plan_crash_falls_back_to_empty()
    test_is_missing_planning_response_error_matcher_directly()
    test_valid_decomposition_unaffected_by_the_guard()
    test_no_recovery_fanout_change()
    test_planning_max_tokens_raised_and_bounded()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All decomposition-policy checks passed")


if __name__ == "__main__":
    main()
