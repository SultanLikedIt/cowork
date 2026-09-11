"""Tests for OpenBot AG-UI bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_controller import ControllerResult


def _run_body() -> dict:
    return {
        "threadId": "thread-1",
        "runId": "run-1",
        "state": {},
        "messages": [
            {
                "id": "m1",
                "role": "user",
                "content": "2020-21 Süper Lig gol krallığı",
            }
        ],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


@pytest.fixture()
def openbot_client(monkeypatch):
    monkeypatch.setenv("OPENBOT_AGUI_ENABLED", "true")
    monkeypatch.setenv("OPENBOT_AGUI_TOKEN", "test-token")

    app = FastAPI()
    import openbot.router as router_module

    router_module.OPENBOT_AGUI_ENABLED = True
    router_module.OPENBOT_AGUI_TOKEN = "test-token"
    router_module.mount_openbot_routes(app)
    return TestClient(app)


def test_openbot_health_when_enabled(openbot_client):
    response = openbot_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["agui_path"] == "/ag-ui"


def test_openbot_agui_requires_token(openbot_client):
    body = _run_body()
    denied = openbot_client.post("/ag-ui", json=body)
    assert denied.status_code == 401

    with patch(
        "openbot.agui_agent.web_controller.run_web_controller",
        # The real dataclass, not an ad-hoc object with the two fields this
        # test happened to need: the bridge later grew a Kaynaklar section
        # that reads result.evidence, and a hand-rolled stub silently drifted
        # behind it until every run died on AttributeError inside the SSE
        # stream. ControllerResult carries every field with its real default,
        # so the stub cannot fall behind the code again.
        new=AsyncMock(
            return_value=ControllerResult(
                answer="Test answer", successful_fetches=1
            )
        ),
    ):
        ok = openbot_client.post(
            "/ag-ui",
            json=body,
            headers={"x-openbot-agent-token": "test-token"},
        )
    assert ok.status_code == 200
    assert "text/event-stream" in ok.headers.get("content-type", "")
    assert "RUN_STARTED" in ok.text
    assert "Test answer" in ok.text
    assert "RUN_FINISHED" in ok.text


def test_openbot_routes_disabled_by_default(monkeypatch):
    monkeypatch.setenv("OPENBOT_AGUI_ENABLED", "false")
    app = FastAPI()
    import openbot.router as router_module

    router_module.OPENBOT_AGUI_ENABLED = False
    router_module.mount_openbot_routes(app)
    client = TestClient(app)
    assert client.get("/health").status_code == 404
