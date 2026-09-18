"""Iterative deep research (DEEP_RESEARCH_MODE=iter): the report loop.

No network and no model -- search, fetch, and complete are injected.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import pytest

import web_controller
from web_controller import IterTurn

os.environ["RERANKER_ENABLED"] = "false"


def _report(*findings: str, open_q: str = "- more facets") -> str:
    lines = "\n".join(findings) or "- (none yet)"
    return (
        f"## Verified findings\n{lines}\n## Open questions\n{open_q}\n"
        "## Contradictions\n- none\n## Current best answer draft\ndraft\n"
        "## Confidence\nmedium -- test"
    )


class _Web:
    """search(query) returns one fresh page per query; fetch serves it."""

    def __init__(self, page_text: str = "", per_query: int = 1) -> None:
        self.page_text = page_text
        self.per_query = per_query
        self.queries: list[str] = []
        self.fetched: list[str] = []

    def search(self, query: str, max_results: int = 10) -> dict[str, Any]:
        self.queries.append(query)
        n = len(self.queries)
        return {
            "ok": True,
            "results": [
                {
                    "url": f"https://site{n}-{i}.example/article/{n}",
                    "title": f"{query} article {i}",
                    "content": f"{query} details",
                }
                for i in range(self.per_query)
            ],
        }

    def fetch(self, url: str) -> dict[str, Any]:
        self.fetched.append(url)
        text = self.page_text or f"Page body for {url}. " * 30
        return {"ok": True, "source_url": url, "title": f"Title {url}", "text": text}


class _Turns:
    """Scripted ``iterate`` replies; records every prompt it was given."""

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.phases: list[str] = []

    async def __call__(
        self, phase: str, system: str, user: str, schema: Optional[type]
    ) -> Any:
        self.phases.append(phase)
        if phase == "iterate":
            self.prompts.append(user)
            if self.turns:
                return self.turns.pop(0)
            return IterTurn(report=_report(), action="finish")
        if schema is web_controller.UrlSelection:
            return web_controller.UrlSelection(urls=[])
        return "scripted answer"


def _acquire(web: _Web, turns: _Turns, **kwargs: Any) -> web_controller.ControllerResult:
    return asyncio.run(
        web_controller.acquire_web_evidence(
            "Türkiye elektrikli araç pazarı durumu, avantajları ve engelleri",
            search=web.search,
            fetch=web.fetch,
            complete=turns,
            depth="deep",
            deep_mode="iter",
            **kwargs,
        )
    )


@pytest.fixture(autouse=True)
def _small_min_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_controller, "ITER_MIN_SEARCHES", 2)


def test_mode_flag_defaults_to_survey(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEP_RESEARCH_MODE", raising=False)
    assert web_controller.deep_research_mode() == "survey"
    monkeypatch.setenv("DEEP_RESEARCH_MODE", "iter")
    assert web_controller.deep_research_mode() == "iter"
    assert web_controller.deep_research_mode("survey") == "survey"


def test_loop_is_markov_report_replaced_and_old_pages_dropped() -> None:
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="ev sales 2026"),
            IterTurn(
                report=_report("- 2026 sales rose [E1]"),
                action="search",
                query="ev charging stations",
            ),
            IterTurn(
                report=_report("- sales rose [E1]", "- 5,000 chargers [E2]"),
                action="finish",
            ),
        ]
    )
    result = _acquire(web, turns)

    assert web.queries == ["ev sales 2026", "ev charging stations"]
    assert turns.phases.count("search") == 0, "model-written query is used as-is"
    first_page = web.fetched[0]
    # Round 2 sees round 1's page; round 3 sees only round 2's -- the old
    # page survives only as a cited finding in the report.
    assert first_page in turns.prompts[1]
    assert first_page not in turns.prompts[2]
    assert "5,000 chargers" not in turns.prompts[2]
    assert "2026 sales rose [E1]" in turns.prompts[2]
    assert result.report.count("## Verified findings") == 1
    assert "5,000 chargers [E2]" in result.report
    assert [e["evidence_id"] for e in result.report_evidence] == ["E1", "E2"]
    assert result.iter_rounds == 3


def test_prompt_size_does_not_grow_with_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_controller, "ITER_MIN_SEARCHES", 8)
    web = _Web()
    script = [IterTurn(report=_report(), action="search", query="q0")]
    for n in range(1, 8):
        script.append(
            IterTurn(
                report=_report(f"- finding {n} [E{n}]"),
                action="search",
                query=f"q{n}",
            )
        )
    turns = _Turns(script + [IterTurn(report=_report("- f [E8]"), action="finish")])
    _acquire(web, turns)
    sizes = [len(p) for p in turns.prompts[1:8]]
    # Only the short "queries already run" list grows between rounds.
    assert max(sizes) - min(sizes) < 400, sizes


def test_finish_refused_before_min_searches() -> None:
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="first"),
            IterTurn(report=_report("- a [E1]"), action="finish"),
            IterTurn(report=_report("- a [E1]"), action="search", query="second"),
            IterTurn(report=_report("- a [E1]", "- b [E2]"), action="finish"),
        ]
    )
    result = _acquire(web, turns)
    assert web.queries == ["first", "second"]
    assert "finish (refused)" in turns.prompts[2]
    assert result.iter_rounds == 4


def test_unknown_citations_and_unsourced_findings_are_removed() -> None:
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="first"),
            IterTurn(
                report=_report("- real [E1]", "- invented [E9]", "- no source at all"),
                action="search",
                query="second",
            ),
            IterTurn(
                report=_report("- real [E1]", "- also [E2, E7]"), action="finish"
            ),
        ]
    )
    result = _acquire(web, turns)
    assert "invented" not in turns.prompts[2]
    assert "no source at all" not in turns.prompts[2]
    assert "had no valid evidence id" in turns.prompts[2]
    assert "- also [E2]" in result.report
    assert "E7" not in result.report


def test_recall_returns_full_page_text_without_spending_budget() -> None:
    long_text = "Intro. " + ("filler sentence. " * 300) + "DEEP DETAIL 42 GWh."
    web = _Web(page_text=long_text)
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="battery output"),
            IterTurn(report=_report("- intro [E1]"), action="recall", evidence_ids=["e1"]),
            IterTurn(report=_report("- 42 GWh [E1]"), action="search", query="other"),
            IterTurn(report=_report("- 42 GWh [E1]"), action="finish"),
        ]
    )
    result = _acquire(web, turns)
    # The search round showed only the ledger's first MAX_EVIDENCE_TEXT chars;
    # recall re-reads the fuller page.
    assert "DEEP DETAIL 42 GWh" not in turns.prompts[1]
    assert "RECALLED PAGES" in turns.prompts[2]
    assert "DEEP DETAIL 42 GWh" in turns.prompts[2]
    assert result.search_count == 2


def test_open_takes_only_candidate_urls() -> None:
    web = _Web(per_query=6)
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="topic"),
            IterTurn(
                report=_report("- x [E1]"),
                action="open",
                urls=["https://invented.example/guess", "https://site1-5.example/article/1"],
            ),
            IterTurn(report=_report("- x [E1]"), action="search", query="more"),
            IterTurn(report=_report("- x [E1]"), action="finish"),
        ]
    )
    _acquire(web, turns)
    assert "https://invented.example/guess" not in web.fetched
    assert "https://site1-5.example/article/1" in web.fetched


def test_repeated_query_is_not_run_again() -> None:
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="Same Query"),
            IterTurn(report=_report("- a [E1]"), action="search", query="same query"),
            IterTurn(report=_report("- a [E1]"), action="search", query="new query"),
            IterTurn(report=_report("- a [E1]"), action="finish"),
        ]
    )
    _acquire(web, turns)
    assert web.queries == ["Same Query", "new query"]
    assert "already ran" in turns.prompts[2]


def test_budget_stop_runs_one_fold_round(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_controller, "MAX_SEARCH_PHASES_DEEP", 2)
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="one"),
            IterTurn(report=_report("- a [E1]"), action="search", query="two"),
            IterTurn(report=_report("- a [E1]", "- b [E2]"), action="search", query="three"),
        ]
    )
    result = _acquire(web, turns)
    assert web.queries == ["one", "two"]
    assert "budget is spent" in turns.prompts[2]
    assert result.iter_rounds == 3
    assert "- b [E2]" in result.report


def test_round_cap_keeps_unfolded_last_pages_for_answer() -> None:
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="one"),
            IterTurn(report=_report("- a [E1]"), action="search", query="two"),
        ]
    )
    result = asyncio.run(
        web_controller._acquire_iterative_evidence(
            web_controller.ControllerResult(),
            "question",
            search=web.search,
            fetch=web.fetch,
            complete=turns,
            progress=None,
            current_date=None,
            wants_artifact=False,
            max_rounds=2,
        )
    )
    # E2 came from the last action; no round folded it into the report.
    assert [e["evidence_id"] for e in result.report_evidence] == ["E1", "E2"]


def test_broken_turns_fall_back_then_stop() -> None:
    web = _Web()
    turns = _Turns(["not json", "still not json", "garbage", "garbage"])
    result = _acquire(web, turns)
    # Two broken replies (one retry) on round 1 -> fallback search from the
    # question; two more on round 2 -> stop with what exists.
    assert len(web.queries) == 1
    assert result.report == ""
    assert result.report_evidence == []
    assert result.successful_fetches == 1


def test_broken_report_keeps_previous_and_still_runs_action() -> None:
    web = _Web()
    turns = _Turns(
        [
            IterTurn(report=_report(), action="search", query="one"),
            IterTurn(report=_report("- a [E1]"), action="search", query="two"),
            IterTurn(report="", action="search", query="three"),
            IterTurn(report="", action="search", query="three"),
            IterTurn(report=_report("- a [E1]", "- c [E3]"), action="finish"),
        ]
    )
    result = _acquire(web, turns)
    assert web.queries == ["one", "two", "three"]
    assert "- a [E1]" in turns.prompts[3], "previous report carried forward"
    assert "- c [E3]" in result.report


def test_validate_report_citations() -> None:
    report = (
        "## Verified findings\n- kept [E1]\n- mixed [E2; E5]\n- bare claim\n"
        "## Open questions\n- a question without an id stays\n"
    )
    cleaned, cited, dropped = web_controller._validate_report_citations(
        report, {"E1", "E2"}
    )
    assert cited == {"E1", "E2"}
    assert dropped == 1
    assert "- mixed [E2]" in cleaned
    assert "bare claim" not in cleaned
    assert "a question without an id stays" in cleaned


class _SystemRecorder:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.systems: list[str] = []
        self.users: list[str] = []

    async def __call__(self, phase, system, user, schema):
        self.systems.append(system)
        self.users.append(user)
        return self.reply


def test_deep_answer_uses_survey_brief_prompt() -> None:
    rec = _SystemRecorder("Brief")
    asyncio.run(
        web_controller.answer_from_evidence("ask", [], complete=rec, depth="deep")
    )
    assert rec.systems[0] == web_controller.SURVEY_ANSWER_SYSTEM


def test_answer_from_report_uses_report_and_strips_ids() -> None:
    rec = _SystemRecorder("EV Pazarı\n\nSatışlar arttı [E1] ve şarj [E2, E3] yayıldı.")
    evidence = [
        {"url": "https://a.example/x", "title": "A", "ok": True, "text": "t", "evidence_id": "E1"}
    ]
    text = asyncio.run(
        web_controller.answer_from_evidence(
            "ask",
            evidence,
            complete=rec,
            depth="deep",
            report=_report("- Satışlar arttı [E1]"),
        )
    )
    assert rec.systems[0].endswith(web_controller.ITER_ANSWER_ADDENDUM)
    assert "RESEARCH REPORT" in rec.users[0]
    assert '"evidence_id": "E1"' in rec.users[0]
    assert "[E" not in text
    assert "Satışlar arttı ve şarj yayıldı." in text
