# Carbone-backed DOCX/PPTX artifacts

`create_docx_artifact` and `create_pptx_artifact` (in `backend/artifacts.py`)
render through [Carbone Community Edition](https://carbone.io) instead of
hand-written openpyxl/python-docx-style code. This followed a spike
(`spikes/carbone/`, now superseded by this production integration -- see its
`docs/archive/NOTES.md` for the full PARTIAL-ADOPT evaluation) that found Carbone produces
professional, fully editable DOCX/PPTX output but renders spreadsheet
numbers as text cells, not real numeric cells. **XLSX/CSV/JSON therefore
stay on their existing native writers unchanged.**

## Architecture

```
create_docx_artifact / create_pptx_artifact   (backend/artifacts.py)
        |  build_table_payload(title, summary, header, rows)
        v
backend/carbone_render.py  (Python bridge: runtime checks, subprocess, cleanup)
        |  one bounded `node` subprocess per render, data via temp JSON file
        v
backend/carbone/render.js  (thin CLI: carbone.render(template, data) -> bytes)
        |
        v
backend/carbone/templates/report.{docx,pptx}   (maintained templates)
```

The model never calls Carbone directly -- it calls `create_docx_artifact`/
`create_pptx_artifact` exactly like every other `create_*_artifact` tool.
`artifacts.py` decides which renderer backs which extension.

### Why a subprocess, not a sidecar service

Carbone is Node-only; the backend is Python. A bounded, short-lived `node`
subprocess per render (no persistent process, no port, no health check) is
materially simpler than standing up and operating a second service for a
narrowly-scoped renderer invoked a handful of times per job. There is no
throughput or latency requirement here that a sidecar would justify.

## Runtime layout

- `backend/carbone/package.json` + `node_modules/` (installed via
  `npm install --omit=dev` in `backend/carbone/`) -- the `carbone` npm
  package itself (~3 MB, pure JS, no native deps).
- `backend/carbone/render.js` -- the CLI subprocess entry point.
  `--template`/`--data`/`--output` are always file **paths**; the JSON
  payload always goes through a temp file, never the command line or a
  prompt, so there is no argv/token size limit for large tables.
- `backend/carbone/templates/report.docx` and `report.pptx` -- the two
  maintained templates, committed to the repo (see below to regenerate).
- `backend/carbone_render.py` -- the only Python entry point. Owns runtime
  checks (`node` on PATH, `render.js` present, `node_modules/carbone`
  installed, template present), the subprocess call (timeout, bounded
  stderr, guaranteed temp-dir cleanup), and shaping tabular data into the
  templates' generic payload.

## The generic table shape

Both templates use the same bounded, generic contract so one template
serves any `create_docx_artifact`/`create_pptx_artifact` call regardless of
the caller's actual column names:

```json
{
  "title": "...",
  "generated_at": "2026-08-14",
  "summary": "...",
  "footnote": "",
  "columns": {"c0": "Region", "c1": "Revenue", "c2": "", "...": "c7"},
  "rows": [
    {"cells": {"c0": "EMEA", "c1": "125000", "c2": "", "...": "c7"}}
  ]
}
```

- Up to `carbone_render.MAX_TABLE_COLUMNS` (8) columns; extra columns are
  dropped with a note pointing at `create_xlsx_artifact`/
  `create_csv_artifact` for the full data.
- Up to `MAX_DOCX_TABLE_ROWS` (500) or `MAX_PPTX_TABLE_ROWS` (20) rows --
  PPTX is bounded much tighter because a slide table cannot paginate the way
  a Word table can.

## Two non-obvious Carbone quirks this design works around

1. **Row/element repetition needs TWO templated rows.** A single
   `{d.rows[i].field}` row/paragraph is *silently* not repeated. Carbone
   detects the repeat boundary by finding two occurrences of the same array
   (`[i]` and `[i+1]`) and replicating the XML between them once per data
   element. This is not documented in the marker syntax -- it was found by
   inspecting Carbone's own bundled `examples/movies.docx`
   (`{d[i+1].movieName}`). **Every template's data table therefore has two
   templated rows** (see `backend/carbone/build_templates.py`). Any new
   Carbone template must follow the same convention or row repetition will
   silently produce only one row.

2. **Fixed numeric array indexing does not work.** `{d.columns[0]}` or
   `{d.rows[i].cells[0]}` (a literal index, not a loop variable) either
   renders empty, or -- when mixed with a real repeated array in the same
   document -- throws `Cannot read properties of undefined (reading
   'position')` and the whole render fails. `[i]`/`[i+1]` is reserved for
   loop-repetition detection only. The working alternative, used throughout,
   is **named object keys** (`c0`, `c1`, ... `c7`) instead of numeric
   indices.

## Regenerating the templates

```bash
source backend/venv/bin/activate
python backend/carbone/build_templates.py
```

## Failure modes (all bounded, all `ArtifactError`/`CarboneError`)

| Condition | reason code |
|---|---|
| `node` not on PATH | `carbone_runtime_missing` |
| `render.js` missing | `carbone_runtime_missing` |
| `node_modules/carbone` not installed | `carbone_runtime_missing` |
| template file missing | `carbone_template_missing` |
| render exceeds `CARBONE_TIMEOUT_SECONDS` (default 20s) | `carbone_timeout` |
| Carbone itself errors (bad template/data) | `carbone_render_failed` |
