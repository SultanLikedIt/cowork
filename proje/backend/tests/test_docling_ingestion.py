"""Deterministic Docling ingestion and workspace-reference regressions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from reportlab.pdfgen import canvas

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="docling-api-test-"))

from document_ingestion import DocumentParseError, parse_document
import main as backend_main
from workspace import (
    MAX_TOOL_READ_CHARS,
    ReadOnlyWorkspaceTools,
    build_job_workspace,
)


class DoclingIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="docling-ingestion-"))
        cls.sources = cls.root / "fixtures"
        cls.sources.mkdir()

        cls.pdf = cls.sources / "sample.pdf"
        pdf = canvas.Canvas(str(cls.pdf))
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 760, "Quarterly Report")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 730, "PDF content parsed through Docling.")
        pdf.save()

        cls.docx = cls.sources / "sample.docx"
        word = Document()
        word.add_heading("Executive Summary", level=1)
        word.add_paragraph("DOCX content parsed through Docling.")
        table = word.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Metric"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "Revenue"
        table.cell(1, 1).text = "42"
        word.save(cls.docx)

        cls.pptx = cls.sources / "sample.pptx"
        slides = Presentation()
        slide = slides.slides.add_slide(slides.slide_layouts[1])
        slide.shapes.title.text = "First Slide"
        slide.placeholders[1].text = "PPTX content parsed through Docling."
        slide = slides.slides.add_slide(slides.slide_layouts[1])
        slide.shapes.title.text = "Second Slide"
        slide.placeholders[1].text = "Slide structure is preserved."
        slides.save(cls.pptx)

        cls.xlsx = cls.sources / "sample.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Metrics"
        sheet.append(["Metric", "Value"])
        sheet.append(["Revenue", 42])
        sheet.append(["Cost", 7])
        workbook.save(cls.xlsx)

        cls.html = cls.sources / "sample.html"
        cls.html.write_text(
            "<html><head><title>Web Report</title></head><body>"
            "<h1>HTML Section</h1><p>HTML content parsed through Docling.</p>"
            "</body></html>",
            encoding="utf-8",
        )

        cls.parsed = {}
        for source in (cls.pdf, cls.docx, cls.pptx, cls.xlsx, cls.html):
            cls.parsed[source.suffix] = parse_document(
                source,
                cls.root / f"parsed-{source.suffix.lstrip('.')}",
                display_name=source.name,
            )

    def test_primary_formats_parse_to_canonical_docling_documents(self) -> None:
        expected_text = {
            ".pdf": "PDF content",
            ".docx": "DOCX content",
            ".pptx": "PPTX content",
            ".xlsx": "Revenue",
            ".html": "HTML content",
        }
        for extension, marker in expected_text.items():
            with self.subTest(extension=extension):
                parsed = self.parsed[extension]
                canonical = json.loads(
                    (parsed.output_dir / "canonical.json").read_text(encoding="utf-8")
                )
                self.assertEqual(canonical["engine"], "docling")
                self.assertEqual(canonical["source"]["extension"], extension)
                self.assertIn(marker, parsed.text)
                self.assertTrue((parsed.output_dir / "chunks.jsonl").is_file())

    def test_headings_pages_slides_and_metadata_are_preserved(self) -> None:
        word_manifest = self.parsed[".docx"].manifest
        self.assertTrue(
            any(
                heading["text"] == "Executive Summary"
                for heading in word_manifest["headings"]
            )
        )
        slides = self.parsed[".pptx"].manifest
        self.assertGreaterEqual(slides["page_count"], 2)
        self.assertEqual(slides["source"]["filename"], "sample.pptx")

    def test_table_extraction_is_a_structured_csv_reference(self) -> None:
        manifest = self.parsed[".xlsx"].manifest
        self.assertGreaterEqual(manifest["table_count"], 1)
        table = manifest["tables"][0]
        self.assertGreaterEqual(table["rows"], 2)
        self.assertEqual(table["columns"][:2], ["Metric", "Value"])
        table_path = self.parsed[".xlsx"].output_dir / table["path"]
        self.assertIn("Revenue,42", table_path.read_text(encoding="utf-8"))

    def test_workspace_uses_references_and_keeps_model_reads_bounded(self) -> None:
        source = self.html
        large_markdown = "# Large Section\n\n" + ("bounded document text " * 5_000)
        representation = self.parsed[".html"].output_dir
        (representation / "content.md").write_text(large_markdown, encoding="utf-8")
        record = {
            "file_id": str(uuid.uuid4()),
            "filename": source.name,
            "path": str(source),
            "kind": "file",
            "upload_size_bytes": source.stat().st_size,
            "extracted_text": large_markdown,
            "docling_dir": str(representation),
            "docling_manifest": self.parsed[".html"].manifest,
            "workspace_files": [source.name],
            "ignored_file_count": 0,
        }
        workspace_root = self.root / "workspaces"
        workspace, _context = build_job_workspace(
            str(uuid.uuid4()), [record], workspace_root
        )
        tools = ReadOnlyWorkspaceTools(Path(workspace["root"]))
        summary = workspace["summary"]["files"][0]
        self.assertTrue(summary["docling_path"].endswith(".docling"))
        content = tools.read_text_file(summary["docling_content_path"])
        self.assertLessEqual(len(content), MAX_TOOL_READ_CHARS + 500)
        canonical = tools.read_text_file(
            f"{summary['docling_path']}/canonical.json"
        )
        self.assertIn("not readable into model context", canonical)

    def test_malformed_unsupported_and_cached_inputs_are_bounded(self) -> None:
        malformed = self.sources / "broken.docx"
        malformed.write_bytes(b"not an office document")
        with self.assertRaises(DocumentParseError) as raised:
            parse_document(malformed, self.root / "broken-output")
        self.assertEqual(raised.exception.reason, "docling_conversion_failed")
        self.assertLessEqual(len(raised.exception.message), 1_000)

        unsupported = self.sources / "unsupported.rtf"
        unsupported.write_text("unsupported", encoding="utf-8")
        with self.assertRaises(DocumentParseError) as raised:
            parse_document(unsupported, self.root / "unsupported-output")
        self.assertEqual(raised.exception.reason, "unsupported_document")

        class MustNotRun:
            def convert(self, *_args, **_kwargs):
                raise AssertionError("cached document was reparsed")

        cached = parse_document(
            self.docx,
            self.parsed[".docx"].output_dir,
            display_name=self.docx.name,
            converter=MustNotRun(),
        )
        self.assertTrue(cached.cached)

    def test_upload_integration_persists_reusable_docling_references(self) -> None:
        response = asyncio.run(
            backend_main._store_upload(
                "uploaded.html", self.html.read_bytes()
            )
        )
        record = backend_main._load_upload_record(response.file_id)
        self.assertEqual(record["docling_manifest"]["engine"], "docling")
        self.assertTrue(Path(record["docling_dir"], "canonical.json").is_file())
        self.assertIn("HTML content", response.extracted_text)


class DoclingArtifactsFallbackTests(unittest.TestCase):
    """A missing model cache must degrade to download-on-use, not hard-fail.

    Docling reads DOCLING_ARTIFACTS_PATH from the environment itself, so
    returning None was not enough: it validated the configured directory
    anyway and every upload failed with docling_conversion_failed.
    """

    def setUp(self) -> None:
        self._saved = os.environ.get("DOCLING_ARTIFACTS_PATH")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
        else:
            os.environ["DOCLING_ARTIFACTS_PATH"] = self._saved

    def test_unusable_artifacts_path_is_cleared_from_the_environment(self) -> None:
        import document_ingestion

        missing = Path(tempfile.mkdtemp()) / "no-such-models"
        os.environ["DOCLING_ARTIFACTS_PATH"] = str(missing)
        original = document_ingestion._artifacts_dir_usable
        document_ingestion._artifacts_dir_usable = lambda path: False
        try:
            resolved = document_ingestion._resolve_artifacts_path()
        finally:
            document_ingestion._artifacts_dir_usable = original

        self.assertIsNone(resolved)
        self.assertNotIn("DOCLING_ARTIFACTS_PATH", os.environ)

    def test_usable_artifacts_path_is_kept(self) -> None:
        import document_ingestion

        bundled = Path(tempfile.mkdtemp())
        (bundled / "weights.bin").write_bytes(b"x")
        os.environ["DOCLING_ARTIFACTS_PATH"] = str(bundled)

        self.assertEqual(
            document_ingestion._resolve_artifacts_path(), bundled
        )
        self.assertEqual(
            os.environ.get("DOCLING_ARTIFACTS_PATH"), str(bundled)
        )


if __name__ == "__main__":
    unittest.main()
