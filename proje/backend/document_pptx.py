"""Draws a slide_model.Deck as an editable .pptx (python-pptx).

Every text box is sized before it is drawn: the renderer estimates how many
lines each paragraph wraps to (Calibri advance widths, `wrap_count`) and
picks the largest font size from a short ladder that fits. When even the
smallest readable size does not fit, the slide is split -- bullets by
whole groups (a sub-bullet never leaves its parent), tables by rows with
the header repeated -- into parts of balanced height, so there is no
"devam" slide carrying one leftover line. Text is never clipped with "…".

Planning (`plan_deck`) and drawing (`build_pptx`) are separate so the
slide count is known before the footer's "n / N" is written, and so tests
can check pagination without opening a file.

Look: the dark research palette document_export's deck already used
(same accent, same rail), a footer with the deck title and page number,
native tables and charts (editable in PowerPoint), speaker notes, and
clickable sources.
"""

from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

import document_export
from slide_model import Bullet, Chart, Deck, Slide, parse_number

# --- Geometry (inches, 16:9) ---------------------------------------------
SLIDE_W, SLIDE_H = 13.333, 7.5
LEFT, RIGHT = 0.9, 0.75
CONTENT_W = SLIDE_W - LEFT - RIGHT
TITLE_TOP, TITLE_H = 0.38, 0.95
RULE_Y = TITLE_TOP + TITLE_H + 0.08
BODY_TOP = RULE_Y + 0.32
BODY_BOTTOM = 6.72
FOOTER_Y = 6.98
SUBTITLE_GAP = 0.18

# Font ladders, largest first. The last entry is the smallest size the
# layout may use before it paginates or degrades instead.
TITLE_SIZES = (30, 28, 26, 24, 22)
BULLET_SIZES = (24, 22, 20, 18, 16)
PAGINATED_BULLET_SIZE = 18
TABLE_SIZES = (16, 14, 13, 12, 11)
PAGINATED_TABLE_SIZE = 12
LINE = 1.2  # line pitch as a multiple of the font size

# Palette: document_export's deck colours, so both builders look alike.
BG = document_export._PPTX_BG
BG_SOFT = document_export._PPTX_BG_SOFT
ACCENT = document_export._PPTX_ACCENT
ACCENT_WARM = document_export._PPTX_ACCENT_WARM
TEXT = document_export._PPTX_TEXT
MUTED = document_export._PPTX_MUTED
DIM = document_export._PPTX_DIM
BULLET_TEXT = document_export._PPTX_BULLET
CARD = document_export._PPTX_CARD
CARD_ALT = document_export._PPTX_CARD_ALT
GRID = "1E2C3E"
SERIES_COLORS = ("3ECFFF", "E8A87C", "7BD88F", "C39BFF", "FF7B9C", "F2D16B", "5AA9E6", "B0BEC5")
FONT = "Calibri"

_MONTHS = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)

# --- Text measurement ------------------------------------------------------
#
# Calibri advance widths in em (hmtx / 2048). Accented letters measure as
# their base letter; anything unknown as 0.5 em. The 6% margin covers
# kerning-free rendering and the bold face, which is slightly wider.

_EM = {
    " ": 0.226, "a": 0.479, "b": 0.525, "c": 0.423, "d": 0.525, "e": 0.498,
    "f": 0.305, "g": 0.471, "h": 0.525, "i": 0.229, "j": 0.239, "k": 0.455,
    "l": 0.229, "m": 0.799, "n": 0.525, "o": 0.527, "p": 0.525, "q": 0.525,
    "r": 0.349, "s": 0.391, "t": 0.335, "u": 0.525, "v": 0.452, "w": 0.715,
    "x": 0.433, "y": 0.453, "z": 0.395, "ı": 0.229,
    "A": 0.579, "B": 0.544, "C": 0.533, "D": 0.615, "E": 0.488, "F": 0.459,
    "G": 0.631, "H": 0.623, "I": 0.252, "J": 0.319, "K": 0.520, "L": 0.420,
    "M": 0.855, "N": 0.646, "O": 0.662, "P": 0.517, "Q": 0.673, "R": 0.543,
    "S": 0.459, "T": 0.487, "U": 0.642, "V": 0.567, "W": 0.890, "X": 0.519,
    "Y": 0.487, "Z": 0.468,
    ".": 0.252, ",": 0.250, ":": 0.268, ";": 0.268, "-": 0.306, "–": 0.498,
    "—": 0.905, "(": 0.303, ")": 0.303, "%": 0.715, "/": 0.386, "'": 0.221,
    "’": 0.250, '"': 0.401, "“": 0.418, "”": 0.418, "·": 0.252, "•": 0.498,
    "!": 0.326, "?": 0.463, "+": 0.498, "=": 0.498, "&": 0.682, "$": 0.507,
    "€": 0.507, "₺": 0.507,
}
_SAFETY = 1.06


def _char_em(ch: str) -> float:
    if ch.isdigit():
        return 0.507
    if ch in _EM:
        return _EM[ch]
    base = unicodedata.normalize("NFKD", ch)[:1]
    return _EM.get(base, 0.5)


def text_width(text: str, size_pt: float, *, bold: bool = False) -> float:
    """Rendered width of one line of `text`, in inches."""
    em = sum(_char_em(ch) for ch in text or "")
    return em * size_pt / 72 * _SAFETY * (1.05 if bold else 1.0)


def wrap_count(text: str, size_pt: float, width_in: float, *, bold: bool = False) -> int:
    """How many lines `text` wraps to in a box `width_in` wide (greedy)."""
    if width_in <= 0:
        return 10_000
    lines = 0
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        if not words:
            lines += 1
            continue
        space = text_width(" ", size_pt, bold=bold)
        current = 0.0
        count = 1
        for word in words:
            width = text_width(word, size_pt, bold=bold)
            if width > width_in:
                # A word wider than the box breaks across lines itself.
                if current:
                    count += 1
                extra = math.ceil(width / width_in)
                count += extra - 1
                current = width - (extra - 1) * width_in
                continue
            needed = width if not current else current + space + width
            if needed <= width_in:
                current = needed
            else:
                count += 1
                current = width
        lines += count
    return lines


def text_height(
    text: str, size_pt: float, width_in: float, *, bold: bool = False, after_pt: float = 0.0
) -> float:
    return (wrap_count(text, size_pt, width_in, bold=bold) * size_pt * LINE + after_pt) / 72


def _plain(text: str) -> str:
    return document_export.INLINE_MARKS.sub(r"\2", text or "")


# --- Planning ----------------------------------------------------------------


@dataclass
class Planned:
    """One slide as it will be drawn."""

    kind: str  # title | agenda | section | sources | a slide layout
    slide: Optional[Slide] = None
    title: str = ""
    continued: bool = False
    size: float = 0.0
    bullets: list[Bullet] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    col_widths: list[float] = field(default_factory=list)
    row_heights: list[float] = field(default_factory=list)
    header_height: float = 0.0
    items: list[Any] = field(default_factory=list)
    number: int = 0
    notes: str = ""


def _title_size(title: str, width: float = CONTENT_W) -> tuple[float, int]:
    for size in TITLE_SIZES:
        lines = wrap_count(title, size, width, bold=True)
        if lines == 1 or (lines == 2 and size <= 26):
            return size, lines
    return TITLE_SIZES[-1], wrap_count(title, TITLE_SIZES[-1], width, bold=True)


def _body_top(slide: Slide) -> float:
    if not slide.subtitle:
        return BODY_TOP
    return BODY_TOP + text_height(slide.subtitle, 18, CONTENT_W) + SUBTITLE_GAP


def _bullet_groups(bullets: Sequence[Bullet]) -> list[list[Bullet]]:
    """Top-level bullets with their sub-bullets; a page never splits one."""
    groups: list[list[Bullet]] = []
    for bullet in bullets:
        if bullet.level > 0 and groups:
            groups[-1].append(bullet)
        else:
            groups.append([bullet])
    return groups


def _bullet_indent(size: float) -> float:
    return 0.32 * size / 20


def bullet_height(bullet: Bullet, size: float, width: float) -> float:
    sub = bullet.level > 0
    font = size - 2 if sub else size
    left = _bullet_indent(size) * (2.2 if sub else 1)
    return text_height(_plain(bullet.text), font, width - left, after_pt=font * 0.55)


def _group_height(group: Sequence[Bullet], size: float, width: float) -> float:
    return sum(bullet_height(b, size, width) for b in group)


def split_balanced(heights: Sequence[float], capacity: float) -> list[list[int]]:
    """Consecutive index chunks, each within `capacity`, of similar height.

    Fewest pages first, then the break points that even them out -- 10+10
    rows rather than 18+2, three bullets on the last page rather than one.
    """
    if not heights:
        return []
    total = sum(heights)
    pages = max(1, math.ceil(total / capacity))
    while True:
        target = total / pages
        chunks: list[list[int]] = []
        current: list[int] = []
        used = 0.0
        for index, height in enumerate(heights):
            remaining_pages = pages - len(chunks) - 1
            if current and (
                used + height > capacity
                or (remaining_pages > 0 and used + height / 2 > target)
            ):
                chunks.append(current)
                current, used = [], 0.0
            current.append(index)
            used += height
        chunks.append(current)
        fits = all(sum(heights[i] for i in chunk) <= capacity + 1e-6 for chunk in chunks)
        if fits or pages >= len(heights):
            return chunks
        pages += 1


def _plan_bullets(slide: Slide, width: float = CONTENT_W) -> list[Planned]:
    top = _body_top(slide)
    capacity = BODY_BOTTOM - top
    for size in BULLET_SIZES:
        if sum(bullet_height(b, size, width) for b in slide.bullets) <= capacity:
            return [Planned(kind=slide.layout, slide=slide, title=slide.title, size=size, bullets=list(slide.bullets))]
    size = PAGINATED_BULLET_SIZE
    groups = _bullet_groups(slide.bullets)
    chunks = split_balanced([_group_height(g, size, width) for g in groups], capacity)
    pages = []
    for number, chunk in enumerate(chunks):
        bullets = [b for i in chunk for b in groups[i]]
        # Each part gets the largest size its own share fits at.
        part_size = next(
            (s for s in BULLET_SIZES if sum(bullet_height(b, s, width) for b in bullets) <= capacity),
            BULLET_SIZES[-1],
        )
        pages.append(
            Planned(
                kind=slide.layout,
                slide=slide,
                title=slide.title,
                continued=number > 0,
                size=min(part_size, size) if len(chunks) > 1 else part_size,
                bullets=bullets,
            )
        )
    return pages


# What may stand next to a figure in a cell that is still "a number":
# currency, percent and a few units. "1 Oca" is a date, not a number.
_NUMERIC_CELL = re.compile(
    r"(?i)^[\s~≈<>+\-−%$€₺(]*\d[\d.,\s]*"
    r"(?:%|\$|€|₺|tl|usd|eur|kg|gr?|ton|km|cm|mm|m|x|bin|milyon|milyar)?[\s)]*$"
)


def _is_numeric_column(rows: Sequence[Sequence[str]], index: int) -> bool:
    """Right-align a column only when nearly every cell is a figure."""
    cells = [str(r[index]).strip() for r in rows if index < len(r) and str(r[index]).strip()]
    if not cells:
        return False
    numeric = sum(1 for c in cells if len(c) <= 20 and _NUMERIC_CELL.match(c))
    return numeric >= 0.8 * len(cells)


CELL_PAD_X, CELL_PAD_Y = 0.1, 0.06


def _column_widths(columns: Sequence[str], rows: Sequence[Sequence[str]], size: float) -> list[float]:
    natural = []
    for index, header in enumerate(columns):
        cells = [str(r[index]) for r in rows if index < len(r)]
        longest_cell = max((text_width(c, size) for c in cells), default=0.0)
        longest_word = max(
            (text_width(w, size, bold=True) for w in str(header).split()), default=0.0
        )
        natural.append(max(longest_cell, longest_word, 0.7) + 2 * CELL_PAD_X)
    total = sum(natural)
    if total <= CONTENT_W:
        # Spread the slack evenly: narrow columns stay readable, the table
        # still spans the slide.
        slack = (CONTENT_W - total) / len(natural)
        return [w + slack for w in natural]
    # Too wide: cap the widest columns first (they wrap best).
    widths = list(natural)
    while sum(widths) > CONTENT_W + 1e-6:
        widest = max(range(len(widths)), key=lambda i: widths[i])
        others = sorted(widths, reverse=True)
        second = others[1] if len(others) > 1 else 0.0
        excess = sum(widths) - CONTENT_W
        floor = max(second, 1.1)
        cut = min(excess, widths[widest] - floor) if widths[widest] > floor else excess / len(widths)
        if widths[widest] > floor:
            widths[widest] -= cut
        else:
            widths = [w * CONTENT_W / sum(widths) for w in widths]
    return widths


def _row_height(cells: Sequence[str], widths: Sequence[float], size: float, *, bold: bool = False) -> float:
    lines = max(
        (wrap_count(str(c), size, w - 2 * CELL_PAD_X, bold=bold) for c, w in zip(cells, widths)),
        default=1,
    )
    return lines * size * LINE / 72 + 2 * CELL_PAD_Y + 0.04


def _plan_table(slide: Slide) -> list[Planned]:
    capacity = BODY_BOTTOM - _body_top(slide)
    columns, rows = slide.columns, slide.rows
    for size in TABLE_SIZES:
        widths = _column_widths(columns, rows, size)
        header = _row_height(columns, widths, size, bold=True)
        heights = [_row_height(r, widths, size) for r in rows]
        if header + sum(heights) <= capacity:
            return [
                Planned(
                    kind="table", slide=slide, title=slide.title, size=size, rows=list(rows),
                    col_widths=widths, row_heights=heights, header_height=header,
                )
            ]
    size = PAGINATED_TABLE_SIZE if len(columns) <= 6 else TABLE_SIZES[-1]
    widths = _column_widths(columns, rows, size)
    header = _row_height(columns, widths, size, bold=True)
    heights = [_row_height(r, widths, size) for r in rows]
    chunks = split_balanced(heights, capacity - header)
    # The split is fixed; now the largest size every part still fits at, so
    # two half-slide parts are not drawn in the small pagination size.
    for bigger in TABLE_SIZES:
        if bigger <= size:
            break
        big_widths = _column_widths(columns, rows, bigger)
        big_header = _row_height(columns, big_widths, bigger, bold=True)
        big_heights = [_row_height(r, big_widths, bigger) for r in rows]
        if all(big_header + sum(big_heights[i] for i in chunk) <= capacity for chunk in chunks):
            size, widths, header, heights = bigger, big_widths, big_header, big_heights
            break
    return [
        Planned(
            kind="table", slide=slide, title=slide.title, continued=number > 0, size=size,
            rows=[rows[i] for i in chunk], col_widths=widths,
            row_heights=[heights[i] for i in chunk], header_height=header,
        )
        for number, chunk in enumerate(chunks)
    ]


def _plan_takeaways(slide: Slide) -> list[Planned]:
    capacity = BODY_BOTTOM - _body_top(slide)
    items = [b for b in slide.bullets if b.level == 0] or slide.bullets
    text_w = CONTENT_W - 1.0

    def item_height(bullet: Bullet, size: float) -> float:
        return max(0.62, text_height(_plain(bullet.text), size, text_w - 0.3) + 0.3) + 0.16

    for size in (24, 22, 20, 18, 16):
        if sum(item_height(b, size) for b in items) <= capacity:
            return [Planned(kind="takeaways", slide=slide, title=slide.title, size=size, bullets=items)]
    chunks = split_balanced([item_height(b, 16) for b in items], capacity)
    start = 0
    pages = []
    for number, chunk in enumerate(chunks):
        pages.append(
            Planned(
                kind="takeaways", slide=slide, title=slide.title, continued=number > 0,
                size=16, bullets=[items[i] for i in chunk], number=start,
            )
        )
        start += len(chunk)
    return pages


def _as_bullets(slide: Slide) -> Slide:
    """A slide whose layout cannot fit, as a plain (paginating) bullet slide."""
    bullets: list[Bullet] = []
    if slide.layout == "two_column":
        for box in (slide.left, slide.right):
            if box is None:
                continue
            if box.heading:
                bullets.append(Bullet(text=f"**{box.heading}**"))
                bullets.extend(Bullet(text=b.text, level=1) for b in box.bullets)
            else:
                bullets.extend(box.bullets)
    elif slide.layout == "quote":
        bullets.append(Bullet(text=slide.text + (f" — {slide.attribution}" if slide.attribution else "")))
    bullets.extend(slide.bullets)
    return slide.model_copy(update={"layout": "bullets", "bullets": bullets, "left": None, "right": None})


def _two_column_size(slide: Slide) -> Optional[float]:
    top = _body_top(slide)
    box_w = (CONTENT_W - 0.45) / 2 - 0.5
    for size in (22, 20, 18, 16, 14):
        fits = True
        for box in (slide.left, slide.right):
            heading = text_height(box.heading, size, box_w, bold=True, after_pt=10) if box and box.heading else 0
            body = sum(bullet_height(b, size, box_w) for b in (box.bullets if box else []))
            if heading + body + 0.6 > BODY_BOTTOM - top:
                fits = False
        if fits:
            return size
    return None


def _quote_size(slide: Slide) -> Optional[float]:
    for size in (36, 32, 28, 24, 22):
        if text_height(_plain(slide.text), size, 10.2) <= 3.9:
            return size
    return None


def _stats_plan(slide: Slide) -> Planned:
    n = len(slide.stats)
    card_w = (CONTENT_W - 0.35 * (n - 1)) / n
    # One line if any size allows it; a value too long for its card even at
    # the smallest ("800–1.200 kg" on a four-card slide) wraps instead.
    value_size = next(
        (
            s for s in (60, 54, 48, 42, 36, 30, 26, 22)
            if all(wrap_count(st.value, s, card_w - 0.5, bold=True) == 1 for st in slide.stats)
        ),
        24,
    )
    return Planned(kind="stats", slide=slide, title=slide.title, size=value_size)


def _stat_value_height(stats: Sequence[Any], size: float, width: float) -> float:
    return max(text_height(st.value, size, width, bold=True) for st in stats)


def plan_deck(deck: Deck, *, sources: Sequence[tuple[str, str]] = ()) -> list[Planned]:
    """Every slide the deck will have, sized and split."""
    planned: list[Planned] = [Planned(kind="title", title=deck.title)]

    sections = [s for s in deck.slides if s.layout == "section"]
    agenda_source = sections if len(sections) >= 2 else [s for s in deck.slides if s.layout != "quote"]
    agenda: list[str] = []
    for slide in agenda_source:
        title = (slide.title or "").strip()
        if title and title.casefold() not in {a.casefold() for a in agenda}:
            agenda.append(title)
    if len(agenda) >= 3:
        planned.append(Planned(kind="agenda", title="Gündem", items=agenda[:10]))

    section_number = 0
    for slide in deck.slides:
        layout = slide.layout
        if layout == "section":
            # A lone divider is not "part 01" of anything: numbered only
            # when the deck has several.
            section_number += 1
            number = section_number if len(sections) >= 2 else 0
            planned.append(Planned(kind="section", slide=slide, title=slide.title, number=number))
            continue
        if layout == "two_column":
            size = _two_column_size(slide)
            if size is None:
                planned.extend(_plan_bullets(_as_bullets(slide)))
            else:
                planned.append(Planned(kind="two_column", slide=slide, title=slide.title, size=size))
            continue
        if layout == "quote":
            size = _quote_size(slide)
            if size is None:
                planned.extend(_plan_bullets(_as_bullets(slide)))
            else:
                planned.append(Planned(kind="quote", slide=slide, title=slide.title, size=size))
            continue
        if layout == "table":
            planned.extend(_plan_table(slide))
            continue
        if layout == "chart" and slide.chart is not None:
            planned.append(Planned(kind="chart", slide=slide, title=slide.title))
            continue
        if layout == "stats" and len(slide.stats) >= 2:
            planned.append(_stats_plan(slide))
            continue
        if layout == "takeaways":
            planned.extend(_plan_takeaways(slide))
            continue
        if slide.bullets:
            planned.extend(_plan_bullets(slide))

    if sources:
        heights = [text_height(_source_label(t, u), 15, CONTENT_W - 0.6, after_pt=8) for t, u in sources]
        chunks = split_balanced(heights, BODY_BOTTOM - BODY_TOP)
        start = 0
        for number, chunk in enumerate(chunks):
            planned.append(
                Planned(
                    kind="sources", title="Kaynaklar", continued=number > 0,
                    items=[sources[i] for i in chunk], number=start,
                )
            )
            start += len(chunk)
    return planned


def _host(url: str) -> str:
    host = urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host


def _source_label(title: str, url: str) -> str:
    host = _host(url)
    if title and title.casefold() != host.casefold():
        return f"{title} — {host}"
    return url if len(url) <= 90 else host


# --- Drawing -------------------------------------------------------------------


def _rgb(hex_color: str):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _in(value: float):
    from pptx.util import Inches

    return Inches(value)


def _rect(slide, left, top, width, height, color: str):
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _in(left), _in(top), _in(width), _in(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _frame(slide, left, top, width, height, *, anchor: str = "top"):
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE

    box = slide.shapes.add_textbox(_in(left), _in(top), _in(width), _in(height))
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[anchor]
    return box, tf


def _para(tf, first: bool):
    return tf.paragraphs[0] if first else tf.add_paragraph()


def _runs(paragraph, text: str, *, size: float, color: str, bold: bool = False, accent: str = ACCENT) -> None:
    """Write `text` into `paragraph`, turning **marks** into bold runs."""
    from pptx.util import Pt

    pieces = re.split(r"(\*\*.+?\*\*)", text or "")
    for piece in pieces:
        if not piece:
            continue
        strong = piece.startswith("**") and piece.endswith("**") and len(piece) > 4
        run = paragraph.add_run()
        run.text = piece[2:-2] if strong else piece
        run.font.name = FONT
        run.font.size = Pt(size)
        run.font.bold = bold or strong
        run.font.color.rgb = _rgb(TEXT if strong and not bold else color)


def _spacing(paragraph, size: float, after: float = 0.0) -> None:
    from pptx.util import Pt

    paragraph.line_spacing = Pt(round(size * LINE, 1))
    paragraph.space_after = Pt(after)
    paragraph.space_before = Pt(0)


def _text(slide, left, top, width, height, text, *, size, color=TEXT, bold=False, anchor="top", align=None):
    from pptx.enum.text import PP_ALIGN

    box, tf = _frame(slide, left, top, width, height, anchor=anchor)
    paragraph = tf.paragraphs[0]
    _spacing(paragraph, size)
    if align == "center":
        paragraph.alignment = PP_ALIGN.CENTER
    elif align == "right":
        paragraph.alignment = PP_ALIGN.RIGHT
    _runs(paragraph, text, size=size, color=color, bold=bold)
    return box


def _set_bullet(paragraph, *, indent: float, margin: float, color: str, char: str = "•") -> None:
    from lxml import etree
    from pptx.oxml.ns import qn
    from pptx.util import Inches

    pPr = paragraph._p.get_or_add_pPr()
    pPr.set("marL", str(int(Inches(margin))))
    pPr.set("indent", str(-int(Inches(indent))))
    for tag in ("a:buClr", "a:buSzPct", "a:buFont", "a:buChar", "a:buNone", "a:buAutoNum"):
        for old in pPr.findall(qn(tag)):
            pPr.remove(old)
    # Schema order after lnSpc/spcBef/spcAft: buClr, buSzPct, buFont, buChar.
    clr = etree.SubElement(pPr, qn("a:buClr"))
    etree.SubElement(clr, qn("a:srgbClr")).set("val", color)
    etree.SubElement(pPr, qn("a:buSzPct")).set("val", "100000")
    etree.SubElement(pPr, qn("a:buFont")).set("typeface", "Arial")
    etree.SubElement(pPr, qn("a:buChar")).set("char", char)


def _bullets(slide, left, top, width, height, bullets: Sequence[Bullet], size: float) -> None:
    box, tf = _frame(slide, left, top, width, height)
    indent = _bullet_indent(size)
    for index, bullet in enumerate(bullets):
        sub = bullet.level > 0
        font = size - 2 if sub else size
        paragraph = _para(tf, index == 0)
        _spacing(paragraph, font, font * 0.55)
        _runs(paragraph, bullet.text, size=font, color=MUTED if sub else BULLET_TEXT)
        _set_bullet(
            paragraph,
            indent=indent,
            margin=indent * (2.2 if sub else 1),
            color=DIM if sub else ACCENT,
            char="–" if sub else "•",
        )


def _chrome(slide, title: str, planned: Planned, deck_title: str, page: int, total: int) -> float:
    """Background, rail, title, rule and footer. Returns the body top."""
    _background(slide, BG)
    _rect(slide, 0, 0, 0.12, SLIDE_H, ACCENT)
    body_top = BODY_TOP
    if title:
        size, _lines = _title_size(title + (" (devam)" if planned.continued else ""))
        box, tf = _frame(slide, LEFT, TITLE_TOP, CONTENT_W, TITLE_H, anchor="bottom")
        paragraph = tf.paragraphs[0]
        _spacing(paragraph, size)
        _runs(paragraph, title, size=size, color=TEXT, bold=True)
        if planned.continued:
            _runs(paragraph, " (devam)", size=size * 0.7, color=DIM)
        _rect(slide, LEFT, RULE_Y, 1.4, 0.05, ACCENT)
    slide_obj = planned.slide
    if slide_obj is not None and slide_obj.subtitle and planned.kind not in {"section", "quote"}:
        height = text_height(slide_obj.subtitle, 18, CONTENT_W)
        _text(slide, LEFT, BODY_TOP, CONTENT_W, height + 0.05, slide_obj.subtitle, size=18, color=ACCENT_WARM)
        body_top = BODY_TOP + height + SUBTITLE_GAP
    _footer(slide, deck_title, page, total)
    return body_top


def _footer(slide, deck_title: str, page: int, total: int) -> None:
    label = deck_title if len(deck_title) <= 80 else deck_title[:77].rstrip() + "…"
    _text(slide, LEFT, FOOTER_Y, CONTENT_W - 1.2, 0.3, label, size=10, color=DIM)
    _text(slide, SLIDE_W - RIGHT - 1.2, FOOTER_Y, 1.2, 0.3, f"{page} / {total}", size=10, color=DIM, align="right")


def _background(slide, color: str) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(color)


def _notes(slide, text: str) -> None:
    if text and text.strip():
        slide.notes_slide.notes_text_frame.text = text.strip()


def _draw_title(slide, deck: Deck, images: Sequence[Any]) -> None:
    _background(slide, BG)
    _rect(slide, 0, 0, 0.12, SLIDE_H, ACCENT)
    photo = next((img for img in images or [] if _usable_image(img)), None)
    text_w = 6.5 if photo is not None else 11.0
    if photo is not None:
        _draw_photo(slide, photo)
    title = deck.title or "Sunum"
    size = next(
        (s for s in (48, 44, 40, 36, 32, 28) if wrap_count(title, s, text_w, bold=True) <= 3),
        28,
    )
    lines = wrap_count(title, size, text_w, bold=True)
    title_h = lines * size * LINE / 72
    top = 2.0 if lines <= 2 else 1.6
    _text(slide, LEFT + 0.15, top - 0.55, text_w, 0.35, "SUNUM", size=13, color=MUTED, bold=True)
    _text(slide, LEFT + 0.15, top, text_w, title_h + 0.05, title, size=size, color=ACCENT, bold=True)
    y = top + title_h + 0.3
    _rect(slide, LEFT + 0.15, y, 2.2, 0.05, ACCENT)
    y += 0.35
    if deck.subtitle:
        sub_size = next((s for s in (22, 20, 18, 16) if wrap_count(deck.subtitle, s, text_w) <= 4), 16)
        height = text_height(deck.subtitle, sub_size, text_w)
        _text(slide, LEFT + 0.15, y, text_w, height + 0.05, deck.subtitle, size=sub_size, color=ACCENT_WARM)
    today = date.today()
    _text(
        slide, LEFT + 0.15, 6.75, text_w, 0.35,
        f"{today.day} {_MONTHS[today.month - 1]} {today.year}", size=13, color=DIM,
    )


def _usable_image(image: Any) -> bool:
    path = getattr(image, "path", None)
    return bool(path and Path(path).is_file())


def _draw_photo(slide, image: Any) -> None:
    """The topic photo as the title slide's right panel, cropped to cover."""
    from PIL import Image as PILImage

    left, width, height = 7.95, SLIDE_W - 7.95, SLIDE_H
    path = str(getattr(image, "path"))
    try:
        with PILImage.open(path) as pil:
            native_w, native_h = pil.size
        picture = slide.shapes.add_picture(path, _in(left), 0, _in(width), _in(height))
    except Exception:  # noqa: BLE001 - re-encode once, then do without
        try:
            with PILImage.open(path) as pil:
                native_w, native_h = pil.size
                buffer = io.BytesIO()
                pil.convert("RGB").save(buffer, format="JPEG", quality=85)
            buffer.seek(0)
            picture = slide.shapes.add_picture(buffer, _in(left), 0, _in(width), _in(height))
        except Exception:  # noqa: BLE001
            return
    image_aspect = native_w / native_h if native_h else 1.0
    box_aspect = width / height
    if image_aspect > box_aspect:
        crop = (1 - box_aspect / image_aspect) / 2
        picture.crop_left = picture.crop_right = crop
    else:
        crop = (1 - image_aspect / box_aspect) / 2
        picture.crop_top = picture.crop_bottom = crop
    attribution = " ".join(str(getattr(image, "attribution", "") or "").split())
    if attribution:
        short = re.split(r"\s+Kaynak:\s+|\s+https?://", attribution)[0][:160]
        band_h = max(0.32, text_height(short, 9, width - 0.3) + 0.14)
        _rect(slide, left, SLIDE_H - band_h, width, band_h, BG)
        _text(slide, left + 0.15, SLIDE_H - band_h + 0.07, width - 0.3, band_h - 0.1, short, size=9, color=MUTED)
        _notes(slide, attribution)


def _draw_agenda(slide, planned: Planned) -> None:
    items = planned.items
    columns = 2 if len(items) > 5 else 1
    per_column = math.ceil(len(items) / columns)
    col_w = (CONTENT_W - 0.5 * (columns - 1)) / columns
    size = next(
        (
            s for s in (24, 22, 20, 18, 16)
            if max(
                sum(text_height(t, s, col_w - 0.8, after_pt=s * 0.9) for t in items[c * per_column:(c + 1) * per_column])
                for c in range(columns)
            ) <= BODY_BOTTOM - BODY_TOP
        ),
        16,
    )
    for column in range(columns):
        chunk = items[column * per_column:(column + 1) * per_column]
        box, tf = _frame(slide, LEFT + column * (col_w + 0.5), BODY_TOP, col_w, BODY_BOTTOM - BODY_TOP)
        for index, item in enumerate(chunk):
            paragraph = _para(tf, index == 0)
            _spacing(paragraph, size, size * 0.9)
            number = column * per_column + index + 1
            _set_hanging(paragraph, 0.8)
            _runs(paragraph, f"{number:02d}\t", size=size, color=ACCENT, bold=True)
            _runs(paragraph, item, size=size, color=TEXT)


def _set_hanging(paragraph, indent: float) -> None:
    from pptx.util import Inches

    pPr = paragraph._p.get_or_add_pPr()
    pPr.set("marL", str(int(Inches(indent))))
    pPr.set("indent", str(-int(Inches(indent))))


def _draw_section(slide, planned: Planned, deck_title: str, page: int, total: int) -> None:
    _background(slide, BG_SOFT)
    _rect(slide, 0, 0, 0.12, SLIDE_H, ACCENT)
    slide_obj = planned.slide
    if planned.number:
        _text(slide, LEFT + 0.15, 2.0, 3.0, 64 * LINE / 72 + 0.05, f"{planned.number:02d}", size=64, color=ACCENT, bold=True)
    else:
        _rect(slide, LEFT + 0.15, 2.95, 1.4, 0.06, ACCENT)
    size = next((s for s in (44, 40, 36, 32) if wrap_count(planned.title, s, 10.5, bold=True) <= 2), 32)
    height = wrap_count(planned.title, size, 10.5, bold=True) * size * LINE / 72
    _text(slide, LEFT + 0.15, 3.25, 10.5, height + 0.05, planned.title, size=size, color=TEXT, bold=True)
    if slide_obj is not None and slide_obj.subtitle:
        y = 3.25 + height + 0.25
        _text(
            slide, LEFT + 0.15, y, 10.5, text_height(slide_obj.subtitle, 20, 10.5) + 0.05,
            slide_obj.subtitle, size=20, color=MUTED,
        )
    _footer(slide, deck_title, page, total)


def _draw_table(slide, planned: Planned, top: float) -> None:
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

    slide_obj = planned.slide
    columns = slide_obj.columns
    rows = planned.rows
    height = planned.header_height + sum(planned.row_heights)
    frame = slide.shapes.add_table(
        len(rows) + 1, len(columns), _in(LEFT), _in(top), _in(CONTENT_W), _in(height)
    )
    table = frame.table
    _plain_table_style(table)
    table.first_row = True
    table.horz_banding = False
    numeric = [_is_numeric_column(slide_obj.rows, i) for i in range(len(columns))]
    for index, width in enumerate(planned.col_widths):
        table.columns[index].width = _in(width)
    table.rows[0].height = _in(planned.header_height)
    for index, row_height in enumerate(planned.row_heights):
        table.rows[index + 1].height = _in(row_height)
    for r in range(len(rows) + 1):
        values = columns if r == 0 else rows[r - 1]
        for c in range(len(columns)):
            cell = table.cell(r, c)
            cell.margin_left = cell.margin_right = _in(CELL_PAD_X)
            cell.margin_top = cell.margin_bottom = _in(CELL_PAD_Y)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(ACCENT if r == 0 else (CARD if r % 2 else CARD_ALT))
            tf = cell.text_frame
            tf.word_wrap = True
            paragraph = tf.paragraphs[0]
            _spacing(paragraph, planned.size)
            if numeric[c]:
                paragraph.alignment = PP_ALIGN.RIGHT
            value = str(values[c] if c < len(values) else "")
            _runs(paragraph, value, size=planned.size, color=BG if r == 0 else TEXT, bold=r == 0)


def _plain_table_style(table) -> None:
    """"No Style, No Grid": the fills below are the whole design."""
    from pptx.oxml.ns import qn

    tbl_pr = table._tbl.tblPr
    style = tbl_pr.find(qn("a:tableStyleId"))
    if style is None:
        from lxml import etree

        style = etree.SubElement(tbl_pr, qn("a:tableStyleId"))
    style.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"


def _draw_chart(slide, planned: Planned, top: float) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
    from pptx.util import Pt

    slide_obj = planned.slide
    chart_spec: Chart = slide_obj.chart
    side = [b for b in slide_obj.bullets if b.level == 0][:3]
    width = CONTENT_W - (3.7 if side else 0)
    height = BODY_BOTTOM - top
    data = CategoryChartData()
    data.categories = chart_spec.categories
    values = [v for s in chart_spec.series for v in s.values if v is not None]
    integers = all(float(v).is_integer() for v in values)
    large = max((abs(v) for v in values), default=0) >= 100
    number_format = "#,##0" if integers or large else "#,##0.0#"
    for index, series in enumerate(chart_spec.series):
        data.add_series(series.name or f"Seri {index + 1}", series.values, number_format)
    points = len(chart_spec.categories)
    chart_type = {
        "line": XL_CHART_TYPE.LINE_MARKERS if points <= 24 else XL_CHART_TYPE.LINE,
        "bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "pie": XL_CHART_TYPE.PIE,
    }[chart_spec.kind]
    frame = slide.shapes.add_chart(chart_type, _in(LEFT), _in(top), _in(width), _in(height), data)
    chart = frame.chart
    chart.has_title = False
    _no_fill(chart._chartSpace, after="c:chart")
    chart.font.name = FONT
    chart.font.size = Pt(13)
    chart.font.color.rgb = _rgb(MUTED)
    multi = len(chart_spec.series) > 1 or chart_spec.kind == "pie"
    chart.has_legend = multi
    if multi:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.color.rgb = _rgb(TEXT)
        chart.legend.font.size = Pt(13)
    plot = chart.plots[0]
    if chart_spec.kind == "pie":
        for index, point in enumerate(plot.series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = _rgb(SERIES_COLORS[index % len(SERIES_COLORS)])
            point.format.line.color.rgb = _rgb(BG)
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.show_percentage = True
        labels.show_value = False
        labels.number_format = "0%"
        labels.number_format_is_linked = False
        labels.font.size = Pt(13)
        labels.font.color.rgb = _rgb(BG)
        labels.font.bold = True
    else:
        for index, series in enumerate(plot.series):
            color = _rgb(SERIES_COLORS[index % len(SERIES_COLORS)])
            if chart_spec.kind == "line":
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.75)
                series.smooth = False
                if points <= 24:
                    from pptx.enum.chart import XL_MARKER_STYLE

                    series.marker.style = XL_MARKER_STYLE.CIRCLE
                    series.marker.size = 6
                    series.marker.format.fill.solid()
                    series.marker.format.fill.fore_color.rgb = color
                    series.marker.format.line.color.rgb = color
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
        if chart_spec.kind in {"bar", "column"}:
            plot.gap_width = 70
            if points <= 12 and len(chart_spec.series) == 1:
                plot.has_data_labels = True
                labels = plot.data_labels
                labels.number_format = number_format
                labels.number_format_is_linked = False
                labels.position = XL_LABEL_POSITION.OUTSIDE_END
                labels.font.size = Pt(12)
                labels.font.color.rgb = _rgb(TEXT)
        value_axis = chart.value_axis
        value_axis.has_major_gridlines = True
        value_axis.major_gridlines.format.line.color.rgb = _rgb(GRID)
        value_axis.format.line.fill.background()
        value_axis.tick_labels.font.color.rgb = _rgb(MUTED)
        value_axis.tick_labels.font.size = Pt(12)
        value_axis.tick_labels.number_format = number_format
        value_axis.tick_labels.number_format_is_linked = False
        floor = axis_floor(values) if chart_spec.kind == "line" else None
        if floor is not None:
            value_axis.minimum_scale = floor
        if chart_spec.unit:
            value_axis.has_title = True
            value_axis.axis_title.text_frame.text = chart_spec.unit
            for paragraph in value_axis.axis_title.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(12)
                    run.font.color.rgb = _rgb(MUTED)
                    run.font.bold = False
        category_axis = chart.category_axis
        category_axis.format.line.color.rgb = _rgb(GRID)
        category_axis.tick_labels.font.color.rgb = _rgb(MUTED)
        category_axis.tick_labels.font.size = Pt(12)
        category_axis.has_major_gridlines = False
    if side:
        size = next(
            (s for s in (20, 18, 16, 14) if sum(bullet_height(b, s, 3.3) for b in side) <= height),
            14,
        )
        _bullets(slide, LEFT + width + 0.4, top + 0.2, 3.3, height - 0.2, side, size)


def _nice_step(span: float) -> float:
    if span <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(span))
    for factor in (1, 2, 2.5, 5, 10):
        if factor * magnitude >= span:
            return factor * magnitude
    return 10 * magnitude


def axis_floor(values: Sequence[float]) -> Optional[float]:
    """A line chart's value-axis minimum, or None to start at zero.

    Prices that move between 5,994 and 7,566 drawn from zero are a flat
    line; the trend the slide is about only shows with the axis starting
    near the data. Only for lines, only for all-positive data that sits
    well away from zero -- bars keep their zero baseline, since a bar's
    length is the value.
    """
    if not values:
        return None
    low, high = min(values), max(values)
    if low <= 0 or high - low >= 0.5 * high:
        return None
    step = _nice_step((high - low) / 5)
    floor = math.floor((low - (high - low) * 0.15) / step) * step
    return floor if floor > 0 else None


def _no_fill(element, *, after: str) -> None:
    """Give a chart part an explicit no-fill spPr (else viewers paint white)."""
    from lxml import etree
    from pptx.oxml.ns import qn

    existing = element.find(qn("c:spPr"))
    if existing is not None:
        element.remove(existing)
    sp_pr = etree.Element(qn("c:spPr"))
    etree.SubElement(sp_pr, qn("a:noFill"))
    line = etree.SubElement(sp_pr, qn("a:ln"))
    etree.SubElement(line, qn("a:noFill"))
    anchor = element.find(qn(after))
    if anchor is not None:
        anchor.addnext(sp_pr)
    else:
        element.append(sp_pr)


def _draw_stats(slide, planned: Planned, top: float) -> None:
    stats = planned.slide.stats
    n = len(stats)
    gap = 0.35
    card_w = (CONTENT_W - gap * (n - 1)) / n
    extra = [b for b in planned.slide.bullets]
    max_h = min(3.6, BODY_BOTTOM - top - (1.3 if extra else 0.2))
    value_h = _stat_value_height(stats, planned.size, card_w - 0.5)

    def content_h(size: float) -> float:
        return max(
            (text_height(st.label, size, card_w - 0.5) if st.label else 0)
            + (text_height(st.detail, 13, card_w - 0.5) + 0.1 if st.detail else 0)
            for st in stats
        )

    label_size = next(
        (s for s in (22, 20, 18, 16, 14) if value_h + 0.85 + content_h(s) <= max_h), 14
    )
    card_h = min(max_h, max(2.0, value_h + 0.85 + content_h(label_size)))
    card_top = top + 0.15 if extra else top + max(0.15, (BODY_BOTTOM - top - card_h) / 2 - 0.2)
    for index, stat in enumerate(stats):
        left = LEFT + index * (card_w + gap)
        _rect(slide, left, card_top, card_w, card_h, CARD)
        _rect(slide, left, card_top, card_w, 0.06, ACCENT if index % 2 == 0 else ACCENT_WARM)
        _text(
            slide, left + 0.25, card_top + 0.35, card_w - 0.5, value_h + 0.05, stat.value,
            size=planned.size, color=ACCENT if index % 2 == 0 else ACCENT_WARM, bold=True,
        )
        y = card_top + 0.35 + value_h + 0.15
        if stat.label:
            height = text_height(stat.label, label_size, card_w - 0.5)
            _text(slide, left + 0.25, y, card_w - 0.5, height + 0.05, stat.label, size=label_size, color=TEXT)
            y += height + 0.1
        if stat.detail:
            height = text_height(stat.detail, 13, card_w - 0.5)
            _text(slide, left + 0.25, y, card_w - 0.5, height + 0.05, stat.detail, size=13, color=MUTED)
    if extra:
        space = BODY_BOTTOM - (card_top + card_h + 0.3)
        size = next(
            (s for s in (20, 18, 16, 14) if sum(bullet_height(b, s, CONTENT_W) for b in extra) <= space),
            None,
        )
        if size is not None:
            _bullets(slide, LEFT, card_top + card_h + 0.3, CONTENT_W, space, extra, size)


def _draw_two_column(slide, planned: Planned, top: float) -> None:
    gap = 0.45
    col_w = (CONTENT_W - gap) / 2
    size = planned.size
    inner = col_w - 0.5

    def column_h(box) -> float:
        if box is None:
            return 0.0
        heading = text_height(box.heading, size, inner, bold=True) + 0.2 if box.heading else 0.0
        return heading + sum(bullet_height(b, size, inner) for b in box.bullets)

    height = min(
        BODY_BOTTOM - top,
        max(2.4, max(column_h(planned.slide.left), column_h(planned.slide.right)) + 0.6),
    )
    for index, box in enumerate((planned.slide.left, planned.slide.right)):
        if box is None:
            continue
        left = LEFT + index * (col_w + gap)
        _rect(slide, left, top, col_w, height, CARD)
        _rect(slide, left, top, 0.06, height, ACCENT if index == 0 else ACCENT_WARM)
        y = top + 0.3
        if box.heading:
            heading_h = text_height(box.heading, size, col_w - 0.5, bold=True)
            _text(
                slide, left + 0.3, y, col_w - 0.5, heading_h + 0.05, box.heading,
                size=size, color=ACCENT if index == 0 else ACCENT_WARM, bold=True,
            )
            y += heading_h + 0.2
        _bullets(slide, left + 0.3, y, col_w - 0.5, top + height - y - 0.1, box.bullets, size)


def _draw_quote(slide, planned: Planned, top: float) -> None:
    slide_obj = planned.slide
    size = planned.size
    text = _plain(slide_obj.text)
    height = text_height(text, size, 10.2)
    block = height + (0.6 if slide_obj.attribution else 0)
    y = max(top, top + (BODY_BOTTOM - top - block) / 2)
    mark = 88
    _text(slide, LEFT + 0.1, y - 0.55, 1.0, mark * LINE / 72 + 0.05, "“", size=mark, color=ACCENT, bold=True)
    _text(slide, LEFT + 1.05, y, 10.2, height + 0.05, slide_obj.text, size=size, color=TEXT)
    if slide_obj.attribution:
        _text(
            slide, LEFT + 1.05, y + height + 0.2, 10.2, 0.4,
            "— " + slide_obj.attribution, size=16, color=ACCENT_WARM,
        )


def _draw_takeaways(slide, planned: Planned, top: float) -> None:
    size = planned.size
    text_w = CONTENT_W - 1.0
    y = top
    for offset, bullet in enumerate(planned.bullets):
        body_h = text_height(_plain(bullet.text), size, text_w - 0.3)
        card_h = max(0.62, body_h + 0.3)
        _rect(slide, LEFT, y, CONTENT_W, card_h, CARD)
        _rect(slide, LEFT, y, 0.06, card_h, ACCENT)
        number = planned.number + offset + 1
        _text(slide, LEFT + 0.25, y, 0.6, card_h, f"{number}", size=size + 4, color=ACCENT, bold=True, anchor="middle")
        _text(slide, LEFT + 0.95, y + 0.15, text_w - 0.3, card_h - 0.3, bullet.text, size=size, color=TEXT, anchor="middle")
        y += card_h + 0.16


def _draw_sources(slide, planned: Planned, top: float) -> None:
    from pptx.util import Pt

    box, tf = _frame(slide, LEFT, top, CONTENT_W, BODY_BOTTOM - top)
    for offset, (title, url) in enumerate(planned.items):
        paragraph = _para(tf, offset == 0)
        _spacing(paragraph, 15, 8)
        _set_hanging(paragraph, 0.6)
        _runs(paragraph, f"{planned.number + offset + 1:02d}\t", size=15, color=ACCENT, bold=True)
        label = _source_label(title, url)
        host = _host(url)
        main, _, rest = label.rpartition(" — ") if " — " in label else (label, "", "")
        run = paragraph.add_run()
        run.text = main
        run.font.name = FONT
        run.font.size = Pt(15)
        run.font.color.rgb = _rgb(TEXT)
        run.hyperlink.address = url
        if rest:
            _runs(paragraph, f" — {host}", size=13, color=DIM)


def _theme_link_colors(prs) -> None:
    """Point the theme's hyperlink colours at the accent.

    PowerPoint draws a link in the theme's hlink colour whatever the run
    says, and the default theme's is a dark blue that disappears on this
    background.
    """
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    try:
        theme = prs.slide_master.part.part_related_by(RT.THEME)
        blob = theme.blob.decode("utf-8")
        for tag, color in (("hlink", ACCENT), ("folHlink", ACCENT_WARM)):
            blob = re.sub(
                rf"(<a:{tag}>\s*)<a:(?:srgbClr|sysClr)[^>]*?/>",
                rf'\1<a:srgbClr val="{color}"/>',
                blob,
                count=1,
            )
        theme._blob = blob.encode("utf-8")
    except Exception:  # noqa: BLE001 - cosmetic; the default colour still works
        pass


def build_pptx(
    deck: Deck,
    *,
    images: Optional[Sequence[Any]] = None,
    sources: Sequence[tuple[str, str]] = (),
) -> bytes:
    """The deck as .pptx bytes."""
    from pptx import Presentation

    planned = plan_deck(deck, sources=sources)
    prs = Presentation()
    _theme_link_colors(prs)
    prs.slide_width = _in(SLIDE_W)
    prs.slide_height = _in(SLIDE_H)
    blank = prs.slide_layouts[6]
    deck_title = deck.title or "Sunum"
    total = len(planned)
    for page, item in enumerate(planned, start=1):
        slide = prs.slides.add_slide(blank)
        if item.kind == "title":
            _draw_title(slide, deck, images or [])
            continue
        if item.kind == "section":
            _draw_section(slide, item, deck_title, page, total)
            _notes(slide, item.slide.notes if item.slide else "")
            continue
        top = _chrome(slide, item.title, item, deck_title, page, total)
        if item.kind == "agenda":
            _draw_agenda(slide, item)
        elif item.kind == "sources":
            _draw_sources(slide, item, top)
        elif item.kind == "table":
            _draw_table(slide, item, top)
        elif item.kind == "chart":
            _draw_chart(slide, item, top)
        elif item.kind == "stats":
            _draw_stats(slide, item, top)
        elif item.kind == "two_column":
            _draw_two_column(slide, item, top)
        elif item.kind == "quote":
            _draw_quote(slide, item, top)
        elif item.kind == "takeaways":
            _draw_takeaways(slide, item, top)
        else:
            _bullets(slide, LEFT, top, CONTENT_W, BODY_BOTTOM - top, item.bullets, item.size)
        if item.slide is not None and not item.continued:
            _notes(slide, item.slide.notes)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


__all__ = [
    "Planned",
    "build_pptx",
    "plan_deck",
    "split_balanced",
    "text_height",
    "text_width",
    "wrap_count",
]
