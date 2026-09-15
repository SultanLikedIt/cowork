"""Deterministic Docling ingestion and workspace-reference regressions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
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


_OCR_ENV_KEYS = (
    "DOCLING_ENABLE_OCR",
    "OCR_ENGINE",
    "OCR_LANGS",
    "OCR_FORCE_FULL_PAGE",
)


@contextlib.contextmanager
def _ocr_env(**values: str):
    """Exactly the given OCR variables set; the environment is restored after."""
    with mock.patch.dict(os.environ):
        for key in _OCR_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(values)
        yield


def _truetype_font(size: int):
    # Tesseract needs real glyph outlines at a readable size, including the
    # Turkish letters; PIL's bitmap default font cannot draw those.
    for candidate in (
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return None


def _tesseract_has_turkish() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        listed = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "tur" in (listed.stdout + listed.stderr).split()


class OcrSettingsTests(unittest.TestCase):
    """OCR configuration and caching rules; no OCR engine required."""

    def test_defaults_are_disabled_tesseract_turkish_english(self) -> None:
        import document_ingestion

        with _ocr_env():
            settings = document_ingestion.ocr_settings()
        self.assertEqual(
            settings,
            {
                "enabled": False,
                "engine": "tesseract",
                "langs": ["tur", "eng"],
                "force_full_page": False,
            },
        )
        from docling.datamodel.pipeline_options import TesseractCliOcrOptions

        options = document_ingestion._build_ocr_options(settings)
        self.assertIsInstance(options, TesseractCliOcrOptions)
        self.assertEqual(options.lang, ["tur", "eng"])
        self.assertFalse(options.force_full_page_ocr)

    def test_engine_langs_and_full_page_come_from_the_environment(self) -> None:
        import document_ingestion
        from docling.datamodel.pipeline_options import EasyOcrOptions

        with _ocr_env(
            DOCLING_ENABLE_OCR="true",
            OCR_ENGINE="EasyOCR",
            OCR_FORCE_FULL_PAGE="true",
        ):
            settings = document_ingestion.ocr_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["engine"], "easyocr")
        # Each engine has its own language codes; the default follows the engine.
        self.assertEqual(settings["langs"], ["tr", "en"])
        options = document_ingestion._build_ocr_options(settings)
        self.assertIsInstance(options, EasyOcrOptions)
        self.assertTrue(options.force_full_page_ocr)

        with _ocr_env(OCR_LANGS="deu+eng, fra"):
            self.assertEqual(
                document_ingestion.ocr_settings()["langs"], ["deu", "eng", "fra"]
            )

    def test_unknown_engine_falls_back_to_tesseract(self) -> None:
        import document_ingestion

        with _ocr_env(OCR_ENGINE="no-such-engine"):
            self.assertEqual(document_ingestion.ocr_settings()["engine"], "tesseract")

    def test_image_upload_with_ocr_disabled_is_rejected_before_parsing(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="ocr-disabled-"))
        image = root / "photo.png"
        Image.new("RGB", (40, 40), "white").save(image)

        class MustNotRun:
            def convert(self, *_args, **_kwargs):
                raise AssertionError("image was parsed with OCR disabled")

        with _ocr_env(DOCLING_ENABLE_OCR="false"):
            with self.assertRaises(DocumentParseError) as raised:
                parse_document(image, root / "out", converter=MustNotRun())
        self.assertEqual(raised.exception.reason, "ocr_disabled")

    def test_pdf_cache_is_reused_only_for_the_same_ocr_settings(self) -> None:
        import document_ingestion

        root = Path(tempfile.mkdtemp(prefix="ocr-cache-"))
        pdf = root / "scan.pdf"
        canvas.Canvas(str(pdf)).save()
        output = root / "scan.docling"
        output.mkdir()
        with _ocr_env(DOCLING_ENABLE_OCR="false"):
            parsed_with = document_ingestion.ocr_settings()
        (output / "content.md").write_text("", encoding="utf-8")
        (output / "canonical.json").write_text("{}", encoding="utf-8")
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "source": {"sha256": document_ingestion._source_hash(pdf)},
                    "ocr": parsed_with,
                }
            ),
            encoding="utf-8",
        )

        class Reparsed(RuntimeError):
            pass

        class RecordsReparse:
            def convert(self, *_args, **_kwargs):
                raise Reparsed("reparsed")

        with _ocr_env(DOCLING_ENABLE_OCR="false"):
            cached = parse_document(pdf, output, converter=RecordsReparse())
        self.assertTrue(cached.cached)

        with _ocr_env(DOCLING_ENABLE_OCR="true"):
            with self.assertRaises(DocumentParseError) as raised:
                parse_document(pdf, output, converter=RecordsReparse())
        self.assertIn("reparsed", raised.exception.message)


@unittest.skipUnless(
    _tesseract_has_turkish() and _truetype_font(10) is not None,
    "needs the tesseract binary with Turkish data and a TrueType font",
)
class TesseractOcrTests(unittest.TestCase):
    """Real OCR through Docling + Tesseract on generated scans."""

    LINES = ("Fatura Numarası 4821", "Güneşli çiçek öğrenci ışık", "Toplam 1250 TL")

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = _ocr_env(DOCLING_ENABLE_OCR="true", OCR_ENGINE="tesseract")
        cls.env.__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="docling-ocr-"))

        font = _truetype_font(56)
        image = Image.new("RGB", (1700, 500), "white")
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(cls.LINES):
            draw.text((60, 60 + index * 120), line, fill="black", font=font)
        cls.png = cls.root / "scan.png"
        image.save(cls.png)

        # A "scanned" PDF: the page is only a picture, with no text layer.
        cls.pdf = cls.root / "scan.pdf"
        pdf = canvas.Canvas(str(cls.pdf))
        pdf.drawImage(str(cls.png), 30, 500, width=540, height=159)
        pdf.save()

        import document_ingestion

        cls.converter = document_ingestion.build_converter()
        cls.parsed = {
            source.suffix: parse_document(
                source,
                cls.root / f"parsed-{source.suffix.lstrip('.')}",
                converter=cls.converter,
            )
            for source in (cls.png, cls.pdf)
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.__exit__(None, None, None)

    def test_image_text_is_read_with_turkish_characters(self) -> None:
        parsed = self.parsed[".png"]
        for word in ("Fatura", "4821", "Güneşli", "çiçek", "öğrenci", "ışık", "1250"):
            with self.subTest(word=word):
                self.assertIn(word, parsed.text)
        self.assertEqual(parsed.manifest["ocr"]["engine"], "tesseract")
        self.assertTrue(parsed.manifest["ocr"]["enabled"])
        self.assertFalse(parsed.manifest["text_empty"])

    def test_scanned_pdf_without_text_layer_is_read(self) -> None:
        parsed = self.parsed[".pdf"]
        for word in ("Fatura", "4821", "öğrenci"):
            with self.subTest(word=word):
                self.assertIn(word, parsed.text)
        self.assertFalse(parsed.manifest["text_empty"])

    def test_blank_image_is_flagged_as_text_empty(self) -> None:
        blank = self.root / "blank.png"
        Image.new("RGB", (800, 600), "white").save(blank)
        parsed = parse_document(
            blank, self.root / "parsed-blank", converter=self.converter
        )
        self.assertTrue(parsed.manifest["text_empty"])

    def test_image_upload_through_the_api_is_ocr_backed(self) -> None:
        import document_ingestion

        # The process-wide converter may already exist from the non-OCR
        # tests above; build a fresh one under this class's OCR settings.
        with mock.patch.object(document_ingestion, "_CONVERTER", None):
            response = asyncio.run(
                backend_main._store_upload("fatura.png", self.png.read_bytes())
            )
        self.assertIn("4821", response.extracted_text)
        record = backend_main._load_upload_record(response.file_id)
        self.assertEqual(record["docling_manifest"]["ocr"]["engine"], "tesseract")


if __name__ == "__main__":
    unittest.main()
