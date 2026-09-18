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
- POST /outlook/mail/search {"folders": ["inbox"|"sent"], "since",
  "unread_only", "sender", "subject", "query", "with_attachments_only",
  "limit"}: message headers only (sender, subject, time, attachment
  names, a short preview) -- never full bodies, so a broad search cannot
  return the whole mailbox.
- POST /outlook/mail/get {"id", "store_id"}: one message, with its body.
- POST /outlook/mail/attachment {"id", "store_id", "index"}: one
  attachment's bytes, base64.
  These three are READ-ONLY: nothing is marked read, moved or deleted,
  and only the Inbox and Sent Items are reachable (_READABLE_FOLDERS).
  Same token requirement as /outlook/send.

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
from pathlib import Path, PurePosixPath
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

# -- mail reading ------------------------------------------------------------
# Outlook's own folder ids (OlDefaultFolders). Only these two are reachable:
# the request names a folder by one of the keys in _READABLE_FOLDERS, never by
# a path or an id of its own, so no request can widen what is readable.
_OL_FOLDER_INBOX = 6
_OL_FOLDER_SENT = 5
_READABLE_FOLDERS = {"inbox": _OL_FOLDER_INBOX, "sent": _OL_FOLDER_SENT}
# How many items one search may walk. Restrict() narrows by date first, so
# this is the ceiling on an unfiltered "everything since X" pass, not a
# typical cost -- a mailbox with 50k items must not turn one request into a
# minutes-long COM walk holding the mailer lock.
MAX_MAIL_SCAN = 400
MAX_MAIL_RESULTS = 50
# A body is evidence, not a document: the backend truncates again before it
# reaches a model, and an enormous newsletter must not cost a 5MB response.
MAX_MAIL_BODY_CHARS = 20_000
MAX_MAIL_PREVIEW_CHARS = 300
MAX_MAIL_ATTACHMENT_BYTES = 20 * 1024 * 1024
# Outlook marks embedded images (signature logos, tracking pixels) as
# attachments too. They are never what "read the attachment" means and they
# arrive on nearly every corporate message, so they are filtered out by type.
_OL_ATTACHMENT_BY_VALUE = 1


class OutlookError(Exception):
    def __init__(self, code: str, detail: str, status: int = 500) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _basename(value: Any) -> str:
    """Last path component, treating BOTH / and \\ as separators.

    ``Path(name).name`` is not enough: it only knows the separators of the
    platform it runs on, so on a POSIX host "..\\..\\evil.bat" comes back
    whole. The bridge normally runs on Windows, where that is moot -- but
    these names come from Outlook (Windows) and are also sent onward to a
    backend that runs in a Linux container, so the sanitizing must not
    depend on which side is asking. Anything that still looks like a path
    after stripping, or is a bare dot entry, yields "" for the caller to
    reject.
    """
    text = str(value or "").replace("\\", "/")
    name = PurePosixPath(text).name.strip()
    if name in {"", ".", ".."}:
        return ""
    return name


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


class OutlookReader:
    """Read-only view of the Inbox and Sent Items of the open Outlook profile.

    Shares OutlookMailer's lock rather than taking one of its own: both talk
    to the same single-threaded COM object model, and a search walking a few
    hundred items while a send is building a message is exactly the kind of
    concurrent access that makes Outlook return E_FAIL at random.

    Nothing here writes. Messages are never marked read (the COM properties
    touched below are all reads), never moved and never deleted -- reading
    the user's mail is already the sensitive half of this feature, and a
    background job silently clearing someone's unread flags would be its own
    bug report.
    """

    _lock = OutlookMailer._lock

    def _namespace(self):
        import pywintypes
        import win32com.client

        try:
            app = win32com.client.Dispatch("Outlook.Application")
            return app.GetNamespace("MAPI")
        except pywintypes.com_error as exc:
            raise OutlookError(
                "outlook_not_running",
                f"Could not start Outlook ({exc}). The new Outlook app has no "
                "automation support; classic Outlook must be installed and set up.",
                503,
            ) from exc

    def _require(self) -> None:
        if not outlook_available():
            raise OutlookError(
                "outlook_unavailable",
                "Outlook automation needs Windows, classic Outlook and `pip install pywin32`.",
                501,
            )

    def search(
        self,
        *,
        folders: list[str],
        since: Optional[float],
        unread_only: bool,
        sender: str,
        subject: str,
        query: str,
        with_attachments_only: bool,
        limit: int,
    ) -> dict[str, Any]:
        self._require()
        import pythoncom

        with self._lock:
            pythoncom.CoInitialize()
            try:
                found: list[dict[str, Any]] = []
                scanned = 0
                for name in folders:
                    hits, walked = self._scan_folder(
                        name,
                        since=since,
                        unread_only=unread_only,
                        sender=sender,
                        subject=subject,
                        query=query,
                        with_attachments_only=with_attachments_only,
                        limit=limit,
                        budget=MAX_MAIL_SCAN - scanned,
                    )
                    found.extend(hits)
                    scanned += walked
                # Both folders were walked newest-first independently; the
                # merged list has to be re-sorted or "the 10 most recent"
                # would mean "10 from whichever folder came first".
                found.sort(key=lambda item: item.get("received") or "", reverse=True)
                return {
                    "ok": True,
                    "messages": found[:limit],
                    "scanned": scanned,
                    "truncated": scanned >= MAX_MAIL_SCAN,
                }
            finally:
                pythoncom.CoUninitialize()

    def _scan_folder(
        self,
        folder_name: str,
        *,
        since: Optional[float],
        unread_only: bool,
        sender: str,
        subject: str,
        query: str,
        with_attachments_only: bool,
        limit: int,
        budget: int,
    ) -> tuple[list[dict[str, Any]], int]:
        import pywintypes

        if budget <= 0:
            return [], 0
        namespace = self._namespace()
        try:
            folder = namespace.GetDefaultFolder(_READABLE_FOLDERS[folder_name])
            items = folder.Items
        except pywintypes.com_error as exc:
            raise OutlookError(
                "outlook_folder_unavailable",
                f"Could not open the {folder_name} folder ({exc}).",
                503,
            ) from exc

        # Sent Items stamps SentOn; the Inbox stamps ReceivedTime. Sorting a
        # folder by a property its items do not carry silently returns them
        # in arrival order instead, which would make "the newest 10" wrong
        # in exactly the folder nobody checks.
        time_field = "[SentOn]" if folder_name == "sent" else "[ReceivedTime]"
        try:
            items.Sort(time_field, True)
        except pywintypes.com_error:
            pass

        # Deliberately NOT items.Restrict("[ReceivedTime] >= '...'"): Restrict
        # parses its date literal in the *Windows locale*, so the format that
        # works on an en-US machine raises or silently matches nothing on the
        # tr-TR machines this app is built for. Walking a descending sort and
        # stopping at the cutoff is locale-proof and, because it stops early,
        # no more expensive in the normal case.
        sender_cf = sender.casefold()
        subject_cf = subject.casefold()
        query_cf = query.casefold()
        hits: list[dict[str, Any]] = []
        walked = 0
        for item in items:
            if walked >= budget or len(hits) >= limit:
                break
            walked += 1
            try:
                # 43 is olMail. A folder also holds meeting requests, reports
                # and calendar items, whose properties differ enough that
                # reading them as mail throws.
                if int(getattr(item, "Class", 0) or 0) != 43:
                    continue
                stamp = self._timestamp(item, folder_name)
                if since is not None and stamp is not None and stamp < since:
                    # Sorted newest-first, so everything after this is older.
                    break
                if unread_only and not bool(getattr(item, "UnRead", False)):
                    continue
                header = self._header(item, folder_name, stamp)
                if with_attachments_only and not header["attachments"]:
                    continue
                if sender_cf and sender_cf not in (
                    header["from"] + " " + header["from_name"]
                ).casefold():
                    continue
                if subject_cf and subject_cf not in header["subject"].casefold():
                    continue
                if query_cf and query_cf not in (
                    header["subject"] + " " + header["preview"]
                ).casefold():
                    continue
                hits.append(header)
            except pywintypes.com_error:
                # One unreadable item (a corrupt entry, a message being
                # synced right now) must not fail the whole search.
                continue
        return hits, walked

    @staticmethod
    def _timestamp(item: Any, folder_name: str) -> Optional[float]:
        raw = getattr(item, "SentOn" if folder_name == "sent" else "ReceivedTime", None)
        if raw is None:
            return None
        try:
            return float(raw.timestamp())
        except (AttributeError, ValueError, OSError):
            return None

    @staticmethod
    def _iso(stamp: Optional[float]) -> str:
        if stamp is None:
            return ""
        from datetime import datetime, timezone

        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()

    @staticmethod
    def _smtp_of(item: Any) -> tuple[str, str]:
        """(address, display name) of the sender, as SMTP wherever possible.

        On Exchange, SenderEmailAddress is an X500 distinguished name
        (/O=EXCHANGE/OU=.../CN=RECIPIENTS/CN=USER), which is useless both to
        a person reading the summary and to a "from:" filter. The
        PropertyAccessor read is PR_SENDER_SMTP_ADDRESS, which carries the
        real address for those accounts.
        """
        import pywintypes

        name = str(getattr(item, "SenderName", "") or "")
        address = str(getattr(item, "SenderEmailAddress", "") or "")
        if address and not address.startswith("/"):
            return address, name
        try:
            accessor = item.PropertyAccessor
            smtp = accessor.GetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x5D01001E"
            )
            if smtp:
                return str(smtp), name
        except (pywintypes.com_error, AttributeError):
            pass
        return address, name

    @classmethod
    def _recipients(cls, item: Any) -> list[str]:
        import pywintypes

        out: list[str] = []
        try:
            for recipient in item.Recipients:
                try:
                    address = str(getattr(recipient, "Address", "") or "")
                    if address.startswith("/"):
                        accessor = recipient.PropertyAccessor
                        address = str(
                            accessor.GetProperty(
                                "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
                            )
                            or address
                        )
                    label = str(getattr(recipient, "Name", "") or "")
                    out.append(address or label)
                except pywintypes.com_error:
                    continue
        except (pywintypes.com_error, AttributeError):
            return out
        return out[:MAX_OUTLOOK_RECIPIENTS]

    @classmethod
    def _attachment_list(cls, item: Any) -> list[dict[str, Any]]:
        import pywintypes

        out: list[dict[str, Any]] = []
        try:
            for index in range(1, int(item.Attachments.Count) + 1):
                try:
                    attachment = item.Attachments.Item(index)
                    if int(getattr(attachment, "Type", 0) or 0) != _OL_ATTACHMENT_BY_VALUE:
                        continue
                    name = _basename(getattr(attachment, "FileName", ""))
                    if not name:
                        continue
                    out.append(
                        {
                            "index": index,
                            "filename": name,
                            "size_bytes": int(getattr(attachment, "Size", 0) or 0),
                        }
                    )
                except pywintypes.com_error:
                    continue
        except (pywintypes.com_error, AttributeError):
            return out
        return out

    @classmethod
    def _header(cls, item: Any, folder_name: str, stamp: Optional[float]) -> dict[str, Any]:
        address, name = cls._smtp_of(item)
        body = str(getattr(item, "Body", "") or "")
        preview = " ".join(body.split())[:MAX_MAIL_PREVIEW_CHARS]
        return {
            "id": str(getattr(item, "EntryID", "") or ""),
            "store_id": str(
                getattr(getattr(item, "Parent", None), "StoreID", "") or ""
            ),
            "folder": folder_name,
            "from": address,
            "from_name": name,
            "to": cls._recipients(item),
            "subject": str(getattr(item, "Subject", "") or ""),
            "received": cls._iso(stamp),
            "unread": bool(getattr(item, "UnRead", False)),
            "preview": preview,
            "attachments": cls._attachment_list(item),
        }

    def _item_by_id(self, namespace: Any, entry_id: str, store_id: str) -> Any:
        import pywintypes

        try:
            if store_id:
                return namespace.GetItemFromID(entry_id, store_id)
            return namespace.GetItemFromID(entry_id)
        except pywintypes.com_error as exc:
            raise OutlookError(
                "message_not_found",
                f"No message with that id is in this mailbox ({exc}).",
                404,
            ) from exc

    def get(self, *, entry_id: str, store_id: str) -> dict[str, Any]:
        self._require()
        import pythoncom

        with self._lock:
            pythoncom.CoInitialize()
            try:
                namespace = self._namespace()
                item = self._item_by_id(namespace, entry_id, store_id)
                folder_name = "sent" if self._in_sent(item) else "inbox"
                stamp = self._timestamp(item, folder_name)
                header = self._header(item, folder_name, stamp)
                body = str(getattr(item, "Body", "") or "")
                header["body"] = body[:MAX_MAIL_BODY_CHARS]
                header["body_truncated"] = len(body) > MAX_MAIL_BODY_CHARS
                return {"ok": True, "message": header}
            finally:
                pythoncom.CoUninitialize()

    @staticmethod
    def _in_sent(item: Any) -> bool:
        try:
            return str(getattr(item.Parent, "Name", "") or "").casefold() in {
                "sent items",
                "gönderilmiş öğeler",
                "gonderilmis ogeler",
            }
        except AttributeError:
            return False

    def attachment(self, *, entry_id: str, store_id: str, index: int) -> dict[str, Any]:
        self._require()
        import pythoncom
        import pywintypes

        with self._lock, tempfile.TemporaryDirectory(prefix="bridge-mail-") as tmp:
            pythoncom.CoInitialize()
            try:
                namespace = self._namespace()
                item = self._item_by_id(namespace, entry_id, store_id)
                try:
                    attachment = item.Attachments.Item(index)
                except pywintypes.com_error as exc:
                    raise OutlookError(
                        "attachment_not_found",
                        f"That message has no attachment {index} ({exc}).",
                        404,
                    ) from exc
                name = _basename(getattr(attachment, "FileName", ""))
                if not name:
                    raise OutlookError(
                        "attachment_not_found", "That attachment has no filename.", 404
                    )
                size = int(getattr(attachment, "Size", 0) or 0)
                if size > MAX_MAIL_ATTACHMENT_BYTES:
                    raise OutlookError(
                        "attachment_too_large",
                        f"{name} is {size} bytes; the limit is {MAX_MAIL_ATTACHMENT_BYTES}.",
                        413,
                    )
                # SaveAsFile into a private temp dir is the only way COM
                # hands over the bytes; it is deleted with the dir below.
                path = Path(tmp) / name
                try:
                    attachment.SaveAsFile(str(path))
                except pywintypes.com_error as exc:
                    raise OutlookError(
                        "attachment_failed", f"Outlook could not export {name} ({exc})."
                    ) from exc
                content = path.read_bytes()
                return {
                    "ok": True,
                    "filename": name,
                    "size_bytes": len(content),
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            finally:
                pythoncom.CoUninitialize()


def _folders(value: Any) -> list[str]:
    if value is None:
        return ["inbox"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise OutlookError("invalid_request", "folders must be a list.", 400)
    out: list[str] = []
    for raw in value:
        name = str(raw or "").strip().casefold()
        if name not in _READABLE_FOLDERS:
            raise OutlookError(
                "invalid_folder",
                f"Only {', '.join(sorted(_READABLE_FOLDERS))} can be read; got {name!r}.",
                400,
            )
        if name not in out:
            out.append(name)
    return out or ["inbox"]


def parse_mail_search_request(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OutlookError("invalid_request", "Expected a JSON object.", 400)
    since = data.get("since")
    if since is not None:
        try:
            since = float(since)
        except (TypeError, ValueError) as exc:
            raise OutlookError(
                "invalid_request", "since must be a unix timestamp.", 400
            ) from exc
    try:
        limit = int(data.get("limit") or 20)
    except (TypeError, ValueError) as exc:
        raise OutlookError("invalid_request", "limit must be a number.", 400) from exc
    return {
        "folders": _folders(data.get("folders")),
        "since": since,
        "unread_only": bool(data.get("unread_only")),
        "sender": str(data.get("sender") or "").strip(),
        "subject": str(data.get("subject") or "").strip(),
        "query": str(data.get("query") or "").strip(),
        "with_attachments_only": bool(data.get("with_attachments_only")),
        "limit": max(1, min(limit, MAX_MAIL_RESULTS)),
    }


def parse_mail_get_request(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OutlookError("invalid_request", "Expected a JSON object.", 400)
    entry_id = str(data.get("id") or "").strip()
    if not entry_id:
        raise OutlookError("invalid_request", "id is required.", 400)
    return {"entry_id": entry_id, "store_id": str(data.get("store_id") or "").strip()}


def parse_mail_attachment_request(data: Any) -> dict[str, Any]:
    parsed = parse_mail_get_request(data)
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError) as exc:
        raise OutlookError("invalid_request", "index must be a number.", 400) from exc
    if index < 1:
        raise OutlookError("invalid_request", "index is 1-based.", 400)
    parsed["index"] = index
    return parsed


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
        name = _basename(item.get("filename"))
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
    reader: Any = OutlookReader()

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
                        # Lets the backend tell "this bridge is too old to
                        # read mail" apart from "Outlook is not installed" --
                        # a 404 on /outlook/mail/search alone cannot say
                        # which, and the two need different fixes.
                        "can_read": True,
                        "readable_folders": sorted(_READABLE_FOLDERS),
                    },
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/outlook/send":
            self._handle_outlook_send()
            return
        if self.path == "/outlook/mail/search":
            self._handle_mail_search()
            return
        if self.path == "/outlook/mail/get":
            self._handle_mail_get()
            return
        if self.path == "/outlook/mail/attachment":
            self._handle_mail_attachment()
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

    def _outlook_body(self) -> Optional[Any]:
        """Token + content checks shared by every Outlook endpoint.

        Returns the decoded body, or None when it has already answered with
        the reason it refused. The token requirement is the whole CSRF story
        for this port (see the module docstring), so it has to be identical
        on the reading endpoints as on /outlook/send -- reading someone's
        mailbox from a stray browser tab is no better than sending as them.
        """
        if not self.token:
            self._send_json(
                403,
                {
                    "ok": False,
                    "error": "outlook_requires_token",
                    "detail": "Start the bridge with --token (or LOCAL_COMPUTER_TOKEN) to use Outlook.",
                },
            )
            return None
        sent = self.headers.get("X-Bridge-Token") or ""
        if not hmac.compare_digest(sent.encode("utf-8"), self.token.encode("utf-8")):
            self._send_json(401, {"ok": False, "error": "invalid_token"})
            return None
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._send_json(415, {"ok": False, "error": "json_required"})
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "missing_body"})
            return None
        if length > MAX_OUTLOOK_REQUEST_BYTES:
            self._send_json(413, {"ok": False, "error": "payload_too_large"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return None

    def _run_outlook(self, data: Any, parse: Any, call: Any) -> None:
        try:
            result = call(**parse(data))
        except OutlookError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.code, "detail": exc.detail})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": "outlook_failed", "detail": str(exc)})
            return
        self._send_json(200, result)

    def _handle_outlook_send(self) -> None:
        data = self._outlook_body()
        if data is None:
            return
        self._run_outlook(data, parse_outlook_request, self.mailer.send)

    def _handle_mail_search(self) -> None:
        data = self._outlook_body()
        if data is None:
            return
        self._run_outlook(data, parse_mail_search_request, self.reader.search)

    def _handle_mail_get(self) -> None:
        data = self._outlook_body()
        if data is None:
            return
        self._run_outlook(data, parse_mail_get_request, self.reader.get)

    def _handle_mail_attachment(self) -> None:
        data = self._outlook_body()
        if data is None:
            return
        self._run_outlook(data, parse_mail_attachment_request, self.reader.attachment)


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
