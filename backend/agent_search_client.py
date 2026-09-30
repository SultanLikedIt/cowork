"""HTTP client for the brcrusoe72/agent-search sidecar.

AgentSearch sits in front of SearXNG: multi-engine strategies, scoring,
dedupe, optional content fetch. Our agents still call ``web_search``;
this module is an internal discovery backend, not a model-facing tool.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

AGENT_SEARCH_ENABLED = (
    os.environ.get("AGENT_SEARCH_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)

AGENT_SEARCH_BASE_URL = (
    os.environ.get("AGENT_SEARCH_BASE_URL") or "http://localhost:3939"
).rstrip("/")

AGENT_SEARCH_TOKEN = (os.environ.get("AGENT_SEARCH_TOKEN") or "").strip()

AGENT_SEARCH_TIMEOUT_SECONDS = float(
    os.environ.get("AGENT_SEARCH_TIMEOUT_SECONDS") or "20"
)

# Map our SearXNG-style categories onto AgentSearch named strategies.
_CATEGORY_TO_MODE = {
    "general": "general",
    "news": "news",
    "science": "academic",
    "academic": "academic",
    "it": "code",
    "code": "code",
}


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    if AGENT_SEARCH_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_SEARCH_TOKEN}"
    return headers


def is_enabled() -> bool:
    return AGENT_SEARCH_ENABLED


def health() -> Dict[str, Any]:
    """Return AgentSearch /health JSON, or ``{"ok": False, ...}`` on failure."""
    try:
        response = requests.get(
            f"{AGENT_SEARCH_BASE_URL}/health",
            headers=_headers(),
            timeout=min(5.0, AGENT_SEARCH_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"ok": True, "raw": payload}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def search(
    query: str,
    *,
    limit: int = 10,
    categories: str = "general",
    mode: Optional[str] = None,
    fetch: bool = False,
) -> List[Dict[str, Any]]:
    """Call ``GET /search`` and normalize hits to our web_search shape.

    On transport/HTTP failure returns a single ``{"error": "..."}`` dict
    so callers can fall back to SearXNG.
    """
    cats = (categories or "general").strip().lower()
    resolved_mode = (mode or _CATEGORY_TO_MODE.get(cats) or "general").strip()
    params: Dict[str, Any] = {
        "q": query,
        "count": max(1, min(int(limit), 50)),
        "mode": resolved_mode,
        "fetch": "true" if fetch else "false",
    }

    try:
        response = requests.get(
            f"{AGENT_SEARCH_BASE_URL}/search",
            params=params,
            headers=_headers(),
            timeout=AGENT_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"agent-search failed ({resolved_mode}): {exc}"}]

    results = payload.get("results") or []
    responses: List[Dict[str, Any]] = []
    for i, result in enumerate(results[:limit], start=1):
        snippet = str(
            result.get("snippet") or result.get("content") or ""
        ).strip()
        responses.append(
            {
                "result_id": i,
                "title": result.get("title", ""),
                "description": snippet,
                "url": result.get("url", ""),
                "content": snippet,
                "score": result.get("score"),
                "engines": result.get("engines") or [],
                "positions": [result.get("position")]
                if result.get("position") is not None
                else [],
                "publishedDate": result.get("publishedDate"),
                "category": cats,
                "source": "agent-search",
                "mode": resolved_mode,
            }
        )
    return responses


__all__ = [
    "AGENT_SEARCH_BASE_URL",
    "AGENT_SEARCH_ENABLED",
    "health",
    "is_enabled",
    "search",
]
