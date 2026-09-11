"""Two explicit model-facing web tools: ``web_search`` and ``web_read``.

    Agent
      |-- web_search(query, max_results) -> SearXNG, raw ranked candidates
      `-- web_read(url)                  -> Crawl4AI, one page's content

Design philosophy: semantic reasoning belongs to the agent; this module
provides infrastructure and safety only.

    User request -> agent -> web_search(query) -> SearXNG -> search results
    -> agent evaluates relevance/authority/date/entity/location
    -> web_read(url) -> Crawl4AI -> rendered page content
    -> agent verifies the content answers the request
    -> answer / search again / read another source

Concretely, that means:
  - ``web_search`` does NOT re-rank, filter, or score SearXNG's results by
    keyword overlap, "site name isn't relevance", archive-mirror penalties,
    or anything else content-based. It returns what SearXNG returned,
    bounded and shaped, in SearXNG's own order. Deciding which result is
    relevant, authoritative, current, or about the right place/entity is the
    agent's job -- there is no lexicon of place names, institutions, dates,
    or topics in this file, and there must never be one: this module has to
    work unmodified for a request about TCMB, the ECB, NASA, a football
    league, a restaurant, or any organization/topic it has never seen.
  - ``web_read`` does NOT decide which of a page's several tables is "the
    right one" (that used to be a keyword/season-recency/semantic scoring
    step). A page with more than one table gets ALL of them saved
    individually; the default it points ``data_path`` at is a purely
    structural tie-break (the largest one, or a same-header combine -- see
    ``web_fetch.WebFetchTools._save_tables``), and every table's own path
    and caption are listed in ``tables_saved`` so the agent can pick a
    different one without reading any of them first.
  - Source/domain/location authorization is not enforced here either: there
    is no hardcoded organization-to-domain map. If a request needs to stay
    on one domain, that is an instruction the agent follows (e.g. issuing
    ``site:tcmb.gov.tr ...`` searches), not a rule this module checks.
    What IS enforced is provenance, which is a different question: once a
    job has searched, ``web_read`` only accepts a URL that a search
    actually offered or that the user typed (see
    ``WebTools._reject_if_invented``). That checks where an address came
    from, never whether its source is the right one, so it stays free of
    domain knowledge -- an address the model assembled from a pattern
    usually does not exist, and fetching it only spends a turn.

What deterministic code IS still responsible for (infrastructure/safety, not
semantics): SSRF/URL validation (delegated to crawl4ai_engine.py, which is
the only place that talks to the network), per-job budgets, memoisation/
caching, result-count and text-size bounds, provenance shaping, and getting
the extracted table/record data onto disk (``data_path``) so it reaches
artifact tools without passing through model context.
"""

from __future__ import annotations

import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from crawl4ai_engine import AcquisitionError, Crawl4AIAcquisitionEngine, get_crawl4ai_engine
from html_extract import AcquiredPage
from web_fetch import WebFetchTools
from web_fetch import _normalize_url as _fetch_normalize_url

# A closed lexicon of universal blank/placeholder tokens -- not language- or
# domain-specific, the same convention ("-", "N/A", empty) any table in any
# field uses to mark a missing value. Used only to measure whether a table
# actually holds data, never to judge what the data means.
_PLACEHOLDER_CELLS = frozenset({"", "-", "--", "—", "–", "n/a", "na", "null", "none", "tbd"})
# Below this fraction of populated data cells, a table reads as an empty
# shell (headers with no real rows behind them -- e.g. a page whose
# JS-rendered data hadn't finished loading when the engine captured it)
# rather than actual data. Purely structural: counts blanks, infers nothing
# about content.
MIN_TABLE_DENSITY = 0.3

MAX_SEARCH_RESULTS_PER_QUERY = 15
MAX_TEXT_CHARS = 12_000
MAX_RECORDS_PER_READ = 200
MAX_INLINE_RECORDS = 25

# Job-wide budgets. Flat, physical counters: each call is exactly one search
# or one fetch, with no separate "logical session" layer above it.
MAX_WEB_SEARCHES_PER_JOB = 20
MAX_WEB_READS_PER_JOB = 30


def _cell_is_populated(value: Any) -> bool:
    return str(value or "").strip().casefold() not in _PLACEHOLDER_CELLS


def _table_density(rows: list[list[Any]]) -> float:
    """Fraction of cells across ``rows`` holding a real value.

    Structural only: it counts blanks/placeholders, it does not read them.
    Callers pass just the data rows (no header) so a table's own labels
    never count toward or against its density.
    """
    cells = [cell for row in rows for cell in row]
    if not cells:
        return 0.0
    return sum(1 for cell in cells if _cell_is_populated(cell)) / len(cells)


def _normalize(url: str) -> str:
    try:
        return _fetch_normalize_url(url)
    except ValueError:
        return (url or "").strip()


# Literal addresses in the user's own request. Trailing sentence punctuation
# is excluded so "see https://x.example/p." does not become part of the URL.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)


# Language prefixes and landing-page aliases that are still a homepage.
# ``/en/Homepage`` was the live failure: it is a club landing page, not an
# article, but a path-only "/" check let it through, the extractor pulled
# a weekday calendar, and the model answered from that snippet.
_HOME_PATH_SEGMENTS = frozenset(
    {
        "en",
        "tr",
        "de",
        "fr",
        "es",
        "it",
        "home",
        "homepage",
        "index",
        "anasayfa",
        "start",
        "main",
        "default",
    }
)


def _is_site_root(url: str) -> bool:
    """Whether a URL addresses a site's home/landing page and nothing narrower.

    Empty path, "/", and language/homepage aliases (``/en``, ``/en/Homepage``,
    ``/anasayfa``) with no query. A path that names a topic, article, or
    document is not a root.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.query:
        return False
    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if not segments:
        return True
    return len(segments) <= 2 and all(
        segment.casefold() in _HOME_PATH_SEGMENTS for segment in segments
    )


def _path_depth(url: str) -> int:
    try:
        path = urlsplit(url).path or ""
    except ValueError:
        return 0
    return len([segment for segment in path.split("/") if segment])


def _prefer_detail_candidates(
    packed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Structural: news + deeper paths before shallow hubs. No favourites list."""
    news: list[dict[str, Any]] = []
    deep: list[dict[str, Any]] = []
    shallow: list[dict[str, Any]] = []
    for item in packed:
        url = str(item.get("url") or "")
        cat = str(item.get("category") or "").casefold()
        if "news" in cat:
            news.append(item)
        elif _path_depth(url) >= 2 and not _is_site_root(_normalize(url)):
            deep.append(item)
        else:
            shallow.append(item)
    return news + deep + shallow


class _SearchBudgetExhausted(Exception):
    """The injected search tool refused to run: its own budget is spent."""


class _PageTables:
    """One page's tables, deduplicated only -- no scoring, no selection.

    Dedup guards against the same table being extracted twice (e.g. a page
    reachable at both a canonical and a session-specific URL); it is a
    content-identity check, not a judgment about which table matters.
    """

    def __init__(self, page: AcquiredPage) -> None:
        self.tables: list[list[list[str]]] = []
        self.captions: list[str] = []
        self.rows_dropped = page.rows_dropped
        seen: set[tuple[tuple[str, ...], ...]] = set()
        captions = page.table_captions
        for index, table in enumerate(page.tables):
            marker = tuple(
                tuple(" ".join(str(cell or "").split()) for cell in row)
                for row in table
            )
            if marker in seen:
                continue
            seen.add(marker)
            self.tables.append(table)
            self.captions.append(captions[index] if index < len(captions) else "")


@dataclass
class _JobWebState:
    """Budgets/caches shared by every ``web_search``/``web_read`` binding in
    one job, hung off the shared ``WebFetchTools`` instance so fixed and
    dynamic workers share one job-wide budget rather than each getting their
    own.
    """

    search_calls: int = 0
    read_calls: int = 0
    dead_urls: dict[str, AcquisitionError] = field(default_factory=dict)
    read_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_attempts: dict[str, int] = field(default_factory=dict)
    search_cache: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    # Every URL SearXNG actually offered this job, normalized. This is a
    # provenance ledger, not an allowlist of approved sources: it records
    # where an address came from, and knows nothing about which domain or
    # organization is "right" for any request.
    offered_urls: set[str] = field(default_factory=set)
    # The most recent candidates, in SearXNG's order, so a rejected read can
    # hand the real addresses straight back instead of only saying "no".
    last_candidates: list[dict[str, Any]] = field(default_factory=list)
    # Whether anything has been opened since the last search returned
    # results. See WebTools._refuse_search_before_reading.
    read_since_last_search: bool = True
    # Hosts this job has already opened, so a page that says nothing can
    # point at a site that has not been tried. See _untried_sources.
    read_hosts: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)


def _job_state(fetch_tools: WebFetchTools) -> _JobWebState:
    if not hasattr(fetch_tools, "_web_tools_state"):
        fetch_tools._web_tools_state = _JobWebState()
    return fetch_tools._web_tools_state


class WebTools:
    """The two model-facing web tools: ``web_search`` and ``web_read``."""

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        search_fn: Optional[Callable[..., Any]] = None,
        progress: Optional[Callable[[str, str], None]] = None,
        fetch_tools: Optional[WebFetchTools] = None,
        engine: Optional[Crawl4AIAcquisitionEngine] = None,
        request_text: str = "",
    ) -> None:
        self._web = fetch_tools or WebFetchTools(workspace_root=workspace_root)
        self._search_fn = search_fn
        self._progress = progress
        self._engine = engine
        self._state = _job_state(self._web)
        # A URL the user typed themselves is a legitimate source that no
        # search will have offered -- see _reject_if_invented.
        self._allowed_urls = {
            _normalize(found) for found in _URL_IN_TEXT_RE.findall(request_text or "")
        }
        self.web_search = self._wrap_search(self._search)
        self.web_read = self._wrap_read(self._read)

    def as_tools(self) -> list[Callable[..., dict[str, Any]]]:
        return [self.web_search, self.web_read]

    def search_raw(self, query: str, max_results: int = 10) -> dict[str, Any]:
        """SearXNG search for the phase controller -- no ReAct stop heuristics.

        Same network, cache, provenance ledger, and job budget as
        ``web_search``. Does not refuse a follow-up query for unread
        results, and does not move homepages to the end: SELECT decides
        which candidate to open.
        """
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "query is required", "results": []}
        key = (query, int(max_results or 10))
        with self._state.lock:
            cached = self._state.search_cache.get(key)
            if cached is not None:
                return deepcopy(cached)
            if self._state.search_calls >= MAX_WEB_SEARCHES_PER_JOB:
                return {
                    "ok": False,
                    "error_reason": "search_budget_exhausted",
                    "error": (
                        f"This job already made {MAX_WEB_SEARCHES_PER_JOB} "
                        "web_search calls, the maximum allowed."
                    ),
                    "results": [],
                }
            self._state.search_calls += 1
        result = self._search(query, max_results, reorder_homepages=False)
        self._remember_search(result, cache_key=key)
        return result

    def read_page(self, url: str) -> dict[str, Any]:
        """Fetch one URL for the phase controller -- provenance stays, homepage refuse does not.

        SELECT already chose this address from candidates or from a URL
        the user typed. Fetching it is infrastructure; whether the page
        answers the question is ANSWER's job.
        """
        return self._read_guarded(url, refuse_homepage=False)

    def _emit(self, stage: str, detail: str = "") -> None:
        if self._progress is not None:
            try:
                self._progress(stage, detail)
            except Exception:
                pass

    def _remember_search(
        self, result: dict[str, Any], *, cache_key: tuple
    ) -> None:
        with self._state.lock:
            self._state.search_cache[cache_key] = deepcopy(result)
            candidates = [
                item
                for item in (result.get("results") or [])
                if isinstance(item, dict) and item.get("url")
            ]
            for item in candidates:
                self._state.offered_urls.add(_normalize(str(item["url"])))
            if candidates:
                self._state.last_candidates = deepcopy(candidates)
                self._state.read_since_last_search = False

    # -- web_search -----------------------------------------------------

    def _wrap_search(self, func: Callable[..., dict[str, Any]]) -> Callable:
        def web_search(query: str, max_results: int = 10) -> dict[str, Any]:
            """Search the web and return SearXNG's own candidate results.

            Results are returned in SearXNG's own order -- this tool does
            not judge relevance, authority, or currentness; that is your
            job. Look at each result's title/snippet/url yourself, and call
            web_read(url) on the one that actually answers the request.

            Args:
                query (str): The literal search query text. Use a site:
                    filter (e.g. "site:example.org ...") if a specific
                    source should be preferred.
                max_results (int): Maximum candidates to return, capped at
                    15. (default: :obj:`10`)

            Returns:
                dict: ``{"ok": True, "results": [{"url", "title", "snippet",
                "score", "engines", "published_date"}, ...]}`` on success.
            """
            key = ((query or "").strip(), int(max_results or 10))
            with self._state.lock:
                cached = self._state.search_cache.get(key)
                if cached is not None:
                    return deepcopy(cached)
                if self._state.search_calls >= MAX_WEB_SEARCHES_PER_JOB:
                    return {
                        "ok": False,
                        "error_reason": "search_budget_exhausted",
                        "error": (
                            f"This job already made {MAX_WEB_SEARCHES_PER_JOB} "
                            "web_search calls, the maximum allowed."
                        ),
                        "results": [],
                        "instruction": (
                            "Do not call web_search again. Answer from results "
                            "already returned, or continue only with data "
                            "already read."
                        ),
                    }
                unread = self._refuse_search_before_reading()
                if unread is not None:
                    return unread
                self._state.search_calls += 1
            result = func(query=query, max_results=max_results)
            self._remember_search(result, cache_key=key)
            return result

        # `_budgeted_search_tool` already records real network hits under
        # this same name (before its own budget stub). Marking here lets
        # resolve_tools skip a second wrap so one search is one log line.
        if getattr(self._search_fn, "_is_logged_tool", False):
            web_search._is_logged_tool = True
        return web_search

    def _search(
        self,
        query: str,
        max_results: int = 10,
        *,
        reorder_homepages: bool = True,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "query is required", "results": []}
        self._emit("searching", query)
        capped = max(1, min(int(max_results or 10), MAX_SEARCH_RESULTS_PER_QUERY))
        search = self._search_fn
        if search is None:
            from web_search import search_web

            search = search_web
        raw = search(query, number_of_result_pages=capped)
        if isinstance(raw, dict):
            if raw.get("search_budget_exhausted"):
                return {
                    "ok": False,
                    "error_reason": "search_budget_exhausted",
                    "error": (
                        "The internal discovery budget for this job is spent; "
                        "no search ran for this query."
                    ),
                    "results": [],
                }
            raw = raw.get("evidence_already_gathered") or []
        candidates = [item for item in raw if isinstance(item, dict) and not item.get("error")]

        packed = [
            {
                "url": candidate.get("url"),
                "title": str(candidate.get("title") or "")[:200],
                "snippet": str(
                    candidate.get("content") or candidate.get("snippet") or ""
                )[:400],
                "score": candidate.get("score"),
                "engines": candidate.get("engines") or [],
                "published_date": candidate.get("publishedDate"),
                "category": candidate.get("category") or "",
            }
            for candidate in candidates[:capped]
        ]
        # Homepages often rank first and their carousel snippets look like
        # the answer ("X joins Club!"). The ReAct tool puts specific pages
        # first so the model opens an article instead of a landing page.
        # Controller SELECT also benefits: prefer deeper paths and news hits
        # when general engines only return hubs.
        if reorder_homepages:
            specific = [
                item
                for item in packed
                if not _is_site_root(_normalize(str(item.get("url") or "")))
            ]
            roots = [
                item
                for item in packed
                if _is_site_root(_normalize(str(item.get("url") or "")))
            ]
            results = specific + roots
        else:
            results = _prefer_detail_candidates(packed)
        self._emit("done", f"{len(results)} results for {query!r}")
        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "results": results,
            "result_count": len(results),
        }
        if reorder_homepages:
            payload["instruction"] = (
                "Call web_read on a specific article, match, standings or "
                "data URL -- not a site homepage. If that page is a hub or "
                "empty, open a DIFFERENT host from these results before "
                "searching again. Homepage carousel snippets are not "
                "confirmation."
            )
        return payload

    # -- web_read ---------------------------------------------------------

    def _wrap_read(self, func: Callable[..., dict[str, Any]]) -> Callable:
        def web_read(url: str) -> dict[str, Any]:
            """Fetch one page and return its content.

            Renders the page (JavaScript included) and returns whichever of
            the page's own declared records, extracted tables, or cleaned
            text it actually published. This tool does not judge whether the
            page is the right source, current, or about the right
            place/entity -- verify that yourself from what it returns, and
            call web_read on a different URL if it isn't.

            Pass a URL exactly as a web_search result gave it, or one the
            user typed. An address you assembled yourself is refused
            without being fetched, and the real candidates are returned.

            A large table/record set is saved server-side; pass the returned
            ``data_path`` directly to an artifact tool rather than retyping
            the data. If a page has more than one table, EVERY one of them
            is saved individually (see ``tables_saved``) -- `data_path`
            defaults to the largest, which may not be the one you want.

            Args:
                url (str): The full http(s) URL to fetch, copied verbatim
                    from a web_search result or from the user's request.

            Returns:
                dict: ``{"ok": True, "source_url", "title", ...}`` with
                either ``records``/``columns``, or ``columns``/``preview``/
                ``data_path``/``tables_saved`` (tables), or ``text`` (prose).
                ``{"ok": False, "error_reason", "error"}`` on failure.
            """
            return self._read_guarded(url, refuse_homepage=True)

        web_read._job_budget_exhausted = lambda: (
            self._state.read_calls >= MAX_WEB_READS_PER_JOB
        )
        return web_read

    def _read_guarded(self, url: str, *, refuse_homepage: bool) -> dict[str, Any]:
        key = _normalize((url or "").strip())
        if not key:
            return {"ok": False, "error": "url is required"}
        with self._state.lock:
            cached = self._state.read_cache.get(key)
            if cached is not None:
                return deepcopy(cached)
            dead = self._state.dead_urls.get(key)
            if dead is not None:
                return self._error_result(url, dead)
            invented = self._reject_if_invented(url, key)
            if invented is not None:
                return invented
            if refuse_homepage:
                homepage = self._refuse_site_root(url, key)
                if homepage is not None:
                    # The agent DID pick a search result; it was just a
                    # landing page. That attempt must unblock a refined
                    # search (e.g. site:mackolik.com ...) -- otherwise the
                    # next query is refused as unread and the run answers
                    # "not found" with Mackolik sitting unused.
                    self._state.read_since_last_search = True
                    return homepage
            # A genuine attempt at a real candidate, whether or not the
            # fetch succeeds: a dead page is still something learned,
            # so it must unblock the next search.
            self._state.read_since_last_search = True
            host = urlsplit(key).netloc.casefold()
            if host:
                self._state.read_hosts.add(host)
            if self._state.read_calls >= MAX_WEB_READS_PER_JOB:
                return {
                    "ok": False,
                    "source_url": url,
                    "error_reason": "read_budget_exhausted",
                    "error": (
                        f"This job already made {MAX_WEB_READS_PER_JOB} "
                        "web_read calls, the hard per-job limit."
                    ),
                    "instruction": (
                        "Do not call web_read again. Answer with the "
                        "limitation, or continue only with data already read."
                    ),
                }
            self._state.read_calls += 1

        try:
            result = self._read(url)
        except AcquisitionError as exc:
            transient = exc.retryable
            attempts = self._state.read_attempts.get(key, 0) + 1
            self._state.read_attempts[key] = attempts
            if transient and attempts < 2:
                result = self._error_result(url, exc)
                result["retryable"] = True
                result["terminal"] = False
                result["instruction"] = (
                    "This was an explicitly transient network failure. One "
                    "retry for this same URL is allowed."
                )
                return result
            with self._state.lock:
                self._state.dead_urls[key] = exc
            result = self._error_result(url, exc)
            result["retryable"] = False
            result["terminal"] = True
            return result

        with self._state.lock:
            self._state.read_cache[key] = deepcopy(result)
        return result


    def _refuse_search_before_reading(self) -> Optional[dict[str, Any]]:
        """Refuse a new query while the last one's results sit unopened.

        Rewording a query you have not tested teaches you nothing, and it
        is what the model does by default. Measured on the live index, its
        first three or four queries for "Galatasaray son maçtaki ilk 11"
        were all near-identical paraphrases returning the same club hub
        pages; by the time a read finally revealed the opponent and date,
        the search budget was spent and the one query that would have
        worked came back as the exhausted stub.

        Reading is what produces a better query. Requiring one between
        searches makes each search spend the budget on something the
        previous round actually learned, which is the whole shape of
        search -> read -> refine -> read.

        Costs nothing: this returns before the budget is charged, so a
        refused call is not a spent search. It also cannot be dodged by
        guessing a URL -- only a read that clears the provenance check
        counts (see _reject_if_invented).
        """
        state = self._state
        if state.read_since_last_search or not state.last_candidates:
            return None
        return {
            "ok": False,
            "error_reason": "previous_results_unread",
            "error": (
                "The results of the last search have not been opened yet, "
                "so this query was not run and no budget was spent."
            ),
            "results": [],
            "instruction": (
                "web_read one of the candidates below first. Rewording a "
                "query without reading anything returns the same kind of "
                "page. If a page turns out to be a hub or listing, what "
                "you learn from it (the date, the opponent, the version, "
                "the exact title) is what makes the next search work."
            ),
            "candidates": [
                {"url": item.get("url"), "title": str(item.get("title") or "")[:200]}
                for item in state.last_candidates[:10]
            ],
        }

    def _untried_sources(self) -> list[dict[str, Any]]:
        """Candidates hosted somewhere this job has not opened yet.

        Breadth without a favourites list. The complaint this answers is
        "why does it keep going back to the same site instead of trying
        another one", and the tempting fix -- naming sources that are
        usually good -- is exactly the domain knowledge this module must
        not hold: it would encode which outlets matter for football and
        say nothing about central banks, chip errata or drug labels.
        Grouping the candidates the search ALREADY returned by host needs
        no such knowledge, and gives the agent a concrete alternative to
        re-reading a page that told it nothing.
        """
        tried = self._state.read_hosts
        seen: set[str] = set()
        untried: list[dict[str, Any]] = []
        for item in self._state.last_candidates:
            url = item.get("url")
            if not url:
                continue
            host = urlsplit(_normalize(str(url))).netloc.casefold()
            if not host or host in tried or host in seen:
                continue
            seen.add(host)
            untried.append(
                {"url": url, "title": str(item.get("title") or "")[:200]}
            )
        return untried[:5]

    def _refuse_site_root(self, url: str, key: str) -> Optional[dict[str, Any]]:
        """Refuse a bare homepage while a deeper candidate is available.

        Structural, not editorial: a site root is a navigation surface by
        construction. It is not a page ABOUT anything, so it cannot hold
        the specific fact a question asks for -- that is true of every
        site in every field, and needs no knowledge of which one this is.

        This is the page both reported failures were actually built on.
        The agent opened ``https://www.galatasaray.org/``, the extractor
        pulled the weekly-programme widget off it (day-name columns, six
        rows), and because those cells are POPULATED the mostly_empty
        density check passed them as real data -- so the tool went on to
        tell the model to "write the FINAL ANSWER now from the preview".
        The model duly reported that all it had was a calendar of
        weekdays. It never lacked the ability to find the lineup; it was
        handed a homepage and told to answer from it.

        Only fires when a deeper candidate actually exists, so a run whose
        search genuinely returned nothing but roots is not left stuck.
        """
        state = self._state
        if not state.offered_urls or key in self._allowed_urls:
            return None
        if not _is_site_root(key):
            return None
        deeper = [
            {"url": item.get("url"), "title": str(item.get("title") or "")[:200]}
            for item in state.last_candidates
            if item.get("url") and not _is_site_root(_normalize(str(item["url"])))
        ]
        if not deeper:
            return None
        return {
            "ok": False,
            "source_url": url,
            "error_reason": "url_is_a_site_homepage",
            "error": (
                "This is a site's home page, which lists whatever is "
                "current rather than covering one subject, so it was not "
                "fetched."
            ),
            "instruction": (
                "This was a homepage. Do not answer from it. Open a "
                "specific match/standings/news/data page from the "
                "candidates below -- prefer a different site."
            ),
            "candidates": deeper[:10],
        }

    def _reject_if_invented(self, url: str, key: str) -> Optional[dict[str, Any]]:
        """Refuse a URL that no search offered and the user never typed.

        Provenance, not authority. This does not decide which source is
        official, current, or relevant for a request -- it only checks
        where the address itself came from, so it stays free of any
        domain/organization knowledge and works the same for TCMB, NASA or
        a football fixture.

        The failure it removes is specific and was reproduced end to end:
        the agent searched, got real match-report URLs back, and then
        called web_read on addresses it had assembled from a pattern
        (``.../mac/galatasaray-istanbul-vs-corum-fk/2026-08-14``, a club
        page path, a Transfermarkt season path). None existed as given;
        each one cost a turn, and the run ended saying the lineup could
        not be found while the pages that had it sat unread in the search
        results. Prose already told the model not to do this, in two
        places. It did it anyway, so this is enforced instead of asked.

        Deliberately inert until the agent has actually searched: a job
        given a literal URL to fetch has no search results to check
        against, and gating it would break the whole fetch-only path.

        Also keeps the agent on the results of its LATEST query. Once a
        refined search runs, that result set is the working set; reaching
        back to a URL from an earlier, vaguer round is the same mistake as
        inventing one, and it is what the last live run actually did. It
        searched "Galatasaray Çorum FK 14 Ağustos 2026 ilk 11 kadro" --
        whose top three results are lineup pages that do hold the answer
        -- and then opened a Transfermarkt club page left over from its
        first vague query. Deciding WHICH of the fresh results is best
        stays entirely the model's call; this only refuses to let a
        deliberately narrowed search be thrown away unopened.
        """
        state = self._state
        if not state.offered_urls:
            return None
        if key in self._allowed_urls:
            return None
        current = {
            _normalize(str(item["url"]))
            for item in state.last_candidates
            if item.get("url")
        }
        if key in current:
            return None
        candidates = [
            {"url": item.get("url"), "title": str(item.get("title") or "")[:200]}
            for item in state.last_candidates[:10]
        ]
        if key in state.offered_urls:
            return {
                "ok": False,
                "source_url": url,
                "error_reason": "url_from_a_superseded_search",
                "error": (
                    "This URL came from an earlier, broader search, not "
                    "from your most recent one, so it was not fetched."
                ),
                "instruction": (
                    "Your latest query was the more specific one; its "
                    "results are below. Open the one whose title matches "
                    "what you were asked for. If none of them do, say so "
                    "rather than falling back to a general page you "
                    "already know."
                ),
                "candidates": candidates,
            }
        return {
            "ok": False,
            "source_url": url,
            "error_reason": "url_not_from_search_results",
            "error": (
                "This URL was not among the search results and was not "
                "supplied by the user, so it was not fetched. Addresses "
                "assembled from a pattern usually do not exist."
            ),
            "instruction": (
                "Do not retry this URL and do not edit it into another "
                "guess. Choose one of the candidates below -- copy its "
                "`url` value exactly -- or, if none of them can answer the "
                "question, say so."
            ),
            "candidates": candidates,
        }

    def _error_result(self, url: str, exc: AcquisitionError) -> dict[str, Any]:
        return {
            "ok": False,
            "source_url": url,
            "error_reason": exc.reason,
            "error": exc.message,
        }

    def _read(self, url: str) -> dict[str, Any]:
        self._emit("opening", url)
        # Direct PDF downloads routinely hang Chromium for the full
        # WEB_ACQUIRE_TIMEOUT (30s) and return no usable text. Prefer HTML
        # candidates from SEARCH/SELECT; skip .pdf without burning the budget.
        path = (url or "").lower().split("?", 1)[0].split("#", 1)[0]
        if path.endswith(".pdf"):
            return {
                "ok": False,
                "source_url": url,
                "error_reason": "unsupported_content_type",
                "error": (
                    "Direct PDF URLs are skipped to keep research fast; "
                    "open an HTML article/report page instead."
                ),
            }
        engine = self._engine or get_crawl4ai_engine()
        # SSRF/public-URL validation happens inside the engine (up front and
        # per-request via its Playwright route hook) -- not duplicated here.
        #
        # need="" makes the engine's own internal same-host link-following
        # score every link 0 (see html_extract.link_score), so exactly one
        # page comes back -- this is what keeps "one web_read call, one
        # page" true without needing a separate single-page code path in
        # crawl4ai_engine.py.
        acquisition = engine.acquire(url, "")
        page = acquisition.pages[0]
        self._emit("reading", page.url)

        if not page.records and not page.tables and page.javascript_required:
            # First pass looked like an unrendered SPA shell -- heavier
            # client-rendered sites (MSN's article pages are a repeat
            # offender) routinely need more than the default 1.5s to finish
            # hydrating past DOMContentLoaded. One retry with a longer
            # render wait before giving up on the page outright, rather
            # than falling back to a worse/staler source that DID render
            # in time (real bug: this is exactly what silently produced an
            # outdated "last match" answer once).
            retry = engine.acquire(url, "", delay_before_return_html=4.5)
            retry_page = retry.pages[0]
            if retry_page.records or retry_page.tables or (
                retry_page.text.strip() and not retry_page.javascript_required
            ):
                page = retry_page
                self._emit("reading", page.url)

        if page.records:
            return self._deliver_records(page)
        if page.tables:
            return self._deliver_tables(page)
        if page.text.strip() and not page.javascript_required:
            return self._deliver_text(page)
        return self._deliver_failure(page)

    def _deliver_records(self, page: AcquiredPage) -> dict[str, Any]:
        records = page.records[:MAX_RECORDS_PER_READ]
        columns: list[str] = []
        for record in records:
            for key in record:
                if key not in columns:
                    columns.append(key)
        saved = self._web._save_records(page.url, records)
        result: dict[str, Any] = {
            "ok": True,
            "source_url": page.url,
            "title": page.title,
            "record_count": len(records),
            "columns": columns,
            "preview": records[:MAX_INLINE_RECORDS],
        }
        if saved:
            result["data_path"] = saved["saved_to"]
            result["saved_rows"] = saved.get("saved_rows", len(records))
            result["instruction"] = (
                f"The COMPLETE verified data ({result['saved_rows']} records) "
                f"is saved at {result['data_path']}. If a file was requested, "
                "pass that path to the artifact tool; do not read or retype "
                "it. If the user asked a question, write the FINAL ANSWER "
                "now from the preview. Do not search again."
            )
        return result

    def _deliver_tables(self, page: AcquiredPage) -> dict[str, Any]:
        aggregated = _PageTables(page)
        # No selection: which table (if any) is "the right one" is a
        # relevance judgment, not this tool's job. _shape_tables/_save_tables
        # always saves EVERY table individually (tables_saved) and picks a
        # purely structural default for `data_path` -- see that function's
        # own docstring for exactly what "structural" means here.
        shaped = self._web._shape_tables(
            page.url, aggregated, preferred_table_index=None, safe_to_combine=False
        )
        result: dict[str, Any] = {
            "ok": True,
            "source_url": page.url,
            "title": page.title,
            "row_count": shaped.get("total_rows", 0),
            "columns": shaped.get("columns", []),
            "preview": shaped.get("preview", []),
        }
        for key in (
            "data_path", "saved_rows", "combined", "tables_saved",
            "row_limit_reached", "incomplete_warning",
        ):
            if key in shaped:
                result[key] = shaped[key]
        # Attach each table's own caption (real page content -- a <caption>
        # or heading -- not a computed judgment) so the agent can tell which
        # tables_saved entry is which without reading any of them. Same pass
        # flags whether that table is mostly blank/placeholder cells -- a
        # structural data-quality signal, not a relevance one -- so the
        # agent doesn't have to open a file to discover it picked an empty
        # shell.
        tables_saved = result.get("tables_saved")
        if tables_saved:
            for entry in tables_saved:
                index = entry.get("table_index")
                if not isinstance(index, int) or not (0 <= index < len(aggregated.tables)):
                    continue
                if aggregated.captions and index < len(aggregated.captions):
                    caption = aggregated.captions[index]
                    if caption:
                        entry["caption"] = caption
                table = aggregated.tables[index]
                if len(table) > 1 and _table_density(table[1:]) < MIN_TABLE_DENSITY:
                    entry["mostly_empty"] = True

        # Same check on whichever table `data_path`/`preview` actually point
        # at, since that default is exactly what a careless answer would
        # copy without looking at `tables_saved` at all.
        if result.get("row_count", 0) > 0 and _table_density(result.get("preview") or []) < MIN_TABLE_DENSITY:
            result["ok"] = False
            result["error_reason"] = "table_mostly_empty"
            result["error"] = (
                "The extracted table's cells are mostly blank/placeholder "
                "values -- this is not verified data."
            )
            result["instruction"] = (
                "Do not present this table as the answer, and do not fill "
                "in the missing values yourself. If `tables_saved` lists "
                "another table without `mostly_empty` set, that one's own "
                "path may hold the real data instead; otherwise report "
                "plainly that this page did not publish usable data."
            )
        elif result.get("data_path"):
            head = (
                f"The COMPLETE data ({result.get('saved_rows', 0)} rows) is "
                f"saved at {result['data_path']}. If a file was requested, "
                "pass that path to the artifact tool; do not read or retype it. "
            )
            # A page that yielded tables and no prose is a listing surface.
            # Telling the model to "write the FINAL ANSWER now" from it is
            # what produced the reported failure: after reading four real
            # transfer articles, the run's LAST read was a club homepage
            # whose only output was a six-row weekday calendar, and the
            # answer got written from that -- "a table was retrieved but
            # its contents are unknown" -- discarding everything of value
            # read earlier. So the imperative is dropped here; the model
            # may still answer from tables when they genuinely hold the
            # fact (a standings or price table usually does), it is simply
            # no longer pushed to answer from whatever it touched last.
            page_text = (page.text or "").strip()
            # "OTHER NEWS / SEARCH ARTICLES" is not an article. Treating any
            # non-empty string as prose made the tool demand a FINAL ANSWER
            # from a homepage calendar.
            if len(page_text) >= 80:
                delivered, _ = self._web._spend_text(page.text[:MAX_TEXT_CHARS])
                if delivered.strip():
                    result["text"] = delivered
                result["instruction"] = head + (
                    "If the user asked a question, write the FINAL ANSWER now "
                    "from the preview, text, and tables_saved captions and do not "
                    "search again. If these tables only identified what you "
                    "were looking for (a date, an opponent, a version) without "
                    "holding the asked fact, search once more with those "
                    "specifics in the query instead of opening another URL "
                    "from the current results."
                )
            else:
                result["instruction"] = head + (
                    "This page returned tables and NO article text, so it is "
                    "a listing/landing surface. Answer from it only if these "
                    "tables actually contain what was asked. If they do not, "
                    "do NOT report them as all you found, and do not treat "
                    "this page as your conclusion -- earlier pages you read "
                    "may already hold the answer, and any untried source "
                    "below is a different site that has not been checked yet."
                )
                untried = self._untried_sources()
                if untried:
                    result["untried_sources"] = untried
        return result

    def _deliver_text(self, page: AcquiredPage) -> dict[str, Any]:
        delivered, budget_note = self._web._spend_text(page.text[:MAX_TEXT_CHARS])
        result: dict[str, Any] = {
            "ok": bool(delivered.strip()),
            "source_url": page.url,
            "title": page.title,
            "text": delivered,
        }
        if budget_note:
            result["budget_note"] = budget_note
        if result["ok"]:
            result["instruction"] = (
                "If this page answers the question, write the FINAL ANSWER "
                "now and do not search again. If instead it only "
                "identified what you were looking for (a date, an "
                "opponent, a version, a title) without holding the asked "
                "fact, search once more with those specifics in the query "
                "-- that is more likely to reach the right page than "
                "opening another URL from the current results."
            )
        return result

    def _deliver_failure(self, page: AcquiredPage) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "source_url": page.url,
            "title": page.title,
            "error_reason": "no_usable_content",
            "error": "This page had no extractable tables, records or text.",
        }
        if page.javascript_required:
            result["javascript_required"] = True
        return result


__all__ = [
    "MAX_WEB_READS_PER_JOB",
    "MAX_WEB_SEARCHES_PER_JOB",
    "WebTools",
]
