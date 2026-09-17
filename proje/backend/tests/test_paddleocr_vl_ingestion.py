"""PaddleOCR-VL ingestion: service responses -> Docling references, and fallback.

No GPU or service needed: httpx.MockTransport stands in for the
paddleocr-vl-api container and returns responses shaped like its
POST /layout-parsing (prunedResult.parsing_res_list per page).
"""

from __future__ import annotations

import base64
import contextlib
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image
from reportlab.pdfgen import canvas

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="paddleocr-vl-test-"))

import document_ingestion
from document_ingestion import DocumentParseError, parse_document
from paddleocr_vl import PaddleOcrVlError, html_table_to_table_data

SERVICE_URL = "http://paddleocr-vl.test:8080"

_ENV_KEYS = (
    "DOCLING_ENABLE_OCR",
    "OCR_ENGINE",
    "OCR_FALLBACK_ENGINE",
    "OCR_LANGS",
    "OCR_FORCE_FULL_PAGE",
    "PADDLEOCR_VL_URL",
    "PADDLEOCR_VL_ORIENTATION",
    "PADDLEOCR_VL_UNWARPING",
)


@contextlib.contextmanager
def _env(**values: str):
    with mock.patch.dict(os.environ):
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(values)
        yield


def _paddle_env(**extra: str):
    return _env(DOCLING_ENABLE_OCR="true", PADDLEOCR_VL_URL=SERVICE_URL, **extra)


def _block(label: str, content: str, bbox=(10, 10, 500, 60)) -> dict:
    return {"block_label": label, "block_content": content, "block_bbox": list(bbox)}


INVOICE_TABLE = (
    "<table><tr><th>Ürün</th><th>Adet</th><th>Tutar</th></tr>"
    "<tr><td>Çelik vida</td><td>12</td><td>480 TL</td></tr>"
    "<tr><td colspan=\"2\">Genel toplam</td><td>1.250 TL</td></tr></table>"
)

PAGE_ONE = {
    "width": 1654,
    "height": 2339,
    "parsing_res_list": [
        _block("header", "ACME Ltd. Şti."),
        _block("doc_title", "Fatura Numarası 4821"),
        _block("text", "Güneşli çiçek öğrenci ışık İzmir"),
        _block("paragraph_title", "Ödeme Bilgileri", bbox=(10, 300, 800, 360)),
        _block("table", INVOICE_TABLE, bbox=(10, 400, 1600, 900)),
        _block("formula_number", "(1)"),
        _block("some_future_label", "Yeni etiket metni"),
        _block("footer", "Sayfa 1"),
    ],
}
PAGE_TWO = {
    "width": 1654,
    "height": 2339,
    "parsing_res_list": [
        _block("paragraph_title", "Teslimat Koşulları"),
        _block("text", "Teslimat 30 gün içinde yapılır."),
    ],
}


def _response(pages: list[dict]) -> dict:
    return {
        "logId": "test",
        "errorCode": 0,
        "errorMsg": "Success",
        "result": {
            "layoutParsingResults": [
                {"prunedResult": page, "markdown": {"text": ""}} for page in pages
            ],
            "dataInfo": {},
        },
    }


class _Service:
    """Records requests and answers like paddleocr-vl-api (or fails on cue)."""

    def __init__(self, pages=None, *, status=200, error: Exception | None = None):
        self.pages = pages or [PAGE_ONE]
        self.status = status
        self.error = error
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {"url": str(request.url), "body": json.loads(request.content)}
        )
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return httpx.Response(
                self.status, json={"errorCode": self.status, "errorMsg": "boom"}
            )
        return httpx.Response(200, json=_response(self.pages))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class _FallbackConverter:
    """Stands in for Docling (native PDF text, or the Tesseract fallback).

    Produces one page per input page, each with a "docling page N" line, so
    tests can see which parser read which page.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.page_counts: list[int] = []

    def convert(self, source, **_kwargs):
        import pypdfium2
        from docling_core.types.doc import (
            BoundingBox,
            DocItemLabel,
            DoclingDocument,
            ProvenanceItem,
            Size,
        )

        self.calls += 1
        pages = 1
        if Path(source).suffix.lower() == ".pdf":
            pdf = pypdfium2.PdfDocument(str(source))
            pages = len(pdf)
            pdf.close()
        self.page_counts.append(pages)
        document = DoclingDocument(name="fallback")
        document.add_text(label=DocItemLabel.TEXT, text="tesseract fallback text")
        for page_no in range(1, pages + 1):
            document.add_page(page_no=page_no, size=Size(width=100, height=100))
            document.add_text(
                label=DocItemLabel.TEXT,
                text=f"docling page {page_no}",
                prov=ProvenanceItem(
                    page_no=page_no,
                    bbox=BoundingBox(l=0, t=0, r=10, b=10),
                    charspan=(0, 1),
                ),
            )
        return mock.Mock(document=document, input=mock.Mock(page_count=pages))


class _MustNotConvert:
    def convert(self, *_args, **_kwargs):
        raise AssertionError("Docling converter ran although PaddleOCR-VL succeeded")


def _image(root: Path) -> Path:
    path = root / "fatura.png"
    Image.new("RGB", (400, 300), "white").save(path)
    return path


def _pdf(root: Path, kinds: str) -> Path:
    """One page per letter: "S" = scan (image only), "T" = real text layer."""
    path = root / f"doc-{kinds}.pdf"
    picture = root / "page-picture.png"
    Image.new("RGB", (300, 200), "white").save(picture)
    document = canvas.Canvas(str(path))
    for index, kind in enumerate(kinds, start=1):
        if kind == "T":
            document.drawString(72, 720, f"Dijital sayfa {index}: metin katmanı var.")
        else:
            document.drawImage(str(picture), 72, 500, width=300, height=200)
        document.showPage()
    document.save()
    return path


class PaddleOcrVlParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="paddleocr-vl-"))

    def test_image_is_parsed_into_docling_references_with_turkish_text(self) -> None:
        image = _image(self.root)
        service = _Service([PAGE_ONE])
        with _paddle_env():
            parsed = parse_document(
                image,
                self.root / "out",
                display_name="fatura.png",
                converter=_MustNotConvert(),
                paddle_client=service.client(),
            )

        request = service.requests[0]
        self.assertEqual(request["url"], f"{SERVICE_URL}/layout-parsing")
        self.assertEqual(request["body"]["fileType"], 1)
        self.assertEqual(base64.b64decode(request["body"]["file"]), image.read_bytes())
        self.assertFalse(request["body"]["visualize"])

        for text in (
            "Fatura Numarası 4821",
            "Güneşli çiçek öğrenci ışık İzmir",
            "Ödeme Bilgileri",
            "Çelik vida",
            "Yeni etiket metni",  # unknown labels are kept, not dropped
        ):
            with self.subTest(text=text):
                self.assertIn(text, parsed.text)
        self.assertNotIn("(1)", parsed.text)
        # Page header/footer are furniture, not body text.
        self.assertNotIn("Sayfa 1", parsed.text)
        self.assertNotIn("ACME Ltd. Şti.", parsed.text)

        manifest = parsed.manifest
        self.assertEqual(manifest["engine"], "docling")
        self.assertEqual(
            manifest["parser"],
            {"engine": "paddleocr_vl", "paddleocr_vl_pages": [1], "fallback_reason": None},
        )
        self.assertEqual(manifest["page_count"], 1)
        self.assertFalse(manifest["text_empty"])
        self.assertEqual(
            [(h["text"], h["page"]) for h in manifest["headings"]],
            [("Fatura Numarası 4821", 1), ("Ödeme Bilgileri", 1)],
        )
        self.assertEqual(manifest["table_count"], 1)
        table_ref = manifest["tables"][0]
        self.assertEqual(table_ref["page"], 1)
        with (parsed.output_dir / table_ref["path"]).open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Ürün", "Adet", "Tutar"])
        self.assertEqual(rows[1], ["Çelik vida", "12", "480 TL"])
        self.assertEqual(rows[2][-1], "1.250 TL")
        self.assertTrue((parsed.output_dir / "chunks.jsonl").is_file())
        canonical = json.loads(
            (parsed.output_dir / "canonical.json").read_text(encoding="utf-8")
        )
        self.assertEqual(canonical["engine"], "docling")

    def test_scanned_pdf_goes_to_the_service_with_page_numbers_kept(self) -> None:
        pdf = _pdf(self.root, "SS")
        service = _Service([PAGE_ONE, PAGE_TWO])
        with _paddle_env():
            parsed = parse_document(
                pdf,
                self.root / "out",
                converter=_MustNotConvert(),
                paddle_client=service.client(),
            )
        self.assertEqual(service.requests[0]["body"]["fileType"], 0)
        self.assertEqual(parsed.manifest["page_count"], 2)
        self.assertEqual(
            parsed.manifest["parser"],
            {"engine": "paddleocr_vl", "paddleocr_vl_pages": [1, 2], "fallback_reason": None},
        )
        self.assertIn(("Teslimat Koşulları", 2), [
            (h["text"], h["page"]) for h in parsed.manifest["headings"]
        ])
        self.assertIn("Teslimat 30 gün içinde yapılır.", parsed.text)

    def test_pdf_with_a_text_layer_keeps_native_text_and_skips_the_service(self) -> None:
        service = _Service()
        converter = _FallbackConverter()
        with _paddle_env():
            parsed = parse_document(
                _pdf(self.root, "TT"),
                self.root / "out",
                converter=converter,
                paddle_client=service.client(),
            )
        self.assertEqual(service.requests, [])
        self.assertEqual(converter.page_counts, [2])
        self.assertEqual(
            parsed.manifest["parser"],
            {"engine": "docling", "paddleocr_vl_pages": [], "fallback_reason": None},
        )

    def test_mixed_pdf_sends_only_scanned_pages_and_keeps_page_order(self) -> None:
        pdf = _pdf(self.root, "TSST")
        service = _Service([PAGE_ONE, PAGE_TWO])
        converter = _FallbackConverter()
        with _paddle_env():
            parsed = parse_document(
                pdf, self.root / "out", converter=converter, paddle_client=service.client()
            )

        # One request with just the two scanned pages, as their own PDF.
        self.assertEqual(len(service.requests), 1)
        import pypdfium2

        sent = pypdfium2.PdfDocument(base64.b64decode(service.requests[0]["body"]["file"]))
        self.assertEqual(len(sent), 2)
        sent.close()
        # Docling read pages 1 and 4 as separate one-page runs.
        self.assertEqual(converter.page_counts, [1, 1])

        manifest = parsed.manifest
        self.assertEqual(manifest["page_count"], 4)
        self.assertEqual(
            manifest["parser"],
            {"engine": "hybrid", "paddleocr_vl_pages": [2, 3], "fallback_reason": None},
        )
        self.assertEqual(
            [(h["text"], h["page"]) for h in manifest["headings"]],
            [("Fatura Numarası 4821", 2), ("Ödeme Bilgileri", 2), ("Teslimat Koşulları", 3)],
        )
        self.assertEqual(manifest["tables"][0]["page"], 2)
        order = [
            parsed.text.index(marker)
            for marker in ("docling page 1", "Fatura Numarası 4821", "Teslimat Koşulları")
        ]
        self.assertEqual(order, sorted(order))
        self.assertGreater(parsed.text.rindex("docling page 1"), order[2])

    def test_mixed_pdf_falls_back_whole_when_the_service_fails(self) -> None:
        converter = _FallbackConverter()
        with _paddle_env():
            parsed = parse_document(
                _pdf(self.root, "TST"),
                self.root / "out",
                converter=converter,
                paddle_client=_Service(error=httpx.ConnectError("down")).client(),
            )
        # The first text run was already converted, then the whole PDF again.
        self.assertEqual(converter.page_counts[-1], 3)
        self.assertEqual(
            parsed.manifest["parser"], {"engine": "docling", "fallback_reason": "unavailable"}
        )
        self.assertEqual(parsed.manifest["page_count"], 3)

    def test_truncated_pdf_result_falls_back_instead_of_passing_as_complete(self) -> None:
        pdf = _pdf(self.root, "SS")
        converter = _FallbackConverter()
        with _paddle_env():
            parsed = parse_document(
                pdf,
                self.root / "out",
                converter=converter,
                paddle_client=_Service([PAGE_ONE]).client(),
            )
        self.assertEqual(converter.calls, 1)
        self.assertIn("tesseract fallback text", parsed.text)
        self.assertEqual(
            parsed.manifest["parser"],
            {"engine": "docling", "fallback_reason": "incomplete_result"},
        )

    def test_service_failures_fall_back_to_docling(self) -> None:
        cases = {
            "unavailable": _Service(error=httpx.ConnectError("refused")),
            "timeout": _Service(error=httpx.ReadTimeout("slow")),
            "service_error": _Service(status=500),
        }
        for reason, service in cases.items():
            with self.subTest(reason=reason):
                converter = _FallbackConverter()
                with _paddle_env():
                    parsed = parse_document(
                        _image(self.root),
                        self.root / f"out-{reason}",
                        converter=converter,
                        paddle_client=service.client(),
                    )
                self.assertEqual(converter.calls, 1)
                self.assertEqual(parsed.manifest["parser"]["fallback_reason"], reason)
                self.assertEqual(parsed.manifest["ocr"]["fallback_engine"], "tesseract")

    def test_unexpected_conversion_errors_fall_back_too(self) -> None:
        converter = _FallbackConverter()
        with _paddle_env(), mock.patch(
            "paddleocr_vl.build_docling_document",
            side_effect=ValueError("unexpected block shape"),
        ):
            parsed = parse_document(
                _image(self.root),
                self.root / "out",
                converter=converter,
                paddle_client=_Service([PAGE_ONE]).client(),
            )
        self.assertEqual(converter.calls, 1)
        self.assertEqual(parsed.manifest["parser"]["fallback_reason"], "conversion_failed")

    def test_missing_url_skips_the_service_without_a_request(self) -> None:
        service = _Service()
        converter = _FallbackConverter()
        with _env(DOCLING_ENABLE_OCR="true"):
            parsed = parse_document(
                _image(self.root),
                self.root / "out",
                converter=converter,
                paddle_client=service.client(),
            )
        self.assertEqual(service.requests, [])
        self.assertEqual(parsed.manifest["parser"]["fallback_reason"], "not_configured")

    def test_ocr_disabled_never_calls_the_service(self) -> None:
        service = _Service()
        converter = _FallbackConverter()
        with _env(DOCLING_ENABLE_OCR="false", PADDLEOCR_VL_URL=SERVICE_URL):
            parsed = parse_document(
                _pdf(self.root, "S"),
                self.root / "out",
                converter=converter,
                paddle_client=service.client(),
            )
        self.assertEqual(service.requests, [])
        self.assertEqual(
            parsed.manifest["parser"], {"engine": "docling", "fallback_reason": None}
        )

    def test_fallback_parses_are_retried_but_service_parses_are_cached(self) -> None:
        image = _image(self.root)
        output = self.root / "out"
        with _paddle_env():
            parse_document(
                image,
                output,
                converter=_FallbackConverter(),
                paddle_client=_Service(error=httpx.ConnectError("down")).client(),
            )
            recovered = _Service([PAGE_ONE])
            retried = parse_document(
                image, output, converter=_MustNotConvert(), paddle_client=recovered.client()
            )
            self.assertFalse(retried.cached)
            self.assertEqual(len(recovered.requests), 1)

            again = _Service([PAGE_ONE])
            cached = parse_document(
                image, output, converter=_MustNotConvert(), paddle_client=again.client()
            )
        self.assertTrue(cached.cached)
        self.assertEqual(again.requests, [])

    def test_docling_errors_after_a_fallback_still_surface(self) -> None:
        class Broken:
            def convert(self, *_args, **_kwargs):
                raise RuntimeError("tesseract missing")

        with _paddle_env():
            with self.assertRaises(DocumentParseError) as raised:
                parse_document(
                    _image(self.root),
                    self.root / "out",
                    converter=Broken(),
                    paddle_client=_Service(status=503).client(),
                )
        self.assertEqual(raised.exception.reason, "docling_conversion_failed")


class HtmlTableTests(unittest.TestCase):
    def test_row_and_column_spans_are_placed_on_the_grid(self) -> None:
        data = html_table_to_table_data(
            "<table><thead><tr><th rowspan='2'>Bölge</th><th colspan='2'>Satış</th></tr>"
            "<tr><th>2025</th><th>2026</th></tr></thead>"
            "<tbody><tr><td>Ege</td><td>10</td><td>12</td></tr></tbody></table>"
        )
        self.assertEqual((data.num_rows, data.num_cols), (3, 3))
        placed = {
            cell.text: (
                cell.start_row_offset_idx,
                cell.start_col_offset_idx,
                cell.row_span,
                cell.col_span,
                cell.column_header,
            )
            for cell in data.table_cells
        }
        self.assertEqual(placed["Bölge"], (0, 0, 2, 1, True))
        self.assertEqual(placed["Satış"], (0, 1, 1, 2, True))
        self.assertEqual(placed["2025"], (1, 1, 1, 1, True))
        self.assertEqual(placed["Ege"], (2, 0, 1, 1, False))

    def test_non_table_content_is_rejected(self) -> None:
        self.assertIsNone(html_table_to_table_data("Tablo okunamadı"))
        self.assertIsNone(html_table_to_table_data("<table></table>"))

    def test_malformed_table_block_is_kept_as_text(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paddleocr-vl-table-"))
        page = {
            "width": 100,
            "height": 100,
            "parsing_res_list": [_block("table", "Tablo metni: Kalem 5 adet")],
        }
        with _paddle_env():
            parsed = parse_document(
                _image(root),
                root / "out",
                converter=_MustNotConvert(),
                paddle_client=_Service([page]).client(),
            )
        self.assertIn("Tablo metni: Kalem 5 adet", parsed.text)
        self.assertEqual(parsed.manifest["table_count"], 0)


class ServiceResponseValidationTests(unittest.TestCase):
    def test_malformed_responses_raise_paddle_errors(self) -> None:
        from paddleocr_vl import request_layout_parsing, settings_from_env

        root = Path(tempfile.mkdtemp(prefix="paddleocr-vl-resp-"))
        image = _image(root)
        bodies = {
            "empty results": {"errorCode": 0, "result": {"layoutParsingResults": []}},
            "no parsing list": {
                "errorCode": 0,
                "result": {"layoutParsingResults": [{"prunedResult": {}}]},
            },
        }
        with _paddle_env():
            settings = settings_from_env()
        for name, body in bodies.items():
            with self.subTest(name=name):
                client = httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _request, body=body: httpx.Response(200, json=body)
                    )
                )
                with self.assertRaises(PaddleOcrVlError) as raised:
                    request_layout_parsing(
                        image, file_type=1, settings=settings, client=client
                    )
                self.assertEqual(raised.exception.reason, "bad_response")

        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(502, text="<html>"))
        )
        with self.assertRaises(PaddleOcrVlError) as raised:
            request_layout_parsing(image, file_type=1, settings=settings, client=client)
        self.assertEqual(raised.exception.reason, "bad_response")


if __name__ == "__main__":
    unittest.main()
