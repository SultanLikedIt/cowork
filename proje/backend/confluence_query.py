"""From the model's reading of a request to a CQL string, and page bodies to text.

The model never writes CQL. The PLAN phase fills in ``ConfluenceQuery`` and
``build_cql`` turns it into a query from fixed templates, every value a
quoted literal -- so a keyword like ``" OR space = SECRET`` is one literal,
not a new clause. The space allowlist is added here unconditionally.

Page ids are only ever taken from links the user typed (``page_refs``), on
the configured Confluence host: a model can "remember" a page id from
nowhere, and the request text is the only authority -- the same rule as Jira
issue keys (``jira_query``).
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, unquote_plus, urlsplit

from pydantic import BaseModel, Field

import confluence_client
from confluence_client import ConfluenceConfig, build_space_clause, cql_string

MAX_TEXT_CHARS = 120
MAX_TITLE_CHARS = 200
MAX_DAYS = 3650
MAX_PAGE_REFS = 8
# Nothing narrower asked for: the recently changed pages in the allowed spaces.
DEFAULT_WINDOW_DAYS = 30
CONTENT_TYPES = ("page", "blogpost")

# CQL's `text ~` goes through Lucene; its operators are removed, not escaped.
_LUCENE_SPECIAL_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_PAGES_PATH_RE = re.compile(r"/spaces/([^/]+)/(?:pages|blog/\d{4}/\d{2}/\d{2})/(\d{1,20})(?:/|$)")
_DISPLAY_PATH_RE = re.compile(r"/display/([^/]+)/([^/?#]+)")


class ConfluenceQuery(BaseModel):
    """What the user's request asks of Confluence, as the PLAN phase reads it."""

    spaces: list[str] = Field(default_factory=list)
    text: str = ""
    title: str = ""
    content_type: str = ""
    updated_within_days: int = 0
    needs_details: bool = True


@dataclass(frozen=True)
class PageRef:
    """A page the user linked: by id, or (Server/DC display links) by title."""

    page_id: str = ""
    space: str = ""
    title: str = ""


@dataclass
class BuiltCql:
    cql: str
    defaulted_window: bool = False
    # User-named spaces that are not on the allowlist.
    refused_spaces: list[str] = field(default_factory=list)


def _plain(value: Any, limit: int) -> str:
    text = "".join(
        " " if unicodedata.category(ch).startswith("C") else ch for ch in str(value or "")
    )
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(days, MAX_DAYS))


def normalize_query(parsed: ConfluenceQuery, request_text: str) -> ConfluenceQuery:
    """Clamp and clean what PLAN returned. Spaces stay as the user named them;
    the allowlist is applied in ``build_cql`` so the refusal can be reported."""
    spaces: list[str] = []
    for raw in parsed.spaces or []:
        for key in confluence_client.parse_spaces(raw):
            if key not in spaces:
                spaces.append(key)
    content_type = parsed.content_type if parsed.content_type in CONTENT_TYPES else ""
    return ConfluenceQuery(
        spaces=spaces[:10],
        text=_plain(parsed.text, MAX_TEXT_CHARS),
        title=_plain(parsed.title, MAX_TITLE_CHARS),
        content_type=content_type,
        updated_within_days=_days(parsed.updated_within_days),
        needs_details=bool(parsed.needs_details),
    )


def fallback_query(request_text: str) -> ConfluenceQuery:
    """PLAN failed: search the request's own words in every allowed space."""
    return ConfluenceQuery(text=_plain(request_text, MAX_TEXT_CHARS), needs_details=True)


def _keywords(text: str) -> str:
    return " ".join(_LUCENE_SPECIAL_RE.sub(" ", text).split())[:MAX_TEXT_CHARS]


def build_cql(query: ConfluenceQuery, allowed: tuple[str, ...]) -> BuiltCql:
    """The CQL for ``query``, always inside the allowlist."""
    built = BuiltCql(cql="")
    clauses = [build_space_clause(allowed)]
    named = [key for key in query.spaces if key in allowed]
    built.refused_spaces = [key for key in query.spaces if key not in allowed]
    if named:
        clauses.append(build_space_clause(tuple(named)))
    types = (query.content_type,) if query.content_type else CONTENT_TYPES
    clauses.append("type in (" + ", ".join(types) + ")")
    keywords = _keywords(query.text)
    if keywords:
        clauses.append(f"text ~ {cql_string(keywords)}")
    title = _keywords(query.title)
    if title:
        clauses.append(f"title ~ {cql_string(title)}")
    days = query.updated_within_days
    if not days and not keywords and not title:
        days = DEFAULT_WINDOW_DAYS
        built.defaulted_window = True
    if days:
        clauses.append(f'lastmodified >= now("-{days}d")')
    cql = " AND ".join(clauses)
    # A keyword search is ranked by relevance; anything else, newest first.
    if not keywords and not title:
        cql += " order by lastmodified desc"
    built.cql = cql
    return built


def page_refs(request_text: str, config: ConfluenceConfig) -> list[PageRef]:
    """Pages the user linked on THIS Confluence (other hosts are ignored)."""
    base = urlsplit(config.base_url)
    host = (base.hostname or "").lower()
    refs: list[PageRef] = []
    for raw in _URL_RE.findall(request_text or ""):
        parts = urlsplit(raw.rstrip(".,;"))
        if (parts.hostname or "").lower() != host or not host:
            continue
        path = unquote_plus(parts.path)
        ref: Optional[PageRef] = None
        page_id = (parse_qs(parts.query).get("pageId") or [""])[0]
        match = _PAGES_PATH_RE.search(path)
        if page_id and confluence_client.PAGE_ID_RE.match(page_id):
            ref = PageRef(page_id=page_id)
        elif match:
            ref = PageRef(page_id=match.group(2), space=match.group(1))
        else:
            display = _DISPLAY_PATH_RE.search(path)
            if display:
                ref = PageRef(space=display.group(1), title=_plain(display.group(2), MAX_TITLE_CHARS))
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= MAX_PAGE_REFS:
            break
    return refs


# --------------------------------------------------------------------------
# Storage format (XHTML + ac:/ri: macros) -> readable text
# --------------------------------------------------------------------------

_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_DROP_RE = re.compile(
    r"<(ac:parameter|ac:placeholder|script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_BLOCK_BREAK_RE = re.compile(
    r"</?(p|div|h[1-6]|li|tr|table|ul|ol|blockquote|pre|ac:structured-macro|ac:rich-text-body|ac:plain-text-body|ac:task)\b[^>]*>",
    re.IGNORECASE,
)


def storage_to_text(storage: str) -> str:
    """Confluence storage XHTML as plain text with light structure.

    Headings become "## ", list items "- ", table rows "a | b | c"; code
    blocks keep their text (it is CDATA in storage format, which an HTML
    parser would drop). Links keep their text only.
    """
    text = str(storage or "")
    if not text.strip():
        return ""
    text = _CDATA_RE.sub(lambda m: html.escape(m.group(1)), text)
    text = _DROP_RE.sub(" ", text)
    text = re.sub(r"<h[1-6]\b[^>]*>", "\n## ", text, flags=re.IGNORECASE)
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</t[dh]>\s*<t[dh]\b[^>]*>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line in {"-", "##"}:
            continue
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


__all__ = [
    "BuiltCql",
    "ConfluenceQuery",
    "PageRef",
    "build_cql",
    "fallback_query",
    "normalize_query",
    "page_refs",
    "storage_to_text",
]
