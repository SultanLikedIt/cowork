"""Renders narrative + optional tables to .docx / .pdf / structured markdown.

Both writers take the already-rendered final-answer text rather than
re-deriving it from the job store. The frontend's getFinalAnswer()
(stages.js) picks the final answer via the task dependency graph, with
several fallbacks; reimplementing that here would risk the exported file
quietly disagreeing with what the Results view shows. The caller passes the
exact text it displays, so the file always matches the screen.

Formatting recognizes markdown-ish headings, bullets, and numbered lines.
Wall-of-text model output (one giant paragraph with "(1) … (2) …" or
checklist markers inside) is first structured into readable blocks so the
exported Word/PDF is not a single unbroken run of 100+ lines.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Sequence


@dataclass(frozen=True)
class ImageEmbed:
    """An image to embed in a generated document."""
    path: Path
    attribution: str = ""

BULLET_PREFIX = re.compile(r"^\s*[-*•·]\s+")
NUMBERED_PREFIX = re.compile(r"^\s*\d+[.)]\s+")
HEADING_PREFIX = re.compile(r"^\s*(#{1,6})\s+")
# Markdown pipe table: "| a | b |" and separator "|---|---|"
_MD_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")
# Non-empty cells only — so jammed "| a | b | |---| | c | d |" splits into rows
# instead of one greedy match swallowing the whole blob.
_MD_TABLE_ROW_IN_BLOB = re.compile(r"\|(?:[ \t]*[^|\s][^|\n]*\|)+")

# Models frequently emit **bold** / __bold__ markers. Neither writer renders
# markdown, so left alone they'd show up as literal asterisks in the file.
INLINE_MARKS = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
ITALIC_MARKS = re.compile(r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)")
INLINE_CODE = re.compile(r"`([^`]+)`")
MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FENCE_BLOCK = re.compile(r"```(?:\w+)?\n?([\s\S]*?)```")
# Models sometimes paste HTML <img> hoping PDF/Word will render it; they
# will not — leave only the real embedded ImageEmbed bytes.
HTML_IMG = re.compile(r"(?is)<img\b[^>]*(?:/>|>[\s\S]*?</img>)")
HTML_TAG = re.compile(r"(?is)</?(?:div|span|p|br|a|figure|figcaption|style|script)\b[^>]*>")
# Markdown thematic breaks left as literal "---" junk in Word/PDF.
HR_LINE = re.compile(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
FAKE_PAGE_MARK = re.compile(
    r"(?im)^\s*(?:sayfa|page)\s*\d+\s*/\s*\d+\s*$"
)
# Model leftover when it could not embed a photo: drop the instruction, do
# not render it as a fake "error" paragraph in Word/PDF.
IMAGE_INSTRUCTION_LINE = re.compile(
    r"(?is)^\s*\[?\s*(?:görsel|gorsel|image|figure|photo)\s*:"
    r".{0,500}$"
    r"|önerilen kaynaklar\s*:.{0,180}(?:unsplash|pexels|pixabay)"
    r"|^\s*görsel\s+notu\s*:?\s*$"
    r"|telifsiz\s+bir\s+.{0,80}görsel(?:i|inin)?\s+eklenmesi"
    r"|(?:unsplash|pexels|pixabay).{0,80}(?:temin|indir|ekle)"
)
GORSEL_NOTU_BLOCK = re.compile(
    r"(?is)(?:^|\n)\s*görsel\s+notu\s*:?\s*\n"
    r"(?:.*\n)*?"
    r"(?=\n\s*(?:#{1,3}\s+|\d+[.)]\s+\S+|kaynak|\Z))"
)
MACHINE_BLOCK = re.compile(
    r"\n---\n(?:RESEARCH_SOURCES|ARTIFACT_HANDOFF):\n[\s\S]*$",
    re.IGNORECASE,
)
CHAT_PREAMBLE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:elbette|tabii(?:\s+ki)?|of course|sure)[,!]?\s+"
    r")*"
    r"(?:"
    r"aşağıda\b[^\n]{0,280}?(?:rapor|özet|metin|belge|araştırma)[^\n]{0,160}?"
    r"(?:bulunmaktadır|vardır|hazırladım|hazırlanmıştır)[^\n.]*[.\n]+\s*"
    r"|"
    r"here(?:'s| is)\s+(?:a |the )?(?:report|summary|document)[^\n.]*[.\n]+\s*"
    r")?"
)
ASSISTANT_META = re.compile(
    r"(?is)"
    r"(?:^|\n)\s*(?:"
    r"bu metin[, ].{0,200}?(?:kullanılabilir|kullanilabilir)[^\n]*|"
    r"[^\n]{0,120}?slayt(?:lara)?\s+dönüştürülebilecek[^\n]*|"
    r"[^\n]{0,120}?word\s+belgesi\s+ve\s+html\s+sunum[^\n]*|"
    r"[^\n]{0,160}?görsel\s+python\s+ile\s+oluşturul[^\n]*|"
    r"[^\n]{0,160}?gorsel\s+python\s+ile\s+olusturul[^\n]*|"
    r"generated\s+\d{4}-\d{2}-\d{2}[^\n]*"
    r")\s*"
)
# Document Agent "thinking out loud" that must never become the Word title
# or leak into the report body.
PLANNING_LEAK = re.compile(
    r"(?is)^\s*(?:"
    r"elimdeki\s+kanıt|"
    r"kanıtlarda\b.{0,40}?\b(?:bilgi|var)|"
    r"raporu\s+yapılandır|"
    r"yapılandıracağım|"
    r"kullanıcının\s+(?:dilini|istediği)|"
    r"kullanicinin\s+(?:dilini|istedigi)|"
    r"belge\s+ajan|"
    r"oluşturamayacağım|"
    r"olusturamayacagim|"
    r"i\s+(?:can|will|should|cannot)\s+(?:create|write|structure)|"
    r"based\s+on\s+(?:the\s+)?(?:evidence|notes)\s+i\s+have|"
    r"let\s+me\s+(?:structure|organize|draft)"
    r")"
)

MAX_TITLE_LENGTH = 110
# Soft-wrap a wall paragraph into ~sentence groups when nothing else fires.
_MAX_PARAGRAPH_CHARS = 420
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Inline scenario markers the Document Agent loves: "(1) Foo – bar. (2) Baz"
PAREN_ENUM = re.compile(r"(?<!\d)\((\d{1,2})\)\s+")
# Checklist / step markers: "1) En acil … 2) Pilot …"
BARE_ENUM = re.compile(r"(?:(?<=[\s:;])|^)(\d{1,2})\)\s+")
# Soft section cues in Turkish/English prose reports.
SECTION_CUE = re.compile(
    r"(?<=[.!?…])\s+(?="
    r"(?:Raporda|Her senaryo|Kritik|Raporun sonunda|Özet|Sonuç|Giriş|"
    r"In summary|Overall|Key findings|Checklist|Uygulama)"
    r")",
    re.IGNORECASE,
)


def _clean_inline(text: str, *, keep_bold: bool = False) -> str:
    """``keep_bold`` leaves **markers** in place for writers that can turn
    them into real bold runs. Everything else strips them, because a writer
    that cannot render bold would otherwise show the asterisks."""
    cleaned = text or "" if keep_bold else INLINE_MARKS.sub(r"\2", text or "")
    cleaned = ITALIC_MARKS.sub(r"\1", cleaned)
    cleaned = INLINE_CODE.sub(r"\1", cleaned)
    cleaned = MD_IMAGE.sub(r"\1", cleaned)
    cleaned = MD_LINK.sub(r"\1 (\2)", cleaned)
    return cleaned.replace("\u200b", "").rstrip()


def sanitize_report_prose(body: str, *, keep_bold: bool = False) -> str:
    """Strip chat fluff and leftover markdown before Word/PDF/HTML writers.

    Document models often paste the assistant preamble, the raw user ask, or
    light markdown into ``summary``. Writers do not render markdown, so left
    alone those markers become literal junk in the downloaded file.

    Markdown headings (``#``) are kept so ``structure_prose`` / writers can
    turn them into real Word/PDF headings; ``_classify`` strips the hashes
    at render time.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = MACHINE_BLOCK.sub("", text)
    text = FENCE_BLOCK.sub(lambda m: m.group(1).strip(), text)
    text = HTML_IMG.sub("", text)
    text = HTML_TAG.sub("", text)
    text = HR_LINE.sub("", text)
    text = "\n".join(
        line for line in text.split("\n") if not FAKE_PAGE_MARK.match(line)
    )
    text = CHAT_PREAMBLE.sub("", text.strip(), count=1)
    text = ASSISTANT_META.sub("\n", text)
    text = GORSEL_NOTU_BLOCK.sub("\n", text)

    lines: list[str] = []
    for raw in text.split("\n"):
        line = _clean_inline(raw, keep_bold=keep_bold)
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if IMAGE_INSTRUCTION_LINE.search(stripped):
            continue
        if PLANNING_LEAK.search(stripped):
            continue
        # Drop a leading echo of a long user request (common JSON fallback).
        if (
            not lines
            and len(stripped) > 90
            and re.search(
                r"\b(?:yapmanı|istiyorum|hazırla|yazmanı|oluşturmanı)\b",
                stripped,
                re.IGNORECASE,
            )
        ):
            continue
        lines.append(line)

    # Collapse 3+ blank lines.
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    return "\n".join(out).strip()


_SECTION_NUMBER = re.compile(r"^\s*(\d{1,2})[.)]?\s+(\S.*)$")


def _norm_title_cmp(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


_MD_HARD_BREAK_RE = re.compile(r"[ \t]{2,}\n")


def iter_body_blocks(
    body: str, *, title: str = "", keep_bold: bool = False
) -> list[tuple[str, Any, int]]:
    """Structured body blocks, skipping a leading duplicate of the title.

    Markdown's hard line break (two trailing spaces) is promoted to a real
    paragraph break first. sanitize_report_prose strips that trailing
    whitespace, and _blocks then joins consecutive text lines on purpose --
    so a deliberately line-broken run of "**Label:** value" facts arrived in
    the PDF as one wall of text with the figures running together. Doing it
    here keeps _blocks' line-joining, which exists for hard-wrapped prose.
    """
    blocks = list(
        _blocks(
            sanitize_report_prose(
                _MD_HARD_BREAK_RE.sub("\n\n", body or ""), keep_bold=keep_bold
            ),
            keep_bold=keep_bold,
        )
    )
    want = _norm_title_cmp(title)
    if not want or not blocks:
        return blocks
    kind, text, _level = blocks[0]
    if kind in {"heading", "text"} and isinstance(text, str) and _norm_title_cmp(text) == want:
        return blocks[1:]
    return blocks


def _add_docx_matrix_table(document: Any, matrix: list[list[str]]) -> None:
    """Write a markdown-derived matrix as a Word table (first row = header)."""
    if not matrix:
        return
    col_count = min(max(len(r) for r in matrix), 8)
    table = document.add_table(rows=len(matrix), cols=col_count)
    # Never assume an English style name exists. A document created from
    # scratch carries python-docx's default styles, but an uploaded
    # template carries the user's -- a Turkish one has no "Light Grid
    # Accent 1", and the whole write failed with "no style with name
    # 'Light Grid Accent 1'", losing the deliverable over a table border.
    # Take the template's own table style when it has one; otherwise leave
    # the table unstyled, which still renders.
    for candidate in ("Light Grid Accent 1", "Table Grid", "Tablo Kılavuzu"):
        try:
            table.style = candidate
            break
        except KeyError:
            continue
    else:
        for style in document.styles:
            try:
                if style.type is not None and int(style.type) == 3:  # TABLE
                    table.style = style
                    break
            except (KeyError, ValueError, TypeError):
                continue
    for r_idx, row in enumerate(matrix):
        for c in range(col_count):
            value = row[c] if c < len(row) else ""
            cell = table.rows[r_idx].cells[c]
            cell.text = "" if value is None else str(value)
            if r_idx == 0:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True


def _add_pdf_matrix_table(story: list, matrix: list[list[str]], *, font: str, bold_font: str) -> None:
    """Append a reportlab Table for a markdown-derived matrix."""
    if not matrix:
        return
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    cell_style = ParagraphStyle(
        "MdTableCell",
        fontName=font,
        fontSize=9,
        leading=12,
    )
    header_style = ParagraphStyle(
        "MdTableHeader",
        fontName=bold_font,
        fontSize=9,
        leading=12,
    )
    col_count = min(max(len(r) for r in matrix), 8)
    data: list[list] = []
    for r_idx, row in enumerate(matrix):
        style = header_style if r_idx == 0 else cell_style
        cells = []
        for c in range(col_count):
            raw = row[c] if c < len(row) else ""
            cells.append(Paragraph(escape("" if raw is None else str(raw)), style))
        data.append(cells)
    table = Table(data, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.92, 0.93, 0.95)),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(0.7, 0.7, 0.75)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 6))


_KAYNAKLAR_HEAD = re.compile(
    r"(?im)^\s*kaynak(?:lar|ça|ca)?\s*:?\s*$"
)


def split_sources_section(body: str, *, keep_bold: bool = False) -> tuple[str, str]:
    """Split narrative from a trailing Kaynaklar section (optional colon)."""
    text = sanitize_report_prose(body, keep_bold=keep_bold)
    if not text:
        return "", ""
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if _KAYNAKLAR_HEAD.match(line.strip()) or re.match(
            r"(?im)^\s*kaynak(?:lar|ça|ca)?\s*:", line.strip()
        ):
            main = "\n".join(lines[:idx]).rstrip()
            tail = "\n".join(lines[idx:]).strip()
            return main, tail
    return text, ""


def _looks_like_section_heading(text: str) -> bool:
    """True for short titles like 'Giriş' / 'Beslenme Güçlüğü', not sentences."""
    title = (text or "").strip()
    if not title or len(title) > 72 or "http://" in title or "https://" in title:
        return False
    words = title.split()
    if not words or len(words) > 10:
        return False
    if title.endswith((",", ";", ":")):
        return False
    if title.endswith((".", "!", "?", "…")) and len(words) > 4:
        return False
    return True


def _is_md_table_separator(line: str) -> bool:
    cells = _md_table_cells(line)
    return bool(cells) and all(_MD_TABLE_SEP_CELL.match(c.replace(" ", "")) for c in cells)


def _md_table_cells(line: str) -> list[str]:
    """Cells of a markdown table row, closing pipe optional.

    A row that lost its trailing "|" is still a row. It loses it whenever
    the text is clipped at a character cap mid-table, and the strict form
    then stopped recognising it -- so "| Konum | Thames Nehri kıyısı,
    İngiltere'ni" reached a slide with its pipes intact and truncated,
    while the row above it converted cleanly. Being lenient here costs
    nothing: two or more pipe-separated fields is what a row is.
    """
    stripped = line.strip()
    match = _MD_TABLE_ROW.match(stripped)
    if match:
        return [_clean_inline(c.strip()) for c in match.group(1).split("|")]
    if stripped.startswith("|") and stripped.count("|") >= 2:
        return [
            _clean_inline(c.strip())
            for c in stripped.strip("|").split("|")
            if c.strip()
        ]
    return []


def _looks_like_md_table_row(line: str) -> bool:
    cells = _md_table_cells(line)
    return len(cells) >= 2


def _expand_collapsed_md_tables(line: str) -> list[str]:
    """Split a jammed '| a | b | |---| | c | d |' blob into separate row lines."""
    stripped = (line or "").strip()
    if stripped.count("|") < 4 or "\n" in stripped:
        return [line]
    rows = _MD_TABLE_ROW_IN_BLOB.findall(stripped)
    if len(rows) < 2:
        return [line]
    # Only expand when at least one row looks like a real table row.
    if not any(_looks_like_md_table_row(r) or _is_md_table_separator(r) for r in rows):
        return [line]
    return [r.strip() for r in rows]


def _classify(
    line: str, *, keep_bold: bool = False
) -> tuple[Literal["heading", "bullet", "number", "table_row", "text"], str, int]:
    """Return (kind, cleaned_text, heading_level) for one line."""
    heading = HEADING_PREFIX.match(line)
    if heading:
        level = len(heading.group(1))
        return "heading", _clean_inline(HEADING_PREFIX.sub("", line), keep_bold=keep_bold), level
    if BULLET_PREFIX.match(line):
        # Indentation is the only signal for a nested list, and it survives
        # to here unstripped. Two spaces per level, which is what the
        # document worker is told to emit; writers that do not nest ignore
        # the level exactly as before.
        indent = len(line) - len(line.lstrip(" 	"))
        return (
            "bullet",
            _clean_inline(BULLET_PREFIX.sub("", line), keep_bold=keep_bold),
            indent // 2,
        )
    if _looks_like_md_table_row(line) or _is_md_table_separator(line):
        return "table_row", line.strip(), 0
    numbered = _SECTION_NUMBER.match(line)
    if numbered:
        rest = _clean_inline(numbered.group(2), keep_bold=keep_bold)
        if _looks_like_section_heading(rest):
            return "heading", rest, 2
        if NUMBERED_PREFIX.match(line):
            return "number", rest, 0
    elif NUMBERED_PREFIX.match(line):
        return "number", _clean_inline(NUMBERED_PREFIX.sub("", line), keep_bold=keep_bold), 0
    return "text", _clean_inline(line, keep_bold=keep_bold), 0


def _split_wall_paragraph(text: str) -> list[str]:
    """Break one long prose blob into paragraph-sized chunks.

    Prefers explicit enumeration / section cues; falls back to sentence
    grouping so a 1800-char wall becomes several readable paragraphs.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    if len(cleaned) <= _MAX_PARAGRAPH_CHARS and not PAREN_ENUM.search(cleaned):
        return [cleaned]

    # Prefer "(1) … (2) …" scenario lists → numbered markdown lines.
    if len(PAREN_ENUM.findall(cleaned)) >= 2:
        parts = PAREN_ENUM.split(cleaned)
        out: list[str] = []
        lead = parts[0].strip()
        if lead:
            out.extend(_sentence_chunks(lead))
        # split() with a capturing group: [lead, n1, body1, n2, body2, ...]
        for i in range(1, len(parts), 2):
            num = parts[i]
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if not body:
                continue
            # Trailing checklist ("… yer alır: 1) … 2) …") often sticks to the
            # last scenario -- peel it into its own numbered block.
            if i + 2 >= len(parts) and len(BARE_ENUM.findall(body)) >= 3:
                bare_parts = BARE_ENUM.split(body)
                head = bare_parts[0].strip()
                if head:
                    out.append(f"{num}. {head}")
                for j in range(1, len(bare_parts), 2):
                    bnum = bare_parts[j]
                    bbody = bare_parts[j + 1].strip() if j + 1 < len(bare_parts) else ""
                    if bbody:
                        out.append(f"{bnum}. {bbody}")
            else:
                out.append(f"{num}. {body}")
        return out or [cleaned]

    # Checklist style "1) … 2) …" after a colon/intro.
    if len(BARE_ENUM.findall(cleaned)) >= 3:
        parts = BARE_ENUM.split(cleaned)
        out = []
        lead = parts[0].strip()
        if lead:
            out.extend(_sentence_chunks(lead))
        for i in range(1, len(parts), 2):
            num = parts[i]
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if body:
                out.append(f"{num}. {body}")
        return out or [cleaned]

    # Soft section breaks on cue phrases.
    if SECTION_CUE.search(cleaned):
        pieces = SECTION_CUE.split(cleaned)
        chunks: list[str] = []
        for piece in pieces:
            piece = piece.strip()
            if piece:
                chunks.extend(_sentence_chunks(piece))
        return chunks or [cleaned]

    return _sentence_chunks(cleaned)


def _sentence_chunks(text: str) -> list[str]:
    """Group sentences so each paragraph stays under ~_MAX_PARAGRAPH_CHARS."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return [text] if text.strip() else []
    chunks: list[str] = []
    buf = ""
    for sentence in sentences:
        candidate = f"{buf} {sentence}".strip() if buf else sentence
        if buf and len(candidate) > _MAX_PARAGRAPH_CHARS:
            chunks.append(buf)
            buf = sentence
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


def structure_prose(body: str, *, keep_bold: bool = False) -> str:
    """Normalize free-form model prose into markdown-friendly text.

    Already-structured input (blank lines, `#` headings, list markers) is
    preserved. Wall-of-text blobs are split into paragraphs / numbered
    items so Word and PDF writers can render real hierarchy.
    """
    raw = sanitize_report_prose(body, keep_bold=keep_bold)
    if not raw:
        return ""

    source_lines = raw.split("\n") if "\n" in raw else [raw]
    expanded: list[str] = []
    for line in source_lines:
        if not line.strip():
            expanded.append("")
            continue
        # First peel jammed markdown tables into separate rows.
        pieces = _expand_collapsed_md_tables(line.rstrip())
        for piece in pieces:
            kind, _, _ = _classify(piece)
            if kind == "table_row":
                expanded.append(piece.strip())
            elif kind == "text" and len(piece) > _MAX_PARAGRAPH_CHARS * 2:
                expanded.extend(_split_wall_paragraph(piece))
                expanded.append("")
            else:
                expanded.append(piece)

    if "\n" not in raw and len(expanded) == 1 and not _looks_like_md_table_row(expanded[0]):
        return "\n\n".join(_split_wall_paragraph(raw))

    out: list[str] = []
    blank = 0
    for line in expanded:
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    return "\n".join(out).strip()


def _parse_md_table_rows(row_lines: list[str]) -> list[list[str]]:
    """Turn markdown table row lines into a matrix (header + body)."""
    matrix: list[list[str]] = []
    for line in row_lines:
        if _is_md_table_separator(line):
            continue
        cells = _md_table_cells(line)
        if cells:
            matrix.append(cells)
    if len(matrix) < 2:
        return []
    width = max(len(r) for r in matrix)
    return [r + [""] * (width - len(r)) for r in matrix]


def _blocks(body: str, *, keep_bold: bool = False) -> list[tuple[str, Any, int]]:
    """Split body text into renderable blocks.

    Consecutive plain-text lines are joined into one paragraph so that
    hard-wrapped model output doesn't become one paragraph per line, while
    list items, headings, and markdown tables each stay on their own.
    Table blocks use payload ``list[list[str]]`` (header row first).
    """
    structured = structure_prose(body, keep_bold=keep_bold)
    blocks: list[tuple[str, Any, int]] = []
    paragraph: list[str] = []
    table_rows: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(("text", " ".join(paragraph), 0))
            paragraph.clear()

    def flush_table() -> None:
        if not table_rows:
            return
        pending = list(table_rows)
        table_rows.clear()
        matrix = _parse_md_table_rows(pending)
        if matrix:
            blocks.append(("table", matrix, 0))
        else:
            for line in pending:
                blocks.append(("text", _clean_inline(line), 0))

    for raw_line in structured.split("\n"):
        if not raw_line.strip():
            flush_paragraph()
            flush_table()
            continue
        kind, text, level = _classify(raw_line, keep_bold=keep_bold)
        if kind == "table_row":
            flush_paragraph()
            table_rows.append(text)
            continue
        flush_table()
        if kind == "text":
            paragraph.append(text)
            continue
        flush_paragraph()
        if text:
            blocks.append((kind, text, level))

    flush_paragraph()
    flush_table()
    return blocks


def _safe_title(title: str) -> str:
    clean = _clean_inline((title or "").strip()) or "Agent result"
    if len(clean) > MAX_TITLE_LENGTH:
        clean = clean[: MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return clean


def _looks_like_bad_title(candidate: str) -> bool:
    """True for thinking/meta/prompt-echo lines that must not be the doc title."""
    text = (candidate or "").strip()
    if not text:
        return True
    if text.lstrip().startswith(("-", "•", "*", "–", "—")):
        return True
    if PLANNING_LEAK.search(text):
        return True
    if len(text) > MAX_TITLE_LENGTH:
        return True
    # Section labels / bibliography headings are never the document title.
    if re.match(
        r"(?is)^\s*(?:\d+[.)]\s*)?(?:kaynak(?:lar|ça|ca)?|giriş|giris|"
        r"sonuç|sonuc|özet|ozet|içindekiler|icindekiler|references?|"
        r"sources?|introduction|conclusion)\s*:?\s*$",
        text,
    ):
        return True
    if re.search(
        r"(?is)\b(?:yapmanı|istiyorum|hazırla|yazmanı|oluşturmanı|"
        r"yapılandıracağım|yapilandiracagim|oluşturabilirim|olusturabilirim|"
        r"dikkat etmeli|kullanacağım|kullanacagim)\b",
        text,
    ):
        return True
    # Long prose sentences are body, not titles.
    if len(text) > 90 and text.count(" ") > 12:
        return True
    if text.endswith((".", "…", "!", "?")):
        return True
    return False


def _looks_like_chatty_title(candidate: str) -> bool:
    """True when leftover chat/filler still pollutes a deck title."""
    text = (candidate or "").strip()
    if not text:
        return True
    if re.search(
        r"(?is)\b(?:"
        r"hadi|bakal[ıi]m|bak[ıi]y[ıi]m|bakiyim|"
        r"g[uü]zel(?:ce)?|şekilde|sekilde|"
        r"misin|m[ıi]s[ıi]n|musun|m[uü]s[uü]n|"
        r"l[uü]tfen|please|hemen|[sş]imdi"
        r")\b",
        text,
    ):
        return True
    return False


def presentation_title_from_request(
    request: str, *, fallback: str = "Sunum"
) -> str:
    """Deck title from the user ask — strip chat/deliverable fluff."""
    text = (request or "").strip()
    if not text:
        return fallback
    # Prefer an explicit ## heading if the model already wrote one.
    headed = short_report_title(text, fallback="")
    if (
        headed
        and not _looks_like_bad_title(headed)
        and not _looks_like_chatty_title(headed)
        and len(headed) >= 4
    ):
        # Reject prompt-echo titles that still look like the full ask.
        if "pptx" not in headed.casefold() and "hazırla" not in headed.casefold():
            return headed
    cleaned = re.sub(
        r"(?is)\b(?:"
        r"pptx|ppt|powerpoint|power\s*point|sunum|slayt|deck|presentation|"
        r"docx|pdf|word|excel|html|"
        r"detayl[ıi]|kapsaml[ıi]|ufak|k[ıi]sa|g[uü]ncel|"
        r"ara[sş]t[iı]r(?:ma|ma[yıi]|ıp|ip)?|incele(?:me)?|"
        r"haz[iı]rla(?:r\s*m[iı]s[iı]n|y[iı]n)?|"
        r"yap(?:ar\s*m[iı]s[iı]n|man[ıi]|ıp|ip)?|"
        r"ver(?:ir\s*misin)?|oluştur(?:ur\s*musun)?|olustur|"
        r"bana|bunun|hakk[iı]nda|hakkinda|i[cç]in|icin|ve|ile|bir|"
        r"g[uü]zel(?:ce)?|şekilde|sekilde|hadi|"
        r"bakal[ıi]m|bak[ıi]y[ıi]m|bakiyim|bak|"
        r"hemen|[sş]imdi|"
        r"l[uü]tfen|please|can\s+you|could\s+you|would\s+you"
        r")\b",
        " ",
        text,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")
    if len(cleaned) < 3 or _looks_like_chatty_title(cleaned):
        return fallback
    # Keep a compact topic phrase.
    words = cleaned.split()
    phrase = " ".join(words[:8])
    return _safe_title(phrase[:80] if phrase else fallback)


def resolve_presentation_title(
    request: str,
    *,
    document_title: str = "",
    body: str = "",
    fallback: str = "Sunum",
) -> str:
    """Pick a deck title: prefer a real report heading over prompt leftovers.

    After the 'Ekosistem' bullet-as-title bug we started forcing titles from
    the user ask. That over-corrected: chat fluff ('güzel şekilde hadi
    bakıyım') survived stripping while the notes already had a clean
    section title. Prefer Document/notes headings when they look like
    titles; only fall back to the stripped ask.
    """
    candidates: list[str] = []
    for raw in (document_title,):
        cand = _clean_inline((raw or "").strip())
        if cand:
            candidates.append(cand)
    if body:
        from_body = short_report_title(body, fallback="")
        if from_body:
            candidates.append(from_body)
        # First non-generic markdown/outline heading often is the real title.
        for line in (body or "").splitlines():
            headed = HEADING_PREFIX.sub("", line).strip()
            if not headed or _looks_like_bad_title(headed):
                continue
            if re.match(
                r"(?is)^\s*(?:gündem|gundem|agenda|genel\s+bilgiler|"
                r"kaynak(?:lar)?|özet|ozet)\b",
                headed,
            ):
                continue
            if 8 <= len(headed) <= 90:
                candidates.append(headed)
                break

    for cand in candidates:
        if (
            not _looks_like_bad_title(cand)
            and not _looks_like_chatty_title(cand)
            and len(cand) >= 4
        ):
            # Reject near-copies of the raw ask.
            req = re.sub(r"\s+", " ", (request or "").strip().casefold())
            if req and cand.casefold() in req and len(cand) > 40:
                continue
            return _safe_title(cand)

    from_ask = presentation_title_from_request(request, fallback="")
    if from_ask and not _looks_like_chatty_title(from_ask):
        return from_ask
    return fallback if fallback else "Sunum"


def short_report_title(raw: str, *, fallback: str = "Rapor") -> str:
    """Pick a short document title; never thinking notes or the user prompt."""
    text = _clean_inline(sanitize_report_prose(raw or "")).strip()
    if not text:
        return fallback

    best = ""
    best_score = -1
    for line in text.split("\n"):
        candidate = HEADING_PREFIX.sub("", line).strip()
        # Strip leading "1. " section numbers for scoring, keep readable form.
        numbered = re.match(r"^\s*\d+[.)]\s+(\S.*)$", candidate)
        if numbered:
            candidate = numbered.group(1).strip()
        if not candidate or _looks_like_bad_title(candidate):
            continue
        score = 0
        if re.search(
            r"(?is)\b(?:rapor|özet|ozet|inceleme|sistemi|maven|palantir|"
            r"kutup|polar|iklim|climate|ısınma|isinma)\b",
            candidate,
        ):
            score += 2
        if 12 <= len(candidate) <= 90:
            score += 1
        if score > best_score:
            best = candidate
            best_score = score
            if score >= 2 and len(candidate) <= 90:
                return _safe_title(best)

    if best and best_score >= 0:
        return _safe_title(best)
    return fallback


def _label_key(value: str) -> str:
    """Case-fold a table header for synthetic-table detection.

    Turkish ``İ``.lower() becomes ``i`` + combining dot under Unicode, which
    breaks a naive ``== "içerik"`` check -- normalize that away.
    """
    text = (value or "").strip().replace("İ", "I").replace("ı", "i")
    return text.casefold().replace("\u0307", "")


def _is_synthetic_narrative_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> bool:
    """True when the table was invented from prose (Bölüm/İçerik), not real data."""
    if not header or not rows:
        return True
    labels = tuple(_label_key(h) for h in header if str(h or "").strip())
    if labels in {("bölüm", "içerik"), ("bolum", "icerik"), ("section", "content")}:
        return True
    return False


def _add_docx_page_footer(document: Any) -> None:
    """Word footer: 'Sayfa X / Y' via PAGE and NUMPAGES fields."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    footer = document.sections[0].footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p_el = paragraph._element
    for child in list(p_el):
        if not child.tag.endswith("}pPr"):
            p_el.remove(child)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def _field(paragraph_el: Any, instr: str) -> None:
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instr_el = OxmlElement("w:instrText")
        instr_el.set(qn("xml:space"), "preserve")
        instr_el.text = instr
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        text_el = OxmlElement("w:t")
        text_el.text = "1"
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        run = paragraph_el.add_run()
        run._r.append(begin)
        run._r.append(instr_el)
        run._r.append(separate)
        run._r.append(text_el)
        run._r.append(end)

    paragraph.add_run("Sayfa ")
    _field(paragraph, " PAGE ")
    paragraph.add_run(" / ")
    _field(paragraph, " NUMPAGES ")


def build_docx(
    title: str,
    body: str,
    *,
    header: Optional[Sequence[str]] = None,
    rows: Optional[Sequence[Sequence[Any]]] = None,
    footnote: str = "",
    images: Optional[Sequence["ImageEmbed"]] = None,
) -> bytes:
    """Render a structured .docx report (prose + optional data table)."""
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

    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    pf = normal.paragraph_format
    pf.space_after = Pt(10)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.15

    display_title = short_report_title(title, fallback="Rapor")
    document.add_heading(display_title, level=0)

    heading = display_title
    narrative, sources_tail = split_sources_section(body)
    rendered = False
    for kind, text, level in iter_body_blocks(narrative, title=heading):
        if kind == "heading":
            # add_heading(level=0) is the document Title style, already used
            # above -- keep body headings at 1+ so they read as subheadings.
            document.add_heading(text, level=min(max(level, 1), 4))
        elif kind == "bullet":
            document.add_paragraph(text, style="List Bullet")
        elif kind == "number":
            document.add_paragraph(text, style="List Number")
        elif kind == "table" and isinstance(text, list):
            _add_docx_matrix_table(document, text)
        else:
            document.add_paragraph(str(text))
        rendered = True

    # Embed images under the narrative (before Kaynaklar), sized to page.
    for img in (images or []):
        if img.path and img.path.is_file():
            try:
                # Cap width so the picture never overflows the page margins.
                document.add_picture(str(img.path), width=Cm(14))
            except Exception as exc:  # noqa: BLE001
                # Retry once after a Pillow re-encode for exotic JPEGs.
                try:
                    from io import BytesIO

                    from PIL import Image as PILImage

                    with PILImage.open(img.path) as pil:
                        if pil.mode not in {"RGB", "L"}:
                            pil = pil.convert("RGB")
                        elif pil.mode == "L":
                            pil = pil.convert("RGB")
                        buf = BytesIO()
                        pil.save(buf, format="JPEG", quality=82)
                        buf.seek(0)
                        document.add_picture(buf, width=Cm(14))
                except Exception:  # noqa: BLE001
                    pass
            if img.attribution:
                attr_p = document.add_paragraph()
                attr_run = attr_p.add_run(img.attribution)
                attr_run.italic = True
                attr_run.font.size = Pt(8)

    if sources_tail:
        for kind, text, level in iter_body_blocks(sources_tail):
            if kind == "heading":
                document.add_heading(text, level=min(max(level, 1), 4))
            elif kind == "bullet":
                document.add_paragraph(text, style="List Bullet")
            elif kind == "number":
                document.add_paragraph(text, style="List Number")
            elif kind == "table" and isinstance(text, list):
                _add_docx_matrix_table(document, text)
            else:
                document.add_paragraph(str(text))
            rendered = True

    use_header = [str(h) for h in (header or []) if str(h or "").strip()]
    use_rows = [list(r) for r in (rows or []) if isinstance(r, (list, tuple))]
    if use_header and use_rows and not _is_synthetic_narrative_table(use_header, use_rows):
        document.add_heading("Veri / Özet Tablo", level=2)
        col_count = min(len(use_header), 8)
        table = document.add_table(rows=1 + len(use_rows), cols=col_count)
        table.style = "Light Grid Accent 1"
        for c, name in enumerate(use_header[:col_count]):
            cell = table.rows[0].cells[c]
            cell.text = str(name)
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True
        for r_idx, row in enumerate(use_rows):
            for c in range(col_count):
                value = row[c] if c < len(row) else ""
                table.rows[r_idx + 1].cells[c].text = "" if value is None else str(value)
        rendered = True

    if footnote and str(footnote).strip():
        note = document.add_paragraph()
        run = note.add_run(str(footnote).strip())
        run.italic = True
        run.font.size = Pt(9)

    if not rendered and not (images or []):
        document.add_paragraph("This job did not produce a final synthesized answer.")

    _add_docx_page_footer(document)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# The WordprocessingML namespace every .docx element is qualified with.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def etree_tostring(node: Any) -> bytes:
    from lxml import etree

    return etree.tostring(node)


def etree_fromstring(payload: bytes) -> Any:
    from lxml import etree

    return etree.fromstring(payload)


def _pick_style(document: Any, candidates: Sequence[str]) -> Optional[str]:
    """First candidate style name that exists in this docx's styles, else None."""
    names = {s.name for s in document.styles}
    for name in candidates:
        if name in names:
            return name
    return None


def _apply_run_format(run: Any, base_rpr: Any) -> None:
    """Give a generated run the template's own face, size, and colour.

    Applied before bold, so python-docx adds <w:b/> into this rPr rather
    than creating a competing one. Bold and italic are dropped from the
    copy: they belong to the specific source run this was sampled from,
    not to body text in general.
    """
    if base_rpr is None:
        return
    import copy

    clone = copy.deepcopy(base_rpr)
    for tag in ("b", "bCs", "i", "iCs"):
        for node in clone.findall(f"{{{_W_NS}}}{tag}"):
            clone.remove(node)
    existing = run._r.find(f"{{{_W_NS}}}rPr")
    if existing is not None:
        run._r.remove(existing)
    run._r.insert(0, clone)


def _add_runs_with_bold(paragraph: Any, text: str, base_rpr: Any = None) -> None:
    """Write ``text`` into ``paragraph``, turning **spans** into bold runs.

    A model marks emphasis the way it marks everything else, in markdown.
    Stripping those markers (the default everywhere else, since most
    writers cannot render them) meant a template whose own body had 33
    bold runs came back with none: the words were right and every one of
    them was plain.
    """
    parts = INLINE_MARKS.split(text or "")
    # INLINE_MARKS has two groups, so split yields
    # [plain, marker, bold, plain, marker, bold, ...].
    index = 0
    while index < len(parts):
        chunk = parts[index]
        if index % 3 == 0:
            if chunk:
                _apply_run_format(paragraph.add_run(chunk), base_rpr)
            index += 1
        else:
            bold_text = parts[index + 1] if index + 1 < len(parts) else ""
            if bold_text:
                run = paragraph.add_run(bold_text)
                _apply_run_format(run, base_rpr)
                run.bold = True
            index += 2


def _clone_numbering(paragraph: Any, num_pr: Any, *, level: int = 0) -> None:
    """Give ``paragraph`` the template's own list numbering.

    Numbering lives on the paragraph, not in a style name: a template can
    number its items through a numId that no style references. Copying the
    element is what makes the generated list continue to look like the
    template's -- same glyph, same indent, same level.
    """
    if num_pr is None:
        return
    import copy

    ppr = paragraph._p.get_or_add_pPr()
    for existing in ppr.findall(f"{{{_W_NS}}}numPr"):
        ppr.remove(existing)
    clone = copy.deepcopy(num_pr)
    if level > 0:
        ilvl = clone.find(f"{{{_W_NS}}}ilvl")
        if ilvl is None:
            ilvl = clone.makeelement(f"{{{_W_NS}}}ilvl", {})
            clone.insert(0, ilvl)
        ilvl.set(f"{{{_W_NS}}}val", str(min(level, 8)))
    ppr.append(clone)


def _observed_template_styles(document: Any) -> dict[str, Any]:
    """What the template's own body actually uses, read before it is cleared.

    Guessing style names does not survive contact with a real document.
    A Word file authored in Turkish names its heading styles "Başlık 1"
    (id Balk1), so a lookup for "Heading 1"/"List Number" finds nothing,
    add_heading raises KeyError, and every heading, numbered item, and
    bold run collapses into plain body text -- measured on a live
    template: 17 styled paragraphs, 81 numbered items and 33 bold runs in,
    zero of each out, with the text itself perfectly correct.

    So read the styles off the paragraphs instead of naming them. Returns
    heading styles by level (in document order of first appearance),
    the style used by numbered paragraphs, and whether body text was bold.
    """
    headings: list[str] = []
    numbered: Optional[str] = None
    num_pr: Any = None
    by_level: dict[str, Any] = {}
    run_rpr: Any = None
    for paragraph in document.paragraphs:
        element = paragraph._p
        style_id = paragraph.style.name if paragraph.style is not None else None
        if style_id is None:
            continue
        # Numbering lives on the paragraph properties, not in the style name.
        if numbered is None and element.find(f"{{{_W_NS}}}pPr") is not None:
            ppr = element.find(f"{{{_W_NS}}}pPr")
            found = ppr.find(f"{{{_W_NS}}}numPr") if ppr is not None else None
            if found is not None:
                numbered = numbered or style_id
                if num_pr is None:
                    num_pr = found
                # Keep one example per outline level, so a nested list in
                # the source can be reproduced at the right depth instead
                # of being flattened to a single level.
                ilvl = found.find(f"{{{_W_NS}}}ilvl")
                depth = ilvl.get(f"{{{_W_NS}}}val") if ilvl is not None else "0"
                by_level.setdefault(str(depth), found)
        low = style_id.casefold()
        looks_like_heading = (
            low.startswith("heading")
            or low.startswith("başlık")
            or low.startswith("baslik")
            or low.startswith("balk")
            or low in {"title", "başlik", "subtitle"}
        )
        if looks_like_heading and style_id not in headings:
            headings.append(style_id)
    # The body's prevailing run format, rebuilt from what most of the
    # template's own runs say -- not copied from the first run found.
    #
    # Sampling one run is not enough because a template spreads its
    # formatting across runs: the one sampled here carried only <w:sz>,
    # while the face lived on other runs as an explicit <w:rFonts>. The
    # generated text then inherited the face from docDefaults, which in
    # this file is the THEME font -- so a Times New Roman specification
    # came back in Calibri, at the right size.
    #
    # Counting instead of sampling also makes this generic: whatever the
    # uploaded document mostly looks like is what the new body gets, with
    # no assumption about which property lives where.
    fonts: Counter = Counter()
    sizes: Counter = Counter()
    heading_rprs: Counter = Counter()
    for paragraph in document.paragraphs:
        name = str(paragraph.style.name).casefold() if paragraph.style is not None else ""
        if name.startswith(("heading", "başlık", "baslik", "balk", "title")):
            # Headings need their own sample. Sharing the style name is not
            # enough: the template's headings carry run-level colour that
            # overrides the style's, so generated headings that inherit the
            # style came out in the theme's blue against the template's
            # black -- same style, visibly different heading.
            for run in paragraph.runs:
                rpr = run._r.find(f"{{{_W_NS}}}rPr")
                if rpr is not None and run.text.strip():
                    heading_rprs[etree_tostring(rpr)] += 1
            continue
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            rpr = run._r.find(f"{{{_W_NS}}}rPr")
            if rpr is None:
                continue
            rfonts = rpr.find(f"{{{_W_NS}}}rFonts")
            if rfonts is not None:
                fonts[etree_tostring(rfonts)] += 1
            size = rpr.find(f"{{{_W_NS}}}sz")
            if size is not None:
                sizes[size.get(f"{{{_W_NS}}}val")] += 1

    if fonts or sizes:
        run_rpr = document.element.makeelement(f"{{{_W_NS}}}rPr", {})
        if fonts:
            run_rpr.append(etree_fromstring(fonts.most_common(1)[0][0]))
        if sizes:
            common = sizes.most_common(1)[0][0]
            for tag in ("sz", "szCs"):
                node = run_rpr.makeelement(f"{{{_W_NS}}}{tag}", {})
                node.set(f"{{{_W_NS}}}val", common)
                run_rpr.append(node)

    heading_rpr = (
        etree_fromstring(heading_rprs.most_common(1)[0][0]) if heading_rprs else None
    )

    return {
        "headings": headings,
        "numbered": numbered,
        "num_pr": num_pr,
        "num_pr_by_level": by_level,
        "run_rpr": run_rpr,
        "heading_rpr": heading_rpr,
    }


def template_outline(template_path: Path, *, limit: int = 80) -> list[str]:
    """The section labels an uploaded template actually contains.

    Told only to "reproduce every section", the model still shortened the
    tail: measured on a live rewrite, the last four clauses (Tapu ve Ruhsat
    İşlemleri, Noter Sözleşmesi, Kombiler, Kontrol Temsilcisi) were simply
    absent, while everything before them was faithful. A checklist it can
    compare against turns "write it all" into something checkable.

    Two kinds of label, because templates mark sections either way: a real
    heading style, or a bold/colon-prefixed lead-in inside body text. The
    text before the first colon is the label -- that is the shape these
    documents use throughout.

    Never raises: an unreadable template just yields no checklist, and the
    rewrite proceeds exactly as it did before.
    """
    try:
        import docx

        document = docx.Document(str(template_path))
    except Exception:  # noqa: BLE001
        return []

    labels: list[str] = []
    seen: set[str] = set()
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = str(paragraph.style.name).casefold() if paragraph.style is not None else ""
        is_heading = style.startswith(
            ("heading", "başlık", "baslik", "balk", "title")
        )
        label = text
        if not is_heading:
            match = re.match(r"^([^:]{3,60}):", text)
            if not match:
                continue
            label = match.group(1)
        label = " ".join(label.split())[:60]
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def template_paragraphs(template_path: Path, *, limit: int = 400) -> list[str]:
    """The template's own paragraph texts, in order, for editing in place.

    This is the input side of edit_docx_template: the model is shown these
    numbered and returns only the ones whose wording changes.
    """
    try:
        import docx

        document = docx.Document(str(template_path))
    except Exception:  # noqa: BLE001
        return []
    return [(par.text or "").strip() for par in document.paragraphs][:limit]


def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """Replace a paragraph's words while keeping how it is written.

    The first run keeps its own formatting and takes the new text; the rest
    are dropped. That preserves the paragraph's style, list membership,
    indentation and face -- everything except the words -- which is the
    whole point of editing rather than regenerating.

    A paragraph whose original had a bold lead-in ("**Yapı Denetimi:**
    açıklama") loses the split between bold label and plain body, so
    markers in the replacement are re-applied as real runs instead.
    """
    runs = paragraph.runs
    base_rpr = None
    if runs:
        found = runs[0]._r.find(f"{{{_W_NS}}}rPr")
        if found is not None:
            import copy

            base_rpr = copy.deepcopy(found)
    for run in list(runs):
        run._r.getparent().remove(run._r)
    _add_runs_with_bold(paragraph, text, base_rpr)


def edit_docx_template(
    template_path: Path, edits: dict[int, str]
) -> bytes:
    """Rewrite only the paragraphs named in ``edits``; keep the rest exactly.

    The alternative -- clearing the body and asking the model to write the
    whole document back -- costs a full regeneration every time. Measured on
    a 101-paragraph specification: ~9000 output tokens at this model's ~30
    tokens/sec, so about five minutes, and the document came back with 95
    paragraphs, bold on 84 runs instead of 33, and four clauses missing.
    None of that is recoverable by prompting, because a model asked to
    reproduce 9800 characters will not reproduce them exactly.

    Editing in place removes the cause. Paragraphs the model did not touch
    are never rewritten, so they cannot drift, and the model writes only
    what actually changes -- a rename touches perhaps eight paragraphs, a
    few hundred tokens, seconds rather than minutes.

    ``edits`` maps paragraph index (as given by template_paragraphs) to its
    new text. An empty string deletes the paragraph's words but keeps the
    paragraph; an index that is not present is left completely untouched.
    """
    import docx

    document = docx.Document(str(template_path))
    paragraphs = document.paragraphs
    for index, text in edits.items():
        if not isinstance(index, int) or not 0 <= index < len(paragraphs):
            continue
        _set_paragraph_text(paragraphs[index], str(text))

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def fill_docx_template(template_path: Path, title: str, body: str) -> bytes:
    """Write NEW content into an uploaded .docx, reusing its own fonts,
    margins, headers/footers, and styles ("same template" requests).

    Opens the template and clears ONLY its body content
    (element.body.clear_content()) -- this keeps section properties
    (margins, page size, header/footer wiring) and every style definition
    untouched, since both live outside the body. This is a v1, "replace
    the whole body" behavior, not field-level substitution: it does not
    try to find and fill specific blanks inside the template's existing
    text.
    """
    import docx

    document = docx.Document(str(template_path))

    # Read before clearing: once the body is gone there is nothing left to
    # learn the styles from, and their names cannot be guessed (a
    # Turkish-authored file calls them "Başlık 1", not "Heading 1").
    observed = _observed_template_styles(document)
    observed_headings: list[str] = observed["headings"]
    observed_number: Optional[str] = observed["numbered"]
    observed_num_pr = observed["num_pr"]
    observed_run_rpr = observed["run_rpr"]
    observed_heading_rpr = observed["heading_rpr"]
    if observed_heading_rpr is None:
        observed_heading_rpr = observed["run_rpr"]

    document.element.body.clear_content()  # keeps the trailing sectPr

    heading_style = (observed_headings[0] if observed_headings else None) or _pick_style(
        document, ["Heading 1", "Title", "Başlık 1"]
    )
    bullet_style = _pick_style(
        document, ["List Bullet", "List Paragraph", "Liste Paragrafı"]
    )
    number_style = observed_number or _pick_style(
        document, ["List Number", "List Paragraph", "Liste Paragrafı"]
    )

    def heading_for(level: int) -> Optional[str]:
        """The style this template itself uses at that depth."""
        if observed_headings:
            return observed_headings[min(level, len(observed_headings)) - 1]
        return heading_style

    display_title = short_report_title(title, fallback="")
    if display_title:
        try:
            document.add_heading(display_title, level=0)
        except KeyError:
            document.add_paragraph(display_title)

    narrative, sources_tail = split_sources_section(body, keep_bold=True)
    for section_text in (narrative, sources_tail):
        if not section_text:
            continue
        for kind, text, level in iter_body_blocks(
            section_text, title=display_title, keep_bold=True
        ):
            if kind == "heading":
                depth = min(max(level, 1), 4)
                style_name = heading_for(depth)
                p = document.add_paragraph()
                _add_runs_with_bold(p, str(text), observed_heading_rpr)
                if style_name:
                    # The template's own heading style, applied directly.
                    # add_heading would look up "Heading N" by name and
                    # raise on a document that does not use English names.
                    try:
                        p.style = style_name
                    except KeyError:
                        pass
            elif kind == "bullet":
                p = document.add_paragraph()
                _add_runs_with_bold(p, str(text), observed_run_rpr)
                if bullet_style:
                    try:
                        p.style = bullet_style
                    except KeyError:
                        pass
                # A Word bullet is numPr too, pointing at a bullet-format
                # list. Without this a bulleted template came back as flat
                # paragraphs: the marker and the indent both live here.
                _clone_numbering(p, observed_num_pr, level=level)
            elif kind == "number":
                p = document.add_paragraph()
                _add_runs_with_bold(p, str(text), observed_run_rpr)
                if number_style:
                    try:
                        p.style = number_style
                    except KeyError:
                        pass
                # Numbering lives on the paragraph, not the style: the
                # template numbers 81 of its items through a numId no style
                # references, so without this the list came out unnumbered.
                _clone_numbering(p, observed_num_pr, level=level)
            elif kind == "table" and isinstance(text, list):
                _add_docx_matrix_table(document, text)
            else:
                p = document.add_paragraph()
                _add_runs_with_bold(p, str(text), observed_run_rpr)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_PDF_FONT_CACHE: list[str] = []


def _register_pdf_font() -> str:
    """Register a Unicode-capable font family and return its name.

    reportlab's built-in Helvetica is Latin-1 only, so characters outside it
    -- Turkish ş/ı/İ/ğ, Polish ł, Czech č, etc. -- render as solid black
    boxes. Bitstream Vera ships inside the reportlab package itself and does
    cover Latin Extended-A, so it needs no new dependency, no system font,
    and behaves the same on any OS. Falls back to Helvetica if registration
    ever fails, so a font problem degrades rather than breaking the export.
    """
    if _PDF_FONT_CACHE:
        return _PDF_FONT_CACHE[0]

    import os

    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    family = "Helvetica"
    try:
        fonts_dir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
        variants = {
            "Vera": "Vera.ttf",
            "Vera-Bold": "VeraBd.ttf",
            "Vera-Italic": "VeraIt.ttf",
            "Vera-BoldItalic": "VeraBI.ttf",
        }
        for name, filename in variants.items():
            pdfmetrics.registerFont(TTFont(name, os.path.join(fonts_dir, filename)))
        pdfmetrics.registerFontFamily(
            "Vera",
            normal="Vera",
            bold="Vera-Bold",
            italic="Vera-Italic",
            boldItalic="Vera-BoldItalic",
        )
        family = "Vera"
    except Exception:  # noqa: BLE001 - fall back rather than fail the export
        family = "Helvetica"

    _PDF_FONT_CACHE.append(family)
    return family


def _pdf_page_canvas_factory():
    """Canvas that stamps 'Sayfa N / M' after the total page count is known."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas

    class NumberedCanvas(pdf_canvas.Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pdf_canvas.Canvas.__init__(self, *args, **kwargs)
            self._saved_page_states: list[dict[str, Any]] = []

        def showPage(self) -> None:  # noqa: N802
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            page_count = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self.setFont("Helvetica", 9)
                label = f"Sayfa {self._pageNumber} / {page_count}"
                self.drawCentredString(A4[0] / 2.0, 12 * mm, label)
                pdf_canvas.Canvas.showPage(self)
            pdf_canvas.Canvas.save(self)

    return NumberedCanvas


def build_pdf(title: str, body: str, *, images: Optional[Sequence["ImageEmbed"]] = None) -> bytes:
    """Render the final answer as a .pdf document."""
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
    from reportlab.platypus import Image as RLImage

    from xml.sax.saxutils import escape

    font = _register_pdf_font()
    bold_font = f"{font}-Bold" if font == "Vera" else "Helvetica-Bold"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "JobTitle",
        parent=styles["Title"],
        alignment=TA_LEFT,
        fontName=bold_font,
        fontSize=18,
        leading=23,
        spaceAfter=10,
    )
    body_style = ParagraphStyle(
        "JobBody",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=10.5,
        leading=15.5,
        spaceAfter=8,
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=_safe_title(short_report_title(title, fallback="Rapor")),
    )

    story: list = [
        Paragraph(escape(short_report_title(title, fallback="Rapor")), title_style),
        Spacer(1, 4),
    ]

    attr_style = ParagraphStyle(
        "ImageAttr", parent=styles["BodyText"], fontName=font,
        fontSize=8, leading=10, spaceAfter=6, textColor="grey",
    )
    max_img_width = A4[0] - 50 * mm
    max_img_height = 95 * mm
    heading = short_report_title(title, fallback="Rapor")
    narrative, sources_tail = split_sources_section(body)

    # Consecutive list items are grouped so reportlab renders one list rather
    # than a run of separate single-item lists.
    pending: list[tuple[str, str]] = []

    def flush_list() -> None:
        if not pending:
            return
        kind = pending[0][0]
        story.append(
            ListFlowable(
                [ListItem(Paragraph(escape(text), body_style)) for _, text in pending],
                bulletType="bullet" if kind == "bullet" else "1",
                leftIndent=14,
                bulletFontSize=9,
            )
        )
        story.append(Spacer(1, 4))
        pending.clear()

    def append_blocks(source: str) -> bool:
        local_rendered = False
        for kind, text, level in iter_body_blocks(source, title=heading):
            if kind in {"bullet", "number"}:
                if pending and pending[0][0] != kind:
                    flush_list()
                pending.append((kind, str(text)))
                local_rendered = True
                continue
            flush_list()
            if kind == "heading":
                heading_style = ParagraphStyle(
                    f"JobHeading{level}",
                    parent=styles["Heading2" if level <= 2 else "Heading3"],
                    fontName=bold_font,
                    spaceBefore=10,
                    spaceAfter=5,
                )
                story.append(Paragraph(escape(str(text)), heading_style))
            elif kind == "table" and isinstance(text, list):
                _add_pdf_matrix_table(story, text, font=font, bold_font=bold_font)
            else:
                story.append(Paragraph(escape(str(text)), body_style))
            local_rendered = True
        flush_list()
        return local_rendered

    rendered = append_blocks(narrative)

    # Embed images under the narrative (before Kaynaklar), sized to the frame.
    for img in (images or []):
        if img.path and img.path.is_file():
            try:
                rl_img = RLImage(str(img.path))
                iw = float(rl_img.imageWidth or 1)
                ih = float(rl_img.imageHeight or 1)
                scale = min(max_img_width / iw, max_img_height / ih, 1.0)
                rl_img.drawWidth = iw * scale
                rl_img.drawHeight = ih * scale
                rl_img.hAlign = "CENTER"
                story.append(rl_img)
            except Exception:  # noqa: BLE001
                pass
            if img.attribution:
                story.append(Paragraph(escape(img.attribution), attr_style))
            story.append(Spacer(1, 6))

    if sources_tail:
        rendered = append_blocks(sources_tail) or rendered

    if not rendered and not (images or []):
        story.append(
            Paragraph("This job did not produce a final synthesized answer.", body_style)
        )

    document.build(story, canvasmaker=_pdf_page_canvas_factory())
    return buffer.getvalue()


def to_markdown(title: str, body: str) -> str:
    """Structured markdown suitable for .md / readable .txt artifacts."""
    heading = _safe_title(title)
    structured = structure_prose(body)
    if not structured:
        return f"# {heading}\n"
    # Avoid duplicating the title if the body already starts with it.
    first = structured.lstrip().split("\n", 1)[0].lstrip("# ").strip()
    if first.lower() == heading.lower():
        return structured if structured.startswith("#") else f"# {structured}"
    return f"# {heading}\n\n{structured}\n"


MAX_PPTX_TABLE_ROWS = 20
MAX_PPTX_SECTION_SLIDES = 12
MAX_PPTX_BULLETS_PER_SLIDE = 4
MAX_PPTX_BULLET_CHARS = 100
_PPTX_GENERIC_TITLES = frozenset(
    {
        "bölüm",
        "bolum",
        "özet",
        "ozet",
        "section",
        "summary",
        "content",
        "içerik",
        "icerik",
    }
)
_PPTX_SOURCE_JUNK_RE = re.compile(
    r"(?is)gizlilik|tercihleriniz|cookie|consent|sign[\s-]?in|chevron|"
    r"privacy|kabul et|reject all|accept all|geri çevir"
)


# Deck palette — dark research theme (not generic purple).
_PPTX_BG = "07111D"
_PPTX_BG_SOFT = "0B1624"
_PPTX_ACCENT = "3ECFFF"
_PPTX_ACCENT_WARM = "E8A87C"
_PPTX_TEXT = "E8EEF6"
_PPTX_MUTED = "8B97A8"
_PPTX_DIM = "5D6B7C"
_PPTX_BULLET = "C5D0DC"
_PPTX_CARD = "101C2C"
_PPTX_CARD_ALT = "0C1828"


def _pptx_rgb(hex_color: str):
    from pptx.dml.color import RGBColor

    value = hex_color.lstrip("#")
    return RGBColor(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _pptx_set_run(run, *, size_pt: float, bold: bool = False, color: str = _PPTX_TEXT) -> None:
    from pptx.util import Pt

    run.font.name = "Calibri"
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    run.font.color.rgb = _pptx_rgb(color)


def _pptx_fill_slide(slide, hex_color: str = _PPTX_BG) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _pptx_rgb(hex_color)


def _pptx_add_textbox(
    slide,
    left,
    top,
    width,
    height,
    text: str,
    *,
    size_pt: float,
    bold: bool = False,
    color: str = _PPTX_TEXT,
    align_center: bool = False,
):
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Pt

    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.clear()
    if align_center:
        p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = text
    _pptx_set_run(run, size_pt=size_pt, bold=bold, color=color)
    p.space_after = Pt(4)
    return box


def _pptx_shape_rect(slide, left, top, width, height, fill_hex: str) -> None:
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _pptx_rgb(fill_hex)
    shape.line.fill.background()


def _pptx_add_top_bar(slide, prs) -> None:
    from pptx.util import Inches

    _pptx_shape_rect(
        slide, Inches(0), Inches(0), prs.slide_width, Inches(0.08), _PPTX_ACCENT
    )


def _pptx_add_left_rail(slide, prs) -> None:
    from pptx.util import Inches

    _pptx_shape_rect(
        slide, Inches(0), Inches(0), Inches(0.14), prs.slide_height, _PPTX_ACCENT
    )


def _pptx_add_heading_rule(slide, *, left, top, width) -> None:
    from pptx.util import Inches

    _pptx_shape_rect(slide, left, top, width, Inches(0.035), _PPTX_ACCENT)


def _pptx_add_photo_slide(prs, blank, images: Optional[Sequence["ImageEmbed"]]) -> None:
    """A dedicated slide per fetched topic photo (up to 2, matching the HTML
    deck's cap) -- the title slide intentionally skips photos (they clip the
    headline; see build_pptx), so this is the only place a PPTX deck embeds
    one. Mirrors build_docx's embed + attribution caption, sized to fit a
    fixed content box while preserving the image's own aspect ratio (a
    portrait photo at a flat inches(width) would otherwise overflow the
    slide's height)."""
    from pptx.util import Inches

    max_w_in, max_h_in, top_in = 9.2, 5.15, 1.05
    for img in list(images or [])[:2]:
        path = getattr(img, "path", None)
        if not (path and Path(path).is_file()):
            continue
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as pil:
                native_w, native_h = pil.size
        except Exception:  # noqa: BLE001
            continue
        if native_w <= 0 or native_h <= 0:
            continue
        aspect = native_w / native_h
        if max_w_in / aspect <= max_h_in:
            width_in, height_in = max_w_in, max_w_in / aspect
        else:
            width_in, height_in = max_h_in * aspect, max_h_in
        left_in = (13.333 - width_in) / 2

        slide = prs.slides.add_slide(blank)
        _pptx_fill_slide(slide)
        _pptx_add_top_bar(slide, prs)
        _pptx_add_left_rail(slide, prs)
        try:
            slide.shapes.add_picture(
                str(path),
                Inches(left_in),
                Inches(top_in),
                width=Inches(width_in),
                height=Inches(height_in),
            )
        except Exception:  # noqa: BLE001
            # Retry once after a Pillow re-encode, matching build_docx.
            try:
                from io import BytesIO

                with PILImage.open(path) as pil:
                    if pil.mode not in {"RGB", "L"}:
                        pil = pil.convert("RGB")
                    buf = BytesIO()
                    pil.save(buf, format="JPEG", quality=82)
                    buf.seek(0)
                    slide.shapes.add_picture(
                        buf,
                        Inches(left_in),
                        Inches(top_in),
                        width=Inches(width_in),
                        height=Inches(height_in),
                    )
            except Exception:  # noqa: BLE001
                # Both embed attempts failed -- drop the now-empty slide via
                # python-pptx's only removal path (no public API exists).
                # Best-effort: if this private structure ever changes shape,
                # leave one blank chrome-only slide rather than crash the
                # whole deck.
                try:
                    id_list = prs.slides._sldIdLst
                    id_list.remove(id_list[-1])
                except Exception:  # noqa: BLE001
                    pass
                continue
        attribution = str(getattr(img, "attribution", "") or "").strip()
        if attribution:
            _pptx_add_textbox(
                slide,
                Inches(left_in),
                Inches(top_in + height_in + 0.15),
                Inches(width_in),
                Inches(0.4),
                attribution[:240],
                size_pt=11,
                color=_PPTX_DIM,
                align_center=True,
            )


def _pptx_title_key(text: str) -> str:
    cleaned = re.sub(
        r"(?is)\b(?:ara[sş]t[iı]rma\s+raporu|raporu?|özeti?|ozeti?|sunumu?)\b",
        " ",
        text or "",
    )
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip().casefold()


def _pptx_titles_similar(a: str, b: str) -> bool:
    ka, kb = _pptx_title_key(a), _pptx_title_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    return len(shorter) >= 6 and shorter in longer


def _pptx_clip_bullet(text: str, limit: int = MAX_PPTX_BULLET_CHARS) -> str:
    cleaned = _clean_inline(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1]
    for sep in (". ", "; ", ", ", " "):
        idx = cut.rfind(sep)
        if idx >= max(40, limit // 3):
            cut = cut[: idx + (1 if sep.startswith((".", ";")) else 0)]
            break
    return cut.rstrip(" ,;") + "…"


def _pptx_title_from_text(text: str) -> str:
    words = _clean_inline(text or "").split()
    if not words:
        return "Konu"
    return " ".join(words[:6])[:60]


def _pptx_is_sources_title(title: str) -> bool:
    key = (title or "").casefold().strip()
    return key.startswith("kaynak") or key in {"sources", "references", "bibliography"}


def _pptx_is_generic_title(title: str) -> bool:
    return (title or "").casefold().strip() in _PPTX_GENERIC_TITLES


def _md_table_row_to_bullet(line: str) -> Optional[str]:
    """A markdown table row as a readable bullet, or None if not one.

    A slide has no table, so a row reaching the bullet builder was
    previously emitted with its pipes intact -- "| Tür | Devlet
    üniversitesi |" on screen, which reads as leaked markup rather than
    the label/value pair it is. Two cells become "label: value"; more
    become a middot-separated line, which is the only sensible shape when
    a row has no obvious label. Separator rows (|---|---|) are dropped.

    Returns None for anything that is not a table row, so ordinary
    prose -- including a sentence that merely contains a pipe -- is left
    exactly as it was.
    """
    if _is_md_table_separator(line):
        return ""
    if not _looks_like_md_table_row(line):
        return None
    cells = [c.strip() for c in _md_table_cells(line)]
    cells = [c for c in cells if c]
    if not cells:
        return ""
    if len(cells) == 1:
        return cells[0]
    if len(cells) == 2:
        return f"{cells[0]}: {cells[1]}"
    return " · ".join(cells)


def _pptx_paras_to_bullets(paras: Sequence[str]) -> list[str]:
    """Turn paragraphs into short one-idea bullets for a single slide."""
    bullets: list[str] = []
    last_was_row = False
    for para in paras or []:
        for raw_line in str(para or "").split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            as_row = _md_table_row_to_bullet(line)
            if as_row is not None:
                if not as_row:
                    # A separator row. It says the line before it was the
                    # table's HEADER, which as a bullet is only column
                    # labels -- a live slide opened with "Özellik: Detay",
                    # which names nothing. Drop it now that we know.
                    if bullets and last_was_row:
                        bullets.pop()
                    last_was_row = False
                    continue
                last_was_row = True
                line = as_row
            else:
                last_was_row = False
            if BULLET_PREFIX.match(line):
                piece = BULLET_PREFIX.sub("", line)
            else:
                numbered = NUMBERED_PREFIX.match(line)
                if numbered and len(numbered.group(0)) < 8:
                    piece = NUMBERED_PREFIX.sub("", line)
                else:
                    piece = line
            piece = _clean_inline(piece).strip()
            if not piece:
                continue
            # Always break long lines into sentence-sized bullets.
            if len(piece) > MAX_PPTX_BULLET_CHARS:
                parts = [
                    s.strip()
                    for s in re.split(r"(?<=[.!?…])\s+", piece)
                    if len(s.strip()) >= 18
                ]
                if not parts:
                    parts = [piece]
            else:
                parts = [piece]
            for part in parts:
                clipped = _pptx_clip_bullet(part)
                if clipped and clipped not in bullets:
                    bullets.append(clipped)
                if len(bullets) >= MAX_PPTX_BULLETS_PER_SLIDE * MAX_PPTX_SECTION_SLIDES:
                    return bullets
    return bullets


def _pptx_sections_from_prose(prose: str) -> list[tuple[str, list[str]]]:
    """Sections → (title, paragraphs) for slides, via the shared block parser.

    This used to detect headings itself, recognising only "#" lines and
    numbered ones -- and the numbered branch only while no section was
    already open. A report whose sections are plain short lines, or one
    that numbers every section, therefore collapsed into a single slide:
    a live London deck came out as a title, an image caption, ONE bullet
    and the sources, while the HTML deck built from the identical text
    carried every section. Same text, two parsers, two answers.

    Tables are flattened into lines rather than dropped -- a slide has no
    table, and a section that was one is otherwise an empty slide.
    """
    structured = structure_prose(prose)
    section_buf: list[tuple[str, list[str]]] = []
    current_title = ""
    current_paras: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_paras
        paras = [p for p in current_paras if p.strip()]
        if current_title or paras:
            section_buf.append((current_title or "Bölüm", paras))
        current_title = ""
        current_paras = []

    for kind, text, _level in iter_body_blocks(structured):
        if kind == "heading":
            flush()
            current_title = str(text).strip()
            continue
        if kind == "table" and isinstance(text, list):
            header = [str(c).strip() for c in (text[0] if text else [])]
            for row in text[1:]:
                cells = [str(c).strip() for c in row if str(c).strip()]
                if not cells:
                    continue
                if header and len(cells) == len(header) and len(cells) > 2:
                    current_paras.append(
                        " · ".join(
                            f"{h}: {c}" if h else c for h, c in zip(header, cells)
                        )
                    )
                elif len(cells) == 2:
                    current_paras.append(f"{cells[0]}: {cells[1]}")
                else:
                    current_paras.append(" · ".join(cells))
            continue
        line = str(text).strip()
        if line:
            current_paras.append(line)
    flush()
    return section_buf


def _pptx_chunk_bullets(
    title: str, bullets: list[str]
) -> list[tuple[str, list[str]]]:
    """Split long bullet lists across slides (max N bullets each)."""
    if not bullets:
        return []
    if len(bullets) <= MAX_PPTX_BULLETS_PER_SLIDE:
        label = title if title and not _pptx_is_generic_title(title) else _pptx_title_from_text(
            bullets[0]
        )
        return [(label, bullets)]
    out: list[tuple[str, list[str]]] = []
    base = title if title and not _pptx_is_generic_title(title) else ""
    for i in range(0, len(bullets), MAX_PPTX_BULLETS_PER_SLIDE):
        chunk = bullets[i : i + MAX_PPTX_BULLETS_PER_SLIDE]
        part = i // MAX_PPTX_BULLETS_PER_SLIDE
        if base:
            # Avoid ugly "Başlık (2)" labels — keep a clean continuation mark.
            label = base if part == 0 else f"{base} · devam"
        else:
            label = _pptx_title_from_text(chunk[0])
        out.append((label, chunk))
    return out


def _pptx_deck_outline(prose: str) -> list[tuple[str, list[str]]]:
    """Build (title, bullets) slides from prose — never one wall-of-text slide."""
    body = (prose or "").strip()
    if not body:
        return []

    # Drop trailing Kaynaklar block from section parsing (handled separately).
    body_for_sections = re.split(
        r"(?is)\n(?:#{1,3}\s*)?kaynaklar\s*:?\s*\n", body, maxsplit=1
    )[0]

    raw = _pptx_sections_from_prose(body_for_sections)
    outline: list[tuple[str, list[str]]] = []
    for sec_title, paras in raw:
        if _pptx_is_sources_title(sec_title):
            continue
        bullets = _pptx_paras_to_bullets(paras)
        if not bullets:
            continue
        outline.extend(_pptx_chunk_bullets(sec_title, bullets))

    # Wall of text / single generic dump → sentence groups as multiple slides.
    if len(outline) <= 1:
        only_title = outline[0][0] if outline else ""
        only_bullets = outline[0][1] if outline else _pptx_paras_to_bullets(
            [p for p in re.split(r"\n{2,}", body_for_sections) if p.strip()]
        )
        if not only_bullets:
            only_bullets = _pptx_paras_to_bullets([body_for_sections])
        if only_bullets and (
            not outline
            or _pptx_is_generic_title(only_title)
            or len(only_bullets) > MAX_PPTX_BULLETS_PER_SLIDE
        ):
            outline = _pptx_chunk_bullets(
                "" if _pptx_is_generic_title(only_title) else only_title,
                only_bullets,
            )

    return outline[:MAX_PPTX_SECTION_SLIDES]


def _pptx_extract_sources(prose: str) -> list[str]:
    lines: list[str] = []
    m = re.search(r"(?is)(?:^|\n)(?:#{1,3}\s*)?kaynaklar\s*:?\s*\n(.+)$", prose or "")
    block = m.group(1) if m else ""
    if not block:
        m2 = re.search(r"(?is)kaynaklar\s*:\s*(.+)$", prose or "")
        block = m2.group(1) if m2 else ""
    for ln in block.splitlines():
        text = ln.strip()
        if not text or text.casefold().startswith("kaynaklar"):
            continue
        text = BULLET_PREFIX.sub("", text)
        text = NUMBERED_PREFIX.sub("", text)
        text = _clean_inline(text).strip()
        if not text or _PPTX_SOURCE_JUNK_RE.search(text):
            continue
        if text.lstrip().startswith("#") or text.casefold().startswith("##"):
            continue
        # Prefer a clean URL if the line is cookie/UI polluted around a link.
        urls = re.findall(r"https?://[^\s\)\]\>\"']+", text)
        if urls and (len(text) > 140 or _PPTX_SOURCE_JUNK_RE.search(text)):
            text = urls[0].rstrip(".,);]")
        else:
            text = text[:110]
        if text and text not in lines:
            lines.append(text)
        if len(lines) >= 8:
            break
    return lines


def _pptx_fill_bullet_frame(tf, bullets: Sequence[str], *, size_pt: float = 18) -> None:
    from pptx.util import Pt

    first = True
    for bullet in bullets:
        text = str(bullet or "").strip()
        if not text:
            continue
        text = re.sub(r"^[\s•\-–—*]+", "", text).strip()
        if not text:
            continue
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.clear()
        mark = p.add_run()
        mark.text = "●  "
        _pptx_set_run(mark, size_pt=size_pt - 1, color=_PPTX_ACCENT)
        body = p.add_run()
        body.text = text
        _pptx_set_run(body, size_pt=size_pt, color=_PPTX_BULLET)
        p.space_after = Pt(16)
        p.line_spacing = 1.28
        p.level = 0


def _pptx_fill_numbered_frame(
    tf, items: Sequence[str], *, size_pt: float = 18
) -> None:
    from pptx.util import Pt

    first = True
    for idx, item in enumerate(items, start=1):
        text = str(item or "").strip()
        if not text:
            continue
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.clear()
        num = p.add_run()
        num.text = f"{idx:02d}  "
        _pptx_set_run(num, size_pt=size_pt, bold=True, color=_PPTX_ACCENT)
        body = p.add_run()
        body.text = text
        _pptx_set_run(body, size_pt=size_pt, color=_PPTX_TEXT)
        p.space_after = Pt(18)
        p.line_spacing = 1.3
        p.level = 0


def _pptx_content_heading(sec_title: str, display_title: str, bullets: Sequence[str]) -> str:
    heading = (sec_title or "").strip()
    if not heading or _pptx_is_generic_title(heading):
        heading = _pptx_title_from_text(bullets[0] if bullets else "Özet")
    if _pptx_titles_similar(heading, display_title):
        return "Özet"
    # Strip continuation noise for display length.
    heading = re.sub(r"\s*·\s*devam\s*$", " · devam", heading).strip()
    return heading[:72]


def build_pptx(
    title: str,
    body: str,
    *,
    header: Optional[Sequence[str]] = None,
    rows: Optional[Sequence[Sequence[Any]]] = None,
    images: Optional[Sequence["ImageEmbed"]] = None,
) -> bytes:
    """Render a dark, structured research deck as .pptx (python-pptx).

    Skeleton: Title → Agenda → section slides (short bullets) → optional
    data table → Sources. Wall-of-text input is split into bullet slides.
    """
    from datetime import date as _date

    from pptx import Presentation
    from pptx.util import Inches

    prose = sanitize_report_prose(body or "")
    outline = _pptx_deck_outline(prose)
    raw_title = (title or "").strip() or "Sunum"
    if (
        _looks_like_bad_title(raw_title)
        or _looks_like_chatty_title(raw_title)
        or raw_title.lstrip().startswith(("-", "•", "*"))
    ):
        display_title = resolve_presentation_title(
            "", document_title="", body=prose, fallback="Sunum"
        )
    else:
        display_title = short_report_title(raw_title, fallback=raw_title)
    if (
        _looks_like_bad_title(display_title)
        or _looks_like_chatty_title(display_title)
        or len(display_title) < 3
    ):
        display_title = resolve_presentation_title(
            "", document_title="", body=prose, fallback="Sunum"
        )
    source_lines = _pptx_extract_sources(prose)

    teaser = ""
    if outline and outline[0][1]:
        teaser = _pptx_clip_bullet(outline[0][1][0], limit=110)
    elif prose:
        for chunk in re.split(r"\n{2,}", prose):
            chunk = chunk.strip()
            if len(chunk) >= 40 and not chunk.casefold().startswith("kaynak"):
                teaser = _pptx_clip_bullet(chunk, limit=110)
                break

    # Prefer a short tagline over a near-duplicate section title.
    subtitle = ""
    for sec_title, _bullets in outline:
        candidate = (sec_title or "").strip()
        if (
            candidate
            and not _pptx_is_generic_title(candidate)
            and not _pptx_is_sources_title(candidate)
            and not _pptx_titles_similar(candidate, display_title)
        ):
            subtitle = candidate[:70]
            break

    _months = (
        "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
    )
    today = _date.today()
    prepared = f"{today.day} {_months[today.month - 1]} {today.year}"

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # --- Title slide -------------------------------------------------------
    title_slide = prs.slides.add_slide(blank)
    _pptx_fill_slide(title_slide)
    _pptx_add_left_rail(title_slide, prs)
    # Soft bottom band for date / chrome
    _pptx_shape_rect(
        title_slide,
        Inches(0),
        Inches(6.85),
        prs.slide_width,
        Inches(0.65),
        _PPTX_BG_SOFT,
    )
    _pptx_add_textbox(
        title_slide,
        Inches(1.05),
        Inches(1.55),
        Inches(11.2),
        Inches(0.4),
        "ARAŞTIRMA SUNUMU",
        size_pt=12,
        bold=True,
        color=_PPTX_MUTED,
    )
    _pptx_add_textbox(
        title_slide,
        Inches(1.05),
        Inches(2.15),
        Inches(11.2),
        Inches(1.7),
        display_title[:90],
        size_pt=40,
        bold=True,
        color=_PPTX_ACCENT,
    )
    _pptx_add_heading_rule(
        title_slide, left=Inches(1.05), top=Inches(4.0), width=Inches(2.4)
    )
    y_cursor = 4.25
    if subtitle:
        _pptx_add_textbox(
            title_slide,
            Inches(1.05),
            Inches(y_cursor),
            Inches(11.2),
            Inches(0.5),
            subtitle,
            size_pt=18,
            bold=True,
            color=_PPTX_ACCENT_WARM,
        )
        y_cursor += 0.55
    if teaser and (
        not subtitle or not teaser.casefold().startswith(subtitle.casefold()[:16])
    ):
        _pptx_add_textbox(
            title_slide,
            Inches(1.05),
            Inches(y_cursor),
            Inches(11.2),
            Inches(0.85),
            teaser,
            size_pt=15,
            color=_PPTX_MUTED,
        )
    _pptx_add_textbox(
        title_slide,
        Inches(1.05),
        Inches(7.0),
        Inches(10),
        Inches(0.35),
        prepared,
        size_pt=12,
        color=_PPTX_DIM,
    )
    # Intentionally skip title-slide photos: they clipped the headline.
    # Give them their own slide instead, right after the title (mirrors the
    # HTML deck's "photo slide first" placement).
    _pptx_add_photo_slide(prs, blank, images)

    # --- Agenda ------------------------------------------------------------
    seen_agenda: set[str] = set()
    agenda_titles: list[str] = []
    for sec, _b in outline:
        name = (sec or "").strip()
        if not name or _pptx_is_generic_title(name):
            continue
        if _pptx_titles_similar(name, display_title):
            continue
        # Collapse "· devam" continuations into one agenda entry.
        base = re.sub(r"\s*·\s*devam\s*$", "", name).strip()
        key = _pptx_title_key(base)
        if not key or key in seen_agenda:
            continue
        seen_agenda.add(key)
        agenda_titles.append(base[:70])
        if len(agenda_titles) >= 8:
            break
    if len(agenda_titles) >= 2:
        agenda = prs.slides.add_slide(blank)
        _pptx_fill_slide(agenda)
        _pptx_add_top_bar(agenda, prs)
        _pptx_add_left_rail(agenda, prs)
        _pptx_add_textbox(
            agenda,
            Inches(0.95),
            Inches(0.45),
            Inches(11.3),
            Inches(0.55),
            "Gündem",
            size_pt=28,
            bold=True,
            color=_PPTX_TEXT,
        )
        _pptx_add_heading_rule(
            agenda, left=Inches(0.95), top=Inches(1.15), width=Inches(1.6)
        )
        body_box = agenda.shapes.add_textbox(
            Inches(0.95), Inches(1.55), Inches(11.3), Inches(5.2)
        )
        _pptx_fill_numbered_frame(
            body_box.text_frame,
            agenda_titles,
            size_pt=20,
        )

    # --- Content slides ----------------------------------------------------
    for sec_title, bullets in outline:
        slide = prs.slides.add_slide(blank)
        _pptx_fill_slide(slide)
        _pptx_add_top_bar(slide, prs)
        _pptx_add_left_rail(slide, prs)
        heading = _pptx_content_heading(sec_title, display_title, bullets)
        _pptx_add_textbox(
            slide,
            Inches(0.95),
            Inches(0.4),
            Inches(11.3),
            Inches(0.7),
            heading,
            size_pt=26,
            bold=True,
            color=_PPTX_TEXT,
        )
        _pptx_add_heading_rule(
            slide, left=Inches(0.95), top=Inches(1.15), width=Inches(1.6)
        )
        body_box = slide.shapes.add_textbox(
            Inches(0.95), Inches(1.5), Inches(11.3), Inches(5.3)
        )
        _pptx_fill_bullet_frame(
            body_box.text_frame,
            bullets[:MAX_PPTX_BULLETS_PER_SLIDE],
            size_pt=18,
        )

    if len(prs.slides) == 1 and prose:
        # Last-resort single content slide from clipped teaser bullets.
        fallback = _pptx_paras_to_bullets([prose])[:MAX_PPTX_BULLETS_PER_SLIDE]
        if fallback:
            slide = prs.slides.add_slide(blank)
            _pptx_fill_slide(slide)
            _pptx_add_top_bar(slide, prs)
            _pptx_add_left_rail(slide, prs)
            _pptx_add_textbox(
                slide,
                Inches(0.95),
                Inches(0.45),
                Inches(11.3),
                Inches(0.6),
                "Özet",
                size_pt=26,
                bold=True,
                color=_PPTX_TEXT,
            )
            _pptx_add_heading_rule(
                slide, left=Inches(0.95), top=Inches(1.15), width=Inches(1.6)
            )
            body_box = slide.shapes.add_textbox(
                Inches(0.95), Inches(1.5), Inches(11.3), Inches(5.3)
            )
            _pptx_fill_bullet_frame(body_box.text_frame, fallback, size_pt=18)

    # --- Data table slide --------------------------------------------------
    headers = [str(h) for h in (header or [])]
    matrix = [list(r) for r in (rows or []) if isinstance(r, (list, tuple))]
    header_keys = {h.strip().casefold() for h in headers}
    skip_synthetic = header_keys <= {
        "bölüm", "içerik", "bolum", "icerik", "section", "content"
    }
    if headers and matrix and not skip_synthetic:
        bounded = matrix[:MAX_PPTX_TABLE_ROWS]
        slide = prs.slides.add_slide(blank)
        _pptx_fill_slide(slide)
        _pptx_add_top_bar(slide, prs)
        _pptx_add_left_rail(slide, prs)
        _pptx_add_textbox(
            slide,
            Inches(0.95),
            Inches(0.4),
            Inches(11.3),
            Inches(0.55),
            "Veriler",
            size_pt=26,
            bold=True,
            color=_PPTX_TEXT,
        )
        _pptx_add_heading_rule(
            slide, left=Inches(0.95), top=Inches(1.1), width=Inches(1.6)
        )
        cols = min(len(headers), 6)
        table_shape = slide.shapes.add_table(
            len(bounded) + 1,
            cols,
            Inches(0.95),
            Inches(1.45),
            Inches(11.3),
            Inches(0.45 * (len(bounded) + 1)),
        ).table
        for c in range(cols):
            cell = table_shape.cell(0, c)
            cell.text = headers[c]
            for p in cell.text_frame.paragraphs:
                for run in p.runs:
                    _pptx_set_run(run, size_pt=12, bold=True, color=_PPTX_BG)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _pptx_rgb(_PPTX_ACCENT)
        for r_idx, row in enumerate(bounded):
            for c in range(cols):
                cell = table_shape.cell(r_idx + 1, c)
                value = row[c] if c < len(row) else ""
                cell.text = str(value)
                for p in cell.text_frame.paragraphs:
                    for run in p.runs:
                        _pptx_set_run(run, size_pt=11, color=_PPTX_TEXT)
                cell.fill.solid()
                cell.fill.fore_color.rgb = _pptx_rgb(
                    _PPTX_CARD if r_idx % 2 == 0 else _PPTX_CARD_ALT
                )

    # --- Sources slide -----------------------------------------------------
    if source_lines:
        slide = prs.slides.add_slide(blank)
        _pptx_fill_slide(slide)
        _pptx_add_top_bar(slide, prs)
        _pptx_add_left_rail(slide, prs)
        _pptx_add_textbox(
            slide,
            Inches(0.95),
            Inches(0.4),
            Inches(11.3),
            Inches(0.55),
            "Kaynaklar",
            size_pt=28,
            bold=True,
            color=_PPTX_TEXT,
        )
        _pptx_add_heading_rule(
            slide, left=Inches(0.95), top=Inches(1.1), width=Inches(1.6)
        )
        body_box = slide.shapes.add_textbox(
            Inches(0.95), Inches(1.45), Inches(11.3), Inches(5.3)
        )
        _pptx_fill_numbered_frame(
            body_box.text_frame,
            source_lines,
            size_pt=15,
        )

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()

