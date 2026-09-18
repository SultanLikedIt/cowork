"""outlook.py (backend client) and the bridge's /outlook/send endpoint.

The bridge runs for real on a local port with a fake mailer in place of
Outlook, so the token, content-type and request parsing are exercised over
HTTP exactly as the backend container reaches them. No mail is sent.
"""

from __future__ import annotations

import base64
import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import requests

import outlook

BRIDGE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "local_computer_bridge.py"
TOKEN = "test-token-123"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("local_computer_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMailer:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def send(self, **message: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.messages.append(message)
        return {"ok": True, "action": message["action"]}


@pytest.fixture()
def bridge(monkeypatch):
    module = _load_bridge()
    mailer = FakeMailer()

    class Handler(module.Handler):
        pass

    Handler.token = TOKEN
    Handler.mailer = mailer
    Handler.allowed_dirs = []
    Handler.log_message = lambda self, *args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")
    monkeypatch.setenv("LOCAL_COMPUTER_URL", url)
    monkeypatch.setenv("LOCAL_COMPUTER_TOKEN", TOKEN)
    try:
        yield {"url": url, "mailer": mailer, "handler": Handler, "module": module}
    finally:
        server.shutdown()
        server.server_close()


# -- addresses ---------------------------------------------------------------


def test_parse_addresses() -> None:
    assert outlook.parse_addresses("a@example.com, b@example.com;c@example.com") == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]
    assert outlook.parse_addresses(["a@example.com", "A@EXAMPLE.com"]) == ["a@example.com"]
    assert outlook.parse_addresses(None) == []
    with pytest.raises(outlook.AddressError):
        outlook.parse_addresses("ali@")
    with pytest.raises(outlook.AddressError):
        outlook.parse_addresses([f"u{i}@example.com" for i in range(3)], limit=2)


# -- body --------------------------------------------------------------------


def test_render_email_html_escapes_and_formats() -> None:
    body = outlook.render_email_html(
        "Bülten",
        "## Özet\n\n- **Çelik** fiyatı arttı\n- <script>alert(1)</script>\n- <img src=x onerror=y>\n\n| A | B |\n|---|---|\n| 1 | 2 |",
        intro="Değişiklik: yeni tebliğ",
        notes=["Otomatik <gönderildi>"],
    )
    assert "<strong>Çelik</strong>" in body
    # The markdown reader drops script tags; any other markup is escaped.
    assert "<script>" not in body
    assert "<img" not in body
    assert "&lt;img src=x onerror=y&gt;" in body
    assert "<table>" in body
    assert "Değişiklik: yeni tebliğ" in body
    assert "Otomatik &lt;gönderildi&gt;" in body


# -- client without a bridge ------------------------------------------------------


def test_send_refused_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("OUTLOOK_ENABLED", raising=False)
    result = outlook.send_mail(to=["a@example.com"], subject="x")
    assert (result.ok, result.error, result.retryable) == (False, "outlook_disabled", False)


def test_send_refused_without_token(monkeypatch) -> None:
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")
    monkeypatch.delenv("LOCAL_COMPUTER_TOKEN", raising=False)
    result = outlook.send_mail(to=["a@example.com"], subject="x")
    assert (result.ok, result.error, result.retryable) == (False, "outlook_requires_token", False)


def test_unreachable_bridge_is_retryable(monkeypatch) -> None:
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")
    monkeypatch.setenv("LOCAL_COMPUTER_TOKEN", TOKEN)
    monkeypatch.setenv("LOCAL_COMPUTER_URL", "http://127.0.0.1:9")
    result = outlook.send_mail(to=["a@example.com"], subject="x")
    assert (result.ok, result.error, result.retryable) == (False, "bridge_unreachable", True)


# -- client against the real bridge ---------------------------------------------


def test_send_through_bridge(bridge) -> None:
    result = outlook.send_mail(
        to=["ali@example.com"],
        cc=["ekip@example.com"],
        subject="Haftalık bülten\r\n— 21 Eylül",
        html_body="<p>Merhaba</p>",
        attachments=[("rapor.pdf", b"%PDF-1.7 data"), ("rapor.pdf", b"second")],
    )
    assert result.ok, result
    [message] = bridge["mailer"].messages
    assert message["to"] == ["ali@example.com"]
    assert message["cc"] == ["ekip@example.com"]
    assert message["subject"] == "Haftalık bülten  — 21 Eylül"
    assert message["attachments"] == [("rapor.pdf", b"%PDF-1.7 data"), ("rapor.pdf", b"second")]
    assert message["action"] == "send"


def test_attachments_over_the_cap_are_left_out(bridge, monkeypatch) -> None:
    monkeypatch.setattr(outlook, "MAX_ATTACHMENT_BYTES", 10)
    result = outlook.send_mail(
        to=["ali@example.com"], subject="x", attachments=[("a.txt", b"12345"), ("b.txt", b"1234567890")]
    )
    assert result.ok
    assert [name for name, _ in bridge["mailer"].messages[0]["attachments"]] == ["a.txt"]
    assert "b.txt" in result.notes[0]


def test_wrong_token_is_rejected_and_not_retried(bridge, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_COMPUTER_TOKEN", "wrong")
    result = outlook.send_mail(to=["ali@example.com"], subject="x")
    assert (result.ok, result.error, result.retryable) == (False, "invalid_token", False)
    assert bridge["mailer"].messages == []


def test_outlook_not_running_is_retryable(bridge) -> None:
    bridge["mailer"].error = bridge["module"].OutlookError("outlook_not_running", "closed", 503)
    result = outlook.send_mail(to=["ali@example.com"], subject="x")
    assert (result.ok, result.error, result.retryable) == (False, "outlook_not_running", True)


def test_status_reports_bridge(bridge) -> None:
    info = outlook.status()
    assert info["enabled"] and info["bridge_reachable"] and info["bridge_token_configured"]


# -- bridge request guards --------------------------------------------------------


def _post(bridge, body: Any, headers: dict[str, str]) -> requests.Response:
    return requests.post(f"{bridge['url']}/outlook/send", data=body, headers=headers, timeout=5)


def test_bridge_refuses_browser_style_requests(bridge) -> None:
    body = '{"to": ["ali@example.com"], "subject": "x"}'
    # What a web page can send without a CORS preflight: no custom header.
    assert _post(bridge, body, {"Content-Type": "text/plain"}).status_code == 401
    # Right token, but not JSON.
    assert _post(bridge, body, {"Content-Type": "text/plain", "X-Bridge-Token": TOKEN}).status_code == 415
    assert bridge["mailer"].messages == []


def test_bridge_without_token_refuses_outlook(bridge) -> None:
    bridge["handler"].token = None
    response = _post(bridge, '{"to": ["ali@example.com"]}', {"Content-Type": "application/json", "X-Bridge-Token": ""})
    assert response.status_code == 403
    assert response.json()["error"] == "outlook_requires_token"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"to": []}, "invalid_request"),
        ({"to": ["bozuk"]}, "invalid_address"),
        ({"to": ["a@example.com"], "action": "delete"}, "invalid_request"),
        ({"to": ["a@example.com"], "attachments": [{"filename": "a", "content_base64": "!!"}]}, "invalid_base64"),
    ],
)
def test_bridge_validates_payload(bridge, payload, error) -> None:
    response = requests.post(
        f"{bridge['url']}/outlook/send", json=payload, headers={"X-Bridge-Token": TOKEN}, timeout=5
    )
    assert response.status_code == 400
    assert response.json()["error"] == error


def test_bridge_strips_attachment_paths(bridge) -> None:
    content = base64.b64encode(b"x").decode()
    response = requests.post(
        f"{bridge['url']}/outlook/send",
        json={"to": ["a@example.com"], "attachments": [{"filename": "..\\..\\evil.bat", "content_base64": content}]},
        headers={"X-Bridge-Token": TOKEN},
        timeout=5,
    )
    assert response.ok
    [(name, _)] = bridge["mailer"].messages[0]["attachments"]
    assert "\\" not in name and "/" not in name


# -- routes ------------------------------------------------------------------


@pytest.fixture()
def api(monkeypatch):
    """The real router, so a route that cannot read its body fails here.

    Regression: TestRequest was declared inside build_router, where FastAPI
    could not resolve it (postponed annotations), so POST /test answered 422
    "missing query parameter req" and no email was ever sent.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(outlook, "send_mail", lambda **kwargs: (sent.append(kwargs), outlook.SendResult(True))[1])
    monkeypatch.setattr(outlook, "status", lambda: {"enabled": True})
    app = FastAPI()
    app.include_router(outlook.build_router())
    return TestClient(app), sent


def test_test_route_sends(api) -> None:
    client, sent = api
    response = client.post("/integrations/outlook/test", json={"to": "ali@example.com"})
    assert response.status_code == 200, response.text
    assert sent[0]["to"] == ["ali@example.com"]
    assert "test" in sent[0]["subject"].casefold()


def test_test_route_rejects_a_bad_address(api) -> None:
    client, sent = api
    assert client.post("/integrations/outlook/test", json={"to": "ali@"}).status_code == 422
    assert sent == []


def test_status_route(api) -> None:
    client, _ = api
    assert client.get("/integrations/outlook/status").json() == {"enabled": True}
