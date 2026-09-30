"""Intent routing regression: real requests, expected flags and agents.

One classifier call (``artifact_intent.classify_artifact_intent``) decides
whether a Workforce job wants a file, the web, code, the mailbox, Reddit,
Jira, GitHub, an email sent. Every new integration adds a flag to that one
call, and a wrong flag is a wrong answer: on 2026-09-22 "github'daki en
populer rust frameworkleri" also came back with needs_web_research, Research
ran last, and the GitHub evidence was dropped from the reply.

This runs a fixed set of real (mostly Turkish) requests through the SAME
classifier the app uses, several times each, and checks:

* **flags** -- only the ones a case names (``expect``); the rest are free,
  so a harmless difference elsewhere does not fail the case;
* **agents** -- optionally, the worker list the app would schedule with
  every integration enabled (``expect_roles``), derived by the same
  functions ``main._routing_for_workforce`` and ``_select_roles`` use.

Model calls cost money and vary between runs, so this never runs in CI or
by itself. Run it by hand before merging a change to the classifier prompt
or a new integration, inside the backend container (which has MODEL_*):

    docker compose exec backend python -m reliability_harness.intent_eval
    docker compose exec backend python -m reliability_harness.intent_eval --repeat 1 --tag github

Exit code: 0 all runs passed, 1 at least one wrong flag or agent list,
2 no wrong answers but some runs errored (model unreachable, fallback).
``tests/test_intent_eval.py`` checks the case file and the scoring without
a model, so a broken case is caught by the normal test run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

CASES_PATH = Path(__file__).with_name("intent_cases.json")
RESULTS_DIR = Path(__file__).resolve().parent.parent / "reliability-results"

# The flags a case may assert, with the type each must have.
FLAG_TYPES: dict[str, type] = {
    "wants_artifact": bool,
    "artifact_type": str,
    "needs_web_research": bool,
    "needs_code_execution": bool,
    "reuses_uploaded_document": bool,
    "opens_on_user_computer": bool,
    "sends_email": bool,
    "reads_email": bool,
    "reads_social": bool,
    "reads_jira": bool,
    "reads_github": bool,
    "reads_documents": bool,
}
ARTIFACT_TYPES = {"xlsx", "csv", "json", "docx", "pptx", "html", "pdf", "code", "none"}
ROLE_NAMES = {
    "Research Agent",
    "Document Agent",
    "Coding Agent",
    "Assistant",
    "Mail Agent",
    "Social Agent",
    "Jira Agent",
    "GitHub Agent",
    "Knowledge Agent",
}

Classify = Callable[..., Awaitable[Any]]


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    id: str
    text: str
    expect: dict[str, Any]
    expect_roles: Optional[tuple[str, ...]] = None
    attachments: tuple[str, ...] = ()
    # File names in the workspace's document library, as the classifier is
    # shown them (artifact_intent: WORKSPACE DOCUMENTS).
    library: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    note: str = ""


class CaseError(ValueError):
    pass


def parse_cases(raw: Any) -> list[Case]:
    """Validate the case file. Raises CaseError naming the first problem."""
    if not isinstance(raw, list) or not raw:
        raise CaseError("the case file must be a non-empty JSON list")
    cases: list[Case] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"case #{index + 1}"
        if not isinstance(item, dict):
            raise CaseError(f"{where}: not an object")
        case_id = str(item.get("id") or "").strip()
        where = f"case {case_id or index + 1!r}"
        if not case_id or case_id in seen:
            raise CaseError(f"{where}: missing or duplicate id")
        seen.add(case_id)
        text = str(item.get("text") or "").strip()
        if not text:
            raise CaseError(f"{where}: empty text")
        expect = item.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise CaseError(f"{where}: 'expect' must name at least one flag")
        for flag, value in expect.items():
            wanted = FLAG_TYPES.get(flag)
            if wanted is None:
                raise CaseError(f"{where}: unknown flag {flag!r}")
            if type(value) is not wanted:
                raise CaseError(f"{where}: {flag} must be {wanted.__name__}")
            if flag == "artifact_type" and value not in ARTIFACT_TYPES:
                raise CaseError(f"{where}: unknown artifact_type {value!r}")
        roles = item.get("expect_roles")
        if roles is not None:
            if not isinstance(roles, list) or not roles:
                raise CaseError(f"{where}: expect_roles must be a non-empty list")
            unknown = [r for r in roles if r not in ROLE_NAMES]
            if unknown:
                raise CaseError(f"{where}: unknown roles {unknown}")
        cases.append(
            Case(
                id=case_id,
                text=text,
                expect=dict(expect),
                expect_roles=tuple(roles) if roles is not None else None,
                attachments=tuple(str(a) for a in item.get("attachments") or []),
                library=tuple(str(a) for a in item.get("library") or []),
                tags=tuple(str(t) for t in item.get("tags") or []),
                note=str(item.get("note") or ""),
            )
        )
    return cases


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    return parse_cases(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _file_kinds(attachments: tuple[str, ...]) -> list[str]:
    return [Path(name).suffix.lstrip(".").lower() for name in attachments if Path(name).suffix]


def roles_for(
    intent: Any,
    text: str,
    attachments: tuple[str, ...] = (),
    library: tuple[str, ...] = (),
) -> list[str]:
    """The workers the app would schedule for this intent, every integration on.

    Mirrors ``main._routing_for_workforce`` (structural floor + intent ->
    capabilities) and ``_run_workforce_async`` (the local-open fast path,
    then ``_select_roles``), minus the per-deployment gates -- this answers
    "is the routing right", not "is Jira configured here".
    """
    import capabilities
    from execution.langgraph_runtime import workforce

    kinds = _file_kinds(attachments)
    has_files = bool(attachments)
    # main grants document search only when there are documents to search.
    reads_documents = bool(intent.reads_documents) and bool(attachments or library)
    if intent.opens_on_user_computer and not intent.wants_artifact:
        return [workforce.ROLE_GENERAL]
    base = capabilities.expand(
        capabilities.workforce_structural_requirements(text, has_files, kinds)
    )
    effective = capabilities.apply_artifact_intent(
        base,
        wants_artifact=intent.wants_artifact,
        needs_web_research=intent.needs_web_research,
        needs_code_execution=intent.needs_code_execution and not intent.reuses_uploaded_document,
        text=text,
        has_workspace_files=has_files,
        workspace_file_kinds=kinds,
        reads_email=intent.reads_email,
        reads_social=intent.reads_social,
        reads_jira=intent.reads_jira,
        reads_github=intent.reads_github,
        reads_documents=reads_documents,
    )
    return workforce._select_roles(
        job_capabilities=effective,
        fast_path_role=None,
        wants_artifact=intent.wants_artifact,
        wants_sources=workforce._request_wants_sources(text),
        needs_live_data=workforce._request_needs_live_data(text),
        reads_email=intent.reads_email,
        reads_social=intent.reads_social,
        reads_jira=intent.reads_jira,
        reads_github=intent.reads_github,
        reads_documents=reads_documents,
    )


@dataclass
class Run:
    ok: bool = False
    error: str = ""
    mismatches: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)
    seconds: float = 0.0


def score(case: Case, intent: Any, roles: Optional[list[str]]) -> list[str]:
    """What this intent got wrong for this case, as readable lines."""
    wrong: list[str] = []
    for flag, expected in case.expect.items():
        got = getattr(intent, flag, None)
        if got != expected:
            wrong.append(f"{flag}: expected {expected!r}, got {got!r}")
    if case.expect_roles is not None and roles is not None and list(case.expect_roles) != roles:
        wrong.append(f"agents: expected {list(case.expect_roles)}, got {roles}")
    return wrong


async def run_case(case: Case, *, classify: Classify, tracker_factory: Callable[[], Any]) -> Run:
    started = time.monotonic()
    run = Run()
    try:
        intent = await classify(
            case.text,
            tracker_factory(),
            attachment_names=list(case.attachments) or None,
            library_names=list(case.library) or None,
        )
    except Exception as exc:  # noqa: BLE001 - a crashed call is an error, not a verdict
        run.error = f"{type(exc).__name__}: {exc}"[:300]
        run.seconds = time.monotonic() - started
        return run
    run.seconds = time.monotonic() - started
    run.flags = {flag: getattr(intent, flag, None) for flag in FLAG_TYPES}
    source = str(getattr(intent, "source", "") or "")
    if source != "llm":
        # The classifier failed and the app fell back to regex. Scoring that
        # would blame the prompt for an outage.
        run.error = f"classifier did not answer (source={source or '?'})"
        return run
    roles = (
        roles_for(intent, case.text, case.attachments, case.library)
        if case.expect_roles is not None
        else None
    )
    run.roles = list(roles or [])
    run.mismatches = score(case, intent, roles)
    run.ok = not run.mismatches
    return run


@dataclass
class CaseResult:
    case: Case
    runs: list[Run]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.runs if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.runs if not r.ok and not r.error)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.runs if r.error)

    @property
    def verdict(self) -> str:
        if self.failed == 0 and self.errored == 0:
            return "PASS"
        if self.failed == 0:
            return "ERROR"
        return "FLAKY" if self.passed else "FAIL"


async def evaluate(
    cases: list[Case],
    *,
    repeat: int,
    concurrency: int,
    classify: Classify,
    tracker_factory: Callable[[], Any],
) -> list[CaseResult]:
    slots = asyncio.Semaphore(max(1, concurrency))

    async def one(case: Case) -> Run:
        async with slots:
            return await run_case(case, classify=classify, tracker_factory=tracker_factory)

    runs = await asyncio.gather(*(one(case) for case in cases for _ in range(repeat)))
    results: list[CaseResult] = []
    for index, case in enumerate(cases):
        results.append(CaseResult(case, list(runs[index * repeat : (index + 1) * repeat])))
    return results


def exit_code(results: list[CaseResult]) -> int:
    if any(r.failed for r in results):
        return 1
    if any(r.errored for r in results):
        return 2
    return 0


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def render(results: list[CaseResult], *, repeat: int) -> str:
    lines: list[str] = []
    order = {"FAIL": 0, "FLAKY": 1, "ERROR": 2, "PASS": 3}
    for result in sorted(results, key=lambda r: (order[r.verdict], r.case.id)):
        lines.append(f"{result.verdict:<5} {result.passed}/{repeat}  {result.case.id}  — {result.case.text}")
        if result.verdict == "PASS":
            continue
        # Each distinct problem once, with how many runs had it.
        counts: dict[str, int] = {}
        for run in result.runs:
            for line in run.mismatches or ([run.error] if run.error else []):
                counts[line] = counts.get(line, 0) + 1
        for line, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"        {count}x {line}")
    total_runs = sum(len(r.runs) for r in results)
    passed_runs = sum(r.passed for r in results)
    verdicts = [r.verdict for r in results]
    lines.append("")
    lines.append(
        f"{len(results)} cases × {repeat} = {total_runs} runs: {passed_runs} passed "
        f"({100 * passed_runs / max(1, total_runs):.0f}%). "
        f"PASS {verdicts.count('PASS')}, FLAKY {verdicts.count('FLAKY')}, "
        f"FAIL {verdicts.count('FAIL')}, ERROR {verdicts.count('ERROR')}."
    )
    return "\n".join(lines)


def to_json(results: list[CaseResult], *, repeat: int) -> dict[str, Any]:
    return {
        "repeat": repeat,
        "cases": [
            {
                "id": r.case.id,
                "text": r.case.text,
                "verdict": r.verdict,
                "passed": r.passed,
                "runs": [
                    {
                        "ok": run.ok,
                        "error": run.error,
                        "mismatches": run.mismatches,
                        "flags": run.flags,
                        "roles": run.roles,
                        "seconds": round(run.seconds, 2),
                    }
                    for run in r.runs
                ],
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def select(cases: list[Case], *, ids: list[str], tags: list[str]) -> list[Case]:
    chosen = cases
    if ids:
        wanted = set(ids)
        chosen = [c for c in chosen if c.id in wanted]
    if tags:
        wanted_tags = set(tags)
        chosen = [c for c in chosen if wanted_tags & set(c.tags)]
    return chosen


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeat", type=int, default=3, help="runs per case (default 3)")
    parser.add_argument("--concurrency", type=int, default=4, help="parallel model calls")
    parser.add_argument("--case", action="append", default=[], help="only this case id (repeatable)")
    parser.add_argument("--tag", action="append", default=[], help="only cases with this tag (repeatable)")
    parser.add_argument("--list", action="store_true", help="list cases and exit, no model calls")
    parser.add_argument("--json", type=Path, help="also write results here")
    args = parser.parse_args(argv)

    cases = select(load_cases(), ids=args.case, tags=args.tag)
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    if args.list:
        for case in cases:
            print(f"{case.id:<32} [{', '.join(case.tags)}] {case.text}")
        print(f"\n{len(cases)} cases")
        return 0

    import logging

    import artifact_intent
    from model_config import UsageTracker

    # The classifier logs a full traceback for every timeout and a warning for
    # every dropped recipient. Each of those is already in the report (as an
    # ERROR run or a flag), so here they only bury it.
    logging.getLogger("agent_webapp").setLevel(logging.CRITICAL)

    repeat = max(1, args.repeat)
    print(f"{len(cases)} cases × {repeat} runs = {len(cases) * repeat} classifier calls\n", flush=True)
    started = time.monotonic()
    results = asyncio.run(
        evaluate(
            cases,
            repeat=repeat,
            concurrency=args.concurrency,
            classify=artifact_intent.classify_artifact_intent,
            tracker_factory=UsageTracker,
        )
    )
    print(render(results, repeat=repeat))
    print(f"Took {time.monotonic() - started:.0f}s.")

    out = args.json
    if out is None:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"intent-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(to_json(results, repeat=repeat), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Details: {out}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
