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
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DOCLING_EXTENSIONS = frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm"})
MAX_DOCUMENT_BYTES = int(os.getenv("DOCLING_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
MAX_DOCUMENT_PAGES = max(1, min(int(os.getenv("DOCLING_MAX_PAGES", "500")), 2_000))
MAX_CANONICAL_BYTES = int(
    os.getenv("DOCLING_MAX_CANONICAL_BYTES", str(100 * 1024 * 1024))
)
MAX_STORED_TABLES = max(1, min(int(os.getenv("DOCLING_MAX_TABLES", "100")), 2_000))
MAX_STORED_CHUNKS = max(1, min(int(os.getenv("DOCLING_MAX_CHUNKS", "10000")), 50_000))
MAX_MANIFEST_HEADINGS = 500


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
        # Returning None is not enough: Docling reads DOCLING_ARTIFACTS_PATH
        # from the environment itself and then hard-fails validation on a
        # directory that does not exist, so every PDF/DOCX/PPTX upload died
        # with "docling_conversion_failed" instead of falling back. Clearing
        # it here is what actually restores the documented fallback.
        os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
        _logger.warning(
            "DOCLING_ARTIFACTS_PATH=%s is missing or empty; Docling will "
            "download models on first use (requires outbound network). Run "
            "'docling-tools models download --output-dir backend/vendor/docling-models' "
            "and rebuild the backend image for offline use.",
            configured,
        )
    return None


def _get_converter() -> Any:
    global _CONVERTER
    with _CONVERTER_LOCK:
        if _CONVERTER is None:
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import (
                DocumentConverter,
                InputFormat,
                PdfFormatOption,
            )

            # OCR is intentionally opt-in. It can trigger implicit runtime model
            # downloads and is unnecessary for ordinary text-bearing PDFs. The
            # standard Docling layout/table pipeline remains the primary parser.
            artifacts_path = _resolve_artifacts_path()
            pdf_options = PdfPipelineOptions(
                do_ocr=os.getenv("DOCLING_ENABLE_OCR", "false").lower() == "true",
                artifacts_path=artifacts_path,
            )

            _CONVERTER = DocumentConverter(
                allowed_formats=[
                    InputFormat.PDF,
                    InputFormat.DOCX,
                    InputFormat.PPTX,
                    InputFormat.XLSX,
                    InputFormat.HTML,
                ],
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
                },
            )
        return _CONVERTER


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


def parse_document(
    source: Path,
    output_dir: Path,
    *,
    display_name: Optional[str] = None,
    converter: Any = None,
) -> ParsedDocument:
    """Convert one local upload once and persist reusable Docling references."""
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

    source_hash = _source_hash(source)
    manifest_path = output_dir / "manifest.json"
    content_path = output_dir / "content.md"
    canonical_path = output_dir / "canonical.json"
    if manifest_path.is_file() and content_path.is_file() and canonical_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("source", {}).get("sha256") == source_hash:
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
        input_document = getattr(result, "input", None)
        page_count = int(getattr(input_document, "page_count", 0) or 0)
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
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_PAGES",
    "ParsedDocument",
    "parse_document",
]
