# Document ingestion

Uploaded PDF, DOCX, PPTX, XLSX, HTML, and image (PNG, JPG, TIFF, BMP, WEBP)
documents are converted internally by Docling. This path is independent from Scrapy web acquisition and is not
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
`DOCLING_MAX_CHUNKS`.

## OCR (scanned PDFs and images)

Scanned PDFs and photos have no text layer, so without OCR they parse to
nothing. OCR is controlled by:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOCLING_ENABLE_OCR` | `true` in compose, `false` in code | Master switch |
| `OCR_ENGINE` | `tesseract` | `tesseract`, `easyocr`, or `rapidocr` |
| `OCR_LANGS` | `tur,eng` | Comma- or `+`-separated, in the engine's own codes (Tesseract `tur`, EasyOCR `tr`) |
| `OCR_FORCE_FULL_PAGE` | `false` | `true` OCRs every page, not just bitmap regions |

Tesseract is installed in the backend image (`tesseract-ocr`,
`tesseract-ocr-tur`) and needs no model download. EasyOCR and RapidOCR are
not installed and would fetch weights on first use. When OCR is on, Docling
only OCRs bitmap regions, so text-bearing PDFs keep their native text and do not
get slower. Budget a few seconds per scanned page, which is why compose raises
`DOCLING_PARSE_TIMEOUT_SECONDS` to 300.

- An image upload while OCR is disabled fails with the `ocr_disabled` reason
  rather than parsing to empty text.
- `manifest.json` records the `ocr` settings used. A cached PDF or image is
  reparsed when those settings change.
- `manifest.json` sets `text_empty: true` when no readable text came out (a blank
  or unreadable scan), so the agent can say so instead of guessing.

Running the backend on the host: install Tesseract with Turkish data yourself
(Windows: UB Mannheim installer, and select Turkish; Debian/Ubuntu: `apt install
tesseract-ocr tesseract-ocr-tur`) and make sure `tesseract` is on `PATH`.

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
That fallback has to be forced when `DOCLING_ARTIFACTS_PATH` points somewhere
unusable, and clearing the environment variable is **not** enough to do it.
Docling's global settings object reads that variable once, when
`docling.datamodel.settings` is imported, and `BasePipeline` falls back to the
captured value whenever `pipeline_options.artifacts_path` is `None`. It then
fails validation on the missing directory, so every upload -- PDF, image and
DOCX alike -- returned `docling_conversion_failed`. `_resolve_artifacts_path()`
therefore resets `settings.artifacts_path` as well as popping the env var.

Since `docker-compose.yml` always sets the variable and
`backend/vendor/docling-models/` is not in git, this is the ordinary path on a
fresh clone, not an edge case. Measured in the container on that path: a DOCX
converts immediately, and the first PDF takes ~45 s while the layout models
download (later ones are fast). Pre-downloading the models as described above
removes that first-use delay and the network dependency.

For air-gapped deployments, run the command above before building the backend
image so the models are baked in via the existing `COPY . .` step in
`backend/Dockerfile` — that removes the runtime download entirely.

Malformed, unsupported, oversized, timed-out, and conversion failures return a
bounded structured reason.
