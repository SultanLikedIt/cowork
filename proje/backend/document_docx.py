"""Styled Word rendering of a DocumentModel, matching the Chromium PDF design.

Why this module exists
----------------------
The PDF got a designed look (document_html): an accent-barred title block, a
table of contents, accent headings with a rule, tables with a filled header
and zebra rows, highlighted callouts, clickable links and a titled footer.
The Word file of the same report still came out in python-docx's stock
template -- blue Cambria-ish headings, a grid table, an italic centred quote
-- so the two deliverables of one job looked like different products.

This renders the same blocks with the same palette and structure. Word has
no stylesheet, so the look is set on the document's own styles (Normal,
Title, Heading 1-3, Quote, List*) plus a few direct properties that styles
cannot express (paragraph borders and shading, cell fills, bookmarks and
hyperlinks). Keeping it on real styles matters: the navigation pane, a
user-inserted TOC and "change all headings" in Word keep working.

The plain renderer in document_model.build_docx stays as the fallback when
this file fails verification -- see artifacts.ArtifactTools.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any, Optional, Sequence

import document_export
import document_model
from document_html import TOC_MIN_SECTIONS, _URL, _turkish_date
from document_model import (
    DocumentModel,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    ParagraphBlock,
    QuoteBlock,
    TableBlock,
    _SOURCES_LABEL,
)

# Same palette as document_html.CSS.
ACCENT = "2F5D8A"
INK = "1F2933"
MUTED = "616E7C"
SOFT = "EAF1F7"
RULE = "D9E2EC"
ZEBRA = "F5F7FA"
STRONG = "102A43"
WHITE = "FFFFFF"
FONT = "Calibri"

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# --- low-level OOXML helpers --------------------------------------------------


def _el(tag: str, **attrs: str) -> Any:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    element = OxmlElement(tag)
    for key, value in attrs.items():
        element.set(qn(f"w:{key}"), value)
    return element


def _replace_child(parent: Any, child: Any) -> None:
    for existing in parent.findall(child.tag):
        parent.remove(existing)
    parent.append(child)


def _shade(properties: Any, fill: str) -> None:
    _replace_child(properties, _el("w:shd", val="clear", color="auto", fill=fill))


def _paragraph_border(paragraph_or_style_ppr: Any, **sides: tuple[str, int, int]) -> None:
    """sides: name -> (color, size in eighths of a point, space in points)."""
    ppr = paragraph_or_style_ppr
    borders = ppr.find(f"{{{_W}}}pBdr")
    if borders is None:
        borders = _el("w:pBdr")
        ppr.append(borders)
    for side, (color, size, space) in sides.items():
        _replace_child(
            borders, _el(f"w:{side}", val="single", sz=str(size), space=str(space), color=color)
        )


def _set_style_font(style: Any, *, size: Optional[float] = None, color: Optional[str] = None,
                    bold: Optional[bool] = None, italic: Optional[bool] = None) -> None:
    """Pin a style to FONT, overriding any theme font the template assigned.

    python-docx's font.name writes w:ascii/w:hAnsi but leaves asciiTheme in
    place, and the theme attribute wins -- headings stayed in the theme's
    heading face no matter what name was set.
    """
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    font = style.font
    font.name = FONT
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is not None:
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            rfonts.attrib.pop(qn(attr), None)
        rfonts.set(qn("w:eastAsia"), FONT)
    if size is not None:
        font.size = Pt(size)
    if color is not None:
        font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        font.bold = bold
    if italic is not None:
        font.italic = italic


def _configure_styles(document: Any) -> None:
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Cm, Pt

    styles = document.styles
    normal = styles["Normal"]
    _set_style_font(normal, size=10.5, color=INK)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    normal.paragraph_format.line_spacing = 1.2

    title = styles["Title"]
    _set_style_font(title, size=24, color=INK, bold=True)
    title.paragraph_format.space_before = Pt(2)
    title.paragraph_format.space_after = Pt(3)
    title_ppr = title.element.get_or_add_pPr()
    for border in title_ppr.findall(f"{{{_W}}}pBdr"):
        title_ppr.remove(border)  # stock Title has a blue rule under it

    heading_specs = {
        "Heading 1": dict(size=15, color=ACCENT, before=16, after=6),
        "Heading 2": dict(size=12.5, color=INK, before=12, after=4),
        "Heading 3": dict(size=11, color=MUTED, before=10, after=3),
    }
    for name, spec in heading_specs.items():
        style = styles[name]
        _set_style_font(style, size=spec["size"], color=spec["color"], bold=True, italic=False)
        style.paragraph_format.space_before = Pt(spec["before"])
        style.paragraph_format.space_after = Pt(spec["after"])
        style.paragraph_format.keep_with_next = True
    _paragraph_border(styles["Heading 1"].element.get_or_add_pPr(), bottom=(RULE, 6, 3))

    quote = styles["Quote"]
    _set_style_font(quote, size=10.5, color=INK, italic=False)
    quote.paragraph_format.left_indent = Cm(0.35)
    quote.paragraph_format.right_indent = Cm(0.2)
    quote.paragraph_format.space_before = Pt(6)
    quote.paragraph_format.space_after = Pt(10)
    quote_ppr = quote.element.get_or_add_pPr()
    _shade(quote_ppr, SOFT)
    _paragraph_border(quote_ppr, left=(ACCENT, 18, 8))

    for name in ("List Bullet", "List Bullet 2", "List Bullet 3", "List Number", "List Number 2", "List Number 3"):
        if name in {s.name for s in styles}:
            styles[name].paragraph_format.space_after = Pt(3)


# --- inline content -----------------------------------------------------------


def _add_hyperlink(paragraph: Any, text: str, *, url: str = "", anchor: str = "",
                   color: str = ACCENT, size: Optional[float] = None, bold: bool = False) -> None:
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml.ns import qn

    link = _el("w:hyperlink")
    if url:
        rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        link.set(qn("r:id"), rel_id)
    else:
        link.set(qn("w:anchor"), anchor)
    run = _el("w:r")
    rpr = _el("w:rPr")
    rpr.append(_el("w:color", val=color))
    if bold:
        rpr.append(_el("w:b"))
    if size:
        rpr.append(_el("w:sz", val=str(int(size * 2))))
    run.append(rpr)
    text_el = _el("w:t")
    text_el.set(qn("xml:space"), "preserve")
    text_el.text = text
    run.append(text_el)
    link.append(run)
    paragraph._p.append(link)


def _add_rich_text(paragraph: Any, text: str, *, size: Optional[float] = None,
                   color: Optional[str] = None) -> None:
    """Runs for `text`: **spans** bold, URLs as clickable links."""
    from docx.shared import Pt, RGBColor

    parts = document_export.INLINE_MARKS.split(text or "")
    index = 0
    while index < len(parts):
        if index % 3 == 0:
            chunk, bold = parts[index], False
            index += 1
        else:
            chunk, bold = (parts[index + 1] if index + 1 < len(parts) else ""), True
            index += 2
        position = 0
        for match in _URL.finditer(chunk):
            if match.start() > position:
                _styled_run(paragraph, chunk[position:match.start()], bold, size, color)
            _add_hyperlink(paragraph, match.group(0), url=match.group(0), size=size, bold=bold)
            position = match.end()
        if position < len(chunk):
            _styled_run(paragraph, chunk[position:], bold, size, color)


def _styled_run(paragraph: Any, text: str, bold: bool, size: Optional[float], color: Optional[str]) -> None:
    from docx.shared import Pt, RGBColor

    if not text:
        return
    run = paragraph.add_run(text)
    if bold:
        run.bold = True
        run.font.color.rgb = RGBColor.from_string(STRONG)
    elif color:
        run.font.color.rgb = RGBColor.from_string(color)
    if size:
        run.font.size = Pt(size)


def _add_bookmark(paragraph: Any, name: str, bookmark_id: int) -> None:
    start = _el("w:bookmarkStart", id=str(bookmark_id), name=name)
    end = _el("w:bookmarkEnd", id=str(bookmark_id))
    paragraph._p.insert(1 if paragraph._p.pPr is not None else 0, start)
    paragraph._p.append(end)


# --- blocks -------------------------------------------------------------------


def _cover(document: Any, title: str, generated: Optional[datetime]) -> None:
    from docx.shared import Pt, RGBColor

    bar = {"left": (ACCENT, 36, 10)}

    eyebrow = document.add_paragraph()
    run = eyebrow.add_run("RAPOR")
    run.bold = True
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor.from_string(ACCENT)
    run._r.get_or_add_rPr().append(_el("w:spacing", val="40"))
    eyebrow.paragraph_format.space_after = Pt(0)
    _paragraph_border(eyebrow._p.get_or_add_pPr(), **bar)

    heading = document.add_paragraph(title, style="Title")
    _paragraph_border(heading._p.get_or_add_pPr(), **bar)

    meta = document.add_paragraph()
    meta_run = meta.add_run(f"Hazırlanma tarihi: {_turkish_date(generated)}")
    meta_run.font.size = Pt(9)
    meta_run.font.color.rgb = RGBColor.from_string(MUTED)
    meta.paragraph_format.space_after = Pt(18)
    _paragraph_border(meta._p.get_or_add_pPr(), **bar)


def _toc(document: Any, sections: Sequence[tuple[str, str]]) -> None:
    from docx.shared import Cm, Pt, RGBColor

    def boxed(paragraph: Any, *, after: float) -> None:
        fmt = paragraph.paragraph_format
        fmt.space_before = Pt(0)
        fmt.space_after = Pt(after)
        fmt.left_indent = Cm(0.25)
        _shade(paragraph._p.get_or_add_pPr(), SOFT)

    label = document.add_paragraph()
    run = label.add_run("İÇİNDEKİLER")
    run.bold = True
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor.from_string(ACCENT)
    run._r.get_or_add_rPr().append(_el("w:spacing", val="30"))
    boxed(label, after=3)
    for number, (text, anchor) in enumerate(sections, start=1):
        entry = document.add_paragraph()
        entry.add_run(f"{number}. ").font.color.rgb = RGBColor.from_string(MUTED)
        _add_hyperlink(entry, text, anchor=anchor, color=INK)
        boxed(entry, after=2 if number < len(sections) else 14)


def _table(document: Any, block: TableBlock) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    table = document.add_table(rows=1 + len(block.rows), cols=len(block.columns))
    tbl_pr = table._tbl.tblPr
    borders = _el("w:tblBorders")
    for side in ("top", "bottom", "insideH"):
        borders.append(_el(f"w:{side}", val="single", sz="4", space="0", color=RULE))
    for side in ("left", "right", "insideV"):
        borders.append(_el(f"w:{side}", val="nil"))
    _replace_child(tbl_pr, borders)
    margins = _el("w:tblCellMar")
    for side, width in (("top", "60"), ("bottom", "60"), ("left", "100"), ("right", "100")):
        margins.append(_el(f"w:{side}", w=width, type="dxa"))
    _replace_child(tbl_pr, margins)

    def fill(cell: Any, text: str, *, header: bool, zebra: bool) -> None:
        cell.text = ""
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(0)
        if header:
            run = paragraph.add_run(text)
            run.bold = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor.from_string(WHITE)
            _shade(cell._tc.get_or_add_tcPr(), ACCENT)
        else:
            _add_rich_text(paragraph, text, size=9)
            if zebra:
                _shade(cell._tc.get_or_add_tcPr(), ZEBRA)

    for column, name in enumerate(block.columns):
        fill(table.rows[0].cells[column], name, header=True, zebra=False)
    header_pr = table.rows[0]._tr.get_or_add_trPr()
    header_pr.append(_el("w:tblHeader", val="true"))
    for row_index, row in enumerate(block.rows, start=1):
        tr_pr = table.rows[row_index]._tr.get_or_add_trPr()
        tr_pr.append(_el("w:cantSplit", val="true"))
        for column, value in enumerate(row):
            fill(table.rows[row_index].cells[column], value, header=False, zebra=row_index % 2 == 0)
    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(4)


def _list(document: Any, block: ListBlock, *, size: Optional[float]) -> None:
    base = "List Number" if block.ordered else "List Bullet"
    num_id = document_model._docx_new_numbering(document, base) if block.ordered else None
    for item in block.items:
        suffix = "" if item.level == 0 else f" {min(item.level + 1, 3)}"
        style = document_model._docx_style(document, base + suffix, base)
        paragraph = document.add_paragraph(style=style)
        _add_rich_text(paragraph, item.text, size=size)
        if num_id is not None and item.level == 0:
            document_model._docx_set_num_id(paragraph, num_id)


def _footer(document: Any, title: str) -> None:
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    section = document.sections[0]
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    for child in list(paragraph._p):
        if not child.tag.endswith("}pPr"):
            paragraph._p.remove(child)
    width = section.page_width - section.left_margin - section.right_margin
    paragraph.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
    _paragraph_border(paragraph._p.get_or_add_pPr(), top=(RULE, 4, 6))

    def text(value: str) -> None:
        run = paragraph.add_run(value)
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor.from_string(MUTED)

    def field(instruction: str) -> None:
        run = paragraph.add_run()
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor.from_string(MUTED)
        begin = _el("w:fldChar", fldCharType="begin")
        instr = _el("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = instruction
        separate = _el("w:fldChar", fldCharType="separate")
        placeholder = _el("w:t")
        placeholder.text = "1"
        end = _el("w:fldChar", fldCharType="end")
        for part in (begin, instr, separate, placeholder, end):
            run._r.append(part)

    text(title[:90])
    text("\tSayfa ")
    field(" PAGE ")
    text(" / ")
    field(" NUMPAGES ")


def build_docx(
    document_model_: DocumentModel,
    *,
    images: Optional[Sequence[Any]] = None,
    footnote: str = "",
    data_header: Optional[Sequence[str]] = None,
    data_rows: Optional[Sequence[Sequence[Any]]] = None,
    generated: Optional[datetime] = None,
) -> bytes:
    """Render a DocumentModel as a styled .docx (same design as the PDF)."""
    import docx
    from docx.shared import Cm, Pt, RGBColor

    document = docx.Document()
    section = document.sections[0]
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    _configure_styles(document)

    title = document_export.short_report_title(document_model_.title, fallback="Rapor")
    document.core_properties.title = title
    _cover(document, title, generated)

    blocks = list(document_model_.blocks)
    main_sections = [
        b for b in blocks
        if isinstance(b, HeadingBlock) and b.level == 1 and not _SOURCES_LABEL.match(b.text)
    ]
    anchors = {id(block): f"bolum_{n}" for n, block in enumerate(main_sections, start=1)}
    if len(main_sections) >= TOC_MIN_SECTIONS:
        _toc(document, [(block.text, anchors[id(block)]) for block in main_sections])

    pending_images = list(images or [])
    explicit_images = any(isinstance(b, ImageBlock) for b in blocks)
    in_sources = False
    bookmark_id = 0

    for block in blocks:
        if isinstance(block, HeadingBlock):
            is_sources = bool(_SOURCES_LABEL.match(block.text))
            if is_sources and pending_images and not explicit_images:
                for image in pending_images:
                    document_model._docx_add_image(document, image)
                pending_images = []
            in_sources = in_sources or is_sources
            paragraph = document.add_heading(block.text, level=block.level)
            anchor = anchors.get(id(block))
            if anchor:
                bookmark_id += 1
                _add_bookmark(paragraph, anchor, bookmark_id)
        elif isinstance(block, ParagraphBlock):
            _add_rich_text(document.add_paragraph(), block.text, size=9 if in_sources else None)
        elif isinstance(block, ListBlock):
            _list(document, block, size=9 if in_sources else None)
        elif isinstance(block, TableBlock):
            _table(document, block)
        elif isinstance(block, QuoteBlock):
            _add_rich_text(document.add_paragraph(style="Quote"), block.text)
        elif isinstance(block, ImageBlock) and pending_images:
            document_model._docx_add_image(document, pending_images.pop(0), block.caption)

    for image in pending_images:
        document_model._docx_add_image(document, image)

    header = [str(h) for h in (data_header or []) if str(h or "").strip()]
    rows = [list(r) for r in (data_rows or []) if isinstance(r, (list, tuple))]
    if header and rows and not document_export._is_synthetic_narrative_table(header, rows):
        table = document_model._table({"columns": header, "rows": rows})
        if table:
            document.add_heading("Veri / Özet Tablo", level=2)
            _table(document, table)

    if footnote.strip():
        run = document.add_paragraph().add_run(footnote.strip())
        run.italic = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor.from_string(MUTED)

    if not document_model_.has_content() and not images:
        document.add_paragraph("This job did not produce a final synthesized answer.")

    _footer(document, title)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


__all__ = ["build_docx"]
