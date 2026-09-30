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
| `OCR_ENGINE` | `paddleocr_vl` | `paddleocr_vl`, `tesseract`, `easyocr`, or `rapidocr` |
| `OCR_FALLBACK_ENGINE` | `tesseract` | Docling engine used when `paddleocr_vl` is unavailable or fails |
| `OCR_LANGS` | `tur,eng` | Languages for the Docling engine, in its own codes (Tesseract `tur`, EasyOCR `tr`). PaddleOCR-VL takes none |
| `OCR_FORCE_FULL_PAGE` | `false` | Docling engines: `true` OCRs every page, not just bitmap regions |
| `PADDLEOCR_VL_URL` | `http://paddleocr-vl-api:8080` in compose, unset in code | Pipeline service; unset means "always fall back" |
| `PADDLEOCR_VL_TIMEOUT_SECONDS` | `240` | Whole-document read timeout; keep below `DOCLING_PARSE_TIMEOUT_SECONDS` |
| `PADDLEOCR_VL_CONNECT_TIMEOUT_SECONDS` | `5` | How fast a stopped service is detected |
| `PADDLEOCR_VL_ORIENTATION` / `PADDLEOCR_VL_UNWARPING` | `true` / `false` | Fix rotated scans / curled phone photos |
| `PDF_TEXT_LAYER_MIN_CHARS` | `20` | A PDF page with at least this many non-whitespace text-layer characters keeps Docling's native text instead of going to PaddleOCR-VL |

### PaddleOCR-VL (default)

[PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) is a
0.9B vision-language document parser; Turkish is in its 109 supported
languages (technical report, arXiv 2510.14528, Appendix). It is not a Docling
OCR plugin -- those return text lines with boxes, while PaddleOCR-VL detects
layout blocks (PP-DocLayoutV3) and reads each block (text, HTML tables,
formulas). So it runs as two compose services under the `paddleocr` profile:

| Service | What | Where |
| --- | --- | --- |
| `paddleocr-vl-api` | PaddleX pipeline app, `POST /layout-parsing`; layout detection on CPU | built from `paddleocr-vl/` (PyPI + Hugging Face) |
| `paddleocr-vlm-server` | PaddleOCR-VL-1.6 on vLLM, OpenAI-compatible | `vllm/vllm-openai` on the GPU |

`backend/paddleocr_vl.py` posts the upload, then rebuilds the per-page blocks
as a `DoclingDocument`: `doc_title` -> title, `paragraph_title` -> heading,
tables (HTML, row/col spans kept) -> tables, header/footer/page number ->
furniture (not in `content.md`), unknown labels -> body text. Page numbers
and block boxes are kept as provenance. Everything downstream -- `canonical.json`,
`content.md`, chunks, table CSVs -- is unchanged.

**Routing (hybrid)** -- `document_ingestion._parse_with_paddleocr_vl`:

| Upload | Parsed by | `manifest.json` -> `parser.engine` |
| --- | --- | --- |
| Image | PaddleOCR-VL | `paddleocr_vl` |
| PDF, every page has a text layer | Docling native text (Tesseract only for bitmap regions) | `docling` |
| PDF, no page has a text layer (scan) | PaddleOCR-VL | `paddleocr_vl` |
| PDF, mixed | Consecutive runs of pages split out; text runs -> Docling, scanned runs -> PaddleOCR-VL; rejoined in page order | `hybrid` |

`parser.paddleocr_vl_pages` lists which pages (1-based) PaddleOCR-VL read.

### Measured quality (2026-09-17, RTX A1000)

Synthetic Turkish documents (Arial, 150 dpi, skew + blur + noise + JPEG),
word recall against ground truth:

| Document | PaddleOCR-VL-1.6 | Docling + Tesseract |
| --- | --- | --- |
| Scanned invoice with a table (image) | **92%** words, table **18/20** exact cells, ~5 s | 63% words, table 2/20 (all numbers lost), ~5 s |
| Scanned 2-page report, plain paragraphs | 93-94%, ~5 s | **100%**, ~3 s |
| Digital 2-page PDF | 94% when forced through it | **100%** (text layer), ~1.5 s -- hence the routing above |
| Mixed PDF (digital / scanned invoice / digital) | **97%**, table 19/20, pages 1-2-3 kept (`hybrid`) | 81%, table 0/20 |

**Known weakness -- Turkish letters.** PaddleOCR-VL-1.6 systematically reads
dotless `ı` as `i` and sometimes `ğ` as `g`, `Ö` as `Ó`, or doubles a letter:
`Satıcı` -> `Satici`, `Sonrası` -> `Sonrasi`, `sınıflandırılmıştır` ->
`siniflandırılmıştır`, `belediyenin` -> `belediyeenin`. Higher
`maxPixels`/`minPixels` (3-4x slower) and `temperature=0` did not change it, so
it is the model, not the settings. Its strength is layout and tables; on
plain-paragraph scans Tesseract spells Turkish better. A Tesseract
cross-check for diacritics was considered and deferred until measured on real
customer scans.

### Requirements and setup

NVIDIA GPU with compute capability >= 8.0 (RTX 30xx/40xx, A-series) and a
driver new enough for the vLLM image's CUDA version (tested: driver 597.06,
CUDA 13.2; 552.22 / CUDA 12.4 was too old). VRAM in use: ~4.8 GB of 8 GB at
`PADDLEOCR_VLM_GPU_MEMORY_UTILIZATION=0.6`. The official Baidu images
(`ccr-...baidubce.com`) and Paddle pip index are not reachable from our
network, which is why `paddleocr-vl/` builds from PyPI instead.

```powershell
# .env: COMPOSE_PROFILES=paddleocr
docker compose up -d --build
docker compose logs -f paddleocr-vlm-server   # first start: ~2 GB weights, ~7 min
```

`paddleocr-vl/pipeline_config.yaml` is mounted into the container: change it,
then `docker compose up -d paddleocr-vl-api` (no rebuild).

### Docker Desktop on Windows (WSL2) -- settings that are required

Each of these was a live failure on this stack; the fixes are already in
`docker-compose.yml` / `pipeline_config.yaml`. On native Linux the first two
can be reverted for more throughput.

| Symptom | Cause | Fix |
| --- | --- | --- |
| vLLM crash-loops at start: `RuntimeError: UVA is not available` | vLLM reports no pinned memory under WSL; its V2 model runner requires it | `VLLM_USE_V2_MODEL_RUNNER=0` |
| First real request kills the engine: `torch.AcceleratorError: CUDA error: unknown error` (`synchronize_input_prep`) | Async scheduling's non-blocking copies assume pinned memory | `--no-async-scheduling` |
| Every request: `the input params for model settings are invalid!` | Backend sends orientation/unwarping flags; PaddleX rejects them unless the preprocessor was loaded | `use_doc_preprocessor: True` |
| ~40% of PDF requests fail instantly: `Exception from the 'cv' worker: std::exception` | Race in PaddleX background queues when a request follows another | `use_queues: False` (0/15 failures after, same latency) |

### Fallback

If PaddleOCR-VL is preferred but `PADDLEOCR_VL_URL` is unset, the service is
unreachable, times out, returns an error or a malformed body, or returns fewer
pages than the PDF has, the same upload is parsed by Docling with
`OCR_FALLBACK_ENGINE` and the upload still succeeds. The reason is logged and
recorded as `manifest.json` -> `parser.fallback_reason` (`not_configured`,
`unavailable`, `timeout`, `service_error`, `bad_response`,
`incomplete_result`, `conversion_failed`, `too_many_pages`, `unreadable_pdf`). A fallback parse is
never served from cache, so the next parse retries the service.

The page-count check matters: PaddleX serving stops after
`Serving.extra.max_num_input_imgs` pages (10 unless set) and still reports
success. `paddleocr-vl/pipeline_config.yaml` sets it to `null`.

### Tesseract (fallback engine)

Tesseract is installed in the backend image (`tesseract-ocr`,
`tesseract-ocr-tur`) and needs no model download. EasyOCR and RapidOCR are
not installed and would fetch weights on first use. When OCR is on, Docling
only OCRs bitmap regions, so text-bearing PDFs keep their native text and do not
get slower. Budget a few seconds per scanned page, which is why compose raises
`DOCLING_PARSE_TIMEOUT_SECONDS` to 300.

- An image upload while OCR is disabled fails with the `ocr_disabled` reason
  rather than parsing to empty text.
- `manifest.json` records the `ocr` settings used and, under `parser`, which
  parser actually ran. A cached PDF or image is reparsed when those settings
  change.
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

**The backend image already contains them**: `backend/Dockerfile` runs the
download at build time, above `COPY . .` so that editing Python source does
not re-fetch them. Nothing to do for the Docker path.

To populate the directory by hand (running the backend on the host, or
pre-seeding a build context), use Docling's own CLI, which `requirements.txt`
installs:

```bash
docling-tools models download layout tableformer --output-dir backend/vendor/docling-models
```

Name those two models explicitly. Plain `docling-tools models download` grabs
a default set that also pulls RapidOCR weights from `modelscope.cn` -- that
host can time out and take the whole command (or a Docker build) down with it,
and the extra models it fetches are never loaded by this pipeline: 1.2GB
instead of 506MB.

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

This matters whenever the directory is missing -- an image built before the
Dockerfile baked the models in, a host run without the CLI download, or a
build that skipped that step. Measured in the container on that path: a DOCX
converts immediately, and the first PDF takes ~45 s while the layout models
download (later ones are fast). With the models baked in there is no
first-use delay and no runtime network dependency.

For air-gapped deployments, run the command above before building the backend
image so the models are baked in via the existing `COPY . .` step in
`backend/Dockerfile` — that removes the runtime download entirely.

Malformed, unsupported, oversized, timed-out, and conversion failures return a
bounded structured reason.
