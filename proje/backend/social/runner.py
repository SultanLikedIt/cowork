"""Running one upstream social CLI, with the blast radius kept small.

The threat model here is not "the CLI is malicious" -- though rdt-cli is an
unofficial scraper pinned to a git commit, so that is not absurd either. It
is the ordinary one: this process holds ``MODEL_API_KEY``,
``LOCAL_COMPUTER_TOKEN`` and, once Outlook is on, a path to the user's
mailbox. A child process inherits all of it by default. ``subprocess.run``
with no ``env=`` is the entire vulnerability, and it is the default spelling
everywhere, which is why this module exists rather than a helper function
next to the caller.

What it guarantees:

* **An explicit environment.** Built from ``_ENV_ALLOWLIST`` plus the
  credentials this one command declares. ``MODEL_API_KEY`` is never in it.
* **A credential home that survives rebuilds.** rdt-cli hard-codes
  ``~/.config/rdt-cli/credential.json``; the container's ``/root`` is wiped
  by every ``docker compose up --build``, which is often. ``HOME`` is
  redirected into ``SOCIAL_HOME`` (default ``$DATA_DIR/social-sessions/home``) for the
  child only, so the cookie lands on the mounted volume and the backend's
  own ``HOME`` -- Playwright's browser cache, Docling's model path -- is
  untouched.
* **Bounded time and output.** A killed-by-group timeout (reusing
  ``code_exec.terminate_process``), and output captured to files that are
  then read with a byte cap -- pipes plus ``communicate()`` would pull the
  whole of a pathological comment tree into memory before any cap applied.
* **No shell, ever.** argv lists from ``registry.build`` only.
* **Redacted failures.** Whatever the tool prints on stderr is surfaced to
  the model and the logs; a cookie printed in a traceback would end up in
  both, so it is scrubbed first.

What it deliberately does not do: interpret results. Parsing lives with the
platform that produced them, and the quoting of anything a stranger wrote
lives in ``untrusted_content``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .registry import Command

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

# Same reasoning as code_exec._ENV_ALLOWLIST, minus the Python-specific
# entries: enough for a CLI to start, resolve DNS and write a temp file.
_ENV_ALLOWLIST = (
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "SystemRoot",
    "COMSPEC",
    "USERPROFILE",
    # Proxy settings are how a deployment reaches these platforms at all in
    # some networks, so they pass through -- they are configuration, not
    # credentials.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

# Read back at most this much of the child's stdout. A scraper that hits a
# pathological thread could otherwise hand us hundreds of megabytes of JSON
# to hold in memory before anything gets the chance to bound it.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ERROR_CHARS = 600

# Ceiling across the whole backend, not per job. Two reasons, and the second
# is the one that matters: concurrent jobs each doing Reddit reads look, from
# the platform's side, like one account behaving abnormally -- which is how
# an account gets locked. Serializing hard (1) would make two simultaneous
# jobs wait on each other for a minute; 2 is the compromise.
MAX_CONCURRENT_CALLS = int(os.environ.get("SOCIAL_MAX_CONCURRENT") or 2)
MAX_QUEUE_WAIT_SECONDS = 90
_CALL_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_CALLS))


@dataclass(frozen=True)
class RunResult:
    ok: bool
    data: Any = None
    error_code: str = ""
    detail: str = ""
    duration_ms: int = 0

    @property
    def needs_setup(self) -> bool:
        """Failures the user can fix, as opposed to ones to retry or report."""
        return self.error_code in {"not_installed", "auth_required"}


def social_home() -> Path:
    """Where the child's ``HOME`` points, created on demand.

    Mode 0700: this directory holds a session cookie that is equivalent to
    the account's password.
    """
    configured = os.environ.get("SOCIAL_HOME")
    if configured:
        root = Path(configured)
    else:
        # "social-sessions", not "social": DATA_DIR falls back to the backend
        # directory for host development (db.py does the same), and a
        # "social" subdirectory there would land runtime session data inside
        # the `social` source package -- where it is one `git add .` away
        # from committing a live account cookie.
        data_dir = os.environ.get("DATA_DIR") or str(Path(__file__).resolve().parents[1])
        root = Path(data_dir) / "social-sessions" / "home"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        stabilization_logger.warning("could not create social home %s", root, exc_info=True)
    return root


def _read_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    """Read at most ``limit`` bytes, and say whether there was more.

    Reading with a cap rather than reading the file and slicing: a tool
    printing in a loop until the timeout leaves a file far larger than
    memory, and slicing it afterwards is the out-of-memory this avoids.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit)
            overflowed = bool(handle.read(1))
    except OSError:
        return b"", False
    return raw, overflowed


def _child_env(command: Command) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    home = str(social_home())
    # HOME is what Path.home() reads on Linux -- the container, and the only
    # deployment that ships. USERPROFILE is what it reads on Windows, and
    # without it a developer running the backend natively on Windows gets the
    # credential written to their real profile instead of the isolated home:
    # confirmed with `rdt status`, which reported
    # C:\Users\<name>\.config\rdt-cli\credential.json while HOME pointed
    # elsewhere. Setting both keeps the two platforms honest.
    env["HOME"] = home
    env["USERPROFILE"] = home
    # Both, and PYTHONIOENCODING alone is NOT enough -- measured, not
    # assumed. On a Windows host, `rdt search "local llm turkish support"`
    # with only PYTHONIOENCODING set dies with
    #   UnicodeEncodeError: 'charmap' codec can't encode '\U0001f4e3'
    # and returns zero bytes, because one post in the results has a 📣 in
    # its title. The whole search is lost to one emoji. PYTHONUTF8=1 (UTF-8
    # mode) covers the handles PYTHONIOENCODING misses. The Linux container
    # is UTF-8 anyway, so this is purely for host development -- which is
    # exactly where it would otherwise waste an afternoon.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    for name in command.credential_env:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _redact(text: str, command: Command) -> str:
    """Remove credential values before anything is logged or shown.

    Only values this command was actually given can appear, so only those
    are searched for -- a blanket regex over "anything token-shaped" would
    mangle legitimate post content, which is the text this same path carries
    when a read fails halfway.
    """
    cleaned = text or ""
    for name in command.credential_env:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            cleaned = cleaned.replace(value, f"<{name} gizlendi>")
    return cleaned


# Substrings that identify *why* a call failed. Checked against the tool's
# own stderr, lowercased. Order matters: an auth failure that also mentions
# a status code should report as auth, because that is the one the user can
# act on.
_AUTH_MARKERS = (
    "login required",
    "not logged in",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "credential",
    "cookie",
    "session expired",
    "oturum",
)
_RATE_MARKERS = ("429", "rate limit", "too many requests", "slow down")
_NETWORK_MARKERS = (
    "connection",
    "timed out",
    "timeout",
    "dns",
    "name resolution",
    "unreachable",
    "ssl",
    "proxy",
)


# rdt-cli reports failures with its own machine-readable code inside the
# envelope ({"ok": false, "error": {"code": "forbidden", ...}}), which is
# more reliable than matching on a message that changes between releases.
# Substring matching stays as the fallback for tools that have no such code.
_UPSTREAM_CODES = {
    "forbidden": "auth_required",
    "unauthorized": "auth_required",
    "auth_required": "auth_required",
    "login_required": "auth_required",
    "rate_limited": "rate_limited",
    "too_many_requests": "rate_limited",
    "network_error": "network",
    "timeout": "network",
    "not_found": "not_found",
}


def _classify(stderr: str, exit_code: int, *, upstream_code: str = "") -> tuple[str, str]:
    """(error_code, Turkish detail) for a failed call."""
    mapped = _UPSTREAM_CODES.get((upstream_code or "").strip().lower())
    if mapped == "not_found":
        return ("not_found", "Aranan içerik bulunamadı.")
    if mapped == "auth_required":
        return (
            "auth_required",
            "Oturum geçersiz veya süresi dolmuş. Çerezin yenilenmesi gerekiyor "
            "(docs/social-reach.md).",
        )
    if mapped == "rate_limited":
        return (
            "rate_limited",
            "Platform istekleri sınırladı. Bir süre sonra tekrar denenmeli.",
        )
    if mapped == "network":
        return ("network", "Platforma ulaşılamadı (ağ veya proxy sorunu).")
    blob = (stderr or "").lower()
    if any(marker in blob for marker in _AUTH_MARKERS):
        return (
            "auth_required",
            "Oturum geçersiz veya süresi dolmuş. Çerezin yenilenmesi gerekiyor "
            "(docs/social-reach.md).",
        )
    if any(marker in blob for marker in _RATE_MARKERS):
        return (
            "rate_limited",
            "Platform istekleri sınırladı. Bir süre sonra tekrar denenmeli.",
        )
    if any(marker in blob for marker in _NETWORK_MARKERS):
        return ("network", "Platforma ulaşılamadı (ağ veya proxy sorunu).")
    return ("failed", f"Komut {exit_code} koduyla başarısız oldu.")


def run(command: Command, *, home: Optional[Path] = None) -> RunResult:
    """Execute one validated command. Never raises for an expected failure."""
    started = time.monotonic()

    if not shutil.which(command.executable):
        return RunResult(
            ok=False,
            error_code="not_installed",
            detail=(
                f"'{command.executable}' bu sunucuda kurulu değil. "
                "Kurulum: docs/social-reach.md"
            ),
        )

    if not _CALL_SLOTS.acquire(timeout=MAX_QUEUE_WAIT_SECONDS):
        return RunResult(
            ok=False,
            error_code="busy",
            detail=(
                f"Eşzamanlı sosyal medya okuma sınırı ({MAX_CONCURRENT_CALLS}) "
                "dolu ve sıra açılmadı."
            ),
        )

    # Everything after the acquire is inside the try, so the slot comes back
    # on every path. Building the environment first looked harmless, but
    # social_home() touches the filesystem and _child_env reads os.environ:
    # one unexpected exception there and the slot is gone for the life of
    # the process, which after MAX_CONCURRENT_CALLS of them means no job can
    # read social content again until the backend restarts.
    try:
        home_dir = Path(home) if home is not None else social_home()
        env = _child_env(command)
        env["HOME"] = str(home_dir)

        # Output goes to files rather than pipes, for the same reason
        # code_exec does it: `communicate()` reads the child's entire stdout
        # into memory before anything can bound it, so a scraper that walks a
        # pathological comment tree would be an out-of-memory in the backend
        # -- taking every other running job with it -- and the cap below
        # would never be reached.
        with tempfile.TemporaryDirectory(prefix="social-") as scratch:
            out_path = Path(scratch) / "stdout"
            err_path = Path(scratch) / "stderr"
            try:
                with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
                    process = subprocess.Popen(
                        list(command.argv),
                        stdout=out_handle,
                        stderr=err_handle,
                        stdin=subprocess.DEVNULL,
                        env=env,
                        # No cwd inside the job workspace: these tools have no
                        # reason to see job files, and one that writes a cache
                        # next to the user's uploads would have it delivered
                        # to them as an artifact.
                        cwd=str(home_dir),
                        # So the timeout kill reaches anything the tool
                        # spawned (rdt-cli shells out for some backends).
                        start_new_session=(os.name != "nt"),
                    )
                    try:
                        process.wait(timeout=command.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        from code_exec import terminate_process

                        terminate_process(process)
                        return RunResult(
                            ok=False,
                            error_code="timeout",
                            detail=f"{command.timeout_seconds} saniyede yanıt gelmedi.",
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )
            except OSError as exc:
                return RunResult(
                    ok=False,
                    error_code="failed",
                    detail=f"Komut başlatılamadı: {type(exc).__name__}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            raw_out, truncated = _read_bounded(out_path, MAX_OUTPUT_BYTES)
            raw_err, _ = _read_bounded(err_path, 64 * 1024)
            returncode = process.returncode
    finally:
        _CALL_SLOTS.release()

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = raw_out.decode("utf-8", "replace")
    stderr = _redact(raw_err.decode("utf-8", "replace"), command).strip()

    if returncode != 0:
        code, detail = _classify(stderr, returncode)
        tail = [line for line in stderr.splitlines() if line.strip()]
        if tail:
            detail = f"{detail} ({tail[-1][:MAX_ERROR_CHARS]})"
        return RunResult(ok=False, error_code=code, detail=detail, duration_ms=duration_ms)

    if truncated:
        # Partial JSON cannot be parsed, and guessing at a repair would
        # invent data. Reporting it is the honest failure.
        return RunResult(
            ok=False,
            error_code="too_large",
            detail="Yanıt çok büyük olduğu için okunamadı.",
            duration_ms=duration_ms,
        )

    return _envelope(stdout, command, duration_ms)


def _envelope(stdout: str, command: Command, duration_ms: int) -> RunResult:
    """Unwrap the ``{ok, schema_version, data, error}`` envelope.

    Tolerant on purpose. These tools are unofficial and change; a payload
    that is valid JSON but not the documented envelope is far more likely to
    be a version drift than an attack, and returning it raw lets the parser
    above decide, while a hard schema check here would turn every upstream
    release into an outage.
    """
    text = (stdout or "").strip()
    if not text:
        return RunResult(
            ok=False,
            error_code="bad_output",
            detail="Araç boş yanıt döndürdü.",
            duration_ms=duration_ms,
        )
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        stabilization_logger.warning(
            "social: %s returned non-JSON output", command.executable
        )
        return RunResult(
            ok=False,
            error_code="bad_output",
            detail="Araç beklenen JSON biçiminde yanıt vermedi.",
            duration_ms=duration_ms,
        )

    if isinstance(payload, dict) and "ok" in payload:
        if payload.get("ok"):
            return RunResult(ok=True, data=payload.get("data"), duration_ms=duration_ms)
        error = payload.get("error")
        detail = ""
        upstream_code = ""
        if isinstance(error, dict):
            upstream_code = str(error.get("code") or "")
            detail = str(error.get("message") or upstream_code or "")
        elif error:
            detail = str(error)
        code, fallback = _classify(detail, 1, upstream_code=upstream_code)
        return RunResult(
            ok=False,
            error_code=code,
            detail=_redact(detail, command)[:MAX_ERROR_CHARS] or fallback,
            duration_ms=duration_ms,
        )

    return RunResult(ok=True, data=payload, duration_ms=duration_ms)
