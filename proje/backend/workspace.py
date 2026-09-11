"""Safe, read-only uploaded workspaces for one bounded agent job."""

from __future__ import annotations

import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from document_ingestion import DOCLING_EXTENSIONS

WORKSPACE_TEXT_EXTENSIONS = {
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".jsx",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".scss",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    ".vite",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
    "venv",
}
IGNORED_FILE_NAMES = {
    ".ds_store",
    "thumbs.db",
}

MAX_UPLOAD_SOURCES = 10
MAX_BATCH_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 1000
MAX_WORKSPACE_FILES = 200
MAX_WORKSPACE_FILE_BYTES = 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 10 * 1024 * 1024
MAX_TOOL_READ_CHARS = 20_000
MAX_TOOL_SEARCH_MATCHES = 50
MAX_CONTEXT_CHARS = 60_000
MAX_CONTEXT_FILE_CHARS = 8_000

# Files with a structured-access path (inspect_table/read_table_rows/
# search_table/table_summary) instead of flattened-text traversal.
TABLE_EXTENSIONS = {".csv", ".xlsx"}

# Machine-produced intermediates: <workspace>/artifacts/extracted/. Mirrors
# artifacts.ARTIFACTS_DIRNAME / artifacts.INTERNAL_DIRNAME as literals
# because artifacts.py imports THIS module -- importing it back would be a
# cycle. The two are asserted equal in test_workspace_extracted_guard.
#
# Why these files get their own rule: web acquisition writes the COMPLETE
# scraped dataset here precisely so the rows never have to pass through a
# model response, and hands back only the path plus a 25-row preview.
# Reading the file with read_text_file undoes that in one call -- a live run
# paged a year of extracted rows back into context and spent ~412K input
# tokens producing a spreadsheet that the saved path could have produced
# directly. There is never a reason to read one: the preview shows the
# shape, and every consumer (create_xlsx_artifact(from_path=...),
# create_csv_artifact(from_path=...), create_web_bundle(data_path=...),
# convert_table_to_json, run_python) takes the path.
_EXTRACTED_PREFIX = "artifacts/extracted/"


def _is_extracted_intermediate(relative_path: str) -> bool:
    normalized = str(relative_path or "").replace("\\", "/").lstrip("./")
    return normalized.lower().startswith(_EXTRACTED_PREFIX)


def _extracted_redirect(relative_path: str) -> str:
    return (
        f"[{relative_path} is machine-extracted intermediate data, and its "
        "contents are deliberately not readable here -- reading it would "
        "pull the whole dataset into this conversation for no benefit.\n"
        "It is ALREADY a JSON array of records on disk. Pass the path "
        "directly to the tool that consumes it:\n"
        "  create_xlsx_artifact(filename=..., from_path=\"" + relative_path + "\")\n"
        "  create_csv_artifact(filename=..., from_path=\"" + relative_path + "\")\n"
        "  create_web_bundle(..., data_path=\"" + relative_path + "\")\n"
        "  execute_code(...) if it needs reshaping first\n"
        "The web_read response that produced it already showed you the "
        "columns and a sample row. Do not retry any read tool on this path.]"
    )
# Docling-backed uploads get a compatibility `.extracted.txt` companion plus
# a lossless `.docling/` reference directory. XLSX is classified as a table
# first so the established structured table tools remain its primary surface.
DOCUMENT_EXTRACT_EXTENSIONS = set(DOCLING_EXTENSIONS)

MAX_TABLE_ROWS_PER_READ = 200
MAX_TABLE_SEARCH_MATCHES = 50
MAX_TABLE_COLUMNS_PER_READ = 100
MAX_TABLE_CELL_CHARS = 2_000
MAX_TABLE_SUMMARY_COLUMNS = 50
MAX_TABLE_COMPARE_SAMPLE_ROWS = 20
# Defensive backstop only: a standalone (non-ZIP) upload is already capped
# at main.py's MAX_UPLOAD_BYTES (20 MB) before it ever reaches a workspace,
# and a ZIP member is capped at MAX_WORKSPACE_FILE_BYTES (1 MB) above. This
# guards the table tools specifically against a future caller of
# build_job_workspace that skips those checks, so a pandas parse can never
# be handed an unbounded file.
MAX_TABLE_SOURCE_BYTES = 20 * 1024 * 1024

# Per-run (not per-call) hard ceilings on raw record-level data delivered
# into model context. A per-call bound (MAX_TABLE_ROWS_PER_READ,
# MAX_TOOL_READ_CHARS) stops any ONE call from being huge, but a model can
# still page sequentially through many small, individually-legal calls
# until most of a large dataset has been copied into context -- that is
# exactly the amplification observed traversing two ~130KB CSVs via bounded
# read_table_rows calls (342K Document input tokens across 19 calls). These
# totals are tracked per ReadOnlyWorkspaceTools instance, which is built
# once per job and shared across every worker/subtask/attempt (see
# camel_service.py's _workspace_tools()), so the ceiling is genuinely
# per-run, not per-call or per-agent. Once reached, the affected tool
# returns a clear, bounded "budget exhausted" result instead of raising --
# table_summary/compare_tables/inspect_table remain fully available
# afterward, since they were never the source of the amplification.
MAX_TABLE_ROWS_PER_RUN = 1_000
MAX_TEXT_CHARS_PER_RUN = 100_000

# Sentinel strings a model may emit for an omitted optional argument
# instead of leaving it out of the call or passing JSON null -- e.g.
# `sheet="None"` (Python's None repr leaking into a JSON tool-call
# argument). Observed in production as two hard errors on CSV calls that
# should have just used the file's single implicit sheet.
_NONE_LIKE_STRINGS = {"", "none", "null", "nil", "n/a", "na"}


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    """Treat common "no value" sentinels the same as an omitted argument."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NONE_LIKE_STRINGS else text


class WorkspaceSecurityError(ValueError):
    """Raised when an archive or tool path violates workspace boundaries."""


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    size_bytes: int


@dataclass(frozen=True)
class ZipInspection:
    files: list[ArchiveMember]
    ignored_file_count: int


def _portable_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith(("/", "\\"))
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise WorkspaceSecurityError(
            f"Unsafe workspace path: {value!r}"
        )
    return path


def safe_filename(value: str, fallback: str = "upload") -> str:
    """Return a display filename that cannot create a nested path."""
    normalized = (value or "").replace("\\", "/")
    name = PurePosixPath(normalized).name.strip()
    if name in {"", ".", ".."}:
        return fallback
    return re.sub(r"[\x00-\x1f]", "_", name)


def _is_ignored(path: PurePosixPath) -> bool:
    parts = [part.lower() for part in path.parts]
    return (
        any(part in IGNORED_DIRECTORY_NAMES for part in parts[:-1])
        or path.name.lower() in IGNORED_FILE_NAMES
        or path.suffix.lower() not in WORKSPACE_TEXT_EXTENSIONS
    )


def _looks_binary(content: bytes) -> bool:
    if b"\x00" in content[:8192]:
        return True
    sample = content[:8192]
    if not sample:
        return False
    control = sum(
        byte < 32 and byte not in {9, 10, 12, 13} for byte in sample
    )
    return control / len(sample) > 0.20


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def inspect_zip(path: Path) -> ZipInspection:
    """Validate a ZIP and return only bounded, supported text members."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WorkspaceSecurityError(f"Invalid ZIP archive: {exc}") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise WorkspaceSecurityError(
                f"ZIP contains {len(infos)} entries; maximum is "
                f"{MAX_ZIP_ENTRIES}"
            )

        files: list[ArchiveMember] = []
        ignored = 0
        total_size = 0
        seen: set[str] = set()

        for info in infos:
            member_path = _portable_path(info.filename)
            if _is_zip_symlink(info):
                raise WorkspaceSecurityError(
                    f"ZIP symlinks are not allowed: {info.filename}"
                )
            if info.flag_bits & 0x1:
                raise WorkspaceSecurityError(
                    f"Encrypted ZIP entries are not supported: {info.filename}"
                )
            if info.is_dir():
                continue
            if _is_ignored(member_path):
                ignored += 1
                continue

            normalized = member_path.as_posix()
            casefolded = normalized.casefold()
            if casefolded in seen:
                raise WorkspaceSecurityError(
                    f"ZIP contains a duplicate path: {normalized}"
                )
            seen.add(casefolded)

            if info.file_size > MAX_WORKSPACE_FILE_BYTES:
                raise WorkspaceSecurityError(
                    f"ZIP member {normalized!r} exceeds the "
                    f"{MAX_WORKSPACE_FILE_BYTES}-byte individual limit"
                )
            if len(files) >= MAX_WORKSPACE_FILES:
                raise WorkspaceSecurityError(
                    f"ZIP exceeds the {MAX_WORKSPACE_FILES}-file limit"
                )

            content = archive.read(info)
            if len(content) > MAX_WORKSPACE_FILE_BYTES:
                raise WorkspaceSecurityError(
                    f"ZIP member {normalized!r} exceeds the "
                    f"{MAX_WORKSPACE_FILE_BYTES}-byte individual limit"
                )
            if _looks_binary(content):
                ignored += 1
                continue

            total_size += len(content)
            if total_size > MAX_WORKSPACE_TOTAL_BYTES:
                raise WorkspaceSecurityError(
                    "ZIP exceeds the "
                    f"{MAX_WORKSPACE_TOTAL_BYTES}-byte extracted total limit"
                )
            files.append(
                ArchiveMember(path=normalized, size_bytes=len(content))
            )

    return ZipInspection(files=files, ignored_file_count=ignored)


def _safe_child(root: Path, relative_path: str, *, must_exist: bool) -> Path:
    portable = _portable_path(relative_path)
    root_resolved = root.resolve(strict=True)
    candidate = root.joinpath(*portable.parts)
    resolved = candidate.resolve(strict=must_exist)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise WorkspaceSecurityError(
            f"Path is outside the uploaded workspace: {relative_path!r}"
        ) from exc

    current = candidate
    while current != root:
        if current.is_symlink():
            raise WorkspaceSecurityError(
                f"Workspace symlinks are not allowed: {relative_path!r}"
            )
        current = current.parent
    return resolved


def _source_directory(file_id: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9-]", "", file_id)[:12]
    return compact or "source"


def _extracted_companion(path: Path) -> Optional[Path]:
    """The `<name>.extracted.txt` sibling for a binary source file, if any."""
    companion = path.parent / f"{path.name}.extracted.txt"
    return companion if companion.is_file() else None


def _docling_internal_kind(relative_path: str) -> Optional[str]:
    parts = PurePosixPath(str(relative_path).replace("\\", "/")).parts
    if not any(part.lower().endswith(".docling") for part in parts[:-1]):
        return None
    name = parts[-1].lower() if parts else ""
    if name == "content.md":
        return "content"
    if name == "manifest.json":
        return "manifest"
    if name in {"canonical.json", "chunks.jsonl"}:
        return "reference"
    return "table" if name.endswith(".csv") else "reference"


def classify_file(root: Path, path: Path) -> dict[str, Any]:
    """Describe one workspace file: type, size, and which tools support it.

    This is the single place a file's capability is decided -- list_files,
    inspect_file, and the initial workspace-context preview (see
    build_workspace_context) all call it, so they cannot disagree about
    what a given path supports. That matters here specifically: an earlier
    version of this codebase let generic wording ("research", "reads text
    files") imply broader capability than a tool actually had, which is
    what caused agents to keep re-attempting reads that could never
    succeed. Classification lives in exactly one function so there is only
    one place to get it right.
    """
    # Both sides are resolved before comparing. Callers reach this function
    # with paths of two different provenances -- rglob results, which keep
    # whatever form `root` was given in, and _safe_child results, which are
    # always fully resolved -- and a `root` containing a symlinked component
    # makes those two disagree even though they name the same file. On macOS
    # every /var path is such a case (/var -> /private/var), which is where
    # this surfaced; a symlinked DATA_DIR would do the same in a container.
    # Comparing resolved-to-resolved is correct regardless of which caller
    # this is and does not weaken anything: containment is still enforced by
    # _safe_child, and relative_to still raises for a genuine escape.
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    relative = resolved_path.relative_to(resolved_root).as_posix()
    size_bytes = path.stat().st_size
    name = path.name.lower()
    ext = path.suffix.lower()
    notes: list[str] = []
    if size_bytes == 0:
        notes.append("This file is empty (0 bytes).")

    docling_kind = _docling_internal_kind(relative)
    if docling_kind == "reference":
        return {
            "path": relative,
            "size_bytes": size_bytes,
            "file_type": "structured_document_reference",
            "representations": ["docling"],
            "supported_operations": ["inspect_file"],
            "recommended_operations": [],
            "notes": notes + [
                "This is a server-side Docling reference and is deliberately "
                "not copied into model context. Use the sibling content.md, "
                "manifest.json, or tables/*.csv references instead."
            ],
        }
    if docling_kind == "content":
        return {
            "path": relative,
            "size_bytes": size_bytes,
            "file_type": "extracted_document_text",
            "representations": ["docling_markdown"],
            "supported_operations": ["read_text_file", "search_text"],
            "recommended_operations": [f"read_text_file({relative!r})"],
            "notes": notes,
        }

    if name.endswith(".extracted.txt"):
        parent_name = path.name[: -len(".extracted.txt")]
        parent_ext = Path(parent_name).suffix.lower()
        if parent_ext in TABLE_EXTENSIONS:
            parent_relative = (
                (path.parent / parent_name).relative_to(root).as_posix()
            )
            return {
                "path": relative,
                "size_bytes": size_bytes,
                "file_type": "table_extract_companion",
                "representations": ["raw"],
                "supported_operations": ["read_text_file", "search_text"],
                "recommended_operations": [
                    f"inspect_table({parent_relative!r})"
                ],
                "notes": notes + [
                    "This is a flattened text dump of a table file. Prefer "
                    f"inspect_table/read_table_rows/search_table on "
                    f"{parent_relative!r} instead -- it returns structured, "
                    "bounded rows instead of re-paginating this whole dump."
                ],
            }
        return {
            "path": relative,
            "size_bytes": size_bytes,
            "file_type": "extracted_document_text",
            "representations": ["extracted_text"],
            "supported_operations": ["read_text_file", "search_text"],
            "recommended_operations": [f"read_text_file({relative!r})"],
            "notes": notes,
        }

    if ext in TABLE_EXTENSIONS:
        supported = [
            "inspect_table", "read_table_rows", "search_table", "table_summary",
        ]
        if ext in WORKSPACE_TEXT_EXTENSIONS:
            supported += ["read_text_file", "search_text"]
        return {
            "path": relative,
            "size_bytes": size_bytes,
            "file_type": "table",
            "representations": ["raw"],
            "supported_operations": supported + ["compare_tables"],
            "recommended_operations": [
                f"inspect_table({relative!r})",
                "table_summary (stats) or compare_tables (vs. another "
                "table file) for analysis",
            ],
            "notes": notes + [
                "Prefer the table tools over read_text_file/search_text for "
                "this file. Use table_summary/compare_tables for analysis "
                "questions; reserve read_table_rows/search_table for a "
                "specific, already-narrowed drill-down -- a shared per-run "
                "row budget caps how many raw rows can be read in total."
            ],
        }

    if ext in DOCUMENT_EXTRACT_EXTENSIONS:
        companion = _extracted_companion(path)
        if companion is not None:
            companion_relative = companion.relative_to(root).as_posix()
            return {
                "path": relative,
                "size_bytes": size_bytes,
                "file_type": "document",
                "representations": ["extracted_text"],
                "extracted_text_path": companion_relative,
                "supported_operations": ["read_text_file", "search_text"],
                "recommended_operations": [
                    f"read_text_file({companion_relative!r})"
                ],
                "notes": notes + [
                    f"The raw {ext} file itself cannot be read directly; "
                    f"use the extracted text at {companion_relative!r} "
                    "instead."
                ],
            }
        return {
            "path": relative,
            "size_bytes": size_bytes,
            "file_type": "unsupported",
            "representations": [],
            "supported_operations": [],
            "notes": notes + [
                f"No extracted text is available for this {ext} file. It "
                "cannot be read by any tool in this workspace. Do not "
                "retry -- skip this file or ask for a different format."
            ],
        }

    if ext in WORKSPACE_TEXT_EXTENSIONS:
        return {
            "path": relative,
            "size_bytes": size_bytes,
            "file_type": "text",
            "representations": ["raw"],
            "supported_operations": ["read_text_file", "search_text"],
            "recommended_operations": [f"read_text_file({relative!r})"],
            "notes": notes,
        }

    return {
        "path": relative,
        "size_bytes": size_bytes,
        "file_type": "unsupported",
        "representations": [],
        "supported_operations": [],
        "notes": notes + [
            f"No reader tool supports {ext or '(no extension)'} files in "
            "this workspace. Do not retry any read tool on this path -- "
            "skip it or ask for a different format."
        ],
    }


def _json_safe_cell(value: Any) -> Any:
    """Coerce one pandas/numpy cell value into a small, JSON-safe value.

    Bounds string length here too (MAX_TABLE_CELL_CHARS) -- a single
    pathological cell (a huge embedded blob, a wall of repeated text)
    should not be able to blow up an otherwise row-bounded tool response.
    """
    import pandas as pd

    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (int, bool, float)):
        return value
    text = str(value)
    if len(text) > MAX_TABLE_CELL_CHARS:
        return text[:MAX_TABLE_CELL_CHARS] + f"...[truncated, {len(text)} chars total]"
    return text


def ensure_job_workspace(job_id: str, workspace_root: Path) -> Path:
    """Create (or return) an empty isolated workspace for a job with no uploads.

    A workspace used to exist only for jobs that HAD attachments, because its
    only purpose was holding them. That is no longer true: a job whose
    deliverable is a file needs somewhere to write it, and a job that runs
    code needs somewhere to run it -- both with the same per-job isolation and
    the same 0o700 confinement that uploads get.

    Same identifier validation and same directory mode as
    build_job_workspace, deliberately: this creates the identical kind of
    directory, just without copying anything into it. Idempotent, so a caller
    does not have to know whether uploads already created it.
    """
    if not re.fullmatch(r"[a-zA-Z0-9-]+", job_id):
        raise WorkspaceSecurityError("Invalid job workspace identifier")
    workspace_root.mkdir(parents=True, exist_ok=True)
    root = workspace_root / job_id
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def build_job_workspace(
    job_id: str,
    upload_records: list[dict[str, Any]],
    workspace_root: Path,
) -> tuple[dict[str, Any], str]:
    """Copy validated uploads into a new job-isolated workspace."""
    if not re.fullmatch(r"[a-zA-Z0-9-]+", job_id):
        raise WorkspaceSecurityError("Invalid job workspace identifier")
    workspace_root.mkdir(parents=True, exist_ok=True)
    root = workspace_root / job_id
    if root.exists():
        raise WorkspaceSecurityError("Job workspace already exists")
    root.mkdir(mode=0o700)

    files: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    ignored_total = 0
    archive_file_count = 0
    archive_total_bytes = 0

    try:
        for record in upload_records:
            file_id = str(record["file_id"])
            filename = safe_filename(str(record["filename"]))
            source_path = Path(str(record["path"]))
            if not source_path.is_file() or source_path.is_symlink():
                raise WorkspaceSecurityError(
                    f"Uploaded source is unavailable: {filename}"
                )

            source_dir = _source_directory(file_id)
            kind = str(record.get("kind") or "file")
            source_files: list[dict[str, Any]] = []
            ignored = int(record.get("ignored_file_count") or 0)

            if kind == "zip":
                inspection = inspect_zip(source_path)
                ignored = inspection.ignored_file_count
                archive_file_count += len(inspection.files)
                archive_total_bytes += sum(
                    member.size_bytes for member in inspection.files
                )
                if archive_file_count > MAX_WORKSPACE_FILES:
                    raise WorkspaceSecurityError(
                        "Combined ZIP sources exceed the "
                        f"{MAX_WORKSPACE_FILES}-file limit"
                    )
                if archive_total_bytes > MAX_WORKSPACE_TOTAL_BYTES:
                    raise WorkspaceSecurityError(
                        "Combined ZIP sources exceed the "
                        f"{MAX_WORKSPACE_TOTAL_BYTES}-byte extracted limit"
                    )
                archive_root = safe_filename(
                    Path(filename).stem, fallback="workspace"
                )
                with zipfile.ZipFile(source_path) as archive:
                    infos = {
                        _portable_path(info.filename).as_posix(): info
                        for info in archive.infolist()
                        if not info.is_dir()
                    }
                    for member in inspection.files:
                        relative = (
                            PurePosixPath("sources")
                            / source_dir
                            / archive_root
                            / member.path
                        ).as_posix()
                        target = _safe_child(
                            root, relative, must_exist=False
                        )
                        target.parent.mkdir(parents=True, exist_ok=True)
                        content = archive.read(infos[member.path])
                        target.write_bytes(content)
                        summary = {
                            "path": relative,
                            "source_id": file_id,
                            "source_filename": filename,
                            "size_bytes": len(content),
                        }
                        files.append(summary)
                        source_files.append(summary)
            else:
                relative = (
                    PurePosixPath("sources") / source_dir / filename
                ).as_posix()
                target = _safe_child(root, relative, must_exist=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
                summary = {
                    "path": relative,
                    "source_id": file_id,
                    "source_filename": filename,
                    "size_bytes": target.stat().st_size,
                }
                extracted_text = record.get("extracted_text")
                if isinstance(extracted_text, str) and extracted_text.strip():
                    # Keep the original upload untouched, but materialize the
                    # extractor output beside it so job-scoped read/search
                    # tools can inspect the complete text of binary documents.
                    extracted_relative = f"{relative}.extracted.txt"
                    extracted_target = _safe_child(
                        root, extracted_relative, must_exist=False
                    )
                    extracted_target.write_text(extracted_text, encoding="utf-8")
                    summary["extracted_text_path"] = extracted_relative
                docling_value = record.get("docling_dir")
                docling_source = Path(str(docling_value)) if docling_value else None
                if (
                    docling_source is not None
                    and docling_source.is_dir()
                    and not docling_source.is_symlink()
                ):
                    structured_relative = f"{relative}.docling"
                    structured_target = _safe_child(
                        root, structured_relative, must_exist=False
                    )
                    shutil.copytree(docling_source, structured_target)
                    summary["docling_path"] = structured_relative
                    summary["docling_manifest_path"] = (
                        f"{structured_relative}/manifest.json"
                    )
                    summary["docling_content_path"] = (
                        f"{structured_relative}/content.md"
                    )
                files.append(summary)
                source_files.append(summary)

            ignored_total += ignored
            attachments.append(
                {
                    "file_id": file_id,
                    "filename": filename,
                    "kind": kind,
                    "file_count": len(source_files),
                    "total_size_bytes": sum(
                        item["size_bytes"] for item in source_files
                    ),
                    "ignored_file_count": ignored,
                }
            )

        summary = {
            "file_count": len(files),
            "total_size_bytes": sum(item["size_bytes"] for item in files),
            "ignored_file_count": ignored_total,
            "files": files,
            "limits": {
                "max_zip_files": MAX_WORKSPACE_FILES,
                "max_zip_individual_file_bytes": MAX_WORKSPACE_FILE_BYTES,
                "max_zip_total_bytes": MAX_WORKSPACE_TOTAL_BYTES,
                "max_upload_sources": MAX_UPLOAD_SOURCES,
                "max_combined_upload_bytes": MAX_BATCH_UPLOAD_BYTES,
            },
        }
        return {
            "root": str(root),
            "summary": summary,
            "attachments": attachments,
        }, build_workspace_context(root, summary, upload_records)
    except Exception:
        resolved_root = root.resolve()
        if resolved_root.parent == workspace_root.resolve() and root.exists():
            shutil.rmtree(root)
        raise


def build_workspace_context(
    root: Path,
    summary: dict[str, Any],
    upload_records: list[dict[str, Any]],
) -> str:
    """Create a bounded context with explicit source and path boundaries."""
    record_by_id = {
        str(record["file_id"]): record for record in upload_records
    }
    lines = [
        "UPLOADED WORKSPACE (read-only)",
        (
            f"{summary['file_count']} files, "
            f"{summary['total_size_bytes']} bytes."
        ),
        "Relative paths are preserved below. In Workforce runs, agents can "
        "use the job-scoped workspace tools for deeper text inspection.",
    ]
    remaining = MAX_CONTEXT_CHARS - sum(len(line) for line in lines)

    for item in summary["files"]:
        boundary = (
            "\n\n"
            f"=== SOURCE: {item['source_filename']} "
            f"(id: {item['source_id']}) ===\n"
            f"--- WORKSPACE PATH: {item['path']} ---\n"
        )
        if item.get("extracted_text_path"):
            boundary += (
                "--- EXTRACTED TEXT PATH: "
                f"{item['extracted_text_path']} ---\n"
            )
        if item.get("docling_path"):
            boundary += (
                "--- DOCLING STRUCTURED REFERENCE: "
                f"{item['docling_path']} ---\n"
                "--- DOCLING MANIFEST: "
                f"{item['docling_manifest_path']} ---\n"
            )
        if len(boundary) >= remaining:
            break
        lines.append(boundary)
        remaining -= len(boundary)

        path = _safe_child(root, item["path"], must_exist=True)
        record = record_by_id.get(item["source_id"], {})
        ext = path.suffix.lower()
        if ext in TABLE_EXTENSIONS:
            # Steer away from the flattened extracted-text dump from the
            # very first prompt an agent sees, before any tool call happens
            # -- the initial preview is what previously taught agents to
            # reach for read_text_file on a large .csv/.xlsx.extracted.txt
            # in the first place. The table tools give structured, bounded
            # access instead, so there is no reason to ever traverse that
            # dump for a table file.
            content = (
                "[Table file. Call inspect_table("
                f"{item['path']!r}) to see sheet names, columns, and row "
                "count, then table_summary (stats) or compare_tables (if "
                "comparing this to another table file); use read_table_rows"
                "/search_table only for a specific, already-narrowed "
                "drill-down, not to page through the whole table. Do not "
                "read the .extracted.txt companion for this file."
                "]"
            )
        elif (
            record.get("kind") != "zip"
            and isinstance(record.get("extracted_text"), str)
        ):
            content = record["extracted_text"]
        elif ext in WORKSPACE_TEXT_EXTENSIONS:
            content = path.read_text(encoding="utf-8", errors="replace")
        else:
            descriptor = classify_file(root, path)
            content = "[" + " ".join(
                descriptor.get("notes")
                or ["Binary document; extracted text is unavailable."]
            ) + "]"

        allowed = min(MAX_CONTEXT_FILE_CHARS, max(0, remaining))
        preview = content[:allowed]
        if len(content) > allowed and ext not in TABLE_EXTENSIONS:
            preview += "\n[File preview truncated; use read_text_file.]"
        lines.append(preview)
        remaining -= len(preview)
        if remaining <= 0:
            lines.append("\n[Workspace context truncated at bounded limit.]")
            break

    return "".join(lines)


class _CachedFailure:
    """A memoized tool call's exception, so a repeat re-raises instead of
    re-executing -- see ReadOnlyWorkspaceTools._memoize.

    Without this, an exception bypassed the memo cache entirely (only the
    success path wrote to it), so a call that fails deterministically (e.g.
    read_table_rows on artifacts/extracted/... -- see _table_path) reran
    its full body and re-raised the identical error on every repeat: no
    cheaper, and no more informative, the second time than the first.
    """

    __slots__ = ("exc_type", "message")

    def __init__(self, exc_type: type[BaseException], message: str) -> None:
        self.exc_type = exc_type
        self.message = message

    def amplified_message(self, name: str, prior: int) -> str:
        return (
            f"[Repeat blocked: {name} with these exact arguments already "
            f"failed {prior} time(s) earlier in this run with the error "
            f"below, and was NOT re-executed. Retrying will not change the "
            "outcome -- follow the instructions in that error instead of "
            f"calling {name} again on this path.]\n{self.message}"
        )


class ReadOnlyWorkspaceTools:
    """Capability-bounded, read-only operations exposed to job workers.

    Two families: generic text tools (list_files/inspect_file/
    read_text_file/search_text) and structured tabular tools
    (inspect_table/read_table_rows/search_table/table_summary/
    compare_tables). Every tool is bounded (rows, characters, matches),
    and every one of them is memoized per exact (tool, arguments) pair for
    the life of this instance -- see _install_memoized_tools.

    That memoization is not just a duplicate-call *warning*: a confirmed
    production issue had CAMEL's DIRECT/streaming tool-calling path
    occasionally dispatch one logical tool call twice with byte-identical
    arguments (every distinct call in an affected run executed exactly
    2x -- see run_single_agent). Rather than patch CAMEL's internal
    streaming accumulator, the guard lives here, at the one seam every
    tool call from every execution path (DIRECT, Workforce, manual
    workers) already passes through: an exact repeat -- whichever call
    site it comes from, and whether it's a genuine duplicate dispatch or a
    model legitimately re-asking for the same data -- always returns
    byte-identical data anyway (every read tool here documents that its
    own outputs are a pure function of the file plus arguments), so
    returning the memoized result instead of recomputing is correct in
    both cases, not merely a workaround for the one bug that motivated it.
    Real work (the file read, the pandas parse, the budget bookkeeping)
    only ever happens on the first call for a given key; a repeat still
    reaches the wrapped function object logged_tool wraps, so it is still
    fully recorded -- only the underlying computation is skipped.
    """

    # Tools whose response gets an explicit "you already made this call"
    # note appended on a memoized repeat -- the ones with a per-run budget,
    # where a model silently getting the same answer again (with no signal
    # that repeating won't help) is the exact behavior earlier turns added
    # duplicate-call detection to prevent. The remaining tools
    # (list_files/inspect_file/search_text/inspect_table/table_summary/
    # compare_tables) are cheap and side-effect-free, so a repeat there is
    # not a loop worth flagging -- a memoized repeat of those returns the
    # prior result completely unchanged.
    _NOTE_ON_REPEAT = frozenset({"read_text_file", "read_table_rows", "search_table"})

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        # (tool_name, args, kwargs_json) -> count of dispatches seen so far
        # this run, and -> the first dispatch's result. The count is a soft
        # signal (used only to phrase the repeat note); the cache is what
        # actually prevents a repeat from re-executing. One instance is
        # shared by every worker/subtask for the whole job (see
        # camel_service.py's _workspace_tools()), so both are genuinely
        # per-run, not per-call or per-agent.
        self._call_counts: dict[tuple, int] = {}
        self._call_cache: dict[tuple, Any] = {}
        # Hard per-run ceilings -- once these are spent the affected tool
        # stops returning raw data at all, for the rest of this instance's
        # life.
        self._table_rows_delivered = 0
        self._text_chars_delivered = 0
        self._install_memoized_tools()

    def _install_memoized_tools(self) -> None:
        """Shadow each public tool method with a memoized wrapper.

        Assigning onto the instance (rather than only inside as_tools())
        means EVERY caller -- as_tools()'s returned list, and a direct
        `tools.read_table_rows(...)` call alike -- goes through the same
        memoization, so there is exactly one code path to reason about.
        `@wraps` preserves `__name__`/`__doc__`/`__wrapped__` on each
        wrapper, which matters beyond readability: CAMEL's FunctionTool
        builds the JSON schema shown to the model from
        `inspect.signature(func)` and `func.__doc__`, and
        `inspect.signature` follows `__wrapped__` automatically -- without
        `@wraps` the model would see `(*args, **kwargs)` instead of each
        tool's real, documented parameters.
        """
        for name in (
            "list_files", "inspect_file", "read_text_file", "search_text",
            "inspect_table", "read_table_rows", "search_table",
            "table_summary", "compare_tables",
        ):
            setattr(self, name, self._memoize(name, getattr(self, name)))

    def _memoize(self, name: str, func: Any) -> Any:
        attach_note = name in self._NOTE_ON_REPEAT

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                key = (name, args, json.dumps(kwargs, sort_keys=True, default=str))
            except TypeError:
                return func(*args, **kwargs)

            prior = self._call_counts.get(key, 0)
            self._call_counts[key] = prior + 1
            if key in self._call_cache:
                cached = self._call_cache[key]
                if isinstance(cached, _CachedFailure):
                    # A failing call (e.g. read_table_rows on
                    # artifacts/extracted/... -- see _table_path) must not
                    # re-execute and re-raise the identical error forever:
                    # the original message already told the model what to
                    # do instead, and a repeat means that instruction was
                    # not followed, not that trying again will help. Raise
                    # the SAME exception type with an amplified, harder to
                    # ignore message rather than silently succeeding or
                    # looping.
                    raise cached.exc_type(cached.amplified_message(name, prior))
                if not attach_note:
                    return cached
                return self._with_repeat_note(name, cached, prior)

            try:
                result = func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - cache and re-raise as-is
                self._call_cache[key] = _CachedFailure(type(exc), str(exc))
                raise
            self._call_cache[key] = result
            return result

        return wrapper

    @staticmethod
    def _with_repeat_note(name: str, cached: Any, prior: int) -> Any:
        note = (
            f"[Note: this exact {name} call (identical arguments) was "
            f"already made {prior} time(s) earlier in this run and "
            "returned the same result -- it was not re-executed. Repeating "
            "it again will not reveal new information; advance an "
            "argument (e.g. offset) or use a different tool instead.]"
        )
        if isinstance(cached, str):
            return note + "\n" + cached
        if isinstance(cached, dict):
            copy = dict(cached)
            existing = copy.get("note")
            copy["note"] = f"{note} {existing}" if existing else note
            return copy
        return cached

    def _text_files(self) -> list[Path]:
        files: list[Path] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.resolve(strict=True).relative_to(self.root)
            except ValueError:
                continue
            if path.suffix.lower() in WORKSPACE_TEXT_EXTENSIONS:
                relative = path.relative_to(self.root).as_posix()
                if _docling_internal_kind(relative) == "reference":
                    continue
                files.append(path)
        return files[:MAX_WORKSPACE_FILES]

    def _all_files(self) -> list[Path]:
        files: list[Path] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.resolve(strict=True).relative_to(self.root)
            except ValueError:
                continue
            files.append(path)
        return files[:MAX_WORKSPACE_FILES]

    def list_files(self) -> dict[str, Any]:
        """List every file in the workspace with its capability type.

        Call this before reading anything unfamiliar: `file_type` says
        whether a file is directly readable ("text"), needs the table
        tools ("table"), only has an extracted-text representation
        ("document"/"extracted_document_text"), or has no reader at all
        ("unsupported") -- so you know which tool applies, or that none
        does, before spending a call finding out.
        """
        files = [classify_file(self.root, path) for path in self._all_files()]
        return {"file_count": len(files), "files": files}

    def inspect_file(self, relative_path: str) -> dict[str, Any]:
        """Report one file's type, size, and which tools actually support it.

        Same classification list_files uses, for a single path -- use this
        when you already know the path and just need its capability
        details (recommended_operations, notes) without listing everything.
        """
        path = _safe_child(self.root, relative_path, must_exist=True)
        if not path.is_file():
            raise WorkspaceSecurityError(
                f"Workspace path is not a file: {relative_path!r}"
            )
        return classify_file(self.root, path)

    def read_text_file(
        self,
        relative_path: str,
        offset: int = 0,
        max_chars: int = MAX_TOOL_READ_CHARS,
    ) -> str:
        """Read one page of a selected text file in the uploaded workspace.

        Each call returns at most `max_chars` characters starting at
        `offset` (both bounded by MAX_TOOL_READ_CHARS). Large files are NOT
        returned in one call: when a page ends before the file does, the
        response ends with a `Call again with offset=...` instruction --
        follow it to read the rest of the file across multiple calls.
        """
        path = _safe_child(self.root, relative_path, must_exist=True)
        if not path.is_file():
            raise WorkspaceSecurityError(
                f"Workspace path is not a file: {relative_path!r}"
            )
        # Reference, don't transport -- see _EXTRACTED_PREFIX.
        if _is_extracted_intermediate(relative_path):
            return _extracted_redirect(relative_path)
        if _docling_internal_kind(relative_path) == "reference":
            return (
                f"[{relative_path} is a server-side Docling structured "
                "reference and is not readable into model context. Use its "
                "sibling content.md, manifest.json, or tables/*.csv files.]"
            )
        if path.suffix.lower() not in WORKSPACE_TEXT_EXTENSIONS:
            raise WorkspaceSecurityError(
                f"Workspace file type is not readable: {path.suffix}"
            )
        limit = max(1, min(int(max_chars), MAX_TOOL_READ_CHARS))
        start = max(0, int(offset))
        text = path.read_text(encoding="utf-8", errors="replace")
        total = len(text)

        if start >= total:
            if total == 0:
                return text
            return (
                f"[offset {start} is at or past end of file; "
                f"{relative_path!r} is {total} characters long. "
                "Nothing more to read.]"
            )

        budget_remaining = max(0, MAX_TEXT_CHARS_PER_RUN - self._text_chars_delivered)
        if budget_remaining <= 0:
            return (
                f"[The per-run text-read budget ({MAX_TEXT_CHARS_PER_RUN} "
                "characters across all read_text_file calls) is exhausted "
                "for this run. No more text can be read via read_text_file "
                "-- use search_text to find specific content instead of "
                "continuing to page through this or any other file.]"
            )

        effective_limit = min(limit, budget_remaining)
        chunk = text[start : start + effective_limit]
        end = start + len(chunk)
        self._text_chars_delivered += len(chunk)
        budget_hit = effective_limit < limit

        if start == 0 and end >= total:
            # Whole file fit in one page: no pagination footer needed.
            return chunk
        if end < total:
            footer = (
                f"\n[Read characters {start}-{end} of {total} total. "
                f"File continues; call again with offset={end} to read "
                "the next page.]"
            )
            if budget_hit:
                footer += (
                    " [Per-run text-read budget is now exhausted; further "
                    "read_text_file calls will return no text -- use "
                    "search_text instead.]"
                )
        else:
            footer = (
                f"\n[Read characters {start}-{end} of {total} total. "
                "End of file reached.]"
            )
        return chunk + footer

    def search_text(
        self, query: str, max_matches: int = 20
    ) -> dict[str, Any]:
        """Search text across files in the current uploaded workspace."""
        needle = str(query).strip()
        if not needle:
            raise ValueError("Search query must not be empty")
        limit = max(1, min(int(max_matches), MAX_TOOL_SEARCH_MATCHES))
        matches: list[dict[str, Any]] = []
        for path in self._text_files():
            relative = path.relative_to(self.root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle.casefold() in line.casefold():
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": line.strip()[:500],
                        }
                    )
                    if len(matches) >= limit:
                        return {
                            "query": needle,
                            "match_count": len(matches),
                            "matches": matches,
                            "truncated": True,
                        }
        return {
            "query": needle,
            "match_count": len(matches),
            "matches": matches,
            "truncated": False,
        }

    # -- Structured tabular access (CSV/XLSX) -----------------------------
    #
    # Prefer these over read_text_file/search_text for .csv/.xlsx: they
    # return structured, row-bounded data computed directly from the
    # original file via pandas, instead of a flattened, unbounded
    # to_string() dump that has to be paginated through as raw text (see
    # main.py's _extract_tabular -- that dump still exists as a
    # `.extracted.txt` companion for other tooling, but these tools never
    # read it). Every method routes through _table_path, so the same
    # sandboxing (_safe_child), extension check, and size backstop apply
    # uniformly, and every method is read-only -- none of them can mutate
    # or write the source file.

    def _table_path(self, relative_path: str) -> tuple[Path, str]:
        path = _safe_child(self.root, relative_path, must_exist=True)
        if not path.is_file():
            raise WorkspaceSecurityError(
                f"Workspace path is not a file: {relative_path!r}"
            )
        ext = path.suffix.lower()
        # The exact invalid sequence seen live: read_table_rows on the JSON
        # web_read just wrote. Answer with the path-based tool instead
        # of a generic "wrong extension", which the model followed with
        # inspect_file and read_text_file on the same file.
        if _is_extracted_intermediate(relative_path):
            raise ValueError(_extracted_redirect(relative_path))
        if ext not in TABLE_EXTENSIONS:
            raise ValueError(
                f"{relative_path!r} is not a table file (extension "
                f"{ext or '(none)'}); inspect_table/read_table_rows/"
                f"search_table/table_summary only support "
                f"{sorted(TABLE_EXTENSIONS)}. Call inspect_file first to "
                "see what this file actually supports -- do not retry a "
                "table tool on this path."
            )
        size = path.stat().st_size
        if size > MAX_TABLE_SOURCE_BYTES:
            raise ValueError(
                f"{relative_path!r} is {size} bytes, over the "
                f"{MAX_TABLE_SOURCE_BYTES}-byte limit for table tools; it "
                "cannot be loaded. Do not retry -- there is no bounded way "
                "to read this file with the available tools."
            )
        return path, ext

    def _load_table_sheets(self, path: Path, ext: str) -> dict:
        import pandas as pd

        try:
            if ext == ".csv":
                return {None: pd.read_csv(path)}
            return pd.read_excel(path, sheet_name=None, engine="openpyxl")
        except Exception as exc:
            raise ValueError(
                f"Could not parse {path.name!r} as a {ext} table: {exc}. Do "
                "not retry the same table tool on this path -- the file is "
                "not a valid table."
            ) from exc

    def _select_sheet(self, sheets: dict, ext: str, sheet: Optional[str]):
        """Pick a sheet from an already-loaded table.

        `sheet` must already be normalized (see _normalize_optional_str) by
        the caller -- every public method below does that before this is
        reached, so a sentinel like "None"/"null"/"" never gets here as a
        literal string.
        """
        if ext == ".csv":
            if sheet is not None:
                raise ValueError(
                    "CSV files have a single implicit sheet; omit `sheet` "
                    f"(got {sheet!r})."
                )
            return sheets[None]
        if sheet is None:
            return next(iter(sheets.values()))
        if sheet not in sheets:
            raise ValueError(
                f"Unknown sheet {sheet!r}; available sheets: "
                f"{sorted(sheets)}. Call inspect_table first to see valid "
                "sheet names."
            )
        return sheets[sheet]

    def inspect_table(self, relative_path: str) -> dict[str, Any]:
        """Return schema/shape for a CSV or XLSX file without reading rows.

        Call this before read_table_rows/search_table/table_summary on any
        unfamiliar table -- a cheap, bounded probe (sheet names, row/column
        counts, column dtypes, a 5-row preview) that tells you what's in
        the file before you spend calls reading it.
        """
        path, ext = self._table_path(relative_path)
        sheets = self._load_table_sheets(path, ext)
        sheet_reports = []
        for sheet_name, frame in sheets.items():
            columns = list(frame.columns)[:MAX_TABLE_COLUMNS_PER_READ]
            report = {
                "sheet": sheet_name,
                "row_count": len(frame),
                "column_count": len(frame.columns),
                "columns": [
                    {"name": str(col), "dtype": str(frame[col].dtype)}
                    for col in columns
                ],
                "preview_rows": [
                    {
                        str(col): _json_safe_cell(row[col])
                        for col in columns
                    }
                    for _, row in frame.head(5).iterrows()
                ],
            }
            if len(frame.columns) > MAX_TABLE_COLUMNS_PER_READ:
                report["columns_truncated"] = (
                    f"{len(frame.columns)} columns total; showing first "
                    f"{MAX_TABLE_COLUMNS_PER_READ}."
                )
            sheet_reports.append(report)
        return {"path": relative_path, "sheets": sheet_reports}

    def read_table_rows(
        self,
        relative_path: str,
        offset: int = 0,
        limit: int = MAX_TABLE_ROWS_PER_READ,
        sheet: Optional[str] = None,
        columns: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Read one bounded window of rows from a CSV/XLSX file.

        Returns at most `limit` rows (capped at MAX_TABLE_ROWS_PER_READ)
        starting at `offset`, as structured records -- not a flattened
        text dump, so most tables need far fewer calls to read fully than
        paging through their extracted text would. Call inspect_table
        first if you don't already know the sheet/column names or row
        count. When `truncated` is true, call again with
        offset=`next_offset` to continue; the same offset+limit always
        returns an identical result, so repeating it wastes a call.
        """
        sheet = _normalize_optional_str(sheet)
        path, ext = self._table_path(relative_path)
        sheets = self._load_table_sheets(path, ext)
        frame = self._select_sheet(sheets, ext, sheet)

        if columns:
            unknown = [c for c in columns if c not in frame.columns]
            if unknown:
                raise ValueError(
                    f"Unknown column(s) {unknown} for {relative_path!r}; "
                    f"available columns: {list(frame.columns)}."
                )
            frame = frame[columns]

        total = len(frame)
        start = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_TABLE_ROWS_PER_READ))

        if start >= total:
            return {
                "path": relative_path,
                "sheet": sheet,
                "offset": start,
                "returned_rows": 0,
                "total_rows": total,
                "truncated": False,
                "next_offset": None,
                "rows": [],
                "note": (
                    f"offset {start} is at or past the end of the table "
                    f"({total} rows). Nothing more to read."
                ),
            }

        budget_remaining = max(0, MAX_TABLE_ROWS_PER_RUN - self._table_rows_delivered)
        if budget_remaining <= 0:
            return {
                "path": relative_path,
                "sheet": sheet,
                "offset": start,
                "returned_rows": 0,
                "total_rows": total,
                "truncated": True,
                "next_offset": start,
                "rows": [],
                "note": (
                    f"The per-run table-row budget ({MAX_TABLE_ROWS_PER_RUN} "
                    "rows across all read_table_rows/search_table/"
                    "compare_tables calls) is exhausted for this run. Use "
                    "table_summary or compare_tables to analyze the rest "
                    "of this data instead of reading more raw rows."
                ),
            }

        effective_limit = min(limit, budget_remaining)
        window = frame.iloc[start : start + effective_limit]
        used_columns = list(window.columns)[:MAX_TABLE_COLUMNS_PER_READ]
        rows = [
            {col: _json_safe_cell(row[col]) for col in used_columns}
            for _, row in window.iterrows()
        ]
        self._table_rows_delivered += len(rows)
        end = start + len(window)
        result = {
            "path": relative_path,
            "sheet": sheet,
            "offset": start,
            "returned_rows": len(rows),
            "total_rows": total,
            "truncated": end < total,
            "next_offset": end if end < total else None,
            "rows": rows,
        }
        if len(window.columns) > MAX_TABLE_COLUMNS_PER_READ:
            result["columns_truncated"] = (
                f"{len(window.columns)} columns total; showing first "
                f"{MAX_TABLE_COLUMNS_PER_READ} per row."
            )
        if effective_limit < limit:
            result["note"] = (
                f"Only {effective_limit} of the requested {limit} rows "
                "were returned; the per-run table-row budget is now "
                "exhausted. Use table_summary/compare_tables for further "
                "analysis instead of reading more rows."
            )
        return result

    def search_table(
        self,
        relative_path: str,
        query: str,
        sheet: Optional[str] = None,
        column: Optional[str] = None,
        limit: int = MAX_TABLE_SEARCH_MATCHES,
    ) -> dict[str, Any]:
        """Search a CSV/XLSX table for rows containing `query`.

        Case-insensitive substring match, optionally restricted to one
        column. Returns at most `limit` matching rows (capped at
        MAX_TABLE_SEARCH_MATCHES), each tagged with its 0-based row
        offset so a match can be followed up with
        read_table_rows(offset=...) for surrounding context.
        """
        needle = str(query).strip()
        if not needle:
            raise ValueError("Search query must not be empty")
        sheet = _normalize_optional_str(sheet)
        column = _normalize_optional_str(column)
        path, ext = self._table_path(relative_path)
        sheets = self._load_table_sheets(path, ext)
        frame = self._select_sheet(sheets, ext, sheet)
        if column is not None and column not in frame.columns:
            raise ValueError(
                f"Unknown column {column!r} for {relative_path!r}; "
                f"available columns: {list(frame.columns)}."
            )
        requested_limit = max(1, min(int(limit), MAX_TABLE_SEARCH_MATCHES))

        budget_remaining = max(0, MAX_TABLE_ROWS_PER_RUN - self._table_rows_delivered)
        if budget_remaining <= 0:
            return {
                "path": relative_path,
                "sheet": sheet,
                "query": needle,
                "match_count": 0,
                "matches": [],
                "truncated": True,
                "note": (
                    f"The per-run table-row budget ({MAX_TABLE_ROWS_PER_RUN} "
                    "rows across all read_table_rows/search_table/"
                    "compare_tables calls) is exhausted for this run. Use "
                    "table_summary or compare_tables instead."
                ),
            }
        limit = min(requested_limit, budget_remaining)

        search_columns = [column] if column is not None else list(frame.columns)
        report_columns = list(frame.columns)[:MAX_TABLE_COLUMNS_PER_READ]

        matches: list[dict[str, Any]] = []
        truncated = False
        for row_index, (_, row) in enumerate(frame.iterrows()):
            haystack = " ".join(str(row[col]) for col in search_columns)
            if needle.casefold() in haystack.casefold():
                matches.append(
                    {
                        "row": row_index,
                        **{
                            col: _json_safe_cell(row[col])
                            for col in report_columns
                        },
                    }
                )
                if len(matches) >= limit:
                    truncated = row_index < len(frame) - 1
                    break

        self._table_rows_delivered += len(matches)
        result = {
            "path": relative_path,
            "sheet": sheet,
            "query": needle,
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
        }
        if limit < requested_limit:
            result["note"] = (
                f"Only up to {limit} matches were searched for (of the "
                f"{requested_limit} requested) because the per-run "
                "table-row budget is close to exhausted. Use "
                "table_summary/compare_tables for a fuller picture instead "
                "of raising the search limit."
            )
        return result

    def table_summary(
        self,
        relative_path: str,
        sheet: Optional[str] = None,
        columns: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Return bounded per-column statistics for a CSV/XLSX table.

        Cheaper than reading rows when the question is "what's in this
        data" rather than "show me specific records": count, null count,
        distinct count, and either numeric min/max/mean or the top 5 most
        common values per column -- computed server-side, no raw rows
        returned.
        """
        sheet = _normalize_optional_str(sheet)
        path, ext = self._table_path(relative_path)
        sheets = self._load_table_sheets(path, ext)
        frame = self._select_sheet(sheets, ext, sheet)

        if columns:
            unknown = [c for c in columns if c not in frame.columns]
            if unknown:
                raise ValueError(
                    f"Unknown column(s) {unknown} for {relative_path!r}; "
                    f"available columns: {list(frame.columns)}."
                )
            target_columns = list(columns)
        else:
            target_columns = list(frame.columns)

        columns_truncated = None
        if len(target_columns) > MAX_TABLE_SUMMARY_COLUMNS:
            columns_truncated = (
                f"{len(target_columns)} columns requested; summarizing the "
                f"first {MAX_TABLE_SUMMARY_COLUMNS}."
            )
            target_columns = target_columns[:MAX_TABLE_SUMMARY_COLUMNS]

        import pandas as pd

        column_summaries = []
        for col in target_columns:
            series = frame[col]
            non_null = series.dropna()
            summary: dict[str, Any] = {
                "name": str(col),
                "dtype": str(series.dtype),
                "row_count": len(series),
                "null_count": int(series.isna().sum()),
                "distinct_count": int(non_null.nunique()),
            }
            if pd.api.types.is_numeric_dtype(series) and len(non_null) > 0:
                summary["min"] = _json_safe_cell(non_null.min())
                summary["max"] = _json_safe_cell(non_null.max())
                summary["mean"] = _json_safe_cell(non_null.mean())
            else:
                top_values = non_null.astype(str).value_counts().head(5)
                summary["top_values"] = [
                    {"value": _json_safe_cell(value), "count": int(count)}
                    for value, count in top_values.items()
                ]
            column_summaries.append(summary)

        result = {
            "path": relative_path,
            "sheet": sheet,
            "row_count": len(frame),
            "columns": column_summaries,
        }
        if columns_truncated:
            result["columns_truncated"] = columns_truncated
        return result

    def compare_tables(
        self,
        relative_path_a: str,
        relative_path_b: str,
        sheet_a: Optional[str] = None,
        sheet_b: Optional[str] = None,
        key_columns: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Compare two CSV/XLSX tables without loading their rows into context.

        Use this INSTEAD of reading both tables row by row to answer "what
        changed" or "what's different" -- that traversal pattern is exactly
        what the per-run row budget exists to stop. Bounded, server-side
        result: row/column counts, which columns exist only in one file,
        and aggregate stats (numeric min/max/mean, distinct counts) for
        columns common to both. Pass `key_columns` (one or more column
        names present in both tables, e.g. an id or name column) to
        additionally get counts of rows only in A, only in B, and a small
        bounded sample of rows whose shared columns differ between the two.
        """
        sheet_a = _normalize_optional_str(sheet_a)
        sheet_b = _normalize_optional_str(sheet_b)
        path_a, ext_a = self._table_path(relative_path_a)
        path_b, ext_b = self._table_path(relative_path_b)
        frame_a = self._select_sheet(self._load_table_sheets(path_a, ext_a), ext_a, sheet_a)
        frame_b = self._select_sheet(self._load_table_sheets(path_b, ext_b), ext_b, sheet_b)

        import pandas as pd

        columns_a, columns_b = list(frame_a.columns), list(frame_b.columns)
        set_a, set_b = set(columns_a), set(columns_b)
        common_columns = [c for c in columns_a if c in set_b]

        column_comparisons = []
        for col in common_columns[:MAX_TABLE_SUMMARY_COLUMNS]:
            series_a, series_b = frame_a[col].dropna(), frame_b[col].dropna()
            entry: dict[str, Any] = {
                "name": str(col),
                "distinct_count_a": int(series_a.nunique()),
                "distinct_count_b": int(series_b.nunique()),
            }
            if (
                pd.api.types.is_numeric_dtype(frame_a[col])
                and pd.api.types.is_numeric_dtype(frame_b[col])
                and len(series_a) > 0
                and len(series_b) > 0
            ):
                entry["mean_a"] = _json_safe_cell(series_a.mean())
                entry["mean_b"] = _json_safe_cell(series_b.mean())
                entry["mean_delta"] = _json_safe_cell(series_b.mean() - series_a.mean())
                entry["min_a"] = _json_safe_cell(series_a.min())
                entry["max_a"] = _json_safe_cell(series_a.max())
                entry["min_b"] = _json_safe_cell(series_b.min())
                entry["max_b"] = _json_safe_cell(series_b.max())
            column_comparisons.append(entry)

        result: dict[str, Any] = {
            "path_a": relative_path_a,
            "path_b": relative_path_b,
            "sheet_a": sheet_a,
            "sheet_b": sheet_b,
            "row_count_a": len(frame_a),
            "row_count_b": len(frame_b),
            "row_count_delta": len(frame_b) - len(frame_a),
            "columns_only_in_a": sorted(set_a - set_b),
            "columns_only_in_b": sorted(set_b - set_a),
            "common_columns": column_comparisons,
        }
        if len(common_columns) > MAX_TABLE_SUMMARY_COLUMNS:
            result["common_columns_truncated"] = (
                f"{len(common_columns)} common columns; summarizing the "
                f"first {MAX_TABLE_SUMMARY_COLUMNS}."
            )

        if not key_columns:
            return result

        unknown_a = [c for c in key_columns if c not in set_a]
        unknown_b = [c for c in key_columns if c not in set_b]
        if unknown_a or unknown_b:
            raise ValueError(
                f"key_columns {key_columns} must exist in both tables; "
                f"missing from A: {unknown_a or 'none'}, missing from B: "
                f"{unknown_b or 'none'}."
            )

        merged = frame_a.merge(
            frame_b, on=key_columns, how="outer", indicator=True,
            suffixes=("_a", "_b"),
        )
        only_a = int((merged["_merge"] == "left_only").sum())
        only_b = int((merged["_merge"] == "right_only").sum())
        both = int((merged["_merge"] == "both").sum())
        result["key_columns"] = key_columns
        result["rows_only_in_a"] = only_a
        result["rows_only_in_b"] = only_b
        result["rows_in_both"] = both

        compare_cols = [c for c in common_columns if c not in key_columns]
        diff_rows: list[dict[str, Any]] = []
        differing_cell_count = 0
        if compare_cols and both:
            both_rows = merged[merged["_merge"] == "both"]
            for col in compare_cols:
                col_a, col_b = f"{col}_a", f"{col}_b"
                if col_a not in both_rows.columns or col_b not in both_rows.columns:
                    continue
                mismatch_mask = (
                    both_rows[col_a].astype(str) != both_rows[col_b].astype(str)
                )
                differing_cell_count += int(mismatch_mask.sum())
                if len(diff_rows) < MAX_TABLE_COMPARE_SAMPLE_ROWS:
                    remaining_slots = MAX_TABLE_COMPARE_SAMPLE_ROWS - len(diff_rows)
                    for _, row in both_rows[mismatch_mask].head(remaining_slots).iterrows():
                        diff_rows.append(
                            {
                                **{k: _json_safe_cell(row[k]) for k in key_columns},
                                "column": str(col),
                                "value_a": _json_safe_cell(row[col_a]),
                                "value_b": _json_safe_cell(row[col_b]),
                            }
                        )

        budget_remaining = max(0, MAX_TABLE_ROWS_PER_RUN - self._table_rows_delivered)
        if budget_remaining <= 0:
            diff_rows = []
            result["differing_rows_note"] = (
                "Per-run table-row budget is exhausted; omitting the "
                "differing-rows sample. Aggregate counts above are still "
                "complete and accurate."
            )
        elif len(diff_rows) > budget_remaining:
            diff_rows = diff_rows[:budget_remaining]
        self._table_rows_delivered += len(diff_rows)

        result["differing_cell_count"] = differing_cell_count
        result["differing_rows_sample"] = diff_rows
        result["differing_rows_sample_truncated"] = differing_cell_count > len(diff_rows)
        return result

    def as_tools(self) -> list:
        return [
            self.list_files,
            self.inspect_file,
            self.read_text_file,
            self.search_text,
            self.inspect_table,
            self.read_table_rows,
            self.search_table,
            self.table_summary,
            self.compare_tables,
        ]

    def describe(self) -> str:
        return json.dumps(self.list_files(), ensure_ascii=False)
