"""Tests for the styled Word renderer (document_docx) and its fallbacks."""

from __future__ import annotations

import io
import json
from datetime import datetime

from docx import Document

import document_docx
import document_export
import document_model
import document_verify

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

BLOCKS = [
    {"type": "heading", "level": 1, "text": "Giriş"},
    {"type": "paragraph", "text": "Üretim **2024** yılında arttı. Kaynak: https://data.tuik.gov.tr/Bulten/Index?p=Bitkisel-Uretim-2024"},
    {"type": "heading", "level": 1, "text": "Bulgular"},
    {"type": "list", "ordered": True, "items": ["Verim", {"text": "Alan", "items": ["Adana"]}]},
    {"type": "heading", "level": 1, "text": "Tablo"},
    {"type": "table", "columns": ["Yıl", "Üretim"], "rows": [["2023", "7,5"], ["2024", "8,1"]]},
    {"type": "heading", "level": 1, "text": "Riskler"},
    {"type": "quote", "text": "Kuraklık başlıca risktir."},
    {"type": "heading", "level": 1, "text": "Kaynaklar"},
    {"type": "list", "items": ["TÜİK — https://data.tuik.gov.tr/Bulten/Index?p=Bitkisel-Uretim-Istatistikleri-2024"]},
]


def _document() -> document_model.DocumentModel:
    doc = document_model.document_from_blocks("Türkiye Mısır Üretimi", BLOCKS)
    assert doc is not None
    return doc


def _styled() -> Document:
    return Document(io.BytesIO(document_docx.build_docx(_document(), generated=datetime(2026, 9, 15))))


def test_cover_and_table_of_contents() -> None:
    doc = _styled()
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert texts[:3] == ["RAPOR", "Türkiye Mısır Üretimi", "Hazırlanma tarihi: 15 Eylül 2026"]
    assert doc.paragraphs[1].style.name == "Title"
    assert "İÇİNDEKİLER" in texts
    body_xml = doc.element.body.xml
    # TOC entries link to bookmarks placed on the section headings.
    assert 'w:anchor="bolum_1"' in body_xml and 'w:anchor="bolum_4"' in body_xml
    assert 'w:name="bolum_1"' in body_xml
    assert doc.core_properties.title == "Türkiye Mısır Üretimi"


def test_headings_are_restyled_not_theme_fonts() -> None:
    doc = _styled()
    heading = doc.styles["Heading 1"]
    assert str(heading.font.color.rgb) == document_docx.ACCENT
    assert heading.font.bold is True
    rfonts = heading.element.rPr.find(f"{W}rFonts")
    assert rfonts is not None and rfonts.get(f"{W}asciiTheme") is None
    assert rfonts.get(f"{W}ascii") == document_docx.FONT
    assert ("Heading 1", "Giriş") in [(p.style.name, p.text) for p in doc.paragraphs]


def test_table_quote_links_and_bold() -> None:
    doc = _styled()
    assert len(doc.tables) == 1
    header_cell = doc.tables[0].rows[0].cells[0]
    shading = header_cell._tc.tcPr.find(f"{W}shd")
    assert shading is not None and shading.get(f"{W}fill") == document_docx.ACCENT
    assert doc.tables[0].rows[0]._tr.trPr.find(f"{W}tblHeader") is not None

    quote = [p for p in doc.paragraphs if p.style.name == "Quote"]
    assert quote and quote[0].text == "Kuraklık başlıca risktir."
    assert doc.styles["Quote"].element.pPr.find(f"{W}shd").get(f"{W}fill") == document_docx.SOFT

    targets = [rel.target_ref for rel in doc.part.rels.values() if rel.reltype.endswith("/hyperlink")]
    assert any("data.tuik.gov.tr" in target for target in targets)
    assert "2024" in [r.text for p in doc.paragraphs for r in p.runs if r.bold]


def test_footer_has_title_and_page_fields() -> None:
    doc = _styled()
    footer_xml = doc.sections[0].footer._element.xml
    assert "Türkiye Mısır Üretimi" in footer_xml
    assert " PAGE " in footer_xml and " NUMPAGES " in footer_xml


def test_styled_docx_passes_verification() -> None:
    doc = _document()
    report = document_verify.verify_document(
        "docx", document_docx.build_docx(doc), expected_headings=document_verify.headings_of(doc)
    )
    assert report.ok, report.problems


def test_artifact_docx_is_styled_by_default(tmp_path) -> None:
    from artifacts import ArtifactTools

    result = ArtifactTools(tmp_path).create_docx_artifact(
        "r.docx", title="Türkiye Mısır Üretimi", blocks=json.dumps(BLOCKS)
    )
    assert result["verification"]["ok"] is True
    assert result["verification"]["path"] == "blocks"
    assert result["verification"]["style"] == "styled"


def test_artifact_docx_plain_when_styled_renderer_crashes(tmp_path, monkeypatch) -> None:
    from artifacts import ArtifactTools

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(document_docx, "build_docx", broken)
    result = ArtifactTools(tmp_path).create_docx_artifact(
        "r.docx", title="Türkiye Mısır Üretimi", blocks=json.dumps(BLOCKS)
    )
    assert result["verification"]["ok"] is True
    assert result["verification"]["style"] == "plain"


def test_failed_styled_docx_is_redrawn_plain(tmp_path, monkeypatch) -> None:
    from artifacts import ArtifactTools

    monkeypatch.setattr(
        document_docx, "build_docx", lambda *a, **k: document_export.build_docx("x", "")
    )
    result = ArtifactTools(tmp_path).create_docx_artifact(
        "r.docx", title="Türkiye Mısır Üretimi", blocks=json.dumps(BLOCKS)
    )
    verification = result["verification"]
    assert verification["path"] == "plain_docx_fallback"
    assert verification["style"] == "plain"
    assert verification["ok"] is True and verification["rejected"]["ok"] is False
