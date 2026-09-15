"""Local BGE cross-encoder that scores search candidates against the ask.

Replaces the per-round LLM SELECT call in web_controller: one batched CPU
forward pass over ~10 (query, title+snippet) pairs instead of a full model
round-trip. Weights are vendored offline like Docling's; when they are
missing or fail to load, ``score`` returns None and callers fall back to the
LLM SELECT path unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)

MAX_PAIR_TOKENS = 512

_MODEL: Optional[tuple[Any, Any]] = None
_LOAD_FAILED = False
_LOAD_LOCK = threading.Lock()
# One model instance shared by every concurrent workforce worker.
_INFER_LOCK = threading.Lock()


def _dir_usable(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _resolve_model_path() -> Optional[Path]:
    bundled = Path(__file__).parent / "vendor" / "bge-reranker-v2-m3"
    configured = os.getenv("RERANKER_MODEL_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    if bundled not in candidates:
        candidates.append(bundled)
    for candidate in candidates:
        if _dir_usable(candidate):
            return candidate
    _logger.warning(
        "Reranker weights not found (RERANKER_MODEL_PATH=%s); web SELECT "
        "falls back to the LLM. Run 'huggingface-cli download "
        "BAAI/bge-reranker-v2-m3 --local-dir backend/vendor/bge-reranker-v2-m3' "
        "and rebuild the backend image.",
        configured or "(unset)",
    )
    return None


def _get_model() -> Optional[tuple[Any, Any]]:
    global _MODEL, _LOAD_FAILED
    with _LOAD_LOCK:
        if _MODEL is not None or _LOAD_FAILED:
            return _MODEL
        path = _resolve_model_path()
        if path is None:
            _LOAD_FAILED = True
            return None
        try:
            import torch
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )

            threads = int(os.getenv("RERANKER_THREADS") or 4)
            torch.set_num_threads(max(1, threads))
            tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
            model = AutoModelForSequenceClassification.from_pretrained(
                path, local_files_only=True
            )
            model.eval()
            _MODEL = (tokenizer, model)
        except Exception:  # noqa: BLE001 - unavailable reranker means LLM SELECT
            _logger.warning("Reranker failed to load from %s", path, exc_info=True)
            _LOAD_FAILED = True
        return _MODEL


def enabled() -> bool:
    flag = os.getenv("RERANKER_ENABLED", "true").strip().casefold()
    return flag not in {"0", "false", "no", "off"}


def is_available() -> bool:
    return _get_model() is not None


def score(query: str, passages: list[str]) -> Optional[list[float]]:
    """Relevance in [0, 1] per passage, or None when the reranker is unusable."""
    if not passages:
        return []
    loaded = _get_model()
    if loaded is None:
        return None
    tokenizer, model = loaded
    try:
        import torch

        with _INFER_LOCK, torch.inference_mode():
            inputs = tokenizer(
                [[query, passage] for passage in passages],
                padding=True,
                truncation=True,
                max_length=MAX_PAIR_TOKENS,
                return_tensors="pt",
            )
            logits = model(**inputs).logits.view(-1).float()
            return torch.sigmoid(logits).tolist()
    except Exception:  # noqa: BLE001
        _logger.warning("Reranker inference failed", exc_info=True)
        return None


def warm_up() -> None:
    score("warm up", ["warm up"])


__all__ = ["enabled", "is_available", "score", "warm_up"]
