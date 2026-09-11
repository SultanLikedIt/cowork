"""SSE event models for workforce and direct execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

class ActivityEvent(BaseModel):
    """A real execution step, for the UI's status line.

    Same shape/transport as TokenDeltaEvent (queue -> _apply_event ->
    snapshot -> SSE), so it needs no new plumbing. Emitted only from places
    that actually DID the thing: web_search/web_read report searching/opening/
    reading as they perform them, and the artifact layer reports
    what it wrote. `stage` is a fixed vocabulary the frontend maps to
    wording; `detail` is a short factual subject (a URL, a filename).

    This is deliberately NOT reasoning. It never carries model thinking,
    plans, or intentions -- only steps that have already happened.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    event_type: Literal["activity"] = "activity"
    task_id: str
    stage: Literal[
        "searching", "opening", "browsing", "reading", "building", "done"
    ]
    detail: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TokenDeltaEvent(BaseModel):
    """Not a CAMEL event -- this repo's own addition, shaped like the real
    CAMEL events above so it flows through the same
    queue.put_nowait(...) -> _apply_event -> snapshot -> SSE pipeline
    (see main.py) without any transport special-casing. Emitted while the
    final answer's text is arriving, so the frontend can render it as it
    comes in instead of waiting for the job to finish.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    event_type: Literal["token_delta"] = "token_delta"
    task_id: str
    delta: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
