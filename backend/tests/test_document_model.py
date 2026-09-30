"""Tests for the typed document model (DOCUMENT_BLOCKS path)."""

from __future__ import annotations

import io
import json

import pytest
from docx import Document
from pypdf import PdfReader

import document_model
from document_model import (
    HeadingBlock,
    ListBlock,
    ParagraphBlock,
    QuoteBlock,
    TableBlock,
)

SAMPLE_BLOCKS = [
    {"type": "heading", "level": 1, "text": "Mısır Üretimi Raporu"},
    {"type": "heading", "level": 1, "text": "Giriş"},
    {"type": "paragraph", "text": "Türkiye'de mısır üretimi **2024** yılında arttı."},
    {"type": "paragraph", "text": "3 ana bulgu var ve bunlar aşağıda listelenmiştir."},
    {"type": "list", "ordered": True, "items": ["Verim arttı", "Alan genişledi", "İhracat büyüdü"]},
    {"type": "heading", "level": 2, "text": "Bölgeler"},
    {"type": "ul", "items": ["Adana", {"text": "Konya", "items": ["Ereğli"]}]},
    {"type": "table", "columns": ["Yıl", "Üretim (ton)"], "rows": [["2023", "7.5M"], ["2024", "8.1M"]]},
    {"type": "numbered", "items": ["Birinci adım", "İkinci adım"]},
    {"type": "callout", "text": "Önemli: veriler TÜİK kaynaklıdır."},
    {"type": "paragraph", "text": "Görsel: buraya bir mısır tarlası fotoğrafı eklenmeli"},
    {"type": "heading", "text": "Kaynaklar"},
    {"type": "paragraph", "text": "model-invented source list"},
]


def _document() -> document_model.DocumentModel:
    doc = document_model.document_from_blocks(
        "Mısır Üretimi Raporu", SAMPLE_BLOCKS, drop_sources=True
    )
    assert doc is not None
    return doc


def test_sources_kept_unless_asked_to_drop() -> None:
    doc = document_model.document_from_blocks("Mısır Üretimi Raporu", SAMPLE_BLOCKS)
    assert doc is not None
    assert "model-invented" in doc.to_markdown()


def test_normalizes_aliases_and_drops_noise() -> None:
    doc = _document()
    kinds = [type(b).__name__ for b in doc.blocks]
    # Leading title duplicate, image instruction and model sources are gone.
    assert not isinstance(doc.blocks[0], HeadingBlock) or doc.blocks[0].text == "Giriş"
    assert "model-invented" not in doc.to_markdown()
    assert "Görsel:" not in doc.to_markdown()
    assert kinds.count("ListBlock") == 3
    assert any(isinstance(b, TableBlock) for b in doc.blocks)
    assert any(isinstance(b, QuoteBlock) for b in doc.blocks)
    nested = [b for b in doc.blocks if isinstance(b, ListBlock) and not b.ordered][0]
    assert [(i.text, i.level) for i in nested.items] == [("Adana", 0), ("Konya", 0), ("Ereğli", 1)]


def test_numbered_sentence_stays_a_paragraph() -> None:
    doc = _document()
    paragraphs = [b.text for b in doc.blocks if isinstance(b, ParagraphBlock)]
    assert any(text.startswith("3 ana bulgu") for text in paragraphs)


def test_unusable_blocks_return_none() -> None:
    assert document_model.document_from_blocks("T", "not json") is None
    assert document_model.document_from_blocks("T", []) is None
    assert document_model.document_from_blocks("T", [{"type": "heading", "text": "Only"}]) is None
    assert document_model.document_from_blocks("T", json.dumps({"blocks": SAMPLE_BLOCKS})) is not None


def test_table_rows_as_objects() -> None:
    doc = document_model.document_from_blocks(
        "T", [{"type": "table", "rows": [{"Ad": "A", "Puan": 3}, {"Ad": "B", "Puan": None}]}]
    )
    assert doc is not None
    table = doc.blocks[0]
    assert isinstance(table, TableBlock)
    assert table.columns == ["Ad", "Puan"]
    assert table.rows == [["A", "3"], ["B", ""]]


def test_docx_render_structure() -> None:
    raw = document_model.build_docx(_document())
    doc = Document(io.BytesIO(raw))
    styles = [(p.style.name, p.text) for p in doc.paragraphs if p.text.strip()]
    assert styles[0] == ("Title", "Mısır Üretimi Raporu")
    assert ("Heading 1", "Giriş") in styles
    assert ("Heading 2", "Bölgeler") in styles
    assert ("List Bullet 2", "Ereğli") in styles
    assert any(style == "Intense Quote" for style, _ in styles)
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert "2024" in bold
    assert len(doc.tables) == 1

    # Each numbered list restarts: the two ordered lists use different numIds.
    num_ids = {
        p._p.pPr.numPr.numId.val
        for p in doc.paragraphs
        if p.style.name == "List Number" and p._p.pPr is not None and p._p.pPr.numPr is not None
    }
    assert len(num_ids) == 2


def test_pdf_render_contains_text() -> None:
    raw = document_model.build_pdf(_document())
    reader = PdfReader(io.BytesIO(raw))
    text = "\n".join(page.extract_text() for page in reader.pages)
    for needle in ("Mısır Üretimi Raporu", "Giriş", "Alan genişledi", "8.1M", "TÜİK", "Sayfa 1 /"):
        assert needle in text, needle
    assert "**" not in text


def test_payload_roundtrip_with_injected_sources() -> None:
    payload = {"title": "Mısır Üretimi Raporu", "blocks": SAMPLE_BLOCKS}
    assert document_model.attach_blocks_to_payload(payload) is not None
    assert payload["content"].startswith("## Giriş")
    # What the Document worker does next: sanitize, then append sources.
    import document_export

    payload["content"] = document_export.sanitize_report_prose(payload["content"])
    payload["content"] += "\n\nKaynaklar:\n1. TÜİK — https://tuik.gov.tr"
    doc = document_model.resolve_payload_document(payload)
    assert doc is not None
    assert isinstance(doc.blocks[-2], HeadingBlock) and doc.blocks[-2].text == "Kaynaklar"
    assert "tuik.gov.tr" in doc.to_markdown()


def test_payload_replaced_content_falls_back() -> None:
    payload = {"title": "T", "blocks": SAMPLE_BLOCKS}
    document_model.attach_blocks_to_payload(payload)
    payload["content"] = "## Completely different research brief\n\nOther text."
    assert document_model.resolve_payload_document(payload) is None


def _payload_after_worker(content_override: str = "") -> dict:
    """A payload in the state the Document worker hands to _write_artifact."""
    import document_export

    payload = {"filename": "rapor.docx", "title": "Mısır Üretimi Raporu", "blocks": SAMPLE_BLOCKS}
    document_model.attach_blocks_to_payload(payload)
    payload["content"] = document_export.sanitize_report_prose(payload["content"])
    payload["content"] += "\n\nKaynaklar:\n1. TÜİK — https://tuik.gov.tr"
    if content_override:
        payload["content"] = content_override
    payload["body"] = payload["content"]
    return payload


def test_write_artifact_renders_blocks_for_docx_and_pdf(tmp_path) -> None:
    pytest.importorskip("langgraph")
    from artifacts import ArtifactTools
    from execution.langgraph_runtime import workforce

    tools = ArtifactTools(tmp_path)
    docx_result = workforce._write_artifact(
        tools, kind="docx", filename="rapor.docx", payload=_payload_after_worker()
    )
    doc = Document(str(tmp_path / docx_result["path"]))
    styles = [(p.style.name, p.text) for p in doc.paragraphs if p.text.strip()]
    assert ("Heading 1", "Giriş") in styles
    assert ("Heading 1", "Kaynaklar") in styles
    assert any("tuik.gov.tr" in text for _, text in styles)
    assert "2024" in [r.text for p in doc.paragraphs for r in p.runs if r.bold]

    pdf_result = workforce._write_artifact(
        tools, kind="pdf", filename="rapor.pdf", payload=_payload_after_worker()
    )
    text = "\n".join(
        page.extract_text() for page in PdfReader(str(tmp_path / pdf_result["path"])).pages
    )
    assert "Alan genişledi" in text and "tuik.gov.tr" in text


def test_write_artifact_falls_back_when_content_replaced(tmp_path) -> None:
    pytest.importorskip("langgraph")
    from artifacts import ArtifactTools
    from execution.langgraph_runtime import workforce

    tools = ArtifactTools(tmp_path)
    payload = _payload_after_worker("## Araştırma Özeti\n\nNotlardan gelen farklı metin.")
    result = workforce._write_artifact(tools, kind="docx", filename="rapor.docx", payload=payload)
    doc = Document(str(tmp_path / result["path"]))
    body = " ".join(p.text for p in doc.paragraphs)
    assert "Notlardan gelen farklı metin" in body
    assert "Alan genişledi" not in body


def test_tool_accepts_blocks_directly(tmp_path) -> None:
    from artifacts import ArtifactTools

    tools = ArtifactTools(tmp_path)
    result = tools.create_pdf_artifact(
        "direct.pdf", title="Direkt", blocks=json.dumps(SAMPLE_BLOCKS)
    )
    assert result["created"] and result["kind"] == "pdf"
    again = tools.create_pdf_artifact("direct.pdf", title="Direkt", blocks=json.dumps(SAMPLE_BLOCKS))
    assert again.get("reused_existing") is True


def test_invalid_blocks_removed_from_payload() -> None:
    payload = {"title": "T", "blocks": "garbage", "content": "x"}
    assert document_model.attach_blocks_to_payload(payload) is None
    assert "blocks" not in payload
    assert payload["content"] == "x"
