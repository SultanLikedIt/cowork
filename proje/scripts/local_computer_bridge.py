#!/usr/bin/env python3
"""Local Computer Bridge -- lets the backend open a file on YOUR machine.

Why this exists: backend/document_export.py writes generated PPTX/DOCX/PDF
files inside the backend Docker container's own filesystem
(WORKSPACE_DIR/{job_id}/artifacts/...), not on your Mac. The browser
downloads the file to your real Downloads folder, but nothing lets the
agent say "open the file I just made" -- Docker containers on macOS run
inside a Linux VM with no access to the host's GUI, so the container
itself can never launch a Mac app no matter what runs inside it.

This script is the fix: a tiny HTTP server that runs directly on your Mac
(NOT in Docker), so it can actually call `open <path>`. The backend
reaches it through Docker's `host.docker.internal` DNS name, which always
resolves to your host machine from inside a container.

Two endpoints:
- POST /open-file {"filename"}: open a file that ALREADY exists in an
  allowed directory (e.g. one the browser already downloaded).
- POST /save-and-open {"filename", "content_base64"}: write the given
  bytes into the first allowed directory (overwriting any stale file of
  the same name) and open THAT. Used whenever the backend has the file's
  real content on hand, so "open it" can never open an old, unrelated
  file that merely happens to share the same generic name (e.g. every
  Word report defaulting to "rapor.docx").

Outlook (Windows, classic Outlook desktop only):
- POST /outlook/send {"to", "cc", "subject", "html_body", "text_body",
  "attachments": [{"filename", "content_base64"}], "action": "send" |
  "display"}: builds a mail item in the Outlook profile open on this
  machine and either sends it or opens it for the user to review.
  Needs `pip install pywin32` and a shared token (--token or
  LOCAL_COMPUTER_TOKEN) sent as the X-Bridge-Token header. The token is
  what stops any web page open in the browser from POSTing to this port
  and sending mail as the user: a custom header forces a CORS preflight
  that this server never approves. Without a token the Outlook endpoints
  are refused. See docs/outlook.md.

Deliberately NOT a full MCP (Model Context Protocol) server -- hand-rolling
MCP's JSON-RPC/session-handshake wire protocol for a synchronous client
inside this backend's existing synchronous tool-binding code
(capabilities.py) was worse engineering than it was worth for one tool.
This is a minimal, dependency-free HTTP bridge with the same spirit (a
local, non-cloud-dependent tool server) -- the backend's local_computer.py
module is the only thing that would need to change if this later grows
into a real MCP server or gains more tools.

Usage:
    python3 scripts/local_computer_bridge.py
    # or, to allow a different/additional directory beyond ~/Downloads:
    python3 scripts/local_computer_bridge.py --allow ~/Desktop/reports
    # with Outlook (Windows):
    python scripts/local_computer_bridge.py --token <same value as LOCAL_COMPUTER_TOKEN in .env>

No third-party dependencies for opening files -- stdlib only. Outlook
additionally needs pywin32, imported only when an Outlook endpoint is used.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

DEFAULT_PORT = 8765
# Base64-encoded body; ~33% larger than the raw file. 30MB raw is already a
# generous cap for anything this app's Document Agent produces (reports,
# presentations, spreadsheets), and keeps one request from tying up the
# bridge's single-threaded-per-connection handling for too long.
MAX_SAVE_AND_OPEN_BYTES = 40 * 1024 * 1024
# Exchange Online rejects messages over ~25-35MB after encoding; the backend
# keeps attachments well under this, so hitting it means a bad request.
MAX_OUTLOOK_REQUEST_BYTES = 40 * 1024 * 1024
MAX_OUTLOOK_RECIPIENTS = 50
_ADDRESS = re.compile(r"^[^@\s;,<>\"]+@[^@\s;,<>\"]+\.[^@\s;,<>\"]+$")
_OL_MAIL_ITEM = 0


class OutlookError(Exception):
    def __init__(self, code: str, detail: str, status: int = 500) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def outlook_available() -> bool:
    """pywin32 importable on Windows. Does not start Outlook."""
    if platform.system() != "Windows":
        return False
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    return True


class OutlookMailer:
    """Classic Outlook through COM. Outlook's object model is single-threaded,
    so calls are serialized and each one initializes COM on its own thread
    (ThreadingHTTPServer runs every request on a new thread)."""

    _lock = threading.Lock()

    def send(
        self,
        *,
        to: list[str],
        cc: list[str],
        subject: str,
        html_body: str,
        text_body: str,
        attachments: list[tuple[str, bytes]],
        action: str,
    ) -> dict[str, Any]:
        if not outlook_available():
            raise OutlookError(
                "outlook_unavailable",
                "Outlook automation needs Windows, classic Outlook and `pip install pywin32`.",
                501,
            )
        import pythoncom
        import pywintypes
        import win32com.client

        with self._lock, tempfile.TemporaryDirectory(prefix="bridge-outlook-") as tmp:
            pythoncom.CoInitialize()
            try:
                try:
                    app = win32com.client.Dispatch("Outlook.Application")
                    mail = app.CreateItem(_OL_MAIL_ITEM)
                except pywintypes.com_error as exc:
                    raise OutlookError(
                        "outlook_not_running",
                        f"Could not start Outlook ({exc}). The new Outlook app has no "
                        "automation support; classic Outlook must be installed and set up.",
                        503,
                    ) from exc
                mail.To = "; ".join(to)
                if cc:
                    mail.CC = "; ".join(cc)
                mail.Subject = subject
                if html_body:
                    mail.HTMLBody = html_body
                else:
                    mail.Body = text_body
                for index, (filename, content) in enumerate(attachments):
                    # Own folder per file: two attachments may share a name.
                    folder = Path(tmp) / str(index)
                    folder.mkdir()
                    path = folder / filename
                    path.write_bytes(content)
                    mail.Attachments.Add(str(path))
                try:
                    if action == "display":
                        mail.Display(False)
                        return {"ok": True, "action": "display"}
                    mail.Send()
                    return {"ok": True, "action": "send"}
                except pywintypes.com_error as exc:
                    raise OutlookError("outlook_send_failed", f"Outlook refused the message: {exc}") from exc
            finally:
                pythoncom.CoUninitialize()


def _addresses(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OutlookError("invalid_request", f"{field} must be a list of addresses.", 400)
    cleaned = [item.strip() for item in value if item.strip()]
    bad = [item for item in cleaned if not _ADDRESS.match(item)]
    if bad:
        raise OutlookError("invalid_address", f"Invalid address in {field}: {bad[0]}", 400)
    return cleaned


def parse_outlook_request(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OutlookError("invalid_request", "Expected a JSON object.", 400)
    to = _addresses(data.get("to"), "to")
    cc = _addresses(data.get("cc"), "cc")
    if not to:
        raise OutlookError("invalid_request", "At least one recipient is required.", 400)
    if len(to) + len(cc) > MAX_OUTLOOK_RECIPIENTS:
        raise OutlookError("invalid_request", f"At most {MAX_OUTLOOK_RECIPIENTS} recipients.", 400)
    action = data.get("action") or "send"
    if action not in {"send", "display"}:
        raise OutlookError("invalid_request", "action must be send or display.", 400)
    attachments: list[tuple[str, bytes]] = []
    for item in data.get("attachments") or []:
        if not isinstance(item, dict):
            raise OutlookError("invalid_request", "Each attachment must be an object.", 400)
        name = Path(str(item.get("filename") or "")).name
        if not name:
            raise OutlookError("invalid_request", "An attachment has no filename.", 400)
        try:
            content = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
        except (base64.binascii.Error, ValueError) as exc:
            raise OutlookError("invalid_base64", f"Attachment {name} is not valid base64.", 400) from exc
        attachments.append((name, content))
    subject = str(data.get("subject") or "").replace("\r", " ").replace("\n", " ").strip()
    return {
        "to": to,
        "cc": cc,
        "subject": subject[:255],
        "html_body": str(data.get("html_body") or ""),
        "text_body": str(data.get("text_body") or ""),
        "attachments": attachments,
        "action": action,
    }


def _resolve_allowed_dirs(extra: list[str]) -> list[Path]:
    dirs = [Path.home() / "Downloads"]
    for raw in extra:
        dirs.append(Path(raw).expanduser())
    return [d.resolve() for d in dirs if d.exists()]


def _open_with_default_app(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", str(path)], check=True)
    elif system == "Linux":
        subprocess.run(["xdg-open", str(path)], check=True)
    elif system == "Windows":
        import os

        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


class Handler(BaseHTTPRequestHandler):
    allowed_dirs: list[Path] = []
    token: Optional[str] = None
    mailer: Any = OutlookMailer()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Default logs to stderr with no timestamp context worth keeping;
        # keep it, just route through print so it's visible either way.
        print(f"[local-computer-bridge] {self.address_string()} - {fmt % args}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "allowed_dirs": [str(d) for d in self.allowed_dirs],
                    "outlook": {
                        "available": outlook_available(),
                        "token_configured": bool(self.token),
                    },
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/outlook/send":
            self._handle_outlook_send()
            return
        if self.path == "/save-and-open":
            self._handle_save_and_open()
            return
        if self.path != "/open-file":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return

        requested = str(data.get("path") or data.get("filename") or "").strip()
        if not requested:
            self._send_json(400, {"ok": False, "error": "missing_path"})
            return

        # Safety: only ever open a file that resolves inside an allow-listed
        # directory (~/Downloads by default -- exactly where the browser
        # already put anything the agent generated). A bare filename is
        # tried against every allowed dir; a path is resolved as given, then
        # checked. This is the whole trust boundary -- there is no auth
        # beyond "this port is only reachable from your own machine and the
        # Docker containers on it", so it must never be able to open an
        # arbitrary absolute path the model invents.
        candidates: list[Path] = []
        as_given = Path(requested).expanduser()
        if as_given.is_absolute():
            candidates.append(as_given)
        else:
            candidates.extend(d / requested for d in self.allowed_dirs)

        resolved: Path | None = None
        for candidate in candidates:
            try:
                real = candidate.resolve()
            except OSError:
                continue
            if not real.is_file():
                continue
            if any(
                real == d or d in real.parents for d in self.allowed_dirs
            ):
                resolved = real
                break

        if resolved is None:
            self._send_json(
                403,
                {
                    "ok": False,
                    "error": "not_allowed_or_not_found",
                    "detail": (
                        f"'{requested}' does not resolve to an existing file "
                        f"inside an allowed directory: "
                        f"{[str(d) for d in self.allowed_dirs]}"
                    ),
                },
            )
            return

        try:
            _open_with_default_app(resolved)
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send_json(200, {"ok": True, "opened": str(resolved)})

    def _handle_save_and_open(self) -> None:
        """Write the ACTUAL bytes the backend just produced into the first
        allowed directory (overwriting any stale file of the same name),
        then open it.

        Why this exists: /open-file only ever matches by filename against
        whatever already happens to sit in an allowed directory -- if the
        user never downloaded the artifact this exact turn (or an older
        file with the same generic name, e.g. "rapor.docx", is still
        there from an earlier request), it silently opens the WRONG,
        stale file with no error at all. Sending the real content removes
        that ambiguity entirely.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "missing_body"})
            return
        if length > MAX_SAVE_AND_OPEN_BYTES:
            self._send_json(
                413,
                {
                    "ok": False,
                    "error": "payload_too_large",
                    "detail": (
                        f"{length} bytes exceeds the "
                        f"{MAX_SAVE_AND_OPEN_BYTES}-byte limit for this endpoint."
                    ),
                },
            )
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return

        filename = str(data.get("filename") or "").strip()
        content_b64 = str(data.get("content_base64") or "")
        if not filename or not content_b64:
            self._send_json(
                400, {"ok": False, "error": "missing_filename_or_content"}
            )
            return
        # Same trust boundary as /open-file: never write outside an
        # allowed directory. Path(...).name strips any directory
        # component a caller sent, bare or not.
        safe_name = Path(filename).name
        if not safe_name or not self.allowed_dirs:
            self._send_json(400, {"ok": False, "error": "invalid_filename"})
            return
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (base64.binascii.Error, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid_base64"})
            return

        target = self.allowed_dirs[0] / safe_name
        try:
            target.write_bytes(content)
        except OSError as exc:
            self._send_json(500, {"ok": False, "error": f"write_failed: {exc}"})
            return

        try:
            _open_with_default_app(target)
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send_json(200, {"ok": True, "opened": str(target), "saved": True})

    def _handle_outlook_send(self) -> None:
        if not self.token:
            self._send_json(
                403,
                {
                    "ok": False,
                    "error": "outlook_requires_token",
                    "detail": "Start the bridge with --token (or LOCAL_COMPUTER_TOKEN) to use Outlook.",
                },
            )
            return
        sent = self.headers.get("X-Bridge-Token") or ""
        if not hmac.compare_digest(sent.encode("utf-8"), self.token.encode("utf-8")):
            self._send_json(401, {"ok": False, "error": "invalid_token"})
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._send_json(415, {"ok": False, "error": "json_required"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "missing_body"})
            return
        if length > MAX_OUTLOOK_REQUEST_BYTES:
            self._send_json(413, {"ok": False, "error": "payload_too_large"})
            return
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return
        try:
            message = parse_outlook_request(data)
            result = self.mailer.send(**message)
        except OutlookError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.code, "detail": exc.detail})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": "outlook_failed", "detail": str(exc)})
            return
        self._send_json(200, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}"
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="additional directory to allow opening files from (repeatable)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("LOCAL_COMPUTER_TOKEN") or None,
        help="shared secret required by the Outlook endpoints (default: $LOCAL_COMPUTER_TOKEN)",
    )
    args = parser.parse_args()

    Handler.token = args.token
    Handler.allowed_dirs = _resolve_allowed_dirs(args.allow)
    if not Handler.allowed_dirs:
        print(
            "No allowed directories exist (checked ~/Downloads and any "
            "--allow paths). Nothing would ever be openable -- exiting.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"local-computer-bridge listening on http://127.0.0.1:{args.port}")
    print(f"Allowed directories: {[str(d) for d in Handler.allowed_dirs]}")
    if not Handler.token:
        print("Outlook: off (no --token / LOCAL_COMPUTER_TOKEN)")
    elif not outlook_available():
        print("Outlook: token set, but pywin32 / Windows is missing")
    else:
        print("Outlook: on")
    print("From inside Docker, the backend reaches this via host.docker.internal.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
