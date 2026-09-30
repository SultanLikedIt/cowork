# Builds the FastAPI + LangGraph Workforce backend. Python dependencies come
# from PyPI (torch from PyTorch's CPU wheel index).
FROM python:3.11-slim

WORKDIR /app

# Docling's table model imports OpenCV even when OCR is disabled. Keep only
# the native runtime libraries needed by the prebuilt wheel (no browser/UI).
# tesseract-ocr is the default OCR engine for scanned PDFs and image uploads
# (document_ingestion.py); it pulls in English + OSD data, -tur adds Turkish.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libxcb1 \
        tesseract-ocr \
        tesseract-ocr-tur \
    && rm -rf /var/lib/apt/lists/*

# Crawl4AI (web acquisition engine) and Playwright's Chromium, in their own
# layer: the browser download is large and changes rarely.
RUN pip install --no-cache-dir crawl4ai==0.9.2 playwright==1.62.0 \
    && python -m playwright install --with-deps chromium

# torch's CPU-only build lives on PyTorch's own wheel index, not PyPI, hence
# --index-url here; requirements.txt's torch pins are then already satisfied.
RUN pip install --no-cache-dir \
        torch==2.6.0+cpu torchvision==0.21.0+cpu \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir docling==2.114.0 python-pptx==1.0.2

# Docling's layout/table weights (~506MB), baked in rather than fetched on
# first upload. docker-compose.yml points DOCLING_ARTIFACTS_PATH here; when
# the directory is absent, document_ingestion falls back to downloading from
# Hugging Face at runtime, which costs ~45s on the first PDF or image and
# needs outbound network. Deliberately ABOVE `COPY . .` so editing Python
# source does not re-download half a gigabyte on every rebuild.
#
# The two models are named explicitly. Plain `docling-tools models download`
# (the default set) pulls RapidOCR weights from modelscope.cn, which times
# out from here and would fail the whole build -- and it also drags in
# CodeFormula and the figure classifier, which this pipeline never loads,
# for 1.2GB total. Measured: layout + tableformer alone satisfy the standard
# PDF pipeline's artifacts_path validation. The easyocr/rapidocr OCR engines
# have their own weights and are NOT included; OCR_ENGINE=tesseract (the
# default) needs none, since it is the apt package installed above.
RUN docling-tools models download layout tableformer -q -o /app/vendor/docling-models

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Reddit reading (backend/social/, docs/social-reach.md). Pinned to one
# commit on purpose: PyPI is stuck at 0.4.1 while the working release is
# 0.4.2, and upstream stopped updating in 2026-03, so "latest" is not a
# thing that exists here.
#
# Installed from the commit's ARCHIVE rather than git+https, because this
# image is python:3.11-slim and has no `git` -- a git+ URL fails the build
# with exit 1, which is exactly what the first version of this line did.
# Adding git for one dependency would cost an apt layer; the archive URL
# pins the identical commit without it.
#
# Optional at runtime: when rdt is absent the Social Agent reports "not
# installed" through the ordinary capability-missing channel rather than
# failing a job.
RUN pip install --no-cache-dir         "rdt-cli @ https://github.com/public-clis/rdt-cli/archive/5e4fb3720d5c174e976cd425ccc3b879d52cac66.zip"

# Charts for the Coding Agent (code_controller.HAS_MATPLOTLIB; docs/coding-agent.md).
# Its own layer, below the heavy ones and not in requirements.txt: adding it
# there would invalidate the torch/docling install layer and re-download
# gigabytes for a ~40 MB package. The font cache is built here so the first
# chart of every job does not pay for it.
RUN pip install --no-cache-dir "matplotlib==3.11.2" \
    && MPLBACKEND=Agg python -c "import matplotlib.pyplot"

# Now the application source.
COPY . .

# All persistent state (sqlite DB, uploads, workspaces, logs) lives under
# DATA_DIR -- see backend/db.py -- so a single volume mounted at /data
# covers everything that needs to survive a container restart.
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
