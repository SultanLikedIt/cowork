# Document ingestion

Uploaded PDF, DOCX, PPTX, XLSX, and HTML documents are converted internally by
Docling. This path is independent from Scrapy web acquisition and is not
exposed as a model-facing tool.

For each upload, the backend persists a reusable `.docling/` directory:

- `canonical.json`: lossless Docling document model and source provenance
- `manifest.json`: bounded headings, page/slide count, and reference paths
- `content.md`: clean document text for bounded Files previews/search
- `chunks.jsonl`: hierarchical Docling chunks
- `tables/*.csv`: deterministic table references usable by existing table tools

The Files abstraction copies these references into each job workspace. It does
not place canonical JSON or complete large documents in model context, and a
source SHA-256 cache prevents unnecessary reparsing. Artifact generation stays
separate.

Bounds are configurable with `DOCLING_PARSE_TIMEOUT_SECONDS`,
`DOCLING_MAX_FILE_BYTES`, `DOCLING_MAX_PAGES`, `DOCLING_MAX_TABLES`, and
`DOCLING_MAX_CHUNKS`. `DOCLING_ENABLE_OCR` defaults to `false`; enable it only
when OCR model assets and the additional processing cost are acceptable.

## Model artifacts (`vendor/docling-models/`)

`document_ingestion.py` points Docling's layout/table pipeline at
`backend/vendor/docling-models/` when that directory exists (overridable with
`DOCLING_ARTIFACTS_PATH`, which `docker-compose.yml` sets to
`/app/vendor/docling-models`). This directory is a downloaded cache of the
`docling-project/docling-layout-heron` and `docling-project/docling-models`
Hugging Face repos (~500 MB) and is **not** committed to git -- it is a
deterministic download, not reviewed source, and too large for the repo.

Fetch it once per machine/image build with Docling's own CLI (installed as
part of `requirements.txt`):

```bash
docling-tools models download --output-dir backend/vendor/docling-models
```

If the directory is absent, Docling falls back to downloading models from
Hugging Face on first use, which requires outbound network access at runtime.
That fallback holds even when `DOCLING_ARTIFACTS_PATH` points somewhere
unusable: Docling reads that variable itself and validates it before our
pipeline options apply, so `_resolve_artifacts_path()` clears it rather than
letting every upload fail with `docling_conversion_failed`. Since
`docker-compose.yml` always sets the variable, this is the ordinary path on a
fresh clone, not an edge case.

For air-gapped deployments, run the command above before building the backend
image so the models are baked in via the existing `COPY . .` step in
`backend/Dockerfile` — that removes the runtime download entirely.

Malformed, unsupported, oversized, timed-out, and conversion failures return a
bounded structured reason.
