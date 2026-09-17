"""Thin, bounded Docling adapter for uploaded document understanding.

Docling owns format parsing and the canonical document model.  This module
only persists that model plus Docling-derived Markdown, hierarchical chunks,
and table references so the existing workspace tools can consume them without
reparsing or copying a whole document into model context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Images carry no text layer: OCR is the only way they yield content, so an
# image upload with OCR disabled is rejected instead of parsed to nothing.
IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
)
DOCLING_EXTENSIONS = (
    frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm"}) | IMAGE_EXTENSIONS
)
# Formats whose parse result depends on the OCR configuration.
OCR_EXTENSIONS = frozenset({".pdf"}) | IMAGE_EXTENSIONS
# paddleocr_vl is a separate document-parsing service (paddleocr_vl.py), not
# a Docling OCR stage; the other three run inside Docling's PDF pipeline.
PADDLEOCR_VL_ENGINE = "paddleocr_vl"
_DOCLING_OCR_ENGINES = ("tesseract", "easyocr", "rapidocr")
_OCR_ENGINES = (PADDLEOCR_VL_ENGINE,) + _DOCLING_OCR_ENGINES
# Each engine names languages differently (Tesseract: ISO 639-2 "tur",
# EasyOCR: ISO 639-1 "tr", RapidOCR: model names). Defaults target the
# Turkish + English documents this app is used with. PaddleOCR-VL needs no
# language setting (Turkish is among its 109 languages); with it, OCR_LANGS
# configures the Docling fallback engine.
_DEFAULT_OCR_LANGS = {
    "tesseract": ["tur", "eng"],
    "easyocr": ["tr", "en"],
    "rapidocr": ["english"],
}
MAX_DOCUMENT_BYTES = int(os.getenv("DOCLING_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
MAX_DOCUMENT_PAGES = max(1, min(int(os.getenv("DOCLING_MAX_PAGES", "500")), 2_000))
MAX_CANONICAL_BYTES = int(
    os.getenv("DOCLING_MAX_CANONICAL_BYTES", str(100 * 1024 * 1024))
)
MAX_STORED_TABLES = max(1, min(int(os.getenv("DOCLING_MAX_TABLES", "100")), 2_000))
MAX_STORED_CHUNKS = max(1, min(int(os.getenv("DOCLING_MAX_CHUNKS", "10000")), 50_000))
MAX_MANIFEST_HEADINGS = 500
# With OCR_ENGINE=paddleocr_vl, a PDF page with at least this many
# non-whitespace characters in its text layer keeps Docling's native text;
# below it (a scan, or only a stray page number) it goes to PaddleOCR-VL.
PDF_TEXT_LAYER_MIN_CHARS = max(1, int(os.getenv("PDF_TEXT_LAYER_MIN_CHARS", "20")))


class DocumentParseError(RuntimeError):
    """A stable, structured ingestion failure safe to return to the API."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = " ".join(str(message or "Document parsing failed.").split())[:1_000]
        super().__init__(f"{reason}: {self.message}")


@dataclass(frozen=True)
class ParsedDocument:
    output_dir: Path
    text: str
    manifest: dict[str, Any]
    cached: bool = False


_CONVERTER: Any = None
_CONVERTER_LOCK = threading.Lock()
_logger = logging.getLogger("agent_webapp.document_ingestion")


def _artifacts_dir_usable(path: Path) -> bool:
    """Docling requires a non-empty local model cache when artifacts_path is set."""
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _resolve_artifacts_path() -> Optional[Path]:
    bundled = Path(__file__).parent / "vendor" / "docling-models"
    configured = os.getenv("DOCLING_ARTIFACTS_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    if bundled not in candidates:
        candidates.append(bundled)
    for candidate in candidates:
        if _artifacts_dir_usable(candidate):
            return candidate
    if configured:
        # Returning None is not enough, and neither is clearing the env var.
        # Docling's global settings object reads DOCLING_ARTIFACTS_PATH once,
        # when docling.datamodel.settings is imported, and BasePipeline falls
        # back to that captured value whenever pipeline_options.artifacts_path
        # is None (base_pipeline.py: "elif settings.artifacts_path is not
        # None"). It then hard-fails validation on the missing directory, so
        # every upload -- PDF, image, and DOCX alike -- died with
        # "docling_conversion_failed". Popping the env var only helps a
        # process that has not imported docling yet, which is never the case
        # here; resetting the live settings object is what actually restores
        # the documented download-on-first-use fallback.
        os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
        try:
            from docling.datamodel.settings import settings as docling_settings

            docling_settings.artifacts_path = None
        except Exception:  # noqa: BLE001 - never block ingestion on this
            _logger.warning("Could not reset docling settings", exc_info=True)
        _logger.warning(
            "DOCLING_ARTIFACTS_PATH=%s is missing or empty; Docling will "
            "download models on first use (requires outbound network). Run "
            "'docling-tools models download --output-dir backend/vendor/docling-models' "
            "and rebuild the backend image for offline use.",
            configured,
        )
    return None


def _env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def ocr_settings() -> dict[str, Any]:
    """OCR configuration from the environment; also recorded in manifests.

    ``engine`` is the preferred engine. ``fallback_engine`` is the Docling
    OCR engine used when PaddleOCR-VL is preferred but unavailable; it is
    None for the Docling engines, which have no fallback.
    """
    engine = os.getenv("OCR_ENGINE", "").strip().lower() or PADDLEOCR_VL_ENGINE
    if engine not in _OCR_ENGINES:
        _logger.warning("Unknown OCR_ENGINE=%r; using %s.", engine, PADDLEOCR_VL_ENGINE)
        engine = PADDLEOCR_VL_ENGINE
    fallback_engine: Optional[str] = None
    if engine == PADDLEOCR_VL_ENGINE:
        fallback_engine = (
            os.getenv("OCR_FALLBACK_ENGINE", "").strip().lower() or "tesseract"
        )
        if fallback_engine not in _DOCLING_OCR_ENGINES:
            _logger.warning(
                "Unknown OCR_FALLBACK_ENGINE=%r; using tesseract.", fallback_engine
            )
            fallback_engine = "tesseract"
    docling_engine = fallback_engine or engine
    langs = [
        token.strip()
        for token in re.split(r"[,+]", os.getenv("OCR_LANGS", ""))
        if token.strip()
    ] or list(_DEFAULT_OCR_LANGS[docling_engine])
    return {
        "enabled": _env_flag("DOCLING_ENABLE_OCR"),
        "engine": engine,
        "fallback_engine": fallback_engine,
        "langs": langs,
        "force_full_page": _env_flag("OCR_FORCE_FULL_PAGE"),
    }


def _build_ocr_options(settings: dict[str, Any]) -> Any:
    from docling.datamodel.pipeline_options import (
        EasyOcrOptions,
        RapidOcrOptions,
        TesseractCliOcrOptions,
    )

    option_class = {
        "tesseract": TesseractCliOcrOptions,
        "easyocr": EasyOcrOptions,
        "rapidocr": RapidOcrOptions,
    }[settings.get("fallback_engine") or settings["engine"]]
    return option_class(
        lang=list(settings["langs"]),
        force_full_page_ocr=bool(settings["force_full_page"]),
    )


def build_converter(settings: Optional[dict[str, Any]] = None) -> Any:
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import (
        DocumentConverter,
        ImageFormatOption,
        InputFormat,
        PdfFormatOption,
    )

    settings = settings or ocr_settings()
    # OCR is opt-in (DOCLING_ENABLE_OCR). Unless force_full_page is set,
    # Docling only OCRs bitmap regions, so text-bearing PDFs keep their
    # native text and stay fast. Tesseract needs no model download; EasyOCR
    # and RapidOCR fetch weights on first use. With OCR_ENGINE=paddleocr_vl
    # this converter is the fallback path and uses OCR_FALLBACK_ENGINE.
    pdf_options = PdfPipelineOptions(
        do_ocr=bool(settings["enabled"]),
        ocr_options=_build_ocr_options(settings),
        artifacts_path=_resolve_artifacts_path(),
    )
    return DocumentConverter(
        allowed_formats=[
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.PPTX,
            InputFormat.XLSX,
            InputFormat.HTML,
            InputFormat.IMAGE,
        ],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
            # Images run through the same page pipeline as scanned PDFs.
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pdf_options),
        },
    )


def _get_converter() -> Any:
    global _CONVERTER
    with _CONVERTER_LOCK:
        if _CONVERTER is None:
            _CONVERTER = build_converter()
        return _CONVERTER


def _has_visible_text(markdown: str) -> bool:
    # Docling writes "<!-- image -->" placeholders for pictures it could not
    # read; those alone mean the document produced no usable text.
    return bool(re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL).strip())


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _page_number(item: dict[str, Any]) -> Optional[int]:
    provenance = item.get("prov") or []
    if provenance and isinstance(provenance[0], dict):
        value = provenance[0].get("page_no")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _headings(document_dict: dict[str, Any]) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for item in document_dict.get("texts") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        if label not in {"title", "section_header"}:
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            level = int(item.get("level") or (1 if label == "title" else 2))
        except (TypeError, ValueError):
            level = 1 if label == "title" else 2
        headings.append(
            {
                "text": text[:1_000],
                "level": level,
                "page": _page_number(item),
                "self_ref": item.get("self_ref"),
            }
        )
        if len(headings) >= MAX_MANIFEST_HEADINGS:
            break
    return headings


def _write_chunks(document: Any, target: Path) -> int:
    from docling_core.transforms.chunker import HierarchicalChunker

    count = 0
    with target.open("w", encoding="utf-8") as handle:
        for chunk in HierarchicalChunker().chunk(document):
            if count >= MAX_STORED_CHUNKS:
                break
            metadata = getattr(chunk, "meta", None)
            record = {
                "chunk_id": count + 1,
                "text": str(getattr(chunk, "text", "")),
                "meta": (
                    metadata.model_dump(mode="json")
                    if hasattr(metadata, "model_dump")
                    else {}
                ),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def _write_tables(document: Any, output_dir: Path) -> list[dict[str, Any]]:
    table_dir = output_dir / "tables"
    references: list[dict[str, Any]] = []
    tables = list(getattr(document, "tables", None) or [])[:MAX_STORED_TABLES]
    if tables:
        table_dir.mkdir(mode=0o700)
    for index, table in enumerate(tables, start=1):
        try:
            frame = table.export_to_dataframe(doc=document)
        except Exception as exc:
            raise DocumentParseError(
                "table_extraction_failed",
                f"Docling could not serialize table {index}: {exc}",
            ) from exc
        filename = f"table-{index}.csv"
        frame.to_csv(table_dir / filename, index=False)
        references.append(
            {
                "table_id": index,
                "path": f"tables/{filename}",
                "rows": int(len(frame)),
                "columns": [str(column) for column in frame.columns],
                "self_ref": str(getattr(table, "self_ref", "") or ""),
                "page": _page_number(
                    table.model_dump(mode="json")
                    if hasattr(table, "model_dump")
                    else {}
                ),
            }
        )
    return references


def _pdf_text_layer_pages(source: Path) -> Optional[list[bool]]:
    """Per page: does it carry a real text layer? None if unreadable."""
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(source))
        try:
            flags: list[bool] = []
            for index in range(len(document)):
                page = document[index]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
                    page.close()
                flags.append(len("".join(text.split())) >= PDF_TEXT_LAYER_MIN_CHARS)
            return flags
        finally:
            document.close()
    except Exception:  # noqa: BLE001 - Docling reports unreadable PDFs itself
        return None


def _page_runs(flags: list[bool]) -> list[tuple[bool, list[int]]]:
    """Consecutive pages of the same kind: [(has_text_layer, 0-based indices)]."""
    runs: list[tuple[bool, list[int]]] = []
    for index, has_text in enumerate(flags):
        if runs and runs[-1][0] == has_text:
            runs[-1][1].append(index)
        else:
            runs.append((has_text, [index]))
    return runs


def _write_pdf_pages(source: Path, indices: list[int], target: Path) -> None:
    import pypdfium2

    original = pypdfium2.PdfDocument(str(source))
    subset = pypdfium2.PdfDocument.new()
    try:
        subset.import_pages(original, indices)
        subset.save(str(target))
    finally:
        subset.close()
        original.close()


def _convert_with_docling(source: Path, converter: Any) -> tuple[Any, int]:
    active_converter = converter or _get_converter()
    result = active_converter.convert(
        source,
        raises_on_error=True,
        max_num_pages=MAX_DOCUMENT_PAGES,
        max_file_size=MAX_DOCUMENT_BYTES,
    )
    document = getattr(result, "document", None)
    if document is None:
        raise DocumentParseError(
            "docling_conversion_failed", "Docling returned no document."
        )
    input_document = getattr(result, "input", None)
    return document, int(getattr(input_document, "page_count", 0) or 0)


def _parse_with_paddleocr_vl(
    source: Path, extension: str, *, converter: Any, client: Any
) -> tuple[Any, int, dict[str, Any]]:
    """Route an upload between PaddleOCR-VL and Docling's native PDF text.

    Images always go to PaddleOCR-VL. PDF pages with a real text layer stay
    on Docling: measured on Turkish documents, the text layer was exact while
    PaddleOCR-VL-1.6 lost ~6% of words to dotless-ı/ğ errors (Sonrası ->
    Sonrasi), so re-reading text that is already in the file only makes it
    worse. Pages without one (scans) go to PaddleOCR-VL, whose layout and
    table recognition beat Tesseract clearly. A mixed PDF is split into runs
    of consecutive pages and put back together in page order.

    Raises PaddleOcrVlError when the whole upload should fall back to Docling.
    """
    from paddleocr_vl import PaddleOcrVlError, parse_with_paddleocr_vl

    if extension != ".pdf":
        document, pages = parse_with_paddleocr_vl(source, is_pdf=False, client=client)
        return document, pages, {"engine": PADDLEOCR_VL_ENGINE, "paddleocr_vl_pages": [1]}

    flags = _pdf_text_layer_pages(source)
    if flags is None:
        raise PaddleOcrVlError("unreadable_pdf", "Could not read PDF pages.")
    if len(flags) > MAX_DOCUMENT_PAGES:
        # Docling enforces the page limit with its own error; sending an
        # oversized scan to the GPU first would only delay that.
        raise PaddleOcrVlError(
            "too_many_pages",
            f"{len(flags)} pages exceeds DOCLING_MAX_PAGES={MAX_DOCUMENT_PAGES}.",
        )
    scanned_pages = [number for number, has_text in enumerate(flags, 1) if not has_text]
    if not scanned_pages:
        document, pages = _convert_with_docling(source, converter)
        return document, pages, {"engine": "docling", "paddleocr_vl_pages": []}
    if len(scanned_pages) == len(flags):
        document, pages = parse_with_paddleocr_vl(
            source, is_pdf=True, expected_pages=len(flags), client=client
        )
        return document, pages, {
            "engine": PADDLEOCR_VL_ENGINE,
            "paddleocr_vl_pages": scanned_pages,
        }

    from docling_core.types.doc import DoclingDocument

    parts: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="pdf-pages-") as workdir:
        for run_index, (has_text, indices) in enumerate(_page_runs(flags)):
            part = Path(workdir) / f"run-{run_index}.pdf"
            _write_pdf_pages(source, indices, part)
            if has_text:
                document, _ = _convert_with_docling(part, converter)
            else:
                document, _ = parse_with_paddleocr_vl(
                    part, is_pdf=True, expected_pages=len(indices), client=client
                )
            # concatenate() numbers pages by position, so every part must
            # contribute exactly its own pages or later page numbers shift.
            if len(document.pages) != len(indices):
                raise PaddleOcrVlError(
                    "page_mismatch",
                    f"Pages {indices[0] + 1}-{indices[-1] + 1} produced "
                    f"{len(document.pages)} pages instead of {len(indices)}.",
                )
            parts.append(document)
    return DoclingDocument.concatenate(parts), len(flags), {
        "engine": "hybrid",
        "paddleocr_vl_pages": scanned_pages,
    }


def parse_document(
    source: Path,
    output_dir: Path,
    *,
    display_name: Optional[str] = None,
    converter: Any = None,
    paddle_client: Any = None,
) -> ParsedDocument:
    """Convert one local upload once and persist reusable Docling references.

    With OCR enabled and OCR_ENGINE=paddleocr_vl, images and PDF pages
    without a text layer are parsed by the PaddleOCR-VL service, and PDF
    pages with one keep Docling's native text (_parse_with_paddleocr_vl). If
    the service is unavailable or fails, the whole upload is parsed by
    Docling with OCR_FALLBACK_ENGINE instead.
    ``paddle_client`` is an optional httpx.Client for that service (tests).
    """
    source = Path(source)
    output_dir = Path(output_dir)
    extension = source.suffix.lower()
    if extension not in DOCLING_EXTENSIONS:
        raise DocumentParseError(
            "unsupported_document",
            f"Docling ingestion does not support extension {extension or '(none)'!r}.",
        )
    try:
        size_bytes = source.stat().st_size
    except OSError as exc:
        raise DocumentParseError("source_unavailable", str(exc)) from exc
    if size_bytes > MAX_DOCUMENT_BYTES:
        raise DocumentParseError(
            "document_too_large",
            f"Document is {size_bytes} bytes; maximum is {MAX_DOCUMENT_BYTES} bytes.",
        )
    ocr = ocr_settings() if extension in OCR_EXTENSIONS else None
    if extension in IMAGE_EXTENSIONS and not ocr["enabled"]:
        raise DocumentParseError(
            "ocr_disabled",
            "Reading text from images requires OCR. Set DOCLING_ENABLE_OCR=true.",
        )

    source_hash = _source_hash(source)
    manifest_path = output_dir / "manifest.json"
    content_path = output_dir / "content.md"
    canonical_path = output_dir / "canonical.json"
    if manifest_path.is_file() and content_path.is_file() and canonical_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            # A PDF parsed with OCR off (or another engine) must be reparsed
            # once the OCR configuration changes, not served from cache.
            # A fallback parse is not reused either: it exists only because
            # PaddleOCR-VL was down at the time, so the next parse retries it.
            if (
                manifest.get("source", {}).get("sha256") == source_hash
                and manifest.get("ocr") == ocr
                and not (manifest.get("parser") or {}).get("fallback_reason")
            ):
                return ParsedDocument(
                    output_dir=output_dir,
                    text=content_path.read_text(encoding="utf-8"),
                    manifest=manifest,
                    cached=True,
                )
        except (OSError, json.JSONDecodeError):
            pass

    temporary = output_dir.with_name(output_dir.name + ".building")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(mode=0o700, parents=True)
    try:
        document: Any = None
        page_count = 0
        parser_info: dict[str, Any] = {"engine": "docling", "fallback_reason": None}
        if ocr and ocr["enabled"] and ocr["engine"] == PADDLEOCR_VL_ENGINE:
            from paddleocr_vl import PaddleOcrVlError

            try:
                document, page_count, routed = _parse_with_paddleocr_vl(
                    source, extension, converter=converter, client=paddle_client
                )
                parser_info = {**routed, "fallback_reason": None}
            except PaddleOcrVlError as exc:
                _logger.warning(
                    "PaddleOCR-VL failed for %s (%s); falling back to Docling + %s",
                    display_name or source.name,
                    exc,
                    ocr["fallback_engine"],
                )
                parser_info = {"engine": "docling", "fallback_reason": exc.reason}

        if document is None:
            document, page_count = _convert_with_docling(source, converter)

        document_dict = document.export_to_dict()
        canonical = {
            "schema_version": 1,
            "engine": "docling",
            "source": {
                "filename": display_name or source.name,
                "extension": extension,
                "size_bytes": size_bytes,
                "sha256": source_hash,
            },
            "document": document_dict,
        }
        encoded = json.dumps(canonical, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_CANONICAL_BYTES:
            raise DocumentParseError(
                "structured_output_too_large",
                f"Canonical Docling output exceeds {MAX_CANONICAL_BYTES} bytes.",
            )
        (temporary / "canonical.json").write_bytes(encoded)

        markdown = document.export_to_markdown()
        (temporary / "content.md").write_text(markdown, encoding="utf-8")
        table_refs = _write_tables(document, temporary)
        chunk_count = _write_chunks(document, temporary / "chunks.jsonl")
        manifest = {
            "schema_version": 1,
            "engine": "docling",
            "source": canonical["source"],
            "content_path": "content.md",
            "canonical_path": "canonical.json",
            "chunks_path": "chunks.jsonl",
            "chunk_count": chunk_count,
            "page_count": page_count,
            "headings": _headings(document_dict),
            "tables": table_refs,
            "table_count": len(table_refs),
            "ocr": ocr,
            # Which parser actually produced this document. "engine" above
            # stays "docling": the stored format is a DoclingDocument either way.
            "parser": parser_info,
            "text_empty": not _has_visible_text(markdown),
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        temporary.replace(output_dir)
        return ParsedDocument(output_dir=output_dir, text=markdown, manifest=manifest)
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("docling_conversion_failed", str(exc)) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


__all__ = [
    "DOCLING_EXTENSIONS",
    "DocumentParseError",
    "IMAGE_EXTENSIONS",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_PAGES",
    "OCR_EXTENSIONS",
    "PADDLEOCR_VL_ENGINE",
    "ParsedDocument",
    "build_converter",
    "ocr_settings",
    "parse_document",
]
