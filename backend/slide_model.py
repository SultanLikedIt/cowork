"""Typed slide deck for PPTX deliverables.

Why this module exists
----------------------
A .pptx used to be reverse-engineered from prose: the Document LLM wrote a
markdown outline, the worker usually swapped it for the (longer) research
notes, and document_export.build_pptx cut that text into four 100-character
bullets per slide with regexes. A 20-row price table became six slides of
bullets, each row clipped at "…"; nested lists fell apart; every slide had
the same layout.

Here the Document LLM designs the deck itself -- an ordered list of slides,
each with a layout (bullets, stats, table, chart, two_column, quote,
section, takeaways) and speaker notes -- and document_pptx only draws it.
The same approach document_model takes for PDF/DOCX blocks.

Parsing is lenient in the same way: aliases ("kpi", "comparison",
"graph"), bullets as strings or {text, items}, table rows as objects,
numbers written "6.842,40" or "1,200". A slide that cannot be understood
is dropped on its own, or degraded to a simpler layout (a chart without
usable numbers becomes a table or bullets), instead of failing the deck.

Numbers on stat cards and charts, and quotes, are checked against the
research notes (`ground_deck`): these are what a model is most tempted to
round, convert or invent, and a big "%34" on a slide is read as fact.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

import document_export
import document_model

# --- Bounds ---------------------------------------------------------------
MAX_SLIDES = 40
MAX_BULLETS = 40
MAX_BULLET_LEVEL = 1
MAX_STATS = 4
MAX_TABLE_COLUMNS = 8
MAX_TABLE_ROWS = 200
MAX_CHART_POINTS = 60
MAX_CHART_SERIES = 6
MAX_PIE_SLICES = 8
MAX_NOTES_CHARS = 2_000
MAX_TEXT_CHARS = 600
# A deck with fewer content slides than this is not a deck the model
# designed; the worker falls back to the notes-driven builder instead.
MIN_CONTENT_SLIDES = 2

Layout = Literal[
    "bullets", "stats", "table", "chart", "two_column", "quote", "section", "takeaways"
]
ChartKind = Literal["line", "bar", "column", "pie"]


def slides_enabled() -> bool:
    """True when the Document worker should request and render typed slides.

    On unless DOCUMENT_SLIDES says otherwise: the notes-driven builder it
    replaces stays reachable as the fallback, and as the whole path with
    DOCUMENT_SLIDES=false.
    """
    return os.environ.get("DOCUMENT_SLIDES", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# --- Model ----------------------------------------------------------------


class Bullet(BaseModel):
    text: str
    level: int = 0


class Stat(BaseModel):
    value: str
    label: str = ""
    detail: str = ""


class ColumnBox(BaseModel):
    heading: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Series(BaseModel):
    name: str = ""
    values: list[Optional[float]] = Field(default_factory=list)


class Chart(BaseModel):
    kind: ChartKind = "column"
    categories: list[str] = Field(default_factory=list)
    series: list[Series] = Field(default_factory=list)
    unit: str = ""


class Slide(BaseModel):
    layout: Layout = "bullets"
    title: str = ""
    # One-line key message under the title (table/chart/stats/section).
    subtitle: str = ""
    bullets: list[Bullet] = Field(default_factory=list)
    stats: list[Stat] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    chart: Optional[Chart] = None
    left: Optional[ColumnBox] = None
    right: Optional[ColumnBox] = None
    text: str = ""
    attribution: str = ""
    notes: str = ""
    # Set by pagination in document_pptx, never by the model.
    continued: bool = False


class Deck(BaseModel):
    title: str = ""
    subtitle: str = ""
    slides: list[Slide] = Field(default_factory=list)

    def content_slides(self) -> list[Slide]:
        return [s for s in self.slides if s.layout != "section"]

    def slide_titles(self) -> list[str]:
        return [s.title for s in self.slides if s.title]

    def to_markdown(self) -> str:
        """The deck as markdown, for everything that still reads `content`.

        The HTML deck, a secondary DOCX, the chat answer and the length
        checks in the Document worker all read prose; one heading per slide
        keeps each slide a section there too.
        """
        parts: list[str] = []
        for slide in self.slides:
            if slide.layout == "section":
                continue
            lines: list[str] = []
            if slide.title:
                lines.append(f"## {slide.title}")
            if slide.subtitle:
                lines.append(slide.subtitle)
            if slide.layout == "stats":
                lines.extend(
                    f"- **{s.value}** {s.label}".rstrip()
                    + (f" ({s.detail})" if s.detail else "")
                    for s in slide.stats
                )
            elif slide.layout == "table" and slide.columns:
                lines.append(_markdown_table(slide.columns, slide.rows))
            elif slide.layout == "chart" and slide.chart is not None:
                lines.append(_markdown_table(*chart_as_table(slide.chart)))
            elif slide.layout == "two_column":
                for box in (slide.left, slide.right):
                    if box is None:
                        continue
                    if box.heading:
                        lines.append(f"### {box.heading}")
                    lines.extend(_bullet_lines(box.bullets))
            elif slide.layout == "quote":
                lines.append(
                    slide.text + (f" — {slide.attribution}" if slide.attribution else "")
                )
            lines.extend(_bullet_lines(slide.bullets))
            if len(lines) > (1 if slide.title else 0):
                parts.append("\n".join(line for line in lines if line.strip()))
        return "\n\n".join(parts)


def _bullet_lines(bullets: list[Bullet]) -> list[str]:
    return ["  " * b.level + f"- {b.text}" for b in bullets]


def _markdown_table(columns: list[str], rows: list[list[str]]) -> str:
    def line(cells: list[str]) -> str:
        return "| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |"

    out = [line(columns), line(["---"] * len(columns))]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def chart_as_table(chart: Chart) -> tuple[list[str], list[list[str]]]:
    """(columns, rows) holding a chart's data -- one row per category."""
    columns = [""] + [s.name or f"Seri {i + 1}" for i, s in enumerate(chart.series)]
    rows = []
    for index, category in enumerate(chart.categories):
        row = [category]
        for series in chart.series:
            value = series.values[index] if index < len(series.values) else None
            row.append("" if value is None else format_number(value))
        rows.append(row)
    return columns, rows


# --- Numbers --------------------------------------------------------------

# A number as people write it: 6.842,40 / 6,842.40 / 1.200 / 12,5 / 4393.76.
_NUMBER_TOKEN = re.compile(r"(?<![\w.,])[-+−]?\d[\d.,  ]*\d|(?<![\w.,])[-+−]?\d")
_THOUSANDS_COMMA = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(?:\.\d{3})+$")


def _token_value(token: str) -> Optional[float]:
    sign = -1.0 if token[:1] in {"-", "−"} else 1.0
    digits = token.lstrip("+-−").replace(" ", "").replace(" ", "").strip(".,")
    if not digits:
        return None
    if "." in digits and "," in digits:
        decimal = "," if digits.rfind(",") > digits.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        digits = digits.replace(thousands, "").replace(decimal, ".")
    elif "," in digits:
        digits = digits.replace(",", "") if _THOUSANDS_COMMA.match(digits) else digits.replace(",", ".")
    elif "." in digits and _THOUSANDS_DOT.match(digits):
        # Turkish thousands ("1.200 kg", "6.842") -- the likelier reading
        # in this product, and "1.200" as 1.2 is almost never written.
        digits = digits.replace(".", "")
    if digits.count(".") > 1:
        return None
    try:
        return sign * float(digits)
    except ValueError:
        return None


def parse_number(value: Any) -> Optional[float]:
    """The first number in `value` ("6.842,40 TL" -> 6842.4), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    match = _NUMBER_TOKEN.search(str(value or ""))
    return _token_value(match.group(0)) if match else None


def numbers_in_text(text: str) -> list[float]:
    """Every number written in `text`, in both readings when ambiguous."""
    found: list[float] = []
    for match in _NUMBER_TOKEN.finditer(text or ""):
        token = match.group(0)
        value = _token_value(token)
        if value is not None:
            found.append(value)
        # "1.5" / "1,5" / "4,4–5,5": also keep each piece around a
        # separator, so a range or a list still grounds its members.
        for piece in re.split(r"[\s  ]+", token.strip()):
            if piece and piece != token:
                alt = _token_value(piece)
                if alt is not None:
                    found.append(alt)
    return found


def format_number(value: float) -> str:
    """Turkish display: 6842.4 -> "6.842,4", 1200.0 -> "1.200"."""
    if value == int(value) and abs(value) < 1e15:
        text = f"{int(value):,}"
    else:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


class NumberIndex:
    """The numbers of a source text, for "does this figure appear there?"."""

    def __init__(self, text: str) -> None:
        self._values = sorted(set(numbers_in_text(text)))

    def __bool__(self) -> bool:
        return bool(self._values)

    def contains(self, value: Optional[float]) -> bool:
        if value is None:
            return True
        tolerance = max(abs(value) * 0.005, 0.01)
        import bisect

        index = bisect.bisect_left(self._values, value - tolerance)
        return index < len(self._values) and self._values[index] <= value + tolerance


# --- Normalization --------------------------------------------------------

_LAYOUT_ALIASES: dict[str, Layout] = {
    **dict.fromkeys(
        ["bullets", "bullet", "list", "content", "text", "points", "bullet_list"], "bullets"
    ),
    **dict.fromkeys(
        ["stats", "stat", "kpi", "kpis", "metrics", "numbers", "big_numbers", "figures"], "stats"
    ),
    **dict.fromkeys(["table", "grid", "data", "data_table"], "table"),
    **dict.fromkeys(["chart", "graph", "plot", "line_chart", "bar_chart"], "chart"),
    **dict.fromkeys(
        ["two_column", "two-column", "twocolumn", "comparison", "compare", "columns", "split"],
        "two_column",
    ),
    **dict.fromkeys(["quote", "callout", "highlight", "key_message", "statement"], "quote"),
    **dict.fromkeys(["section", "divider", "section_header", "chapter"], "section"),
    **dict.fromkeys(
        ["takeaways", "takeaway", "summary", "conclusion", "key_points", "sonuc", "sonuç"],
        "takeaways",
    ),
}
_CHART_ALIASES: dict[str, ChartKind] = {
    **dict.fromkeys(["line", "lines", "trend", "line_markers", "area"], "line"),
    **dict.fromkeys(["bar", "horizontal_bar", "barh", "hbar"], "bar"),
    **dict.fromkeys(["column", "columns", "vertical_bar", "col", "histogram"], "column"),
    **dict.fromkeys(["pie", "donut", "doughnut"], "pie"),
}

TextFilter = Callable[[str], str]


def _text(value: Any, text_filter: Optional[TextFilter], *, keep_bold: bool = True) -> str:
    text = document_model._clean_text(value, keep_bold=keep_bold)
    text = document_model._LEADING_LIST_MARK.sub("", text)
    if not text or document_model._is_noise(text):
        return ""
    if text_filter is not None:
        text = " ".join((text_filter(text) or "").split())
    return text[:MAX_TEXT_CHARS]


def _plain(value: Any, text_filter: Optional[TextFilter] = None) -> str:
    return _text(value, text_filter, keep_bold=False)


def _bullets(raw: Any, text_filter: Optional[TextFilter], level: int = 0) -> list[Bullet]:
    if isinstance(raw, str):
        raw = [line for line in raw.split("\n") if line.strip()]
    if not isinstance(raw, list):
        return []
    out: list[Bullet] = []
    for entry in raw:
        if len(out) >= MAX_BULLETS:
            break
        if isinstance(entry, dict):
            text = _text(entry.get("text") or entry.get("title") or "", text_filter)
            # An explicit level is what a dumped Bullet carries back in.
            item_level = document_model._clamp(entry.get("level", level), 0, MAX_BULLET_LEVEL, level)
            if text:
                out.append(Bullet(text=text, level=item_level))
            children = entry.get("items") or entry.get("children") or entry.get("bullets")
            if children and item_level < MAX_BULLET_LEVEL:
                out.extend(_bullets(children, text_filter, item_level + 1))
        elif isinstance(entry, list):
            out.extend(_bullets(entry, text_filter, min(level + 1, MAX_BULLET_LEVEL)))
        else:
            text = _text(entry, text_filter)
            if text:
                out.append(Bullet(text=text, level=level))
    return out[:MAX_BULLETS]


def _stats(raw: Any, text_filter: Optional[TextFilter]) -> list[Stat]:
    if not isinstance(raw, list):
        return []
    out: list[Stat] = []
    for entry in raw:
        if isinstance(entry, dict):
            value = _plain(entry.get("value") or entry.get("number") or entry.get("figure"))
            label = _plain(entry.get("label") or entry.get("title") or entry.get("text"), text_filter)
            detail = _plain(entry.get("detail") or entry.get("context") or entry.get("note"), text_filter)
        else:
            # "5,5 m — yetişkin erkek boyu" / "Nüfus: 85 milyon"
            pieces = re.split(r"\s+[—–-]\s+|:\s+", _plain(entry, text_filter), maxsplit=1)
            value = pieces[0].strip()
            label = pieces[1].strip() if len(pieces) > 1 else ""
            detail = ""
        if value and len(value) <= 24:
            out.append(Stat(value=value, label=label, detail=detail))
    return out[:MAX_STATS]


def _box(raw: Any, text_filter: Optional[TextFilter]) -> Optional[ColumnBox]:
    if isinstance(raw, (list, str)):
        raw = {"bullets": raw}
    if not isinstance(raw, dict):
        return None
    heading = _plain(raw.get("heading") or raw.get("title"), text_filter)
    bullets = _bullets(raw.get("bullets") or raw.get("items") or raw.get("points"), text_filter)
    if not bullets:
        return None
    return ColumnBox(heading=heading, bullets=bullets)


def _chart(raw: Any, text_filter: Optional[TextFilter]) -> Optional[Chart]:
    if not isinstance(raw, dict):
        return None
    kind_raw = str(raw.get("type") or raw.get("kind") or "column").strip().lower()
    kind = _CHART_ALIASES.get(kind_raw, "column")
    categories = raw.get("categories") or raw.get("labels") or raw.get("x") or []
    series_raw = raw.get("series") or []
    if not series_raw and isinstance(raw.get("values") or raw.get("data"), list):
        series_raw = [{"name": raw.get("name") or "", "values": raw.get("values") or raw.get("data")}]
    if isinstance(series_raw, dict):
        series_raw = [{"name": k, "values": v} for k, v in series_raw.items()]
    if not isinstance(categories, list) or not isinstance(series_raw, list):
        return None
    cats = [_plain(c) or str(c) for c in categories][:MAX_CHART_POINTS]
    series: list[Series] = []
    for entry in series_raw[:MAX_CHART_SERIES]:
        if isinstance(entry, dict):
            name = _plain(entry.get("name") or entry.get("label"), text_filter)
            values = entry.get("values") or entry.get("data") or []
        elif isinstance(entry, list):
            name, values = "", entry
        else:
            continue
        if not isinstance(values, list):
            continue
        parsed = [parse_number(v) for v in values[: len(cats)]]
        parsed.extend([None] * (len(cats) - len(parsed)))
        if sum(v is not None for v in parsed) >= 2:
            series.append(Series(name=name, values=parsed))
    if len(cats) < 2 or not series:
        return None
    if kind == "pie":
        values = series[0].values
        if (
            len(series) > 1
            or len(cats) > MAX_PIE_SLICES
            or any(v is None or v <= 0 for v in values)
        ):
            kind = "bar"
    return Chart(kind=kind, categories=cats, series=series, unit=_plain(raw.get("unit")))


def _table(entry: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    table = document_model._table(
        {
            "columns": entry.get("columns") or entry.get("headers") or entry.get("header"),
            "rows": entry.get("rows") or entry.get("data"),
        }
    )
    if table is None:
        return [], []
    columns = table.columns[:MAX_TABLE_COLUMNS]
    rows = [row[: len(columns)] for row in table.rows[:MAX_TABLE_ROWS]]
    return columns, rows


def _notes_text(raw: Any, text_filter: Optional[TextFilter]) -> str:
    """Speaker notes: plain sentences (the notes pane shows no bold)."""
    if isinstance(raw, list):
        raw = " ".join(str(part) for part in raw)
    text = document_model._clean_text(raw, keep_bold=False)
    if text and text_filter is not None:
        text = " ".join((text_filter(text) or "").split())
    return text[:MAX_NOTES_CHARS]


def _layout_of(entry: dict[str, Any]) -> Optional[Layout]:
    raw = str(entry.get("layout") or entry.get("type") or entry.get("kind") or "").strip().lower()
    if raw in _LAYOUT_ALIASES:
        return _LAYOUT_ALIASES[raw]
    if isinstance(entry.get("chart"), dict):
        return "chart"
    if entry.get("stats"):
        return "stats"
    if entry.get("rows"):
        return "table"
    if entry.get("left") or entry.get("right"):
        return "two_column"
    if entry.get("bullets") or entry.get("items") or entry.get("points"):
        return "bullets"
    if entry.get("text") or entry.get("quote"):
        return "quote"
    return None


def _coerce_slide(entry: Any, text_filter: Optional[TextFilter]) -> Optional[Slide]:
    if not isinstance(entry, dict):
        return None
    layout = _layout_of(entry)
    if layout is None:
        return None
    title = _plain(entry.get("title") or entry.get("heading"), text_filter)
    if document_model._SOURCES_LABEL.match(title or ""):
        # The server appends the verified sources slide itself.
        return None
    notes = _notes_text(entry.get("notes") or entry.get("speaker_notes"), text_filter)
    slide = Slide(
        layout=layout,
        title=title,
        subtitle=_plain(entry.get("subtitle") or entry.get("message") or entry.get("takeaway"), text_filter),
        bullets=_bullets(
            entry.get("bullets") or entry.get("items") or entry.get("points"), text_filter
        ),
        notes=notes,
    )
    if layout == "stats":
        slide.stats = _stats(entry.get("stats") or entry.get("items"), text_filter)
        slide.bullets = [] if entry.get("stats") is None else slide.bullets
    elif layout == "table":
        slide.columns, slide.rows = _table(entry)
    elif layout == "chart":
        slide.chart = _chart(entry.get("chart") or entry, text_filter)
    elif layout == "two_column":
        slide.left = _box(entry.get("left"), text_filter)
        slide.right = _box(entry.get("right"), text_filter)
    elif layout == "quote":
        slide.text = _text(entry.get("text") or entry.get("quote"), text_filter)
        slide.attribution = _plain(entry.get("attribution") or entry.get("source"), text_filter)
    return _repair(slide)


def _repair(slide: Slide) -> Optional[Slide]:
    """Degrade a slide whose layout lost its content; None if nothing is left."""
    layout = slide.layout
    if layout == "chart" and slide.chart is None:
        slide.layout = "table" if slide.rows and slide.columns else "bullets"
    if layout == "stats" and len(slide.stats) < 2:
        slide.bullets = [
            Bullet(text=f"**{s.value}** {s.label}".strip()) for s in slide.stats
        ] + slide.bullets
        slide.stats = []
        slide.layout = "bullets"
    if slide.layout == "table" and not (slide.columns and slide.rows):
        slide.layout = "bullets"
    if slide.layout == "two_column" and not (slide.left and slide.right):
        box = slide.left or slide.right
        if box is not None:
            if box.heading:
                slide.bullets.append(Bullet(text=box.heading))
                slide.bullets.extend(
                    Bullet(text=b.text, level=min(b.level + 1, MAX_BULLET_LEVEL))
                    for b in box.bullets
                )
            else:
                slide.bullets.extend(box.bullets)
        slide.left = slide.right = None
        slide.layout = "bullets"
    if slide.layout == "quote" and not slide.text:
        slide.layout = "bullets"
    if slide.layout in {"bullets", "takeaways"} and not slide.bullets:
        return None
    if slide.layout == "section" and not slide.title:
        return None
    return slide


def parse_slides_value(value: Any) -> Optional[list[Any]]:
    """The raw slide array from whatever shape the caller passed."""
    if value is None:
        return None
    if isinstance(value, str):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", value.strip(), flags=re.IGNORECASE)
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        value = value.get("slides")
    return value if isinstance(value, list) else None


def deck_from_slides(
    title: str,
    raw_slides: Any,
    *,
    subtitle: str = "",
    text_filter: Optional[TextFilter] = None,
) -> Optional[Deck]:
    """A validated deck from model-supplied slides, or None if unusable."""
    entries = parse_slides_value(raw_slides)
    if not entries:
        return None
    slides: list[Slide] = []
    for entry in entries[: MAX_SLIDES * 2]:
        slide = _coerce_slide(entry, text_filter)
        if slide is not None:
            slides.append(slide)
    clean_title = _plain(title)
    # A first slide that only repeats the deck title is the title slide the
    # renderer draws anyway.
    if slides and clean_title and slides[0].layout in {"section", "quote"}:
        if _same(slides[0].title or slides[0].text, clean_title):
            slides = slides[1:]
    deck = Deck(title=clean_title, subtitle=_plain(subtitle, text_filter), slides=slides[:MAX_SLIDES])
    return deck if len(deck.content_slides()) >= MIN_CONTENT_SLIDES else None


def _same(a: str, b: str) -> bool:
    fold = lambda s: " ".join(re.sub(r"[^\w\s]", " ", s or "").split()).casefold()  # noqa: E731
    return bool(fold(a)) and fold(a) == fold(b)


# --- From typed document blocks ---------------------------------------------


def deck_from_document(document: Any, *, subtitle: str = "") -> Optional[Deck]:
    """A deck drawn from PDF/DOCX typed blocks (a "Word + PowerPoint" job).

    No model call: every level-1 section becomes a slide, its lists and
    paragraphs become bullets, and each table and callout gets its own
    slide -- so the .pptx carries the same report as the .docx beside it.
    """
    slides: list[Slide] = []
    current: Optional[Slide] = None

    def flush() -> None:
        nonlocal current
        if current is not None and current.bullets:
            slides.append(current)
        current = None

    for block in getattr(document, "blocks", []):
        text = getattr(block, "text", "")
        if isinstance(block, (document_model.HeadingBlock, document_model.ParagraphBlock)) and (
            document_model._SOURCES_LABEL.match(text or "")
        ):
            # The appended Kaynaklar tail: the renderer draws its own
            # sources slide from the same list.
            break
        if isinstance(block, document_model.HeadingBlock):
            if block.level == 1 or current is None:
                flush()
                current = Slide(layout="bullets", title=document_model._strip_bold(block.text))
            else:
                current.bullets.append(Bullet(text=f"**{document_model._strip_bold(block.text)}**"))
            continue
        if current is None:
            current = Slide(layout="bullets", title="")
        if isinstance(block, document_model.ParagraphBlock):
            current.bullets.extend(Bullet(text=s) for s in _sentences(block.text))
        elif isinstance(block, document_model.ListBlock):
            current.bullets.extend(
                Bullet(text=i.text, level=min(i.level, MAX_BULLET_LEVEL)) for i in block.items
            )
        elif isinstance(block, document_model.TableBlock):
            title = current.title
            flush()
            slides.append(
                Slide(
                    layout="table",
                    title=title,
                    columns=block.columns[:MAX_TABLE_COLUMNS],
                    rows=[r[:MAX_TABLE_COLUMNS] for r in block.rows[:MAX_TABLE_ROWS]],
                )
            )
            current = Slide(layout="bullets", title=title)
        elif isinstance(block, document_model.QuoteBlock):
            title = current.title
            flush()
            slides.append(Slide(layout="quote", title=title, text=block.text))
            current = Slide(layout="bullets", title=title)
    flush()
    for slide in slides:
        if not slide.title:
            slide.title = document_export._pptx_title_from_text(
                (slide.bullets[0].text if slide.bullets else slide.text) or "Özet"
            )
    deck = Deck(title=str(getattr(document, "title", "") or ""), subtitle=subtitle, slides=slides[:MAX_SLIDES])
    return deck if len(deck.content_slides()) >= MIN_CONTENT_SLIDES else None


def _sentences(text: str) -> list[str]:
    """A paragraph as slide bullets: whole sentences, never clipped."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+(?=[A-ZÇĞİÖŞÜ0-9\"“(])", text or "")]
    return [p for p in parts if p]


# --- Grounding --------------------------------------------------------------


def ground_deck(
    deck: Deck, source_text: str, *, log: Optional[Callable[[str], None]] = None
) -> Deck:
    """Drop stat cards, charts and quotes that `source_text` does not back.

    Only the layouts that present something as fact at a glance are
    checked: a stat card and a chart turn a number into a headline, and
    they are where a model rounds, converts or computes a figure the
    research never gave; a quote is presented as someone's words. Tables
    and bullets carry the notes' own wording and are left alone.
    Without a source (a deck written from general knowledge) nothing is
    checked, since there is nothing to check against.
    """
    index = NumberIndex(source_text)
    words = _content_words(source_text)
    if not index and not words:
        return deck
    kept: list[Slide] = []
    for slide in deck.slides:
        if slide.layout == "quote" and words:
            # A quote is presented as someone's words. One the notes do not
            # contain is invented -- live: "Zürafa, doğanın en zarif ve en
            # uzun canlısıdır; her adımı bir dans..." credited to "doğa
            # belgeseli anlatıcıları (bilinmiyor)".
            quoted = _content_words(slide.text)
            if quoted and len(quoted & words) < 0.6 * len(quoted):
                if log:
                    log("slide %r: quote dropped, not found in the notes: %r" % (slide.title, slide.text[:80]))
                continue
            kept.append(slide)
            continue
        if not index:
            kept.append(slide)
            continue
        if slide.layout == "stats":
            grounded = [s for s in slide.stats if index.contains(parse_number(s.value))]
            if len(grounded) < len(slide.stats) and log:
                log(
                    "slide %r: dropped %d stat(s) not found in the notes: %s"
                    % (
                        slide.title,
                        len(slide.stats) - len(grounded),
                        [s.value for s in slide.stats if s not in grounded],
                    )
                )
            slide.stats = grounded
            repaired = _repair(slide)
            if repaired is not None:
                kept.append(repaired)
            continue
        if slide.layout == "chart" and slide.chart is not None:
            values = [v for s in slide.chart.series for v in s.values if v is not None]
            missing = [v for v in values if not index.contains(v)]
            if values and len(missing) > len(values) * 0.2:
                if log:
                    log(
                        "slide %r: chart dropped, %d of %d values not in the notes (e.g. %s)"
                        % (slide.title, len(missing), len(values), missing[:3])
                    )
                slide.chart = None
                slide.layout = "bullets"
                repaired = _repair(slide)
                if repaired is not None:
                    kept.append(repaired)
                continue
        kept.append(slide)
    deck.slides = kept
    return deck


def _content_words(text: str) -> set[str]:
    """Words of 4+ letters, case- and diacritic-folded, for overlap checks."""
    import unicodedata

    folded = unicodedata.normalize("NFKD", (text or "").replace("ı", "i").casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return {w for w in re.findall(r"[^\W\d_]{4,}", folded)}


# --- Sources ------------------------------------------------------------------

_SOURCE_LINE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])?\s*(?P<title>.*?)\s*\(?(?P<url>https?://[^\s)]+)\)?\s*$")


def sources_from_text(text: str) -> list[tuple[str, str]]:
    """(title, url) pairs from a server-written "Kaynaklar:" tail."""
    _, tail = document_model.split_trailing_sources(text or "")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in tail.splitlines()[1:]:
        match = _SOURCE_LINE.match(line)
        if not match:
            continue
        url = match.group("url").rstrip(".,;")
        if url in seen:
            continue
        seen.add(url)
        title = match.group("title").strip(" -–—:()")
        out.append((title if title and title != url else "", url))
    return out


# --- Payload plumbing ----------------------------------------------------------
#
# Like document_model's blocks: the worker keeps editing `content` after the
# reply is parsed (sources appended, refusals stripped). The slides are only
# still the thing to render if `content` is, apart from that tail, the
# markdown they produced.

SLIDES_MARKDOWN_KEY = "_slides_markdown"


def attach_slides_to_payload(
    payload: dict[str, Any], *, text_filter: Optional[TextFilter] = None
) -> Optional[Deck]:
    """Validate payload["slides"] and mirror them into payload["content"]."""
    raw = payload.get("slides")
    if raw is None:
        return None
    deck = deck_from_slides(
        str(payload.get("title") or ""),
        raw,
        subtitle=str(payload.get("subtitle") or ""),
        text_filter=text_filter,
    )
    if deck is None:
        payload.pop("slides", None)
        return None
    set_payload_deck(payload, deck)
    if not str(payload.get("summary") or "").strip():
        payload["summary"] = deck.subtitle or next(
            (document_model._strip_bold(b.text) for s in deck.slides for b in s.bullets), ""
        )[:600]
    return deck


def set_payload_deck(payload: dict[str, Any], deck: Deck) -> None:
    markdown = deck.to_markdown()
    payload["slides"] = [slide.model_dump() for slide in deck.slides]
    payload["subtitle"] = deck.subtitle
    payload["content"] = markdown
    payload["body"] = markdown
    payload[SLIDES_MARKDOWN_KEY] = markdown


def resolve_payload_deck(payload: dict[str, Any]) -> Optional[Deck]:
    """The deck to render for this payload, or None for the legacy path."""
    raw = payload.get("slides")
    marker = str(payload.get(SLIDES_MARKDOWN_KEY) or "")
    if not raw or not marker:
        return None
    content = str(payload.get("content") or payload.get("body") or "")
    body, _tail = document_model.split_trailing_sources(content)
    if document_model._fingerprint(body) != document_model._fingerprint(marker):
        return None
    try:
        slides = [Slide.model_validate(s) for s in raw]
    except Exception:  # noqa: BLE001 - a hand-edited payload is not ours to fix
        return None
    return Deck(
        title=str(payload.get("title") or ""),
        subtitle=str(payload.get("subtitle") or ""),
        slides=slides,
    )


__all__ = [
    "Bullet",
    "Chart",
    "ColumnBox",
    "Deck",
    "Series",
    "Slide",
    "Stat",
    "attach_slides_to_payload",
    "chart_as_table",
    "deck_from_document",
    "deck_from_slides",
    "format_number",
    "ground_deck",
    "numbers_in_text",
    "parse_number",
    "parse_slides_value",
    "resolve_payload_deck",
    "set_payload_deck",
    "slides_enabled",
    "sources_from_text",
]
