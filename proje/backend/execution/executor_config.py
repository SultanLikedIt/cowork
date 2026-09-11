"""Which orchestration backend runs jobs in this process."""

from __future__ import annotations

import os
from typing import Literal

ExecutorName = Literal["camel", "langgraph"]

DEFAULT_EXECUTOR: ExecutorName = "camel"


def agent_executor() -> ExecutorName:
    """Return the active executor. Unknown values fall back to Camel (safe)."""
    raw = (os.environ.get("AGENT_EXECUTOR") or DEFAULT_EXECUTOR).strip().lower()
    if raw == "langgraph":
        return "langgraph"
    return "camel"
