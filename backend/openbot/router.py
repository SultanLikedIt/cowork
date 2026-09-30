"""FastAPI routes for OpenBot remote-ag-ui registration."""

from __future__ import annotations

import os
from typing import Optional

from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from openbot.agui_agent import run_openbot_turn

OPENBOT_AGUI_ENABLED = (
    os.environ.get("OPENBOT_AGUI_ENABLED", "false").strip().casefold()
    in {"1", "true", "yes", "on"}
)
OPENBOT_AGUI_TOKEN = (os.environ.get("OPENBOT_AGUI_TOKEN") or "").strip()


def _authorized(request: Request) -> bool:
    if not OPENBOT_AGUI_TOKEN:
        return True
    # OpenBot managed remote agents send x-openbot-agent-token (see OpenBot
    # server/src/agents/runtime-agents.ts), not necessarily Authorization.
    openbot_token = (request.headers.get("x-openbot-agent-token") or "").strip()
    if openbot_token == OPENBOT_AGUI_TOKEN:
        return True
    header = (request.headers.get("authorization") or "").strip()
    if header == OPENBOT_AGUI_TOKEN:
        return True
    if header.lower().startswith("bearer "):
        return header[7:].strip() == OPENBOT_AGUI_TOKEN
    return False


def mount_openbot_routes(app) -> None:
    """Register /ag-ui and /health when OpenBot integration is enabled."""
    if not OPENBOT_AGUI_ENABLED:
        return

    router = APIRouter(tags=["openbot"])

    @router.get("/health")
    async def openbot_health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "framework": "agent-webapp-langgraph",
                "agui_path": "/ag-ui",
                "auth_required": bool(OPENBOT_AGUI_TOKEN),
            }
        )

    @router.post("/ag-ui")
    async def openbot_agui(input_data: RunAgentInput, request: Request):
        if not _authorized(request):
            raise HTTPException(status_code=401, detail="Unauthorized")

        accept_header = request.headers.get("accept")
        encoder = EventEncoder(accept=accept_header)

        async def event_generator():
            async for event in run_openbot_turn(input_data):
                yield encoder.encode(event)

        return StreamingResponse(
            event_generator(),
            media_type=encoder.get_content_type(),
        )

    app.include_router(router)
