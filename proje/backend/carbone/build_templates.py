"""Builds the two production Carbone templates checked into this repo.

Run this to regenerate templates/report.docx and templates/report.pptx if
they ever need to change. It is NOT run at request time -- the committed
.docx/.pptx files under templates/ are what production actually renders
against.

Templates are authored the same way a business user would in Word/
PowerPoint: plain Carbone marker text (``{d.field}``) typed directly into
headings, paragraphs, and table cells -- via openpyxl/python-docx/python-pptx
here only because this environment has no desktop Office apps, not because
templates need code to exist.

IMPORTANT -- Carbone's array-repetition convention (see carbone_render.py's
module docstring and docs/carbone-docx-pptx.md): a single templated table
row is *silently* NOT repeated per data row. Carbone detects the repeat
boundary by finding TWO marker occurrences for the same array (``[i]`` and
``[i+1]``) and replicating the XML delta between them once per element.
Every table below therefore has two templated data rows, not one. Any new
Carbone template MUST follow this same two-row convention or row repetition
will silently produce only a single, wrong row.

The table shape is intentionally generic and bounded (see
carbone_render.MAX_TABLE_COLUMNS): a fixed number of named ``c0``..``c7``
positions per row rather than the caller's actual business field names, so
one template serves any create_docx_artifact/create_pptx_artifact call
regardless of column names -- unused trailing columns render as empty
cells.

IMPORTANT -- fixed numeric array indexing (``{d.columns[0]}``,
``{d.rows[i].cells[0]}``) was tried first and DOES NOT WORK: Carbone's
``[i]``/``[i+1]`` bracket syntax is reserved for loop-repetition detection,
not plain indexing, and a literal index silently renders empty (or, when
mixed with a real repeated array, throws `Cannot read properties of
undefined (reading 'position')`). Named object keys (``c0``, ``c1``, ...)
are the working alternative and are what these templates use.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.shared import Pt
from pptx import Presentation
from pptx.util import Inches

TEMPLATES = Path(__file__).parent / "templates"
TEMPLATES.mkdir(exist_ok=True)

# Must match carbone_render.MAX_TABLE_COLUMNS.
MAX_TABLE_COLUMNS = 8


def _column_key(col: int) -> str:
    return f"c{col}"


def _cell_marker(row_expr: str, col: int) -> str:
    return f"{{d.rows[{row_expr}].cells.{_column_key(col)}}}"


def build_docx() -> None:
    doc = Document()
    doc.add_heading("{d.title}", level=1)

    meta = doc.add_paragraph()
    meta.add_run("Generated {d.generated_at}").italic = True

    doc.add_paragraph("{d.summary}")

    doc.add_heading("Data", level=2)
    table = doc.add_table(rows=3, cols=MAX_TABLE_COLUMNS)
    table.style = "Light Grid Accent 1"

    header = table.rows[0].cells
    for col in range(MAX_TABLE_COLUMNS):
        header[col].text = f"{{d.columns.{_column_key(col)}}}"
        header[col].paragraphs[0].runs[0].font.bold = True

    # Two templated rows (i, i+1) -- see module docstring.
    for row_idx, expr in enumerate(("i", "i+1"), start=1):
        cells = table.rows[row_idx].cells
        for col in range(MAX_TABLE_COLUMNS):
            cells[col].text = _cell_marker(expr, col)

    closing = doc.add_paragraph()
    run = closing.add_run("{d.footnote}")
    run.font.size = Pt(10)
    run.italic = True

    doc.save(TEMPLATES / "report.docx")


def build_pptx() -> None:
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(7.5)

    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = "{d.title}"
    title_slide.placeholders[1].text = "Generated {d.generated_at}"

    summary_slide = prs.slides.add_slide(prs.slide_layouts[1])
    summary_slide.shapes.title.text = "Summary"
    summary_slide.placeholders[1].text_frame.text = "{d.summary}"

    table_slide = prs.slides.add_slide(prs.slide_layouts[5])
    table_slide.shapes.title.text = "Data"
    rows = 3
    table_shape = table_slide.shapes.add_table(
        rows, MAX_TABLE_COLUMNS, Inches(0.4), Inches(1.4), Inches(9.2), Inches(1.6)
    )
    table = table_shape.table
    for col in range(MAX_TABLE_COLUMNS):
        table.cell(0, col).text = f"{{d.columns.{_column_key(col)}}}"
    for row_idx, expr in enumerate(("i", "i+1"), start=1):
        for col in range(MAX_TABLE_COLUMNS):
            table.cell(row_idx, col).text = _cell_marker(expr, col)

    prs.save(TEMPLATES / "report.pptx")


if __name__ == "__main__":
    build_docx()
    build_pptx()
    print("Templates written to", TEMPLATES)
