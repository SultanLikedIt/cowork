"""Tests for the DOCX/PPTX artifact tools (artifacts.py -> document_export).

Every assertion opens the real rendered file and checks its structure and
content -- never trusts a tool's "created: true" response alone. Runs under
pytest, or directly:

    python test_office_artifacts.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from docx import Document
from pptx import Presentation

import artifacts
import document_export
from artifacts import ArtifactTools


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    assert condition, f"{name}: {detail}"


def _workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="office_artifact_ws_")).resolve()
    (root / "sources").mkdir(parents=True)
    return root


SAMPLE_ROWS = [
    {"Region": "EMEA", "Revenue": 125000, "Growth": "8%"},
    {"Region": "AMER", "Revenue": 210000, "Growth": "15%"},
    {"Region": "APAC", "Revenue": 98000, "Growth": "11%"},
]


# ---------------------------------------------------------------------------
# JSON payload -> DOCX / PPTX
# ---------------------------------------------------------------------------


def test_docx_artifact_from_inline_data_is_real() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_docx_artifact(
        "quarterly-report.docx",
        title="Q3 Regional Revenue Report",
        summary="Quarterly revenue grew across all regions.",
        data=json.dumps(SAMPLE_ROWS),
    )
    check("docx: created=True", result.get("created") is True, result)
    path = root / result["path"]
    check("docx: the file exists on disk", path.is_file(), path)
    check("docx: it is inside artifacts/", result["path"].startswith("artifacts/"), result["path"])
    check("docx: reported size matches the real file", result["size_bytes"] == path.stat().st_size)

    doc = Document(path)
    full_text = "\n".join(p.text for p in doc.paragraphs)
    check("docx: title rendered", "Q3 Regional Revenue Report" in full_text, full_text[:200])
    check("docx: summary rendered", "Quarterly revenue grew" in full_text)
    check("docx: exactly one table", len(doc.tables) == 1, len(doc.tables))
    table = doc.tables[0]
    check("docx: header + one row per data element", len(table.rows) == len(SAMPLE_ROWS) + 1, len(table.rows))
    header_text = [cell.text for cell in table.rows[0].cells]
    check("docx: header shows real column names", header_text[:3] == ["Region", "Revenue", "Growth"], header_text)
    rendered_regions = [table.rows[i + 1].cells[0].text for i in range(len(SAMPLE_ROWS))]
    check(
        "docx: row order/content matches source data",
        rendered_regions == [row["Region"] for row in SAMPLE_ROWS],
        rendered_regions,
    )


def test_pptx_artifact_from_inline_data_is_valid_and_editable() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_pptx_artifact(
        "quarterly-review.pptx",
        title="Q3 Regional Revenue Review",
        summary="Growth was strongest in AMER.",
        data=json.dumps(SAMPLE_ROWS),
    )
    check("pptx: created=True", result.get("created") is True, result)
    path = root / result["path"]
    check("pptx: the file exists on disk", path.is_file())

    prs = Presentation(path)
    check("pptx: at least one slide", len(prs.slides) >= 1)
    all_text = []
    table_rows = None
    header_cells = None
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                all_text.append(shape.text_frame.text)
            if shape.has_table:
                table_rows = len(shape.table.rows)
                header_cells = [shape.table.cell(0, c).text for c in range(3)]
    joined = "\n".join(all_text)
    check("pptx: title rendered", "Q3 Regional Revenue Review" in joined, joined[:200])
    check("pptx: summary rendered", "Growth was strongest" in joined)
    check("pptx: header + one row per data element", table_rows == len(SAMPLE_ROWS) + 1, table_rows)
    check("pptx: header shows real column names", header_cells == ["Region", "Revenue", "Growth"], header_cells)


# ---------------------------------------------------------------------------
# Reference-based (from_path) input and table bounds
# ---------------------------------------------------------------------------


def test_docx_and_pptx_accept_reference_based_json() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    data_result = tools.create_json_artifact("source-data.json", json.dumps(SAMPLE_ROWS))

    docx_result = tools.create_docx_artifact("from-ref.docx", title="From Reference", from_path=data_result["path"])
    pptx_result = tools.create_pptx_artifact("from-ref.pptx", title="From Reference", from_path=data_result["path"])
    check("docx: from_path succeeds", (root / docx_result["path"]).is_file())
    check("pptx: from_path succeeds", (root / pptx_result["path"]).is_file())

    doc = Document(root / docx_result["path"])
    check("docx: from_path renders the referenced rows", len(doc.tables[0].rows) == len(SAMPLE_ROWS) + 1)


def test_large_dataset_is_bounded_and_reported() -> None:
    """A large dataset works via from_path, is bounded to a report table,
    and the truncation is reported rather than silent."""
    root = _workspace()
    tools = ArtifactTools(root)
    big_rows = [
        {"Region": f"Region-{i}", "Revenue": i * 1000, "Growth": f"{i % 20}%"}
        for i in range(artifacts.MAX_DOCX_TABLE_ROWS + 50)
    ]
    data_result = tools.create_json_artifact("big-data.json", json.dumps(big_rows))
    result = tools.create_docx_artifact("big-report.docx", title="Large Dataset Report", from_path=data_result["path"])
    check("docx: large reference dataset renders", (root / result["path"]).is_file())
    doc = Document(root / result["path"])
    check(
        "docx: table rows are bounded",
        len(doc.tables[0].rows) - 1 == artifacts.MAX_DOCX_TABLE_ROWS,
        len(doc.tables[0].rows),
    )
    check(
        "docx: row truncation is reported in table_notes",
        any("rows are shown" in note for note in result.get("table_notes", [])),
        result.get("table_notes"),
    )


def test_too_many_columns_are_reported() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    wide = [{f"C{i}": i for i in range(artifacts.MAX_OFFICE_TABLE_COLUMNS + 3)}]
    result = tools.create_docx_artifact("wide.docx", title="Wide", data=json.dumps(wide))
    doc = Document(root / result["path"])
    check(
        "docx: table columns are bounded",
        len(doc.tables[0].columns) == artifacts.MAX_OFFICE_TABLE_COLUMNS,
        len(doc.tables[0].columns),
    )
    check(
        "docx: column truncation is reported in table_notes",
        any("columns are shown" in note for note in result.get("table_notes", [])),
        result.get("table_notes"),
    )


def test_pptx_table_rows_are_bounded_tighter_than_docx() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    rows = [{"Region": f"R{i}", "Revenue": i} for i in range(50)]
    result = tools.create_pptx_artifact("many-rows.pptx", title="Many Rows", data=json.dumps(rows))
    prs = Presentation(root / result["path"])
    table_rows = next(
        len(shape.table.rows) for slide in prs.slides for shape in slide.shapes if shape.has_table
    )
    check(
        "pptx: table rows bounded to MAX_PPTX_TABLE_ROWS",
        table_rows - 1 == min(len(rows), document_export.MAX_PPTX_TABLE_ROWS),
        table_rows,
    )


# ---------------------------------------------------------------------------
# Narrative structure
# ---------------------------------------------------------------------------


def test_docx_wall_of_text_becomes_multiple_paragraphs() -> None:
    """Narrative reports must not land as a single Normal paragraph."""
    root = _workspace()
    tools = ArtifactTools(root)
    wall = (
        "Bu rapor, 2026 yılı itibarıyla Türkiye’deki KOBİ’lerin AI agent "
        "kullanımını ortaya koymaktadır. Raporda sekiz temel senaryo "
        "detaylandırılmıştır: (1) Fatura İşleme Agent’ı – muhasebe için "
        "ayda 500+ faturayı işler. (2) WhatsApp + GPT-4o Botu – kayıp "
        "lead’i azaltır. (3) Satış Teklifi Agent’ı – süreyi kısaltır. "
        "Raporun sonunda 5 maddelik checklist yer alır: 1) En acil süreci "
        "belirle 2) Pilot ile başla 3) KVKK’yı sağla 4) Çift sağlayıcı kur "
        "5) KPI tanımla."
    )
    result = tools.create_docx_artifact(
        "kobi-report.docx",
        title="KOBİ AI Agent Raporu",
        summary=wall,
        data=json.dumps([{"Senaryo": "Fatura", "Etki": "6+ saat"}, {"Senaryo": "WhatsApp", "Etki": "%60-80"}]),
    )
    doc = Document(root / result["path"])
    body_paras = [p for p in doc.paragraphs if (p.text or "").strip()]
    check(
        "docx: wall-of-text yields multiple body paragraphs",
        len(body_paras) >= 4,
        [p.text[:40] for p in body_paras],
    )
    check("docx: still has a data table", len(doc.tables) == 1, len(doc.tables))


# ---------------------------------------------------------------------------
# Regression: XLSX/CSV/JSON stay on their native path
# ---------------------------------------------------------------------------


def test_xlsx_uses_native_writer_with_real_numeric_cells() -> None:
    from openpyxl import load_workbook

    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_xlsx_artifact("revenue.xlsx", data=json.dumps(SAMPLE_ROWS))
    path = root / result["path"]
    check("xlsx: file exists", path.is_file())
    revenue_cell = load_workbook(path).active.cell(row=2, column=2)
    check("xlsx: revenue cell is a TRUE numeric cell", revenue_cell.data_type == "n", revenue_cell.data_type)
    check("xlsx: revenue value round-trips as a number", isinstance(revenue_cell.value, (int, float)))


def test_json_csv_and_bundle_paths_are_unaffected() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    json_result = tools.create_json_artifact("d.json", json.dumps(SAMPLE_ROWS))
    csv_result = tools.create_csv_artifact("d.csv", json.dumps(SAMPLE_ROWS))
    bundle_result = tools.create_web_bundle("app", html="<html><body>hi</body></html>")

    check("json: still created", (root / json_result["path"]).is_file())
    check("csv: still created", (root / csv_result["path"]).is_file())
    check("bundle: still created", bundle_result.get("created") is True)

    tool_names = [fn.__name__ for fn in tools.as_tools()]
    check(
        "as_tools(): docx/pptx tools are registered",
        {"create_docx_artifact", "create_pptx_artifact"} <= set(tool_names),
        tool_names,
    )
    check("as_tools(): convert_table_to_json remains first", tool_names[0] == "convert_table_to_json", tool_names)


def test_scan_artifacts_reports_docx_and_pptx_producers() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_docx_artifact("scan-me.docx", title="X", data=json.dumps(SAMPLE_ROWS))
    tools.create_pptx_artifact("scan-me.pptx", title="X", data=json.dumps(SAMPLE_ROWS))
    found = {entry["filename"]: entry for entry in artifacts.scan_artifacts(root)}
    check("scan: docx artifact listed", "scan-me.docx" in found, list(found))
    check("scan: pptx artifact listed", "scan-me.pptx" in found, list(found))
    check("scan: docx producer attributed", found.get("scan-me.docx", {}).get("producer") == "create_docx_artifact")
    check("scan: pptx producer attributed", found.get("scan-me.pptx", {}).get("producer") == "create_pptx_artifact")


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"\nAll {len(tests)} office artifact checks passed")


if __name__ == "__main__":
    main()
