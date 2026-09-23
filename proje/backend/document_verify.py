"""Checks a rendered PDF/DOCX/PPTX before it is handed to the user.

Why this module exists
----------------------
Until now a document writer returning bytes was treated as success. That
catches a crash, but none of the ways a document is actually broken while
still being a valid file: a PDF that is blank because every block was
dropped, a report whose "## Sonuç" came out as literal hash marks, a Word
file that says "This job did not produce a final synthesized answer.", or a
section the model wrote that never made it onto the page.

So the file is reopened with an independent reader -- pypdfium2 for PDF (the
engine Chrome uses, already installed with Docling), python-docx for Word,
python-pptx for slides (which also gets a layout check, see inspect_pptx) --
and what it reads back is compared with what was supposed to be written.

This never raises. A reader that is not installed or a file that will not
open becomes a problem in the report, and the caller decides what to do.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# A real report has at least a couple of sentences.
MIN_TEXT_CHARS = 40
MAX_REPORTED_HEADINGS = 10

# Markup that means a writer printed its input instead of rendering it.
_LEAKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bold markers (**) printed as text", re.compile(r"\*\*\S")),
    ("markdown heading marks (#) printed as text", re.compile(r"(?m)^\s*#{1,6}\s+\S")),
    ("markdown table syntax printed as text", re.compile(r"\|\s*:?-{3,}")),
    ("raw JSON printed as text", re.compile(r'\{\s*"(?:type|blocks|filename|title)"\s*:')),
    ("escaped newlines (\\n) printed as text", re.compile(r"\\n")),
)
_PLACEHOLDER = "did not produce a final synthesized answer"


@dataclass
class VerificationReport:
    kind: str
    ok: bool = True
    pages: int = 0
    chars: int = 0
    problems: list[str] = field(default_factory=list)
    missing_headings: list[str] = field(default_factory=list)

    def fail(self, problem: str) -> None:
        self.ok = False
        self.problems.append(problem)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": self.ok, "pages": self.pages, "chars": self.chars}
        if self.problems:
            data["problems"] = list(self.problems)
        if self.missing_headings:
            data["missing_headings"] = list(self.missing_headings)
        return data

    def summary(self) -> str:
        head = f"ok={self.ok} kind={self.kind} pages={self.pages} chars={self.chars}"
        return head + (f" problems={self.problems}" if self.problems else "")


def _normalize(text: str) -> str:
    text = (text or "").replace("­", "").replace("​", "")
    return " ".join(text.split()).casefold()


def extract_pdf_text(data: bytes) -> tuple[int, str]:
    """(page count, text) read back from a PDF. Raises if it will not open."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(data)
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            pages.append(textpage.get_text_range())
            textpage.close()
            page.close()
        return len(pages), "\n".join(pages)
    finally:
        document.close()


def extract_docx_text(data: bytes) -> str:
    """All paragraph and table text from a .docx. Raises if it will not open."""
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


# A text box may run this much past its frame before it counts as
# overflowing: the line estimate is deliberately conservative, and a
# descender brushing the edge is not a broken slide.
_OVERFLOW_RATIO = 1.15
_OVERFLOW_SLACK_IN = 0.1
_EMU_PER_INCH = 914400


def _paragraph_height(paragraph: Any, width_in: float) -> float:
    """Estimated height of one pptx paragraph, in inches."""
    import document_pptx

    text = "".join(run.text for run in paragraph.runs) or paragraph.text or ""
    sizes = [run.font.size.pt for run in paragraph.runs if run.font.size is not None]
    size = max(sizes) if sizes else 18.0
    bold = any(bool(run.font.bold) for run in paragraph.runs)
    p_pr = paragraph._p.pPr
    margin = int(p_pr.get("marL", "0")) / _EMU_PER_INCH if p_pr is not None else 0.0
    lines = document_pptx.wrap_count(text, size, max(0.1, width_in - margin), bold=bold)
    spacing = paragraph.line_spacing
    if spacing is None:
        pitch = size * 1.2
    elif isinstance(spacing, float):
        pitch = size * 1.2 * spacing
    else:
        pitch = spacing.pt
    after = paragraph.space_after.pt if paragraph.space_after is not None else 0.0
    return (lines * pitch + after) / 72


def inspect_pptx(data: bytes) -> tuple[int, str, list[str]]:
    """(slide count, slide text, layout problems) read back from a .pptx.

    Layout problems are what a reader sees and a text dump does not: a text
    box whose text runs past its frame (overlapping whatever is below it)
    and a shape placed partly off the slide. Speaker notes are not slide
    text and are left out. Raises if the file will not open.
    """
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    slide_w, slide_h = prs.slide_width, prs.slide_height
    tolerance = int(0.02 * _EMU_PER_INCH)
    parts: list[str] = []
    problems: list[str] = []
    for number, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if (
                shape.left is not None
                and shape.width is not None
                and (
                    shape.left < -tolerance
                    or shape.top < -tolerance
                    or shape.left + shape.width > slide_w + tolerance
                    or shape.top + shape.height > slide_h + tolerance
                )
            ):
                problems.append(f"slide {number}: a shape extends past the slide edge")
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.extend(cell.text for cell in row.cells)
                continue
            if not shape.has_text_frame:
                continue
            frame = shape.text_frame
            parts.append(frame.text)
            if not frame.text.strip() or shape.width is None or shape.height is None:
                continue
            if frame.word_wrap is False:
                continue
            margins = (frame.margin_left, frame.margin_right, frame.margin_top, frame.margin_bottom)
            left, right, top, bottom = (m / _EMU_PER_INCH for m in margins)
            width = shape.width / _EMU_PER_INCH - left - right
            height = shape.height / _EMU_PER_INCH - top - bottom
            needed = sum(_paragraph_height(p, width) for p in frame.paragraphs)
            if needed > height * _OVERFLOW_RATIO + _OVERFLOW_SLACK_IN:
                problems.append(
                    f"slide {number}: text overflows its box "
                    f"({needed:.2f}in of text in {height:.2f}in: {frame.text.strip()[:40]!r})"
                )
    return len(prs.slides), "\n".join(parts), problems


def verify_document(
    kind: str,
    data: bytes,
    *,
    expected_headings: Iterable[str] = (),
    expect_sources: bool = False,
    min_chars: int = MIN_TEXT_CHARS,
) -> VerificationReport:
    """Reopen `data` and check it against what the writer was given."""
    report = VerificationReport(kind=kind)
    if not data:
        report.fail("file is empty (0 bytes)")
        return report

    try:
        if kind == "pdf":
            report.pages, text = extract_pdf_text(data)
            if report.pages == 0:
                report.fail("PDF has no pages")
        elif kind == "docx":
            text = extract_docx_text(data)
        elif kind == "pptx":
            report.pages, text, layout_problems = inspect_pptx(data)
            if report.pages < 2:
                report.fail(f"deck has {report.pages} slide(s)")
            for problem in layout_problems[:MAX_REPORTED_HEADINGS]:
                report.fail(problem)
        else:
            return report
    except ImportError as exc:
        # Cannot check is not the same as broken: say so, but do not fail.
        report.problems.append(f"not verified: reader unavailable ({exc})")
        return report
    except Exception as exc:  # noqa: BLE001 - any open failure is the finding
        report.fail(f"file does not reopen ({type(exc).__name__}: {str(exc)[:160]})")
        return report

    normalized = _normalize(text)
    report.chars = len(normalized)
    if report.chars < min_chars:
        report.fail(f"almost no text ({report.chars} characters)")
    if _PLACEHOLDER in normalized:
        report.fail("contains the 'no final answer' placeholder instead of a report")

    for label, pattern in _LEAKS:
        if pattern.search(text):
            report.fail(label)

    missing = [
        heading
        for heading in dict.fromkeys(h.strip() for h in expected_headings if h and h.strip())
        if _normalize(heading) not in normalized
    ]
    if missing:
        report.missing_headings = missing[:MAX_REPORTED_HEADINGS]
        report.fail(f"{len(missing)} section heading(s) missing from the file")

    if expect_sources and "kaynak" not in normalized and "source" not in normalized:
        report.fail("sources section missing")
    return report


def headings_of(document_model: Any) -> list[str]:
    """Heading texts of a document_model.DocumentModel."""
    return [
        str(getattr(block, "text", ""))
        for block in getattr(document_model, "blocks", [])
        if getattr(block, "type", "") == "heading"
    ]


def headings_of_markdown(title: str, body: str) -> list[str]:
    """Headings the markdown writers will draw for `body`."""
    import document_model

    return headings_of(document_model.document_from_markdown(title, body))


def expects_sources(text: Optional[str]) -> bool:
    return bool(re.search(r"(?im)^\s*kaynak(?:lar|ça|ca)?\s*:?\s*$", text or ""))


__all__ = [
    "VerificationReport",
    "expects_sources",
    "extract_docx_text",
    "extract_pdf_text",
    "inspect_pptx",
    "headings_of",
    "headings_of_markdown",
    "verify_document",
]
