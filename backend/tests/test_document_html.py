"""Tests for HTML/CSS + Chromium PDF rendering (document_html)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import document_export
import document_html
import document_model
import document_verify

BLOCKS = [
    {"type": "heading", "level": 1, "text": "Giriş"},
    {"type": "paragraph", "text": "Üretim **2024** yılında arttı. Ayrıntı: https://data.tuik.gov.tr/Bulten/Index?p=Bitkisel-Uretim-2024."},
    {"type": "paragraph", "text": "Oran 5 < 7 & 8 > 3 olarak metinde kalmalı."},
    {"type": "heading", "level": 1, "text": "Bulgular"},
    {"type": "list", "ordered": True, "items": ["Verim", {"text": "Alan", "items": ["Adana", "Konya"]}, "İhracat"]},
    {"type": "heading", "level": 2, "text": "Tablo"},
    {"type": "table", "columns": ["Yıl", "Üretim"], "rows": [["2023", "7,5"], ["2024", "8,1"]]},
    {"type": "heading", "level": 1, "text": "Riskler"},
    {"type": "quote", "text": "Kuraklık başlıca risktir."},
    {"type": "heading", "level": 1, "text": "Sonuç"},
    {"type": "paragraph", "text": "Kısa bir değerlendirme paragrafı."},
    {"type": "heading", "level": 1, "text": "Kaynaklar"},
    {"type": "list", "items": ["TÜİK — https://data.tuik.gov.tr/Bulten/Index?p=Bitkisel-Uretim-Istatistikleri-2024"]},
]


def _document() -> document_model.DocumentModel:
    doc = document_model.document_from_blocks("Türkiye Mısır Üretimi", BLOCKS)
    assert doc is not None
    return doc


def test_render_html_structure() -> None:
    page = document_html.render_html(_document(), generated=datetime(2026, 9, 15))
    assert "<h1>Türkiye Mısır Üretimi</h1>" in page
    assert "Hazırlanma tarihi: 15 Eylül 2026" in page
    # Four main sections (Kaynaklar excluded) -> table of contents with anchors.
    assert 'class="toc"' in page and 'href="#bolum-4"' in page
    assert '<h2 id="bolum-1">Giriş</h2>' in page
    assert "<strong>2024</strong>" in page
    assert '<a href="https://data.tuik.gov.tr/Bulten/Index?p=Bitkisel-Uretim-2024">' in page
    assert "Oran 5 &lt; 7 &amp; 8 &gt; 3 olarak" in page
    assert "<ol><li>Verim</li><li>Alan<ol><li>Adana</li><li>Konya</li></ol></li><li>İhracat</li></ol>" in page
    assert "<thead><tr><th>Yıl</th><th>Üretim</th></tr></thead>" in page
    assert '<aside class="callout">' in page
    assert '<section class="sources"><h2>Kaynaklar</h2>' in page


def test_no_toc_for_short_reports() -> None:
    doc = document_model.document_from_blocks(
        "Kısa", [{"type": "heading", "text": "Tek"}, {"type": "paragraph", "text": "Metin."}]
    )
    assert doc is not None
    assert 'class="toc"' not in document_html.render_html(doc)


def test_engine_setting(monkeypatch) -> None:
    monkeypatch.delenv("DOCUMENT_PDF_ENGINE", raising=False)
    assert document_html.pdf_engine() == "chromium"
    monkeypatch.setenv("DOCUMENT_PDF_ENGINE", "reportlab")
    assert document_html.pdf_engine() == "reportlab"


def _chromium_pdf_or_skip(doc: document_model.DocumentModel) -> bytes:
    pytest.importorskip("playwright")
    try:
        return document_html.build_pdf(doc)
    except Exception as exc:  # noqa: BLE001 - no browser on this machine
        pytest.skip(f"Chromium unavailable: {type(exc).__name__}: {exc}")


def test_chromium_pdf_passes_verification() -> None:
    doc = _document()
    data = _chromium_pdf_or_skip(doc)
    report = document_verify.verify_document(
        "pdf", data, expected_headings=document_verify.headings_of(doc)
    )
    assert report.ok, report.problems
    pages, text = document_verify.extract_pdf_text(data)
    assert "Türkiye Mısır Üretimi" in text
    assert "Sayfa 1 /" in text
    # The TOC label is uppercased by CSS, and text extraction sees the result.
    assert "İÇİNDEKİLER" in text


def test_artifact_uses_chromium_then_reportlab_on_failure(tmp_path, monkeypatch) -> None:
    from artifacts import ArtifactTools

    monkeypatch.delenv("DOCUMENT_PDF_ENGINE", raising=False)

    def broken(doc, images=None):
        raise RuntimeError("no browser here")

    monkeypatch.setattr(document_html, "build_pdf", broken)
    tools = ArtifactTools(tmp_path)
    result = tools.create_pdf_artifact("r.pdf", title="Türkiye Mısır Üretimi", blocks=json.dumps(BLOCKS))
    assert result["verification"]["ok"] is True
    assert result["verification"]["engine"] == "reportlab"
    assert result["verification"]["path"] == "blocks"


def test_failed_chromium_file_is_redrawn_by_reportlab(tmp_path, monkeypatch) -> None:
    from artifacts import ArtifactTools

    monkeypatch.delenv("DOCUMENT_PDF_ENGINE", raising=False)
    # Chromium "succeeds" but prints a placeholder-only file.
    monkeypatch.setattr(
        document_html, "build_pdf", lambda doc, images=None: document_export.build_pdf("x", "")
    )
    tools = ArtifactTools(tmp_path)
    result = tools.create_pdf_artifact("r.pdf", title="Türkiye Mısır Üretimi", blocks=json.dumps(BLOCKS))
    verification = result["verification"]
    assert verification["path"] == "reportlab_fallback"
    assert verification["engine"] == "reportlab"
    assert verification["ok"] is True and verification["rejected"]["ok"] is False


def test_reportlab_engine_setting_skips_chromium(tmp_path, monkeypatch) -> None:
    from artifacts import ArtifactTools

    monkeypatch.setenv("DOCUMENT_PDF_ENGINE", "reportlab")

    def must_not_run(doc, images=None):
        raise AssertionError("chromium must not be used")

    monkeypatch.setattr(document_html, "build_pdf", must_not_run)
    tools = ArtifactTools(tmp_path)
    result = tools.create_pdf_artifact("r.pdf", title="T", blocks=json.dumps(BLOCKS))
    assert result["verification"]["engine"] == "reportlab"


def test_render_deck_from_blocks() -> None:
    page = document_html.render_deck(_document(), generated=datetime(2026, 9, 15))
    assert "<h1>Türkiye Mısır Üretimi</h1>" in page
    assert "Hazırlanma tarihi: 15 Eylül 2026" in page
    # One slide per main section plus a sources slide; agenda lists the sections.
    assert page.count('<section class="slide">') == 4
    assert '<section class="slide sources"><h2>Kaynaklar</h2>' in page
    assert '<div class="agenda-title">Gündem</div>' in page and "<li>Riskler</li>" in page
    assert '<div class="no">01</div><h2>Giriş</h2>' in page
    # Real structure, not flattened lines.
    assert "<thead><tr><th>Yıl</th><th>Üretim</th></tr></thead>" in page
    assert "<ol><li>Verim</li><li>Alan<ol><li>Adana</li><li>Konya</li></ol></li>" in page
    assert '<aside class="callout">Kuraklık başlıca risktir.</aside>' in page
    assert '<p class="lead">Üretim 2024 yılında arttı.' in page
