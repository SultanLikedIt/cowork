"""Structural web phase controller: SEARCH → SELECT → FETCH → ANSWER.

Stop/continue is a phase cut, not a ReAct tool loop. The model writes the
query, picks URLs, and writes the answer. This module owns the sequence,
budgets, provenance filter, and append-only evidence ledger. It does not
import CAMEL -- callers inject ``complete`` (and search/fetch).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field, ValidationError

import reranker
import web_fetch

MAX_SEARCH_PHASES = 3
MAX_SEARCH_PHASES_MULTI = 4
# Survey: multi-angle research without the old 8-search / 12-fetch marathon.
MAX_SEARCH_PHASES_SURVEY = 5
MAX_FETCHES = 6
MAX_FETCHES_SURVEY = 8
SELECT_LIMIT = 3
SELECT_LIMIT_SURVEY = 2
# PDF/Word/report jobs: keep caps tight, but leave room to replace an
# off-topic first pair (place-name-only hits) with a fact-matching page.
MAX_SEARCH_PHASES_ARTIFACT = 3
MAX_FETCHES_ARTIFACT = 4
SELECT_LIMIT_ARTIFACT = 2
# Recovery / compact pass: one search, two pages, no refine/retry loop.
MAX_SEARCH_PHASES_COMPACT = 1
MAX_FETCHES_COMPACT = 2
SELECT_LIMIT_COMPACT = 2
MAX_PLAN_ANGLES = 4
MIN_PLAN_ANGLES = 2
# Deep research: user opt-in only (never phrase-triggered like "survey"
# above). Genuinely comprehensive, not a quick pass with a bigger number —
# these are generous on purpose; treat them as headroom, not a target to
# hit on every run. A run legitimately stops earlier once angles are
# covered or candidates run out.
MAX_SEARCH_PHASES_DEEP = 30
MAX_FETCHES_DEEP = 60
SELECT_LIMIT_DEEP = 5
MAX_PLAN_ANGLES_DEEP = 12
# Angles, not the search budget, are what a deep run actually spends: the
# controller works roughly one search per angle, so a plan of 6 finishes
# around 10 searches and leaves two thirds of the 30 unused. Measured on
# two deep runs of similar length -- one planned wide and read 47 pages,
# one planned narrow and read 8, and the narrow one produced a report
# thin enough to be worth nothing. Raised toward the ceiling so an
# explicit deep request actually buys breadth.
MIN_PLAN_ANGLES_DEEP = 10
MAX_EVIDENCE_TEXT = 4_000
# Below this a "successful" text fetch carries no usable prose -- it is a
# consent wall, a paywall stub or a JS shell. Deliberately low: the point
# is to reject shells, not to judge whether a real page is worth reading.
MIN_PROSE_EVIDENCE_CHARS = 400
# Outer guard on one page open. The engine already bounds the request it
# issues (crawl4ai_engine.ACQUIRE_TIMEOUT_SECONDS), but that bound lives
# inside a call this layer runs on a worker thread -- if the call itself
# never returns, nothing above it was watching the clock. Set well above
# the engine's own limit plus its one SPA re-read, so this only fires when
# that machinery has genuinely stopped answering.
FETCH_WALL_CLOCK_SECONDS = 90.0
# A fetch round stops waiting on slow pages once others have loaded (see
# _gather_until_enough): FETCH_STRAGGLER_GRACE_SECONDS after the first
# usable page, FETCH_SETTLE_GRACE_SECONDS once FETCH_ENOUGH_SUCCESSES are in.
FETCH_ENOUGH_SUCCESSES = max(1, int(os.environ.get("WEB_FETCH_ENOUGH_SUCCESSES") or 2))
FETCH_STRAGGLER_GRACE_SECONDS = float(
    os.environ.get("WEB_FETCH_STRAGGLER_GRACE_SECONDS") or 6.0
)
FETCH_SETTLE_GRACE_SECONDS = float(
    os.environ.get("WEB_FETCH_SETTLE_GRACE_SECONDS") or 1.5
)
MAX_CANDIDATE_SNIPPET = 400

CompleteFn = Callable[[str, str, str, Optional[type]], Awaitable[Any]]
SearchFn = Callable[[str, int], dict[str, Any]]
FetchFn = Callable[[str], dict[str, Any]]
ProgressFn = Callable[[str, str], None]


class SearchQuery(BaseModel):
    query: str = ""


class UrlSelection(BaseModel):
    urls: list[str] = Field(default_factory=list)


class ResearchPlan(BaseModel):
    angles: list[str] = Field(default_factory=list)


class CoverageAssessment(BaseModel):
    enough: bool = False
    missing: list[str] = Field(default_factory=list)


@dataclass
class ControllerResult:
    evidence: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    search_count: int = 0
    fetch_count: int = 0
    successful_fetches: int = 0
    handoff: Optional[dict[str, Any]] = None
    skipped_search: bool = False
    covered_targets: list[str] = field(default_factory=list)
    target_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (step name, seconds, detail) for timed steps that are neither tool nor model calls.
    on_timing: Optional[Callable[[str, float, str], None]] = None


def _normalize_url(url: str) -> str:
    try:
        return web_fetch._normalize_url(url)
    except (ValueError, TypeError):
        parsed = urlparse((url or "").strip())
        path = parsed.path.rstrip("/") or "/"
        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                "",
                parsed.query,
                "",
            )
        )


def recover_model(response_text: Any, schema: type) -> Optional[Any]:
    """Local JSON recover -- no second model call. Same shape as routing."""
    raw = (response_text or "").strip() if isinstance(response_text, str) else ""
    if not raw:
        return None
    fenced = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced).strip()
    start = fenced.find("{")
    if start < 0:
        return None
    try:
        decoded, _ = json.JSONDecoder().raw_decode(fenced[start:])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    try:
        return schema.model_validate(decoded)
    except ValidationError:
        return None


def is_successful_fetch(result: dict[str, Any]) -> bool:
    """Technical fullness only -- not 'does this page answer the question'."""
    if not result.get("ok"):
        return False
    if result.get("error_reason") == "table_mostly_empty":
        return False
    text = str(result.get("text") or "").strip()
    preview = result.get("preview") or []
    records = result.get("records") or []
    row_count = int(result.get("row_count") or 0)
    record_count = int(result.get("record_count") or 0)
    return bool(text or preview or records or row_count > 0 or record_count > 0)


def _candidate_urls(search_result: dict[str, Any]) -> list[str]:
    return [str(item["url"]) for item in _candidate_entries(search_result)]


def _candidate_entries(search_result: dict[str, Any]) -> list[dict[str, str]]:
    """Search hits with title — used for SELECT and Kaynaklar fallback."""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in search_result.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        key = _normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "url": url,
                "title": str(item.get("title") or "").strip(),
            }
        )
    return entries


_DELIVERY_NOISE_RE = re.compile(
    r"(?is)\b(?:"
    r"pdf|docx?|pptx?|xlsx?|csv|excel|word|powerpoint|html|"
    r"sunum|slayt|deck|presentation|spreadsheet|workbook|"
    r"indirilebilir|dosya(?:lar)?|rapor\s*(?:hazırla|yaz|oluştur)|"
    r"hazırla|oluştur|yazmanı?\s*istiyorum|m[üu]mk[üu]nse|l[üu]tfen|"
    # ö/o, ğ/g are distinct letters (unlike ı/i, which \b(?is) already
    # folds) — ASCII-typed Turkish ("gorsel", "fotograf") needs an
    # explicit alternative or it survives into the query untouched.
    r"g[öo]rsel(?:i|le|ini)?|image|foto(?:[gğ]raf)?|as\s+a\s+(?:pdf|word|pptx)|"
    # \bbana\b, not "bana\s+": trailing whitespace is absent at end of the
    # ask ("… getir bana"), which left the pronoun in the query.
    r"olarak\s+(?:ver|iste|hazırla|yaz)|\bbana\b|\bbize\b|"
    # "bilgisayarda aç / open on my computer" is the local-computer bridge
    # asking where the file lands. It reached SEARCH as topic words and
    # turned "istanbul namaz vakitleri" into a query that returned
    # tourist guides.
    r"bilgisayar[ıi]?m?(?:da|a|[ıi]n)?\s*(?:a[çc]|indir|kaydet)\w*|"
    r"masa[üu]st[üu](?:ne|nde)?\s*(?:a[çc]|kaydet)\w*|"
    r"open\s+(?:it\s+)?(?:on|in)\s+my\s+(?:computer|desktop)|"
    r"kaynak\s+link(?:ler(?:i|ini)?)?|"
    r"link(?:ler(?:i|ini)?)?\s+ekle|"
    # "kısa" as a length adjective ("kısa bir PDF/sunum/rapor hazırla") is
    # delivery noise, not a fact word — but "en kısa X" (the shortest X)
    # is a real superlative ask, so the lookbehind spares that case.
    r"(?<!en\s)k[ıi]sa\b|"
    r"\bver\b|\bekle\b"
    r")\b"
)

# Machine block Research appends for the LLM — must never become SEARCH tokens.
_SEARCH_HINT_BLOCK_RE = re.compile(r"(?is)\n*\s*SEARCH\s+HINT\s*:[\s\S]*$")

# Meta instructions that pollute SEARCH (any language / any topic).
_INSTRUCTION_NOISE_RE = re.compile(
    r"(?is)\b(?:"
    r"g[uü]ncel\s+(?:bir\s+)?kaynak\s+bul|"
    r"(?:g[uü]venilir|resmi)\s+(?:bir\s+)?kaynak|"
    r"\d\s*[-–—]\s*\d\s*se[cç]enek(?:\s+yaz)?|"
    r"(?:3|4|5|\d+)\s*[-–—]?\s*(?:\d+\s*)?se[cç]enek(?:\s+yaz)?|"
    r"kaynak\s+linki|"
    r"k[ıi]sa\s+not|"
    r"bulamazsan|"
    # Bare imperatives telling the agent what to do. One of these is enough
    # to wreck discovery on its own: "ankara namaz vakitleri" returns
    # Diyanet, "ankara namaz vakitlerini bul" returns Wikipedia and the
    # governor's office. Suffixed forms ("bulur musun", "getirsene") are
    # covered by the optional tail.
    r"\b(?:bul|getir|listele|g[öo]ster|s[öo]yle|derle|topla)"
    r"(?:sana?|senden?|ur\s*musun|ir\s*misin|iver)?\b|"
    r"\b(?:find|fetch|list|show|tell|gather)\s+(?:me|us)\b|"
    r"uydurma|"
    r"net\s+s[oö]yle|"
    r"sonucu?\s+(?:olarak\s+)?(?:word|pdf|docx|excel)|"
    r"yer,?\s*tarih(?:\s*/\s*saat)?|"
    r"tarih\s*/\s*saat|"
    r"sonucu?|"
    r"\(yer.*?\)|"
    r"find\s+(?:a\s+)?(?:current\s+|reliable\s+)?source|"
    r"don'?t\s+invent|"
    r"do\s+not\s+invent|"
    r"write\s+\d|"
    r"list\s+\d|"
    r"options?(?:\s+with)?|"
    r"place/?time|"
    r"deliver(?:\s+as)?|"
    r"don'?t\s+invent|"
    r"as\s+(?:a\s+)?(?:word|pdf|docx)\b|"
    r"please\s+(?:find|write|list)|"
    r"respond\s+with|"
    r"format\s+as|"
    r"search\s+hint|"
    r"prefer\s+html|"
    r"reputable\s+on[- ]topic|"
    r"generic\s+hubs?|"
    r"off[- ]topic\s+encyclopedia|"
    r"\.?pdf\s+downloads?"
    r")\b"
)

_TIME_RELATIVE_RE = re.compile(
    r"(?is)\b(?:"
    r"bu\s+hafta\s+sonu|hafta\s+sonu|this\s+weekend|weekend|"
    r"bug[uü]n|today|yar[ıi]n|tomorrow|"
    r"bu\s+hafta|this\s+week|"
    r"bu\s+ay|this\s+month|"
    r"[oö]n[uü]m[uü]zdeki\s+(?:hafta|g[uü]nler)|coming\s+week|"
    r"next\s+week|gelecek\s+hafta"
    r")\b"
)

# "son maç / last X" — latest completed instance, not a calendar window.
# Do NOT inject today's ISO dates (those match fixture/upcoming pages).
_LAST_OCCURRENCE_RE = re.compile(
    r"(?is)(?:\ben\s+son\b|\blatest\b|\blast\b|\bson\b)"
)
_HISTORICAL_SPAN_RE = re.compile(
    # Trailing suffix class, not \b: Turkish agglutinates the unit, so
    # "son 5 yılda" / "son 6 aylık" are spans just like "son 5 yıl".
    r"(?is)\b(?:son|last|past)\s+\d+\s*"
    r"(?:g[uü]n|ay|y[ıi]l|hafta|saat|days?|months?|years?|weeks?|hours?)"
    r"[a-zçğıöşü]*\b"
)
_PREVIEW_OR_SCHEDULE_RE = re.compile(
    r"(?is)(?:"
    r"muhtemel|beklenen|tahmini|olas[ıi]|predicted|probable|expected\s+line|"
    r"preview|upcoming|yakla[sş]an|[oö]n\s*izleme|"
    r"fikst[uü]r|fixtures?|\bschedule\b"
    r")"
)

# A settled instance already happened: scores, past-tense outcomes, official
# confirmations. Used to keep a real recap that merely links a "muhtemel 11"
# sidebar from being thrown away as speculation.
_SETTLED_RESULT_RE = re.compile(
    r"(?is)(?:"
    r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b|"
    r"sonu[cç]land[ıi]|kazand[ıi]|yendi|ma[gğ]lup|berabere|bitti|"
    r"tamamland[ıi]|kesinle[sş]ti|sahaya\s+[cç][ıi]kt[ıi]|"
    r"a[cç][ıi]kland[ıi]|resm[iî]|"
    r"final\s+score|full[-\s]?time|full\s+time|"
    r"\bwon\b|\bbeat\b|defeated|\bended\b|\brecap\b|official"
    r")"
)

_QUERY_FILLER_RE = re.compile(
    r"(?is)^(?:hangileri|hangi|nedir|neler|ne|what|which|where|how|"
    r"about|var\??|mı\??|mi\??|about)$"
)

_TR_MONTHS = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}

_MONTH_ALIASES: dict[int, tuple[str, ...]] = {
    1: ("january", "ocak", "jan"),
    2: ("february", "şubat", "subat", "feb"),
    3: ("march", "mart", "mar"),
    4: ("april", "nisan", "apr"),
    5: ("may", "mayıs", "mayis"),
    6: ("june", "haziran", "jun"),
    7: ("july", "temmuz", "jul"),
    8: ("august", "ağustos", "agustos", "aug"),
    9: ("september", "eylül", "eylul", "sep", "sept"),
    10: ("october", "ekim", "oct"),
    11: ("november", "kasım", "kasim", "nov"),
    12: ("december", "aralık", "aralik", "dec"),
}

_QUANT_ASK_RE = re.compile(
    r"(?is)\b(?:"
    r"ihracat|ithalat|d[iı]ş\s*ticaret|istatistik|rakam(?:lar)?|"
    r"export|import|trade\s+balance|statistics|skor|kaç|yüzde|oran|"
    r"gol\s*krall|ranking|sıralama|puan|tablo|milyar|million|billion"
    r")\b"
)

_FIGURE_SNIPPET_RE = re.compile(
    r"(?is)(?:"
    r"\d+[.,]?\d*\s*(?:milyar|milyon|billion|million|%|usd|\$|tl|euro|gol|puan)|"
    r"(?:ihracat|ithalat|export|import|skor|gol).{0,48}\d|"
    r"\d.{0,48}(?:ihracat|ithalat|export|import|skor|gol)"
    r")"
)

_STOPWORDS = frozenset(
    {
        "bir",
        "bu",
        "şu",
        "ve",
        "ile",
        "için",
        "icin",
        "olan",
        "olarak",
        "hakkında",
        "hakkinda",
        "detaylı",
        "detayli",
        "güncel",
        "guncel",
        # Connectors, not content: "X ile ilgili/birlikte" ≈ "about/with X" —
        # never the actual fact/entity being asked for.
        "ilgili",
        "birlikte",
        "the",
        "and",
        "for",
        "with",
        "about",
        "from",
        "that",
        "this",
        "please",
        "also",
        # 2-char fillers — only reachable since tokens of length 2 are kept
        # (so "11", "AB", "BM", "17" survive as real content words).
        "de",
        "da",
        "ki",
        "mi",
        "mı",
        "mu",
        "mü",
        "ne",
        "ya",
        "is",
        "it",
        "in",
        "on",
        "at",
        "to",
        "of",
        "by",
        "as",
        "an",
        "or",
        "if",
        "be",
        "do",
        "we",
        "my",
        "me",
        "no",
        "so",
    }
)


def request_looks_like_stats(text: str) -> bool:
    """Backward-compatible alias: quantitative / figure-seeking asks."""
    return bool(_QUANT_ASK_RE.search(text or ""))


def _content_tokens(text: str) -> list[str]:
    """Content words with original casing; dedupe/stopword on casefold.

    Numbers are kept at any length: "ilk 11" loses its list size and
    "son 5 yılda" turns into the very different "son yılda" if a digit floor
    drops them. Two-letter words are kept for the same reason — "AB", "BM",
    "TC" are the subject of their ask — with the short fillers they let
    through ("de", "of", "mi") handled by _STOPWORDS.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]{2,}|[0-9]+", text or ""):
        key = raw.casefold()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        tokens.append(raw)
    return tokens


# How much of the topic the answer should span ("1 haftalık", "3 günlük",
# "7-day") — a shape constraint on the reply, not part of what the page is
# called. Engines treat the extra words as topic terms: "istanbul namaz
# vakitleri" returns Diyanet, "istanbul namaz vakitleri 1 haftalik" returns
# tourist guides. The words stay in the request the ANSWER phase reads.
_OUTPUT_SCOPE_RE = re.compile(
    r"(?is)\b\d+\s*[-–]?\s*(?:"
    r"g[üu]nl[üu]k|haftal[ıi]k|ayl[ıi]k|y[ıi]ll[ıi]k|saatlik|"
    r"day|days|week|weeks|month|months"
    r")\b"
)


def _cleaned_ask_text(request_text: str) -> str:
    """User fact words only — drop SEARCH HINT / delivery / meta instructions."""
    cleaned = _SEARCH_HINT_BLOCK_RE.sub(" ", request_text or "")
    cleaned = _DELIVERY_NOISE_RE.sub(" ", cleaned)
    cleaned = _INSTRUCTION_NOISE_RE.sub(" ", cleaned)
    cleaned = _OUTPUT_SCOPE_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" .,;:!?")


def _ask_tokens(request_text: str) -> list[str]:
    return [
        t
        for t in _content_tokens(_cleaned_ask_text(request_text))
        if not _QUERY_FILLER_RE.match(t)
    ]


def _token_in_text(token: str, blob_cf: str) -> bool:
    """True when ``token`` appears in text, allowing a short Turkish stem."""
    t = (token or "").casefold()
    if not t or not blob_cf:
        return False
    if t in blob_cf:
        return True
    if t.isdigit():
        return bool(re.search(rf"(?<!\d){re.escape(t)}(?!\d)", blob_cf))
    # vakitlerini → vakit… so "namaz vakitleri" still counts.
    if len(t) >= 7:
        stem = t[:5]
        return bool(re.search(rf"(?<![a-zçğıöşü0-9]){re.escape(stem)}", blob_cf))
    return False


def _subject_and_payload_tokens(request_text: str) -> tuple[list[str], list[str]]:
    """Split place/entity words from the asked fact words.

    First half ≈ who/where; second half ≈ what was asked. A classifieds
    page that only repeats the city is not on-topic for the fact.
    """
    tokens = _ask_tokens(request_text)
    if len(tokens) < 3:
        return tokens, tokens
    split = max(1, len(tokens) // 2)
    return tokens[:split], tokens[split:]


def request_looks_last_occurrence(text: str) -> bool:
    """True for 'son / last / latest' of one instance, not 'son 5 yıl'."""
    raw = text or ""
    if _HISTORICAL_SPAN_RE.search(raw):
        return False
    return bool(_LAST_OCCURRENCE_RE.search(raw))


def _parse_anchor_date(current_date: Optional[str]) -> Optional[date]:
    """Best-effort parse of runtime date strings into a calendar date."""
    raw = (current_date or "").strip()
    if not raw:
        return None
    iso = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    month_map = {
        alias: month
        for month, aliases in _MONTH_ALIASES.items()
        for alias in aliases
    }
    m = re.search(
        r"(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+(20\d{2})",
        raw,
    )
    if m:
        day = int(m.group(1))
        mon = month_map.get(m.group(2).casefold())
        year = int(m.group(3))
        if mon:
            try:
                return date(year, mon, day)
            except ValueError:
                return None
    return None


def request_looks_time_sensitive(text: str) -> bool:
    """True when the ask depends on 'now' (weekend/today/this week/…)."""
    return bool(_TIME_RELATIVE_RE.search(text or ""))


def _time_window_query_terms(
    request_text: str, current_date: Optional[str]
) -> str:
    """Concrete calendar terms for time-relative asks (domain-agnostic)."""
    anchor = _parse_anchor_date(current_date)
    if anchor is None or not request_looks_time_sensitive(request_text):
        return ""
    text = (request_text or "").casefold()
    month_en = anchor.strftime("%B")
    year = anchor.year

    def _fmt(d: date) -> str:
        # ISO first (engine-safe); add TR + EN month names for local news.
        return (
            f"{d.isoformat()} {d.day} {_TR_MONTHS[d.month]} "
            f"{d.strftime('%B')} {d.year}"
        )

    if re.search(r"(?is)\b(?:bu\s+hafta\s+sonu|hafta\s+sonu|this\s+weekend|weekend)\b", text):
        # Saturday/Sunday of the upcoming weekend (or this weekend if Sat/Sun).
        weekday = anchor.weekday()  # Mon=0
        if weekday <= 5:  # Mon-Sat → this week's Saturday
            saturday = anchor + timedelta(days=(5 - weekday))
        else:  # Sunday → today is the weekend; keep this Sunday's Saturday
            saturday = anchor - timedelta(days=1)
        sunday = saturday + timedelta(days=1)
        return f"{_fmt(saturday)} {_fmt(sunday)}"

    # _fmt already ends with the English month and year; repeating them here
    # only ate room from the topic words under the 240-char query cap.
    if re.search(r"(?is)\b(?:bug[uü]n|today)\b", text):
        return _fmt(anchor)

    if re.search(r"(?is)\b(?:yar[ıi]n|tomorrow)\b", text):
        return _fmt(anchor + timedelta(days=1))

    if re.search(r"(?is)\b(?:bu\s+hafta|this\s+week)\b", text):
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        return f"{_fmt(start)} {_fmt(end)} {month_en} {year}"

    if re.search(r"(?is)\b(?:bu\s+ay|this\s+month)\b", text):
        return f"{month_en} {year}"

    if re.search(r"(?is)\b(?:next\s+week|gelecek\s+hafta|[oö]n[uü]m[uü]zdeki\s+hafta)\b", text):
        start = anchor - timedelta(days=anchor.weekday()) + timedelta(days=7)
        end = start + timedelta(days=6)
        return f"{_fmt(start)} {_fmt(end)}"

    return f"{month_en} {year}"


def fact_seed_query(
    request_text: str, current_date: Optional[str] = None
) -> Optional[str]:
    """First search from the user's fact words — works for any research ask.

    Strips delivery *and* instruction noise so SEARCH does not chase
    file-format pages or meta prompts ("find a source", "write 3 options").
    For time-relative asks, appends a concrete calendar window from
    ``current_date`` so engines are not stuck on stale "this weekend" pages.
    """
    tokens = _ask_tokens(request_text)
    if len(tokens) < 3:
        return None
    # Prefer content tokens over raw first-N words (which still keep
    # "hangileri var?" style filler after partial cleaning).
    query = " ".join(tokens[:10])
    window = _time_window_query_terms(request_text, current_date)
    if window:
        # Time window must survive the length cap — truncate topic first.
        room = max(24, 240 - len(window) - 1)
        topic = query[:room].rsplit(" ", 1)[0].strip() if len(query) > room else query
        query = f"{topic} {window}".strip()
    elif current_date and request_looks_last_occurrence(request_text or ""):
        # "son maç / latest release" with no calendar window: without the
        # current year engines happily return a prior season's instance.
        # Current year only — never y-1/y-2 like the stats branch below.
        ym = re.search(r"(20\d{2})", current_date)
        if ym and ym.group(1) not in query:
            query = f"{query} {ym.group(1)}"
    elif current_date and request_looks_like_stats(request_text or ""):
        ym = re.search(r"(20\d{2})", current_date)
        if ym and ym.group(1) not in query:
            y = int(ym.group(1))
            query = f"{query} {y - 2} {y - 1}"
    return query[:240]


# Keep old name for imports/tests during transition.
def stats_seed_query(
    request_text: str, current_date: Optional[str] = None
) -> Optional[str]:
    return fact_seed_query(request_text, current_date)


def _is_pdf_url(url: str) -> bool:
    return (url or "").lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf")


def _month_mentions(blob: str) -> set[int]:
    found: set[int] = set()
    cf = (blob or "").casefold()
    for month, aliases in _MONTH_ALIASES.items():
        if any(alias in cf for alias in aliases):
            found.add(month)
    return found


def _published_date(item: dict[str, Any]) -> Optional[date]:
    """Parse the publish date SearXNG/AgentSearch attach to a hit."""
    raw = str(
        item.get("published_date") or item.get("publishedDate") or ""
    ).strip()
    if not raw:
        return None
    iso = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if not iso:
        return None
    try:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    except ValueError:
        return None


_TEXT_DATE_RE = re.compile(
    r"(?is)\b(?:"
    r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})"          # 2026-08-21
    r"|(\d{1,2})[./](\d{1,2})[./](20\d{2})"           # 21.08.2026
    r")\b"
)

_TEXT_MONTH_DATE_RE = re.compile(
    r"(?is)\b(\d{1,2})\s+"
    r"(ocak|şubat|subat|mart|nisan|mayıs|mayis|haziran|temmuz|ağustos|agustos|"
    r"eylül|eylul|ekim|kasım|kasim|aralık|aralik|"
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(20\d{2})\b"
)

_MONTH_BY_ALIAS = {
    alias: month for month, aliases in _MONTH_ALIASES.items() for alias in aliases
}


def _dates_in_text(blob: str) -> list[date]:
    """Every calendar date written in a title/snippet, in any common form."""
    found: list[date] = []
    for m in _TEXT_DATE_RE.finditer(blob or ""):
        try:
            if m.group(1):
                found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            else:
                found.append(date(int(m.group(6)), int(m.group(5)), int(m.group(4))))
        except ValueError:
            continue
    for m in _TEXT_MONTH_DATE_RE.finditer(blob or ""):
        month = _MONTH_BY_ALIAS.get(m.group(2).casefold())
        if not month:
            continue
        try:
            found.append(date(int(m.group(3)), month, int(m.group(1))))
        except ValueError:
            continue
    return found


def _instance_recency_bonus(blob: str, anchor: Optional[date]) -> int:
    """Rank *which instance* a page is about, from dates in its own text.

    Pinning the year was not enough: asked for Galatasaray's last match, the
    ranking happily returned the season's opening week, because every recap
    of every 2026 match matches the wording equally well. Sports recaps,
    releases, and bulletins nearly always carry their own date in the title
    or snippet, so the newest already-happened date wins.
    """
    if anchor is None:
        return 0
    dates = [d for d in _dates_in_text(blob) if abs((anchor - d).days) <= 400]
    if not dates:
        return 0
    past = [d for d in dates if (anchor - d).days >= 0]
    if not past:
        # Only future dates: a fixture/preview, not the instance that happened.
        return -8
    age_days = (anchor - max(past)).days
    if age_days <= 7:
        return 10
    if age_days <= 21:
        return 6
    if age_days <= 60:
        return 2
    return -4


def _recency_bonus(item: dict[str, Any], anchor: Optional[date]) -> int:
    """Freshness score for latest/timed asks; 0 when either date is unknown.

    Absent dates score 0 rather than negative: plenty of good pages simply do
    not publish one, and penalising them would rank the dated-but-wrong page
    above the undated-but-right one.
    """
    if anchor is None:
        return 0
    published = _published_date(item)
    if published is None:
        return 0
    age_days = (anchor - published).days
    if age_days < -14:
        return -6  # dated well past the ask — a fixture/announcement page
    if age_days <= 7:
        return 8
    if age_days <= 30:
        return 5
    if age_days <= 365:
        return 1
    return -6


def _prefer_relevant_candidates(
    search_result: dict[str, Any],
    request_text: str,
    current_date: Optional[str] = None,
) -> dict[str, Any]:
    """Rank hits for any ask: on-topic HTML before PDFs, hubs, stale listicles."""
    results = [
        item
        for item in (search_result.get("results") or [])
        if isinstance(item, dict) and item.get("url")
    ]
    if len(results) < 2:
        return search_result
    tokens = _ask_tokens(request_text)
    subject, payload = _subject_and_payload_tokens(request_text)
    wants_numbers = request_looks_like_stats(request_text)
    time_sensitive = request_looks_time_sensitive(request_text)
    last_occurrence = request_looks_last_occurrence(request_text)
    anchor = _parse_anchor_date(current_date) if time_sensitive else None
    # Recency ranking needs an anchor even when no calendar window was named.
    recency_anchor = (
        _parse_anchor_date(current_date)
        if (time_sensitive or last_occurrence)
        else None
    )
    window_terms = {
        t.casefold()
        for t in _time_window_query_terms(request_text, current_date).split()
        if len(t) >= 3
    }

    def _score(item: dict[str, Any]) -> int:
        url = str(item.get("url") or "")
        blob = (
            f"{item.get('title') or ''} {item.get('content') or ''} "
            f"{item.get('snippet') or ''} {url}"
        )
        blob_cf = blob.casefold()
        score = 0
        if tokens:
            hits = sum(1 for t in tokens[:12] if _token_in_text(t, blob_cf))
            score += min(hits, 5) * 2
        pay_hits = sum(1 for t in payload[:8] if _token_in_text(t, blob_cf))
        sub_hits = sum(1 for t in subject[:8] if _token_in_text(t, blob_cf))
        score += pay_hits * 5
        score += min(sub_hits, 3)
        # Place/brand-only hits (city listings, club fixtures) are off-topic.
        if payload and payload != subject and pay_hits == 0:
            score -= 14
        if wants_numbers and _FIGURE_SNIPPET_RE.search(blob):
            score += 5
        if window_terms:
            score += min(sum(1 for t in window_terms if t in blob_cf), 4) * 2
        if last_occurrence and _PREVIEW_OR_SCHEDULE_RE.search(blob):
            score -= 10
        # SearXNG hands us publishedDate but nothing read it, so a 2019 recap
        # outranked last week's whenever its wording matched better.
        score += _recency_bonus(item, recency_anchor)
        if last_occurrence:
            # Which instance the page is about, not when it was published.
            score += _instance_recency_bonus(blob, recency_anchor)
        if _is_pdf_url(url):
            score -= 8
        host = (urlparse(url).hostname or "").lower()
        if host.endswith("wikipedia.org") or host.endswith("wikidata.org"):
            score -= 4
        if host.endswith("turkiye.gov.tr") or "giris." in host:
            score -= 3
        path = (urlparse(url).path or "/").rstrip("/") or "/"
        # Homepages / shallow hubs rarely answer concrete timed asks.
        if path == "/":
            score -= 5 if time_sensitive else 2
        elif path.count("/") <= 1 and not _FIGURE_SNIPPET_RE.search(blob):
            score -= 2 if time_sensitive else 1
        if time_sensitive and anchor is not None:
            months = _month_mentions(blob)
            if months and anchor.month not in months:
                # Explicit other-month listicles are usually stale for
                # "this weekend / today" style asks.
                score -= 6
            year_hits = {int(y) for y in re.findall(r"20\d{2}", blob)}
            if year_hits and anchor.year not in year_hits:
                score -= 3
        return score

    ranked = sorted(
        enumerate(results),
        key=lambda pair: (_score(pair[1]), -pair[0]),
        reverse=True,
    )
    out = dict(search_result)
    out["results"] = [item for _, item in ranked]
    return out


def _prefer_figure_candidates(
    search_result: dict[str, Any],
    request_text: str,
    current_date: Optional[str] = None,
) -> dict[str, Any]:
    """Alias kept for older tests."""
    return _prefer_relevant_candidates(
        search_result, request_text, current_date=current_date
    )


def evidence_has_figures(evidence: list[dict[str, Any]]) -> bool:
    """True when opened pages already contain quantitative snippets."""
    for item in evidence or []:
        if not item.get("ok"):
            continue
        blob = f"{item.get('title') or ''} {item.get('text') or ''}"
        if _FIGURE_SNIPPET_RE.search(blob):
            return True
        preview = item.get("preview")
        if preview and _FIGURE_SNIPPET_RE.search(str(preview)):
            return True
    return False


def evidence_looks_on_topic(
    evidence: list[dict[str, Any]], request_text: str
) -> bool:
    """Opened pages share the asked fact words, not only a place/entity name."""
    tokens = _ask_tokens(request_text)
    if not tokens:
        return bool(evidence)
    subject, payload = _subject_and_payload_tokens(request_text)
    need = 2 if len(tokens) >= 4 else 1
    payload_need = max(1, (len(payload) + 1) // 2) if payload else 0
    for item in evidence or []:
        if not item.get("ok"):
            continue
        blob = f"{item.get('title') or ''} {item.get('text') or ''}".casefold()
        if not blob.strip():
            continue
        hits = sum(1 for t in tokens[:16] if _token_in_text(t, blob))
        pay_hits = sum(1 for t in payload[:8] if _token_in_text(t, blob))
        entity = [t for t in subject if not _TIMEISH_TOKEN_RE.match(t)]
        entity_hits = sum(1 for t in entity[:8] if _token_in_text(t, blob))
        if payload and payload != subject:
            payload_ok = pay_hits >= payload_need
            entity_ok = (not entity) or entity_hits >= 1
            if payload_ok and entity_ok:
                return True
        elif hits >= need:
            return True
        if request_looks_like_stats(request_text) and _FIGURE_SNIPPET_RE.search(
            blob
        ):
            return True
    return False


def _url_is_hub(url: str) -> bool:
    """Bare site roots / shallow portals — rarely answer a concrete ask."""
    try:
        path = (urlparse(url or "").path or "/").rstrip("/") or "/"
    except Exception:
        return False
    if path == "/":
        return True
    return path.casefold() in {"/tr", "/en", "/home", "/index", "/tr/en"}


_EVENT_INTENT_RE = re.compile(
    r"(?is)\b(?:"
    r"etkinlik|event|müze|muze|sergi|exhibition|konser|concert|festival|"
    r"takvim|schedule|program|show|tour|bilet|ticket|listing|agenda"
    r")\b"
)
_WEATHER_PAGE_RE = re.compile(
    r"(?is)\b(?:"
    r"hava\s*durumu|weather|forecast|yağış|yagis|yağmur|yagmur|"
    r"sıcaklık|sicaklik|degree|°c|celcius|celsius"
    r")\b"
)
_TIMEISH_TOKEN_RE = re.compile(
    r"(?is)^(weekend|today|tomorrow|yesterday|hafta|haftal[ıi]k|"
    r"g[uü]n(?:ler)?|ay|week|weeks|month|son|last|latest|g[uü]ncel)$"
)
_MONTH_ONLY_RE = re.compile(
    r"(?is)^(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|ocak|şubat|subat|mart|nisan|mayıs|mayis|"
    r"haziran|temmuz|ağustos|agustos|eylül|eylul|ekim|kasım|kasim|"
    r"aralık|aralik)$"
)


def _evidence_mentions_time_window(
    evidence: list[dict[str, Any]],
    request_text: str,
    current_date: Optional[str],
) -> bool:
    """True when opened text shows the ask's concrete day — not just month/year.

    Matching bare 'Ağustos' / '2026' alone falsely accepts weather pages for
    a different weekend in the same month.
    """
    window = _time_window_query_terms(request_text, current_date)
    if not window:
        return False
    isos = re.findall(r"20\d{2}-\d{2}-\d{2}", window)
    day_months = re.findall(
        r"(?is)\b(\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December|Ocak|Şubat|Subat|Mart|Nisan|Mayıs|Mayis|"
        r"Haziran|Temmuz|Ağustos|Agustos|Eylül|Eylul|Ekim|Kasım|Kasim|"
        r"Aralık|Aralik)\b",
        window,
    )
    if not isos and not day_months:
        return False
    for item in evidence or []:
        if not item.get("ok"):
            continue
        blob = f"{item.get('title') or ''} {item.get('text') or ''}"
        blob_cf = blob.casefold()
        if any(iso in blob_cf for iso in isos):
            return True
        for day, month in day_months:
            pat = re.compile(
                rf"(?is)\b{re.escape(day)}\s*{re.escape(month)}\b|"
                rf"\b{re.escape(month)}\s*{re.escape(day)}\b"
            )
            if pat.search(blob):
                return True
    return False


def _evidence_mismatches_intent(
    evidence: list[dict[str, Any]], request_text: str
) -> bool:
    """Opened pages are a different answer type than the ask (any locale).

    Example: user asked for events/listings, opened page is only a weather
    forecast. Domain-agnostic — no city or site favourites.
    """
    if _evidence_is_speculative(evidence, request_text):
        return True
    if not _EVENT_INTENT_RE.search(request_text or ""):
        return False
    if _WEATHER_PAGE_RE.search(request_text or ""):
        return False
    for item in evidence or []:
        if not item.get("ok"):
            continue
        blob = (
            f"{item.get('title') or ''} {item.get('url') or ''} "
            f"{str(item.get('text') or '')[:800]}"
        )
        if _WEATHER_PAGE_RE.search(blob) and not _EVENT_INTENT_RE.search(blob):
            return True
    return False


def _evidence_is_speculative(
    evidence: list[dict[str, Any]], request_text: str
) -> bool:
    """Every opened page predicts the instance instead of reporting it.

    "Galatasaray son maç ilk 11" kept coming back as "muhtemel ilk 11" — a
    lineup someone guessed before kickoff. Those pages match the ask word for
    word, so token overlap alone cannot reject them. Only fires when the user
    did not ask for a prediction, and never when a page carries settled-result
    markers (a score, a past-tense outcome), so a real recap that links a
    preview in its sidebar still counts.
    """
    if not request_looks_last_occurrence(request_text):
        return False
    if _PREVIEW_OR_SCHEDULE_RE.search(request_text or ""):
        return False
    ok_items = [item for item in (evidence or []) if item.get("ok")]
    if not ok_items:
        return False
    for item in ok_items:
        head = (
            f"{item.get('title') or ''} {item.get('url') or ''} "
            f"{str(item.get('text') or '')[:800]}"
        )
        if _SETTLED_RESULT_RE.search(head):
            return False
        if not _PREVIEW_OR_SCHEDULE_RE.search(head):
            return False
    return True


def evidence_needs_refine(
    evidence: list[dict[str, Any]],
    request_text: str,
    current_date: Optional[str] = None,
) -> bool:
    """Whether another SEARCH→SELECT pass is needed (domain-agnostic).

    Token overlap alone is not enough for time-sensitive asks: a city
    homepage or same-month weather page can match place/month words without
    answering the concrete ask.
    """
    ok_items = [item for item in (evidence or []) if item.get("ok")]
    if not ok_items:
        return True
    if not evidence_looks_on_topic(ok_items, request_text):
        return True
    if _evidence_mismatches_intent(ok_items, request_text):
        return True
    if request_looks_last_occurrence(request_text) and all(
        _url_is_hub(str(item.get("url") or "")) for item in ok_items
    ):
        # A club/product homepage never pins down *which* instance was last.
        return True
    if request_looks_time_sensitive(request_text):
        if all(_url_is_hub(str(item.get("url") or "")) for item in ok_items):
            return True
        if not _evidence_mentions_time_window(
            ok_items, request_text, current_date
        ):
            return True
    return False


def _prefer_non_hub_urls(
    selected: list[str], candidates: list[str], *, limit: int
) -> list[str]:
    """If SELECT only picked site roots, swap in deeper candidates when any."""
    limit = max(1, int(limit))
    selected = [u for u in selected if u]
    if not selected:
        deeper = [u for u in candidates if not _url_is_hub(u)]
        return (deeper or candidates)[:limit]
    if not all(_url_is_hub(u) for u in selected):
        # Keep picks but drop pure hubs when mixed with deeper URLs.
        mixed = [u for u in selected if not _url_is_hub(u)]
        if mixed:
            for u in selected:
                if u not in mixed and len(mixed) < limit:
                    mixed.append(u)
            return mixed[:limit]
        return selected[:limit]
    deeper = [u for u in candidates if not _url_is_hub(u)]
    if deeper:
        return deeper[:limit]
    return selected[:limit]


def _search_item_blob(item: dict[str, Any]) -> str:
    return (
        f"{item.get('title') or ''} {item.get('content') or ''} "
        f"{item.get('snippet') or ''} {item.get('url') or ''}"
    ).casefold()


def _fact_matching_urls(
    urls: list[str],
    search_result: dict[str, Any],
    request_text: str,
) -> list[str]:
    """URLs whose title/snippet includes asked fact words, not only the entity."""
    by_url = {
        str(item.get("url") or ""): item
        for item in (search_result.get("results") or [])
        if isinstance(item, dict) and item.get("url")
    }
    matching: list[str] = []
    _, payload = _subject_and_payload_tokens(request_text)
    for url in urls:
        item = by_url.get(url)
        if not item:
            continue
        blob = _search_item_blob(item)
        if payload and any(_token_in_text(t, blob) for t in payload[:8]):
            matching.append(url)
        elif not payload:
            matching.append(url)
    return matching


def _prefer_fact_matching_urls(
    selected: list[str],
    candidates: list[str],
    search_result: dict[str, Any],
    request_text: str,
    *,
    limit: int,
) -> list[str]:
    """If SELECT picked place-only pages, swap in fact-matching candidates."""
    limit = max(1, int(limit))
    matching = _fact_matching_urls(candidates, search_result, request_text)
    if not matching:
        return (selected or candidates)[:limit]
    selected_hits = _fact_matching_urls(selected, search_result, request_text)
    if not selected_hits:
        return matching[:limit]
    out = list(selected_hits)
    for url in matching:
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out[:limit]


def _filter_selected_urls(
    selected: list[str], candidates: list[str], *, limit: int
) -> list[str]:
    allowed = {_normalize_url(url): url for url in candidates}
    picked: list[str] = []
    seen: set[str] = set()
    for raw in selected:
        url = str(raw or "").strip()
        if not url:
            continue
        key = _normalize_url(url)
        if key not in allowed or key in seen:
            continue
        seen.add(key)
        picked.append(allowed[key])
        if len(picked) >= limit:
            break
    return picked


def _evidence_item(result: dict[str, Any], url: str) -> dict[str, Any]:
    text = str(result.get("text") or "")
    if len(text) > MAX_EVIDENCE_TEXT:
        text = text[:MAX_EVIDENCE_TEXT]
    item: dict[str, Any] = {
        "url": result.get("source_url") or url,
        "title": result.get("title") or "",
        "ok": bool(result.get("ok")),
        "error_reason": result.get("error_reason") or "",
        "error": result.get("error") or "",
        "text": text,
        "preview": result.get("preview") or [],
        "columns": result.get("columns") or [],
        "row_count": int(result.get("row_count") or 0),
        "record_count": int(result.get("record_count") or 0),
        "data_path": result.get("data_path") or "",
        "tables_saved": result.get("tables_saved") or [],
    }
    return item


def handoff_from_evidence(evidence: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """First successful table/records with a data_path -- not the last page."""
    for item in evidence:
        path = str(item.get("data_path") or "").strip()
        rows = int(item.get("row_count") or item.get("record_count") or 0)
        columns = item.get("columns") or []
        if not (item.get("ok") and path and rows > 0 and columns):
            continue
        return {
            "data_path": path,
            "row_count": rows,
            "columns": list(columns),
            "source_url": item.get("url") or "",
            "title": item.get("title") or "",
            "provenance": [item.get("url")] if item.get("url") else [],
            "selected_table_index": item.get("selected_table_index"),
        }
    return None


def format_evidence_prompt(request_text: str, evidence: list[dict[str, Any]]) -> str:
    """Bounded evidence block for ANSWER. Rows stay on disk via data_path."""
    compact: list[dict[str, Any]] = []
    for item in evidence:
        entry = {
            "url": item.get("url"),
            "title": item.get("title"),
            "ok": item.get("ok"),
        }
        if item.get("error_reason"):
            entry["error_reason"] = item["error_reason"]
            entry["error"] = item.get("error") or ""
        if item.get("text"):
            entry["text"] = item["text"]
        if item.get("preview"):
            entry["preview"] = item["preview"]
        if item.get("columns"):
            entry["columns"] = item["columns"]
        if item.get("row_count"):
            entry["row_count"] = item["row_count"]
        if item.get("record_count"):
            entry["record_count"] = item["record_count"]
        # ANSWER has no artifact tools. Paths here become fake markdown
        # download links in the chat; rows stay on disk for a later worker.
        tables = item.get("tables_saved") or []
        if tables:
            entry["tables"] = [
                {
                    "caption": table.get("caption") or "",
                    "row_count": table.get("row_count"),
                }
                for table in tables
                if isinstance(table, dict)
            ]
        compact.append(entry)
    body = json.dumps(compact, ensure_ascii=False, indent=2)
    return (
        "ACQUIRED EVIDENCE (append-only, from pages that were actually opened).\n"
        "You have no web search, fetch, or artifact tools. Answer only from "
        "this evidence and the user request. Do not mention data_path, "
        "workspace paths, or write a markdown/file download link.\n\n"
        f"USER REQUEST:\n{request_text}\n\n"
        f"EVIDENCE:\n{body}"
    )


SEARCH_SYSTEM = (
    "You write ONE web search query for the information the user needs. "
    "Respond with JSON only: {\"query\": \"...\"}. Put specific names, "
    "places, and the current year into the query when the request is "
    "time-sensitive. Keep the exact fact words from the request in the "
    "query (not only the entity name). Preserve the exact competition / "
    "league / product / topic name the user wrote — e.g. 'Süper Lig' is "
    "Turkey, not Premier League; do not substitute a different topic. "
    "When the ask is the last/latest completed instance of something, prefer "
    "a recap/result page for that instance — not a fixture, preview, or "
    "upcoming schedule. "
    "When the ask needs published numbers, include year and unit words "
    "so snippets show figures. Use a site: filter only when the user "
    "named a source. "
    "Write only what the page you want is called. Leave out everything the "
    "user said ABOUT the answer rather than about the topic: how to deliver "
    "it (a file, spreadsheet, PDF, download), where to put it (save it, "
    "open it on my computer, desktop), how much to cover (1 haftalık, "
    "7-day, top 5), and what to do (bul, getir, listele, find, show, give "
    "me). A page listing prayer times is called \"ankara namaz vakitleri\", "
    "not \"ankara 1 haftalık namaz vakitlerini bul ve pdf ver\" — the extra "
    "words match travel guides instead. Keep the query short: the topic, "
    "the place or entity, and a year only when recency matters. "
    "Do not explain."
)

# First SEARCH is often too vague and returns hubs/lists. After a read, a
# second query that names what was learned (opponent, date, version, …)
# is what actually surfaces detail pages. Domain-agnostic: no favourites.
REFINE_SEARCH_SYSTEM = (
    "You write ONE NEW web search query. Respond with JSON only: "
    '{"query": "..."}. Pages were already opened; they did not fully answer '
    "the ask. Use concrete specifics from those pages (names, dates, scores, "
    "versions, IDs, titles) together with the exact fact the user asked for. "
    "When the ask needs a list/table for one event (a specific match, "
    "meeting, release, quote board, dated report), put that same event's "
    "identifying details (name/date/opponent/version) in the query so "
    "results match THAT event — not an earlier or neighboring one. "
    "If the pages so far only predicted or previewed the instance "
    "(probable/expected lineup, upcoming fixture, forecast), search for the "
    "reported outcome instead: add result/recap wording and the instance's "
    "own identifiers, and drop preview wording from the query. "
    "Prefer a different on-topic detail/article/bulletin page — not a "
    "homepage or encyclopedia overview. Do not vaguely rephrase the "
    "original request. Do not explain."
)

SELECT_SYSTEM = (
    "You choose which search results to open. Respond with JSON only: "
    '{"urls": ["..."]}. Copy each url EXACTLY from the candidate list. '
    "Pick at most 3. Prefer pages that look like they hold the asked facts "
    "or data over generic homepages, list/index/overview/category pages, "
    "directory pages, or encyclopedia overviews when the ask is concrete. "
    "Reject bare site roots (URL path only '/') when any deeper candidate "
    "exists — a corporate homepage almost never answers a dated/local ask. "
    "If the ask is about events/listings/schedules, do not pick weather/forecast-only pages. "
    "Skip a candidate whose title/snippet only repeats a place, person, or brand "
    "name but not the asked fact words — that page is a different topic. "
    "For time-sensitive asks (this weekend/today/this week), prefer "
    "candidates whose title/snippet already show a concrete date, schedule, "
    "listing, or event/item name. Prefer a specific "
    "article/report/detail/bulletin/calendar page. Prefer HTML over direct "
    ".pdf file links — PDFs often fail to fetch. When the ask needs numbers, "
    "prefer candidates whose title/snippet already show digits or totals; "
    "any reputable on-topic page is fine — do not require one official host. "
    "Match the exact competition/league/entity/topic in the user request. "
    "Do not prefer a download or file page just because the user asked for "
    "a spreadsheet or file. Inventing a URL is forbidden. Do not explain."
)

RETRY_SELECT_SYSTEM = (
    SELECT_SYSTEM
    + " Previous fetches failed technically. Prefer a different host from "
    "these new candidates. Prefer HTML pages; avoid repeating .pdf links "
    "that already failed."
)

REFINE_SELECT_SYSTEM = (
    SELECT_SYSTEM
    + " Prefer a different host than already-opened pages when those did "
    "not hold the asked facts. Detail/report/article pages beat "
    "list/hub/overview/roster pages for a concrete ask. When the ask is "
    "about something that already happened, pick the page reporting what "
    "actually happened over one predicting it."
)

ANSWER_SYSTEM = (
    "You answer the user from the acquired evidence only. Do not invent "
    "facts, URLs, or table cells that are not in the evidence. If evidence "
    "is missing, failed, or about a different topic than the user request, "
    "say clearly that the evidence does not answer this request — do not "
    "answer a different question that happens to appear in the pages. "
    "When the evidence DOES contain the asked facts or figures, quote "
    "them clearly — do not claim the facts are missing. Match the exact "
    "competition/league/"
    "entity the user named (Süper Lig ≠ Premier League). If opened pages "
    "are the wrong league or topic, "
    "say so — do not silently switch. "
    "SAME-EVENT GROUNDING: When the ask is about one concrete event "
    "(match, meeting, release, dated report, quote board) and you name "
    "that event's identity (date, opponent, venue, version, title), every "
    "detail in the answer — especially list/table rows — must come from "
    "evidence about THAT SAME event. Do not mix a result/score/context "
    "from one event with a roster/list/table from an earlier, later, or "
    "neighboring event. If evidence has the event outcome but not that "
    "event's list, say the list is missing for that event; do not fill it "
    "from another event's page. "
    "REPORTED, NOT PREDICTED: when the ask is about something that already "
    "happened, state as fact only what the evidence reports as having "
    "happened. A probable/expected/predicted lineup, an upcoming fixture, or "
    "a preview is a guess published beforehand — never present it as the "
    "actual outcome. If the evidence holds only such a preview, say the "
    "reported result was not found, and label what you do have as a "
    "prediction. "
    "LATEST INSTANCE: when the user asks for the most recent one (son, en "
    "son, latest, last) and the evidence describes several dated instances, "
    "answer with the one whose date is newest while still in the past "
    "relative to the current date — not the first one you find, not an "
    "opening/earlier round, and not a future fixture. Always state that "
    "instance's date so the choice is checkable. If the evidence does not "
    "show which instance is the most recent, say so instead of guessing. "
    "Do not invent a Kaynaklar/Sources list or domain-only citations; the "
    "system appends verified full https:// URLs of opened pages. "
    "Your reply is PROSE FOR A CHAT WINDOW, never a file's contents. "
    "Do not answer with HTML, a code fence, a script, or a document "
    "source: asked for \"html olarak ver\" a run replied with "
    "<!DOCTYPE html> and the whole stylesheet, which the user then read "
    "in the chat while the server built the actual page separately from "
    "that same text. The deliverable is produced by the server from what "
    "you write -- so write what it should SAY, not what it should be.\n"
    "Structure the reply for chat readability: start with a short title "
    "line, then 1–3 sentences of context. That title NAMES THE SUBJECT the "
    "way a report would (\"İstanbul Namaz Vakitleri, 3-9 Eylül 2026\"); it "
    "is never the user's own sentence handed back. Echoing "
    "\"istanbul namaz vakitlerini getir pdf 1 haftalik\" as a heading "
    "tells the reader nothing they did not just type. Drop the instruction "
    "words -- getir, ver, hazırla, araştır, pdf, docx -- and keep the "
    "topic. When the ask is a ranking, "
    "standings, list, or comparison with clear columns, render a GitHub-"
    "flavored Markdown table (header row + separator + data rows) — not a "
    "bulleted wall of text. If sources disagree on a cell, pick the best-"
    "supported value and note the conflict in one short sentence under the "
    "table. Write in the user's language. Do not mention data_path, "
    "workspace paths, or write markdown file links. "
    "PRIMARY SOURCES FOR LEGAL/TAX/REGULATORY FACTS: for a tax rate, "
    "incentive amount, legal threshold, or government-program detail, "
    "prefer evidence from an official/government source (a .gov domain, "
    "an official gazette, a named regulator/ministry/tax authority) over a "
    "blog, marketplace, or aggregator site. If only non-official pages were "
    "opened for such a fact, say plainly that the figure comes from a "
    "secondary/unofficial source and was not verified against an official "
    "one — do not present it as settled fact just because a page stated it "
    "confidently. "
    "CONSISTENT NUMBERS ACROSS THE WHOLE ANSWER: when evidence gives "
    "different values for the same metric at different dates, use the most "
    "recent value consistently in EVERY section and table of this answer — "
    "never let one part state a newer figure while another table states an "
    "older figure for that same thing. If you show an older figure for "
    "historical context, label its date and note the current value in one "
    "short sentence next to it. "
    "DO NOT SUBSTITUTE AN ADJACENT TOPIC: if the user asked about one "
    "specific facet (e.g. a company's sales or production volume) and the "
    "evidence lacks that specific fact, say so plainly for that facet — do "
    "not fill the gap with a related-but-different fact about the same "
    "entity (e.g. that company's unrelated service pricing) as if it "
    "answers the original ask. "
    "NAME EVERY GAP, DON'T JUST SOUND COMPLETE: if the user named several "
    "facets (e.g. sales, production, regional breakdown, challenges, "
    "outlook) and evidence could not establish one or more of them, list "
    "exactly which ones and why (not found / page failed to load / no "
    "primary source) in a short dedicated note — a polished multi-section "
    "answer must not imply full coverage while a named facet is actually "
    "missing."
)

# Survey / deep-research: decompose the ask into independent lookup angles
# before SEARCH. Domain-agnostic — no favourite sources or fields.
PLAN_SYSTEM = (
    "You plan a deep web research pass. Respond with JSON only: "
    '{"angles": ["...", "..."]}. Each angle is one short, concrete web '
    "lookup facet of the user request (comparison arm, time window, "
    "method, dataset, limitation, stakeholder view, etc.). Produce "
    f"between {MIN_PLAN_ANGLES} and {MAX_PLAN_ANGLES} angles. Do not "
    "duplicate angles. Do not write search operators unless the user "
    "named a source. Ignore delivery format (Word, PDF, file). Do not "
    "explain."
)

# User explicitly opted into deep research (not phrase-triggered): plan
# many more, narrower angles so a large fetch budget actually buys broader
# coverage instead of redundant searches for the same handful of facets.
PLAN_SYSTEM_DEEP = (
    "You plan a THOROUGH, wide-coverage web research pass -- the user "
    "explicitly asked for a deep/comprehensive investigation, not a quick "
    "pass. Respond with JSON only: "
    '{"angles": ["...", "..."]}. Each angle is one short, concrete web '
    "lookup facet of the user request (comparison arms, time windows, "
    "methods, datasets, limitations, stakeholder views, sub-topics, "
    "regional/temporal breakdowns, counter-arguments, primary vs "
    "secondary sources, etc.). Cover the request from many independent "
    f"angles. Produce between {MIN_PLAN_ANGLES_DEEP} and "
    f"{MAX_PLAN_ANGLES_DEEP} angles -- err toward more, narrower angles "
    "over few broad ones. Do not duplicate angles. Do not write search "
    "operators unless the user named a source. Ignore delivery format "
    "(Word, PDF, file). Do not explain."
)

COVERAGE_SYSTEM = (
    "You judge whether the opened evidence is enough to answer the user "
    "request as a survey/synthesis. Respond with JSON only: "
    '{"enough": true|false, "missing": ["..."]}. Set enough=true only if '
    "the evidence covers the main facets of the ask. If not, list up to "
    "3 short missing lookup angles (not full essays). Missing angles must "
    "be new facets, not a vague restatement of the whole request. Do not "
    "explain."
)

SURVEY_ANSWER_SYSTEM = (
    "You write a structured research brief from the acquired evidence "
    "only. Do not invent facts, URLs, numbers, or citations that are not "
    "in the evidence. "
    "COVER EVERY PART OF THE ASK: first list the distinct things the user "
    "asked for — a request like 'the state of X, its advantages, and the "
    "barriers to Y' names three — then give each one its own section, in "
    "the order asked. Answering one part well and dropping the rest is a "
    "failed brief, even when that one part is thorough. If the evidence "
    "does not cover a part, still write its heading and say plainly what "
    "is missing; never silently omit a section. Only then may you add "
    "further sections (overview, comparison, limitations) if they help. "
    "When a section is tabular (rankings, metrics, comparisons), use a "
    "GitHub-flavored Markdown table. "
    "Keep each event's facts together: do not attach one event's list or "
    "table rows to another event's date/result. "
    "Do NOT write a Kaynaklar / Sources section or domain-only citations — "
    "the system appends verified full https:// URLs of opened pages. "
    "If evidence is thin or off-topic, say so clearly "
    "instead of filling gaps. Write in the user's language. Do not "
    "mention data_path, workspace paths, or write markdown file links."
)


def _date_prefix(current_date: Optional[str]) -> str:
    iso = (current_date or "").strip()
    if not iso:
        return ""
    try:
        year = int(iso[:4])
    except ValueError:
        year = 0
    prior = f"{year - 2}-{year - 1}" if year else "a previous season"
    prior_year = str(year - 1) if year else "a previous year"
    return (
        f"CURRENT DATE: {iso} ({year or iso}). For latest/current/son "
        "requests, put this year/season in the query. Prefer candidate "
        "URLs/titles from this year when the ask is latest/current. Do not "
        f"use a previous season (never {prior} or {prior_year} when today "
        f"is {year or iso}).\n\n"
    )


def _with_date(current_date: Optional[str], body: str) -> str:
    return f"{_date_prefix(current_date)}{body}"


def _year_only_date_prefix(current_date: Optional[str]) -> str:
    """Same steer as ``_date_prefix`` but never spells out today's exact day.

    SEARCH/REFINE only need the year ("put this year/season in the query");
    handing the model the literal day-of-month (e.g. "2026-08-28") invites a
    weak model to copy that bare "28" into the query verbatim, where it
    collides with any unrelated page that happens to contain that number
    (a bus route, a jersey number, an article ID) — see the "28 Ağustos"
    query dragging in İETT bus-schedule results for a currency ask.
    """
    iso = (current_date or "").strip()
    if not iso:
        return ""
    try:
        year = int(iso[:4])
    except ValueError:
        year = 0
    prior = f"{year - 2}-{year - 1}" if year else "a previous season"
    prior_year = str(year - 1) if year else "a previous year"
    return (
        f"CURRENT YEAR: {year or iso}. For latest/current/son requests, put "
        "this year/season in the query — never a specific day/date number. "
        "Prefer candidate URLs/titles from this year when the ask is "
        f"latest/current. Do not use a previous season (never {prior} or "
        f"{prior_year} when the current year is {year or iso}).\n\n"
    )


def _with_year(current_date: Optional[str], body: str) -> str:
    return f"{_year_only_date_prefix(current_date)}{body}"


def _strip_stray_anchor_day(
    query: str, request_text: str, current_date: Optional[str]
) -> str:
    """Drop today's bare day-of-month from a model-written query.

    Defense in depth for ``_with_year`` above: if the day number was never
    in the user's own ask, it has no business in the search string either —
    it is calendar noise a keyword engine will happily match against any
    unrelated page carrying that same number.
    """
    anchor = _parse_anchor_date(current_date)
    if anchor is None:
        return query
    day = str(anchor.day)
    if day not in (request_text or ""):
        query = re.sub(rf"(?<!\d){day}(?!\d)", "", query)
        query = re.sub(r"\s+", " ", query).strip()
    return query


def _gated_handoff(
    evidence: list[dict[str, Any]], *, wants_artifact: bool
) -> Optional[dict[str, Any]]:
    if not wants_artifact:
        return None
    return handoff_from_evidence(evidence)


def _emit(progress: Optional[ProgressFn], stage: str, detail: str = "") -> None:
    if progress is None:
        return
    try:
        progress(stage, detail)
    except Exception:
        pass


def _report_timing(
    result: ControllerResult, name: str, seconds: float, detail: str = ""
) -> None:
    if result.on_timing is None:
        return
    try:
        result.on_timing(name, seconds, detail)
    except Exception:  # noqa: BLE001 - telemetry must never fail a research phase
        pass


async def _complete(
    complete: CompleteFn,
    phase: str,
    system: str,
    user: str,
    schema: Optional[type],
) -> Any:
    return await complete(phase, system, user, schema)


def _query_from_complete(parsed: Any, fallback: str) -> str:
    if isinstance(parsed, SearchQuery):
        query = (parsed.query or "").strip()
        return query or fallback
    if isinstance(parsed, dict):
        query = str(parsed.get("query") or "").strip()
        return query or fallback
    if isinstance(parsed, str) and parsed.strip():
        return parsed.strip()
    return fallback


def _urls_from_complete(parsed: Any) -> list[str]:
    if isinstance(parsed, UrlSelection):
        return list(parsed.urls or [])
    if isinstance(parsed, dict):
        urls = parsed.get("urls") or []
        if isinstance(urls, list):
            return [str(item) for item in urls]
    return []


def _angles_from_complete(
    parsed: Any, *, max_angles: int = MAX_PLAN_ANGLES
) -> list[str]:
    raw: list[Any]
    if isinstance(parsed, ResearchPlan):
        raw = list(parsed.angles or [])
    elif isinstance(parsed, dict):
        raw = list(parsed.get("angles") or [])
    else:
        raw = []
    return _target_names([str(item) for item in raw])[:max_angles]


def _coverage_from_complete(parsed: Any) -> CoverageAssessment:
    if isinstance(parsed, CoverageAssessment):
        missing = _target_names([str(item) for item in (parsed.missing or [])])
        return CoverageAssessment(enough=bool(parsed.enough), missing=missing[:3])
    if isinstance(parsed, dict):
        missing = _target_names(
            [str(item) for item in (parsed.get("missing") or [])]
        )
        return CoverageAssessment(
            enough=bool(parsed.get("enough")), missing=missing[:3]
        )
    return CoverageAssessment(enough=False, missing=[])


def _format_candidates(search_result: dict[str, Any]) -> str:
    lines = []
    for item in search_result.get("results") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        snippet = str(item.get("snippet") or item.get("content") or "")
        if len(snippet) > MAX_CANDIDATE_SNIPPET:
            snippet = snippet[:MAX_CANDIDATE_SNIPPET]
        lines.append(
            f"- url: {item.get('url')}\n"
            f"  title: {item.get('title') or ''}\n"
            f"  snippet: {snippet}"
        )
    return "\n".join(lines) if lines else "(no candidates)"


def _target_names(targets: Optional[list[str]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in targets or []:
        name = " ".join(str(item or "").split())
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _evidence_learning_block(evidence: list[dict[str, Any]]) -> str:
    """Short excerpts so SEARCH can refine — capped to limit token burn."""
    lines: list[str] = []
    for item in (evidence or [])[-4:]:
        if not item.get("ok"):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        text = str(item.get("text") or "").strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:280] + "…"
        lines.append(f"- title: {title}\n  url: {url}\n  excerpt: {text}")
    return "\n".join(lines) if lines else "(none)"


def _opened_url_keys(evidence: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for item in evidence or []:
        url = str(item.get("url") or "").strip()
        if url:
            keys.add(_normalize_url(url))
    return keys


def _drop_opened_candidates(
    search_result: dict[str, Any], opened: set[str]
) -> dict[str, Any]:
    """Hide already-opened URLs so SELECT cannot re-pick hubs."""
    if not opened:
        return search_result
    kept = []
    for item in search_result.get("results") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        if _normalize_url(str(item["url"])) in opened:
            continue
        kept.append(item)
    if not kept:
        return search_result
    out = dict(search_result)
    out["results"] = kept
    return out


def _opened_hosts(evidence: list[dict[str, Any]]) -> set[str]:
    hosts: set[str] = set()
    for item in evidence or []:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        host = urlparse(url).netloc.casefold()
        if host:
            hosts.add(host)
    return hosts


def _prefer_untried_hosts(
    search_result: dict[str, Any], tried_hosts: set[str]
) -> dict[str, Any]:
    """Stable reorder: hosts not yet opened first (no domain favourites)."""
    if not tried_hosts:
        return search_result
    fresh: list[dict[str, Any]] = []
    repeats: list[dict[str, Any]] = []
    for item in search_result.get("results") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        host = urlparse(str(item["url"])).netloc.casefold()
        if host and host in tried_hosts:
            repeats.append(item)
        else:
            fresh.append(item)
    if not fresh:
        return search_result
    out = dict(search_result)
    out["results"] = fresh + repeats
    return out


def _search_user_payload(
    request_text: str,
    *,
    target: str = "",
    retry: bool = False,
    refine: bool = False,
    prior_evidence: Optional[list[dict[str, Any]]] = None,
) -> str:
    parts = [f"USER REQUEST:\n{request_text}"]
    if target:
        parts.append(
            f"THIS LOOKUP TARGET:\n{target}\n"
            "Write a query for THIS target only."
        )
    if refine:
        parts.append(
            "LEARNED FROM PAGES ALREADY OPENED (put these specifics into "
            "the new query with the exact fact asked):\n"
            f"{_evidence_learning_block(prior_evidence or [])}"
        )
    elif retry:
        parts.append(
            "Previous fetches returned no usable content. Write a "
            "different query, preferably another host or more specific "
            "terms."
        )
    return "\n\n".join(parts)


# Tie-break weight for the heuristic order (PDF/homepage/recency penalties)
# on top of the reranker's [0, 1] relevance.
RERANK_HEURISTIC_WEIGHT = 0.05
RERANK_QUERY_REQUEST_CHARS = 240


async def _rerank_select(
    search_result: dict[str, Any],
    request_text: str,
    *,
    target: str,
    limit: int,
) -> Optional[list[str]]:
    """Cross-encoder SELECT; None means use the LLM SELECT instead."""
    if not reranker.enabled():
        return None
    entries = [
        item
        for item in (search_result.get("results") or [])
        if isinstance(item, dict) and item.get("url")
    ]
    if not entries:
        return None
    ask = " ".join(_SEARCH_HINT_BLOCK_RE.sub(" ", request_text or "").split())
    focus = " ".join((target or "").split())
    query = (
        f"{focus} {ask[:RERANK_QUERY_REQUEST_CHARS]}".strip() if focus else ask
    )
    if not query:
        return None
    passages = [
        f"{item.get('title') or ''}\n"
        f"{str(item.get('snippet') or item.get('content') or '')[:MAX_CANDIDATE_SNIPPET]}"
        for item in entries
    ]
    scores = await asyncio.to_thread(reranker.score, query, passages)
    if scores is None or len(scores) != len(entries):
        return None
    count = len(entries)
    order = sorted(
        range(count),
        key=lambda i: scores[i] + RERANK_HEURISTIC_WEIGHT * (count - i) / count,
        reverse=True,
    )
    return [str(entries[i]["url"]) for i in order[: max(1, limit)]]


async def _search_select_fetch(
    result: ControllerResult,
    request_text: str,
    *,
    search: SearchFn,
    fetch: FetchFn,
    complete: CompleteFn,
    progress: Optional[ProgressFn],
    select_limit: int,
    current_date: Optional[str],
    target: str = "",
    retry: bool = False,
    refine: bool = False,
    max_fetches: int = MAX_FETCHES,
) -> None:
    seed_source = _SEARCH_HINT_BLOCK_RE.sub(
        " ", (target or request_text) or ""
    ).strip()
    # Prefer the fact-word-only seed over the raw sentence when the SEARCH
    # call itself fails/returns empty (same path as `seed` below, just not
    # gated on first-turn/no-target -- this fallback needs a clean query
    # every time it's reached, not only then). Verified live: the raw
    # sentence "Deniz kaplumbağaları hakkında görselli bir powerpoint
    # sunumu hazırla" ranked DenizBank's homepage #1 on SearXNG (its "bir
    # powerpoint sunumu hazırla" tail apparently matches well against a
    # high-authority corporate site); the fact-word seed "Deniz
    # kaplumbağaları görselli sunumu" still isn't perfect but drops
    # DenizBank below every genuine sea-turtle result, and the existing
    # SELECT-phase relevance check (skip name/brand-only candidates) then
    # reliably avoids it -- both verified live, 5/5 trials.
    fallback_query = fact_seed_query(seed_source, current_date) or (
        " ".join(seed_source.split())[:240]
    )
    searches_before = result.search_count
    fetches_before = result.fetch_count
    success_before = result.successful_fetches
    query = ""
    selected: list[str] = []
    candidate_entries: list[dict[str, str]] = []
    fetch_error_reason = ""
    prior = list(result.evidence)
    try:
        # The model writes the query; it reads the sentence, so it knows
        # "bilgisayarımda aç" says where the file goes and "1 haftalık" says
        # how much to cover — neither names a page. The deterministic seed
        # below cannot know that without a pattern for every phrasing, and
        # one leaked verb was enough to return tourist guides for a prayer
        # timetable. It stays as the fallback for when the call fails.
        seed = None
        if not retry and not refine and not (target or "").strip():
            seed = fact_seed_query(request_text, current_date)
        query = ""
        search_system = REFINE_SEARCH_SYSTEM if refine else SEARCH_SYSTEM
        try:
            parsed = await _complete(
                complete,
                "search",
                search_system,
                _with_year(
                    current_date,
                    _search_user_payload(
                        request_text,
                        target=target,
                        retry=retry and not refine,
                        refine=refine,
                        prior_evidence=prior if refine else None,
                    ),
                ),
                SearchQuery,
            )
            query = _query_from_complete(parsed, "").strip()
        except Exception:  # noqa: BLE001 - fall back, never fail the phase
            query = ""
        if query:
            query = _strip_stray_anchor_day(query, request_text, current_date)
        else:
            query = seed or fallback_query
            _emit(progress, "searching", f"fact seed: {query[:120]}")
        search_result = search(query, 10)
        result.search_count += 1
        if refine or retry:
            search_result = _drop_opened_candidates(
                search_result, _opened_url_keys(prior)
            )
        if refine:
            search_result = _prefer_untried_hosts(
                search_result, _opened_hosts(prior)
            )
        search_result = _prefer_relevant_candidates(
            search_result, request_text, current_date=current_date
        )
        candidates = _candidate_urls(search_result)
        candidate_entries = _candidate_entries(search_result)
        if not candidates:
            return
        rerank_started = time.monotonic()
        picked = await _rerank_select(
            search_result, request_text, target=target, limit=select_limit
        )
        if picked is not None:
            _emit(progress, "selecting", "reranker")
            _report_timing(
                result,
                "rerank",
                time.monotonic() - rerank_started,
                f"{len(candidates)} candidates, picked {len(picked)}",
            )
        else:
            if refine:
                select_system = REFINE_SELECT_SYSTEM
            elif retry:
                select_system = RETRY_SELECT_SYSTEM
            else:
                select_system = SELECT_SYSTEM
            parsed_select = await _complete(
                complete,
                "select",
                select_system,
                _with_date(
                    current_date,
                    f"USER REQUEST:\n{request_text}\n"
                    + (f"\nTHIS LOOKUP TARGET:\n{target}\n" if target else "")
                    + f"\nCANDIDATES:\n{_format_candidates(search_result)}",
                ),
                UrlSelection,
            )
            picked = _urls_from_complete(parsed_select)
        selected = _filter_selected_urls(
            picked,
            candidates,
            limit=select_limit,
        )
        selected = _prefer_non_hub_urls(
            selected, candidates, limit=select_limit
        )
        selected = _prefer_fact_matching_urls(
            selected,
            candidates,
            search_result,
            request_text,
            limit=select_limit,
        )
        # SELECT sometimes returns [] even when candidates look right —
        # still open the top hits so Research has pages + citation URLs.
        # Prefer HTML over .pdf (gov PDFs often fail behind anti-bot).
        # Prefer fact-matching HTML so a city listing is not used as filler.
        html_first = [url for url in candidates if not _is_pdf_url(url)]
        fact_html = _fact_matching_urls(
            html_first, search_result, request_text
        )
        pool = fact_html or html_first or candidates
        if not selected:
            selected = pool[: min(2, select_limit)]
        elif html_first:
            # Never open a PDF-only set when HTML candidates exist — gov/trade
            # PDFs routinely time out and leave Research with zero pages.
            selected = [
                url for url in selected if not _is_pdf_url(url)
            ] or pool[: min(2, select_limit)]
            if len(selected) < min(2, select_limit):
                for url in pool:
                    if url not in selected:
                        selected.append(url)
                    if len(selected) >= min(2, select_limit):
                        break
        evidence_before = len(result.evidence)
        remaining = max_fetches - result.fetch_count
        before = result.successful_fetches
        await _fetch_urls(
            result,
            selected[:remaining],
            fetch=fetch,
            progress=progress,
            max_fetches=max_fetches,
        )
        if target and result.successful_fetches > before:
            result.covered_targets.append(target)
        fetch_error_reason = ""
        for item in result.evidence[evidence_before:]:
            if item.get("ok") and is_successful_fetch(item):
                continue
            reason = str(item.get("error_reason") or "").strip()
            if reason:
                fetch_error_reason = reason
    finally:
        key = target or "(unscoped)"
        stats = result.target_stats.setdefault(
            key, {"search": 0, "fetch": 0, "success": 0}
        )
        stats["search"] += result.search_count - searches_before
        stats["fetch"] += result.fetch_count - fetches_before
        stats["success"] += result.successful_fetches - success_before
        if query:
            stats["last_query"] = query
        if selected:
            stats["selected_urls"] = list(selected)
        if candidate_entries:
            # Search hits for recovery routing (not final Kaynaklar citations).
            stats["candidate_urls"] = list(candidate_entries[:8])
        if fetch_error_reason:
            stats["fetch_error_reason"] = fetch_error_reason
        elif int(stats.get("success") or 0) > 0:
            stats.pop("fetch_error_reason", None)


def _lookup_budgets(
    *,
    wants_artifact: bool,
    compact: bool,
    select_limit: int,
) -> tuple[int, int, int, bool]:
    """Return (search_cap, fetch_cap, select_limit, allow_retry_loop)."""
    if compact:
        return (
            MAX_SEARCH_PHASES_COMPACT,
            MAX_FETCHES_COMPACT,
            min(select_limit, SELECT_LIMIT_COMPACT),
            False,
        )
    if wants_artifact:
        return (
            MAX_SEARCH_PHASES_ARTIFACT,
            MAX_FETCHES_ARTIFACT,
            min(select_limit, SELECT_LIMIT_ARTIFACT),
            True,
        )
    return (MAX_SEARCH_PHASES, MAX_FETCHES, select_limit, True)


async def acquire_web_evidence(
    request_text: str,
    *,
    search: SearchFn,
    fetch: FetchFn,
    complete: CompleteFn,
    progress: Optional[ProgressFn] = None,
    select_limit: int = SELECT_LIMIT,
    current_date: Optional[str] = None,
    wants_artifact: bool = False,
    targets: Optional[list[str]] = None,
    allow_refine: bool = True,
    depth: str = "lookup",
    compact: bool = False,
    on_timing: Optional[Callable[[str, float, str], None]] = None,
) -> ControllerResult:
    """Run SEARCH → SELECT → FETCH. ANSWER is a separate call.

    Literal URLs in the request skip SEARCH and SELECT.
    ``depth="lookup"`` (default): technical retry when
    ``successful_fetches == 0``; when pages opened but may be hubs/lists,
    one refine SEARCH uses learned specifics (search → read → refine →
    read). Cap is ``MAX_SEARCH_PHASES``. Multiple named targets each get
    a SEARCH until covered or the shared search/fetch budget is spent --
    the first successful page does not cancel the remaining targets.
    ``depth="survey"``: plan angles → search/read each → coverage check
    (plan → search → read → enough? → missing angles → synthesize later).
    Caps are ``MAX_SEARCH_PHASES_SURVEY`` / ``MAX_FETCHES_SURVEY``.
    ``depth="deep"``: same shape as survey, but the user explicitly opted
    into a thorough pass (never phrase-triggered): more/narrower planned
    angles, and caps raised to ``MAX_SEARCH_PHASES_DEEP`` /
    ``MAX_FETCHES_DEEP``, with the coverage gap-fill repeating instead of
    running once.
    ``compact=True``: one search + two fetches (recovery / second pass).
    ``data_path`` alone does not produce a handoff; ``wants_artifact`` must
    also be true.
    """
    result = ControllerResult(on_timing=on_timing)
    search_cap, fetch_cap, select_limit, allow_retry_loop = _lookup_budgets(
        wants_artifact=wants_artifact,
        compact=compact,
        select_limit=select_limit,
    )
    literal = web_fetch.find_urls(request_text)
    if literal:
        result.skipped_search = True
        await _fetch_urls(
            result,
            literal[:select_limit],
            fetch=fetch,
            progress=progress,
            max_fetches=fetch_cap,
        )
        result.handoff = _gated_handoff(
            result.evidence, wants_artifact=wants_artifact
        )
        return result

    depth_normalized = (depth or "").strip().casefold()
    if depth_normalized in ("survey", "deep") and not compact:
        return await _acquire_survey_evidence(
            result,
            request_text,
            search=search,
            fetch=fetch,
            complete=complete,
            progress=progress,
            current_date=current_date,
            wants_artifact=wants_artifact,
            deep=(depth_normalized == "deep"),
        )

    names = _target_names(targets)
    if len(names) > 1 and not compact:
        # One SEARCH pass per target, plus room for a single retry when a
        # target fetch technically fails (same shape as single-target retry).
        # Artifact caps must not drop later named subjects (e.g. 3 countries
        # with MAX_SEARCH_PHASES_ARTIFACT=2) — floor at len(names). Fetch
        # floor allows at least one opened page per target under select_limit.
        search_budget = min(
            MAX_SEARCH_PHASES_MULTI,
            max(len(names), search_cap),
        )
        fetch_budget = min(
            MAX_FETCHES,
            max(len(names) * max(1, min(select_limit, 2)), fetch_cap),
        )
        for name in names:
            if (
                result.search_count >= search_budget
                or result.fetch_count >= fetch_budget
            ):
                break
            await _search_select_fetch(
                result,
                request_text,
                search=search,
                fetch=fetch,
                complete=complete,
                progress=progress,
                select_limit=select_limit,
                current_date=current_date,
                target=name,
                max_fetches=fetch_budget,
            )
            stats = result.target_stats.get(name) or {}
            if (
                int(stats.get("success") or 0) == 0
                and result.search_count < search_budget
                and result.fetch_count < fetch_budget
            ):
                await _search_select_fetch(
                    result,
                    request_text,
                    search=search,
                    fetch=fetch,
                    complete=complete,
                    progress=progress,
                    select_limit=select_limit,
                    current_date=current_date,
                    target=name,
                    retry=True,
                    max_fetches=fetch_budget,
                )
        result.handoff = _gated_handoff(
            result.evidence, wants_artifact=wants_artifact
        )
        return result

    focus = names[0] if names else ""
    # Pass 1: opening query (often vague / hub results).
    await _search_select_fetch(
        result,
        request_text,
        search=search,
        fetch=fetch,
        complete=complete,
        progress=progress,
        select_limit=select_limit,
        current_date=current_date,
        target=focus,
        max_fetches=fetch_cap,
    )
    opened_ok = result.successful_fetches > 0
    # Technical retries while nothing usable was fetched.
    while (
        allow_retry_loop
        and result.successful_fetches == 0
        and result.search_count < search_cap
        and result.fetch_count < fetch_cap
    ):
        await _search_select_fetch(
            result,
            request_text,
            search=search,
            fetch=fetch,
            complete=complete,
            progress=progress,
            select_limit=select_limit,
            current_date=current_date,
            target=focus,
            retry=True,
            max_fetches=fetch_cap,
        )
    # Refine: one extra SEARCH→SELECT→FETCH when the opened pages are hubs,
    # off-topic, or lack the asked substance — any domain, within the shared
    # search/fetch budget (keeps PDF jobs from wandering).
    #
    # The gate used to be `not wants_artifact or needs_topic_refine`, which
    # for a chat ask (wants_artifact False) is True before the detector is
    # ever consulted -- so EVERY chat research paid a second round even when
    # the first one already held the answer. Measured on a live "gold price
    # in Turkey" run: 60.4s total, of which the unconditional refine round
    # (SEARCH + SELECT + FETCH) was ~23s, and evidence_needs_refine returns
    # False for that same evidence -- i.e. the round was pure latency.
    #
    # evidence_needs_refine is exactly the hub→detail detector the chat path
    # wanted (it already rejects hub-only, off-topic, intent-mismatched and
    # stale-window evidence), so binding both paths to it keeps the
    # hub→detail behavior the chat branch existed for and drops only the
    # rounds that had nothing to find. See
    # test_successful_fetch_allows_one_refine_search for that contract.
    needs_topic_refine = opened_ok and evidence_needs_refine(
        result.evidence, request_text, current_date=current_date
    )
    if (
        allow_refine
        and not compact
        and opened_ok
        and result.search_count < search_cap
        and result.fetch_count < fetch_cap
        and needs_topic_refine
    ):
        _emit(progress, "searching", "refine with learned specifics")
        await _search_select_fetch(
            result,
            request_text,
            search=search,
            fetch=fetch,
            complete=complete,
            progress=progress,
            select_limit=select_limit,
            current_date=current_date,
            target=focus,
            refine=True,
            max_fetches=fetch_cap,
        )
    result.handoff = _gated_handoff(
        result.evidence, wants_artifact=wants_artifact
    )
    return result


async def _plan_research_angles(
    request_text: str,
    *,
    complete: CompleteFn,
    current_date: Optional[str],
    deep: bool = False,
) -> list[str]:
    min_angles = MIN_PLAN_ANGLES_DEEP if deep else MIN_PLAN_ANGLES
    max_angles = MAX_PLAN_ANGLES_DEEP if deep else MAX_PLAN_ANGLES
    parsed = await _complete(
        complete,
        "plan",
        PLAN_SYSTEM_DEEP if deep else PLAN_SYSTEM,
        _with_date(current_date, f"USER REQUEST:\n{request_text}"),
        ResearchPlan,
    )
    angles = _angles_from_complete(parsed, max_angles=max_angles)
    if len(angles) >= min_angles:
        return angles[:max_angles]
    # Thin/failed plan: still run at least one angle from the request.
    fallback = " ".join((request_text or "").split())[:160]
    if fallback and fallback.casefold() not in {a.casefold() for a in angles}:
        angles.append(fallback)
    return angles[:max_angles] or ([fallback] if fallback else [])


async def _assess_survey_coverage(
    request_text: str,
    evidence: list[dict[str, Any]],
    *,
    complete: CompleteFn,
    current_date: Optional[str],
    covered_angles: list[str],
) -> CoverageAssessment:
    covered = "\n".join(f"- {name}" for name in covered_angles) or "(none)"
    excerpts = _evidence_learning_block(evidence)
    parsed = await _complete(
        complete,
        "coverage",
        COVERAGE_SYSTEM,
        _with_date(
            current_date,
            f"USER REQUEST:\n{request_text}\n\n"
            f"ANGLES ALREADY SEARCHED:\n{covered}\n\n"
            f"EVIDENCE EXCERPTS:\n{excerpts}",
        ),
        CoverageAssessment,
    )
    return _coverage_from_complete(parsed)


async def _acquire_survey_evidence(
    result: ControllerResult,
    request_text: str,
    *,
    search: SearchFn,
    fetch: FetchFn,
    complete: CompleteFn,
    progress: Optional[ProgressFn],
    current_date: Optional[str],
    wants_artifact: bool,
    deep: bool = False,
) -> ControllerResult:
    """Plan → search/read per angle → optional coverage gap-fill.

    Keeps multi-angle survey quality but avoids a coverage LLM call after
    every angle (that ballooned runs to 10–12+ minutes).

    ``deep=True`` is the user's explicit opt-in (never phrase-triggered):
    MAX_*_DEEP budgets, more/narrower planned angles, and the coverage
    gap-fill repeats (bounded by the same budget) instead of running once —
    there is enough headroom that "good enough after one pass" is the
    wrong stopping rule. This overrides the wants_artifact compact branch
    on purpose: an explicit thoroughness request should not be quietly
    capped just because the deliverable is a file.
    """
    if deep:
        search_budget = MAX_SEARCH_PHASES_DEEP
        fetch_budget = MAX_FETCHES_DEEP
        select_limit = SELECT_LIMIT_DEEP
        max_angles = MAX_PLAN_ANGLES_DEEP
        allow_coverage_expand = True
        gap_fill_limit = 4
    elif wants_artifact:
        # Report deliverables: compact survey, no gap-fill expand.
        search_budget = min(2, MAX_SEARCH_PHASES_SURVEY)
        fetch_budget = min(4, MAX_FETCHES_SURVEY)
        select_limit = SELECT_LIMIT_SURVEY
        max_angles = 2
        allow_coverage_expand = False
        gap_fill_limit = 2
    else:
        search_budget = MAX_SEARCH_PHASES_SURVEY
        fetch_budget = MAX_FETCHES_SURVEY
        select_limit = SELECT_LIMIT_SURVEY
        max_angles = MAX_PLAN_ANGLES
        allow_coverage_expand = True
        gap_fill_limit = 2

    _emit(progress, "searching", "survey: plan research angles")
    queue = await _plan_research_angles(
        request_text, complete=complete, current_date=current_date, deep=deep
    )
    queue = queue[:max_angles]
    seen = {name.casefold() for name in queue}
    covered: list[str] = []

    async def _run_angle(angle: str, *, retry: bool = False) -> None:
        await _search_select_fetch(
            result,
            request_text,
            search=search,
            fetch=fetch,
            complete=complete,
            progress=progress,
            select_limit=select_limit,
            current_date=current_date,
            target=angle,
            retry=retry,
            max_fetches=fetch_budget,
        )

    while (
        queue
        and result.search_count < search_budget
        and result.fetch_count < fetch_budget
    ):
        angle = queue.pop(0)
        _emit(progress, "searching", f"survey angle: {angle[:120]}")
        await _run_angle(angle)
        covered.append(angle)
        stats = result.target_stats.get(angle) or {}
        # Retry a dead angle only when we still have no usable pages at all.
        if (
            int(stats.get("success") or 0) == 0
            and result.successful_fetches == 0
            and result.search_count < search_budget
            and result.fetch_count < fetch_budget
        ):
            await _run_angle(angle, retry=True)

    # Coverage check(s): fill up to gap_fill_limit gaps per round. Deep mode
    # repeats this (plenty of budget left over after the angle pass makes
    # "good enough after one look" the wrong stopping rule); everything
    # else keeps the original single-pass behavior.
    coverage_rounds = 0
    max_coverage_rounds = 10  # safety cap: search/fetch budgets should bind first
    while (
        allow_coverage_expand
        and result.successful_fetches > 0
        and result.search_count < search_budget
        and result.fetch_count < fetch_budget
        and coverage_rounds < max_coverage_rounds
    ):
        coverage_rounds += 1
        assessment = await _assess_survey_coverage(
            request_text,
            result.evidence,
            complete=complete,
            current_date=current_date,
            covered_angles=covered,
        )
        if assessment.enough:
            _emit(progress, "searching", "survey: coverage enough")
            break
        extra: list[str] = []
        for miss in assessment.missing:
            key = miss.casefold()
            if key in seen:
                continue
            seen.add(key)
            extra.append(miss)
            if len(extra) >= gap_fill_limit:
                break
        if not extra:
            # Nothing new to chase (all "missing" angles already covered/seen).
            break
        for angle in extra:
            if (
                result.search_count >= search_budget
                or result.fetch_count >= fetch_budget
            ):
                break
            _emit(progress, "searching", f"survey gap: {angle[:120]}")
            await _run_angle(angle)
            covered.append(angle)
        if not deep:
            break

    result.handoff = _gated_handoff(
        result.evidence, wants_artifact=wants_artifact
    )
    return result


async def _fetch_urls(
    result: ControllerResult,
    urls: list[str],
    *,
    fetch: FetchFn,
    progress: Optional[ProgressFn],
    max_fetches: int = MAX_FETCHES,
) -> None:
    opened = {
        _normalize_url(str(item.get("url") or ""))
        for item in result.evidence
        if item.get("url")
    }
    to_open: list[str] = []
    pending: set[str] = set()
    for url in urls:
        if result.fetch_count + len(to_open) >= max_fetches:
            break
        key = _normalize_url(url)
        if key in opened or key in pending:
            continue
        pending.add(key)
        to_open.append(url)
    if not to_open:
        return
    for url in to_open:
        _emit(progress, "opening", url)

    async def _one(url: str) -> tuple[str, dict[str, Any]]:
        try:
            page = await asyncio.wait_for(
                asyncio.to_thread(fetch, url), timeout=FETCH_WALL_CLOCK_SECONDS
            )
        except asyncio.TimeoutError:
            # The engine's own timeout governs the request it makes; it
            # cannot end a call that never returns at all. One site holding
            # the connection open kept a whole run in "opening" for five
            # minutes with nothing in the log -- the browser had not
            # crashed, so the crash-recovery path never saw it either.
            # A page that does not answer is simply a page that failed;
            # the run already knows how to carry on without one.
            return url, {
                "ok": False,
                "error_reason": "fetch_timeout",
                "error": (
                    f"Page did not respond within "
                    f"{FETCH_WALL_CLOCK_SECONDS:.0f}s and was skipped."
                ),
            }
        return url, page if isinstance(page, dict) else {"ok": False, "error": str(page)}

    # Parallel open: 2–3 pages at ~30s each used to serialize into minutes.
    fetched = await _gather_until_enough([_one(url) for url in to_open], to_open)
    for url, page in fetched:
        result.fetch_count += 1
        item = _evidence_item(page, url)
        result.evidence.append(item)
        opened.add(_normalize_url(str(item.get("url") or url)))
        if is_successful_fetch(page):
            result.successful_fetches += 1


async def _gather_until_enough(
    coros: list[Awaitable[tuple[str, dict[str, Any]]]],
    urls: list[str],
) -> list[tuple[str, dict[str, Any]]]:
    """Like gather, but stop waiting on stragglers once pages have landed.

    A plain gather held the whole round until its slowest page finished --
    one site stalling to the engine's timeout kept two pages that had
    already loaded waiting with it. After the first usable page the rest get
    FETCH_STRAGGLER_GRACE_SECONDS; once FETCH_ENOUGH_SUCCESSES pages are in,
    only FETCH_SETTLE_GRACE_SECONDS. A page still loading after that is
    recorded as skipped (not ok), so it counts against the fetch budget,
    is never re-selected, and is never cited. Results keep ``urls`` order.
    """
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    done_pages: dict[int, tuple[str, dict[str, Any]]] = {}
    pending = set(tasks)
    successes = 0
    deadline: Optional[float] = None
    while pending:
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        finished, pending = await asyncio.wait(
            pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not finished:
            break
        for task in finished:
            index = tasks.index(task)
            done_pages[index] = task.result()
            if is_successful_fetch(done_pages[index][1]):
                successes += 1
        now = time.monotonic()
        if successes >= FETCH_ENOUGH_SUCCESSES:
            settle = now + FETCH_SETTLE_GRACE_SECONDS
            deadline = settle if deadline is None else min(deadline, settle)
        elif successes and deadline is None:
            deadline = now + FETCH_STRAGGLER_GRACE_SECONDS
    for task in pending:
        # The page open itself runs on a worker thread and ends on its own
        # engine timeout; this only stops the round from waiting on it.
        task.cancel()
    ordered: list[tuple[str, dict[str, Any]]] = []
    for index, url in enumerate(urls):
        if index in done_pages:
            ordered.append(done_pages[index])
        else:
            ordered.append(
                (
                    url,
                    {
                        "ok": False,
                        "error_reason": "fetch_abandoned",
                        "error": (
                            "Page was still loading after enough other pages "
                            "had loaded, and was skipped."
                        ),
                    },
                )
            )
    return ordered


_HTML_PAGE_RE = re.compile(r"(?i)<!doctype\s+html|<html[\s>]")
# A page runs to </html>, or to the end of the text when the answer was cut
# off mid-markup -- a truncated page must be recovered too, not left behind.
_PAGE_IN_TEXT_RE = re.compile(
    r"(?is)(?:```[a-z0-9]*[^\S\n]*\n)?\s*"
    r"((?:<!doctype\s+html|<html[\s>]).*?(?:</html\s*>|\Z))"
)


def unwrap_document_source(text: str) -> str:
    """Turn a whole HTML page written into an answer back into prose.

    The answer is meant to be read in chat; the server turns it into
    whatever file was asked for. Told "html olarak ver", the model
    sometimes writes the finished page into the answer instead, leaving a
    two-line cover note wrapped around six kilobytes of markup. Three
    things then went wrong at once: the user read a stylesheet in chat, the
    handoff check saw a stub and dropped the Document role, and the run
    produced no file -- though the material had in fact been gathered.

    So the page is unwrapped rather than deleted: its headings and list
    items become markdown, and the result rejoins the surrounding prose.
    The findings survive, Document still runs, and the deck is built by the
    path that already works.

    Only a full page is touched. This same answer path serves ordinary chat
    questions -- "python decorator nasil calisir, ornek ver" must keep its
    code block -- so a fence is opened only when what it holds is a
    document, never merely because it is code.
    """
    body = str(text or "")
    match = _PAGE_IN_TEXT_RE.search(body)
    if not match:
        return body

    try:
        from parsel import Selector

        selector = Selector(text=match.group(1))
    except Exception:
        return body

    lines: list[str] = []
    for node in selector.css("h1, h2, h3, h4, p, li, figcaption, caption"):
        content = " ".join("".join(node.css("::text").getall()).split()).strip()
        if not content:
            continue
        tag = getattr(node.root, "tag", "")
        if tag in {"h1", "h2", "h3", "h4"}:
            lines.append(f"\n{'#' * (int(tag[1]) + 1)} {content}")
        elif tag == "li":
            lines.append(f"- {content}")
        else:
            lines.append(content)

    recovered = "\n".join(lines).strip()
    prose = (body[: match.start()] + "\n" + body[match.end() :]).replace("```", " ")
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    if len(recovered) < 200:
        # Nothing worth keeping. Drop the markup anyway -- it was never
        # readable as an answer -- and let the usual handling judge what is
        # left on its merits.
        return prose
    return f"{prose}\n\n{recovered}".strip()


_TR_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇâîû", "iisSgGuUoOcCaiu")


def _title_tokens(text: str) -> set[str]:
    folded = str(text or "").translate(_TR_FOLD).casefold()
    return {w for w in re.findall(r"[0-9a-z]+", folded) if len(w) > 1}


def drop_echoed_request(text: str, request_text: str) -> str:
    """Remove an opening line that just repeats the user's own request.

    The chat renders the answer's first plain line as its heading, so when
    the model opens by restating the ask, the reader gets their own typing
    back as a title: "erzincan 1 haftalik namaz vakitleri pdf olarak ver
    ac" in bold, with the real title -- "Erzincan 1 Haftalık Namaz
    Vakitleri (03-09 Eylül 2026)" -- demoted to the line below. Telling the
    model not to do this did not hold.

    The test is a comparison against the actual request, not a list of
    words to look for: an echo repeats nearly every token of the ask,
    delivery instructions included, while a real title keeps only the
    subject and adds something new (here, the dates). Measured on that run,
    the echo covered 100% of the request's tokens and the good title 56%,
    so the two do not sit near the threshold.
    """
    body = str(text or "").lstrip()
    request_tokens = _title_tokens(request_text)
    if len(request_tokens) < 3:
        return body

    lines = body.split("\n")
    first = lines[0].strip()
    if not first or len(first) > 140 or first.startswith(("#", "|", ">")):
        return body
    # Nothing would be left to read.
    if not any(line.strip() for line in lines[1:]):
        return body

    line_tokens = _title_tokens(first)
    if not line_tokens:
        return body
    covered = len(request_tokens & line_tokens) / len(request_tokens)
    if covered < 0.85:
        return body

    return "\n".join(lines[1:]).lstrip()


async def answer_from_evidence(
    request_text: str,
    evidence: list[dict[str, Any]],
    *,
    complete: CompleteFn,
    current_date: Optional[str] = None,
    depth: str = "lookup",
) -> str:
    system = (
        SURVEY_ANSWER_SYSTEM
        if (depth or "").strip().casefold() == "survey"
        else ANSWER_SYSTEM
    )
    prompt = _with_date(current_date, format_evidence_prompt(request_text, evidence))
    text = ""
    # One retry when the model returns nothing usable -- a single empty/
    # degenerate completion here otherwise wastes the whole research pass
    # (wants_artifact callers have no prose left to build a deliverable
    # from, and any handoff table becomes context-free; see _run_research).
    for _attempt in range(2):
        parsed = await _complete(complete, "answer", system, prompt, None)
        text = parsed.strip() if isinstance(parsed, str) else str(parsed or "").strip()
        text = unwrap_document_source(text)
        text = drop_echoed_request(text, request_text)
        if text:
            break
    return text


async def run_web_controller(
    request_text: str,
    *,
    search: SearchFn,
    fetch: FetchFn,
    complete: CompleteFn,
    progress: Optional[ProgressFn] = None,
    write_answer: bool = True,
    select_limit: int = SELECT_LIMIT,
    current_date: Optional[str] = None,
    wants_artifact: bool = False,
    depth: str = "lookup",
) -> ControllerResult:
    result = await acquire_web_evidence(
        request_text,
        search=search,
        fetch=fetch,
        complete=complete,
        progress=progress,
        select_limit=select_limit,
        current_date=current_date,
        wants_artifact=wants_artifact,
        depth=depth,
    )
    if write_answer:
        result.answer = await answer_from_evidence(
            request_text,
            result.evidence,
            complete=complete,
            current_date=current_date,
            depth=depth,
        )
    result.handoff = _gated_handoff(result.evidence, wants_artifact=wants_artifact)
    return result


__all__ = [
    "ANSWER_SYSTEM",
    "SURVEY_ANSWER_SYSTEM",
    "ControllerResult",
    "CoverageAssessment",
    "MAX_FETCHES",
    "MAX_FETCHES_ARTIFACT",
    "MAX_FETCHES_COMPACT",
    "MAX_FETCHES_SURVEY",
    "MAX_SEARCH_PHASES",
    "MAX_SEARCH_PHASES_ARTIFACT",
    "MAX_SEARCH_PHASES_COMPACT",
    "MAX_SEARCH_PHASES_SURVEY",
    "ResearchPlan",
    "SELECT_LIMIT",
    "SELECT_LIMIT_ARTIFACT",
    "SELECT_LIMIT_COMPACT",
    "SearchQuery",
    "UrlSelection",
    "acquire_web_evidence",
    "answer_from_evidence",
    "evidence_has_figures",
    "evidence_looks_on_topic",
    "evidence_needs_refine",
    "fact_seed_query",
    "format_evidence_prompt",
    "handoff_from_evidence",
    "is_successful_fetch",
    "recover_model",
    "request_looks_like_stats",
    "run_web_controller",
    "stats_seed_query",
]
