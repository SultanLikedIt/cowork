"""Regression for the post-decomposition assignment collapse onto Research Agent.

Scenario that motivated this (observed live):

    A production-readiness request decomposed CORRECTLY into 3 independent
    subtasks spanning 3 distinct technical domains -- API reliability/
    observability, cloud infrastructure/deployment, application security/
    access control -- and all three were assigned to the fixed Research
    Agent.

Each decomposed subtask opens with a generic research verb ("Research and
summarize current best practices, common production risks, and mitigation
strategies for ..."). The coordinator matches that leading verb against
Research Agent's web_search/web_read description
sentence and never applies the TECHNICAL_DOMAIN_EXCLUSIONS clause that the
same description carries.

test_assignment_capability.py already pins the wording half of this fix
(every fixed worker disclaims the domains; NONE_FIT is reachable). That was
sufficient for ONE broad task and is not sufficient after decomposition --
which is why this guard is deterministic rather than another prompt edit.

What this test proves: the guard's classifier draws the domain boundary in
the right place, AND an assignment contradicting a bounded worker's own
description is rewritten to NONE_FIT at the coordinator seam. No model, no
network -- the coordinator call is stubbed, so the real veto logic runs
against a fixed assignment answer.

Run directly:
    python test_assignment_domain_guard.py
"""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402
from camel.societies.workforce.utils import (  # noqa: E402
    TaskAssignment,
    TaskAssignResult,
)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


# The three subtasks exactly as decomposition produced them, generic
# research verb and all.
SUBTASK_API = (
    "Research and summarize current best practices, common production "
    "risks, and mitigation strategies for API reliability and observability."
)
SUBTASK_INFRA = (
    "Research current best practices and production readiness "
    "considerations for cloud infrastructure and deployment strategy."
)
SUBTASK_SECURITY = (
    "Research and summarize common production risks and mitigations for "
    "application security and access control."
)

# Genuine current-fact / source-gathering work that must stay with Research
# Agent. The last one deliberately names a technical product: it has the
# technical DOMAIN but not the assessment FRAMING, which is precisely the
# distinction the two-signal classifier exists to draw.
RESEARCH_MARKET = "Research current market trends for electric vehicles in Europe."
RESEARCH_REGULATIONS = (
    "Research the current regulations governing consumer data privacy in the EU."
)
RESEARCH_COMPARISON = (
    "Research current product comparisons between the leading CRM vendors."
)
RESEARCH_TECHNICAL_FACT = (
    "Find the current pricing tiers and region availability for AWS Lambda."
)


class _FakeChild:
    def __init__(self, node_id: str, description: str) -> None:
        self.node_id = node_id
        self.description = description


class _FakeTask:
    def __init__(self, task_id: str, content: str) -> None:
        self.id = task_id
        self.content = content


class _FakeWorkforce:
    """Only what the patched coordinator wrapper actually reads."""

    def __init__(self, children) -> None:
        self._children = children


def _run_guard(workforce, tasks, assignments):
    """Call the patched wrapper with the real coordinator call stubbed."""
    original = camel_service._ORIGINAL_CALL_COORDINATOR_FOR_ASSIGNMENT
    camel_service._ORIGINAL_CALL_COORDINATOR_FOR_ASSIGNMENT = (
        lambda self, tasks, invalid_ids=None: TaskAssignResult(
            assignments=assignments
        )
    )
    try:
        return camel_service._patched_call_coordinator_for_assignment(
            workforce, tasks
        )
    finally:
        camel_service._ORIGINAL_CALL_COORDINATOR_FOR_ASSIGNMENT = original


def main() -> None:
    # --- 1-3. The three decomposed technical subtasks are NOT research ---
    for label, text in (
        ("API reliability/observability", SUBTASK_API),
        ("cloud infrastructure/deployment", SUBTASK_INFRA),
        ("application security/access control", SUBTASK_SECURITY),
    ):
        check(
            f"{label} subtask is classified technical despite the research verb",
            camel_service.is_technical_domain_task(text),
            text,
        )

    # --- 4. Genuine current-fact research stays research -----------------
    for label, text in (
        ("current market trends", RESEARCH_MARKET),
        ("current regulations", RESEARCH_REGULATIONS),
        ("current product comparisons", RESEARCH_COMPARISON),
        ("current facts about a technical product", RESEARCH_TECHNICAL_FACT),
    ):
        check(
            f"{label} is NOT classified technical",
            not camel_service.is_technical_domain_task(text),
            text,
        )

    # Degenerate input must not classify as technical.
    for label, text in (("empty", ""), ("whitespace", "   "), ("none", None)):
        check(
            f"{label} input is not classified technical",
            not camel_service.is_technical_domain_task(text),
        )

    # --- The veto actually fires at the assignment seam ------------------
    research_desc = (
        "A research specialist that uses web_search/web_read to find and open "
        "current, verifiable sources." + camel_service.TECHNICAL_DOMAIN_EXCLUSIONS
    )
    children = [
        _FakeChild("node_research", research_desc),
        # A dynamic specialist: created WITHOUT the exclusions constant, so
        # it must stay assignable or a vetoed task would bounce forever.
        _FakeChild(
            "node_dynamic",
            "A production engineering specialist covering reliability, "
            "infrastructure, and application security.",
        ),
    ]
    workforce = _FakeWorkforce(children)

    tasks = [
        _FakeTask("task_1", SUBTASK_API),
        _FakeTask("task_2", SUBTASK_INFRA),
        _FakeTask("task_3", SUBTASK_SECURITY),
    ]
    all_on_research = [
        TaskAssignment(task_id="task_1", assignee_id="node_research"),
        TaskAssignment(task_id="task_2", assignee_id="node_research"),
        TaskAssignment(task_id="task_3", assignee_id="node_research"),
    ]

    result = _run_guard(workforce, tasks, all_on_research)
    vetoed = [
        a.assignee_id == camel_service.NONE_FIT_SENTINEL for a in result.assignments
    ]
    check(
        "all three technical subtasks are vetoed off Research Agent",
        all(vetoed),
        str([a.assignee_id for a in result.assignments]),
    )
    check(
        "vetoed assignments keep their task ids",
        [a.task_id for a in result.assignments] == ["task_1", "task_2", "task_3"],
        str([a.task_id for a in result.assignments]),
    )

    # A vetoed assignment must be the sentinel the existing fallback path
    # already routes to dynamic worker creation -- not some new signal.
    check(
        "the veto reuses the existing NONE_FIT sentinel",
        camel_service.NONE_FIT_SENTINEL == "NONE_FIT"
        and all(a.assignee_id == "NONE_FIT" for a in result.assignments),
    )
    check(
        "NONE_FIT still routes to dynamic worker creation",
        camel_service.Workforce._handle_assignment_retry_and_fallback
        is camel_service._patched_handle_assignment_retry_and_fallback,
    )
    check(
        "the patched coordinator call is the one Workforce actually uses",
        camel_service.Workforce._call_coordinator_for_assignment
        is camel_service._patched_call_coordinator_for_assignment,
    )

    # --- The guard must not touch anything else -------------------------
    genuine_tasks = [_FakeTask("task_1", RESEARCH_MARKET)]
    genuine = [TaskAssignment(task_id="task_1", assignee_id="node_research")]
    result = _run_guard(workforce, genuine_tasks, genuine)
    check(
        "a genuine research task is left assigned to Research Agent",
        result.assignments[0].assignee_id == "node_research",
        result.assignments[0].assignee_id,
    )

    # A dynamic specialist has no exclusions, so the same technical task
    # assigned to it must survive untouched.
    dynamic = [TaskAssignment(task_id="task_1", assignee_id="node_dynamic")]
    result = _run_guard(workforce, [_FakeTask("task_1", SUBTASK_API)], dynamic)
    check(
        "a technical task assigned to a dynamic specialist is NOT vetoed",
        result.assignments[0].assignee_id == "node_dynamic",
        result.assignments[0].assignee_id,
    )

    # Mixed batch: only the offending assignment changes.
    mixed_tasks = [
        _FakeTask("task_1", SUBTASK_SECURITY),
        _FakeTask("task_2", RESEARCH_COMPARISON),
    ]
    mixed = [
        TaskAssignment(task_id="task_1", assignee_id="node_research"),
        TaskAssignment(task_id="task_2", assignee_id="node_research"),
    ]
    result = _run_guard(workforce, mixed_tasks, mixed)
    by_id = {a.task_id: a.assignee_id for a in result.assignments}
    check(
        "in a mixed batch only the technical task is vetoed",
        by_id == {"task_1": "NONE_FIT", "task_2": "node_research"},
        str(by_id),
    )

    # Dependencies carried by an assignment must survive the rewrite.
    dep = [
        TaskAssignment(
            task_id="task_1", assignee_id="node_research", dependencies=["task_0"]
        )
    ]
    result = _run_guard(workforce, [_FakeTask("task_1", SUBTASK_API)], dep)
    check(
        "a vetoed assignment preserves its dependencies",
        result.assignments[0].dependencies == ["task_0"],
        str(result.assignments[0].dependencies),
    )

    # No bounded workers at all (e.g. a workforce of only dynamic
    # specialists) -> the guard is a no-op.
    only_dynamic = _FakeWorkforce([_FakeChild("node_dynamic", "A specialist.")])
    passthrough = [TaskAssignment(task_id="task_1", assignee_id="node_dynamic")]
    result = _run_guard(only_dynamic, [_FakeTask("task_1", SUBTASK_API)], passthrough)
    check(
        "with no exclusion-bounded workers the guard is a no-op",
        result.assignments[0].assignee_id == "node_dynamic",
        result.assignments[0].assignee_id,
    )

    # --- Fixed worker capabilities were NOT broadened or narrowed --------
    roles = {role: description for description, role, _ in camel_service.WORKER_ROLES}
    check(
        "the three fixed workers are unchanged",
        set(roles)
        == {"Research Agent", "Document Agent", "Coding Agent"},
        str(sorted(roles)),
    )
    check(
        "Research Agent still advertises its real capability",
        "uses web_search/web_read" in roles["Research Agent"],
    )
    check(
        "Research and Document still share the exclusion constant",
        all(
            camel_service.TECHNICAL_DOMAIN_EXCLUSIONS in roles[r]
            for r in ("Research Agent", "Document Agent")
        ),
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All assignment domain-guard checks passed")


if __name__ == "__main__":
    main()
