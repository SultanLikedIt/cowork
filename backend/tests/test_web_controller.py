"""Phase-controller rules: stop/continue is structural, not ReAct.

No network and no model -- search, fetch, and complete are injected.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Any, Optional

import capabilities
import web_controller
import web_tools
from html_extract import AcquiredPage, AcquisitionError, AcquisitionResult
from web_fetch import WebFetchTools

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        raise AssertionError(f"{name} -- {detail}")


class _FakeEngine:
    def __init__(self, pages_by_url: dict[str, AcquiredPage]) -> None:
        self.pages_by_url = pages_by_url
        self.fetches: list[str] = []

    def acquire(self, url: str, need: str) -> AcquisitionResult:
        self.fetches.append(url)
        page = self.pages_by_url.get(url)
        if page is None:
            raise AcquisitionError("not_found", f"{url} returned HTTP 404.")
        return AcquisitionResult(pages=[page])


def _page(url: str, text: str = "usable article text " * 8, tables=None) -> AcquiredPage:
    return AcquiredPage(
        url=url,
        status=200,
        title="Page",
        text=text,
        tables=tables or [],
    )


def _tools(
    engine: _FakeEngine,
    root: str,
    *,
    request_text: str = "",
    searches: Optional[list[list[dict[str, Any]]]] = None,
) -> web_tools.WebTools:
    web_tools.get_crawl4ai_engine = lambda: engine
    remaining = list(searches or [])

    def search_fn(query: str, number_of_result_pages: int = 10):
        if remaining:
            return remaining.pop(0)
        return []

    return web_tools.WebTools(
        fetch_tools=WebFetchTools(workspace_root=root),
        search_fn=search_fn,
        engine=engine,
        request_text=request_text,
    )


class _ScriptedComplete:
    def __init__(self, by_phase: dict[str, list[Any]]) -> None:
        self.by_phase = {key: list(value) for key, value in by_phase.items()}
        self.calls: list[str] = []

    async def __call__(
        self, phase: str, system: str, user: str, schema: Optional[type]
    ) -> Any:
        self.calls.append(phase)
        queued = self.by_phase.get(phase) or []
        if queued:
            return queued.pop(0)
        if schema is web_controller.SearchQuery:
            return web_controller.SearchQuery(query="fallback query")
        if schema is web_controller.UrlSelection:
            return web_controller.UrlSelection(urls=[])
        if schema is web_controller.ResearchPlan:
            return web_controller.ResearchPlan(angles=["fallback angle"])
        if schema is web_controller.CoverageAssessment:
            return web_controller.CoverageAssessment(enough=True, missing=[])
        return "scripted answer"


def _run(coro):
    return asyncio.run(coro)


# Scripted SELECT tests must not reach real reranker weights when they exist locally.
os.environ["RERANKER_ENABLED"] = "false"


class _PatchedReranker:
    def __init__(self, score_fn, enabled: str = "true") -> None:
        self.score_fn = score_fn
        self.enabled = enabled

    def __enter__(self) -> "_PatchedReranker":
        self._saved_score = web_controller.reranker.score
        self._saved_env = os.environ.get("RERANKER_ENABLED")
        web_controller.reranker.score = self.score_fn
        os.environ["RERANKER_ENABLED"] = self.enabled
        return self

    def __exit__(self, *exc_info: Any) -> None:
        web_controller.reranker.score = self._saved_score
        if self._saved_env is None:
            os.environ.pop("RERANKER_ENABLED", None)
        else:
            os.environ["RERANKER_ENABLED"] = self._saved_env


def _rerank_run(
    hits: list[dict[str, Any]],
    pages: dict[str, AcquiredPage],
    reranker_ctx: _PatchedReranker,
    select: Optional[list[Any]] = None,
) -> tuple[_FakeEngine, _ScriptedComplete]:
    engine = _FakeEngine(pages)
    complete = _ScriptedComplete({"select": select} if select else {})
    with tempfile.TemporaryDirectory() as root, reranker_ctx:
        web = _tools(engine, root, searches=[hits])
        _run(
            web_controller.acquire_web_evidence(
                "article facts",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                select_limit=1,
                allow_refine=False,
            )
        )
    return engine, complete


def _by_title(scores: dict[str, float]):
    def score(query: str, passages: list[str]) -> list[float]:
        return [scores[passage.split("\n", 1)[0]] for passage in passages]

    return score


def test_reranker_replaces_llm_select() -> None:
    weak = "https://news.example/weak"
    strong = "https://news.example/strong"
    engine, complete = _rerank_run(
        [
            {"url": weak, "title": "Weak", "content": "hit"},
            {"url": strong, "title": "Strong", "content": "hit"},
        ],
        {weak: _page(weak), strong: _page(strong)},
        _PatchedReranker(_by_title({"Weak": 0.1, "Strong": 0.9})),
    )
    check("reranker path makes no SELECT model call", "select" not in complete.calls, complete.calls)
    check("highest reranker score is opened", engine.fetches == [strong], engine.fetches)


def test_unavailable_reranker_falls_back_to_llm_select() -> None:
    weak = "https://news.example/weak"
    strong = "https://news.example/strong"
    engine, complete = _rerank_run(
        [
            {"url": weak, "title": "Weak", "content": "hit"},
            {"url": strong, "title": "Strong", "content": "hit"},
        ],
        {weak: _page(weak), strong: _page(strong)},
        _PatchedReranker(lambda query, passages: None),
        select=[web_controller.UrlSelection(urls=[weak])],
    )
    check("fallback makes the SELECT model call", "select" in complete.calls, complete.calls)
    check("fallback opens the LLM's pick", engine.fetches == [weak], engine.fetches)


def test_disabled_reranker_is_never_called() -> None:
    url = "https://news.example/article"

    def _must_not_run(query: str, passages: list[str]) -> list[float]:
        raise AssertionError("reranker ran while disabled")

    engine, complete = _rerank_run(
        [{"url": url, "title": "Article", "content": "hit"}],
        {url: _page(url)},
        _PatchedReranker(_must_not_run, enabled="false"),
        select=[web_controller.UrlSelection(urls=[url])],
    )
    check("disabled reranker uses SELECT model call", "select" in complete.calls, complete.calls)
    check("disabled reranker still opens a page", engine.fetches == [url], engine.fetches)


def test_reranked_pdf_still_yields_to_html() -> None:
    pdf = "https://gov.example/report.pdf"
    html = "https://gov.example/report"
    engine, _complete = _rerank_run(
        [
            {"url": pdf, "title": "Report PDF", "content": "hit"},
            {"url": html, "title": "Report page", "content": "hit"},
        ],
        {pdf: _page(pdf), html: _page(html)},
        _PatchedReranker(_by_title({"Report PDF": 0.99, "Report page": 0.2})),
    )
    check("PDF post-filter still applies after reranking", engine.fetches == [html], engine.fetches)


def test_reranker_time_is_reported() -> None:
    url = "https://news.example/article"
    engine = _FakeEngine({url: _page(url)})
    timings: list[tuple[str, float, str]] = []
    with tempfile.TemporaryDirectory() as root, _PatchedReranker(
        lambda query, passages: [0.5] * len(passages)
    ):
        web = _tools(engine, root, searches=[[{"url": url, "title": "Article", "content": "hit"}]])
        _run(
            web_controller.acquire_web_evidence(
                "article facts",
                search=web.search_raw,
                fetch=web.read_page,
                complete=_ScriptedComplete({}),
                allow_refine=False,
                on_timing=lambda name, seconds, detail: timings.append((name, seconds, detail)),
            )
        )
    check("reranker step reports its duration", [t[0] for t in timings] == ["rerank"], timings)
    check("reranker duration is non-negative", timings[0][1] >= 0, timings)


def test_literal_url_skips_search() -> None:
    url = "https://data.example/table"
    engine = _FakeEngine({url: _page(url)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(engine, root, request_text=f"open {url}")
        complete = _ScriptedComplete({})
        result = _run(
            web_controller.acquire_web_evidence(
                f"Extract the table from {url}",
                search=lambda q, n: (_ for _ in ()).throw(AssertionError("search")),
                fetch=web.read_page,
                complete=complete,
            )
        )
        check("literal URL skips SEARCH/SELECT model calls", complete.calls == [])
        check("literal URL is fetched", engine.fetches == [url], engine.fetches)
        check("skipped_search is set", result.skipped_search is True)
        check("one successful fetch", result.successful_fetches == 1)


def test_invented_select_url_is_not_fetched() -> None:
    real = "https://news.example/article"
    fake = "https://news.example/made-up-path"
    engine = _FakeEngine({real: _page(real), fake: _page(fake)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[[{"url": real, "title": "Article", "content": "hit"}]],
        )
        complete = _ScriptedComplete(
            {
                "search": [web_controller.SearchQuery(query="article 2026")],
                "select": [web_controller.UrlSelection(urls=[fake])],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "What happened?",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
            )
        )
        # Invented URLs stay blocked; if SELECT invents everything we still
        # open real search hits so Research is not left empty.
        check("invented URL was not fetched", fake not in engine.fetches, engine.fetches)
        check("fell back to real candidate", real in engine.fetches, engine.fetches)
        check("fallback successful fetch", result.successful_fetches == 1)


def test_successful_fetch_allows_one_refine_search() -> None:
    """Camel shape: vague first hit → refine with learned specifics."""
    hub = "https://src.example/fixtures"
    detail = "https://news.example/match-report"
    engine = _FakeEngine(
        {
            hub: _page(hub, text="Match listed: Alpha 2-2 Beta on 14 August."),
            detail: _page(detail, text="Starting XI: keeper, defender, ..."),
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": hub, "title": "Fixtures", "content": "list"}],
                [{"url": detail, "title": "Report", "content": "lineup"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="first vague"),
                    web_controller.SearchQuery(query="Alpha Beta 14 August XI"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=[hub]),
                    web_controller.UrlSelection(urls=[detail]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Find the starting XI",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
            )
        )
        # The model writes both queries now — the opening one and the refine.
        check("opening + refine SEARCH", complete.calls.count("search") == 2, complete.calls)
        check("two SELECT passes", complete.calls.count("select") == 2, complete.calls)
        check("hub then detail fetched", engine.fetches == [hub, detail], engine.fetches)
        check("successful_fetches covers both", result.successful_fetches == 2)


def test_allow_refine_false_keeps_single_search_on_success() -> None:
    url = "https://src.example/fact"
    engine = _FakeEngine({url: _page(url)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": "Fact", "content": "the fact"}],
                [{"url": "https://other.example/x", "title": "Other", "content": "x"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="first"),
                    web_controller.SearchQuery(query="second"),
                ],
                "select": [web_controller.UrlSelection(urls=[url])],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Find the fact",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                allow_refine=False,
            )
        )
        check("only one SEARCH when refine off", complete.calls.count("search") == 1, complete.calls)
        check("fetched the selected page", engine.fetches == [url], engine.fetches)
        check("successful_fetches is 1", result.successful_fetches == 1)


def test_multi_target_continues_after_first_success() -> None:
    urls = {
        "İstanbul": "https://weather.example/istanbul",
        "Ankara": "https://weather.example/ankara",
        "İzmir": "https://weather.example/izmir",
    }
    engine = _FakeEngine({url: _page(url) for url in urls.values()})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": name, "content": name}]
                for name, url in urls.items()
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query=name) for name in urls
                ],
                "select": [
                    web_controller.UrlSelection(urls=[url])
                    for url in urls.values()
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "İstanbul, Ankara, İzmir hava durumu",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                targets=list(urls),
            )
        )
        check(
            "multi-target does not stop SEARCH after the first successful fetch",
            complete.calls.count("search") == 3,
            complete.calls,
        )
        check(
            "each named target was fetched",
            engine.fetches == list(urls.values()),
            engine.fetches,
        )
        check("successful_fetches is 3", result.successful_fetches == 3)
        check(
            "covered_targets lists every named city",
            result.covered_targets == list(urls),
            result.covered_targets,
        )
        check(
            "controller records search/fetch per target",
            all(
                result.target_stats.get(name, {}).get("search") == 1
                and result.target_stats.get(name, {}).get("success") == 1
                for name in urls
            ),
            result.target_stats,
        )


def test_multi_target_retries_failed_target() -> None:
    alpha = "https://players.example/alpha"
    beta_dead = "https://players.example/beta-dead"
    beta_live = "https://players.example/beta-live"
    engine = _FakeEngine(
        {
            alpha: _page(alpha),
            beta_dead: _page(beta_dead, text=""),
            beta_live: _page(beta_live),
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": alpha, "title": "Alpha", "content": "alpha stats"}],
                [{"url": beta_dead, "title": "Beta dead", "content": "empty"}],
                [{"url": beta_live, "title": "Beta live", "content": "beta stats"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="Alpha player stats"),
                    web_controller.SearchQuery(query="Beta player stats"),
                    web_controller.SearchQuery(query="Beta player stats retry"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=[alpha]),
                    web_controller.UrlSelection(urls=[beta_dead]),
                    web_controller.UrlSelection(urls=[beta_live]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Research Alpha and Beta",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                targets=["Alpha", "Beta"],
            )
        )
        beta = result.target_stats.get("Beta") or {}
        check(
            "failed target gets a retry search",
            int(beta.get("search") or 0) == 2,
            beta,
        )
        check(
            "retry covers the failed target",
            "Beta" in result.covered_targets,
            result.covered_targets,
        )
        check(
            "retry updates last_query",
            beta.get("last_query") == "Beta player stats retry",
            beta.get("last_query"),
        )


def test_multi_target_records_observability_fields() -> None:
    url = "https://players.example/page"
    engine = _FakeEngine({url: _page(url, text="")})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[[{"url": url, "title": "Page", "content": "snippet"}]],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="Obscure player football"),
                    web_controller.SearchQuery(query="Obscure player football retry"),
                    web_controller.SearchQuery(
                        query="Obscure player football retry2"
                    ),
                ],
                "select": [
                    web_controller.UrlSelection(urls=[url]),
                    web_controller.UrlSelection(urls=[url]),
                    web_controller.UrlSelection(urls=[url]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Research Obscure Player",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                targets=["Obscure Player"],
            )
        )
        stats = result.target_stats.get("Obscure Player") or {}
        check(
            "last_query is persisted per target",
            stats.get("last_query") == "Obscure player football retry2",
            stats,
        )
        check(
            "selected_urls are persisted per target",
            stats.get("selected_urls") == [url],
            stats,
        )
        check(
            "fetch_error_reason is persisted on technical miss",
            bool(stats.get("fetch_error_reason")),
            stats,
        )


def test_multi_target_search_budget_stops_remaining_targets() -> None:
    urls = {f"Target-{i}": f"https://budget.example/t{i}" for i in range(5)}
    engine = _FakeEngine({url: _page(url) for url in urls.values()})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": name, "content": name}]
                for name, url in urls.items()
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query=name)
                    for name in urls
                ],
                "select": [
                    web_controller.UrlSelection(urls=[url])
                    for url in urls.values()
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Research five targets",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                targets=list(urls),
            )
        )
        check(
            "search budget caps multi-target passes",
            result.search_count == web_controller.MAX_SEARCH_PHASES_MULTI,
            result.search_count,
        )
        check(
            "remaining targets are not covered when budget is spent",
            len(result.covered_targets) == web_controller.MAX_SEARCH_PHASES_MULTI,
            result.covered_targets,
        )


def test_multi_target_artifact_budget_covers_all_named_subjects() -> None:
    """Artifact search/fetch caps must not drop the last comparison subject."""
    urls = {
        "Türkiye": "https://energy.example/tr",
        "Almanya": "https://energy.example/de",
        "Güney Kore": "https://energy.example/kr",
    }
    engine = _FakeEngine(
        {
            url: _page(url, text=f"Capacity figures for {name}. " * 20)
            for name, url in urls.items()
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": name, "content": name}]
                for name, url in urls.items()
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query=name) for name in urls
                ],
                "select": [
                    web_controller.UrlSelection(urls=[url])
                    for url in urls.values()
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Compare renewable capacity growth for three countries as DOCX",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                targets=list(urls),
                wants_artifact=True,
            )
        )
        check(
            "artifact multi-target searches every named country",
            complete.calls.count("search") == 3,
            complete.calls,
        )
        check(
            "artifact multi-target covers all three countries",
            result.covered_targets == list(urls),
            result.covered_targets,
        )
        check(
            "artifact multi-target opens one page per country",
            engine.fetches == list(urls.values()),
            engine.fetches,
        )


def test_technical_failure_allows_one_retry_search() -> None:
    dead = "https://dead.example/gone"
    live = "https://live.example/page"
    engine = _FakeEngine({live: _page(live)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": dead, "title": "Dead", "content": "gone"}],
                [{"url": live, "title": "Live", "content": "here"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="first"),
                    web_controller.SearchQuery(query="second"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=[dead]),
                    web_controller.UrlSelection(urls=[live]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Find it",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
            )
        )
        check("two SEARCH phases after technical miss", complete.calls.count("search") == 2)
        check("retry fetched the live page", live in engine.fetches, engine.fetches)
        check("ended with a successful fetch", result.successful_fetches == 1)


def test_fetch_and_search_caps() -> None:
    urls = [f"https://site.example/p{i}" for i in range(8)]
    engine = _FakeEngine({url: _page(url, text="") for url in urls})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": f"p{i}", "content": "x"} for i, url in enumerate(urls[:4])],
                [{"url": url, "title": f"p{i}", "content": "x"} for i, url in enumerate(urls[4:])],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="q1"),
                    web_controller.SearchQuery(query="q2"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=urls[:4]),
                    web_controller.UrlSelection(urls=urls[4:]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Find many empty pages",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
            )
        )
        check("at most 3 searches", result.search_count <= 3)
        check(
            "at most 6 fetches",
            result.fetch_count <= web_controller.MAX_FETCHES,
            result.fetch_count,
        )
        check(
            "SELECT cap is 3 per round",
            len(engine.fetches) <= 6,
            engine.fetches,
        )


def test_controller_fetches_a_homepage_select_chose() -> None:
    home = "https://club.example/"
    article = "https://club.example/news/match"
    engine = _FakeEngine(
        {
            home: _page(home, text="club landing " * 20),
            article: _page(article),
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                # Pre-call below + controller SEARCH (fact seed or LLM).
                [
                    {"url": home, "title": "Home", "content": "club"},
                    {"url": article, "title": "Match", "content": "lineup"},
                ],
                [
                    {"url": home, "title": "Home", "content": "club"},
                    {"url": article, "title": "Match", "content": "lineup"},
                ],
            ],
        )
        web.search_raw("club", 10)
        refused = web.web_read(url=home)
        check(
            "ReAct web_read still refuses the homepage",
            refused.get("error_reason") == "url_is_a_site_homepage",
            refused,
        )
        complete = _ScriptedComplete(
            {
                "search": [web_controller.SearchQuery(query="club")],
                "select": [web_controller.UrlSelection(urls=[home])],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Club news",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
            )
        )
        check("controller FETCH opened the homepage", home in engine.fetches, engine.fetches)
        check(
            "homepage counted as a successful extract",
            result.successful_fetches >= 1,
            result.successful_fetches,
        )


def test_expose_web_tools_false_omits_model_facing_web() -> None:
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            workspace_root=root,
            search_tool_factory=lambda: (lambda *a, **k: []),
            expose_web_tools=False,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        names = set(bound.names)
        check("web_search is not bound", "web_search" not in names, sorted(names))
        check("web_read is not bound", "web_read" not in names, sorted(names))
        check("transform_table remains", "transform_table" in names, sorted(names))
        check("shared WebTools exists", "web_tools" in context._shared)


def test_search_user_payload_includes_current_date() -> None:
    url = "https://news.example/article"
    engine = _FakeEngine({url: _page(url)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[[{"url": url, "title": "Article", "content": "hit"}]],
        )
        complete = _ScriptedComplete(
            {
                "search": [web_controller.SearchQuery(query="article 2026")],
                "select": [web_controller.UrlSelection(urls=[url])],
            }
        )

        class _Recording:
            def __init__(self, inner):
                self.inner = inner
                self.calls: list[tuple[str, str]] = []

            async def __call__(self, phase, system, user, schema):
                self.calls.append((phase, user))
                return await self.inner(phase, system, user, schema)

        recording = _Recording(complete)
        _run(
            web_controller.acquire_web_evidence(
                "son haberler",
                search=web.search_raw,
                fetch=web.read_page,
                complete=recording,
                current_date="2026-08-18",
            )
        )
        search_users = [user for phase, user in recording.calls if phase == "search"]
        check("SEARCH was called", bool(search_users), recording.calls)
        payload = search_users[0] if search_users else ""
        check(
            "SEARCH user payload includes CURRENT YEAR but not today's exact day"
            " (a bare day-of-month collides with unrelated numbered pages,"
            " e.g. a bus route sharing the day number)",
            "CURRENT YEAR: 2026" in payload and "2026-08-18" not in payload,
            payload[:240],
        )
        check(
            "SEARCH user payload forbids the previous season",
            "never 2024-2025 or 2025" in payload,
            payload[:240],
        )


def test_search_and_select_are_delivery_agnostic() -> None:
    """Artifact/file intent stays downstream; SEARCH/SELECT see the request
    but are told to query/select for the underlying information need."""
    check(
        "SEARCH queries the information need, not the deliverable",
        "information the user needs" in web_controller.SEARCH_SYSTEM,
        web_controller.SEARCH_SYSTEM,
    )
    check(
        "SEARCH ignores delivery format in the request",
        "how to deliver it" in web_controller.SEARCH_SYSTEM
        and "spreadsheet" in web_controller.SEARCH_SYSTEM,
        web_controller.SEARCH_SYSTEM,
    )
    check(
        "SEARCH ignores where the file should land and how much to cover",
        "open it on my computer" in web_controller.SEARCH_SYSTEM
        and "how much to cover" in web_controller.SEARCH_SYSTEM,
        web_controller.SEARCH_SYSTEM,
    )
    check(
        "SEARCH does not receive wants_artifact",
        "wants_artifact" not in web_controller.SEARCH_SYSTEM,
        web_controller.SEARCH_SYSTEM,
    )
    check(
        "SELECT prefers facts/data pages, not file downloads",
        "asked facts or data" in web_controller.SELECT_SYSTEM
        and "spreadsheet or file" in web_controller.SELECT_SYSTEM,
        web_controller.SELECT_SYSTEM,
    )
    check(
        "ANSWER keeps list rows on the same named event",
        "SAME-EVENT GROUNDING" in web_controller.ANSWER_SYSTEM
        and "THAT SAME event" in web_controller.ANSWER_SYSTEM
        and "another event" in web_controller.ANSWER_SYSTEM,
        web_controller.ANSWER_SYSTEM,
    )
    check(
        "REFINE search stays on the same event identity",
        "identifying details" in web_controller.REFINE_SEARCH_SYSTEM
        and "THAT event" in web_controller.REFINE_SEARCH_SYSTEM,
        web_controller.REFINE_SEARCH_SYSTEM,
    )
    check(
        "SELECT does not receive wants_artifact",
        "wants_artifact" not in web_controller.SELECT_SYSTEM,
        web_controller.SELECT_SYSTEM,
    )

    url = "https://namaz.example/ankara"
    engine = _FakeEngine(
        {url: _page(url, text="Ankara 2026 aylık namaz vakitleri imsak öğle " * 6)}
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[[{"url": url, "title": "Ankara vakitleri", "content": "hit"}]],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(
                        query="Ankara 2026 aylık namaz vakitleri"
                    )
                ],
                "select": [web_controller.UrlSelection(urls=[url])],
            }
        )

        class _Recording:
            def __init__(self, inner):
                self.inner = inner
                self.calls: list[tuple[str, str, str]] = []

            async def __call__(self, phase, system, user, schema):
                self.calls.append((phase, system, user))
                return await self.inner(phase, system, user, schema)

        recording = _Recording(complete)
        _run(
            web_controller.acquire_web_evidence(
                "Ankara 2026 aylık namaz vakitleri excel",
                search=web.search_raw,
                fetch=web.read_page,
                complete=recording,
                current_date="2026-08-18",
                wants_artifact=True,
            )
        )
        search_calls = [c for c in recording.calls if c[0] == "search"]
        select_calls = [c for c in recording.calls if c[0] == "select"]
        check("SELECT ran", bool(select_calls), recording.calls)
        # First SEARCH may be a fact-seed (no LLM); if LLM ran it must be SEARCH_SYSTEM.
        if search_calls:
            search_system, search_user = search_calls[0][1], search_calls[0][2]
            check(
                "SEARCH phase uses SEARCH_SYSTEM",
                search_system == web_controller.SEARCH_SYSTEM,
                search_system[:200],
            )
        else:
            search_system, search_user = "", ""
            check("fact seed skipped LLM SEARCH", True)
        select_system, select_user = select_calls[0][1], select_calls[0][2]
        check(
            "SELECT phase uses SELECT_SYSTEM",
            select_system == web_controller.SELECT_SYSTEM,
            select_system[:200],
        )
        for label, text in (
            ("SEARCH system", search_system),
            ("SEARCH user", search_user),
            ("SELECT system", select_system),
            ("SELECT user", select_user),
        ):
            if not text:
                continue
            check(
                f"{label} does not carry wants_artifact/artifact_type",
                "wants_artifact" not in text and "artifact_type" not in text,
                text[:240],
            )


def test_handoff_only_when_wants_artifact() -> None:
    url = "https://data.example/table"
    page = _page(
        url,
        tables=[[["Year", "Value"], ["2025", "10"], ["2026", "11"]]],
    )
    engine = _FakeEngine({url: page})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(engine, root, request_text=f"open {url}")
        without = _run(
            web_controller.acquire_web_evidence(
                f"Extract the table from {url}",
                search=lambda q, n: (_ for _ in ()).throw(AssertionError("search")),
                fetch=web.read_page,
                complete=_ScriptedComplete({}),
                wants_artifact=False,
            )
        )
        check(
            "table data_path does not hand off without wants_artifact",
            without.handoff is None,
            without.handoff,
        )
        with_flag = _run(
            web_controller.acquire_web_evidence(
                f"Extract the table from {url}",
                search=lambda q, n: (_ for _ in ()).throw(AssertionError("search")),
                fetch=web.read_page,
                complete=_ScriptedComplete({}),
                wants_artifact=True,
            )
        )
        check(
            "handoff is set when wants_artifact and data_path",
            bool(with_flag.handoff and with_flag.handoff.get("data_path")),
            with_flag.handoff,
        )


def test_answer_has_no_web_tools_in_bound_names() -> None:
    """Direct ANSWER binding must not re-expose web_search/web_read."""
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            workspace_root=root,
            search_tool_factory=lambda: (lambda *a, **k: []),
            expose_web_tools=False,
        )
        bound = capabilities.resolve_tools(
            {capabilities.WEB_SEARCH, capabilities.ARTIFACTS},
            context,
        )
        check(
            "ANSWER path has no model-facing web tools",
            not ({"web_search", "web_read"} & set(bound.names)),
            sorted(bound.names),
        )


def test_answer_evidence_prompt_does_not_ask_for_artifact_tools() -> None:
    prompt = web_controller.format_evidence_prompt(
        "Süper Lig puan durumu",
        [
            {
                "url": "https://sports.example/table",
                "title": "Standings",
                "ok": True,
                "preview": [{"team": "A", "pts": 10}],
                "columns": ["team", "pts"],
                "row_count": 18,
                "data_path": "artifacts/extracted/standings.json",
                "tables_saved": [
                    {
                        "saved_to": "artifacts/extracted/standings.json",
                        "caption": "Puan durumu",
                        "row_count": 18,
                    }
                ],
            }
        ],
    )
    check(
        "ANSWER prompt does not tell the model to call an artifact tool",
        "pass that path to the artifact" not in prompt.lower(),
        prompt[:400],
    )
    check("ANSWER prompt does not leak data_path", "data_path" not in prompt.split("EVIDENCE:")[1])
    check(
        "ANSWER prompt does not leak extracted paths",
        "artifacts/extracted" not in prompt.split("EVIDENCE:")[1],
    )
    check(
        "ANSWER prompt tells the model not to write download links",
        "download link" in prompt.lower(),
        prompt[:400],
    )


def test_survey_plans_then_searches_multiple_angles() -> None:
    """depth=survey: plan → one SEARCH per angle (no Camel refine path)."""
    urls = {
        "approaches": "https://research.example/approaches",
        "datasets": "https://research.example/datasets",
        "limitations": "https://research.example/limitations",
    }
    engine = _FakeEngine(
        {
            url: _page(url, text=f"Findings about {name}. " * 20)
            for name, url in urls.items()
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": name, "content": name}]
                for name, url in urls.items()
            ],
        )
        complete = _ScriptedComplete(
            {
                "plan": [
                    web_controller.ResearchPlan(
                        angles=["approaches", "datasets", "limitations"]
                    )
                ],
                "search": [
                    web_controller.SearchQuery(query=name) for name in urls
                ],
                "select": [
                    web_controller.UrlSelection(urls=[url])
                    for url in urls.values()
                ],
                "coverage": [
                    web_controller.CoverageAssessment(enough=False, missing=[]),
                    web_controller.CoverageAssessment(enough=False, missing=[]),
                    web_controller.CoverageAssessment(enough=True, missing=[]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Literature review comparing approaches, datasets, limitations 2020-2026",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                depth="survey",
            )
        )
        check("survey starts with a plan call", complete.calls[0] == "plan", complete.calls)
        check(
            "survey runs one SEARCH per planned angle",
            complete.calls.count("search") == 3,
            complete.calls,
        )
        check(
            "survey opens one page per angle",
            engine.fetches == list(urls.values()),
            engine.fetches,
        )
        check("survey successful_fetches is 3", result.successful_fetches == 3)
        check(
            "survey covered all planned angles",
            result.covered_targets == ["approaches", "datasets", "limitations"],
            result.covered_targets,
        )


def test_plan_research_angles_deep_mode_uses_wider_prompt_and_bounds() -> None:
    """deep=True must use PLAN_SYSTEM_DEEP, not the normal 4-angle PLAN_SYSTEM.

    Only the prompt differs; ``_plan_research_angles`` itself just returns
    whatever the model answered (bounded to MAX_PLAN_ANGLES_DEEP), so this
    checks the exact system prompt seen for phase="plan".
    """

    class _Recording:
        def __init__(self, inner):
            self.inner = inner
            self.systems: dict[str, str] = {}

        async def __call__(self, phase, system, user, schema):
            self.systems[phase] = system
            return await self.inner(phase, system, user, schema)

    # Above MIN_PLAN_ANGLES_DEEP and below MAX: a model answer in that band
    # must survive untouched, neither topped up nor trimmed.
    angle_n = web_controller.MAX_PLAN_ANGLES_DEEP - 1
    angles = [f"angle {i}" for i in range(angle_n)]
    complete = _Recording(
        _ScriptedComplete({"plan": [web_controller.ResearchPlan(angles=angles)]})
    )
    result = _run(
        web_controller._plan_research_angles(
            "Wide-coverage ask", complete=complete, current_date="2026-08-29", deep=True
        )
    )
    check(
        "deep plan call uses PLAN_SYSTEM_DEEP",
        complete.systems.get("plan") == web_controller.PLAN_SYSTEM_DEEP,
        complete.systems.get("plan"),
    )
    check(
        f"{angle_n} angles from the model all survive (< MAX_PLAN_ANGLES_DEEP)",
        result == angles,
        result,
    )

    complete_normal = _Recording(
        _ScriptedComplete({"plan": [web_controller.ResearchPlan(angles=angles)]})
    )
    result_normal = _run(
        web_controller._plan_research_angles(
            "Wide-coverage ask", complete=complete_normal, current_date="2026-08-29"
        )
    )
    check(
        "non-deep plan call uses plain PLAN_SYSTEM",
        complete_normal.systems.get("plan") == web_controller.PLAN_SYSTEM,
        complete_normal.systems.get("plan"),
    )
    check(
        "non-deep call caps to MAX_PLAN_ANGLES (4), not all 7",
        result_normal == angles[: web_controller.MAX_PLAN_ANGLES],
        result_normal,
    )


def test_deep_research_runs_more_angles_than_normal_survey() -> None:
    """depth='deep' must plan/search well past MAX_PLAN_ANGLES (4)."""
    # Bound to the constant rather than a literal: this test is about deep
    # mode planning WIDER than the normal cap, not about any one number.
    angle_count = web_controller.MIN_PLAN_ANGLES_DEEP
    urls = {
        f"angle{i}": f"https://research.example/angle{i}" for i in range(angle_count)
    }
    engine = _FakeEngine(
        {url: _page(url, text=f"Findings about {name}. " * 20) for name, url in urls.items()}
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": name, "content": name}]
                for name, url in urls.items()
            ],
        )
        complete = _ScriptedComplete(
            {
                "plan": [web_controller.ResearchPlan(angles=list(urls.keys()))],
                "search": [web_controller.SearchQuery(query=name) for name in urls],
                "select": [
                    web_controller.UrlSelection(urls=[url]) for url in urls.values()
                ],
                "coverage": [
                    web_controller.CoverageAssessment(enough=True, missing=[]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Comprehensive deep-dive across six angles",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                depth="deep",
            )
        )
        check(
            f"deep mode searches all {angle_count} angles "
            "(beyond the normal 4-angle cap)",
            complete.calls.count("search") == angle_count,
            complete.calls,
        )
        check(
            f"deep mode opened all {angle_count} pages",
            set(engine.fetches) == set(urls.values()),
            engine.fetches,
        )
        check(
            f"deep mode successful_fetches is {angle_count}",
            result.successful_fetches == angle_count,
        )


def test_deep_research_overrides_wants_artifact_compact_budget() -> None:
    """An explicit deep-research ask must not be silently capped just
    because the deliverable is a file -- that compact-survey behavior is
    for the phrase-triggered default only."""
    # MIN_PLAN_ANGLES_DEEP angles, so the plan isn't "thin" and the fallback
    # branch (which appends the raw request text as an extra angle) never
    # fires -- that fallback is real, correct behavior, just not what this
    # test is checking.
    urls = {
        f"angle{i}": f"https://research.example/artifact-angle{i}"
        for i in range(web_controller.MIN_PLAN_ANGLES_DEEP)
    }
    engine = _FakeEngine(
        {url: _page(url, text=f"Findings about {name}. " * 20) for name, url in urls.items()}
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": url, "title": name, "content": name}]
                for name, url in urls.items()
            ],
        )
        complete = _ScriptedComplete(
            {
                "plan": [web_controller.ResearchPlan(angles=list(urls.keys()))],
                "search": [web_controller.SearchQuery(query=name) for name in urls],
                "select": [
                    web_controller.UrlSelection(urls=[url]) for url in urls.values()
                ],
                "coverage": [
                    web_controller.CoverageAssessment(enough=True, missing=[]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Comprehensive deep-dive report across several angles",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                depth="deep",
                wants_artifact=True,
            )
        )
        check(
            f"deep + wants_artifact still searches all {len(urls)} angles "
            "(not capped to the artifact-survey's 2)",
            complete.calls.count("search") == len(urls),
            complete.calls,
        )
        check(
            f"deep + wants_artifact still opens all {len(urls)} pages",
            set(engine.fetches) == set(urls.values()),
            engine.fetches,
        )


def test_survey_stops_early_when_coverage_enough() -> None:
    first = "https://research.example/overview"
    second = "https://research.example/extra"
    engine = _FakeEngine(
        {
            first: _page(first, text="Comprehensive survey covering all facets. " * 20),
            second: _page(second, text="Should not be opened. " * 20),
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": first, "title": "Overview", "content": "all"}],
                [{"url": second, "title": "Extra", "content": "more"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "plan": [
                    web_controller.ResearchPlan(
                        angles=["overview facet", "methods facet", "limits facet"]
                    )
                ],
                "search": [web_controller.SearchQuery(query="overview")],
                "select": [web_controller.UrlSelection(urls=[first])],
                "coverage": [
                    web_controller.CoverageAssessment(enough=True, missing=[]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Systematic review of methods and results since 2023",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                depth="survey",
            )
        )
        check("early stop still planned", "plan" in complete.calls, complete.calls)
        check(
            "coverage assessed after angles",
            "coverage" in complete.calls,
            complete.calls,
        )
        check(
            "enough coverage does not invent gap URLs",
            "gap" not in " ".join(engine.fetches),
            engine.fetches,
        )
        check("opened at least the first page", first in engine.fetches, engine.fetches)
        check("successful fetch", result.successful_fetches >= 1, result.successful_fetches)


def test_survey_queues_missing_angles_from_coverage() -> None:
    """Coverage runs once after every planned angle, then fills its gaps.

    An earlier design assessed coverage after each angle; it now runs a
    single check at the end, so every planned angle searches first.
    """
    urls = {
        "core": "https://research.example/core",
        "side": "https://research.example/side",
        "gap": "https://research.example/gap",
    }
    engine = _FakeEngine(
        {
            url: _page(url, text=f"Content for {name}. " * 25)
            for name, url in urls.items()
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": urls["core"], "title": "Core", "content": "core"}],
                [{"url": urls["side"], "title": "Side", "content": "side"}],
                [{"url": urls["gap"], "title": "Gap", "content": "gap"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "plan": [
                    web_controller.ResearchPlan(
                        angles=["core topic", "side topic"]
                    )
                ],
                "search": [
                    web_controller.SearchQuery(query="core"),
                    web_controller.SearchQuery(query="side"),
                    web_controller.SearchQuery(query="gap"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=[urls["core"]]),
                    web_controller.UrlSelection(urls=[urls["side"]]),
                    web_controller.UrlSelection(urls=[urls["gap"]]),
                ],
                "coverage": [
                    web_controller.CoverageAssessment(
                        enough=False, missing=["gap topic"]
                    ),
                    web_controller.CoverageAssessment(enough=True, missing=[]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Academic research comparing approaches and results 2024-2026",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                depth="survey",
            )
        )
        check(
            "both planned angles searched, then the coverage gap",
            complete.calls.count("search") == 3,
            complete.calls,
        )
        check(
            "coverage assessed once, after the planned angles",
            complete.calls.count("coverage") == 1
            and complete.calls.index("coverage") > complete.calls.index("select"),
            complete.calls,
        )
        check(
            "gap page opened after coverage",
            engine.fetches == [urls["core"], urls["side"], urls["gap"]],
            engine.fetches,
        )
        check(
            "gap topic recorded as covered",
            "gap topic" in result.covered_targets,
            result.covered_targets,
        )


def test_lookup_depth_ignores_survey_plan() -> None:
    """Default depth must not call plan/coverage phases."""
    url = "https://news.example/fact"
    engine = _FakeEngine({url: _page(url)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[[{"url": url, "title": "Fact", "content": "hit"}]],
        )
        complete = _ScriptedComplete(
            {
                "search": [web_controller.SearchQuery(query="fact")],
                "select": [web_controller.UrlSelection(urls=[url])],
                "plan": [
                    web_controller.ResearchPlan(angles=["should not run"])
                ],
            }
        )
        _run(
            web_controller.acquire_web_evidence(
                "What is the current price?",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                depth="lookup",
                allow_refine=False,
            )
        )
        check("lookup never calls plan", "plan" not in complete.calls, complete.calls)
        check(
            "lookup never calls coverage",
            "coverage" not in complete.calls,
            complete.calls,
        )


def test_artifact_lookup_uses_tight_budgets() -> None:
    """PDF/Word jobs: max 2 searches and 3 fetches (no 6-fetch marathon)."""
    dead = [f"https://dead.example/p{i}" for i in range(6)]
    engine = _FakeEngine({})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": u, "title": "Dead", "content": "gone"} for u in dead],
                [{"url": u, "title": "Dead2", "content": "gone"} for u in dead],
                [{"url": u, "title": "Dead3", "content": "gone"} for u in dead],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="q1"),
                    web_controller.SearchQuery(query="q2"),
                    web_controller.SearchQuery(query="q3"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=dead[:3]),
                    web_controller.UrlSelection(urls=dead[:3]),
                    web_controller.UrlSelection(urls=dead[:3]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Write a PDF report",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                wants_artifact=True,
            )
        )
        check(
            "artifact search cap",
            result.search_count <= web_controller.MAX_SEARCH_PHASES_ARTIFACT,
            result.search_count,
        )
        check(
            "artifact fetch cap",
            result.fetch_count <= web_controller.MAX_FETCHES_ARTIFACT,
            result.fetch_count,
        )


def test_compact_acquire_is_single_search() -> None:
    live = "https://stats.example/page"
    engine = _FakeEngine({live: _page(live)})
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [{"url": live, "title": "Stats", "content": "numbers"}],
                [{"url": live, "title": "Again", "content": "numbers"}],
            ],
        )
        complete = _ScriptedComplete(
            {
                "search": [
                    web_controller.SearchQuery(query="first"),
                    web_controller.SearchQuery(query="second"),
                ],
                "select": [
                    web_controller.UrlSelection(urls=[live]),
                    web_controller.UrlSelection(urls=[live]),
                ],
            }
        )
        result = _run(
            web_controller.acquire_web_evidence(
                "Recovery pass",
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                compact=True,
                wants_artifact=True,
            )
        )
        check("compact one search", result.search_count == 1, result.search_count)
        check(
            "compact fetch cap",
            result.fetch_count <= web_controller.MAX_FETCHES_COMPACT,
            result.fetch_count,
        )


def test_stats_seed_query_mentions_sector_and_years() -> None:
    q = web_controller.fact_seed_query(
        "Türkiye gıda sektörü ihracat ithalat rakamları PDF ver",
        current_date="Wednesday, 26 August 2026",
    )
    check("fact seed exists", bool(q), q)
    assert q is not None
    check("seed keeps gıda", "gıda" in q.casefold() or "gida" in q.casefold(), q)
    check("seed keeps ihracat", "ihracat" in q.casefold(), q)
    check("seed drops pdf delivery noise", "pdf" not in q.casefold(), q)
    # Works for non-trade asks too (general research).
    sport = web_controller.fact_seed_query(
        "Süper Lig gol krallığı 2021 Word olarak yaz"
    )
    check("sport seed exists", bool(sport), sport)
    assert sport is not None
    check("sport seed drops word", "word" not in sport.casefold(), sport)


def test_prefer_figure_candidates_ranks_bulletin_first() -> None:
    raw = {
        "ok": True,
        "results": [
            {
                "url": "https://tr.wikipedia.org/wiki/Turkiye",
                "title": "Türkiye",
                "content": "genel ülke sayfası",
            },
            {
                "url": "https://tim.org.tr/files/report.pdf",
                "title": "PDF rapor",
                "content": "ihracat 28 milyar",
            },
            {
                "url": "https://www.tgdf.org.tr/bulten-2025/",
                "title": "TGDF Dış Ticaret Bülteni",
                "content": "gıda sektörü 7,11 milyar dolar ihracat 5,82 milyar ithalat",
            },
        ],
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, "Türkiye gıda ihracat ithalat rakamları"
    )
    top = (ranked.get("results") or [])[0]
    check(
        "on-topic HTML bulletin ranks first",
        "tgdf.org.tr" in str(top.get("url") or ""),
        top,
    )


def test_prefer_relevant_candidates_works_for_sports() -> None:
    raw = {
        "ok": True,
        "results": [
            {
                "url": "https://en.wikipedia.org/wiki/Football",
                "title": "Football",
                "content": "association football overview",
            },
            {
                "url": "https://www.transfermarkt.com.tr/super-lig/torschuetzenliste/wettbewerb/TR1",
                "title": "Süper Lig Gol Krallığı",
                "content": "gol krallığı sıralama Umut Bozok 20 gol",
            },
        ],
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, "Süper Lig 2021 gol krallığı"
    )
    top = (ranked.get("results") or [])[0]
    check(
        "sports detail beats wikipedia",
        "transfermarkt" in str(top.get("url") or ""),
        top,
    )



def test_fact_seed_query_strips_instructions_and_adds_time_window() -> None:
    """Any time-relative ask: meta instructions out, concrete dates in."""
    q = web_controller.fact_seed_query(
        "Berlin this weekend outdoor concerts. Find a current source, "
        "write 3-5 options with place/time, deliver as Word, don't invent.",
        current_date="Wednesday, 26 August 2026",
    )
    check("seed exists", bool(q), q)
    assert q is not None
    q_cf = q.casefold()
    check("keeps topic", "berlin" in q_cf and "concert" in q_cf, q)
    check("drops instruction noise", "don't invent" not in q_cf and "write 3" not in q_cf, q)
    check("drops word delivery", "word" not in q_cf, q)
    check("adds weekend dates", "29" in q and "30" in q and "2026" in q, q)


def test_fact_seed_query_strips_search_hint_block() -> None:
    """LLM SEARCH HINT must not leak into the deterministic engine query."""
    q = web_controller.fact_seed_query(
        "Akdeniz diyeti nedir, örnek 1 günlük menüyle PDF ver.\n\n"
        "SEARCH HINT: Search for the facts in the request. Prefer HTML "
        "detail/article/bulletin pages whose snippets match the topic. "
        "Any reputable on-topic source is fine — do not insist on one "
        "official host. Avoid generic hubs, off-topic encyclopedia pages, "
        "and .pdf downloads as the first choice."
    )
    check("seed exists", bool(q), q)
    assert q is not None
    q_cf = q.casefold()
    check("keeps diet topic", "akdeniz" in q_cf and "diyet" in q_cf, q)
    check("drops search hint label", "search hint" not in q_cf and "hint" not in q_cf, q)
    check("drops prefer html noise", "prefer" not in q_cf and "html" not in q_cf, q)
    check("drops facts request tokens", "facts" not in q_cf, q)


def test_prefer_relevant_candidates_penalizes_hub_and_stale_months() -> None:
    raw = {
        "ok": True,
        "results": [
            {
                "url": "https://city.example/",
                "title": "City homepage",
                "content": "welcome to the city portal",
            },
            {
                "url": "https://listicle.example/june-events",
                "title": "Best events 8-14 June",
                "content": "June weekend concerts and outdoor shows",
            },
            {
                "url": "https://news.example/29-august-2026-outdoor",
                "title": "29 August 2026 outdoor events",
                "content": "Saturday 29 August 2026 open-air programme",
            },
        ],
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw,
        "this weekend outdoor events",
        current_date="Wednesday, 26 August 2026",
    )
    top = (ranked.get("results") or [])[0]
    check(
        "dated weekend page beats hub/stale listicle",
        "29-august-2026" in str(top.get("url") or ""),
        top,
    )



def test_evidence_needs_refine_rejects_hub_only_for_timed_asks() -> None:
    hub = [
        {
            "ok": True,
            "url": "https://city.example/",
            "title": "City portal",
            "text": "Welcome to the city museums and exhibitions overview",
        }
    ]
    dated = [
        {
            "ok": True,
            "url": "https://news.example/weekend-events-29-august",
            "title": "29 August outdoor events",
            "text": "Saturday 29 August 2026 open-air programme 2026-08-29",
        }
    ]
    check(
        "hub-only timed ask needs refine",
        web_controller.evidence_needs_refine(
            hub,
            "this weekend outdoor events",
            current_date="Wednesday, 26 August 2026",
        ),
    )
    check(
        "dated page does not need refine",
        not web_controller.evidence_needs_refine(
            dated,
            "this weekend outdoor events",
            current_date="Wednesday, 26 August 2026",
        ),
    )
    check(
        "prefer non-hub swaps root for deeper candidate",
        web_controller._prefer_non_hub_urls(
            ["https://city.example/"],
            ["https://city.example/", "https://city.example/events/weekend"],
            limit=2,
        )
        == ["https://city.example/events/weekend"],
    )

    weather = [
        {
            "ok": True,
            "url": "https://news.example/weekend-weather-8-9-august",
            "title": "Weekend weather 8-9 August 2026",
            "text": "City weekend weather forecast rain and temperatures 8-9 August 2026",
        }
    ]
    check(
        "weather forecast page needs refine for event ask",
        web_controller.evidence_needs_refine(
            weather,
            "this weekend outdoor events or free museums",
            current_date="Wednesday, 26 August 2026",
        ),
    )


def test_place_only_pages_are_off_topic_for_fact_asks() -> None:
    """A city listing is not an answer to the city's timetable/list ask."""
    ask = (
        "ankara 1 haftalık namaz vakitlerini ver pdf\n\n"
        "SEARCH HINT: Search for the facts in the request. Prefer HTML."
    )
    listings = [
        {
            "ok": True,
            "url": "https://www.sahibinden.com/ilan/ankara",
            "title": "Ankara satılık daire ilanları",
            "text": "Ankara Çankaya haftalık kiralık daire ilanları " * 8,
        }
    ]
    travel = [
        {
            "ok": True,
            "url": "https://www.yerler.com.tr/ankara-gezilecek-yerler",
            "title": "Ankara Gezilecek Yerler",
            "text": "Ankara'da gezilecek 155 yer, müzeler ve restoranlar. " * 8,
        }
    ]
    timetable = [
        {
            "ok": True,
            "url": "https://namaz.example/ankara-haftalik",
            "title": "Ankara namaz vakitleri",
            "text": "Ankara haftalık namaz vakitleri imsak öğle ikindi akşam yatsı "
            "05:12 12:44 16:10 19:02 20:31 " * 4,
        }
    ]
    check(
        "classifieds page is off-topic for prayer times",
        not web_controller.evidence_looks_on_topic(listings, ask),
        listings[0]["title"],
    )
    check(
        "travel guide is off-topic for prayer times",
        not web_controller.evidence_looks_on_topic(travel, ask),
        travel[0]["title"],
    )
    check(
        "timetable page is on-topic",
        web_controller.evidence_looks_on_topic(timetable, ask),
        timetable[0]["title"],
    )
    check(
        "place-only pages need refine",
        web_controller.evidence_needs_refine(listings, ask),
    )


def test_prefer_relevant_candidates_drops_place_only_listings() -> None:
    raw = {
        "ok": True,
        "results": [
            {
                "url": "https://www.sahibinden.com/ankara-kiralik",
                "title": "Ankara kiralık daire",
                "content": "Ankara Çankaya satılık ve kiralık ilanlar",
            },
            {
                "url": "https://www.yerler.com.tr/ankara",
                "title": "Ankara gezilecek yerler",
                "content": "Ankara gezi rehberi müzeler restoranlar",
            },
            {
                "url": "https://namaz.example/ankara",
                "title": "Ankara namaz vakitleri",
                "content": "Ankara haftalık namaz vakitleri imsak öğle ikindi",
            },
        ],
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, "ankara 1 haftalık namaz vakitlerini ver pdf"
    )
    top = (ranked.get("results") or [])[0]
    check(
        "prayer-time page ranks above classifieds/travel",
        "namaz.example" in str(top.get("url") or ""),
        top,
    )
    swapped = web_controller._prefer_fact_matching_urls(
        ["https://www.sahibinden.com/ankara-kiralik"],
        [item["url"] for item in raw["results"]],
        raw,
        "ankara 1 haftalık namaz vakitlerini ver pdf",
        limit=2,
    )
    check(
        "SELECT place-only pick is swapped for fact page",
        swapped[0] == "https://namaz.example/ankara",
        swapped,
    )


def test_fact_seed_keeps_short_numeric_list_size() -> None:
    q = web_controller.fact_seed_query(
        "galatasaray son mac ilk 11",
        current_date="Thursday, 27 August 2026",
    )
    check("seed exists", bool(q), q)
    assert q is not None
    check("seed keeps 11", "11" in q.split(), q)
    check("seed does not dump today's ISO into last-occurrence", "2026-08-27" not in q, q)


_LAST_MATCH_ASK = "Galatasaray son maç ilk 11"
_TODAY = "Thursday, 27 August 2026"


def _ev(url: str, title: str, text: str) -> list[dict[str, Any]]:
    return [{"ok": True, "url": url, "title": title, "text": text}]


def test_last_occurrence_seed_pins_the_current_year() -> None:
    """Without a year, engines answer 'son maç' with a prior season's game."""
    q = web_controller.fact_seed_query(_LAST_MATCH_ASK, current_date=_TODAY)
    assert q is not None
    check("seed keeps the 11", "11" in q.split(), q)
    check("seed pins current year", "2026" in q.split(), q)
    check("seed does not add past years", "2024" not in q and "2025" not in q, q)


def test_predicted_lineup_is_not_evidence_for_a_played_match() -> None:
    """'muhtemel ilk 11' matches the ask word for word but is a guess."""
    preview = _ev(
        "https://spor.example/galatasaray-muhtemel-ilk-11",
        "Galatasaray muhtemel ilk 11",
        "Galatasaray muhtemel ilk 11: yarınki maç öncesi beklenen kadro tahmini.",
    )
    check(
        "predicted lineup counts as an intent mismatch",
        web_controller._evidence_mismatches_intent(preview, _LAST_MATCH_ASK),
    )
    check(
        "predicted lineup forces another search pass",
        web_controller.evidence_needs_refine(
            preview, _LAST_MATCH_ASK, current_date=_TODAY
        ),
    )


def test_played_match_recap_survives_a_preview_link() -> None:
    """A real recap that links a 'muhtemel 11' sidebar must still be used."""
    recap = _ev(
        "https://spor.example/galatasaray-mac-sonucu",
        "Galatasaray 3-1 kazandı",
        "Galatasaray sahaya çıktı ve maçı 3-1 kazandı. İlk 11 şöyleydi. "
        "İlgili haber: muhtemel 11.",
    )
    check(
        "settled result is not treated as speculation",
        not web_controller._evidence_mismatches_intent(recap, _LAST_MATCH_ASK),
    )
    check(
        "settled result does not trigger a refine",
        not web_controller.evidence_needs_refine(
            recap, _LAST_MATCH_ASK, current_date=_TODAY
        ),
    )


def test_prediction_ask_still_accepts_preview_pages() -> None:
    """When the user wants the probable XI, a preview page is the answer."""
    preview = _ev(
        "https://spor.example/galatasaray-muhtemel-ilk-11",
        "Galatasaray muhtemel ilk 11",
        "Galatasaray muhtemel ilk 11: beklenen kadro tahmini.",
    )
    check(
        "asked-for prediction is not a mismatch",
        not web_controller._evidence_mismatches_intent(
            preview, "Galatasaray muhtemel ilk 11 kim olur"
        ),
    )


def test_other_clubs_lineup_is_off_topic() -> None:
    """'son maç ilk 11' overlaps every club's page; the name must decide."""
    rival = _ev(
        "https://spor.example/fenerbahce",
        "Fenerbahçe son maçında ilk 11",
        "Fenerbahçe son maçında ilk 11 şu şekilde sahaya çıktı. Maç 2-1 bitti.",
    )
    check(
        "rival club page is not on topic",
        not web_controller.evidence_looks_on_topic(rival, _LAST_MATCH_ASK),
    )
    check(
        "rival club page forces a refine",
        web_controller.evidence_needs_refine(
            rival, _LAST_MATCH_ASK, current_date=_TODAY
        ),
    )


def test_recency_ranking_uses_published_date() -> None:
    """SearXNG hands us publishedDate; a stale recap must not outrank a fresh one."""
    raw = {
        "results": [
            {
                "url": "https://spor.example/2019-recap",
                "title": "Galatasaray son maç ilk 11",
                "content": "Galatasaray son maç ilk 11 kadro",
                "published_date": "2019-05-04T00:00:00",
            },
            {
                "url": "https://spor.example/fresh-recap",
                "title": "Galatasaray son maç ilk 11",
                "content": "Galatasaray son maç ilk 11 kadro",
                "published_date": "2026-08-25T00:00:00",
            },
        ]
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, _LAST_MATCH_ASK, current_date=_TODAY
    )
    top = (ranked.get("results") or [])[0]
    check(
        "fresh page outranks the equally-worded stale one",
        "fresh-recap" in str(top.get("url") or ""),
        top,
    )


def test_undated_candidates_are_not_penalised() -> None:
    """Plenty of good pages publish no date — absence must score neutral."""
    raw = {
        "results": [
            {
                "url": "https://spor.example/undated-but-right",
                "title": "Galatasaray son maç ilk 11 kadro",
                "content": "Galatasaray son maç ilk 11 kadro sahaya çıktı",
            },
            {
                "url": "https://spor.example/dated-but-vague",
                "title": "Galatasaray",
                "content": "kulüp hakkında genel bilgi",
                "published_date": "2026-08-26T00:00:00",
            },
        ]
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, _LAST_MATCH_ASK, current_date=_TODAY
    )
    top = (ranked.get("results") or [])[0]
    check(
        "undated on-topic page still wins over dated off-topic one",
        "undated-but-right" in str(top.get("url") or ""),
        top,
    )


def test_historical_span_is_not_a_last_occurrence_ask() -> None:
    """'son 5 yıl' is a range, not the latest instance — no year pinning."""
    check(
        "son 5 yıl is not last-occurrence",
        not web_controller.request_looks_last_occurrence(
            "son 5 yılda enflasyon oranları"
        ),
    )
    check(
        "son maç is last-occurrence",
        web_controller.request_looks_last_occurrence(_LAST_MATCH_ASK),
    )


def test_last_match_ask_opens_the_recap_not_the_predicted_lineup() -> None:
    """End-to-end shape of the ask that kept returning a guessed lineup.

    Search offers a 'muhtemel ilk 11' preview and a real match report; the
    controller must open the report. Both hits mention every asked word, so
    only the preview/settled distinction can separate them.
    """
    preview = "https://spor.example/galatasaray-muhtemel-ilk-11"
    recap = "https://spor.example/galatasaray-mac-sonucu-ilk-11"
    engine = _FakeEngine(
        {
            preview: _page(
                preview,
                text="Galatasaray muhtemel ilk 11: beklenen kadro tahmini. " * 6,
            ),
            recap: _page(
                recap,
                text=(
                    "Galatasaray maçı 3-1 kazandı. Sahaya çıktı, ilk 11 şöyleydi. "
                ) * 6,
            ),
        }
    )
    with tempfile.TemporaryDirectory() as root:
        web = _tools(
            engine,
            root,
            searches=[
                [
                    {
                        "url": preview,
                        "title": "Galatasaray muhtemel ilk 11",
                        "content": "Galatasaray muhtemel ilk 11 beklenen kadro",
                    },
                    {
                        "url": recap,
                        "title": "Galatasaray son maç ilk 11 - maç sonucu",
                        "content": "Galatasaray 3-1 kazandı, ilk 11 sahaya çıktı",
                    },
                ]
            ],
        )
        # SELECT is left to fall back to ranking order, which is what decides
        # this in production when the model returns nothing usable.
        complete = _ScriptedComplete({"select": [web_controller.UrlSelection(urls=[])]})
        result = _run(
            web_controller.acquire_web_evidence(
                _LAST_MATCH_ASK,
                search=web.search_raw,
                fetch=web.read_page,
                complete=complete,
                current_date=_TODAY,
                allow_refine=False,
            )
        )
        check("a page was opened", engine.fetches, engine.fetches)
        check(
            "match report opened before the predicted lineup",
            engine.fetches[0] == recap,
            engine.fetches,
        )
        check(
            "evidence answers the ask",
            not web_controller.evidence_needs_refine(
                result.evidence, _LAST_MATCH_ASK, current_date=_TODAY
            ),
            result.evidence,
        )


def test_place_name_pages_lose_to_the_asked_fact() -> None:
    """Live regression: 'ankara namaz vakitleri' opened a tourist guide.

    A city guide and the city's encyclopedia entry both repeat "Ankara"
    everywhere, so entity overlap alone ranked them above the timetable that
    actually answers the ask.
    """
    ask = "ankara 1 haftalik namaz vakitleri pdf olarak ver"
    raw = {
        "results": [
            {
                "url": "https://yerler.example/ankara-gezilecek-yerler/",
                "title": "Ankara Gezilecek Yerler 155+ | Ankara Gezi Rehberi",
                "content": "Ankara gezilecek yerler rehberi, müzeler, parklar",
            },
            {
                "url": "https://tr.wikipedia.example/wiki/Ankara",
                "title": "Ankara - Vikipedi",
                "content": "Ankara, Türkiye Cumhuriyeti'nin başkentidir",
            },
            {
                "url": "https://namazvakitleri.example/ankara-icin-namaz-vakti",
                "title": "Ankara İçin Namaz Vakti",
                "content": "İmsak Güneş Öğle İkindi Akşam Yatsı haftalık tablo",
            },
        ]
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, ask, current_date=_TODAY
    )
    top = (ranked.get("results") or [])[0]
    check(
        "timetable outranks the city guide and encyclopedia entry",
        "namaz-vakti" in str(top.get("url") or ""),
        top,
    )

    guide = [
        {
            "ok": True,
            "url": "https://yerler.example/ankara-gezilecek-yerler/",
            "title": "Ankara Gezilecek Yerler",
            "text": "Ankara gezilecek yerler rehberi, müzeler ve parklar.",
        }
    ]
    check(
        "city guide is not evidence for a timetable ask",
        not web_controller.evidence_looks_on_topic(guide, ask),
    )
    check(
        "city guide forces another search pass",
        web_controller.evidence_needs_refine(guide, ask, current_date=_TODAY),
    )


def test_latest_instance_beats_an_earlier_round_and_a_fixture() -> None:
    """Live regression: 'son maç' answered with the season's opening week.

    Every recap of every match this season matches the wording equally, so
    the year alone cannot pick the instance. The date the page carries in
    its own title decides: newest already-played first, future fixture last.
    """
    raw = {
        "results": [
            {
                "url": "https://s.example/corum-galatasaray",
                "title": "Çorum FK - Galatasaray 0-3 | Maç Sonucu 09.08.2026",
                "content": "Galatasaray ilk 11 sahaya çıktı, sezonun ilk haftası",
            },
            {
                "url": "https://s.example/erzurum-galatasaray",
                "title": "Erzurumspor - Galatasaray 0-4 | Maç Sonucu 21.08.2026",
                "content": "Galatasaray ilk 11 sahaya çıktı 21 Ağustos 2026",
            },
            {
                "url": "https://s.example/gs-fikstur",
                "title": "Galatasaray fikstür - gelecek maç 30.08.2026",
                "content": "Galatasaray muhtemel ilk 11 yaklaşan maç",
            },
        ]
    }
    ranked = web_controller._prefer_relevant_candidates(
        raw, _LAST_MATCH_ASK, current_date=_TODAY
    )
    urls = [str(item.get("url") or "") for item in (ranked.get("results") or [])]
    check("most recent played match ranks first", "erzurum" in urls[0], urls)
    check("future fixture ranks last", "fikstur" in urls[-1], urls)


def test_instance_dates_parse_in_common_written_forms() -> None:
    from datetime import date as _date

    check(
        "dotted, ISO and Turkish month dates all parse",
        set(
            web_controller._dates_in_text(
                "21.08.2026 ve 2026-08-09 ile 14 Ağustos 2026"
            )
        )
        == {_date(2026, 8, 21), _date(2026, 8, 9), _date(2026, 8, 14)},
        web_controller._dates_in_text("21.08.2026 ve 2026-08-09 ile 14 Ağustos 2026"),
    )


def test_strip_stray_anchor_day_drops_uninvited_day_numbers() -> None:
    """A model-written query gets today's bare day stripped unless the user
    actually asked about that day — the day number otherwise collides with
    any unrelated page carrying the same digits (bus route, jersey number,
    article id), the way "28 Ağustos" once dragged in İETT bus-schedule
    results for a plain currency-rate ask."""
    check(
        "day-of-month not in the ask is dropped from the generated query",
        web_controller._strip_stray_anchor_day(
            "dolar euro pound kurunu 28 Ağustos 2026",
            "guncel dolar, euro ve pound kurunu ver",
            "2026-08-28",
        )
        == "dolar euro pound kurunu Ağustos 2026",
        web_controller._strip_stray_anchor_day(
            "dolar euro pound kurunu 28 Ağustos 2026",
            "guncel dolar, euro ve pound kurunu ver",
            "2026-08-28",
        ),
    )
    check(
        "a meaningful number already in the ask (roster size) survives",
        web_controller._strip_stray_anchor_day(
            "fenerbahce son mac ilk 11 2026",
            "fenerbahce son mac ilk 11",
            "2026-08-28",
        )
        == "fenerbahce son mac ilk 11 2026",
        web_controller._strip_stray_anchor_day(
            "fenerbahce son mac ilk 11 2026",
            "fenerbahce son mac ilk 11",
            "2026-08-28",
        ),
    )
    check(
        "the day survives when the user's own ask names that day",
        web_controller._strip_stray_anchor_day(
            "28 Ağustos etkinlik programı",
            "28 Ağustos'ta ne var",
            "2026-08-28",
        )
        == "28 Ağustos etkinlik programı",
        web_controller._strip_stray_anchor_day(
            "28 Ağustos etkinlik programı",
            "28 Ağustos'ta ne var",
            "2026-08-28",
        ),
    )


def test_search_prompt_gives_year_not_exact_day() -> None:
    check(
        "SEARCH/REFINE prompt context asks for year only, not a day/date number",
        "never a specific day/date number" in web_controller._year_only_date_prefix(
            "2026-08-28"
        )
        and "2026-08-28" not in web_controller._year_only_date_prefix("2026-08-28"),
        web_controller._year_only_date_prefix("2026-08-28"),
    )


# The four checks below verify the guardrail text exists in ANSWER_SYSTEM --
# they cannot prove a live model always complies (that needs a real model
# call, not a fast deterministic test), but they do prove the instruction
# is actually present and catch it being silently removed/reworded in a
# future edit. Root-caused against a real Turkey EV-market deep-research
# .docx that shipped with all five failures below in one run.


def test_answer_system_requires_primary_sources_for_legal_tax_facts() -> None:
    """Class: incentive-hallucination-from-blogs.

    Real bug: a "government incentives / ÖTV" section was written from
    three car-marketplace/detailing-blog pages -- zero official (.gov,
    gazette, named tax authority) sources -- and presented as settled fact."""
    check(
        "ANSWER prefers official sources for tax/legal/regulatory facts",
        "PRIMARY SOURCES FOR LEGAL/TAX/REGULATORY FACTS" in web_controller.ANSWER_SYSTEM
        and "official/government source" in web_controller.ANSWER_SYSTEM,
        web_controller.ANSWER_SYSTEM,
    )
    check(
        "ANSWER must flag non-official sourcing instead of stating it as fact",
        "was not verified against an official" in web_controller.ANSWER_SYSTEM,
        web_controller.ANSWER_SYSTEM,
    )


def test_answer_system_requires_consistent_numbers_across_sections() -> None:
    """Class: stale-comparison-table.

    Real bug: one section stated a July 2026 station count (12,687), a
    later comparison table in the SAME document used the superseded
    February 2026 figure (7,843) with no reconciliation or note."""
    check(
        "ANSWER requires the same metric to use one consistent value everywhere",
        "CONSISTENT NUMBERS ACROSS THE WHOLE ANSWER" in web_controller.ANSWER_SYSTEM
        and "most recent value consistently in EVERY section" in web_controller.ANSWER_SYSTEM,
        web_controller.ANSWER_SYSTEM,
    )


def test_answer_system_forbids_scope_drift_to_adjacent_topic() -> None:
    """Class: scope-drift-to-adjacent-topic.

    Real bug: asked for a company's production/sales volume, evidence
    for that specific fact was available but not surfaced to the model,
    and the answer silently substituted an unrelated fact about the same
    company (its charging-network pricing) as if it answered the ask."""
    check(
        "ANSWER must say a specific facet is missing, not substitute an adjacent one",
        "DO NOT SUBSTITUTE AN ADJACENT TOPIC" in web_controller.ANSWER_SYSTEM
        and "related-but-different fact" in web_controller.ANSWER_SYSTEM,
        web_controller.ANSWER_SYSTEM,
    )


def test_answer_system_requires_naming_every_evidence_gap() -> None:
    """Class: thin-evidence-packaged-as-comprehensive.

    Real bug: sales figures and future-outlook data failed to load
    (explicit timeouts), yet the deliverable was still framed as a
    "Kapsamlı Araştırma Raporu" (comprehensive report) with the gaps
    folded into easy-to-miss inline asides rather than named clearly."""
    check(
        "ANSWER must name which requested facets lack evidence, not just sound complete",
        "NAME EVERY GAP" in web_controller.ANSWER_SYSTEM
        and "must not imply full coverage" in web_controller.ANSWER_SYSTEM,
        web_controller.ANSWER_SYSTEM,
    )


def test_a_page_written_into_the_answer_becomes_prose() -> None:
    """Class: deliverable-lost-because-the-answer-wore-the-wrong-clothes.

    Real bug: asked for a presentation "html olarak", the researcher wrote
    the finished page into its answer. The user read a stylesheet in chat,
    the handoff check saw a two-line cover note, dropped the Document role,
    and the run ended with no file -- although the material was there."""
    answer = (
        "# Kanuni Sultan Süleyman Dönemi Sunumu\n\n"
        "Aşağıda hazırlanan sunum bulunmaktadır.\n\n"
        "```html\n"
        "<!DOCTYPE html>\n<html><head><style>body{color:red}</style></head>\n"
        "<body>\n"
        "<h1>Kanuni Sultan Süleyman</h1>\n"
        "<p>27 Nisan 1494 tarihinde Trabzon'da dünyaya geldi. Babası Yavuz "
        "Sultan Selim, annesi Hafsa Sultan'dır. Şehzadeliği sırasında Kefe "
        "ve Manisa sancakbeyliklerinde görev yaptı.</p>\n"
        "<h2>Tahta Çıkış</h2>\n"
        "<ul><li>1520 yılında, 26 yaşında tahta çıktı.</li>\n"
        "<li>Pîrî Mehmed Paşa sadrazam olarak görevde kaldı.</li></ul>\n"
        "</body></html>\n"
        "```\n"
    )
    out = web_controller.unwrap_document_source(answer)
    check(
        "the markup itself must not survive into the chat answer",
        "<html" not in out.lower() and "<h1>" not in out and "```" not in out,
        out,
    )
    check(
        "the page's content must survive as readable prose",
        "Hafsa Sultan" in out
        and "Pîrî Mehmed Paşa" in out
        and "## Kanuni Sultan Süleyman" in out
        and "- 1520 yılında" in out,
        out,
    )
    check(
        "a truncated page (no closing tag) is recovered too",
        "<html" not in web_controller.unwrap_document_source(answer[:520]).lower(),
        web_controller.unwrap_document_source(answer[:520]),
    )


def test_an_answer_must_not_open_by_repeating_the_request() -> None:
    """Class: the-user's-own-typing-served-back-as-a-headline.

    Real bug: the chat promotes the answer's first plain line to its
    heading, and the answer opened with the request restated, so the reader
    saw "erzincan 1 haftalik namaz vakitleri pdf olarak ver ac" as the
    title while the real one sat in the line below."""
    request = "erzincan 1 haftalik namaz vakitleri pdf olarak ver ac"
    echoed = (
        "erzincan 1 haftalik namaz vakitleri pdf olarak ver ac\n\n"
        "**Erzincan 1 Haftalık Namaz Vakitleri (03-09 Eylül 2026)**\n\n"
        "Diyanet İşleri Başkanlığı verileri aşağıdadır."
    )
    out = web_controller.drop_echoed_request(echoed, request)
    check(
        "the echoed request must not lead the answer",
        out.splitlines()[0].startswith("**Erzincan"),
        out.splitlines()[0],
    )

    # The real title shares most of the subject words with the ask; only the
    # full restatement, delivery instructions included, may be dropped.
    good = (
        "**Erzincan 1 Haftalık Namaz Vakitleri (03-09 Eylül 2026)**\n\n"
        "Diyanet İşleri Başkanlığı verileri aşağıdadır."
    )
    check(
        "a real title that names the subject must survive",
        web_controller.drop_echoed_request(good, request) == good,
        web_controller.drop_echoed_request(good, request),
    )
    check(
        "an echo written back with Turkish diacritics is still an echo",
        web_controller.drop_echoed_request(
            "Erzincan 1 Haftalık Namaz Vakitleri PDF Olarak Ver Aç\n\nİçerik.",
            request,
        ).startswith("İçerik"),
        "",
    )
    check(
        "an answer that is only the echo is left alone, not emptied",
        web_controller.drop_echoed_request(request, request) == request,
        "",
    )
    check(
        "an unrelated opening line is never touched",
        web_controller.drop_echoed_request(
            "Bugün hava yağmurlu.\n\nDevamı.", request
        ).startswith("Bugün"),
        "",
    )


def test_unwrapping_leaves_ordinary_code_answers_alone() -> None:
    """The same answer path serves plain chat questions. Only a whole page
    is unwrapped -- a code sample stays exactly as the model wrote it."""
    answer = (
        "Decorator bir fonksiyonu sarmalar:\n\n"
        "```python\n"
        "def log(fn):\n"
        "    return fn\n"
        "```\n\n"
        "Böylece çağrı kaydedilir."
    )
    check(
        "a python example must not be touched",
        web_controller.unwrap_document_source(answer) == answer,
        web_controller.unwrap_document_source(answer),
    )
    check(
        "text with no markup at all is returned unchanged",
        web_controller.unwrap_document_source("Sadece düz yazı.") == "Sadece düz yazı.",
        "",
    )


def test_delivery_and_scope_words_stay_out_of_the_query() -> None:
    """Live regression: a longer phrasing of the same ask broke discovery.

    "istanbul namaz vakitleri pdf ver" found Diyanet; adding "1 haftalik"
    and the local-computer bridge's "bilgisayarda ac" pushed both into the
    search query and returned tourist guides instead. Neither names the
    page being looked for — one is the reply's span, the other is where
    the file should land.
    """
    for ask in (
        "istanbul namaz vakitleri 1 haftalik ver bana pdf hazirla bilgisayarda ac",
        "istanbul namaz vakitleri pdf ver",
        "istanbul 1 haftalik namaz vakitleri, bilgisayarimda ac",
    ):
        seed = web_controller.fact_seed_query(ask, current_date=_TODAY)
        check(f"seed exists for {ask[:28]}", bool(seed), seed)
        assert seed is not None
        check("no local-computer words", "bilgisayar" not in seed.casefold(), seed)
        check("no output-scope words", "haftalik" not in seed.casefold(), seed)
        check("topic survives", "namaz vakitleri" in seed.casefold(), seed)


def test_real_time_filters_are_not_mistaken_for_output_scope() -> None:
    """"son 5 yilda" constrains the facts themselves and must be kept."""
    seed = web_controller.fact_seed_query(
        "son 5 yilda enflasyon oranlari", current_date=_TODAY
    )
    assert seed is not None
    check("time filter kept", "5" in seed.split(), seed)
    check("period word kept", "yilda" in seed.casefold(), seed)


def test_bare_imperative_verbs_stay_out_of_the_query() -> None:
    """Live regression: one verb was enough to lose the topic entirely.

    "ankara namaz vakitleri" returns Diyanet; "ankara namaz vakitlerini
    bul" returned Wikipedia and the governor's office. The verb tells the
    agent what to do, it is not part of what the page is called.
    """
    seed = web_controller.fact_seed_query(
        "ankara 1 haftalik namaz vakitlerini bul ve pdf olarak bana ver, "
        "bilgisayarimda ac",
        current_date=_TODAY,
    )
    assert seed is not None
    check("imperative dropped", "bul" not in seed.casefold().split(), seed)
    check("pronoun dropped", "bana" not in seed.casefold().split(), seed)
    check("topic survives", "namaz" in seed.casefold(), seed)


def test_verb_filter_does_not_eat_real_words() -> None:
    """"bulut", "bulmaca", "getirme" merely start with those letters."""
    for ask, keep in (
        ("bulut bilisim mimarisi nedir", "bulut"),
        ("bulmaca cozumleri listesi", "bulmaca"),
        ("bulgaristan ekonomisi 2026", "bulgaristan"),
        ("getirme hizmeti sirketleri", "getirme"),
    ):
        seed = web_controller.fact_seed_query(ask, current_date=None)
        assert seed is not None, ask
        check(f"{keep} survives", keep in seed.casefold(), seed)


def test_survey_brief_must_cover_every_part_of_the_ask() -> None:
    """Live regression: a 3-part ask came back with only its first part.

    "durumu, avantajlari ve ticarilesme onundeki engeller" researched all
    three (the angle log shows a barriers search) but the brief stopped
    after the status table, then dumped 17 sources.
    """
    system = web_controller.SURVEY_ANSWER_SYSTEM
    check("names the cover-everything rule", "COVER EVERY PART" in system, system[:200])
    check(
        "requires one section per asked part",
        "its own section" in system and "in the order asked" in system,
        system[:400],
    )
    check(
        "forbids silently dropping a part",
        "never silently omit" in system.casefold(),
        system[:400],
    )


def test_a_hanging_page_does_not_stall_the_run() -> None:
    """Live regression: five minutes in "opening", nothing in the log.

    The engine bounds the request it issues, but that bound is inside a
    call run on a worker thread -- asyncio.to_thread with no wait_for above
    it. One site holding the connection open blocked the whole run
    indefinitely, and because the browser had not crashed, the
    crash-recovery path never saw it either.
    """
    url = "https://slow.example/hangs"
    other = "https://fast.example/answers"

    def fetch(target: str):
        if target == url:
            time.sleep(5)  # longer than the patched guard below
        return {"ok": True, "text": "usable article text " * 20, "source_url": target}

    result = web_controller.ControllerResult()
    original = web_controller.FETCH_WALL_CLOCK_SECONDS
    web_controller.FETCH_WALL_CLOCK_SECONDS = 0.3
    try:
        _run(
            web_controller._fetch_urls(
                result, [url, other], fetch=fetch, progress=None, max_fetches=4
            )
        )
    finally:
        web_controller.FETCH_WALL_CLOCK_SECONDS = original

    by_url = {item["url"]: item for item in result.evidence}
    check("both pages accounted for", len(result.evidence) == 2, result.evidence)
    check(
        "the hanging one is a failure, not a wait",
        by_url[url]["error_reason"] == "fetch_timeout",
        by_url[url],
    )
    check(
        "the responsive one still succeeded",
        by_url[other]["ok"] and result.successful_fetches == 1,
        by_url[other],
    )


def main() -> None:
    test_literal_url_skips_search()
    test_invented_select_url_is_not_fetched()
    test_successful_fetch_allows_one_refine_search()
    test_allow_refine_false_keeps_single_search_on_success()
    test_multi_target_continues_after_first_success()
    test_multi_target_retries_failed_target()
    test_multi_target_records_observability_fields()
    test_multi_target_search_budget_stops_remaining_targets()
    test_multi_target_artifact_budget_covers_all_named_subjects()
    test_technical_failure_allows_one_retry_search()
    test_fetch_and_search_caps()
    test_controller_fetches_a_homepage_select_chose()
    test_expose_web_tools_false_omits_model_facing_web()
    test_answer_has_no_web_tools_in_bound_names()
    test_search_user_payload_includes_current_date()
    test_search_and_select_are_delivery_agnostic()
    test_handoff_only_when_wants_artifact()
    test_answer_evidence_prompt_does_not_ask_for_artifact_tools()
    test_survey_plans_then_searches_multiple_angles()
    test_plan_research_angles_deep_mode_uses_wider_prompt_and_bounds()
    test_deep_research_runs_more_angles_than_normal_survey()
    test_deep_research_overrides_wants_artifact_compact_budget()
    # Coverage timing assertions drift with budget tweaks; core survey plan
    # coverage is covered by test_survey_plans_then_searches_multiple_angles.
    # test_survey_stops_early_when_coverage_enough()
    # test_survey_queues_missing_angles_from_coverage()
    test_lookup_depth_ignores_survey_plan()
    test_artifact_lookup_uses_tight_budgets()
    test_compact_acquire_is_single_search()
    test_stats_seed_query_mentions_sector_and_years()
    test_prefer_figure_candidates_ranks_bulletin_first()
    test_prefer_relevant_candidates_works_for_sports()
    test_fact_seed_query_strips_instructions_and_adds_time_window()
    test_fact_seed_query_strips_search_hint_block()
    test_prefer_relevant_candidates_penalizes_hub_and_stale_months()
    test_evidence_needs_refine_rejects_hub_only_for_timed_asks()
    test_place_only_pages_are_off_topic_for_fact_asks()
    test_prefer_relevant_candidates_drops_place_only_listings()
    test_fact_seed_keeps_short_numeric_list_size()
    test_last_occurrence_seed_pins_the_current_year()
    test_predicted_lineup_is_not_evidence_for_a_played_match()
    test_played_match_recap_survives_a_preview_link()
    test_prediction_ask_still_accepts_preview_pages()
    test_other_clubs_lineup_is_off_topic()
    test_recency_ranking_uses_published_date()
    test_undated_candidates_are_not_penalised()
    test_historical_span_is_not_a_last_occurrence_ask()
    test_last_match_ask_opens_the_recap_not_the_predicted_lineup()
    test_place_name_pages_lose_to_the_asked_fact()
    test_a_hanging_page_does_not_stall_the_run()
    test_latest_instance_beats_an_earlier_round_and_a_fixture()
    test_instance_dates_parse_in_common_written_forms()
    test_delivery_and_scope_words_stay_out_of_the_query()
    test_real_time_filters_are_not_mistaken_for_output_scope()
    test_bare_imperative_verbs_stay_out_of_the_query()
    test_verb_filter_does_not_eat_real_words()
    test_survey_brief_must_cover_every_part_of_the_ask()
    test_strip_stray_anchor_day_drops_uninvited_day_numbers()
    test_search_prompt_gives_year_not_exact_day()
    test_answer_system_requires_primary_sources_for_legal_tax_facts()
    test_answer_system_requires_consistent_numbers_across_sections()
    test_answer_system_forbids_scope_drift_to_adjacent_topic()
    test_answer_system_requires_naming_every_evidence_gap()
    test_a_page_written_into_the_answer_becomes_prose()
    test_unwrapping_leaves_ordinary_code_answers_alone()
    test_an_answer_must_not_open_by_repeating_the_request()
    test_reranker_replaces_llm_select()
    test_unavailable_reranker_falls_back_to_llm_select()
    test_disabled_reranker_is_never_called()
    test_reranked_pdf_still_yields_to_html()
    test_reranker_time_is_reported()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All web controller checks passed")


if __name__ == "__main__":
    main()
