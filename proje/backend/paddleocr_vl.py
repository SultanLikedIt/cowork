"""PaddleOCR-VL client: scanned pages and images -> DoclingDocument.

PaddleOCR-VL is not a line-level OCR engine Docling can plug into its OCR
stage (that interface wants text cells with boxes). It is a document parser:
PP-DocLayout finds the blocks on a page, the PaddleOCR-VL-1.6 vision-language
model reads each block (text, tables as HTML, formulas), and the pipeline
returns them in reading order. That pipeline runs as its own service
(paddleocr-vl-api in docker-compose.yml); this module calls its
``POST /layout-parsing`` endpoint and rebuilds the blocks as a
DoclingDocument, so everything downstream of document_ingestion (canonical
JSON, Markdown, chunks, table CSVs, workspace tools) stays unchanged.

Any failure raises PaddleOcrVlError; document_ingestion then falls back to
the Docling + Tesseract path instead of failing the upload.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("agent_webapp.paddleocr_vl")

PDF_FILE_TYPE = 0
IMAGE_FILE_TYPE = 1

# Layout labels emitted by PP-DocLayoutV3 (paddlex configs/pipelines/
# PaddleOCR-VL-1.6.yaml). Anything unlisted is kept as body text rather than
# dropped: losing a block of a user's scan is worse than mislabelling it.
_TITLE_LABELS = frozenset({"doc_title"})
_HEADING_LABELS = frozenset({"paragraph_title"})
_FURNITURE_LABELS = frozenset(
    {"header", "footer", "number", "header_image", "footer_image", "aside_text"}
)
_FOOTNOTE_LABELS = frozenset({"footnote", "vision_footnote"})
_CAPTION_LABELS = frozenset({"figure_title", "table_title", "chart_title"})
_FORMULA_LABELS = frozenset({"display_formula", "inline_formula", "formula"})
_PICTURE_LABELS = frozenset({"image", "chart", "seal"})
_REFERENCE_LABELS = frozenset({"reference", "reference_content"})
_SKIPPED_LABELS = frozenset({"formula_number"})


class PaddleOcrVlError(RuntimeError):
    """PaddleOCR-VL could not produce a usable result; callers fall back."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = " ".join(str(message or "").split())[:1_000]
        super().__init__(f"{reason}: {self.message}")


@dataclass(frozen=True)
class PaddleOcrVlSettings:
    url: str
    timeout_seconds: float
    connect_timeout_seconds: float
    use_doc_orientation_classify: bool
    use_doc_unwarping: bool

    @property
    def configured(self) -> bool:
        return bool(self.url)


def _env_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, "") or default))
    except ValueError:
        return default


def settings_from_env() -> PaddleOcrVlSettings:
    return PaddleOcrVlSettings(
        url=os.getenv("PADDLEOCR_VL_URL", "").strip().rstrip("/"),
        # Read timeout for one whole document. Keep it below
        # DOCLING_PARSE_TIMEOUT_SECONDS so a slow service still leaves time
        # for the Tesseract fallback inside the same upload request.
        timeout_seconds=_env_float("PADDLEOCR_VL_TIMEOUT_SECONDS", 240.0),
        # Short on purpose: a stopped service should cost seconds, not the
        # whole read timeout, before the fallback runs.
        connect_timeout_seconds=_env_float("PADDLEOCR_VL_CONNECT_TIMEOUT_SECONDS", 5.0),
        use_doc_orientation_classify=(
            os.getenv("PADDLEOCR_VL_ORIENTATION", "true").strip().lower() == "true"
        ),
        use_doc_unwarping=(
            os.getenv("PADDLEOCR_VL_UNWARPING", "false").strip().lower() == "true"
        ),
    )


def request_layout_parsing(
    source: Path,
    *,
    file_type: int,
    settings: PaddleOcrVlSettings,
    client: Any = None,
) -> list[dict[str, Any]]:
    """POST one file to the PaddleOCR-VL service; return its per-page results."""
    import httpx

    if not settings.configured:
        raise PaddleOcrVlError("not_configured", "PADDLEOCR_VL_URL is not set.")
    payload = {
        "file": base64.b64encode(Path(source).read_bytes()).decode("ascii"),
        "fileType": file_type,
        "useDocOrientationClassify": settings.use_doc_orientation_classify,
        "useDocUnwarping": settings.use_doc_unwarping,
        # Only prunedResult is consumed; skip the server-side image encoding
        # that would otherwise dominate the response size.
        "visualize": False,
        "returnMarkdownImages": False,
        "prettifyMarkdown": False,
    }
    timeout = httpx.Timeout(
        settings.timeout_seconds, connect=settings.connect_timeout_seconds
    )
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.post(
            f"{settings.url}/layout-parsing", json=payload, timeout=timeout
        )
    except httpx.TimeoutException as exc:
        raise PaddleOcrVlError("timeout", f"PaddleOCR-VL timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise PaddleOcrVlError("unavailable", f"PaddleOCR-VL unreachable: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    try:
        body = response.json()
    except ValueError as exc:
        raise PaddleOcrVlError(
            "bad_response", f"HTTP {response.status_code}, non-JSON body"
        ) from exc
    if response.status_code != 200 or body.get("errorCode") not in (0, None):
        raise PaddleOcrVlError(
            "service_error",
            f"HTTP {response.status_code}: {body.get('errorMsg') or body.get('detail')}",
        )
    results = (body.get("result") or {}).get("layoutParsingResults")
    if not isinstance(results, list) or not results:
        raise PaddleOcrVlError("bad_response", "layoutParsingResults is missing or empty.")
    pages: list[dict[str, Any]] = []
    for index, item in enumerate(results, start=1):
        pruned = item.get("prunedResult") if isinstance(item, dict) else None
        if not isinstance(pruned, dict) or not isinstance(
            pruned.get("parsing_res_list"), list
        ):
            raise PaddleOcrVlError(
                "bad_response", f"Page {index} has no parsing_res_list."
            )
        pages.append(pruned)
    return pages


def _bbox(block: dict[str, Any], width: float, height: float) -> Any:
    from docling_core.types.doc import BoundingBox, CoordOrigin

    raw = block.get("block_bbox")
    try:
        left, top, right, bottom = (float(value) for value in raw)
    except (TypeError, ValueError):
        left, top, right, bottom = 0.0, 0.0, width, height
    return BoundingBox(
        l=left, t=top, r=right, b=bottom, coord_origin=CoordOrigin.TOPLEFT
    )


class _HtmlTableParser(HTMLParser):
    """Collect <tr>/<td>/<th> cells with their spans from one HTML table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._cell: Optional[dict[str, Any]] = None
        self._in_thead = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self.rows.append([])
        elif tag in {"td", "th"}:
            if not self.rows:
                self.rows.append([])
            values = dict(attrs)
            self._cell = {
                "text": [],
                "header": tag == "th" or self._in_thead,
                "rowspan": _span(values.get("rowspan")),
                "colspan": _span(values.get("colspan")),
            }
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "thead":
            self._in_thead = False
        elif tag in {"td", "th"} and self._cell is not None:
            self.rows[-1].append(self._cell)
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)


def _span(value: Optional[str]) -> int:
    try:
        return max(1, min(int(value or 1), 1_000))
    except ValueError:
        return 1


def html_table_to_table_data(html: str) -> Optional[Any]:
    """Convert PaddleOCR-VL's HTML table into Docling TableData, or None."""
    from docling_core.types.doc import TableCell, TableData

    if "<table" not in (html or "").lower():
        return None
    parser = _HtmlTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup is kept as text instead
        return None
    occupied: set[tuple[int, int]] = set()
    cells: list[Any] = []
    num_cols = 0
    for row_index, row in enumerate(parser.rows):
        col_index = 0
        for cell in row:
            while (row_index, col_index) in occupied:
                col_index += 1
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    occupied.add((row_index + dr, col_index + dc))
            cells.append(
                TableCell(
                    text=" ".join("".join(cell["text"]).split()),
                    row_span=cell["rowspan"],
                    col_span=cell["colspan"],
                    start_row_offset_idx=row_index,
                    end_row_offset_idx=row_index + cell["rowspan"],
                    start_col_offset_idx=col_index,
                    end_col_offset_idx=col_index + cell["colspan"],
                    column_header=cell["header"],
                )
            )
            col_index += cell["colspan"]
            num_cols = max(num_cols, col_index)
    if not cells:
        return None
    num_rows = max(row for row, _ in occupied) + 1
    return TableData(table_cells=cells, num_rows=num_rows, num_cols=num_cols)


def build_docling_document(pages: list[dict[str, Any]], name: str) -> Any:
    """Rebuild PaddleOCR-VL page results as a DoclingDocument.

    Page numbers and block boxes become provenance (in the page image's pixel
    space, which is the coordinate system PaddleOCR-VL reports), so manifest
    headings and table references keep their page numbers.
    """
    from docling_core.types.doc import (
        ContentLayer,
        DocItemLabel,
        DoclingDocument,
        ProvenanceItem,
        Size,
    )

    document = DoclingDocument(name=Path(name).stem or "document")
    for page_no, page in enumerate(pages, start=1):
        width = float(page.get("width") or 0) or 1.0
        height = float(page.get("height") or 0) or 1.0
        document.add_page(page_no=page_no, size=Size(width=width, height=height))
        for block in page.get("parsing_res_list") or []:
            if not isinstance(block, dict):
                continue
            label = str(block.get("block_label") or "text").strip().lower()
            text = str(block.get("block_content") or "").strip()
            if label in _SKIPPED_LABELS:
                continue

            def prov(content: str = text) -> Any:
                return ProvenanceItem(
                    page_no=page_no,
                    bbox=_bbox(block, width, height),
                    charspan=(0, len(content)),
                )

            if label == "table":
                table_data = html_table_to_table_data(text)
                if table_data is not None:
                    document.add_table(data=table_data, prov=prov(""))
                    continue
                # Unparseable table markup still carries the scan's words.
                label = "text"
            if label in _PICTURE_LABELS:
                document.add_picture(prov=prov(""))
                # Seal/image blocks can carry recognized text; keep it.
                if text:
                    document.add_text(label=DocItemLabel.TEXT, text=text, prov=prov())
                continue
            if not text:
                continue
            if label in _TITLE_LABELS:
                document.add_title(text=text, prov=prov())
            elif label in _HEADING_LABELS:
                document.add_heading(text=text, level=1, prov=prov())
            elif label in _FURNITURE_LABELS:
                document.add_text(
                    label=DocItemLabel.PAGE_FOOTER
                    if label.startswith("footer") or label == "number"
                    else DocItemLabel.PAGE_HEADER,
                    text=text,
                    prov=prov(),
                    content_layer=ContentLayer.FURNITURE,
                )
            elif label in _FOOTNOTE_LABELS:
                document.add_text(label=DocItemLabel.FOOTNOTE, text=text, prov=prov())
            elif label in _CAPTION_LABELS:
                document.add_text(label=DocItemLabel.CAPTION, text=text, prov=prov())
            elif label in _FORMULA_LABELS:
                document.add_formula(text=text, prov=prov())
            elif label == "algorithm":
                document.add_code(text=text, prov=prov())
            elif label in _REFERENCE_LABELS:
                document.add_text(label=DocItemLabel.REFERENCE, text=text, prov=prov())
            else:
                document.add_text(label=DocItemLabel.TEXT, text=text, prov=prov())
    return document


def parse_with_paddleocr_vl(
    source: Path,
    *,
    is_pdf: bool,
    expected_pages: Optional[int] = None,
    settings: Optional[PaddleOcrVlSettings] = None,
    client: Any = None,
) -> tuple[Any, int]:
    """Parse one upload through the service; return (DoclingDocument, pages)."""
    settings = settings or settings_from_env()
    pages = request_layout_parsing(
        source,
        file_type=PDF_FILE_TYPE if is_pdf else IMAGE_FILE_TYPE,
        settings=settings,
        client=client,
    )
    # PaddleX serving silently stops at Serving.extra.max_num_input_imgs
    # (10 by default) and reports success. A truncated scan must not pass
    # as the whole document.
    if expected_pages is not None and len(pages) < expected_pages:
        raise PaddleOcrVlError(
            "incomplete_result",
            f"Service returned {len(pages)} of {expected_pages} pages; check "
            "Serving.extra.max_num_input_imgs in the pipeline config.",
        )
    try:
        document = build_docling_document(pages, source.name)
    except Exception as exc:  # noqa: BLE001 - an odd block must not fail the upload
        raise PaddleOcrVlError(
            "conversion_failed", f"Could not rebuild the service result: {exc}"
        ) from exc
    return document, len(pages)


__all__ = [
    "PaddleOcrVlError",
    "PaddleOcrVlSettings",
    "build_docling_document",
    "html_table_to_table_data",
    "parse_with_paddleocr_vl",
    "request_layout_parsing",
    "settings_from_env",
]
