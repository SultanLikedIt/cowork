# PaddleOCR-VL pipeline service (POST /layout-parsing) for backend uploads.
#
# Baidu publishes a ready-made image for this, but its registry
# (ccr-2vdh3abv-pub.cnc.bj.baidubce.com) and Paddle's own pip index
# (paddlepaddle.org.cn) time out from our network, so this image is built
# from reachable sources only: PyPI for paddlepaddle/paddleocr and Hugging
# Face for model weights. It is the same `paddlex --serve` pipeline app the
# official image runs.
#
# Only layout detection (PP-DocLayoutV3, a small detector) runs here, on CPU.
# The expensive part -- PaddleOCR-VL-1.6 reading every block -- runs on the
# GPU in the paddleocr-vlm-server container (vLLM), reached over HTTP via
# pipeline_config.yaml.
FROM python:3.11-slim

WORKDIR /app

# OpenCV (pulled in by paddlex[ocr]) needs these at import time; curl is for
# the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# paddlepaddle CPU build from PyPI (the GPU build is not published there).
# CPU is enough: the VLM does not run in this container.
RUN pip install --no-cache-dir \
        paddlepaddle==3.3.1 \
        "paddleocr[doc-parser]==3.7.0" \
        "paddlex[serving]==3.7.2"

ENV PADDLE_PDX_MODEL_SOURCE=huggingface \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_CACHE_HOME=/models

# Bake the CPU-side weights into the image so the service starts without
# network access: layout detection, plus the optional orientation and
# unwarping models (toggled per request by PADDLEOCR_VL_ORIENTATION /
# PADDLEOCR_VL_UNWARPING in the backend). The VLM weights are NOT here; the
# vLLM container loads them.
RUN python -c "from paddlex.inference.utils.official_models import official_models as m; [print(name, m[name]) for name in ('PP-DocLayoutV3', 'PP-LCNet_x1_0_doc_ori', 'UVDoc')]"

COPY pipeline_config.yaml /app/pipeline_config.yaml

EXPOSE 8080

CMD ["paddlex", "--serve", "--pipeline", "/app/pipeline_config.yaml", "--device", "cpu", "--host", "0.0.0.0", "--port", "8080"]
