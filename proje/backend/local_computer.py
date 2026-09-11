"""Opens a just-created artifact in its default app on the user's own
machine -- not the backend container's filesystem, which the user's OS
cannot see.

Requires ``scripts/local_computer_bridge.py`` running natively on that
same machine (Mac, Windows, or Linux -- the bridge script picks the right
`open`/`os.startfile`/`xdg-open` call itself); this module is only the
backend-side HTTP client for it, reached through Docker's
``host.docker.internal`` DNS name, which works the same way regardless of
the host OS. Same-machine only for now: this name resolves to whichever
machine is running the Docker containers, not to some other browser
session on a different computer.

Deliberately not real MCP (Model Context Protocol) wire format -- see the
docstring in ``scripts/local_computer_bridge.py`` for why a minimal,
dependency-free HTTP bridge was chosen over hand-rolling MCP's async
JSON-RPC/session-handshake protocol inside this backend's existing
synchronous tool-binding code (capabilities.py).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Optional, Sequence

import requests

LOCAL_COMPUTER_ENABLED = (
    os.environ.get("LOCAL_COMPUTER_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
LOCAL_COMPUTER_URL = (
    os.environ.get("LOCAL_COMPUTER_URL") or "http://host.docker.internal:8765"
).rstrip("/")
_HEALTH_TIMEOUT_SECONDS = 2.0
_OPEN_TIMEOUT_SECONDS = 10.0
_SAVE_AND_OPEN_TIMEOUT_SECONDS = 20.0
# Below the bridge's own 40MB cap (base64 adds ~33%), leaving headroom for
# JSON framing. A file resolved from the workspace above this size falls
# back to the old by-name /open-file call instead of failing outright.
_MAX_SAVE_AND_OPEN_BYTES = 30 * 1024 * 1024


def is_enabled() -> bool:
    return LOCAL_COMPUTER_ENABLED


def bridge_reachable(base_url: str = LOCAL_COMPUTER_URL) -> bool:
    """Best-effort reachability check -- used to decide whether to bind the
    tool at all (see capabilities.py), not to gate an individual call."""
    try:
        response = requests.get(
            f"{base_url}/health", timeout=_HEALTH_TIMEOUT_SECONDS
        )
        return response.ok
    except requests.RequestException:
        return False


def _latest_artifact_filename(workspace_root: str) -> Optional[str]:
    """Most recently created artifact's filename, or None if there isn't one.

    Reuses artifacts.scan_artifacts (the same disk-scan the /jobs API uses
    for job.artifacts) rather than trusting anything the model claims was
    produced -- same "only what's actually on disk" rule the artifact
    download route already follows.
    """
    entry = _latest_artifact_entry(workspace_root)
    if entry is None:
        return None
    filename = entry.get("filename")
    return str(filename) if filename else None


def _latest_artifact_entry(workspace_root: str) -> Optional[dict[str, Any]]:
    from artifacts import scan_artifacts

    found = scan_artifacts(workspace_root)
    if not found:
        return None
    found.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return found[0]


def _latest_artifact_disk_path(workspace_root: str) -> Optional[Path]:
    """Absolute on-disk path of the most recently created artifact, so its
    real bytes can be read and sent to the bridge (see
    LocalComputerTools.open_local_file) -- not just its bare filename,
    which the bridge can only ever match against whatever already happens
    to sit in an allowed directory on the user's machine."""
    entry = _latest_artifact_entry(workspace_root)
    if entry is None:
        return None
    rel_path = entry.get("path")
    if not rel_path:
        return None
    return Path(workspace_root) / str(rel_path)


class LocalComputerTools:
    """Bound as the LOCAL_COMPUTER capability's tool (see capabilities.py)."""

    def __init__(
        self,
        *,
        workspace_root: Optional[str],
        base_url: str = LOCAL_COMPUTER_URL,
        fallback_workspace_roots: Sequence[str] = (),
    ) -> None:
        self.workspace_root = workspace_root
        self.base_url = base_url
        # A brand-new job's own workspace starts empty -- these are prior
        # jobs' workspace roots in the same conversation, most-recent-first,
        # searched only when the current job's own workspace has nothing.
        # See db.workspace_roots_for_conversation.
        self.fallback_workspace_roots = fallback_workspace_roots

    def open_local_file(self, filename: str = "") -> dict[str, Any]:
        """Open a file on the user's own computer in its default app.

        Args:
            filename (str): The file to open, by name only (e.g.
                "rapor.docx") -- never a full path. Omit it to open the
                most recently created deliverable in this job.
        """
        target = str(filename or "").strip()
        source_path: Optional[Path] = None
        if not target:
            for root in (self.workspace_root, *self.fallback_workspace_roots):
                if not root:
                    continue
                path = _latest_artifact_disk_path(root)
                if path is not None:
                    target = path.name
                    source_path = path
                    break
        if not target:
            return {
                "ok": False,
                "error": "no_filename",
                "detail": "No filename given and no artifact was found for this job.",
            }
        # Only the bare filename ever leaves this process as a NAME -- the
        # container-side workspace path is meaningless to the user's OS.
        target = Path(target).name

        # Resolve a NAMED file to its bytes too, not just the unnamed
        # "latest artifact" case. Without this a caller that names the file
        # -- which is the only way to open more than one -- sent a bare
        # name the bridge could match only against files that already
        # happened to sit in an allowed directory, so opening a freshly
        # created artifact by name always failed.
        if source_path is None:
            for root in (self.workspace_root, *self.fallback_workspace_roots):
                if not root:
                    continue
                candidate = Path(root) / "artifacts" / target
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        source_path = candidate
                        break
                except OSError:
                    continue

        if source_path is not None and source_path.is_file():
            try:
                size = source_path.stat().st_size
            except OSError:
                size = None
            if size is not None and size <= _MAX_SAVE_AND_OPEN_BYTES:
                result = self._save_and_open(target, source_path)
                if result is not None:
                    return result
                # None means the bridge doesn't support this endpoint yet
                # (an older bridge process still running) -- fall through
                # to the by-name request below as a best-effort.
        return self._open_by_name(target)

    def _save_and_open(self, filename: str, path: Path) -> Optional[dict[str, Any]]:
        """Send the artifact's real bytes to the bridge so it saves and
        opens THIS file, never an unrelated stale file that merely shares
        the same name (real bug: every Word report defaults to
        "rapor.docx", and a follow-up "open it" opened a WEEK-OLD
        "rapor.docx" already sitting in ~/Downloads from an earlier,
        unrelated request instead of the one just created).

        Returns None (not a dict) when the bridge doesn't recognize this
        endpoint at all (404) -- the one signal that should fall back to
        the older by-name call rather than being reported as a failure.
        """
        try:
            content = path.read_bytes()
        except OSError as exc:
            return {
                "ok": False,
                "error": "read_failed",
                "detail": f"Could not read {filename} to send it: {exc}",
            }
        try:
            response = requests.post(
                f"{self.base_url}/save-and-open",
                json={
                    "filename": filename,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
                timeout=_SAVE_AND_OPEN_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            return {
                "ok": False,
                "error": "bridge_unreachable",
                "detail": (
                    f"Could not reach the local computer bridge at "
                    f"{self.base_url} ({exc}). It must be started on the "
                    f"user's own machine first (scripts/local_computer_bridge.py)."
                ),
            }
        if response.status_code == 404:
            return None
        return self._parse_bridge_response(response)

    def _open_by_name(self, filename: str) -> dict[str, Any]:
        """Ask the bridge to open a file that already exists in one of its
        allowed directories, by name only -- the original mechanism, kept
        as a fallback for an explicit filename with no known disk path, or
        an older bridge process that predates /save-and-open."""
        try:
            response = requests.post(
                f"{self.base_url}/open-file",
                json={"filename": filename},
                timeout=_OPEN_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            return {
                "ok": False,
                "error": "bridge_unreachable",
                "detail": (
                    f"Could not reach the local computer bridge at "
                    f"{self.base_url} ({exc}). It must be started on the "
                    f"user's own machine first (scripts/local_computer_bridge.py)."
                ),
            }
        return self._parse_bridge_response(response)

    @staticmethod
    def _parse_bridge_response(response: "requests.Response") -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"ok": False, "error": "invalid_bridge_response"}
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "invalid_bridge_response"}
        payload.setdefault("ok", response.ok)
        return payload

    def as_tools(self) -> list[Any]:
        return [self.open_local_file]
