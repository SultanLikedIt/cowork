"""Generic web search tool backed by AgentSearch and/or SearXNG.

Provider-agnostic on purpose: ``web_tools.WebTools.web_search`` calls
``search_web(query, ...)`` internally, while agents see only ``web_search`` and
never see "searxng", "agent-search", or "duckduckgo" in a tool schema.  The
backing engine can therefore change without touching model-facing wiring or
prompts.

When ``AGENT_SEARCH_ENABLED=true``, AgentSearch is blended with SearXNG
(additive): SearXNG keeps the previous discovery baseline, AgentSearch
adds strategy/news packs on top. If the sidecar is down, SearXNG alone
still runs — discovery never depends on AgentSearch being up.

All requests of one search start together. SearXNG is waited for; the
AgentSearch requests only get ``AGENT_SEARCH_GRACE_SECONDS`` past that, and
whatever has not answered by then is left out of this result.

Return shape preserves useful discovery fields: ``score``, ``content``
(snippet), ``engines``, ``positions``, and ``publishedDate``.  The older
``description`` alias for ``content`` is kept so callers that predate the rich
fields continue to work unchanged.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

import agent_search_client

# Docker Compose sets this to the searxng service's in-network address
# (see docker-compose.yml); the localhost fallback is only for running the
# backend directly against a locally-published SearXNG container.
SEARXNG_BASE_URL = (
    os.environ.get("SEARXNG_BASE_URL") or "http://localhost:8080"
).rstrip("/")

SEARXNG_TIMEOUT_SECONDS = 15

# How long AgentSearch may keep answering after SearXNG already has. It is
# additive, so a slow sidecar costs this much at most instead of its full
# 20s HTTP timeout. Measured: the sidecar normally answers in 0.2-0.5s.
AGENT_SEARCH_GRACE_SECONDS = max(
    0.0, float(os.environ.get("AGENT_SEARCH_GRACE_SECONDS") or "1.5")
)

# AgentSearch strategy for general-category searches, or "off" to skip the
# sidecar there (news searches still use it). Its own "general" mode asks
# Bing, DuckDuckGo and Brave through the very same SearXNG instance
# search_web already queried. Measured on six Turkish/English queries: 0-3
# of its 10 URLs were not already in SearXNG's results, and those were
# mostly off-topic (a Steam game for a Rust tokio query, forum spam for a
# history query). "reference" (Wikipedia + Wikidata) returned 0-1 hits and
# took 0.6-0.9s cold. Hence off by default; "general" = old behavior.
AGENT_SEARCH_GENERAL_MODE = (
    os.environ.get("AGENT_SEARCH_GENERAL_MODE") or "off"
).strip().lower()
_AGENT_SEARCH_MODE_OFF = frozenset({"off", "none", "false", "0"})

# Shared across calls: a search is at most four requests, and concurrent
# research workers each run their own.
_SEARCH_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="web-search"
)


def _normalize_result_url(url: str) -> str:
    try:
        parts = urlsplit((url or "").strip())
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), path, "", "")
        )
    except Exception:  # noqa: BLE001
        return (url or "").strip()


def _searx_raw(
    query: str,
    *,
    categories: str,
    language: Optional[str],
    time_range: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "categories": categories or "general",
        "pageno": 1,
    }
    if language:
        params["language"] = language
    if time_range:
        params["time_range"] = time_range

    try:
        response = requests.get(
            f"{SEARXNG_BASE_URL}/search",
            params=params,
            timeout=SEARXNG_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [{"error": f"web search failed ({categories}): {exc}"}]

    results = payload.get("results") or []
    responses: List[Dict[str, Any]] = []
    for i, result in enumerate(results[:limit], start=1):
        snippet = str(result.get("content") or "").strip()
        responses.append(
            {
                "result_id": i,
                "title": result.get("title", ""),
                "description": snippet,
                "url": result.get("url", ""),
                "content": snippet,
                "score": result.get("score"),
                "engines": result.get("engines") or [],
                "positions": result.get("positions") or [],
                "publishedDate": result.get("publishedDate"),
                "category": result.get("category") or categories,
            }
        )
    return responses


def _merge_category_results(
    preferred: List[Dict[str, Any]],
    extra: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    """Dedupe by URL. Prefer ``preferred`` order (usually news) then extra."""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    errors: List[Dict[str, Any]] = []
    for item in list(preferred) + list(extra):
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            errors.append(item)
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        key = _normalize_result_url(url)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= limit:
            break
    if merged:
        for i, item in enumerate(merged, start=1):
            item["result_id"] = i
        return merged
    return errors[:1] if errors else []


_DATE_TOKEN_RE = re.compile(
    r"(?i)\b(?:20\d{2}-\d{2}-\d{2}|20\d{2}|january|february|march|april|may|june|"
    r"july|august|september|october|november|december|ocak|şubat|subat|mart|nisan|"
    r"mayıs|mayis|haziran|temmuz|ağustos|agustos|eylül|eylul|ekim|kasım|kasim|"
    r"aralık|aralik)\b"
)

# News pack is valuable for live/time-sensitive asks; for evergreen "what is X"
# topics it mostly burns latency on engines that timeout/CAPTCHA.
_NEWS_BLEND_RE = re.compile(
    r"(?is)\b(?:"
    r"news|haber|g[uü]ncel|bug[uü]n|today|yesterday|d[uü]n|"
    r"latest|recent|breaking|son\s+dakika|\ben\s+son\b|\bson\b|"
    r"this\s+weekend|hafta\s+sonu|bu\s+hafta|this\s+week|"
    r"right\s+now|anl[ıi]k|canl[ıi]"
    r")\b"
)


def _compact_news_query(query: str) -> Optional[str]:
    """Shorter news-oriented query: topic tokens + ISO dates only.

    News engines often fail on long English month phrases; ISO dates
    (2026-08-29) stay language-agnostic and still match dated articles.
    """
    words = (query or "").split()
    if len(words) <= 8:
        return None
    isos = [w for w in words if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", w)]
    topic = [w for w in words if not _DATE_TOKEN_RE.search(w)][:5]
    if not isos or not topic:
        return None
    return " ".join(topic + isos)[:240]


def _query_wants_news_blend(query: str) -> bool:
    return bool(_NEWS_BLEND_RE.search(query or ""))


def search_web(
    query: str,
    number_of_result_pages: int = 10,
    categories: str = "general",
    language: Optional[str] = None,
    time_range: Optional[str] = None,
    *,
    merge_news: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Search the web and return results with full SearXNG metadata.

    Queries a self-hosted search engine and returns a list of result
    dictionaries.  Every dict contains both the legacy fields (``result_id``,
    ``title``, ``description``, ``url``) and the richer fields that SearXNG
    exposes (``score``, ``content``, ``engines``, ``positions``,
    ``publishedDate``, ``category``).

    ``description`` is identical to ``content`` and is kept only for backward
    compatibility with callers written before the rich fields were added.

    When ``categories`` is ``general`` (the default), also queries the
    ``news`` category and merges results (news first) for time-sensitive /
    newsy asks. Evergreen lookups skip the news pack so discovery does not
    wait on CAPTCHA/timeout news engines. Pass ``merge_news=True/False`` to
    override.

    Args:
        query: The search query string.
        number_of_result_pages: Maximum number of results to return.
            (default: :obj:`10`)
        categories: SearXNG category string, e.g. ``"general"`` or ``"news"``.
            (default: :obj:`"general"`)
        language: BCP-47 language tag to prefer (e.g. ``"tr"``). Passes
            SearXNG's ``language`` param. (default: :obj:`None`)
        time_range: SearXNG time-range filter: ``"day"``, ``"week"``,
            ``"month"``, or ``"year"``. (default: :obj:`None`)
        merge_news: Override default news merge for general searches.

    Returns:
        List of result dicts, or a single ``{"error": "..."}`` dict on
        failure.
    """
    limit = max(1, int(number_of_result_pages or 10))
    cats = (categories or "general").strip().lower()
    if merge_news is not None:
        do_merge = bool(merge_news)
    else:
        do_merge = cats == "general" and _query_wants_news_blend(query)

    # Always keep SearXNG in the mix (old quality path). AgentSearch is an
    # additive layer (strategies/news packs), not a hard replace — that was
    # what made discovery regress after the sidecar landed.
    #
    # These used to run one after another, so a newsy search waited for the
    # sum of up to five requests. They are independent, so they now start
    # together: SearXNG (the baseline) is always waited for, AgentSearch
    # only for a short grace period after it.
    required: Dict[str, concurrent.futures.Future] = {
        "primary": _SEARCH_POOL.submit(
            _searx_raw,
            query,
            categories=cats,
            language=language,
            time_range=time_range,
            limit=limit,
        )
    }
    if do_merge:
        required["news"] = _SEARCH_POOL.submit(
            _searx_raw,
            query,
            categories="news",
            language=language,
            time_range=time_range,
            limit=limit,
        )
    optional: Dict[str, concurrent.futures.Future] = {}
    if agent_search_client.is_enabled():
        if not (cats == "general" and AGENT_SEARCH_GENERAL_MODE in _AGENT_SEARCH_MODE_OFF):
            optional["agent_hits"] = _SEARCH_POOL.submit(
                _agent_search_raw, query, categories=cats, limit=limit
            )
        # Agent news pack only when the ask is actually newsy — otherwise
        # AgentSearch general already fills strategy gaps without a second
        # round-trip that often times out on news engines.
        if do_merge:
            optional["agent_news"] = _SEARCH_POOL.submit(
                _agent_news_with_compact_retry, query, limit
            )

    primary = _future_hits(required["primary"])
    news = _future_hits(required["news"]) if "news" in required else []
    deadline = time.monotonic() + AGENT_SEARCH_GRACE_SECONDS
    agent_hits = _future_hits(optional["agent_hits"], deadline=deadline) if "agent_hits" in optional else []
    agent_news = _future_hits(optional["agent_news"], deadline=deadline) if "agent_news" in optional else []
    if not _usable_hits(agent_hits):
        agent_hits = []
    if not _usable_hits(agent_news):
        agent_news = []

    if not do_merge:
        if agent_hits:
            # SearXNG first (stable), AgentSearch fills gaps.
            return _merge_category_results(primary, agent_hits, limit=limit)
        return primary[:limit]

    # News first (agent news → searx news), then general (searx → agent).
    preferred_news = _merge_category_results(agent_news, news, limit=limit)
    preferred_general = _merge_category_results(primary, agent_hits, limit=limit)
    return _merge_category_results(preferred_news, preferred_general, limit=limit)


def _usable_hits(hits: List[Dict[str, Any]]) -> bool:
    return bool(hits) and not any("error" in h for h in hits)


def _future_hits(
    future: concurrent.futures.Future,
    *,
    deadline: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Result of one search request, or [] if it failed or missed ``deadline``.

    A request that misses the deadline keeps running on its pool thread
    until its own HTTP timeout; only this search stops waiting for it.
    """
    timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
    try:
        value = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return []
    except Exception as exc:  # noqa: BLE001 - one engine never fails the search
        return [{"error": f"web search request failed: {exc}"}]
    return value if isinstance(value, list) else []


def _agent_news_with_compact_retry(query: str, limit: int) -> List[Dict[str, Any]]:
    hits = _agent_search_raw(query, categories="news", limit=limit)
    if not _usable_hits(hits):
        compact = _compact_news_query(query)
        if compact and compact != query:
            hits = _agent_search_raw(compact, categories="news", limit=limit)
    return hits


def _agent_search_raw(
    query: str,
    *,
    categories: str,
    limit: int,
) -> List[Dict[str, Any]]:
    mode: Optional[str] = None
    if (categories or "general").strip().lower() == "general":
        mode = AGENT_SEARCH_GENERAL_MODE
    return agent_search_client.search(
        query,
        limit=limit,
        categories=categories,
        mode=mode,
    )


__all__ = [
    "search_web",
    "SEARXNG_BASE_URL",
    "_merge_category_results",
    "_query_wants_news_blend",
]
