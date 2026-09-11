# Builds the FastAPI + CAMEL workforce backend without touching PyPI at
# build time: all Python deps install from the pre-downloaded Linux wheel
# bundle in vendor/camel-offline-bundle-linux (see that folder's sibling,
# vendor/camel-offline-bundle, for the original Windows-only bundle this
# was rebuilt from -- that one is excluded from the build context, see
# .dockerignore).
# The offline wheel bundle in vendor/camel-offline-bundle-linux was built
# for manylinux/x86_64, so this image must be built for linux/amd64 even on
# an arm64 host. docker-compose.yml pins `platform: linux/amd64` for this
# service; building standalone, pass `--platform linux/amd64` explicitly.
FROM python:3.11-slim

WORKDIR /app

# Docling's table model imports OpenCV even when OCR is disabled. Keep only
# the native runtime libraries needed by the prebuilt wheel (no browser/UI).
# nodejs/npm are for backend/carbone -- the bounded DOCX/PPTX renderer
# subprocess (backend/carbone_render.py) -- not a service of their own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libxcb1 \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Install deps first (better layer caching) straight from the offline
# wheel bundle -- no index, so this step never reaches PyPI.
COPY requirements.txt ./requirements.txt
COPY vendor/camel-offline-bundle-linux ./vendor/camel-offline-bundle-linux
# Crawl4AI (web acquisition engine), Playwright, and their transitive deps
# are NOT in the offline wheel bundle below, so -- unlike every other
# dependency in this image -- installing them reaches PyPI/Playwright's CDN
# at build time. Acceptable for now (self-hosted, not yet airgapped -- see
# docs/web-execution.md); before an airgapped deployment, vendor these
# wheels and the Chromium binary into vendor/camel-offline-bundle-linux the
# same way the rest of this image's deps are, and fold this into the
# offline --find-links install below instead.
RUN pip install --no-cache-dir crawl4ai==0.9.2 playwright==1.62.0 \
    && python -m playwright install --with-deps chromium

# Pre-existing gap, unrelated to the above: docling/torch/torchvision/
# python-pptx were added to requirements.txt (the docling-integration
# commit) but the offline bundle was never regenerated to include them --
# `pip install --no-index -r requirements.txt` fails with "No matching
# distribution found for docling" without this. torch's CPU-only build
# lives on PyTorch's own wheel index, not PyPI, hence --index-url here.
# Same offline-vendoring follow-up as the crawl4ai block above applies.
RUN pip install --no-cache-dir \
        torch==2.6.0+cpu torchvision==0.21.0+cpu \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir docling==2.114.0 python-pptx==1.0.2

RUN pip install --no-cache-dir --no-index \
        --find-links=vendor/camel-offline-bundle-linux \
        -r requirements.txt \
    && rm -rf vendor/camel-offline-bundle-linux

# LangGraph dual-path (AGENT_EXECUTOR=langgraph). Not in the camel offline
# bundle, so install from PyPI in its own step -- same pattern as crawl4ai.
RUN pip install --no-cache-dir "langgraph==0.6.7" "ag-ui-protocol>=0.1.15"

# camel-ai==0.2.80 declares psutil<6; crawl4ai needs >=6.1.1 -- see the
# psutil note in requirements.txt. The offline install above satisfies
# camel-ai's pin; this separate --no-deps step then overrides it upward for
# crawl4ai, deliberately its own pip invocation (a combined resolve of both
# constraints is unsatisfiable, verified with `pip install --dry-run`).
RUN pip install --no-cache-dir --no-deps "psutil>=6.1.1"

# carbone's own dependency tree is tiny (~3 MB, pure JS, no native deps) --
# installed from npm's registry here rather than vendored, unlike the Python
# wheel bundle above.
COPY carbone/package.json carbone/package-lock.json ./carbone/
RUN cd carbone && npm install --omit=dev --no-audit --no-fund

# Now the application source.
COPY . .

# All persistent state (sqlite DB, uploads, workspaces, logs) lives under
# DATA_DIR -- see backend/db.py -- so a single volume mounted at /data
# covers everything that needs to survive a container restart.
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
