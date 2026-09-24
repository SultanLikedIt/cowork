"""Typed document model for PDF/DOCX deliverables, and the renderers for it.

Why this module exists
----------------------
The Document LLM used to hand the server one markdown string, and
document_export then *guessed* its structure back out of it: regexes for
"(1) ... (2) ..." enumerations, wall-of-text sentence splitting, jammed pipe
tables, and "is this short numbered line a heading?". Every new model habit
needed another pattern, and some guesses were simply wrong -- a paragraph
starting "3 ana bulgu var" could come out as a heading.

Here the model states the structure explicitly as an ordered list of typed
blocks (heading / paragraph / list / table / quote / image). Nothing about
structure is inferred, so the renderers below only *draw* what they are told.

Parsing is deliberately lenient: small models send aliases ("ul", "h2",
"bullets"), list items as bare strings, table rows as objects, or a whole
block as a plain string. Each is normalized; a block that cannot be
understood is dropped on its own instead of failing the whole document.

Gated by the DOCUMENT_BLOCKS environment flag. With it off, nothing here is
reached and the markdown path in document_export is used exactly as before.
It also stays the fallback whenever a model reply carries no usable blocks.
"""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any, Callable, Literal, Optional, Sequence, Union

from pydantic import BaseModel, Field

import brand
import document_export

# --- Bounds ---------------------------------------------------------------
MAX_BLOCKS = 400
MAX_TEXT_CHARS = 20_000
MAX_LIST_ITEMS = 200
MAX_LIST_LEVEL = 2
MAX_HEADING_LEVEL = 3
# Same limits the markdown writers use, so switching paths never changes
# how much of a table survives.
MAX_TABLE_COLUMNS = 8
MAX_TABLE_ROWS = 500


def blocks_enabled() -> bool:
    """True when the Document worker should request and render typed blocks."""
    return os.environ.get("DOCUMENT_BLOCKS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# --- Model ----------------------------------------------------------------


class HeadingBlock(BaseModel):
    type: Literal["heading"] = "heading"
    text: str
    level: int = 1


class ParagraphBlock(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    text: str


class ListItem(BaseModel):
    text: str
    level: int = 0


class ListBlock(BaseModel):
    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[ListItem] = Field(default_factory=list)


class TableBlock(BaseModel):
    type: Literal["table"] = "table"
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class QuoteBlock(BaseModel):
    type: Literal["quote"] = "quote"
    text: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    caption: str = ""


Block = Union[HeadingBlock, ParagraphBlock, ListBlock, TableBlock, QuoteBlock, ImageBlock]


class DocumentModel(BaseModel):
    title: str = ""
    blocks: list[Block] = Field(default_factory=list)

    def has_content(self) -> bool:
        return any(
            not isinstance(block, (HeadingBlock, ImageBlock)) for block in self.blocks
        )

    def blocks_as_dicts(self) -> list[dict[str, Any]]:
        return [block.model_dump() for block in self.blocks]

    def to_markdown(self) -> str:
        """Canonical markdown for consumers that still read prose.

        The PPTX/HTML writers, the chat answer and the length checks in the
        Document worker all read `content` as markdown, so the blocks are
        written back out in exactly the shape those already understand.
        """
        parts: list[str] = []
        for block in self.blocks:
            if isinstance(block, HeadingBlock):
                parts.append("#" * (block.level + 1) + " " + _strip_bold(block.text))
            elif isinstance(block, (ParagraphBlock, QuoteBlock)):
                parts.append(block.text)
            elif isinstance(block, ListBlock):
                counters: dict[int, int] = {}
                lines = []
                for item in block.items:
                    counters[item.level] = counters.get(item.level, 0) + 1
                    for deeper in [lvl for lvl in counters if lvl > item.level]:
                        counters.pop(deeper)
                    marker = f"{counters[item.level]}." if block.ordered else "-"
                    lines.append("  " * item.level + f"{marker} {item.text}")
                parts.append("\n".join(lines))
            elif isinstance(block, TableBlock):
                def row_line(cells: Sequence[str]) -> str:
                    return "| " + " | ".join(
                        str(c).replace("|", "/").replace("\n", " ") for c in cells
                    ) + " |"

                lines = [row_line(block.columns), row_line(["---"] * len(block.columns))]
                lines.extend(row_line(row) for row in block.rows)
                parts.append("\n".join(lines))
        return "\n\n".join(part for part in parts if part.strip())


# --- Normalization --------------------------------------------------------

_HEADING_TYPES = {"heading", "header", "section", "title", "h1", "h2", "h3", "h4"}
_PARAGRAPH_TYPES = {"paragraph", "p", "para", "text", "body"}
_ORDERED_LIST_TYPES = {"ol", "numbered", "numbered_list", "ordered_list", "steps"}
_LIST_TYPES = {"list", "bullets", "bullet", "bullet_list", "ul", "items"} | _ORDERED_LIST_TYPES
_TABLE_TYPES = {"table", "grid"}
_QUOTE_TYPES = {"quote", "blockquote", "callout", "note", "highlight"}
_IMAGE_TYPES = {"image", "figure", "photo", "picture"}

_LEADING_LIST_MARK = re.compile(r"^\s*(?:[-*•·]|\d{1,3}[.)])\s+")
_LEADING_HASHES = re.compile(r"^\s*#{1,6}\s+")
_SOURCES_LABEL = re.compile(
    r"(?i)^\s*(?:kaynak(?:lar|ça|ca)?|sources?|references?)\s*:?\s*$"
)

TextFilter = Callable[[str], str]


def _strip_bold(text: str) -> str:
    return document_export.INLINE_MARKS.sub(r"\2", text or "")


def _clean_text(value: Any, *, keep_bold: bool = True) -> str:
    """One block's text: inline markdown resolved, no structural markup."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = document_export.HTML_TAG.sub("", text)
    text = document_export.HTML_IMG.sub("", text)
    text = " ".join(text.replace("​", "").split())
    # _clean_inline only preserves **markers** when asked; strip separately
    # afterwards when this text cannot carry bold (headings, table cells).
    text = document_export._clean_inline(text, keep_bold=True)
    if text.count("**") % 2:
        # An unpaired marker can never become bold; it would print as "**".
        cut = text.rfind("**")
        text = text[:cut] + text[cut + 2 :]
    if not keep_bold:
        text = _strip_bold(text)
    return text[:MAX_TEXT_CHARS].strip()


def _is_noise(text: str) -> bool:
    """Author notes and planning leaks that must never reach the reader."""
    return bool(
        document_export.IMAGE_INSTRUCTION_LINE.search(text)
        or document_export.PLANNING_LEAK.search(text)
    )


def _filtered(text: str, text_filter: Optional[TextFilter]) -> str:
    if not text or _is_noise(text):
        return ""
    if text_filter is not None:
        text = " ".join((text_filter(text) or "").split())
    return text


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _list_items(raw: Any, level: int, text_filter: Optional[TextFilter]) -> list[ListItem]:
    items: list[ListItem] = []
    if isinstance(raw, str):
        raw = [line for line in raw.split("\n") if line.strip()]
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if len(items) >= MAX_LIST_ITEMS:
            break
        if isinstance(entry, dict):
            text = _filtered(
                _clean_text(_LEADING_LIST_MARK.sub("", str(entry.get("text") or ""))),
                text_filter,
            )
            item_level = _clamp(entry.get("level", level), 0, MAX_LIST_LEVEL, level)
            if text:
                items.append(ListItem(text=text, level=item_level))
            children = entry.get("items") or entry.get("children")
            if children:
                items.extend(
                    _list_items(children, min(item_level + 1, MAX_LIST_LEVEL), text_filter)
                )
        elif isinstance(entry, list):
            # A bare nested array is a sublist of the previous item.
            items.extend(_list_items(entry, min(level + 1, MAX_LIST_LEVEL), text_filter))
        else:
            text = _filtered(_clean_text(_LEADING_LIST_MARK.sub("", str(entry))), text_filter)
            if text:
                items.append(ListItem(text=text, level=level))
    return items[:MAX_LIST_ITEMS]


def _table(entry: dict[str, Any]) -> Optional[TableBlock]:
    columns = entry.get("columns") or entry.get("headers") or entry.get("header") or []
    rows = entry.get("rows") or entry.get("data") or []
    if not isinstance(rows, list) or not rows:
        return None
    if isinstance(rows[0], dict):
        if not columns:
            seen: list[str] = []
            for record in rows:
                if isinstance(record, dict):
                    seen.extend(str(k) for k in record if str(k) not in seen)
            columns = seen
        rows = [
            [record.get(col) for col in columns] if isinstance(record, dict) else []
            for record in rows
        ]
    elif not columns and isinstance(rows[0], list):
        columns, rows = rows[0], rows[1:]
    if not isinstance(columns, list) or not columns:
        return None
    header = [_clean_text(c, keep_bold=False) for c in columns][:MAX_TABLE_COLUMNS]
    width = len(header)
    body: list[list[str]] = []
    for row in rows[:MAX_TABLE_ROWS]:
        if not isinstance(row, list):
            continue
        cells = [_clean_text(c, keep_bold=False) for c in row[:width]]
        cells.extend([""] * (width - len(cells)))
        if any(cells):
            body.append(cells)
    if not body:
        return None
    return TableBlock(columns=header, rows=body)


def _coerce_block(entry: Any, text_filter: Optional[TextFilter]) -> list[Block]:
    if isinstance(entry, str):
        # A bare string is prose; let the markdown reader find any structure
        # the model wrote into it anyway.
        return document_from_markdown("", entry).blocks
    if not isinstance(entry, dict):
        return []

    kind = str(entry.get("type") or entry.get("kind") or "").strip().lower()
    if not kind:
        if "rows" in entry:
            kind = "table"
        elif "items" in entry:
            kind = "list"
        elif "text" in entry:
            kind = "paragraph"

    if kind in _HEADING_TYPES:
        text = _clean_text(_LEADING_HASHES.sub("", str(entry.get("text") or "")), keep_bold=False)
        if not text or _is_noise(text):
            return []
        default_level = int(kind[1]) if re.fullmatch(r"h[1-4]", kind) else 1
        level = _clamp(entry.get("level", default_level), 1, MAX_HEADING_LEVEL, 1)
        return [HeadingBlock(text=text, level=level)]
    if kind in _PARAGRAPH_TYPES:
        text = _filtered(_clean_text(entry.get("text")), text_filter)
        return [ParagraphBlock(text=text)] if text else []
    if kind in _LIST_TYPES:
        ordered = bool(entry.get("ordered")) or kind in _ORDERED_LIST_TYPES
        items = _list_items(entry.get("items") or entry.get("text"), 0, text_filter)
        return [ListBlock(ordered=ordered, items=items)] if items else []
    if kind in _TABLE_TYPES:
        table = _table(entry)
        return [table] if table else []
    if kind in _QUOTE_TYPES:
        text = _filtered(_clean_text(entry.get("text")), text_filter)
        return [QuoteBlock(text=text)] if text else []
    if kind in _IMAGE_TYPES:
        return [ImageBlock(caption=_clean_text(entry.get("caption"), keep_bold=False))]
    return []


def _drop_model_sources(blocks: list[Block]) -> list[Block]:
    """Cut a model-written sources section; the server appends verified URLs.

    Left in, the Document worker's source injection strips it from the
    markdown copy but not from the blocks, and the two would disagree.
    """
    for index, block in enumerate(blocks):
        text = getattr(block, "text", "")
        if isinstance(block, (HeadingBlock, ParagraphBlock)) and _SOURCES_LABEL.match(text):
            return blocks[:index]
    return blocks


def _finalize(title: str, blocks: list[Block]) -> DocumentModel:
    blocks = blocks[:MAX_BLOCKS]
    want = " ".join(_strip_bold(title).split()).casefold()
    if blocks and want and isinstance(blocks[0], (HeadingBlock, ParagraphBlock)):
        if " ".join(_strip_bold(blocks[0].text).split()).casefold() == want:
            blocks = blocks[1:]
    return DocumentModel(title=title, blocks=blocks)


def parse_blocks_value(value: Any) -> Optional[list[Any]]:
    """The raw block array from whatever shape the caller passed."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        value = value.get("blocks")
    return value if isinstance(value, list) else None


def document_from_blocks(
    title: str,
    raw_blocks: Any,
    *,
    text_filter: Optional[TextFilter] = None,
    drop_sources: bool = False,
) -> Optional[DocumentModel]:
    """A validated document from model-supplied blocks, or None if unusable.

    ``drop_sources`` cuts a model-written sources section. Only the Document
    worker wants that (it appends verified URLs itself); a caller rendering
    blocks that already carry those URLs must keep them.
    """
    entries = parse_blocks_value(raw_blocks)
    if not entries:
        return None
    blocks: list[Block] = []
    for entry in entries[: MAX_BLOCKS * 2]:
        blocks.extend(_coerce_block(entry, text_filter))
    if drop_sources:
        blocks = _drop_model_sources(blocks)
    document = _finalize(_clean_text(title, keep_bold=False), blocks)
    return document if document.has_content() else None


def document_from_markdown(title: str, body: str) -> DocumentModel:
    """The same model built from markdown, via document_export's reader.

    Used for the server-appended sources tail and for bare-string blocks. It
    is not used for the main body: that still goes through the legacy writer
    when the model sends no blocks.
    """
    blocks: list[Block] = []
    for kind, text, level in document_export.iter_body_blocks(
        body or "", title=title, keep_bold=True
    ):
        if kind == "table" and isinstance(text, list) and len(text) >= 2:
            table = _table({"columns": text[0], "rows": text[1:]})
            if table:
                blocks.append(table)
            continue
        value = str(text or "").strip()
        if not value:
            continue
        if kind == "heading":
            blocks.append(
                HeadingBlock(text=_strip_bold(value), level=_clamp(level - 1, 1, MAX_HEADING_LEVEL, 1))
            )
        elif kind in {"bullet", "number"}:
            ordered = kind == "number"
            item = ListItem(text=value, level=_clamp(level, 0, MAX_LIST_LEVEL, 0))
            last = blocks[-1] if blocks else None
            if isinstance(last, ListBlock) and last.ordered == ordered:
                last.items.append(item)
            else:
                blocks.append(ListBlock(ordered=ordered, items=[item]))
        elif _SOURCES_LABEL.match(value):
            blocks.append(HeadingBlock(text="Kaynaklar", level=1))
        else:
            blocks.append(ParagraphBlock(text=value))
    return DocumentModel(title=title, blocks=blocks[:MAX_BLOCKS])


# --- Keeping blocks and markdown in step ----------------------------------
#
# The Document worker keeps editing `content` after the reply is parsed: it
# may replace a too-short write-up with the research brief, strip refusal
# lines, and append a verified Kaynaklar block. The blocks are only still the
# right thing to render if `content` is, apart from that appended tail, the
# markdown they produced.

BLOCKS_MARKDOWN_KEY = "_blocks_markdown"
_TRAILING_SOURCES = re.compile(r"(?im)^\s*kaynak(?:lar|ça|ca)?\s*:?\s*$")


def _fingerprint(text: str) -> str:
    cleaned = document_export.sanitize_report_prose(text or "")
    cleaned = re.sub(r"[#*_|`>\-.:;,()\[\]]+", " ", cleaned)
    return " ".join(cleaned.split()).casefold()


def split_trailing_sources(content: str) -> tuple[str, str]:
    """(body, sources tail) split at the LAST standalone Kaynaklar line."""
    matches = list(_TRAILING_SOURCES.finditer(content or ""))
    if not matches:
        return content or "", ""
    start = matches[-1].start()
    return content[:start], content[start:]


def attach_blocks_to_payload(
    payload: dict[str, Any], *, text_filter: Optional[TextFilter] = None
) -> Optional[DocumentModel]:
    """Validate payload["blocks"] and mirror it into payload["content"].

    Returns the document, or None (and removes the key) when the blocks are
    missing or unusable, so every later step sees the legacy payload shape.
    """
    raw = payload.get("blocks")
    if raw is None:
        return None
    document = document_from_blocks(
        str(payload.get("title") or ""), raw, text_filter=text_filter, drop_sources=True
    )
    if document is None:
        payload.pop("blocks", None)
        return None
    markdown = document.to_markdown()
    payload["blocks"] = document.blocks_as_dicts()
    payload["content"] = markdown
    payload["body"] = markdown
    payload[BLOCKS_MARKDOWN_KEY] = markdown
    if not str(payload.get("summary") or "").strip():
        first = next(
            (b.text for b in document.blocks if isinstance(b, ParagraphBlock)), ""
        )
        payload["summary"] = _strip_bold(first)[:600]
    return document


def resolve_payload_document(payload: dict[str, Any]) -> Optional[DocumentModel]:
    """The document to render for this payload, or None for the legacy path."""
    raw = payload.get("blocks")
    marker = str(payload.get(BLOCKS_MARKDOWN_KEY) or "")
    if not raw or not marker:
        return None
    content = str(payload.get("content") or payload.get("body") or "")
    body, tail = split_trailing_sources(content)
    if _fingerprint(body) != _fingerprint(marker):
        return None
    document = document_from_blocks(str(payload.get("title") or ""), raw)
    if document is None:
        return None
    if tail.strip():
        document.blocks.extend(document_from_markdown("", tail).blocks)
    return document


# --- DOCX -----------------------------------------------------------------


def _docx_add_image(document: Any, image: Any, caption: str = "") -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    if not getattr(image, "path", None) or not image.path.is_file():
        return
    try:
        document.add_picture(str(image.path), width=Cm(14))
    except Exception:  # noqa: BLE001 - retry once through Pillow
        try:
            from PIL import Image as PILImage

            with PILImage.open(image.path) as pil:
                buffer = io.BytesIO()
                pil.convert("RGB").save(buffer, format="JPEG", quality=82)
            buffer.seek(0)
            document.add_picture(buffer, width=Cm(14))
        except Exception:  # noqa: BLE001
            return
    document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    note = " — ".join(part for part in (caption, getattr(image, "attribution", "")) if part)
    if note:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(note)
        run.italic = True
        run.font.size = Pt(8)


def _docx_style(document: Any, *candidates: str) -> Optional[str]:
    names = {style.name for style in document.styles}
    return next((name for name in candidates if name in names), None)


def _docx_new_numbering(document: Any, style_name: str) -> Optional[int]:
    """A fresh numId for `style_name`'s list, restarting at 1.

    Word keeps one counter per numId, and every "List Number" paragraph
    shares the style's numId -- so a second numbered list in the same file
    carried on from where the first stopped (4, 5, 6 ...).
    """
    try:
        style = document.styles[style_name]
        num_pr = style.element.pPr.numPr
        numbering = document.part.numbering_part.element
        current = next(n for n in numbering.num_lst if n.numId == num_pr.numId.val)
        fresh = numbering.add_num(current.abstractNumId.val)
        fresh.add_lvlOverride(ilvl=0).add_startOverride(1)
        return int(fresh.numId)
    except Exception:  # noqa: BLE001 - continuing numbering is still readable
        return None


def _docx_set_num_id(paragraph: Any, num_id: int) -> None:
    num_pr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = 0
    num_pr.get_or_add_numId().val = num_id


def _docx_add_list(document: Any, block: ListBlock) -> None:
    base = "List Number" if block.ordered else "List Bullet"
    num_id = _docx_new_numbering(document, base) if block.ordered else None
    for item in block.items:
        suffix = "" if item.level == 0 else f" {min(item.level + 1, 3)}"
        style = _docx_style(document, base + suffix, base)
        paragraph = document.add_paragraph(style=style)
        document_export._add_runs_with_bold(paragraph, item.text)
        if num_id is not None and item.level == 0:
            _docx_set_num_id(paragraph, num_id)


def _docx_add_table(document: Any, block: TableBlock) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document_export._add_docx_matrix_table(document, [block.columns, *block.rows])
    table = document.tables[-1]
    # Repeat the header row on every page a long table spans.
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)
    document.add_paragraph()


def build_docx(
    document_model: DocumentModel,
    *,
    images: Optional[Sequence[Any]] = None,
    footnote: str = "",
    data_header: Optional[Sequence[str]] = None,
    data_rows: Optional[Sequence[Sequence[Any]]] = None,
) -> bytes:
    """Render a DocumentModel as .docx. Structure is taken as given."""
    import docx
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    document = docx.Document()
    section = document.sections[0]
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    normal.paragraph_format.line_spacing = 1.15

    title = document_export.short_report_title(document_model.title, fallback="Rapor")
    document.add_heading(title, level=0)

    pending_images = list(images or [])
    placed_explicitly = any(isinstance(b, ImageBlock) for b in document_model.blocks)

    for block in document_model.blocks:
        if isinstance(block, HeadingBlock):
            if (
                pending_images
                and not placed_explicitly
                and _SOURCES_LABEL.match(block.text)
            ):
                for image in pending_images:
                    _docx_add_image(document, image)
                pending_images = []
            document.add_heading(block.text, level=block.level)
        elif isinstance(block, ParagraphBlock):
            document_export._add_runs_with_bold(document.add_paragraph(), block.text)
        elif isinstance(block, ListBlock):
            _docx_add_list(document, block)
        elif isinstance(block, TableBlock):
            _docx_add_table(document, block)
        elif isinstance(block, QuoteBlock):
            style = _docx_style(document, "Intense Quote", "Quote")
            document_export._add_runs_with_bold(document.add_paragraph(style=style), block.text)
        elif isinstance(block, ImageBlock) and pending_images:
            _docx_add_image(document, pending_images.pop(0), block.caption)

    for image in pending_images:
        _docx_add_image(document, image)

    header = [str(h) for h in (data_header or []) if str(h or "").strip()]
    rows = [list(r) for r in (data_rows or []) if isinstance(r, (list, tuple))]
    if header and rows and not document_export._is_synthetic_narrative_table(header, rows):
        table = _table({"columns": header, "rows": rows})
        if table:
            document.add_heading("Veri / Özet Tablo", level=2)
            _docx_add_table(document, table)

    if footnote.strip():
        run = document.add_paragraph().add_run(footnote.strip())
        run.italic = True
        run.font.size = Pt(9)

    if not document_model.has_content() and not images:
        document.add_paragraph("This job did not produce a final synthesized answer.")

    document_export._add_docx_page_footer(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- PDF ------------------------------------------------------------------


def _pdf_inline(text: str) -> str:
    """Escape for reportlab's mini-markup, then turn **spans** into <b>."""
    from xml.sax.saxutils import escape

    return document_export.INLINE_MARKS.sub(r"<b>\2</b>", escape(text or ""))


def build_pdf(document_model: DocumentModel, *, images: Optional[Sequence[Any]] = None) -> bytes:
    """Render a DocumentModel as an A4 .pdf. Structure is taken as given."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image as RLImage,
        KeepTogether,
        ListFlowable,
        ListItem as RLListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font = document_export._register_pdf_font()
    bold_font = f"{font}-Bold" if font == "Vera" else "Helvetica-Bold"
    styles = getSampleStyleSheet()
    ink = colors.HexColor("#1F2933")
    muted = colors.HexColor("#616E7C")
    # The built-in accent unless a company brand replaces it (brand.py).
    accent = colors.HexColor("#" + brand.current().primary)

    title_style = ParagraphStyle(
        "DocTitle", parent=styles["Title"], alignment=TA_LEFT, fontName=bold_font,
        fontSize=20, leading=25, spaceAfter=12, textColor=ink,
    )
    body_style = ParagraphStyle(
        "DocBody", parent=styles["BodyText"], fontName=font, fontSize=10.5,
        leading=15.5, spaceAfter=7, textColor=ink,
    )
    heading_styles = {
        1: ParagraphStyle(
            "DocH1", parent=body_style, fontName=bold_font, fontSize=15, leading=19,
            spaceBefore=14, spaceAfter=6, textColor=accent, keepWithNext=True,
        ),
        2: ParagraphStyle(
            "DocH2", parent=body_style, fontName=bold_font, fontSize=12.5, leading=16,
            spaceBefore=10, spaceAfter=4, textColor=ink, keepWithNext=True,
        ),
        3: ParagraphStyle(
            "DocH3", parent=body_style, fontName=bold_font, fontSize=11, leading=15,
            spaceBefore=8, spaceAfter=3, textColor=ink, keepWithNext=True,
        ),
    }
    quote_style = ParagraphStyle(
        "DocQuote", parent=body_style, leftIndent=10, rightIndent=10,
        backColor=colors.HexColor("#EEF2F6"), borderPadding=(6, 8, 6, 8),
        spaceBefore=6, spaceAfter=12,
    )
    cell_style = ParagraphStyle("DocCell", parent=body_style, fontSize=9, leading=12, spaceAfter=0)
    head_cell_style = ParagraphStyle("DocHeadCell", parent=cell_style, fontName=bold_font)
    caption_style = ParagraphStyle(
        "DocCaption", parent=body_style, fontSize=8, leading=10, alignment=TA_CENTER,
        textColor=muted, spaceAfter=10,
    )

    left_margin = right_margin = 20 * mm
    frame_width = A4[0] - left_margin - right_margin
    buffer = io.BytesIO()
    title = document_export.short_report_title(document_model.title, fallback="Rapor")
    template = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=left_margin, rightMargin=right_margin,
        topMargin=18 * mm, bottomMargin=20 * mm, title=title,
    )
    story: list = [Paragraph(_pdf_inline(title), title_style)]

    def list_flowable(items: Sequence[ListItem], ordered: bool, depth: int = 0):
        # Each entry is one item's flowables. A deeper run of items becomes a
        # sublist INSIDE the preceding item: added as its own entry, reportlab
        # gives the sublist a bullet of its own ("• • Ereğli").
        entries: list[list] = []
        index = 0
        while index < len(items):
            item = items[index]
            if item.level <= depth or not entries:
                entries.append([Paragraph(_pdf_inline(item.text), body_style)])
                index += 1
                continue
            end = index
            while end < len(items) and items[end].level > depth:
                end += 1
            entries[-1].append(list_flowable(items[index:end], ordered, depth + 1))
            index = end
        bullet = ("1", "a", "i")[min(depth, 2)] if ordered else "bullet"
        return ListFlowable(
            [RLListItem(parts) for parts in entries],
            bulletType=bullet,
            bulletFormat="%s." if ordered else None,
            leftIndent=16,
            bulletFontSize=9 if not ordered else 10,
            bulletFontName=font,
            spaceAfter=6 if depth == 0 else 0,
        )

    def table_flowable(block: TableBlock):
        width = len(block.columns)
        data = [[Paragraph(_pdf_inline(c), head_cell_style) for c in block.columns]]
        data.extend(
            [Paragraph(_pdf_inline(c), cell_style) for c in row] for row in block.rows
        )
        table = Table(data, colWidths=[frame_width / width] * width, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6EF")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8C2CC")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return table

    def image_flowables(image: Any, caption: str = "") -> list:
        if not getattr(image, "path", None) or not image.path.is_file():
            return []
        try:
            picture = RLImage(str(image.path))
            scale = min(
                (frame_width - 10 * mm) / float(picture.imageWidth or 1),
                95 * mm / float(picture.imageHeight or 1),
                1.0,
            )
            picture.drawWidth = picture.imageWidth * scale
            picture.drawHeight = picture.imageHeight * scale
            picture.hAlign = "CENTER"
        except Exception:  # noqa: BLE001
            return []
        parts: list = [Spacer(1, 4), picture]
        note = " — ".join(p for p in (caption, getattr(image, "attribution", "")) if p)
        parts.append(Paragraph(_pdf_inline(note), caption_style) if note else Spacer(1, 8))
        return [KeepTogether(parts)]

    pending_images = list(images or [])
    placed_explicitly = any(isinstance(b, ImageBlock) for b in document_model.blocks)

    for block in document_model.blocks:
        if isinstance(block, HeadingBlock):
            if pending_images and not placed_explicitly and _SOURCES_LABEL.match(block.text):
                for image in pending_images:
                    story.extend(image_flowables(image))
                pending_images = []
            story.append(Paragraph(_pdf_inline(block.text), heading_styles[block.level]))
        elif isinstance(block, ParagraphBlock):
            story.append(Paragraph(_pdf_inline(block.text), body_style))
        elif isinstance(block, ListBlock):
            story.append(list_flowable(block.items, block.ordered))
        elif isinstance(block, TableBlock):
            story.extend([table_flowable(block), Spacer(1, 10)])
        elif isinstance(block, QuoteBlock):
            story.append(Paragraph(_pdf_inline(block.text), quote_style))
        elif isinstance(block, ImageBlock) and pending_images:
            story.extend(image_flowables(pending_images.pop(0), block.caption))

    for image in pending_images:
        story.extend(image_flowables(image))

    if not document_model.has_content() and not images:
        story.append(Paragraph("This job did not produce a final synthesized answer.", body_style))

    template.build(story, canvasmaker=document_export._pdf_page_canvas_factory())
    return buffer.getvalue()


__all__ = [
    "BLOCKS_MARKDOWN_KEY",
    "DocumentModel",
    "attach_blocks_to_payload",
    "blocks_enabled",
    "build_docx",
    "build_pdf",
    "document_from_blocks",
    "document_from_markdown",
    "resolve_payload_document",
    "split_trailing_sources",
]
