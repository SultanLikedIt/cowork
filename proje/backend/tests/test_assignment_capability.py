"""Focused regression for fixed-worker capability matching at ASSIGNMENT.

Scenario that motivated this (observed live, inconsistent run to run):

    "Prepare a production readiness assessment for a FastAPI service
     covering PostgreSQL architecture, deployment/DevOps, and security."

The planner correctly produced ONE broad task, but assignment picked
Document Agent on one run and Research Agent on another. Neither can
actually deliver database architecture, DevOps, or a security review.

The cause is deterministic, not model randomness. ASSIGN_TASK_PROMPT rule 3
only permits the NONE_FIT signal when NO worker is capable "not even
imperfectly", and explicitly says to assign a plausible worker otherwise.
Only Research Agent's description ruled out these technical domains --
Document/Review/Outline each still advertised a broad capability ("writes
and formats text content", "organizes ideas into outlines") with nothing
excluding the subject matter, so the coordinator always had a rule-3
compliant reason to pick one of them.

What this test can and cannot prove: it asserts the DETERMINISTIC
PRECONDITION for NONE_FIT -- that no fixed worker's coordinator-facing
description advertises any capability this task needs, and that every one of
them explicitly disclaims it. It does NOT execute the coordinator, so it
cannot prove the model emits NONE_FIT; it proves that emitting a fixed
worker is no longer consistent with the assignment rules it is given.

No model, no network. Run directly:
    python test_assignment_capability.py
"""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


# The distinct technical capabilities the production-readiness task needs,
# each with the phrase a description must contain to disclaim it. The
# leading "does"/"Does" is left off deliberately: the exclusion sentence
# starts mid-description, so its first verb is capitalised while the rest
# are not.
REQUIRED_DISCLAIMERS = {
    "database/schema architecture": "NOT design database or infrastructure schemas",
    "infrastructure / Docker / Kubernetes": "NOT configure Docker/Kubernetes",
    "DevOps / CI-CD": "NOT perform DevOps or CI/CD work",
    "security review / audit": "NOT conduct security reviews",
    "production-readiness assessment": "production-readiness assessments",
}


def main() -> None:
    roles = {role: description for description, role, _ in camel_service.WORKER_ROLES}

    check(
        "the three fixed workers are unchanged",
        set(roles) == {"Research Agent", "Document Agent", "Coding Agent"},
        str(sorted(roles)),
    )

    # --- Research and Document still disclaim production-readiness tasks ---
    for role in ("Research Agent", "Document Agent"):
        description = roles[role]
        for capability, phrase in REQUIRED_DISCLAIMERS.items():
            check(
                f"{role} disclaims {capability}",
                phrase in description,
                f"missing {phrase!r}",
            )

    # --- The NONE_FIT escape hatch is present and reachable -------------
    prompt = str(camel_service._PATCHED_ASSIGN_TASK_PROMPT)
    check("assignment prompt offers the NONE_FIT signal", "NONE_FIT" in prompt)
    check(
        "assignment prompt states the sentinel value literally",
        camel_service.NONE_FIT_SENTINEL in prompt,
        camel_service.NONE_FIT_SENTINEL,
    )
    check(
        "the patched assign prompt is the one Workforce actually reads",
        camel_service._workforce_module.ASSIGN_TASK_PROMPT
        is camel_service._PATCHED_ASSIGN_TASK_PROMPT,
    )
    check(
        "NONE_FIT still routes to dynamic worker creation",
        camel_service.Workforce._handle_assignment_retry_and_fallback
        is camel_service._patched_handle_assignment_retry_and_fallback,
    )

    # --- Guard against over-tightening ----------------------------------
    still_capable = {
        "Research Agent": "uses web_search/web_read",
        "Document Agent": "creates normal downloadable artifacts",
        "Coding Agent": "coding and software-engineering",
    }
    for role, phrase in still_capable.items():
        check(
            f"{role} still advertises its real capability",
            phrase in roles[role],
            f"missing {phrase!r}",
        )

    # Research and Document share the exclusion constant.
    check(
        "Research and Document share one exclusion constant",
        all(
            camel_service.TECHNICAL_DOMAIN_EXCLUSIONS in roles[r]
            for r in ("Research Agent", "Document Agent")
        ),
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All fixed-worker assignment-capability checks passed")


if __name__ == "__main__":
    main()
