"""Deterministic, server-side artifact creation confined to a job workspace.

Why this module exists
----------------------
The system used to have no way to produce a real file at all. A request whose
DELIVERABLE was a file ("give me the Excel file", "create HTML/CSS/JS so I can
search this") could only ever be answered with prose describing the file, or
with a model fabricating base64 that was never written anywhere. Both read to
the user as an outright failure.

The rule this module enforces is that **the model never fabricates a binary
format**. The model supplies structured *data* (JSON text) plus a filename;
this module does the encoding with openpyxl / csv / json. Consequences that
matter:

  * generated JSON is parseable, because we parse it before writing it;
  * generated XLSX reopens, because openpyxl wrote it, not a language model;
  * a malformed payload fails immediately, explicitly, and cheaply, instead of
    producing a corrupt file the user discovers later.

Confinement
-----------
Everything is written under ``<workspace_root>/artifacts/``. Uploaded sources
live under ``<workspace_root>/sources/`` (see workspace.build_job_workspace),
so an artifact write can never overwrite an upload -- not because we check for
it, but because the two trees are disjoint by construction and every path goes
through workspace._safe_child against the artifacts root.

Bounds
------
Count, per-file bytes, total bytes, rows, and columns are all capped. A model
that decides to write a million rows gets a bounded, explicit failure rather
than filling the disk.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import carbone_render
import document_verify
import image_fetch as _image_fetch
from workspace import WorkspaceSecurityError, _safe_child, safe_filename

# Same logger as the Document worker's path lines, so one grep shows the
# whole story: docker logs proje-backend-1 | grep "document "
document_logger = logging.getLogger("agent_webapp.document")
document_logger.setLevel(logging.INFO)


# --- Bounds ---------------------------------------------------------------
#
# Sized against what a real deliverable needs, not against what the disk can
# hold: the mentor XLSX case is ~31 rows x 7 columns, and a generated
# searchable web app is a few hundred KB of data.json at the very most. These
# leave two orders of magnitude of headroom over that while still making a
# runaway obvious and cheap to stop.
MAX_ARTIFACTS_PER_JOB = 20
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_ARTIFACTS_TOTAL_BYTES = 25 * 1024 * 1024
MAX_ARTIFACT_ROWS = 50_000
MAX_ARTIFACT_COLUMNS = 256
# The JSON/text payload the MODEL is allowed to hand us in one tool call.
# Deliberately smaller than MAX_ARTIFACT_BYTES: anything larger than this
# should be produced by code execution writing into the workspace directly
# (execute_code has the same artifacts directory available), not streamed
# through a completion. This is also the backstop that stops one tool call
# from consuming the whole output budget.
MAX_PAYLOAD_CHARS = 400_000

# Rows/cells a DETERMINISTIC (server-side) conversion may move. These are far
# higher than MAX_ARTIFACT_ROWS-through-a-tool-argument because no model token
# is spent per row: the data goes file -> file without passing through a
# completion. That asymmetry is the whole point of the reference-based path.
MAX_CONVERT_ROWS = 200_000
MAX_CONVERT_CELL_CHARS = 32_000

ARTIFACTS_DIRNAME = "artifacts"
# Subdirectory of the artifacts tree holding intermediate working data (today:
# tables extracted from web pages, see web_fetch.EXTRACTED_DIRNAME). Confined
# and cleaned up like everything else, but excluded from the user-facing
# artifact listing -- it is input to the next step, not a deliverable.
INTERNAL_DIRNAME = "extracted"

# Models pass JSON null / "none" as from_path when they have no data_path.
# That is not a workspace path; treat it as "no reference".
_UNSET_PATH_TOKENS = frozenset({"", "null", "none", "undefined", "nil", "~"})


def usable_workspace_path(value: Any) -> str:
    """A workspace-relative path, or empty if the model passed a sentinel."""
    text = "" if value is None else str(value).strip()
    if not text or text.casefold() in _UNSET_PATH_TOKENS:
        return ""
    return text


def _has_blocks(blocks: Any) -> bool:
    """True when a create_* call actually supplied typed blocks."""
    if isinstance(blocks, str):
        return bool(usable_workspace_path(blocks)) and blocks.strip() not in {"[]", "{}"}
    return bool(blocks)


def _blocks_memo_part(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    return json.dumps(blocks, ensure_ascii=False, default=str)

# Tool responses used to include a href-shaped `download_path` (`artifacts/foo.xlsx`)
# that models pasted into the chat as a markdown link. The real download is
# the job snapshot's disk scan + the chat's ProducedFiles list, not that field.
_UI_LISTS_FILE_NOTE = (
    "The file is written and ready. The product UI lists it for download. "
    "Do not include a markdown link, URL, or workspace path in your answer, "
    "and do not recreate the file."
)

# A multi-file bundle (create_web_bundle / create_code_bundle) writes one of
# these into its own folder. scan_artifacts reads it to report the whole
# bundle as ONE deliverable (entry point + member list + size) instead of N
# unrelated-looking files -- see _write_bundle_manifest / _bundle_entry.
# Dotfile so it never collides with a real project file (package.json,
# data.json, ...) and is trivially excluded from the member list.
_MANIFEST_NAME = ".artifact-manifest.json"
# Per-call cap on bundle members (code files + data files combined). Sized
# like MAX_ARTIFACT_COLUMNS: generous for any real project (a Node API is
# rarely more than a handful of files), cheap to make a runaway obvious.
MAX_BUNDLE_FILES = 40


def _normalize_optional(value: Any) -> Optional[str]:
    """Treat a model's placeholder for "omitted" as omitted.

    Mirrors workspace._NONE_LIKE_STRINGS: a small model routinely sends
    sheet="None" instead of leaving the argument out, and taking that
    literally turns a valid call into a "no such sheet" error.
    """
    text = "" if value is None else str(value).strip()
    return None if text.lower() in {"", "none", "null", "nil", "n/a", "na"} else text


def _json_cell(value: Any) -> Any:
    """One table cell as a JSON-safe value.

    pandas hands back numpy scalars, NaT/NaN, and Timestamps, none of which
    json.dumps accepts. NaN becoming null (rather than the string "nan") is
    what makes the generated file usable by the JS that reads it.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    # numpy scalars expose .item(); do this before str() so numbers stay numbers.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_cell(item())
        except Exception:  # noqa: BLE001 - fall through to the string form
            pass
    text = str(value)
    if text in {"NaT", "nan", "NaN", "<NA>"}:
        return None
    return text[:MAX_CONVERT_CELL_CHARS]


# Extensions this module knows how to write, mapped to the writer that owns
# them. A filename whose extension is absent or wrong is corrected rather than
# rejected -- a model that calls create_xlsx_artifact("data.xls") means the
# xlsx writer, and failing the call over that would waste a whole turn.
_EXTENSION_FOR_KIND = {
    "json": ".json",
    "csv": ".csv",
    "xlsx": ".xlsx",
    "html": ".html",
    "text": ".md",
    "docx": ".docx",
    "pptx": ".pptx",
    "pdf": ".pdf",
}

# Bundle members are fixed, so a generated web app always has the same shape
# and the frontend/user always knows which file is the entry point.
BUNDLE_INDEX = "index.html"
BUNDLE_STYLES = "styles.css"
BUNDLE_SCRIPT = "app.js"
BUNDLE_DATA = "data.json"


class ArtifactError(ValueError):
    """A bounded, explicit artifact failure the calling agent can act on.

    Deliberately distinct from WorkspaceSecurityError: this is "your payload
    was wrong", which the model can fix on the next turn, versus "you tried to
    leave the workspace", which it must not retry.
    """


@dataclass(frozen=True)
class ArtifactRecord:
    """One written artifact, as reported to the API/UI layer."""

    path: str  # workspace-relative, e.g. "artifacts/prayer-times.xlsx"
    filename: str
    kind: str
    size_bytes: int
    # Set for bundles: the member a browser should open first.
    entry_point: Optional[str] = None
    # The tool that produced this file and when, so provenance survives past
    # the run that created it (scan_artifacts re-derives this from disk --
    # see created_at's mtime fallback and _PRODUCER_BY_KIND).
    producer: Optional[str] = None
    created_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "path": self.path,
            "filename": self.filename,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }
        if self.entry_point:
            data["entry_point"] = self.entry_point
        if self.producer:
            data["producer"] = self.producer
        if self.created_at:
            data["created_at"] = self.created_at
        return data


def _coerce_payload(payload: Any, *, what: str) -> Any:
    """Turn whatever the model passed into real Python data.

    Models pass structured arguments in three shapes depending on the backend:
    a JSON string, an already-decoded object (strict json_schema backends), or
    a JSON string wrapped in a ```json fence (small models, frequently). All
    three mean the same thing, and rejecting two of them would make the tool
    fail for reasons that have nothing to do with the user's request.
    """
    if payload is None:
        raise ArtifactError(f"{what} is required")
    if not isinstance(payload, str):
        return payload

    text = payload.strip()
    if len(text) > MAX_PAYLOAD_CHARS:
        raise ArtifactError(
            f"{what} is {len(text)} characters; the limit for one call is "
            f"{MAX_PAYLOAD_CHARS}. Write the data with execute_code instead -- "
            "it can write into the same artifacts directory without passing "
            "the content through a model response."
        )
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    if not text:
        raise ArtifactError(f"{what} is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            f"{what} is not valid JSON ({exc.msg} at line {exc.lineno} "
            f"column {exc.colno}). Send a single well-formed JSON value."
        ) from exc


def _normalize_rows(
    data: Any, columns: Optional[list[str]] = None
) -> tuple[list[str], list[list[Any]]]:
    """Reduce the several shapes a model sends tabular data in to one.

    Accepted, because all of them show up in practice:
      * ``[{"col": v, ...}, ...]``           -- list of record objects
      * ``[[v, ...], ...]`` with `columns`   -- rows plus an explicit header
      * ``[[header...], [v, ...], ...]``     -- header as the first row
      * ``{"columns": [...], "rows": [...]}`` -- explicit envelope

    Returns (header, rows). Column order is taken from the first record and
    extended by later records, so it is stable and does not depend on dict
    iteration order across records.
    """
    if isinstance(data, dict):
        if "rows" in data:
            columns = data.get("columns") or columns
            data = data.get("rows")
        else:
            # A single record object is a one-row table, which is a
            # reasonable thing to ask for and cheap to support.
            data = [data]

    if not isinstance(data, list):
        raise ArtifactError(
            "Tabular data must be a JSON array of row objects or row arrays, "
            f"got {type(data).__name__}."
        )
    if not data:
        raise ArtifactError("Tabular data contains no rows.")
    if len(data) > MAX_ARTIFACT_ROWS:
        raise ArtifactError(
            f"Tabular data has {len(data)} rows; the limit is "
            f"{MAX_ARTIFACT_ROWS}."
        )

    if isinstance(data[0], dict):
        header: list[str] = list(columns) if columns else []
        seen = set(header)
        for record in data:
            if not isinstance(record, dict):
                raise ArtifactError(
                    "Mixed row shapes: the first row is an object, but a "
                    "later row is not. Use one shape for every row."
                )
            for key in record:
                if key not in seen:
                    seen.add(key)
                    header.append(str(key))
        if len(header) > MAX_ARTIFACT_COLUMNS:
            raise ArtifactError(
                f"Tabular data has {len(header)} columns; the limit is "
                f"{MAX_ARTIFACT_COLUMNS}."
            )
        rows = [[record.get(name) for name in header] for record in data]
        return header, rows

    # Row-array shape.
    body = [row for row in data]
    if columns:
        header = [str(name) for name in columns]
    else:
        first = body.pop(0)
        if not isinstance(first, list):
            raise ArtifactError(
                "Tabular data must be a JSON array of row objects or row "
                "arrays."
            )
        header = [str(name) for name in first]
    if len(header) > MAX_ARTIFACT_COLUMNS:
        raise ArtifactError(
            f"Tabular data has {len(header)} columns; the limit is "
            f"{MAX_ARTIFACT_COLUMNS}."
        )
    rows = []
    for row in body:
        if not isinstance(row, list):
            raise ArtifactError(
                "Mixed row shapes: expected every row to be a JSON array."
            )
        # Pad/trim to the header so a ragged row cannot shift columns
        # silently -- a wrong-but-plausible spreadsheet is worse than an
        # obviously short one.
        padded = list(row[: len(header)])
        padded.extend([None] * (len(header) - len(padded)))
        rows.append(padded)
    return header, rows


def _cell(value: Any) -> Any:
    """Make one value safe for a csv/xlsx cell.

    openpyxl raises on arbitrary objects, and a nested list/dict has no
    spreadsheet representation at all -- JSON-encoding those keeps the data
    visible rather than crashing the write halfway through.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 32_000:
            # Excel's own hard per-cell limit is 32,767 characters.
            return value[:32_000]
        return value
    return json.dumps(value, ensure_ascii=False)


class ArtifactTools:
    """Artifact-creation tools bound to exactly one job's workspace.

    One instance per job (built in capabilities.resolve_tools), so the counts
    and byte totals below are genuine per-run budgets shared by every worker
    and every subtask -- the same ownership model ReadOnlyWorkspaceTools uses
    for its row/char budgets.
    """

    def __init__(self, workspace_root: Path, *, source_request: str = "") -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        self.root = self.workspace_root / ARTIFACTS_DIRNAME
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Original user ask for this job. When set, create_pdf/docx/pptx can
        # auto-fetch a Commons image even if the model omits image_query.
        self.source_request = str(source_request or "").strip()
        self._records: dict[str, ArtifactRecord] = {}
        # Memo of (tool, normalized args) -> previous result, so an identical
        # create call returns the existing artifact instead of re-encoding it.
        # See _memo_key: this is what stops a retried or looping executor from
        # redoing expensive work, and what lets it SEE that the deliverable it
        # is about to produce already exists.
        self._memo: dict[tuple, dict[str, Any]] = {}

    # --- internals --------------------------------------------------------

    def _read_workspace_json(self, relative_path: str, *, what: str) -> Any:
        """Load a JSON file that already exists in this job's workspace.

        The reference-based half of the artifact interface. A model that has
        already produced artifacts/data.json must be able to say "use that
        file" instead of pasting its contents back through a tool argument --
        which is where a large dataset gets truncated mid-string and fails.

        Reads from the workspace root, so both sources/ (an upload) and
        artifacts/ (something this run generated) are reachable, and both go
        through _safe_child so neither can escape.
        """
        try:
            path = _safe_child(self.workspace_root, relative_path, must_exist=True)
        except WorkspaceSecurityError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactError(
                f"{what}: {relative_path!r} could not be resolved ({exc}). "
                "Pass a path relative to the workspace root, e.g. "
                "'artifacts/data.json'."
            ) from exc
        if not path.is_file():
            raise ArtifactError(
                f"{what}: {relative_path!r} is not a file in this workspace. "
                "Use list_artifacts or list_files to see what exists."
            )
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactError(
                f"{what}: {relative_path!r} is larger than the "
                f"{MAX_ARTIFACT_BYTES}-byte limit."
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ArtifactError(
                f"{what}: {relative_path!r} is not valid JSON ({exc.msg} at "
                f"line {exc.lineno}). Regenerate it before referencing it."
            ) from exc
        except OSError as exc:
            raise ArtifactError(f"{what}: could not read {relative_path!r} ({exc})") from exc

    @staticmethod
    def _memo_key(tool: str, *parts: Any) -> tuple:
        """A stable identity for one create call.

        Long payloads are hashed rather than stored: the memo must not become
        a second copy of every dataset the run has produced.
        """
        normalized = []
        for part in parts:
            text = "" if part is None else str(part)
            if len(text) > 512:
                normalized.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
            else:
                normalized.append(text)
        return (tool, *normalized)

    def _memoized(self, key: tuple) -> Optional[dict[str, Any]]:
        """A previous identical call's result, if the file is still there.

        Existence is re-checked rather than trusted: a memo that outlives its
        file would report an artifact the user cannot download, which is
        exactly the "claimed but not real" failure the artifact layer exists
        to prevent.
        """
        cached = self._memo.get(key)
        if cached is None:
            return None
        relative = cached.get("path") or ""
        try:
            if not (self.workspace_root / relative).is_file():
                self._memo.pop(key, None)
                return None
        except OSError:
            return None
        return {
            **cached,
            "reused_existing": True,
            "note": (
                "This exact artifact was already created earlier in this run "
                "and is unchanged, so nothing was rewritten. It is ready for "
                "the user -- do not create it again. Move on to the remaining "
                "work, or write your final answer."
            ),
        }

    @staticmethod
    def _embed_image_in_pptx(pptx_bytes: bytes, image) -> bytes:
        """Insert an image into the first content slide of a rendered PPTX."""
        from pptx import Presentation
        from pptx.util import Cm

        try:
            buf = io.BytesIO(pptx_bytes)
            prs = Presentation(buf)
            # Find the first slide after the title slide (or the title slide if only one)
            slide = prs.slides[1] if len(prs.slides) > 1 else prs.slides[0]
            # Place image centered, below title area
            slide_width = prs.slide_width
            img_width = Cm(18)
            left = int((slide_width - img_width) / 2)
            top = Cm(4)
            slide.shapes.add_picture(str(image.path), left, top, width=img_width)
            out = io.BytesIO()
            prs.save(out)
            return out.getvalue()
        except Exception:  # noqa: BLE001
            return pptx_bytes

    def _fetch_images(
        self,
        image_query: str,
        *,
        context: str = "",
        require_user_ask: bool = False,
    ) -> list:
        """Fetch a Commons image if query is provided. Returns list of ImageEmbed.

        When ``require_user_ask`` is True (PDF/Word data reports), skip unless
        the user asked for a photo — schedule/summary PDFs stay image-free.
        """
        from document_export import ImageEmbed

        if require_user_ask and not _image_fetch.request_wants_image(
            self.source_request or ""
        ):
            return []
        query = _image_fetch.resolve_image_query(
            self.source_request or "",
            model_query=str(image_query or "").strip(),
            fallback=str(context or "").strip(),
        )
        if not query:
            return []
        cache = getattr(self, "_image_cache", None)
        if cache is None:
            self._image_cache = {}
            cache = self._image_cache
        if query in cache:
            return list(cache[query])
        images_dir = self.root / "images"
        result = _image_fetch.fetch_image(
            query, images_dir, topic=self.source_request or context or query
        )
        if result is None:
            cache[query] = []
            return []
        embeds = [ImageEmbed(path=result.path, attribution=result.attribution)]
        cache[query] = embeds
        return list(embeds)

    def _verify_render(
        self,
        kind: str,
        body: bytes,
        *,
        path: str,
        headings: Sequence[str] = (),
        expect_sources: bool = False,
        fallback: Optional[Callable[[], bytes]] = None,
        fallback_label: str = "markdown_fallback",
    ) -> tuple[bytes, dict[str, Any]]:
        """Reopen a rendered PDF/DOCX and check it before it is written.

        ``fallback`` renders the same content with another writer. It runs
        only when the first file fails, and wins only if it passes. The
        user always gets a file: a check that fails on both is reported in
        the logs and the tool result, not turned into a lost deliverable.
        """
        report = document_verify.verify_document(
            kind, body, expected_headings=headings, expect_sources=expect_sources
        )
        if report.ok:
            document_logger.info("document verify %s path=%s", report.summary(), path)
            return body, {**report.as_dict(), "path": path}

        document_logger.warning(
            "document verify FAILED %s path=%s missing=%s",
            report.summary(),
            path,
            report.missing_headings,
        )
        if fallback is not None:
            try:
                alternative = fallback()
            except Exception as exc:  # noqa: BLE001 - keep the first file
                document_logger.warning("document verify: fallback writer raised (%s)", exc)
            else:
                second = document_verify.verify_document(
                    kind, alternative, expected_headings=headings, expect_sources=expect_sources
                )
                if second.ok:
                    document_logger.warning(
                        "document verify: using %s instead of %s (%s)",
                        fallback_label,
                        path,
                        second.summary(),
                    )
                    return alternative, {
                        **second.as_dict(),
                        "path": fallback_label,
                        "rejected": {**report.as_dict(), "path": path},
                    }
                document_logger.warning(
                    "document verify: fallback also failed (%s); keeping %s",
                    second.summary(),
                    path,
                )
        return body, {**report.as_dict(), "path": path}

    def _render_blocks_pdf(self, block_document: Any, images: list) -> tuple[bytes, str]:
        """PDF bytes for typed blocks, and the engine that produced them.

        Chromium (HTML/CSS) first, unless DOCUMENT_PDF_ENGINE=reportlab. Any
        Chromium failure -- not installed, crashed, timed out -- is logged
        and answered with the reportlab renderer, never with a failed job.
        """
        import document_html
        import document_model

        if document_html.pdf_engine() == "chromium":
            started = time.monotonic()
            try:
                data = document_html.build_pdf(block_document, images=images)
            except Exception as exc:  # noqa: BLE001 - reportlab is the answer
                document_logger.warning(
                    "document pdf engine=chromium FAILED after %.1fs (%s: %s) -> reportlab",
                    time.monotonic() - started,
                    type(exc).__name__,
                    str(exc)[:200],
                )
            else:
                document_logger.info(
                    "document pdf engine=chromium (%.1fs, %d bytes)",
                    time.monotonic() - started,
                    len(data),
                )
                return data, "chromium"
        return document_model.build_pdf(block_document, images=images), "reportlab"

    def _target(self, filename: str, kind: str, subdir: str = "") -> Path:
        """Resolve a model-supplied filename to a real, confined path.

        Two independent defences, because they fail differently:
        safe_filename strips any directory component from the *name*, and
        _safe_child re-validates the assembled path against the artifacts
        root (rejecting traversal, absolute paths, and symlinks). Either one
        alone would be enough for the cases we can think of; both together
        are enough for the ones we cannot.
        """
        name = safe_filename(filename or "", fallback=f"artifact{_EXTENSION_FOR_KIND.get(kind, '')}")
        expected = _EXTENSION_FOR_KIND.get(kind)
        if expected and not name.lower().endswith(expected):
            name = f"{Path(name).stem or 'artifact'}{expected}"
        relative = f"{subdir}/{name}" if subdir else name
        return _safe_child(self.root, relative, must_exist=False)

    def _check_budget(self, path: Path, size_bytes: int) -> None:
        if size_bytes > MAX_ARTIFACT_BYTES:
            raise ArtifactError(
                f"Artifact is {size_bytes} bytes; the per-file limit is "
                f"{MAX_ARTIFACT_BYTES}."
            )
        relative = path.relative_to(self.workspace_root).as_posix()
        # Rewriting an artifact this run is allowed (a model correcting its
        # own output should not be charged twice), so the replaced file's
        # bytes come back out of the total before the new ones go in.
        existing = self._records.get(relative)
        current_total = sum(record.size_bytes for record in self._records.values())
        if existing is not None:
            current_total -= existing.size_bytes
        elif len(self._records) >= MAX_ARTIFACTS_PER_JOB:
            raise ArtifactError(
                f"This job already created {MAX_ARTIFACTS_PER_JOB} artifacts, "
                "which is the limit."
            )
        if current_total + size_bytes > MAX_ARTIFACTS_TOTAL_BYTES:
            raise ArtifactError(
                "Writing this artifact would exceed the "
                f"{MAX_ARTIFACTS_TOTAL_BYTES}-byte total artifact budget for "
                "this job."
            )

    def _write(
        self,
        path: Path,
        payload: bytes,
        kind: str,
        entry_point: Optional[str] = None,
        producer: Optional[str] = None,
    ) -> ArtifactRecord:
        self._check_budget(path, len(payload))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(payload)
        relative = path.relative_to(self.workspace_root).as_posix()
        record = ArtifactRecord(
            path=relative,
            filename=path.name,
            kind=kind,
            size_bytes=len(payload),
            entry_point=entry_point,
            producer=producer,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._records[relative] = record
        return record

    def _ok(self, record: ArtifactRecord, **extra: Any) -> dict[str, Any]:
        """The success shape every create_* tool returns.

        Says "created" with a real path and a real byte count so later tools
        can consume `path`. Does not emit `download_path`: that name looked
        like a URL, and models pasted it into the chat as a markdown link.
        The API/UI download channel is the disk scan, not this payload.
        """
        payload = {
            "created": True,
            **record.as_dict(),
            **extra,
        }
        payload.setdefault("note", _UI_LISTS_FILE_NOTE)
        return payload

    def records(self) -> list[ArtifactRecord]:
        return sorted(self._records.values(), key=lambda record: record.path)

    # --- tools ------------------------------------------------------------

    def convert_table_to_json(
        self,
        source_path: str,
        filename: str = "data.json",
        sheet: str = "",
        max_rows: int = 0,
    ) -> dict[str, Any]:
        """Convert a spreadsheet or CSV in this workspace directly into a JSON file.

        THE right way to turn an uploaded table into JSON. The conversion
        happens server-side: the rows go from file to file without passing
        through your response, so there is no size limit worth worrying about
        and no chance of the data being truncated. You do not need to read the
        rows first.

        Args:
            source_path (str): Workspace-relative path of the .xlsx or .csv,
                e.g. "sources/abc123/data.xlsx". Use list_files to find it.
            filename (str): Name for the JSON file to create.
                (default: :obj:`"data.json"`)
            sheet (str): Worksheet name for a multi-sheet .xlsx. Omit for the
                first/only sheet. (default: "")
            max_rows (int): Optional cap on rows converted. 0 means all of
                them. (default: :obj:`0`)

        Returns:
            Dict[str, Any]: The created file's path, size, row/column counts,
                the column names, and a small sample of rows so you can see
                the shape without reading the whole file.
        """
        memo_key = self._memo_key("convert_table_to_json", source_path, filename, sheet, max_rows)
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        try:
            path = _safe_child(self.workspace_root, source_path, must_exist=True)
        except WorkspaceSecurityError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactError(
                f"source_path {source_path!r} could not be resolved ({exc}). "
                "Call list_files to see the exact paths available."
            ) from exc
        if not path.is_file():
            raise ArtifactError(f"source_path {source_path!r} is not a file.")
        extension = path.suffix.lower()
        if extension not in {".csv", ".xlsx"}:
            raise ArtifactError(
                f"convert_table_to_json handles .csv and .xlsx; got "
                f"{extension or 'no extension'}. For other formats, read the "
                "file and build the JSON with execute_code."
            )

        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - pandas is pinned
            raise ArtifactError(f"table conversion is unavailable ({exc})") from exc

        try:
            if extension == ".csv":
                frame = pd.read_csv(path)
            else:
                sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
                if not sheets:
                    raise ArtifactError(f"{source_path!r} contains no sheets.")
                name = _normalize_optional(sheet)
                if name is not None and name not in sheets:
                    raise ArtifactError(
                        f"{source_path!r} has no sheet {name!r}. Available: "
                        + ", ".join(str(key) for key in list(sheets)[:20])
                    )
                frame = sheets[name] if name is not None else next(iter(sheets.values()))
        except ArtifactError:
            raise
        except Exception as exc:  # noqa: BLE001 - any parse failure is terminal
            raise ArtifactError(
                f"Could not parse {source_path!r} as a table ({exc}). Do not "
                "retry this call on the same path."
            ) from exc

        limit = MAX_CONVERT_ROWS if not max_rows else min(int(max_rows), MAX_CONVERT_ROWS)
        truncated = len(frame) > limit
        frame = frame.head(limit)
        columns = [str(name) for name in frame.columns]
        records = [
            {
                column: _json_cell(value)
                for column, value in zip(columns, row)
            }
            for row in frame.itertuples(index=False, name=None)
        ]

        body = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        record = self._write(
            self._target(filename, "json"), body, "json", producer="convert_table_to_json"
        )
        result = self._ok(
            record,
            row_count=len(records),
            column_count=len(columns),
            columns=columns[:MAX_ARTIFACT_COLUMNS],
            truncated=truncated,
            # Enough to see the shape and write UI code against it; nowhere
            # near enough to be a way of reading the dataset.
            sample=records[:3],
            note=(
                "The JSON file is written and ready. Reference it by path "
                f"({record.path}) in create_web_bundle's data_path -- do not "
                "paste its contents into a tool argument."
            ),
        )
        self._memo[memo_key] = result
        return result

    def create_json_artifact(
        self, filename: str, data: str = "", from_path: str = ""
    ) -> dict[str, Any]:
        """Create a real .json file in this job's artifacts directory.

        Supply EITHER `data` (for JSON you are writing yourself) OR
        `from_path` (to copy/validate JSON that already exists in the
        workspace). To convert an uploaded spreadsheet, use
        convert_table_to_json instead -- it does not route the rows through
        your response at all.

        Args:
            filename (str): Name for the file, e.g. "prayer-times.json". Any
                directory part is stripped and a .json extension is enforced.
            data (str): The JSON content, as a JSON-encoded string. Parsed and
                re-serialized here, so the file is guaranteed to be valid
                JSON; a malformed value fails this call instead of producing a
                broken file. (default: "")
            from_path (str): Workspace-relative path of an existing JSON file
                to use as the content instead of `data`. (default: "")

        Returns:
            Dict[str, Any]: The created file's path and size.
        """
        memo_key = self._memo_key("create_json_artifact", filename, data, from_path)
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        if usable_workspace_path(from_path):
            parsed = self._read_workspace_json(from_path, what="from_path")
        else:
            parsed = _coerce_payload(data, what="data")
        body = json.dumps(parsed, ensure_ascii=False, indent=2).encode("utf-8")
        record = self._write(
            self._target(filename, "json"), body, "json", producer="create_json_artifact"
        )
        result = self._ok(record)
        self._memo[memo_key] = result
        return result

    def create_csv_artifact(
        self, filename: str, data: str = "", columns: str = "", from_path: str = ""
    ) -> dict[str, Any]:
        """Create a real .csv file from tabular data.

        Supply EITHER `data` OR `from_path`. Prefer `from_path` whenever the
        rows already exist as a JSON file in the workspace -- re-emitting a
        dataset through your response is slow and truncates on anything large.

        Args:
            filename (str): Name for the file, e.g. "prayer-times.csv".
            data (str): Rows as a JSON array -- either an array of objects
                (keys become the header) or an array of arrays (the first
                array is the header unless `columns` is given). (default: "")
            columns (str): Optional JSON array of column names, used when
                `data` holds row arrays with no header row. (default: "")
            from_path (str): Workspace-relative path of an existing JSON file
                holding the rows, e.g. "artifacts/data.json". (default: "")

        Returns:
            Dict[str, Any]: The created file's path, size, and row/column counts.
        """
        memo_key = self._memo_key("create_csv_artifact", filename, data, columns, from_path)
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        if usable_workspace_path(from_path):
            parsed = self._read_workspace_json(from_path, what="from_path")
        else:
            parsed = _coerce_payload(data, what="data")
        header_override = (
            _coerce_payload(columns, what="columns") if columns else None
        )
        header, rows = _normalize_rows(parsed, header_override)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if cell is None else _cell(cell) for cell in row])
        # utf-8-sig: Excel on Windows reads a plain UTF-8 CSV as latin-1 and
        # mangles every non-ASCII character. A CSV the user cannot open
        # correctly is not a delivered artifact.
        body = buffer.getvalue().encode("utf-8-sig")
        record = self._write(
            self._target(filename, "csv"), body, "csv", producer="create_csv_artifact"
        )
        result = self._ok(record, row_count=len(rows), column_count=len(header))
        self._memo[memo_key] = result
        return result

    def create_xlsx_artifact(
        self,
        filename: str,
        data: str = "",
        columns: str = "",
        sheet_name: str = "Sheet1",
        from_path: str = "",
    ) -> dict[str, Any]:
        """Create a real .xlsx (Excel) file from tabular data.

        The spreadsheet is written server-side with openpyxl -- never
        generated as text or base64 by a model -- so the resulting file is
        always a valid workbook that opens in Excel.

        Supply EITHER `data` OR `from_path`. Prefer `from_path` whenever the
        rows already exist as a JSON file in the workspace.

        Args:
            filename (str): Name for the file, e.g. "ankara-prayer-times.xlsx".
            data (str): Rows as a JSON array -- either an array of objects
                (keys become the header) or an array of arrays (the first
                array is the header unless `columns` is given). (default: "")
            columns (str): Optional JSON array of column names, used when
                `data` holds row arrays with no header row. (default: "")
            sheet_name (str): Worksheet name. (default: :obj:`"Sheet1"`)
            from_path (str): Workspace-relative path of an existing JSON file
                holding the rows, e.g. "artifacts/data.json". (default: "")

        Returns:
            Dict[str, Any]: The created file's path, size, and row/column counts.
        """
        memo_key = self._memo_key(
            "create_xlsx_artifact", filename, data, columns, sheet_name, from_path
        )
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        try:
            from openpyxl import Workbook
        except ImportError as exc:  # pragma: no cover - openpyxl is pinned
            raise ArtifactError(
                "XLSX support is unavailable in this deployment "
                f"({exc}). Use create_csv_artifact instead."
            ) from exc

        if usable_workspace_path(from_path):
            parsed = self._read_workspace_json(from_path, what="from_path")
        else:
            parsed = _coerce_payload(data, what="data")
        header_override = (
            _coerce_payload(columns, what="columns") if columns else None
        )
        header, rows = _normalize_rows(parsed, header_override)

        workbook = Workbook()
        sheet = workbook.active
        # Excel rejects >31 chars and the characters below in a sheet name;
        # openpyxl raises rather than truncating, which would fail the write
        # for a reason the user does not care about.
        sheet.title = (re.sub(r"[\\/*?:\[\]]", "-", sheet_name or "Sheet1") or "Sheet1")[:31]
        sheet.append([_cell(name) for name in header])
        for row in rows:
            sheet.append([_cell(cell) for cell in row])

        buffer = io.BytesIO()
        workbook.save(buffer)
        body = buffer.getvalue()
        record = self._write(
            self._target(filename, "xlsx"), body, "xlsx", producer="create_xlsx_artifact"
        )
        result = self._ok(record, row_count=len(rows), column_count=len(header))
        self._memo[memo_key] = result
        return result

    def _create_carbone_artifact(
        self,
        kind: str,
        filename: str,
        title: str,
        summary: str,
        data: str,
        columns: str,
        from_path: str,
        image_query: str = "",
        blocks: Any = "",
    ) -> dict[str, Any]:
        """Shared PPTX (Carbone) / DOCX (document_export) render path.

        The model never talks to the renderer directly -- it calls
        create_docx_artifact/create_pptx_artifact exactly like every other
        create_*_artifact tool. XLSX/CSV/JSON keep their existing native
        writers untouched.

        DOCX uses python-docx via document_export so long narrative reports
        get real paragraphs, headings, and lists instead of one Carbone
        `{d.summary}` blob. PPTX uses python-pptx via document_export for a
        dark structured deck (title + section slides + optional table).
        """
        memo_key = self._memo_key(
            f"create_{kind}_artifact",
            filename,
            title,
            summary,
            data,
            columns,
            from_path,
            image_query,
            _blocks_memo_part(blocks),
        )
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        if usable_workspace_path(from_path):
            parsed = self._read_workspace_json(from_path, what="from_path")
        elif not str(data or "").strip() or str(data or "").strip() in {"[]", "null", "{}"}:
            parsed = []
        else:
            parsed = _coerce_payload(data, what="data")
        header_override = _coerce_payload(columns, what="columns") if columns else None
        if parsed in ([], {}, None) or parsed == "":
            header, rows = list(header_override or []), []
        else:
            header, rows = _normalize_rows(parsed, header_override)

        import document_export

        clean_title = document_export.short_report_title(
            title or "Report", fallback="Report"
        )
        clean_summary = document_export.sanitize_report_prose(summary or "")

        payload, notes = carbone_render.build_table_payload(
            title=clean_title,
            summary=clean_summary,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            header=header,
            rows=rows,
            kind=kind,
        )

        images = self._fetch_images(
            image_query,
            context=clean_title,
            require_user_ask=(kind == "docx"),
        )

        verification: Optional[dict[str, Any]] = None
        block_document = None
        if kind == "docx" and _has_blocks(blocks):
            import document_model

            block_document = document_model.document_from_blocks(clean_title, blocks)

        if block_document is not None:
            try:
                body = document_model.build_docx(
                    block_document,
                    images=images,
                    footnote=" ".join(notes) if notes else "",
                    data_header=list(header)[: carbone_render.MAX_TABLE_COLUMNS],
                    data_rows=[
                        list(row)[: carbone_render.MAX_TABLE_COLUMNS]
                        for row in list(rows)[: carbone_render.MAX_DOCX_TABLE_ROWS]
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - surface as ArtifactError
                raise ArtifactError(f"Could not create the DOCX artifact: {exc}") from exc
            body, verification = self._verify_render(
                "docx",
                body,
                path="blocks",
                headings=document_verify.headings_of(block_document),
                fallback=lambda: document_export.build_docx(
                    block_document.title,
                    block_document.to_markdown(),
                    header=list(header)[: carbone_render.MAX_TABLE_COLUMNS],
                    rows=[
                        list(row)[: carbone_render.MAX_TABLE_COLUMNS]
                        for row in list(rows)[: carbone_render.MAX_DOCX_TABLE_ROWS]
                    ],
                    footnote=" ".join(notes) if notes else "",
                    images=images,
                ),
            )
        elif kind == "docx":
            prose = clean_summary or str(payload.get("summary") or "").strip()
            if not prose and header and rows:
                prose = "Aşağıdaki tabloda rapor verileri özetlenmiştir."
            bounded_header = list(header)[: carbone_render.MAX_TABLE_COLUMNS]
            bounded_rows = [
                list(row)[: carbone_render.MAX_TABLE_COLUMNS]
                for row in list(rows)[: carbone_render.MAX_DOCX_TABLE_ROWS]
            ]
            try:
                body = document_export.build_docx(
                    str(payload.get("title") or clean_title or "Report"),
                    prose,
                    header=bounded_header,
                    rows=bounded_rows,
                    footnote=" ".join(notes) if notes else "",
                    images=images,
                )
            except Exception as exc:  # noqa: BLE001 - surface as ArtifactError
                raise ArtifactError(
                    f"Could not create the DOCX artifact: {exc}"
                ) from exc
            docx_title = str(payload.get("title") or clean_title or "Report")
            body, verification = self._verify_render(
                "docx",
                body,
                path="markdown",
                headings=document_verify.headings_of_markdown(docx_title, prose),
                expect_sources=document_verify.expects_sources(prose),
            )
        else:
            prose = clean_summary or str(payload.get("summary") or "").strip()
            bounded_header = list(header)[: carbone_render.MAX_TABLE_COLUMNS]
            bounded_rows = [
                list(row)[: carbone_render.MAX_TABLE_COLUMNS]
                for row in list(rows)[: document_export.MAX_PPTX_TABLE_ROWS]
            ]
            try:
                body = document_export.build_pptx(
                    str(payload.get("title") or clean_title or "Sunum"),
                    prose,
                    header=bounded_header,
                    rows=bounded_rows,
                    images=images,
                )
            except Exception as exc:  # noqa: BLE001
                raise ArtifactError(
                    f"Could not create the PPTX artifact: {exc}"
                ) from exc

        record = self._write(
            self._target(filename, kind), body, kind, producer=f"create_{kind}_artifact"
        )
        result = self._ok(
            record,
            row_count=len(rows),
            column_count=len(header),
            table_notes=notes,
            **({"verification": verification} if verification else {}),
        )
        self._memo[memo_key] = result
        return result

    def create_docx_artifact(
        self,
        filename: str,
        title: str = "",
        summary: str = "",
        data: str = "",
        columns: str = "",
        from_path: str = "",
        image_query: str = "",
        blocks: str = "",
    ) -> dict[str, Any]:
        """Create a real, professionally formatted .docx report.

        Renders `title`, structured narrative (`summary`), and an optional
        data table server-side -- never generated as text or base64 by a
        model. Wall-of-text summaries are split into paragraphs, numbered
        lists, and section breaks. Best for a narrative report with a small
        table; for a full dataset dump use create_xlsx_artifact or
        create_csv_artifact instead (a Word table is bounded to a handful of
        columns here on purpose).

        Supply EITHER `data` OR `from_path`. Prefer `from_path` whenever the
        rows already exist as a JSON file in the workspace.

        Args:
            filename (str): Name for the file, e.g. "quarterly-report.docx".
            title (str): Report title/heading. (default: "")
            summary (str): Narrative text (plain or light markdown).
                (default: "")
            data (str): Rows as a JSON array -- either an array of objects
                (keys become the header) or an array of arrays (the first
                array is the header unless `columns` is given). (default: "")
            columns (str): Optional JSON array of column names, used when
                `data` holds row arrays with no header row. (default: "")
            from_path (str): Workspace-relative path of an existing JSON file
                holding the rows, e.g. "artifacts/data.json". (default: "")
            image_query (str): Search term for a royalty-free image to embed
                below the title (e.g. "polar bear arctic"). Fetched from
                Wikimedia Commons; no API key. (default: "")
            blocks (str): Optional JSON array of typed blocks that replaces
                `summary` as the body: {"type": "heading", "level": 1-3,
                "text"}, {"type": "paragraph", "text"}, {"type": "list",
                "ordered": bool, "items": [...]}, {"type": "table",
                "columns": [...], "rows": [[...]]}, {"type": "quote",
                "text"}, {"type": "image", "caption"}. **bold** is allowed
                inside text. (default: "")

        Returns:
            Dict[str, Any]: The created file's path, size, and row/column counts.
        """
        return self._create_carbone_artifact(
            "docx", filename, title, summary, data, columns, from_path, image_query, blocks
        )

    def edit_docx_template(
        self,
        filename: str,
        template_path: str,
        edits: dict[str, str],
    ) -> dict[str, Any]:
        """Rewrite only the named paragraphs of an uploaded .docx.

        Args:
            filename (str): Name for the output file, e.g. "sartname.docx".
            template_path (str): Workspace-relative path to the uploaded
                .docx being edited, e.g. "sources/ab12cd34/sartname.docx".
            edits (Dict[str, str]): Paragraph index (as a string, matching
                the numbering the paragraphs were listed with) mapped to
                that paragraph's new text. Paragraphs not listed keep their
                exact original wording AND formatting -- they are never
                rewritten, so they cannot drift.

        Returns:
            Dict[str, Any]: The created file's path and size.
        """
        resolved = usable_workspace_path(template_path)
        if not resolved:
            raise ArtifactError(
                "template_path is required and must be a workspace-relative "
                "path to the uploaded .docx template."
            )
        template_file = _safe_child(self.workspace_root, resolved, must_exist=True)
        if template_file.suffix.lower() != ".docx":
            raise ArtifactError(f"template_path must be a .docx file: {resolved!r}")

        import document_export

        parsed: dict[int, str] = {}
        for key, value in (edits or {}).items():
            try:
                parsed[int(str(key).strip())] = str(value)
            except (TypeError, ValueError):
                continue
        if not parsed:
            raise ArtifactError("edits was empty -- nothing to change.")

        try:
            payload = document_export.edit_docx_template(template_file, parsed)
        except Exception as exc:  # noqa: BLE001
            raise ArtifactError(f"Could not edit the uploaded template: {exc}") from exc

        record = self._write(
            self._target(filename, "docx"),
            payload,
            "docx",
            producer="edit_docx_template",
        )
        return self._ok(record, template_path=resolved, edited_paragraphs=len(parsed))

    def create_docx_from_template(
        self,
        filename: str,
        template_path: str,
        title: str = "",
        content: str = "",
    ) -> dict[str, Any]:
        """Write NEW content into an uploaded .docx, reusing its own
        fonts/spacing/headers/footers/structure exactly ("write this into
        the same template" requests). The server clones the template's
        formatting; the model supplies plain prose only.

        Args:
            filename (str): Name for the output file, e.g. "sozlesme.docx".
            template_path (str): Workspace-relative path to the uploaded
                .docx being cloned, e.g. "sources/ab12cd34/vekaletname.docx"
                -- a FORMATTING source (unlike from_path elsewhere, which is
                a DATA source). Must already exist in this job's workspace.
            title (str): New document title/heading. (default: "")
            content (str): New body text as plain or light markdown
                (headings, bullets). Replaces the template's own body text;
                fonts/margins/headers/footers/styles are preserved from the
                template. (default: "")

        Returns:
            Dict[str, Any]: The created file's path and size.
        """
        memo_key = self._memo_key(
            "create_docx_from_template", filename, template_path, title, content
        )
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        resolved = usable_workspace_path(template_path)
        if not resolved:
            raise ArtifactError(
                "template_path is required and must be a workspace-relative "
                "path to the uploaded .docx template."
            )
        template_file = _safe_child(self.workspace_root, resolved, must_exist=True)
        if template_file.suffix.lower() != ".docx":
            raise ArtifactError(f"template_path must be a .docx file: {resolved!r}")

        import document_export

        clean_title = document_export.short_report_title(title or "", fallback="")
        # keep_bold: this is the one writer that can turn **markers** into
        # real bold runs, using the template's own styles. Everywhere else
        # strips them because the writer would show the asterisks.
        clean_body = document_export.sanitize_report_prose(
            content or "", keep_bold=True
        )
        try:
            payload = document_export.fill_docx_template(
                template_file, clean_title, clean_body
            )
        except Exception as exc:  # noqa: BLE001
            raise ArtifactError(
                f"Could not write into the uploaded template: {exc}"
            ) from exc

        record = self._write(
            self._target(filename, "docx"),
            payload,
            "docx",
            producer="create_docx_from_template",
        )
        result = self._ok(record, template_path=resolved)
        self._memo[memo_key] = result
        return result

    def create_pptx_artifact(
        self,
        filename: str,
        title: str = "",
        summary: str = "",
        data: str = "",
        columns: str = "",
        from_path: str = "",
        image_query: str = "",
    ) -> dict[str, Any]:
        """Create a real, editable .pptx presentation.

        Renders `title`, `summary`, and a data table into a maintained
        PowerPoint template server-side (via Carbone) -- never generated as
        text or base64 by a model. The data table is bounded to a single
        slide's worth of rows on purpose; for a full dataset use
        create_xlsx_artifact or create_csv_artifact instead.

        Supply EITHER `data` OR `from_path`. Prefer `from_path` whenever the
        rows already exist as a JSON file in the workspace.

        Args:
            filename (str): Name for the file, e.g. "quarterly-review.pptx".
            title (str): Title-slide heading. (default: "")
            summary (str): One paragraph of narrative text for the summary
                slide. (default: "")
            data (str): Rows as a JSON array -- either an array of objects
                (keys become the header) or an array of arrays (the first
                array is the header unless `columns` is given). (default: "")
            columns (str): Optional JSON array of column names, used when
                `data` holds row arrays with no header row. (default: "")
            from_path (str): Workspace-relative path of an existing JSON file
                holding the rows, e.g. "artifacts/data.json". (default: "")
            image_query (str): Search term for a royalty-free image to embed
                in the presentation (e.g. "polar bear arctic"). Fetched from
                Wikimedia Commons; no API key. (default: "")

        Returns:
            Dict[str, Any]: The created file's path, size, and row/column counts.
        """
        return self._create_carbone_artifact(
            "pptx", filename, title, summary, data, columns, from_path, image_query
        )

    def create_pdf_artifact(
        self,
        filename: str,
        title: str = "",
        content: str = "",
        from_path: str = "",
        image_query: str = "",
        blocks: str = "",
    ) -> dict[str, Any]:
        """Create a real .pdf report from title + body text.

        The model supplies the narrative; this tool renders a real PDF
        server-side (reportlab via document_export) -- never as base64.
        Prefer `from_path` when the body already exists as a workspace
        .md/.txt file.

        Args:
            filename (str): Name for the file, e.g. "quarterly-report.pdf".
            title (str): Document title. (default: "")
            content (str): Report body as plain text or light markdown
                (headings, bullets). (default: "")
            from_path (str): Workspace-relative path of an existing text
                file to use as the body. (default: "")
            image_query (str): Search term for a royalty-free image to embed
                below the title (e.g. "polar bear arctic"). Fetched from
                Wikimedia Commons; no API key. (default: "")
            blocks (str): Optional JSON array of typed blocks used as the
                body instead of `content` -- same block types as
                create_docx_artifact. (default: "")

        Returns:
            Dict[str, Any]: The created file's path and size.
        """
        memo_key = self._memo_key(
            "create_pdf_artifact",
            filename,
            title,
            content,
            from_path,
            image_query,
            _blocks_memo_part(blocks),
        )
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        import document_export

        block_document = None
        if _has_blocks(blocks):
            import document_model

            block_document = document_model.document_from_blocks(
                document_export.short_report_title(
                    (title or "").strip()
                    or Path(filename or "report").stem.replace("-", " ").replace("_", " "),
                    fallback="Rapor",
                ),
                blocks,
            )
        if block_document is not None:
            images = self._fetch_images(
                image_query, context=block_document.title, require_user_ask=True
            )
            try:
                payload, engine = self._render_blocks_pdf(block_document, images)
            except Exception as exc:  # noqa: BLE001
                raise ArtifactError(f"Could not create the PDF artifact: {exc}") from exc
            if engine == "chromium":
                # A Chromium file that fails its check is redrawn from the same
                # blocks by reportlab before anything falls back to markdown.
                fallback = lambda: document_model.build_pdf(block_document, images=images)  # noqa: E731
                fallback_label = "reportlab_fallback"
            else:
                fallback = lambda: document_export.build_pdf(  # noqa: E731
                    block_document.title, block_document.to_markdown(), images=images
                )
                fallback_label = "markdown_fallback"
            payload, verification = self._verify_render(
                "pdf",
                payload,
                path="blocks",
                headings=document_verify.headings_of(block_document),
                fallback=fallback,
                fallback_label=fallback_label,
            )
            if verification.get("path") == "blocks":
                verification["engine"] = engine
            else:
                verification["engine"] = (
                    "reportlab" if verification.get("path") == "reportlab_fallback" else "reportlab-markdown"
                )
            record = self._write(
                self._target(filename, "pdf"), payload, "pdf", producer="create_pdf_artifact"
            )
            result = self._ok(record, verification=verification)
            self._memo[memo_key] = result
            return result

        body = ""
        if usable_workspace_path(from_path):
            source = _safe_child(self.workspace_root, from_path, must_exist=True)
            try:
                body = source.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactError(
                    "from_path must be a UTF-8 text file to render as PDF"
                ) from exc
        elif isinstance(content, str):
            body = content
        if not body.strip():
            raise ArtifactError(
                "content must be a non-empty string, or from_path a text file"
            )
        if len(body) > MAX_PAYLOAD_CHARS:
            raise ArtifactError(
                f"content is {len(body)} characters; the limit for one "
                f"call is {MAX_PAYLOAD_CHARS}."
            )

        import document_export

        heading = document_export.short_report_title(
            (title or "").strip()
            or Path(filename or "report").stem.replace("-", " ").replace("_", " "),
            fallback="Rapor",
        )
        images = self._fetch_images(
            image_query, context=heading, require_user_ask=True
        )

        prose = document_export.sanitize_report_prose(body)
        payload = document_export.build_pdf(heading, prose, images=images)
        payload, verification = self._verify_render(
            "pdf",
            payload,
            path="markdown",
            headings=document_verify.headings_of_markdown(heading, prose),
            expect_sources=document_verify.expects_sources(prose),
        )
        record = self._write(
            self._target(filename, "pdf"),
            payload,
            "pdf",
            producer="create_pdf_artifact",
        )
        result = self._ok(record, verification=verification)
        self._memo[memo_key] = result
        return result

    def create_text_artifact(self, filename: str, content: str) -> dict[str, Any]:
        """Create a real .md or .txt file with the given text content.

        Wall-of-text input is structured into markdown paragraphs / lists
        before writing so downloads are readable in any editor.

        Args:
            filename (str): Name for the file, e.g. "report.md". A .md
                extension is used when none is given.
            content (str): The exact text to write.

        Returns:
            Dict[str, Any]: The created file's path and size.
        """
        memo_key = self._memo_key("create_text_artifact", filename, content)
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached
        if not isinstance(content, str) or not content.strip():
            raise ArtifactError("content must be a non-empty string")
        if len(content) > MAX_PAYLOAD_CHARS:
            raise ArtifactError(
                f"content is {len(content)} characters; the limit for one "
                f"call is {MAX_PAYLOAD_CHARS}."
            )
        name = safe_filename(filename or "", fallback="report.md")
        suffix = Path(name).suffix.lower()
        if suffix not in {".md", ".markdown", ".txt"}:
            name = f"{Path(name).stem or 'report'}.md"
            suffix = ".md"
        import document_export

        heading = Path(name).stem.replace("-", " ").replace("_", " ")
        if suffix == ".txt":
            # Plain text: structured paragraphs, no markdown heading markers.
            text_out = document_export.structure_prose(content)
            if heading and not text_out.lower().startswith(heading.lower()):
                text_out = f"{heading}\n\n{text_out}"
            if not text_out.endswith("\n"):
                text_out += "\n"
        else:
            text_out = document_export.to_markdown(heading, content)
        target = _safe_child(self.root, name, must_exist=False)
        record = self._write(
            target, text_out.encode("utf-8"), "text", producer="create_text_artifact"
        )
        result = self._ok(record)
        self._memo[memo_key] = result
        return result

    def create_html_artifact(self, filename: str, html: str) -> dict[str, Any]:
        """Create a real, single-file .html page.

        Use this for a self-contained page (CSS and JS inline). For a page
        that should ship separate stylesheet/script/data files, use
        create_web_bundle instead.

        Args:
            filename (str): Name for the file, e.g. "index.html".
            html (str): The complete HTML document to write.

        Returns:
            Dict[str, Any]: The created file's path and size.
        """
        memo_key = self._memo_key("create_html_artifact", filename, html)
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached
        if not isinstance(html, str) or not html.strip():
            raise ArtifactError("html must be a non-empty string")
        if len(html) > MAX_PAYLOAD_CHARS:
            raise ArtifactError(
                f"html is {len(html)} characters; the limit for one call is "
                f"{MAX_PAYLOAD_CHARS}."
            )
        record = self._write(
            self._target(filename, "html"),
            html.encode("utf-8"),
            "html",
            producer="create_html_artifact",
        )
        result = self._ok(record)
        self._memo[memo_key] = result
        return result

    def create_web_bundle(
        self,
        name: str,
        html: str,
        css: str = "",
        js: str = "",
        data: str = "",
        data_path: str = "",
    ) -> dict[str, Any]:
        """Create a small, self-contained web app as a real directory of files.

        Writes index.html plus, when supplied, styles.css, app.js and
        data.json into a folder inside this job's artifacts directory. The
        page is opened from index.html and loads its siblings by relative
        path, so the whole folder is the deliverable.

        For the data, STRONGLY prefer `data_path` over `data`: point at a JSON
        file that already exists (typically the one convert_table_to_json just
        wrote) and the server copies it in. Pasting a large dataset into
        `data` means re-emitting every row through your own response, which
        is slow and gets truncated mid-string on anything sizeable.

        Args:
            name (str): Folder name for the bundle, e.g. "prayer-times-app".
            html (str): The complete index.html document. Reference the other
                files as "styles.css", "app.js" and "data.json".
            css (str): Stylesheet contents for styles.css. (default: "")
            js (str): Script contents for app.js. (default: "")
            data (str): JSON data for data.json, as a JSON-encoded string.
                Only for small, hand-written data -- use `data_path` for
                anything derived from a file. (default: "")
            data_path (str): Workspace-relative path of an existing JSON file
                to use as data.json, e.g. "artifacts/data.json". Takes
                precedence over `data`. (default: "")

        Returns:
            Dict[str, Any]: The bundle folder, every file written, and the
                entry point to open.
        """
        if not isinstance(html, str) or not html.strip():
            raise ArtifactError("html must be a non-empty string")
        memo_key = self._memo_key("create_web_bundle", name, html, css, js, data, data_path)
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached
        folder = safe_filename(name or "", fallback="web-app")
        folder = Path(folder).stem or "web-app"
        # Validate the folder itself before writing any member, so a bad name
        # cannot leave a half-written bundle behind.
        _safe_child(self.root, folder, must_exist=False)

        members: list[dict[str, Any]] = []
        total = 0
        pieces: list[tuple[str, bytes]] = [(BUNDLE_INDEX, html.encode("utf-8"))]
        if css and css.strip():
            pieces.append((BUNDLE_STYLES, css.encode("utf-8")))
        if js and js.strip():
            pieces.append((BUNDLE_SCRIPT, js.encode("utf-8")))
        # data_path wins: it is the path that does not route the dataset
        # through a model response, so when both are given the referenced
        # file is the one the caller actually means.
        if data_path and str(data_path).strip():
            parsed = self._read_workspace_json(data_path, what="data_path")
            pieces.append(
                (
                    BUNDLE_DATA,
                    json.dumps(parsed, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            )
        elif data and str(data).strip():
            parsed = _coerce_payload(data, what="data")
            pieces.append(
                (
                    BUNDLE_DATA,
                    json.dumps(parsed, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            )

        for member_name, payload in pieces:
            # Same policy every other inline-text tool uses (MAX_PAYLOAD_CHARS,
            # measured pre-encoding on the source string) plus the shared
            # post-encoding backstop (MAX_ARTIFACT_BYTES) -- previously this
            # loop only checked the post-encoding byte count, a second,
            # uncentralized threshold that let a single member grow ~25x
            # larger before rejection than create_html_artifact/
            # create_text_artifact would allow for equivalent content.
            if len(payload) > MAX_ARTIFACT_BYTES:
                raise ArtifactError(
                    f"{member_name} is {len(payload)} bytes; the per-file "
                    f"limit is {MAX_ARTIFACT_BYTES}."
                )
            target = _safe_child(
                self.root, f"{folder}/{member_name}", must_exist=False
            )
            record = self._write(
                target,
                payload,
                "web_bundle",
                entry_point=BUNDLE_INDEX if member_name == BUNDLE_INDEX else None,
                producer="create_web_bundle",
            )
            members.append(record.as_dict())
            total += record.size_bytes

        entry = f"{ARTIFACTS_DIRNAME}/{folder}/{BUNDLE_INDEX}"
        self._write_bundle_manifest(
            folder,
            kind="web_bundle",
            entry_point=BUNDLE_INDEX,
            members=members,
            producer="create_web_bundle",
        )
        result = {
            "created": True,
            "kind": "web_bundle",
            "path": f"{ARTIFACTS_DIRNAME}/{folder}/{BUNDLE_INDEX}",
            "entry_point": entry,
            "files": members,
            "size_bytes": total,
            "note": (
                "The bundle is written and ready. The product UI lists it for "
                "download. Do not include a markdown link, URL, or workspace "
                "path in your answer, and do not recreate it. Write your final "
                "answer now unless the request asked for something else as well."
            ),
        }
        self._memo[memo_key] = result
        return result

    def create_code_bundle(
        self,
        name: str,
        files: str,
        data_files: str = "",
        entry_point: str = "",
    ) -> dict[str, Any]:
        """Create an arbitrary multi-file project as a real directory of files.

        Use this for anything create_web_bundle's fixed index.html/styles.css/
        app.js/data.json shape does not fit: a Node/Python API server, a
        script plus its config, or any project with more than one source
        file. Keep code and data separate -- write `files` for source/config
        content and use `data_files` to copy in data that already exists in
        the workspace (e.g. from convert_table_to_json or web_read).
        NEVER type a dataset's rows into a file's content in `files`, even
        inside a generated source file; that defeats the entire point of
        `data_files` and re-creates the exact failure this tool exists to
        prevent.

        Args:
            name (str): Folder name for the project, e.g. "prayer-api".
            files (str): JSON object mapping relative file path to text
                content, e.g. {"server.js": "...", "package.json": "..."}.
                Paths may include subdirectories (e.g. "src/index.js") but may
                not escape the project folder.
            data_files (str): JSON object mapping relative file path to a
                workspace-relative source path to copy in as data, e.g.
                {"data/prayer-times.json": "artifacts/extracted/x-tables.json"}.
                The server copies the bytes directly -- the data never passes
                through your response. (default: "")
            entry_point (str): Relative path of the file a user should run or
                open first, e.g. "server.js". Defaults to the first key in
                `files`. (default: "")

        Returns:
            Dict[str, Any]: The project folder, every file written, and the
                entry point to run/open.
        """
        file_map = _coerce_payload(files, what="files")
        if not isinstance(file_map, dict) or not file_map:
            raise ArtifactError("files must be a non-empty JSON object of path -> content")
        data_map: dict[str, Any] = {}
        if data_files and str(data_files).strip():
            data_map = _coerce_payload(data_files, what="data_files")
            if not isinstance(data_map, dict):
                raise ArtifactError("data_files must be a JSON object of path -> source path")

        total_members = len(file_map) + len(data_map)
        if total_members > MAX_BUNDLE_FILES:
            raise ArtifactError(
                f"create_code_bundle got {total_members} files; the limit "
                f"per bundle is {MAX_BUNDLE_FILES}."
            )

        memo_key = self._memo_key(
            "create_code_bundle", name, files, data_files, entry_point
        )
        cached = self._memoized(memo_key)
        if cached is not None:
            return cached

        folder = safe_filename(name or "", fallback="project")
        folder = Path(folder).stem or "project"
        _safe_child(self.root, folder, must_exist=False)

        members: list[dict[str, Any]] = []
        total = 0
        first_key = next(iter(file_map))
        chosen_entry = _normalize_optional(entry_point) or first_key

        for rel_path, content in file_map.items():
            if not isinstance(content, str):
                raise ArtifactError(f"files[{rel_path!r}] must be a string of text content")
            if len(content) > MAX_PAYLOAD_CHARS:
                raise ArtifactError(
                    f"files[{rel_path!r}] is {len(content)} characters; the "
                    f"limit for one file is {MAX_PAYLOAD_CHARS}."
                )
            payload = content.encode("utf-8")
            if len(payload) > MAX_ARTIFACT_BYTES:
                raise ArtifactError(
                    f"files[{rel_path!r}] is {len(payload)} bytes; the "
                    f"per-file limit is {MAX_ARTIFACT_BYTES}."
                )
            target = _safe_child(self.root, f"{folder}/{rel_path}", must_exist=False)
            record = self._write(
                target,
                payload,
                "code_bundle",
                entry_point=chosen_entry if rel_path == chosen_entry else None,
                producer="create_code_bundle",
            )
            members.append(record.as_dict())
            total += record.size_bytes

        for rel_path, source_path in data_map.items():
            if not isinstance(source_path, str) or not source_path.strip():
                raise ArtifactError(
                    f"data_files[{rel_path!r}] must be a workspace-relative path"
                )
            try:
                source = _safe_child(self.workspace_root, source_path, must_exist=True)
            except WorkspaceSecurityError:
                raise
            except (OSError, ValueError) as exc:
                raise ArtifactError(
                    f"data_files[{rel_path!r}]: {source_path!r} could not be "
                    f"resolved ({exc}). Pass a path relative to the workspace "
                    "root, e.g. 'artifacts/data.json'."
                ) from exc
            if not source.is_file():
                raise ArtifactError(
                    f"data_files[{rel_path!r}]: {source_path!r} is not a file "
                    "in this workspace. Use list_artifacts or list_files to "
                    "see what exists."
                )
            payload = source.read_bytes()
            target = _safe_child(self.root, f"{folder}/{rel_path}", must_exist=False)
            record = self._write(
                target,
                payload,
                "code_bundle",
                entry_point=chosen_entry if rel_path == chosen_entry else None,
                producer="create_code_bundle",
            )
            members.append(record.as_dict())
            total += record.size_bytes

        entry = f"{ARTIFACTS_DIRNAME}/{folder}/{chosen_entry}"
        self._write_bundle_manifest(
            folder,
            kind="code_bundle",
            entry_point=chosen_entry,
            members=members,
            producer="create_code_bundle",
        )
        result = {
            "created": True,
            "kind": "code_bundle",
            "path": entry,
            "entry_point": entry,
            "files": members,
            "size_bytes": total,
            "note": (
                "The project is written and ready. The product UI lists it "
                "for download. Do not include a markdown link, URL, or "
                "workspace path in your answer, and do not recreate it. Write "
                "your final answer now unless the request asked for something "
                "else as well."
            ),
        }
        self._memo[memo_key] = result
        return result

    def _write_bundle_manifest(
        self,
        folder: str,
        *,
        kind: str,
        entry_point: str,
        members: list[dict[str, Any]],
        producer: str,
    ) -> None:
        """Write the sidecar manifest scan_artifacts uses to report a bundle
        folder as one deliverable instead of N unrelated-looking files."""
        manifest = {
            "kind": kind,
            "entry_point": entry_point,
            "producer": producer,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [member["path"] for member in members],
        }
        target = _safe_child(self.root, f"{folder}/{_MANIFEST_NAME}", must_exist=False)
        target.write_bytes(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    def list_artifacts(self) -> dict[str, Any]:
        """List the files created in this job's artifacts directory so far.

        Returns:
            Dict[str, Any]: Every artifact written during this run, with its
                path, kind and size.
        """
        return {
            "artifact_count": len(self._records),
            "artifacts": [record.as_dict() for record in self.records()],
        }

    def as_tools(self) -> list:
        return [
            # First in the list on purpose: for the common "turn this
            # spreadsheet into JSON" ask it is both the cheapest and the only
            # option that cannot truncate the data.
            self.convert_table_to_json,
            self.create_json_artifact,
            self.create_csv_artifact,
            self.create_xlsx_artifact,
            self.create_docx_artifact,
            self.create_pptx_artifact,
            self.create_pdf_artifact,
            self.create_text_artifact,
            self.create_html_artifact,
            self.create_web_bundle,
            self.create_code_bundle,
            self.list_artifacts,
        ]


def ensure_pdf_from_text(
    workspace_root: Path,
    title: str,
    body: str,
    filename: str = "report.pdf",
) -> dict[str, Any]:
    """Write a PDF into an existing job workspace from already-final text.

    Used as a backstop when the user asked for a PDF but the model never
    called create_pdf_artifact. Same writer as the tool, so the file is
    indistinguishable from one produced mid-run.
    """
    tools = ArtifactTools(Path(workspace_root))
    return tools.create_pdf_artifact(filename, title=title or "", content=body)


# Best-effort provenance for a standalone file found on disk. scan_artifacts
# has no persisted record for these (only ArtifactRecord, which lives only
# for the run that created them), so this is inferred from the file
# extension rather than known for certain -- still strictly better than
# leaving `producer` unset when the mapping is unambiguous.
_PRODUCER_BY_KIND = {
    "json": "create_json_artifact",
    "csv": "create_csv_artifact",
    "xlsx": "create_xlsx_artifact",
    "md": "create_text_artifact",
    "markdown": "create_text_artifact",
    "txt": "create_text_artifact",
    "html": "create_html_artifact",
    "docx": "create_docx_artifact",
    "pptx": "create_pptx_artifact",
    "pdf": "create_pdf_artifact",
}


def _standalone_entry(path: Path, workspace_root: Path) -> dict[str, Any]:
    relative = path.relative_to(workspace_root).as_posix()
    suffix = path.suffix.lower().lstrip(".")
    stat = path.stat()
    entry: dict[str, Any] = {
        "path": relative,
        "filename": path.name,
        "kind": suffix or "file",
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }
    producer = _PRODUCER_BY_KIND.get(suffix)
    if producer:
        entry["producer"] = producer
    return entry


def _scan_bundle_dir(folder: Path, workspace_root: Path) -> list[dict[str, Any]]:
    """Report one bundle folder as either a single grouped entry (manifest
    present) or the old flat per-file listing (no manifest -- a bundle
    written before this file gained manifests, or a folder a model wrote
    some other way)."""
    manifest_path = folder / _MANIFEST_NAME
    manifest: Optional[dict[str, Any]] = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("files"), list):
                manifest = loaded
        except (OSError, ValueError):
            manifest = None

    if manifest is not None:
        members: list[dict[str, Any]] = []
        total = 0
        for rel in manifest["files"]:
            if not isinstance(rel, str):
                continue
            member_path = workspace_root / rel
            try:
                member_path.resolve(strict=True).relative_to(folder.resolve(strict=True))
            except (ValueError, OSError):
                continue
            if member_path.is_symlink() or not member_path.is_file():
                continue
            size = member_path.stat().st_size
            members.append({"path": rel, "filename": member_path.name, "size_bytes": size})
            total += size
        # Disk truth: a manifest describing files that are no longer there is
        # not a deliverable. Fall through to the flat listing of whatever
        # actually remains instead of reporting a bundle with nothing in it.
        if members:
            folder_rel = folder.relative_to(workspace_root).as_posix()
            entry_point = manifest.get("entry_point")
            entry_full = f"{folder_rel}/{entry_point}" if entry_point else None
            entry_exists = any(m["path"] == entry_full for m in members)
            created_at = manifest.get("created_at")
            if not created_at:
                created_at = datetime.fromtimestamp(
                    folder.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            return [
                {
                    "path": entry_full if entry_exists else members[0]["path"],
                    "filename": Path(
                        entry_full if entry_exists else members[0]["path"]
                    ).name,
                    "kind": manifest.get("kind", "bundle"),
                    "size_bytes": total,
                    "entry_point": entry_full if entry_exists else None,
                    "producer": manifest.get("producer"),
                    "created_at": created_at,
                    "bundle": folder_rel,
                    "files": members,
                }
            ]

    # No usable manifest: fall back to the pre-manifest flat walk, scoped to
    # this folder, so bundles created before this file gained manifests (or
    # any other directory a model wrote) still get listed.
    fallback: list[dict[str, Any]] = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name == _MANIFEST_NAME:
            continue
        try:
            path.resolve(strict=True).relative_to(folder.resolve(strict=True))
        except (ValueError, OSError):
            continue
        entry = _standalone_entry(path, workspace_root)
        if path.name == BUNDLE_INDEX:
            entry["kind"] = "web_bundle"
            entry["entry_point"] = entry["path"]
            entry.pop("producer", None)
        fallback.append(entry)
    return fallback


def scan_artifacts(workspace_root: str | Path) -> list[dict[str, Any]]:
    """List artifacts on disk for a job, independent of the tool instance.

    The ArtifactTools instance lives only for the duration of a run, but a
    job's artifacts must still be listable afterwards (job snapshot, reload,
    restart). This walks the artifacts directory instead, so what the API
    reports is what is actually on disk -- an artifact the model *claims* to
    have created but that is not here will simply not be listed.

    Top-level files are reported individually. Top-level directories (a
    bundle written by create_web_bundle/create_code_bundle) are reported as
    ONE entry each, grouped via that bundle's manifest -- see
    _scan_bundle_dir -- so a project of N files does not look like N
    unrelated deliverables.
    """
    root = Path(workspace_root) / ARTIFACTS_DIRNAME
    if not root.is_dir():
        return []
    workspace = Path(workspace_root)
    found: list[dict[str, Any]] = []
    limit = MAX_ARTIFACTS_PER_JOB * 4
    for entry_path in sorted(root.iterdir()):
        if len(found) >= limit:
            break
        if entry_path.is_symlink():
            continue
        if entry_path.is_file():
            found.append(_standalone_entry(entry_path, workspace))
            continue
        if entry_path.is_dir():
            # Intermediate scrape output (web_fetch.EXTRACTED_DIRNAME) lives
            # under the artifacts tree so it is confined and cleaned up with
            # the job, but it is working data, not something the user asked
            # for. Listing it would put "page-tables.json" in the
            # deliverables next to the Excel file the user actually
            # requested.
            if entry_path.name == INTERNAL_DIRNAME:
                continue
            found.extend(_scan_bundle_dir(entry_path, workspace)[: limit - len(found)])
    return found


# Reuse the same caps create_code_bundle/create_web_bundle already enforce at
# write time, rather than inventing a second, contradictory ceiling for the
# read-time zip path.
MAX_ZIP_MEMBERS = MAX_BUNDLE_FILES
MAX_ZIP_UNCOMPRESSED_BYTES = MAX_ARTIFACTS_TOTAL_BYTES


@dataclass(frozen=True)
class BundleMember:
    """One real file that will go into a bundle's zip, as verified on disk."""

    relative: str  # path within the bundle, e.g. "data/prayer-times.json"
    absolute: Path
    size_bytes: int


@dataclass(frozen=True)
class ResolvedBundle:
    """A bundle folder, re-verified against disk, ready to be zipped."""

    folder_name: str
    kind: str
    entry_point: Optional[str]
    producer: Optional[str]
    members: list[BundleMember]
    total_bytes: int


def resolve_bundle_for_zip(workspace_root: str | Path, bundle_path: str) -> ResolvedBundle:
    """Resolve a bundle folder for a trusted, disk-verified ZIP download.

    `bundle_path` must name a direct child of the job's artifacts/ directory
    that carries a real .artifact-manifest.json. That manifest -- written
    server-side by create_web_bundle/create_code_bundle, never by the model
    directly -- is what lets this function trust the path enough to zip it:
    an arbitrary directory a caller points at (no manifest) is rejected, so
    this can never be used to archive something other than a real bundle.

    Every member is re-checked against disk (existence, containment, not a
    symlink) before being included -- the manifest describes what was
    written, not what is guaranteed to still be there or safe to read.
    Symlinked members are always skipped, never followed, whether or not
    they would resolve inside the bundle: a bundle is never expected to
    contain one, so there is no behavior worth preserving for it.
    """
    workspace = Path(workspace_root)
    artifacts_root = workspace / ARTIFACTS_DIRNAME
    try:
        folder = _safe_child(artifacts_root, bundle_path, must_exist=True)
    except WorkspaceSecurityError:
        raise
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"{bundle_path!r} could not be resolved ({exc})") from exc

    if not folder.is_dir():
        raise ArtifactError(f"{bundle_path!r} is not a bundle directory")
    try:
        artifacts_root_resolved = artifacts_root.resolve(strict=True)
    except OSError as exc:
        raise ArtifactError(f"artifacts directory is unavailable ({exc})") from exc
    if folder.parent != artifacts_root_resolved:
        # Bundles are always a direct child of artifacts/ -- never nested --
        # so this rejects e.g. pointing at a bundle's own data/ subfolder.
        raise ArtifactError(f"{bundle_path!r} is not a top-level bundle")

    manifest_path = folder / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ArtifactError(
            f"{bundle_path!r} has no artifact manifest; it is not a downloadable bundle"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"{bundle_path!r} has an unreadable manifest ({exc})") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ArtifactError(f"{bundle_path!r} has an invalid manifest")

    folder_resolved = folder.resolve(strict=True)
    members: list[BundleMember] = []
    total = 0
    for rel in manifest["files"]:
        if not isinstance(rel, str):
            continue
        member_path = workspace / rel
        if member_path.is_symlink():
            continue
        try:
            resolved = member_path.resolve(strict=True)
            bundle_relative = resolved.relative_to(folder_resolved).as_posix()
        except (OSError, ValueError):
            # Gone since the manifest was written, or -- if this ever
            # happens -- resolves outside the bundle. Either way: disk
            # truth wins, and it is simply not included.
            continue
        if not resolved.is_file():
            continue
        size = resolved.stat().st_size
        members.append(BundleMember(bundle_relative, resolved, size))
        total += size
        if len(members) > MAX_ZIP_MEMBERS:
            raise ArtifactError(
                f"bundle {bundle_path!r} has more than {MAX_ZIP_MEMBERS} files; "
                "too large to zip."
            )
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ArtifactError(
                f"bundle {bundle_path!r} is larger than the "
                f"{MAX_ZIP_UNCOMPRESSED_BYTES}-byte zip limit."
            )

    if not members:
        raise ArtifactError(f"{bundle_path!r} has no files left to download")

    entry_point = manifest.get("entry_point")
    producer = manifest.get("producer")
    return ResolvedBundle(
        folder_name=folder.name,
        kind=str(manifest.get("kind") or "bundle"),
        entry_point=entry_point if isinstance(entry_point, str) else None,
        producer=producer if isinstance(producer, str) else None,
        members=members,
        total_bytes=total,
    )


def build_bundle_zip(bundle: ResolvedBundle) -> bytes:
    """Serialize a resolved bundle into ZIP bytes, in memory.

    Bundles are bounded (MAX_ZIP_MEMBERS files, MAX_ZIP_UNCOMPRESSED_BYTES
    total) well below anything worth streaming to a temp file for, so an
    in-memory archive keeps this endpoint stateless -- nothing is written to
    disk beyond what create_web_bundle/create_code_bundle already produced.
    Paths inside the archive are rooted at the bundle's own folder name, so
    extracting the zip reproduces the same tree the tool wrote.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in bundle.members:
            arcname = f"{bundle.folder_name}/{member.relative}"
            archive.write(member.absolute, arcname=arcname)
    return buffer.getvalue()


__all__ = [
    "ArtifactError",
    "ArtifactRecord",
    "ArtifactTools",
    "ARTIFACTS_DIRNAME",
    "MAX_ARTIFACTS_PER_JOB",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACT_ROWS",
    "MAX_PAYLOAD_CHARS",
    "scan_artifacts",
    "usable_workspace_path",
    "resolve_bundle_for_zip",
    "build_bundle_zip",
    "ResolvedBundle",
    "BundleMember",
    "MAX_ZIP_MEMBERS",
    "MAX_ZIP_UNCOMPRESSED_BYTES",
]
