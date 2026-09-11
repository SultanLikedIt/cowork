"""Bounded Node subprocess bridge to Carbone Community Edition.

Renders the two document formats openpyxl/python-docx cannot produce well
(a proper PPTX, and a nicer-than-hand-rolled DOCX) using Carbone as a
narrowly-scoped, specialized renderer -- see the spike report under
spikes/carbone/ for why XLSX deliberately stays on the native openpyxl path
(Carbone renders spreadsheet numbers as text cells, not real numeric cells).

This module owns exactly three things: checking the runtime is available,
invoking one bounded `node` subprocess per render, and shaping (title,
summary, tabular rows) into the generic payload backend/carbone/templates/
expects. It is not a service -- there is no persistent process, no port, and
no API surface beyond the two functions below. artifacts.py is the only
caller; the model never sees Carbone directly.

Carbone's array-repetition convention
--------------------------------------
A single templated table row/paragraph is *silently* NOT repeated per data
element. Carbone finds the repeat boundary by locating TWO occurrences of
the same array marker (`[i]` and `[i+1]`) and replicating the XML between
them once per element -- discovered by inspecting Carbone's own bundled
`examples/movies.docx`, not documented in the marker syntax itself. Every
template under backend/carbone/templates/ therefore has two templated data
rows. Any future Carbone template MUST follow the same convention (see
backend/carbone/build_templates.py) or row repetition will silently render
only one row.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

CARBONE_DIR = Path(__file__).parent / "carbone"
RENDER_SCRIPT = CARBONE_DIR / "render.js"
TEMPLATES_DIR = CARBONE_DIR / "templates"
_NODE_MODULES_MARKER = CARBONE_DIR / "node_modules" / "carbone" / "package.json"

TEMPLATE_FOR_KIND: dict[str, Path] = {
    "docx": TEMPLATES_DIR / "report.docx",
    "pptx": TEMPLATES_DIR / "report.pptx",
}

# Must match backend/carbone/build_templates.py's MAX_TABLE_COLUMNS: the
# templates have exactly this many `cells[N]` positions per row.
MAX_TABLE_COLUMNS = 8
# A Word table can span pages, so this is generous; a slide table cannot, so
# PPTX gets a much tighter bound -- past this point the request wants XLSX/
# CSV, not a presentation slide.
MAX_DOCX_TABLE_ROWS = 500
MAX_PPTX_TABLE_ROWS = 20

CARBONE_TIMEOUT_SECONDS = int(os.getenv("CARBONE_TIMEOUT_SECONDS", "20"))
# Bounds how much of a Node stderr blob ever reaches an error message/model
# context -- a stack trace is not useful past its first lines and must not
# become a second, unbounded payload channel.
MAX_STDERR_CHARS = 2_000


class CarboneError(RuntimeError):
    """A stable, structured Carbone failure safe to surface to the caller.

    Mirrors document_ingestion.DocumentParseError's shape: a short `reason`
    code the caller can branch on, plus a bounded, single-line `message`.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = " ".join(str(message or "Carbone render failed.").split())[:1_000]
        super().__init__(f"{reason}: {self.message}")


_node_binary_cache: Optional[str] = None


def _node_binary() -> str:
    global _node_binary_cache
    if _node_binary_cache is None:
        _node_binary_cache = shutil.which("node") or ""
    return _node_binary_cache


def _check_runtime(kind: str) -> Path:
    """Fail fast and clearly if any prerequisite for `kind` is missing.

    Checked in dependency order (runtime, then script, then package, then
    template) so the error names the actual missing piece instead of a
    generic subprocess failure a caller cannot act on.
    """
    template = TEMPLATE_FOR_KIND.get(kind)
    if template is None:
        raise CarboneError("carbone_unsupported_kind", f"No Carbone renderer is registered for {kind!r}.")
    if not _node_binary():
        raise CarboneError(
            "carbone_runtime_missing",
            "Node.js ('node') was not found on PATH. DOCX/PPTX generation "
            "via Carbone is unavailable in this deployment.",
        )
    if not RENDER_SCRIPT.is_file():
        raise CarboneError(
            "carbone_runtime_missing", f"Carbone render script is missing ({RENDER_SCRIPT})."
        )
    if not _NODE_MODULES_MARKER.is_file():
        raise CarboneError(
            "carbone_runtime_missing",
            "The carbone npm package is not installed under "
            "backend/carbone/node_modules. Run `npm install --omit=dev` in "
            "backend/carbone/.",
        )
    if not template.is_file():
        raise CarboneError(
            "carbone_template_missing", f"No Carbone template is available for {kind!r} ({template})."
        )
    return template


def render(kind: str, data: dict[str, Any]) -> bytes:
    """Render `data` against the `kind` ("docx"/"pptx") template.

    Runs exactly one bounded, short-lived `node` subprocess -- no persistent
    process, no network port. `data` goes to a temp JSON file, never onto
    the command line, so there is no argv size limit to trip on. The temp
    directory (data file + rendered output) is always cleaned up, on both
    success and failure.
    """
    template = _check_runtime(kind)
    with tempfile.TemporaryDirectory(prefix="carbone-render-") as tmp:
        tmp_path = Path(tmp)
        data_path = tmp_path / "data.json"
        output_path = tmp_path / f"output.{kind}"
        data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        try:
            completed = subprocess.run(
                [
                    _node_binary(),
                    str(RENDER_SCRIPT),
                    "--template",
                    str(template),
                    "--data",
                    str(data_path),
                    "--output",
                    str(output_path),
                ],
                cwd=str(CARBONE_DIR),
                capture_output=True,
                timeout=CARBONE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CarboneError(
                "carbone_timeout",
                f"Carbone did not finish rendering within {CARBONE_TIMEOUT_SECONDS}s.",
            ) from exc
        except OSError as exc:
            raise CarboneError("carbone_runtime_missing", f"Could not start the Node renderer ({exc}).") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            raise CarboneError(
                "carbone_render_failed",
                (stderr or f"Carbone exited with status {completed.returncode}.")[:MAX_STDERR_CHARS],
            )
        if not output_path.is_file():
            raise CarboneError("carbone_render_failed", "Carbone reported success but produced no output file.")
        return output_path.read_bytes()


def _column_key(index: int) -> str:
    return f"c{index}"


def build_table_payload(
    *,
    title: str,
    summary: str,
    generated_at: str,
    header: list[str],
    rows: list[list[Any]],
    kind: str,
) -> tuple[dict[str, Any], list[str]]:
    """Shape (title, summary, tabular rows) into the generic template payload.

    The templates address cells by fixed, named keys (`c0`..`c7`) rather
    than the caller's actual business field names, so one template serves
    any caller regardless of column names -- unused trailing cells render
    empty. Named keys, not numeric bracket indexing (`cells[0]`): Carbone
    reserves `[i]`/`[i+1]` for loop-repetition detection, and a literal
    numeric index either renders empty or (mixed with a real repeated
    array) throws -- see this module's docstring and
    backend/carbone/build_templates.py.

    This mirrors the header/rows shape create_csv_artifact/
    create_xlsx_artifact already normalize with `_normalize_rows`, so
    callers reuse it unchanged.

    Returns (payload, notes) -- notes describes any column/row truncation so
    the caller can surface it rather than silently drop data.
    """
    notes: list[str] = []
    max_rows = MAX_PPTX_TABLE_ROWS if kind == "pptx" else MAX_DOCX_TABLE_ROWS

    header = header[:MAX_TABLE_COLUMNS]
    if len(header) > MAX_TABLE_COLUMNS:
        notes.append(
            f"Only the first {MAX_TABLE_COLUMNS} of {len(header)} columns are "
            f"shown in the {kind.upper()} table (a narrative document, not a "
            "raw data dump) -- use create_xlsx_artifact or create_csv_artifact "
            "for the full column set."
        )
    columns = {_column_key(i): str(header[i]) if i < len(header) else "" for i in range(MAX_TABLE_COLUMNS)}

    truncated_rows = rows[:max_rows]
    if len(rows) > max_rows:
        notes.append(
            f"Only the first {max_rows} of {len(rows)} rows are shown in the "
            f"{kind.upper()} table -- use create_xlsx_artifact or "
            "create_csv_artifact for the complete data."
        )

    payload_rows = []
    for row in truncated_rows:
        values = ["" if value is None else str(value) for value in row[:MAX_TABLE_COLUMNS]]
        cells = {_column_key(i): values[i] if i < len(values) else "" for i in range(MAX_TABLE_COLUMNS)}
        payload_rows.append({"cells": cells})

    payload = {
        "title": title or "Untitled report",
        "generated_at": generated_at,
        "summary": summary or "",
        "footnote": "" if not notes else " ".join(notes),
        "columns": columns,
        "rows": payload_rows,
    }
    return payload, notes


__all__ = [
    "CarboneError",
    "MAX_DOCX_TABLE_ROWS",
    "MAX_PPTX_TABLE_ROWS",
    "MAX_TABLE_COLUMNS",
    "build_table_payload",
    "render",
]
