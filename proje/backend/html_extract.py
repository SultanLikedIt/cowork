"""Engine-agnostic HTML/JSON extraction shared by every acquisition engine.

Nothing here depends on Scrapy, Twisted, or Playwright/Crawl4AI: every
function takes plain text (HTML) or a parsel Selector built from it, so the
same logic serves whichever engine actually performed the network fetch.
The data-shape contract (``AcquiredPage`` / ``AcquisitionResult`` /
``AcquisitionError``) lives here too, since it is the seam every engine and
``web_tools.py`` agree on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from parsel import Selector

MAX_TEXT_CHARS = 40_000
MAX_TABLES = 20
MAX_TABLE_ROWS = 10_000
MAX_TABLE_COLUMNS = 100
MAX_RECORDS_PER_PAGE = 50

# --- Embedded-JSON dataset recovery ------------------------------------
#
# A growing share of the data-bearing web renders its tables client-side from
# a JSON payload the server already inlined (Next.js `__NEXT_DATA__`, Nuxt's
# `__NUXT__`, and plain `application/json` script blocks). Those pages carry
# NO <table> element at all, so a <table>-only extractor sees an empty page,
# falls through to cleaned prose, and returns the cookie-consent banner as
# the "answer" -- confirmed against live standings pages on beinsports.com.tr
# and ntvspor.net.
#
# The recovered payload is genuinely declared page data, exactly like the
# schema.org path above: we only ever read a homogeneous array of flat
# objects and transpose it. Nothing is inferred, no site-specific selector is
# involved, and a payload that does not have that shape is skipped rather
# than guessed at.
MIN_JSON_TABLE_ROWS = 3
MAX_JSON_TABLE_COLUMNS = 40
# A hydration payload holds the page's dataset next to its own bookkeeping
# (menus, block manifests, season lists), and those are shaped like tables
# too, so several candidates reach table selection. Ranking them by size to
# trim the list was tried and is wrong: on a real standings page the fixture
# and season arrays are LONGER than the 18-row standings table. Size does not
# identify the dataset a page was built to show, so all candidates are kept
# in document order and _select_table decides on headers and sample rows.
# Depth bound on the payload walk. Real payloads nest their data 4-8 levels
# in; past this the traversal is only paying for framework bookkeeping.
MAX_JSON_WALK_DEPTH = 12
MAX_JSON_BLOB_CHARS = 4_000_000

_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

# A closed lexicon: these phrases identify a consent/cookie interstitial in
# the two languages this deployment serves. A JS-rendered data page whose
# only server-rendered prose is its cookie banner otherwise looks like a
# successfully extracted page -- confirmed on live standings pages, where
# the banner text was delivered to the user as the research answer.
_CONSENT_MARKERS = (
    "çerez",
    "cookie",
    "kvkk",
    "gizlilik politikas",
    "privacy policy",
    "consent",
    "accept all",
    "tümünü kabul",
    # Turkish banners lean on KVKK disclosure wording rather than the word
    # "cookie", and run long enough to clear MIN_MEANINGFUL_TEXT_CHARS.
    "aydınlatma metni",
    "aydinlatma metni",
    "kişisel veri",
    "kisisel veri",
    "tercihlerinizi",
)
# Below this, cleaned prose is too short to be an answer to anything; the
# consent check only has to rule out banners longer than that.
MIN_MEANINGFUL_TEXT_CHARS = 400
# Ceiling for the stricter banner test above. A cookie notice is a few
# paragraphs; an article that actually discusses cookie law runs well past
# this, so beyond it the page is treated as real prose whatever it mentions.
_CONSENT_MAX_CHARS = 1_500

# A closed lexicon of weekday names/abbreviations, Turkish and English --
# same pattern as _CONSENT_MARKERS. A <table> whose header row is made up
# ENTIRELY of these is a date-picker/calendar UI widget (a real TCMB page,
# for example, renders one next to its actual exchange-rate table so the
# visitor can pick which day's rates to view) rather than a dataset the page
# is publishing, and must never compete with a real table as an extraction
# candidate -- confirmed live: a calendar widget outscored the real rates
# table and was delivered as the answer. Requiring the WHOLE header to be
# weekday tokens (not just some of them) keeps a legitimate table that
# happens to have day-of-week columns from being discarded.
_WEEKDAY_TOKENS = frozenset(
    {
        "pts", "pazartesi", "sal", "sali", "salı", "çar", "car", "çarşamba",
        "carsamba", "per", "perşembe", "persembe", "cum", "cuma", "cts",
        "cumartesi", "paz", "pazar",
        "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday",
        "thu", "thur", "thurs", "thursday", "fri", "friday", "sat",
        "saturday", "sun", "sunday",
    }
)


def _looks_like_calendar(header_row: list[str]) -> bool:
    cells = [cell.strip().casefold() for cell in header_row if cell.strip()]
    if len(cells) < 3:
        return False
    return all(cell in _WEEKDAY_TOKENS for cell in cells)


class AcquisitionError(RuntimeError):
    """A bounded, classified acquisition failure."""

    def __init__(self, reason: str, message: str, *, retryable: bool = False) -> None:
        self.reason = reason
        self.message = " ".join(str(message or "Web acquisition failed.").split())[:500]
        self.retryable = retryable
        super().__init__(f"{self.reason}: {self.message}")


def classify_http_status(url: str, status: int) -> AcquisitionError:
    """One taxonomy for every HTTP failure status, shared by every engine.

    Only 5xx/429 are retryable -- a genuinely transient, server-side
    condition. 404/401/403/410/451 and any other 4xx are deterministic:
    the URL itself is wrong or forbidden, and retrying (or, worse,
    mutating the path to guess a sibling URL) cannot change that.
    """
    if status == 404:
        return AcquisitionError("not_found", f"{url} returned HTTP 404 (not found).")
    if status in (401, 403, 410, 451):
        return AcquisitionError(
            "access_blocked", f"{url} returned HTTP {status} (access blocked)."
        )
    if status >= 500 or status == 429:
        return AcquisitionError(
            "server_error", f"{url} returned HTTP {status}.", retryable=True
        )
    return AcquisitionError("http_error", f"{url} returned HTTP {status}.")


@dataclass
class AcquiredPage:
    url: str
    status: int
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    content_type: str = ""
    body_bytes: int = 0
    javascript_required: bool = False
    script_chars: int = 0
    body_chars: int = 0
    rows_dropped: bool = False
    # Machine-readable records the PAGE ITSELF declared (schema.org
    # JSON-LD/microdata, OpenGraph product tags) -- see declared_records().
    # Never inferred from prose: a field absent here was absent on the page.
    records: list[dict[str, Any]] = field(default_factory=list)
    # One caption per entry in `tables`, same order: the table's own
    # <caption>, or failing that its nearest preceding heading. A page's
    # cleaned prose (page_context, used elsewhere) is shared by every table
    # on it, so it cannot tell a page's several tables apart -- a
    # season-tab page publishing "2022-2023" and "2025-2026" standings as
    # two same-header <table>s side by side is otherwise indistinguishable
    # from paginated fragments of one dataset. Empty for tables recovered
    # from embedded JSON (no DOM position to caption from).
    table_captions: list[str] = field(default_factory=list)

    @property
    def text_parts(self) -> list[str]:
        return [self.text]


@dataclass
class AcquisitionResult:
    pages: list[AcquiredPage]
    page_limit_reached: bool = False


def is_consent_boilerplate(text: str) -> bool:
    """Whether cleaned prose is just a cookie/consent interstitial.

    Deliberately requires the text to be BOTH short and consent-flavoured:
    a real article about privacy law must not be discarded, and it will be
    far longer than a banner.

    Turkish banners run past MIN_MEANINGFUL_TEXT_CHARS -- sahadan.com's is
    535 characters -- so that limit alone let one through as the page's
    content. A match's squad page was cited as a source while the answer
    said the lineup was not in the evidence, which was true: the evidence
    was the cookie notice. Longer text is still checked, but has to look
    like a banner throughout rather than merely mention cookies once, which
    is what keeps a genuine article on cookie law out of this.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    lowered = stripped.casefold()
    if len(stripped) < MIN_MEANINGFUL_TEXT_CHARS:
        return any(marker in lowered for marker in _CONSENT_MARKERS)
    if len(stripped) >= _CONSENT_MAX_CHARS:
        return False
    # Between the two limits, judge by how saturated the text is: a banner
    # of this length says "çerez" in nearly every sentence. Occurrences,
    # not distinct markers -- sahadan's says it three times and would
    # otherwise count as one.
    hits = sum(lowered.count(marker) for marker in _CONSENT_MARKERS)
    return hits >= 3


def collapse(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


def _table_caption(table_node) -> str:
    """A table's own <caption>, or failing that its nearest preceding
    heading -- the closest static-DOM signal to "which tab/season is this"
    for a page that renders several same-shaped tables side by side.
    """
    own = collapse(table_node.xpath("./caption//text()").getall())
    if own:
        return own[:200]
    heading = collapse(
        table_node.xpath(
            "(preceding::h1 | preceding::h2 | preceding::h3 | preceding::h4)[last()]//text()"
        ).getall()
    )
    return heading[:200]


def extract_tables(
    selector: Selector,
) -> tuple[list[list[list[str]]], list[str]]:
    extracted: list[list[list[str]]] = []
    captions: list[str] = []
    for table in selector.xpath("//table")[:MAX_TABLES]:
        rows: list[list[str]] = []
        # column_index -> (value, rows_remaining), held down by a rowspan
        # from an earlier row. HTML emits a colspan/rowspan cell only ONCE
        # in the source; without expanding it here, a grouped header (e.g.
        # one <th colspan="2"> over "Home"/"Away" data columns) has fewer
        # cells than the data rows beneath it, and every column from that
        # point on silently shifts by one -- the standings/prayer-time-table
        # "columns are shifted" failure this fixes.
        pending: dict[int, tuple[str, int]] = {}
        for tr in table.xpath(".//tr")[:MAX_TABLE_ROWS]:
            cells: list[str] = []
            col = 0
            cell_nodes = iter(tr.xpath("./th|./td"))
            node = next(cell_nodes, None)
            while len(cells) < MAX_TABLE_COLUMNS and (
                node is not None or col in pending
            ):
                if col in pending:
                    value, remaining = pending.pop(col)
                    cells.append(value)
                    if remaining > 1:
                        pending[col] = (value, remaining - 1)
                    col += 1
                    continue
                text = collapse(node.xpath(".//text()").getall())
                try:
                    colspan = max(
                        1,
                        min(int(node.xpath("./@colspan").get() or 1), MAX_TABLE_COLUMNS),
                    )
                except ValueError:
                    colspan = 1
                try:
                    rowspan = max(1, int(node.xpath("./@rowspan").get() or 1))
                except ValueError:
                    rowspan = 1
                for _ in range(colspan):
                    if len(cells) >= MAX_TABLE_COLUMNS:
                        break
                    cells.append(text)
                    if rowspan > 1:
                        pending[col] = (text, rowspan - 1)
                    col += 1
                node = next(cell_nodes, None)
            if cells:
                rows.append(cells)
        if rows and not _looks_like_calendar(rows[0]):
            extracted.append(rows)
            captions.append(_table_caption(table))
    return extracted, captions


def _clean_scalar(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return " ".join(str(value).split())[:300]
    return ""


def _json_cell(value: Any) -> Optional[str]:
    """Render one payload value as a table cell, or None if it is not scalar.

    Booleans and nulls are legitimate cells; nested objects/arrays are not,
    and their presence is what disqualifies a column below.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return " ".join(str(value).split())[:300]
    if isinstance(value, dict):
        # One level of unwrapping: payloads routinely model a cell as
        # {"name": "Galatasaray", "logo": ...}. Anything deeper is structure,
        # not a cell.
        for key in ("name", "title", "label", "text", "value", "shortName"):
            inner = value.get(key)
            if isinstance(inner, (str, int, float)):
                return " ".join(str(inner).split())[:300]
    return None


def _json_rows_to_table(rows: list[dict[str, Any]]) -> Optional[list[list[str]]]:
    """A homogeneous array of flat objects -> a header row plus data rows.

    Returns None unless the array really is tabular: enough rows, a shared
    key set, and at least two columns whose values are scalar in EVERY row.
    That last condition is what keeps framework bookkeeping (menus, route
    manifests, nested component trees) from being mistaken for a dataset.
    """
    if len(rows) < MIN_JSON_TABLE_ROWS:
        return None
    shared: Optional[set[str]] = None
    for row in rows:
        keys = set(row.keys())
        shared = keys if shared is None else (shared & keys)
        if not shared:
            return None
    columns = [
        key
        for key in rows[0]
        if key in shared
        and not key.startswith("@")
        and all(_json_cell(row.get(key)) is not None for row in rows)
    ]
    if len(columns) < 2:
        return None
    columns = columns[:MAX_JSON_TABLE_COLUMNS]
    table = [list(columns)]
    for row in rows[:MAX_TABLE_ROWS]:
        table.append([_json_cell(row.get(key)) or "" for key in columns])
    return table


def _walk_json_tables(
    node: Any, found: list[list[list[str]]], depth: int = 0
) -> None:
    if depth > MAX_JSON_WALK_DEPTH or len(found) >= MAX_TABLES:
        return
    if isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            table = _json_rows_to_table(node)
            if table is not None:
                found.append(table)
                # The array itself is the dataset; its rows are cells, not
                # further tables to descend into.
                return
        for item in node[:MAX_TABLE_ROWS]:
            _walk_json_tables(item, found, depth + 1)
        return
    if isinstance(node, dict):
        for value in node.values():
            _walk_json_tables(value, found, depth + 1)


_NUXT_RE = re.compile(
    r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*(?:</script>|$)", re.DOTALL
)


def embedded_json_tables(selector: Selector) -> list[list[list[str]]]:
    """Datasets the page inlined as JSON but renders only via JavaScript.

    Deliberately limited to payloads the page DECLARES as data: a typed
    JSON script block, or the two framework hydration globals. Arbitrary
    inline JavaScript is not scanned -- parsing that would be guessing.
    """
    blobs: list[str] = []
    for value in selector.xpath(
        '//script[@type="application/json" or @id="__NEXT_DATA__"]/text()'
    ).getall():
        if value and value.strip():
            blobs.append(value)
    for value in selector.xpath("//script[not(@src)]/text()").getall():
        if "__NUXT__" not in value:
            continue
        match = _NUXT_RE.search(value)
        if match:
            blobs.append(match.group(1))

    found: list[list[list[str]]] = []
    for blob in blobs:
        if len(found) >= MAX_TABLES or len(blob) > MAX_JSON_BLOB_CHARS:
            continue
        try:
            payload = json.loads(blob)
        except (ValueError, TypeError):
            continue  # Malformed or not-actually-JSON: skipped, never guessed.
        _walk_json_tables(payload, found)
    return found[:MAX_TABLES]


def _record_from_product(node: dict[str, Any], page_url: str) -> Optional[dict[str, Any]]:
    """One schema.org Product node -> a record with only DECLARED fields.

    A field the page did not declare is simply absent from the record. That
    absence is the whole point: it is what lets the caller report "4 of 10
    products verified" instead of filling the gap with a plausible price.
    """
    name = _clean_scalar(node.get("name"))
    if not name:
        return None
    record: dict[str, Any] = {"name": name}
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict):
        for source_key, target_key in (
            ("price", "price"),
            ("priceCurrency", "currency"),
            ("availability", "availability"),
        ):
            value = _clean_scalar(offers.get(source_key))
            if value:
                record[target_key] = value
    for source_key, target_key in (("sku", "sku"), ("url", "url"), ("brand", "brand")):
        value = node.get(source_key)
        if isinstance(value, dict):
            value = value.get("name")
        cleaned = _clean_scalar(value)
        if cleaned:
            record[target_key] = cleaned
    record.setdefault("url", page_url)
    record["source_url"] = page_url
    return record


def _walk_jsonld(node: Any, page_url: str, found: list[dict[str, Any]]) -> None:
    if len(found) >= MAX_RECORDS_PER_PAGE:
        return
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, page_url, found)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if any(str(value).lower() == "product" for value in types if value):
        record = _record_from_product(node, page_url)
        if record:
            found.append(record)
    for key in ("@graph", "itemListElement", "mainEntity", "item", "hasPart"):
        if key in node:
            _walk_jsonld(node[key], page_url, found)


def declared_records(selector: Selector, page_url: str) -> list[dict[str, Any]]:
    """Structured records the page publishes about itself, never inferred.

    Three declared sources, in descending reliability: schema.org JSON-LD,
    schema.org microdata, and OpenGraph product meta tags. This is what
    makes "find 10 products and their prices" a verifiable extraction
    rather than a reading-comprehension task performed by a model on prose
    -- and therefore what makes a missing price a MISSING price.
    """
    found: list[dict[str, Any]] = []
    for blob in selector.xpath('//script[@type="application/ld+json"]/text()').getall():
        if len(found) >= MAX_RECORDS_PER_PAGE:
            break
        try:
            _walk_jsonld(json.loads(blob), page_url, found)
        except (ValueError, TypeError):
            continue  # A malformed block is skipped, never guessed at.

    if len(found) < MAX_RECORDS_PER_PAGE:
        for scope in selector.xpath(
            '//*[contains(translate(@itemtype, "PRODUCT", "product"), "schema.org/product")]'
        )[:MAX_RECORDS_PER_PAGE]:
            node: dict[str, Any] = {}
            name = scope.xpath('.//*[@itemprop="name"]/@content').get() or collapse(
                scope.xpath('.//*[@itemprop="name"]//text()').getall()
            )
            if name:
                node["name"] = name
            offers: dict[str, Any] = {}
            for prop, key in (
                ("price", "price"),
                ("priceCurrency", "priceCurrency"),
                ("availability", "availability"),
            ):
                value = scope.xpath(f'.//*[@itemprop="{prop}"]/@content').get() or collapse(
                    scope.xpath(f'.//*[@itemprop="{prop}"]//text()').getall()
                )
                if value:
                    offers[key] = value
            if offers:
                node["offers"] = offers
            record = _record_from_product(node, page_url)
            if record and record not in found:
                found.append(record)

    if not found:
        og_type = (selector.xpath('//meta[@property="og:type"]/@content').get() or "").lower()
        og_title = selector.xpath('//meta[@property="og:title"]/@content').get() or ""
        og_price = selector.xpath(
            '//meta[@property="product:price:amount" or @property="og:price:amount"]/@content'
        ).get()
        if og_title and ("product" in og_type or og_price):
            node = {"name": og_title}
            if og_price:
                offers = {"price": og_price}
                currency = selector.xpath(
                    '//meta[@property="product:price:currency" or '
                    '@property="og:price:currency"]/@content'
                ).get()
                if currency:
                    offers["priceCurrency"] = currency
                node["offers"] = offers
            record = _record_from_product(node, page_url)
            if record:
                found.append(record)
    return found[:MAX_RECORDS_PER_PAGE]


def extract_page(
    *,
    url: str,
    status: int,
    html: str,
    content_type: str = "",
    body_bytes: int = 0,
) -> AcquiredPage:
    """Extract one already-fetched HTML document with Parsel and Trafilatura.

    Engine-agnostic: takes plain text, not a Scrapy/Crawl4AI response object,
    so it is exactly what both engines' ``parse``-equivalent step delegates
    to once they have HTML in hand.
    """
    import trafilatura  # local import: keeps this module importable even
    # in contexts (e.g. quick link-scoring) that never need Trafilatura.

    is_xml = "xml" in content_type and "html" not in content_type
    selector = Selector(text=html, type="xml" if is_xml else "html")
    if is_xml:
        title = selector.xpath("string(/*[1]/*[local-name()='title'][1])").get() or ""
        text = collapse(selector.xpath("//text()").getall())[:MAX_TEXT_CHARS]
        links = [
            (value.strip(), "")
            for value in selector.xpath(
                "//*[local-name()='loc' or local-name()='link']/text()"
            ).getall()
            if value.strip().startswith(("http://", "https://"))
        ]
        return AcquiredPage(
            url=url,
            status=status,
            title=collapse([title])[:300],
            text=text,
            links=links,
            content_type=content_type,
            body_bytes=body_bytes or len(html.encode("utf-8", "replace")),
            body_chars=len(text),
        )

    title = collapse(selector.css("title ::text").getall())[:300]
    cleaned = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        include_links=False,
        favor_precision=True,
    ) or ""
    if not cleaned.strip():
        cleaned = collapse(
            selector.xpath(
                "//body//*[not(self::script or self::style or self::noscript)]/text()"
            ).getall()
        )
    cleaned = cleaned.strip()[:MAX_TEXT_CHARS]
    links: list[tuple[str, str]] = []
    for anchor in selector.css("a[href]"):
        href = str(anchor.attrib.get("href") or "").strip()
        if href:
            from urllib.parse import urljoin

            links.append((urljoin(url, href), collapse(anchor.xpath(".//text()").getall())[:200]))
    script_chars = sum(len(value) for value in selector.xpath("//script/text()").getall())
    script_chars += 200 * len(selector.xpath("//script[@src]").getall())
    app_shell = bool(selector.css("#root, #app, [data-reactroot], [ng-app]"))
    tables, table_captions = extract_tables(selector)
    if not tables:
        # Only as a fallback: a real <table> is a stronger statement about
        # what the page publishes than an array found inside its hydration
        # payload, so markup always wins when both exist. No DOM position to
        # caption a recovered-from-JSON table with.
        tables = embedded_json_tables(selector)
        table_captions = [""] * len(tables)
    records = declared_records(selector, url)
    javascript_required = (
        not tables
        and not records
        and (len(cleaned) < 80 or is_consent_boilerplate(cleaned))
        and (script_chars > 2_000 or app_shell)
    )
    return AcquiredPage(
        url=url,
        status=status,
        title=title,
        text=cleaned,
        links=links,
        tables=tables,
        records=records,
        content_type=content_type,
        body_bytes=body_bytes or len(html.encode("utf-8", "replace")),
        javascript_required=javascript_required,
        script_chars=script_chars,
        body_chars=len(cleaned),
        table_captions=table_captions,
    )


def keywords(value: str) -> set[str]:
    return {word.lower() for word in _WORD_RE.findall(value or "")}


def link_score(label: str, url: str, need: str) -> float:
    wanted = keywords(need)
    label_words = keywords(label)
    url_words = keywords(url.replace("/", " ").replace("-", " "))
    score = 2.0 * len(wanted & label_words) + len(wanted & url_words)
    for number in re.findall(r"\d{2,4}", need or ""):
        if number in label or number in url:
            score += 2.0
    return score


# Private-name alias kept for tests written against the pre-extraction
# module (scrapy_engine.py), which only ever exposed this as `_declared_records`.
_declared_records = declared_records

__all__ = [
    "MAX_TEXT_CHARS",
    "MAX_TABLES",
    "MIN_MEANINGFUL_TEXT_CHARS",
    "AcquiredPage",
    "AcquisitionError",
    "AcquisitionResult",
    "classify_http_status",
    "collapse",
    "declared_records",
    "embedded_json_tables",
    "extract_page",
    "extract_tables",
    "is_consent_boilerplate",
    "keywords",
    "link_score",
]
