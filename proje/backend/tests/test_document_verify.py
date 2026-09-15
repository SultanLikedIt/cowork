"""Tests for post-render PDF/DOCX verification (document_verify + artifacts)."""

from __future__ import annotations

import json

import document_export
import document_model
import document_verify
from document_model import DocumentModel, HeadingBlock, ParagraphBlock

GOOD_BLOCKS = [
    {"type": "heading", "level": 1, "text": "Üretim"},
    {"type": "paragraph", "text": "Dane mısır üretimi **2024** yılında 8,1 milyon tona ulaştı ve rekor kırdı."},
    {"type": "heading", "level": 1, "text": "Bölgesel Dağılım"},
    {"type": "list", "items": ["Adana", "Konya"]},
]


def _good_document() -> DocumentModel:
    doc = document_model.document_from_blocks("Mısır Raporu", GOOD_BLOCKS)
    assert doc is not None
    return doc


def test_good_pdf_and_docx_pass() -> None:
    doc = _good_document()
    headings = document_verify.headings_of(doc)
    assert headings == ["Üretim", "Bölgesel Dağılım"]

    pdf = document_verify.verify_document("pdf", document_model.build_pdf(doc), expected_headings=headings)
    assert pdf.ok, pdf.problems
    assert pdf.pages == 1 and pdf.chars > 40

    docx = document_verify.verify_document("docx", document_model.build_docx(doc), expected_headings=headings)
    assert docx.ok, docx.problems


def test_missing_heading_is_reported() -> None:
    report = document_verify.verify_document(
        "pdf",
        document_model.build_pdf(_good_document()),
        expected_headings=["Üretim", "Sonuç ve Öneriler"],
    )
    assert not report.ok
    assert report.missing_headings == ["Sonuç ve Öneriler"]


def test_leaked_markdown_is_reported() -> None:
    doc = DocumentModel(
        title="T",
        blocks=[
            HeadingBlock(text="Giriş"),
            ParagraphBlock(text="## Sonuç bölümü burada yanlışlıkla işaretleriyle yazıldı."),
        ],
    )
    report = document_verify.verify_document("pdf", document_model.build_pdf(doc))
    assert not report.ok
    assert any("heading marks" in problem for problem in report.problems)


def test_placeholder_and_corrupt_files_fail() -> None:
    placeholder = document_verify.verify_document("pdf", document_export.build_pdf("Boş", ""))
    assert not placeholder.ok
    assert any("placeholder" in problem for problem in placeholder.problems)

    corrupt = document_verify.verify_document("pdf", b"%PDF-1.4 this is not really a pdf")
    assert not corrupt.ok and "does not reopen" in corrupt.problems[0]

    empty = document_verify.verify_document("docx", b"")
    assert not empty.ok


def test_sources_expectation() -> None:
    body = "## Giriş\n\nBir paragraf metni burada yer alıyor ve yeterince uzun.\n\nKaynaklar:\n1. TÜİK — https://tuik.gov.tr"
    assert document_verify.expects_sources(body)
    report = document_verify.verify_document(
        "docx",
        document_export.build_docx("Rapor", body),
        expected_headings=document_verify.headings_of_markdown("Rapor", body),
        expect_sources=True,
    )
    assert report.ok, report.problems


def test_unpaired_bold_marker_is_removed() -> None:
    doc = document_model.document_from_blocks(
        "T", [{"type": "paragraph", "text": "**Not: bu cümlede kapanmayan bir işaret var."}]
    )
    assert doc is not None
    assert "**" not in doc.blocks[0].text


def test_tool_result_carries_verification(tmp_path) -> None:
    from artifacts import ArtifactTools

    tools = ArtifactTools(tmp_path)
    blocks_result = tools.create_pdf_artifact("a.pdf", title="Mısır Raporu", blocks=json.dumps(GOOD_BLOCKS))
    assert blocks_result["verification"]["ok"] is True
    assert blocks_result["verification"]["path"] == "blocks"

    markdown_result = tools.create_docx_artifact(
        "b.docx", title="Rapor", summary="## Giriş\n\nYeterince uzun bir paragraf metni burada yer alıyor."
    )
    assert markdown_result["verification"]["ok"] is True
    assert markdown_result["verification"]["path"] == "markdown"


def test_failed_blocks_file_falls_back_to_markdown_writer(tmp_path, monkeypatch) -> None:
    from artifacts import ArtifactTools

    # Simulate a blocks renderer that silently produces an empty report.
    monkeypatch.setattr(
        document_model, "build_pdf", lambda doc, images=None: document_export.build_pdf("x", "")
    )
    tools = ArtifactTools(tmp_path)
    result = tools.create_pdf_artifact("c.pdf", title="Mısır Raporu", blocks=json.dumps(GOOD_BLOCKS))
    verification = result["verification"]
    assert verification["path"] == "markdown_fallback"
    assert verification["ok"] is True
    assert verification["rejected"]["ok"] is False

    pages, text = document_verify.extract_pdf_text((tmp_path / result["path"]).read_bytes())
    assert "Bölgesel Dağılım" in text and "Adana" in text
