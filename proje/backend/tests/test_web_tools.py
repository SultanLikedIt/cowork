"""Behavioural coverage for the web_search / web_read split (web_tools.py).

Validates the general architecture -- infrastructure/safety in code,
semantic judgment left to the agent -- not specific historical regressions.
Covers: budgets/caching, result/text size bounds, data_path/artifact
handoff, and that a multi-table page exposes every table (with captions)
rather than picking one for the agent. Does NOT test source authorization,
location guards, or search re-ranking by keyword/site-name/currentness --
none of that exists any more, on purpose (see web_tools.py's module
docstring). Frontier/session/query-planning behaviour is also out of scope
because it no longer exists.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import capabilities
from artifacts import ArtifactTools
from html_extract import AcquiredPage, AcquisitionError, AcquisitionResult, extract_page
from web_fetch import MAX_INLINE_TABLE_ROWS, WebFetchTools
import web_tools

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        # Raise immediately (rather than only recording to `failures`) so a
        # failed check actually fails the test under pytest, not just under
        # this file's own `python test_web_tools.py` __main__ runner.
        raise AssertionError(f"{name} -- {detail}")


class _FakeEngine:
    def __init__(
        self,
        pages_by_url: dict[str, AcquiredPage],
        retry_pages_by_url: dict[str, AcquiredPage] | None = None,
    ) -> None:
        self.pages_by_url = pages_by_url
        # Returned only when _read's javascript_required retry calls
        # acquire() a second time with a longer delay_before_return_html --
        # defaults to the same page (no retry-specific fixture needed) so
        # existing single-page tests are unaffected.
        self.retry_pages_by_url = retry_pages_by_url
        self.fetches: list[str] = []
        self.acquire_delays: list[float] = []

    def acquire(
        self, url: str, need: str, *, delay_before_return_html: float = 1.5
    ) -> AcquisitionResult:
        self.fetches.append(url)
        self.acquire_delays.append(delay_before_return_html)
        is_retry = delay_before_return_html != 1.5
        source = (
            self.retry_pages_by_url
            if is_retry and self.retry_pages_by_url is not None
            else self.pages_by_url
        )
        page = source.get(url)
        if page is None:
            raise AcquisitionError("not_found", f"{url} returned HTTP 404.")
        return AcquisitionResult(pages=[page])


def _bound_tools(engine: _FakeEngine, root: str, **context_kwargs):
    # Left patched on return, deliberately: the caller still needs to CALL
    # web_read after this returns, and get_crawl4ai_engine() is looked up
    # fresh (module global, not a bound default) at call time inside
    # web_tools._read -- restoring the patch here, before the tool is
    # actually invoked, would silently fall back to the real engine.
    web_tools.get_crawl4ai_engine = lambda: engine
    context = capabilities.ToolContext(
        search_tool_factory=lambda: (lambda *a, **k: []),
        workspace_root=root,
        **context_kwargs,
    )
    bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
    tools = dict(zip(bound.names, bound.tools))
    return tools["web_search"], tools["web_read"]


# ---------------------------------------------------------------------------
# web_read: table shaping / data_path / calendar-widget filtering
# ---------------------------------------------------------------------------


def test_web_read_extracts_table_and_saves_data_path() -> None:
    page = AcquiredPage(
        url="https://data.example/report",
        status=200,
        title="Report",
        text="report",
        tables=[[["Year", "Value"], ["2025", "10"], ["2026", "11"]]],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=page.url)

        check("web_read returns ok on a real table", result.get("ok") is True, result)
        check("columns are the table header", result.get("columns") == ["Year", "Value"], result)
        check("row_count matches the data rows", result.get("row_count") == 2, result)
        check("a data_path was saved", bool(result.get("data_path")), result)
        if result.get("data_path"):
            # WebFetchTools resolves workspace_root (Path.resolve()), which on
            # Windows can normalize the temp dir differently than the raw path
            # this test received -- resolve the same way before joining.
            saved = json.loads((Path(root).resolve() / result["data_path"]).read_text())
            check("the saved file holds the complete rows", len(saved) == 2, saved)


def test_web_read_retries_javascript_required_page_with_longer_delay() -> None:
    """Real bug: a heavier client-rendered page (MSN's article pages
    routinely need more than the default 1.5s render wait to finish
    hydrating past DOMContentLoaded) came back javascript_required=True
    on the first pass and was discarded outright -- research then fell
    back to a worse, staler source that DID render in time, silently
    producing a "last match" answer that was actually two weeks old."""
    js_shell_page = AcquiredPage(
        url="https://news.example/article",
        status=200,
        title="Article",
        text="",
        javascript_required=True,
    )
    rendered_page = AcquiredPage(
        url="https://news.example/article",
        status=200,
        title="Article",
        text="The real article body, long enough to count as usable content.",
        javascript_required=False,
    )
    engine = _FakeEngine(
        {js_shell_page.url: js_shell_page},
        retry_pages_by_url={rendered_page.url: rendered_page},
    )
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=js_shell_page.url)

        check(
            "acquire was called once at the default delay, once longer",
            engine.acquire_delays == [1.5, 4.5],
            engine.acquire_delays,
        )
        check("web_read recovered via the retry", result.get("ok") is True, result)
        check(
            "recovered text is the rendered version, not the empty shell",
            "real article body" in (result.get("text") or ""),
            result,
        )


def test_web_read_javascript_required_page_stays_failed_if_retry_also_empty() -> None:
    """The retry is a second chance, not a guarantee -- a page that is
    genuinely unrenderable (or paywalled/bot-blocked) must still report
    the same clean no_usable_content failure, not raise or hang."""
    js_shell_page = AcquiredPage(
        url="https://news.example/still-blocked",
        status=200,
        title="Article",
        text="",
        javascript_required=True,
    )
    engine = _FakeEngine({js_shell_page.url: js_shell_page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=js_shell_page.url)

        check(
            "still retried once",
            engine.acquire_delays == [1.5, 4.5],
            engine.acquire_delays,
        )
        check("reports the usual clean failure", result.get("ok") is False, result)
        check(
            "error_reason is unchanged",
            result.get("error_reason") == "no_usable_content",
            result,
        )


def test_web_read_large_table_stays_bounded_and_drives_xlsx() -> None:
    """The amplification fix: a large table must not come back inline and
    get resent on every remaining turn. Before this existed, extract_tables
    returning ~2.5M characters in one result was a measured real incident.
    Ported from the pre-Crawl4AI WebFetchTools.extract_tables test to the
    live web_read path when that legacy fetch tier was removed."""
    header = ["a", "b", "c"]
    rows = [[f"row{i}", str(i), "x" * 40] for i in range(800)]
    page = AcquiredPage(
        url="https://big.example/t",
        status=200,
        title="Big table",
        text="big table",
        tables=[[header] + rows],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=page.url)

        inline = json.dumps(result, ensure_ascii=False)
        check("web_read: the inline result is bounded", len(inline) < 8_000, f"{len(inline)} chars")
        check(
            "web_read: the preview is capped",
            len(result.get("preview") or []) <= MAX_INLINE_TABLE_ROWS,
            str(len(result.get("preview") or [])),
        )
        check("web_read: the TRUE row count is still reported", result.get("row_count") == 800, result)
        check("web_read: the full data was saved", bool(result.get("data_path")), result)

        root_path = Path(root).resolve()
        saved = root_path / result["data_path"]
        check("web_read: the saved file exists", saved.is_file(), str(saved))
        records = json.loads(saved.read_text(encoding="utf-8"))
        check("web_read: it holds every row", len(records) == 800, str(len(records)))
        check(
            "web_read: saved as records, ready for from_path",
            records[0] == {"a": "row0", "b": "0", "c": "x" * 40},
            str(records[0])[:100],
        )

        # And the end of the chain works: file -> spreadsheet, no rows inline.
        artifacts_tools = ArtifactTools(root_path)
        xlsx = artifacts_tools.create_xlsx_artifact("out.xlsx", from_path=result["data_path"])
        check("web_read: the saved file drives xlsx creation", xlsx["row_count"] == 800, str(xlsx))


def test_web_read_calendar_widget_is_filtered_at_extraction() -> None:
    """Noise filtering, not a relevance judgment: a date-picker calendar
    widget (header row of only weekday names) is not page data and must
    never be extracted as a table at all -- this lives in html_extract.py,
    upstream of anything web_tools.py decides."""
    html = (
        "<html><head><title>Kurlar</title></head><body>"
        "<table><tr><th>Pts</th><th>Sal</th><th>Çar</th><th>Per</th>"
        "<th>Cum</th><th>Cts</th><th>Paz</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td><td>7</td></tr>"
        "</table>"
        "<table><tr><th>Döviz</th><th>Alış</th><th>Satış</th></tr>"
        "<tr><td>USD</td><td>34.1</td><td>34.2</td></tr></table>"
        "</body></html>"
    )
    page = extract_page(url="https://data.example/kurlar", status=200, html=html, content_type="text/html")
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=page.url)

    check(
        "the calendar widget was filtered, only the real table remains",
        result.get("columns") == ["Döviz", "Alış", "Satış"],
        result,
    )


def test_web_read_exposes_every_table_without_choosing_one() -> None:
    """A page with several tables (e.g. a season-tab widget) is not
    web_read's decision to disambiguate: EVERY table is saved individually,
    with its own caption, and the agent picks."""
    html = (
        "<html><head><title>Süper Lig Puan Durumu</title></head><body>"
        "<h2>2022-2023 Sezonu</h2>"
        "<table><tr><th>Takım</th><th>O</th><th>P</th></tr>"
        "<tr><td>Eski Takım</td><td>18</td><td>30</td></tr></table>"
        "<h2>2025-2026 Sezonu</h2>"
        "<table><tr><th>Takım</th><th>O</th><th>P</th></tr>"
        "<tr><td>Güncel Takım</td><td>18</td><td>45</td></tr></table>"
        "</body></html>"
    )
    page = extract_page(
        url="https://data.example/default.aspx?pageID=198",
        status=200,
        html=html,
        content_type="text/html",
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=page.url)

        tables_saved = result.get("tables_saved") or []
        check("both tables were saved individually", len(tables_saved) == 2, result)
        captions = {entry.get("caption") for entry in tables_saved}
        check(
            "each table's own caption is exposed so the agent can tell them apart",
            captions == {"2022-2023 Sezonu", "2025-2026 Sezonu"},
            tables_saved,
        )
        for entry in tables_saved:
            saved = json.loads((Path(root).resolve() / entry["path"]).read_text())
            check(
                f"table {entry.get('table_index')}'s own file holds only its own rows",
                len(saved) == 1,
                saved,
            )


def test_web_read_extracts_declared_records() -> None:
    page = AcquiredPage(
        url="https://shop.example/products",
        status=200,
        title="Products",
        text="products",
        records=[
            {"name": "Item A", "price": "10", "source_url": "https://shop.example/products"},
            {"name": "Item B", "source_url": "https://shop.example/products"},
        ],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=page.url)

    check("records deliver record_count", result.get("record_count") == 2, result)
    check("a data_path was saved for records too", bool(result.get("data_path")), result)


def test_web_read_reports_failure_without_content() -> None:
    page = AcquiredPage(url="https://empty.example/", status=200, title="", text="")
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=page.url)

    check("a contentless page reports ok=False", result.get("ok") is False, result)
    check(
        "the failure reason is no_usable_content",
        result.get("error_reason") == "no_usable_content",
        result,
    )


def test_web_read_404_is_reported_and_memoized() -> None:
    engine = _FakeEngine({})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        first = web_read(url="https://missing.example/page")
        second = web_read(url="https://missing.example/page")

    check("a 404 is reported as a terminal failure", first.get("ok") is False, first)
    check(
        "the repeated identical call is served from the dead-URL cache, not refetched",
        engine.fetches == ["https://missing.example/page"],
        engine.fetches,
    )
    check("the cached repeat carries the same error_reason", second.get("error_reason") == first.get("error_reason"))


def test_web_read_has_no_source_or_location_check() -> None:
    """Explicit architecture assertion: web_read fetches any URL it's given.
    There is no domain allow-list, no place/date lexicon, nothing that could
    refuse a URL based on what it names -- that judgment is the agent's."""
    page = AcquiredPage(
        url="https://anything.example/whatever-topic-or-place",
        status=200,
        title="x",
        text="hello world content here",
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(
            engine,
            root,
        )
        result = web_read(url=page.url)

    check("the URL was fetched with no gate in front of it", engine.fetches == [page.url], engine.fetches)
    check("web_read has no notion of an unauthorized/mismatched URL", result.get("ok") is True, result)


# ---------------------------------------------------------------------------
# web_search: clean, bounded passthrough -- no re-ranking
# ---------------------------------------------------------------------------


def test_web_search_preserves_searxng_order_with_no_reranking() -> None:
    """Architecture assertion: web_search must NOT re-score or reorder
    SearXNG's results by keyword overlap, site name, archive-mirror
    penalties, or anything else content-based -- the agent judges relevance,
    not this tool. A result whose SearXNG rank is first but shares no
    keyword with the query must still come back first."""
    first = {
        "url": "https://a.example/",
        "title": "Completely unrelated title",
        "content": "nothing to do with the query at all",
        "score": 0.1,
        "engines": ["test"],
    }
    second = {
        "url": "https://b.example/",
        "title": "Exact query match query match query match",
        "content": "query match query match",
        "score": 99.0,
        "engines": ["test"],
    }

    def fake_search(query, **kw):
        return [first, second]

    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: fake_search, workspace_root=root
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        web_search = dict(zip(bound.names, bound.tools))["web_search"]
        result = web_search(query="query match")

    urls = [item["url"] for item in result.get("results", [])]
    check(
        "SearXNG's own order is preserved even though the second result "
        "scores far higher on keyword overlap",
        urls == [first["url"], second["url"]],
        urls,
    )


def test_web_search_bounds_result_count() -> None:
    candidates = [
        {"url": f"https://x.example/{i}", "title": "t", "content": "c", "score": 1.0, "engines": []}
        for i in range(50)
    ]

    def fake_search(query, **kw):
        return candidates

    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: fake_search, workspace_root=root
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        web_search = dict(zip(bound.names, bound.tools))["web_search"]
        result = web_search(query="q", max_results=3)

    check("max_results is honoured", result.get("result_count") == 3, result)
    check(
        "a successful search tells the agent to read next, not search again",
        "web_read" in (result.get("instruction") or "")
        and "before searching again" in (result.get("instruction") or ""),
        result.get("instruction"),
    )
    # A fresh binding, because back-to-back searches with nothing read in
    # between are refused by design now (see
    # test_searching_twice_without_reading_anything_is_refused) and this
    # check is about max_results, not that rule.
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: fake_search, workspace_root=root
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        uncapped_search = dict(zip(bound.names, bound.tools))["web_search"]
        result_uncapped = uncapped_search(query="q2", max_results=999)
    check(
        "max_results is capped at MAX_SEARCH_RESULTS_PER_QUERY regardless of what's requested",
        result_uncapped.get("result_count") == web_tools.MAX_SEARCH_RESULTS_PER_QUERY,
        result_uncapped,
    )


# ---------------------------------------------------------------------------
# URL provenance: read what search offered, not what the model assembled
# ---------------------------------------------------------------------------


_OFFERED = "https://news.example/match-report"
_SEARCH_HIT = [
    {
        "url": _OFFERED,
        "title": "Match report",
        "content": "lineups",
        "score": 1.0,
        "engines": [],
    }
]


def _searching_tools(engine, root, *, request_text=""):
    """web_search/web_read sharing one job state, as a real job binds them."""
    web_tools.get_crawl4ai_engine = lambda: engine
    context = capabilities.ToolContext(
        search_tool_factory=lambda: (lambda query, **kw: _SEARCH_HIT),
        workspace_root=root,
        grounding_text=request_text,
    )
    bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
    tools = dict(zip(bound.names, bound.tools))
    return tools["web_search"], tools["web_read"]


def test_a_url_the_model_invented_is_refused_with_the_real_candidates() -> None:
    """The confirmed live failure: the agent searched, got real match-report
    URLs, then called web_read on addresses it had built from a pattern.
    None existed, each one cost a turn, and the run ended "could not find
    it" with the answering page unread in its own search results."""
    invented = "https://www.mackolik.com/mac/galatasaray-vs-corum-fk/2026-08-14"
    page = AcquiredPage(url=invented, status=200, title="x", text="never fetched")
    engine = _FakeEngine({invented: page})
    with tempfile.TemporaryDirectory() as root:
        web_search, web_read = _searching_tools(engine, root)
        web_search(query="Galatasaray Corum FK ilk 11")
        result = web_read(url=invented)

    check(
        "an address no search offered is not fetched",
        result.get("ok") is False
        and result.get("error_reason") == "url_not_from_search_results",
        result,
    )
    check(
        "the refusal hands back the URLs that DO exist, so the next call can succeed",
        [item["url"] for item in (result.get("candidates") or [])] == [_OFFERED],
        result.get("candidates"),
    )
    check(
        "the invented URL never reached the network",
        engine.fetches == [],
        engine.fetches,
    )


def test_searching_twice_without_reading_anything_is_refused() -> None:
    """The model's default is to reword the query it never tested. Measured
    live: four near-identical opening queries returned the same hub pages
    and spent the whole search budget, so the one query that would have
    worked -- writable only after a read revealed the opponent and date --
    came back as the exhausted stub."""
    engine = _FakeEngine({})
    with tempfile.TemporaryDirectory() as root:
        web_search, _ = _searching_tools(engine, root)
        first = web_search(query="Galatasaray son maç ilk 11")
        second = web_search(query="Galatasaray son maçı ilk 11 kadro")

    check("the first search runs normally", first.get("ok") is True, first)
    check(
        "a reworded query is refused while the last results sit unopened",
        second.get("ok") is False
        and second.get("error_reason") == "previous_results_unread",
        second,
    )
    check(
        "the refusal points at the pages that must be opened first",
        [item["url"] for item in (second.get("candidates") or [])] == [_OFFERED],
        second.get("candidates"),
    )


def test_a_refused_search_does_not_cost_budget_and_reading_unblocks_it() -> None:
    page = AcquiredPage(url=_OFFERED, status=200, title="Hub", text="Çorum FK, 14 Ağustos")
    engine = _FakeEngine({_OFFERED: page})
    with tempfile.TemporaryDirectory() as root:
        web_search, web_read = _searching_tools(engine, root)
        web_search(query="vague opening query")
        refused = web_search(query="another vague query")
        web_read(url=_OFFERED)
        refined = web_search(query="Galatasaray Çorum FK 14 Ağustos ilk 11")

    check("the refined search runs once something has been read", refined.get("ok") is True, refined)
    check(
        "the refused call was not charged to the search budget",
        # one opening search + one refined search; the refusal in between
        # must not have consumed a unit.
        refused.get("ok") is False and engine.fetches == [_OFFERED],
        engine.fetches,
    )


def test_guessing_a_url_does_not_unblock_the_next_search() -> None:
    """Otherwise the read requirement is bypassable by inventing an address."""
    engine = _FakeEngine({})
    with tempfile.TemporaryDirectory() as root:
        web_search, web_read = _searching_tools(engine, root)
        web_search(query="vague opening query")
        web_read(url="https://www.example.com/guessed/path")
        second = web_search(query="another vague query")

    check(
        "a refused invented URL still counts as nothing read",
        second.get("error_reason") == "previous_results_unread",
        second,
    )


def test_a_url_from_the_search_results_is_fetched_normally() -> None:
    page = AcquiredPage(
        url=_OFFERED, status=200, title="Report", text="the lineup is here"
    )
    engine = _FakeEngine({_OFFERED: page})
    with tempfile.TemporaryDirectory() as root:
        web_search, web_read = _searching_tools(engine, root)
        web_search(query="Galatasaray Corum FK ilk 11")
        result = web_read(url=_OFFERED)

    check("a search-result URL is read as before", result.get("ok") is True, result)


def test_a_leftover_url_from_an_earlier_broader_search_is_refused() -> None:
    """The last live failure: the agent finally ran the narrow query whose
    top results were the pages holding the answer, then opened a club hub
    page left over from its first vague query instead of any of them."""
    fixtures = "https://hub.example/fixtures"
    stale = "https://hub.example/club"
    fresh = "https://news.example/lineup-report"
    engine = _FakeEngine(
        {
            fixtures: AcquiredPage(
                url=fixtures, status=200, title="Fixtures", text="opponent and date"
            ),
            stale: AcquiredPage(url=stale, status=200, title="Club", text="hub"),
            fresh: AcquiredPage(url=fresh, status=200, title="Lineup", text="the XI"),
        }
    )
    hit = lambda url, title: {  # noqa: E731 - table-style fixture data
        "url": url,
        "title": title,
        "content": "",
        "score": 1.0,
        "engines": [],
    }
    rounds = [
        [hit(fixtures, "Fixtures list"), hit(stale, "Club hub")],
        [hit(fresh, "Lineup report")],
    ]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: rounds.pop(0)),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="vague opening query")
        tools["web_read"](url=fixtures)
        tools["web_search"](query="narrow query naming the specifics")
        refused = tools["web_read"](url=stale)
        accepted = tools["web_read"](url=fresh)

    check(
        "a URL from the superseded round is not fetched",
        refused.get("error_reason") == "url_from_a_superseded_search",
        refused,
    )
    check(
        "the refusal shows the fresh results instead",
        [item["url"] for item in (refused.get("candidates") or [])] == [fresh],
        refused.get("candidates"),
    )
    check(
        "a result of the latest search is read normally",
        accepted.get("ok") is True,
        accepted,
    )


def test_a_tables_only_page_does_not_demand_a_final_answer() -> None:
    """The reported failure: after reading four real transfer articles, the
    run's LAST read was a club landing page whose only output was a six-row
    weekday calendar. That result carried "write the FINAL ANSWER now", so
    the answer was written from it -- "a table was retrieved but its
    contents are unknown" -- discarding everything read before it."""
    listing = "https://club.example/haberler"
    article = "https://news.example/transfer-story"
    page = AcquiredPage(
        url=listing,
        status=200,
        title="Landing",
        text="",
        tables=[[["Pzt", "Sa", "Ça"], ["1", "2", "3"], ["4", "5", "6"]]],
    )
    engine = _FakeEngine({listing: page})
    results = [
        {"url": listing, "title": "Club landing", "content": "", "score": 1.0, "engines": []},
        {"url": article, "title": "Transfer story", "content": "", "score": 0.9, "engines": []},
    ]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: results),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="Galatasaray transfer haberleri")
        result = tools["web_read"](url=listing)

    instruction = result.get("instruction") or ""
    check(
        "a text-free page is not told to produce the final answer",
        "FINAL ANSWER" not in instruction,
        instruction,
    )
    check(
        "it is named as a listing surface instead",
        "no article text" in instruction.lower(),
        instruction,
    )
    check(
        "and a site that has not been tried is offered",
        [item["url"] for item in (result.get("untried_sources") or [])] == [article],
        result.get("untried_sources"),
    )


def test_a_page_with_prose_still_gets_the_answer_now_instruction() -> None:
    """Tables that come with real page text are ordinary evidence; the
    change must not make every table read hesitant."""
    url = "https://data.example/standings"
    page = AcquiredPage(
        url=url,
        status=200,
        title="Standings",
        text=(
            "The current league standings after 30 rounds are as follows, "
            "with points and goal difference shown for every club."
        ),
        tables=[[["team", "points"], ["A", "70"], ["B", "68"]]],
    )
    engine = _FakeEngine({url: page})
    results = [{"url": url, "title": "Standings", "content": "", "score": 1.0, "engines": []}]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: results),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="league standings")
        result = tools["web_read"](url=url)

    check(
        "a table page with prose still asks for the answer",
        "FINAL ANSWER" in (result.get("instruction") or ""),
        result.get("instruction"),
    )


def test_untried_sources_never_repeat_a_host_already_opened() -> None:
    """Breadth means a different site, not another page of the same one."""
    first = "https://one.example/a"
    same_host = "https://one.example/b"
    other = "https://two.example/c"
    empty = AcquiredPage(url=first, status=200, title="A", text="", tables=[[["h"], ["v"]]])
    engine = _FakeEngine({first: empty})
    results = [
        {"url": first, "title": "A", "content": "", "score": 1.0, "engines": []},
        {"url": same_host, "title": "B", "content": "", "score": 0.9, "engines": []},
        {"url": other, "title": "C", "content": "", "score": 0.8, "engines": []},
    ]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: results),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="q")
        result = tools["web_read"](url=first)

    check(
        "the host just read is excluded from the alternatives",
        [item["url"] for item in (result.get("untried_sources") or [])] == [other],
        result.get("untried_sources"),
    )


def test_a_site_homepage_is_refused_while_a_real_page_is_available() -> None:
    """Both reported failures were built on a homepage. The agent opened
    galatasaray.org, the extractor pulled the weekly-programme widget off
    it, and because those cells are populated the mostly_empty check
    passed them as data -- so the tool told the model to answer from a
    table of weekday names, which is exactly what it then reported."""
    root_url = "https://club.example/"
    article = "https://news.example/match-report"
    engine = _FakeEngine(
        {
            root_url: AcquiredPage(url=root_url, status=200, title="Home", text="hub"),
            article: AcquiredPage(url=article, status=200, title="Report", text="the XI"),
        }
    )
    results = [
        {"url": root_url, "title": "Club home", "content": "", "score": 1.0, "engines": []},
        {"url": article, "title": "Match report", "content": "", "score": 0.9, "engines": []},
    ]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: results),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="Galatasaray son maç ilk 11")
        refused = tools["web_read"](url=root_url)
        accepted = tools["web_read"](url=article)

    check(
        "a bare home page is not fetched",
        refused.get("error_reason") == "url_is_a_site_homepage",
        refused,
    )
    check(
        "the refusal offers only the candidates that are about something",
        [item["url"] for item in (refused.get("candidates") or [])] == [article],
        refused.get("candidates"),
    )
    check("a real page is still read", accepted.get("ok") is True, accepted)
    check("the homepage never reached the network", engine.fetches == [article], engine.fetches)


def test_language_homepage_alias_is_refused_like_a_site_root() -> None:
    """galatasaray.org/en/Homepage is a landing page, not an article.

    A path-only slash check let it through; the extractor then returned a
    weekday calendar plus 'OTHER NEWS' boilerplate, and the model treated
    the search snippet as a confirmed signing.
    """
    landing = "https://club.example/en/Homepage"
    article = "https://news.example/galatasaray-transfer"
    engine = _FakeEngine(
        {
            landing: AcquiredPage(
                url=landing, status=200, title="Home", text="OTHER NEWS"
            ),
            article: AcquiredPage(
                url=article, status=200, title="News", text="Batrakov talks"
            ),
        }
    )
    results = [
        {"url": landing, "title": "Club EN home", "content": "Noa Lang joins!", "score": 1.0, "engines": []},
        {"url": article, "title": "Transfer news", "content": "latest", "score": 0.5, "engines": []},
    ]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: results),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        search = tools["web_search"](query="latest Galatasaray transfer news")
        refused = tools["web_read"](url=landing)
        accepted = tools["web_read"](url=article)

    urls = [item["url"] for item in (search.get("results") or [])]
    check(
        "specific pages are listed before homepage aliases",
        urls == [article, landing],
        urls,
    )
    check(
        "/en/Homepage is refused as a site homepage",
        refused.get("error_reason") == "url_is_a_site_homepage",
        refused,
    )
    check("the news article is still read", accepted.get("ok") is True, accepted)
    check("the landing page never reached the network", engine.fetches == [article], engine.fetches)


def test_refusing_a_homepage_unblocks_a_refined_search() -> None:
    """Live failure: agent opened galatasaray.org (refused as homepage),
    then site:mackolik.com was blocked as previous_results_unread, so the
    run answered 'not found' without ever reading Mackolik."""
    landing = "https://club.example/"
    article = "https://mackolik.example/galatasaray-maclar"
    engine = _FakeEngine(
        {
            article: AcquiredPage(
                url=article, status=200, title="Fixtures", text="lineup page"
            ),
        }
    )
    hits = [
        {"url": landing, "title": "Club home", "content": "", "score": 1.0, "engines": []},
        {"url": article, "title": "Mackolik", "content": "matches", "score": 0.5, "engines": []},
    ]
    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: hits),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="Galatasaray son maç ilk 11")
        refused = tools["web_read"](url=landing)
        refined = tools["web_search"](query="site:mackolik.com Galatasaray")

    check(
        "homepage is still not fetched",
        refused.get("error_reason") == "url_is_a_site_homepage",
        refused,
    )
    check(
        "a refined search can run after the homepage was refused",
        refined.get("ok") is True,
        refined,
    )


def test_a_homepage_is_allowed_when_it_is_all_the_search_found() -> None:
    """The rule must not strand a run whose results are genuinely all
    roots -- better a hub page than nothing at all."""
    root_url = "https://club.example/"
    engine = _FakeEngine(
        {root_url: AcquiredPage(url=root_url, status=200, title="Home", text="hub")}
    )
    results = [
        {"url": root_url, "title": "Club home", "content": "", "score": 1.0, "engines": []}
    ]

    web_tools.get_crawl4ai_engine = lambda: engine
    with tempfile.TemporaryDirectory() as root:
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda query, **kw: results),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        tools = dict(zip(bound.names, bound.tools))
        tools["web_search"](query="something obscure")
        result = tools["web_read"](url=root_url)

    check("the only candidate is read even though it is a root", result.get("ok") is True, result)


def test_a_homepage_the_user_asked_for_is_still_read() -> None:
    root_url = "https://client.example/"
    engine = _FakeEngine(
        {root_url: AcquiredPage(url=root_url, status=200, title="Home", text="landing copy")}
    )
    with tempfile.TemporaryDirectory() as root:
        web_search, web_read = _searching_tools(
            engine, root, request_text=f"Summarise the landing page at {root_url}"
        )
        web_search(query="unrelated")
        result = web_read(url=root_url)

    check("a homepage the user named is fetched", result.get("ok") is True, result)


def test_a_url_the_user_typed_is_never_treated_as_invented() -> None:
    """Search results are not the only legitimate source: a literal URL in
    the user's own request must stay readable even after a search ran."""
    typed = "https://data.example/my-page"
    page = AcquiredPage(url=typed, status=200, title="Mine", text="user supplied page")
    engine = _FakeEngine({typed: page})
    with tempfile.TemporaryDirectory() as root:
        web_search, web_read = _searching_tools(
            engine, root, request_text=f"Please summarise {typed} for me"
        )
        web_search(query="unrelated search")
        result = web_read(url=typed)

    check("a user-supplied URL is still fetched", result.get("ok") is True, result)


def test_provenance_is_inert_before_any_search_runs() -> None:
    """A fetch-only job (literal URL, no search) has nothing to check
    against, so the gate must not exist for it at all."""
    url = "https://data.example/direct"
    page = AcquiredPage(url=url, status=200, title="Direct", text="fetched directly")
    engine = _FakeEngine({url: page})
    with tempfile.TemporaryDirectory() as root:
        _, web_read = _bound_tools(engine, root)
        result = web_read(url=url)

    check("with no search performed, any URL is fetched", result.get("ok") is True, result)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_web_read_budget_is_enforced_and_shared_across_bindings() -> None:
    pages = {
        f"https://data.example/{i}": AcquiredPage(
            url=f"https://data.example/{i}", status=200, title="x", text="hello world content"
        )
        for i in range(web_tools.MAX_WEB_READS_PER_JOB + 2)
    }
    engine = _FakeEngine(pages)
    with tempfile.TemporaryDirectory() as root:
        fetch_instance = WebFetchTools(workspace_root=root)
        with_engine_patch = web_tools.get_crawl4ai_engine
        web_tools.get_crawl4ai_engine = lambda: engine
        try:
            shared: dict = {}
            results = []
            for i in range(web_tools.MAX_WEB_READS_PER_JOB + 2):
                context = capabilities.ToolContext(
                    search_tool_factory=lambda: (lambda *a, **k: []),
                    workspace_root=root,
                )
                context._shared = shared
                context._shared.setdefault("web_fetch", fetch_instance)
                bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
                web_read = dict(zip(bound.names, bound.tools))["web_read"]
                results.append(web_read(url=f"https://data.example/{i}"))
        finally:
            web_tools.get_crawl4ai_engine = with_engine_patch

    exhausted = [r for r in results if r.get("error_reason") == "read_budget_exhausted"]
    check(
        "the job-wide read budget eventually blocks further fetches",
        len(exhausted) == 2,
        len(exhausted),
    )


def _main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all web_tools regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
