"""Outlook mail through the local computer bridge.

The backend container cannot reach Outlook; classic Outlook desktop on the
user's own Windows machine does the sending, driven by
``scripts/local_computer_bridge.py`` (POST /outlook/send). This module is the
backend-side client, the same shape as local_computer.py: same bridge URL,
plus a shared token the bridge requires for anything that sends mail.

Same-machine only, like the rest of the bridge: the mailbox is whichever
Outlook profile is open on the computer running Docker. See docs/outlook.md.

Settings are read at call time so tests can monkeypatch the environment.
"""

from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import requests

_STATUS_TIMEOUT_SECONDS = 3.0
# Classic Outlook can take a while to start on the first call of the day.
_SEND_TIMEOUT_SECONDS = 90.0
# Exchange Online's default message limit is 25MB after base64 (~33% larger),
# so raw attachments stay below that with room for the body.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_RECIPIENTS = 20
MAX_BODY_CHARS = 60_000

_ADDRESS = re.compile(r"^[^@\s;,<>\"]+@[^@\s;,<>\"]+\.[^@\s;,<>\"]+$")
_SPLIT = re.compile(r"[\s,;]+")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    return _flag("OUTLOOK_ENABLED", False)


def bridge_url() -> str:
    return (os.environ.get("LOCAL_COMPUTER_URL") or "http://host.docker.internal:8765").rstrip("/")


def bridge_token() -> str:
    return (os.environ.get("LOCAL_COMPUTER_TOKEN") or "").strip()


# -- addresses ---------------------------------------------------------------


class AddressError(ValueError):
    pass


def parse_addresses(value: Any, *, limit: int = MAX_RECIPIENTS) -> list[str]:
    """A list or a comma/semicolon/space separated string -> unique addresses.

    Raises AddressError naming the first address that is not valid.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else _SPLIT.split(str(value))
    found: list[str] = []
    for item in items:
        for part in _SPLIT.split(str(item or "")):
            address = part.strip()
            if not address:
                continue
            if not _ADDRESS.match(address):
                raise AddressError(f"Not an email address: {address}")
            if address.casefold() not in {existing.casefold() for existing in found}:
                found.append(address)
    if len(found) > limit:
        raise AddressError(f"At most {limit} addresses.")
    return found


# -- body --------------------------------------------------------------------

_EMAIL_CSS = """
body { font-family: Calibri, 'Segoe UI', Arial, sans-serif; font-size: 11pt; color: #1f2328; line-height: 1.45; }
h2 { font-size: 15pt; margin: 18px 0 6px; }
h3 { font-size: 13pt; margin: 14px 0 4px; }
h4 { font-size: 11pt; margin: 12px 0 4px; }
p { margin: 0 0 8px; }
table { border-collapse: collapse; margin: 8px 0 12px; }
th, td { border: 1px solid #d0d7de; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #f3f4f6; }
aside { border-left: 3px solid #d0d7de; padding: 2px 10px; color: #444; margin: 8px 0; display: block; }
.note { color: #57606a; font-size: 9pt; margin-top: 18px; border-top: 1px solid #e5e7eb; padding-top: 8px; }
.summary { background: #f6f8fa; padding: 8px 12px; margin-bottom: 12px; }
"""


def render_email_html(
    title: str,
    markdown: str,
    *,
    intro: Optional[str] = None,
    notes: Sequence[str] = (),
) -> str:
    """A job's markdown answer as an HTML email body.

    Reuses the report pipeline's markdown reader and block renderers, so
    headings, lists and tables look the way they do in the HTML/PDF export.
    """
    from document_html import _inline, _list_html, _table_html
    from document_model import (
        HeadingBlock,
        ListBlock,
        ParagraphBlock,
        QuoteBlock,
        TableBlock,
        document_from_markdown,
    )

    parts: list[str] = []
    if intro:
        parts.append(f'<div class="summary">{_inline(intro)}</div>')
    body = (markdown or "")[:MAX_BODY_CHARS]
    for block in document_from_markdown(title, body).blocks:
        if isinstance(block, HeadingBlock):
            tag = {1: "h2", 2: "h3"}.get(block.level, "h4")
            parts.append(f"<{tag}>{_inline(block.text)}</{tag}>")
        elif isinstance(block, ParagraphBlock):
            parts.append(f"<p>{_inline(block.text)}</p>")
        elif isinstance(block, ListBlock):
            parts.append(_list_html(block.items, block.ordered))
        elif isinstance(block, TableBlock):
            parts.append(_table_html(block))
        elif isinstance(block, QuoteBlock):
            parts.append(f"<aside>{_inline(block.text)}</aside>")
    if notes:
        parts.append(
            '<div class="note">' + "<br>".join(html.escape(note, quote=False) for note in notes) + "</div>"
        )
    return (
        '<!doctype html><html lang="tr"><head><meta charset="utf-8">'
        f"<style>{_EMAIL_CSS}</style></head><body>{''.join(parts)}</body></html>"
    )


# -- bridge calls --------------------------------------------------------------


@dataclass
class SendResult:
    ok: bool
    error: Optional[str] = None
    detail: Optional[str] = None
    # False when trying again cannot help (bad address, feature off, bad token).
    retryable: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        return self.detail or self.error or ("sent" if self.ok else "failed")


_NOT_RETRYABLE = frozenset(
    {
        "outlook_disabled",
        "outlook_requires_token",
        "invalid_token",
        "invalid_request",
        "invalid_address",
        "invalid_base64",
        "payload_too_large",
        "json_required",
        "outlook_unavailable",
        "no_recipients",
    }
)


def status() -> dict[str, Any]:
    """What the UI shows before a user relies on email delivery."""
    info: dict[str, Any] = {
        "enabled": is_enabled(),
        "token_configured": bool(bridge_token()),
        "bridge_reachable": False,
        "outlook_available": False,
        "bridge_token_configured": False,
    }
    if not info["enabled"]:
        return info
    try:
        response = requests.get(f"{bridge_url()}/health", timeout=_STATUS_TIMEOUT_SECONDS)
        payload = response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return info
    outlook = payload.get("outlook") if isinstance(payload, dict) else None
    info["bridge_reachable"] = bool(response.ok)
    if isinstance(outlook, dict):
        info["outlook_available"] = bool(outlook.get("available"))
        info["bridge_token_configured"] = bool(outlook.get("token_configured"))
    return info


def send_mail(
    *,
    to: Sequence[str],
    subject: str,
    html_body: str = "",
    text_body: str = "",
    cc: Sequence[str] = (),
    attachments: Sequence[tuple[str, bytes]] = (),
    action: str = "send",
) -> SendResult:
    """Send (or, with action="display", open for review) one message.

    Blocking; call through asyncio.to_thread from async code. Attachments
    over MAX_ATTACHMENT_BYTES in total are left out and listed in ``notes``
    rather than failing the whole message.
    """
    if not is_enabled():
        return SendResult(False, "outlook_disabled", "Outlook is turned off (OUTLOOK_ENABLED).", retryable=False)
    token = bridge_token()
    if not token:
        return SendResult(
            False, "outlook_requires_token", "LOCAL_COMPUTER_TOKEN is not set.", retryable=False
        )
    if not to:
        return SendResult(False, "no_recipients", "No recipients.", retryable=False)

    notes: list[str] = []
    encoded: list[dict[str, str]] = []
    total = 0
    for filename, content in attachments:
        if total + len(content) > MAX_ATTACHMENT_BYTES:
            notes.append(
                f"{filename} eklenmedi: e-posta ekleri toplam {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB ile sınırlı."
            )
            continue
        total += len(content)
        encoded.append({"filename": filename, "content_base64": base64.b64encode(content).decode("ascii")})

    try:
        response = requests.post(
            f"{bridge_url()}/outlook/send",
            json={
                "to": list(to),
                "cc": list(cc),
                "subject": subject,
                "html_body": html_body,
                "text_body": text_body,
                "attachments": encoded,
                "action": action,
            },
            headers={"X-Bridge-Token": token},
            timeout=_SEND_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return SendResult(
            False,
            "bridge_unreachable",
            f"Could not reach the local computer bridge at {bridge_url()} ({exc}). "
            "It must be running on the computer with Outlook.",
            notes=notes,
        )
    if response.status_code == 404:
        return SendResult(
            False,
            "bridge_outdated",
            "The running bridge has no Outlook support. Restart scripts/local_computer_bridge.py.",
            notes=notes,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.ok and payload.get("ok"):
        return SendResult(True, notes=notes)
    error = str(payload.get("error") or f"http_{response.status_code}")
    return SendResult(
        False,
        error,
        str(payload.get("detail") or error),
        retryable=error not in _NOT_RETRYABLE,
        notes=notes,
    )


# -- HTTP --------------------------------------------------------------------


def build_router() -> Any:
    """/integrations/outlook: status for the UI and a test message."""
    import asyncio

    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, Field

    router = APIRouter(prefix="/integrations/outlook", tags=["outlook"])

    class TestRequest(BaseModel):
        to: str = Field(min_length=3, max_length=320)

    @router.get("/status")
    async def outlook_status():
        return await asyncio.to_thread(status)

    @router.post("/test")
    async def outlook_test(req: TestRequest):
        try:
            to = parse_addresses(req.to, limit=1)
        except AddressError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not to:
            raise HTTPException(status_code=422, detail="Enter an email address.")
        result = await asyncio.to_thread(
            send_mail,
            to=to,
            subject="agent-webapp test e-postası",
            html_body=render_email_html(
                "Test",
                "Bu bir test e-postasıdır. Zamanlanmış görev sonuçları bu hesaptan gönderilecek.",
            ),
        )
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.message)
        return {"ok": True}

    return router
