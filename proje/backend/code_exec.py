"""Workspace-confined, bounded Python execution.

Why not CAMEL's CodeExecutionToolkit
------------------------------------
It was already bound to dynamic Workforce workers, and its default sandbox is
``subprocess`` (code_execution.py:63) with ``timeout=None``. That means: the
host interpreter, the backend's own working directory, the backend's full
environment (including MODEL_API_KEY), no wall-clock limit, and no bound on
output size. A single non-terminating loop from a model would hang a job
thread until the workforce task timeout, and any generated code could read the
API key straight out of ``os.environ``.

This module replaces it with an executor that runs the same kind of code under
real constraints:

  * **Confined** -- the child process's cwd is the job workspace, and nothing
    outside it is passed in.
  * **Bounded time** -- a hard wall-clock timeout, killed by process group so
    a child that spawns children still dies.
  * **Bounded output** -- stdout/stderr go to files and only a capped prefix
    is read back, so a program printing in a loop cannot be turned into an
    out-of-memory in the backend.
  * **Scrubbed environment** -- an explicit allowlist. No API keys.
  * **Network off by default** -- see `_GUARD` below for exactly how far that
    goes, and where it stops.
  * **Explicit failure** -- syntax error, runtime exception and timeout each
    return a structured, bounded result the model can act on, instead of an
    exception that fails the whole subtask.

Honest limitation
-----------------
This is process-level confinement, not a sandbox. The guard is installed
in-process by the runner, so code that deliberately sets out to undo it (
re-importing the C-level socket module, calling ``os.system``) can. It stops
accidental and incidental escapes -- which is what actually happens when a
model writes data-processing code -- not a determined adversary. Real
isolation needs a container or seccomp and is deferred; ``docker`` is already
a supported CAMEL sandbox if this deployment later runs untrusted input.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

# --- Bounds ---------------------------------------------------------------
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20_000
MAX_CODE_CHARS = 100_000
# Per run, shared by every worker/subtask. Code execution is the most
# expensive capability per call, and a model that has failed the same script
# eight times is looping, not converging.
MAX_EXECUTIONS_PER_RUN = 12

# Process-level ceiling across the WHOLE backend, not per job.
#
# Per-run budgets bound one job; nothing bounded the host. Concurrent jobs each
# get their own CodeExecutionTools, so N simultaneous jobs could hold N Python
# interpreters -- each free to allocate memory until the OS intervenes, since
# there is no RLIMIT in this implementation. A handful of jobs running pandas
# over a large frame is enough to make the backend itself unresponsive, which
# takes down every OTHER job too.
#
# 4 is deliberately conservative for a single-container deployment. Waiting is
# the right behaviour when the limit is reached: the work is legitimate, it
# just has to queue. The wait is bounded so a stuck slot cannot block a job
# forever.
MAX_CONCURRENT_EXECUTIONS = int(os.environ.get("MAX_CONCURRENT_EXECUTIONS") or 4)
MAX_EXECUTION_QUEUE_WAIT_SECONDS = 120
_EXECUTION_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_EXECUTIONS))

# Environment variables the child is allowed to see. Everything else --
# MODEL_API_KEY above all -- is dropped. PATH/HOME/TMPDIR are needed for the
# interpreter itself to start and for tempfile to work; the locale variables
# stop pandas/openpyxl from misparsing non-ASCII data.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")

# Installed by the runner before the user's script is executed. Two jobs:
# disable outbound network at the Python level, and make the failure message
# say why rather than looking like a transient connection error.
_GUARD = '''
import builtins, socket

class NetworkDisabled(RuntimeError):
    pass

_MESSAGE = (
    "Network access is disabled in this execution environment. "
    "Use web_search/web_read to retrieve data, then pass it "
    "into your code, or read it from the workspace files."
)

def _blocked(*args, **kwargs):
    raise NetworkDisabled(_MESSAGE)

socket.socket = _blocked
socket.create_connection = _blocked
socket.socketpair = _blocked
try:
    socket.gethostbyname = _blocked
    socket.getaddrinfo = _blocked
except Exception:
    pass
'''

_RUNNER = '''
import runpy, sys

GUARD = sys.argv[1]
SCRIPT = sys.argv[2]

with open(GUARD, "r", encoding="utf-8") as handle:
    exec(compile(handle.read(), "<network-guard>", "exec"), {"__name__": "_guard"})

sys.argv = [SCRIPT]
runpy.run_path(SCRIPT, run_name="__main__")
'''


class CodeExecutionTools:
    """Bounded Python execution scoped to one job's workspace.

    One instance per run (capabilities.resolve_tools), which makes
    MAX_EXECUTIONS_PER_RUN a real per-job budget.
    """

    def __init__(self, workspace_root: Path, python_executable: Optional[str] = None) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        self.python = python_executable or sys.executable
        self._executions = 0
        # Created up front so generated code can always write here without
        # first having to mkdir -- and so the path named in the tool
        # docstring is guaranteed to exist.
        (self.workspace_root / "artifacts").mkdir(mode=0o700, parents=True, exist_ok=True)

    def _child_env(self) -> dict[str, str]:
        env = {
            key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ
        }
        # Keeps the child from writing .pyc files into the workspace, which
        # would then show up as job artifacts.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Unbuffered, so output written before a timeout kill is not lost in
        # a buffer that never flushes -- a timed-out run's partial output is
        # usually the most useful thing it produced.
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _snapshot(self) -> dict[str, float]:
        """Files under the workspace, so created/modified ones can be reported.

        The model claiming "I wrote the file" is not evidence; a path that
        appears in this diff is.
        """
        found: dict[str, float] = {}
        for path in self.workspace_root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                found[path.relative_to(self.workspace_root).as_posix()] = path.stat().st_mtime
            except OSError:
                continue
            if len(found) > 2000:
                break
        return found

    def run_python(
        self, code: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Run Python code inside this job's workspace and return its output.

        The working directory is the job workspace: uploaded files are under
        "sources/" and anything you write to "artifacts/" is delivered to the
        user as a real downloadable file. pandas and openpyxl are available,
        so this is the right way to transform a spreadsheet, compute over a
        CSV, or generate a large JSON file -- especially when the data is too
        big to pass through a tool argument.

        Do NOT use it to print or format prose for an answer -- write that
        answer directly instead. A report-writing task that reaches for code
        execution to emit text is the failure this warning exists to prevent:
        it once produced a 16,466-character `code` argument that overran the
        completion budget and truncated the tool call mid-string.

        Network access is disabled: retrieve data with the search/fetch tools
        first, then process it here.

        Args:
            code (str): The Python source to execute. Print what you want to
                see -- only stdout, stderr and the list of files you created
                come back.
            timeout_seconds (int): Wall-clock limit, capped at 120.
                (default: :obj:`30`)

        Returns:
            Dict[str, Any]: `ok` (whether it completed without error),
                `stdout`, `stderr`, `exit_code`, `duration_seconds`, and
                `files_written` (workspace-relative paths this run created or
                modified).
        """
        if not isinstance(code, str) or not code.strip():
            return {
                "ok": False,
                "error": "no code was provided",
                "stdout": "",
                "stderr": "",
            }
        if len(code) > MAX_CODE_CHARS:
            return {
                "ok": False,
                "error": (
                    f"code is {len(code)} characters; the limit is "
                    f"{MAX_CODE_CHARS}. Write the data to a workspace file in "
                    "smaller steps instead of embedding it all in the source."
                ),
                "stdout": "",
                "stderr": "",
            }
        if self._executions >= MAX_EXECUTIONS_PER_RUN:
            return {
                "ok": False,
                "error": (
                    f"This run's code-execution budget "
                    f"({MAX_EXECUTIONS_PER_RUN} runs) is fully used. No "
                    "further execution will happen -- write your final answer "
                    "from the results you already have."
                ),
                "stdout": "",
                "stderr": "",
                "budget_exhausted": True,
            }

        # Caught here rather than in the child so an obvious mistake costs no
        # process spawn and comes back with the exact line and column.
        try:
            compile(code, "<generated>", "exec")
        except SyntaxError as exc:
            return {
                "ok": False,
                "error": f"SyntaxError: {exc.msg}",
                "line": exc.lineno,
                "offset": exc.offset,
                "stdout": "",
                "stderr": f"SyntaxError: {exc.msg} (line {exc.lineno})",
                "exit_code": None,
            }

        limit = max(1, min(int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS))

        # Host-wide slot, taken BEFORE the execution is counted so a run that
        # never got to execute is not charged against its own budget.
        if not _EXECUTION_SLOTS.acquire(timeout=MAX_EXECUTION_QUEUE_WAIT_SECONDS):
            return {
                "ok": False,
                "error": (
                    "The server is running the maximum number of concurrent "
                    f"code executions ({MAX_CONCURRENT_EXECUTIONS}) and a slot "
                    f"did not free up within {MAX_EXECUTION_QUEUE_WAIT_SECONDS}s. "
                    "Try a smaller computation, or continue without it."
                ),
                "stdout": "",
                "stderr": "",
                "busy": True,
            }
        try:
            return self._run_locked(code, limit)
        finally:
            _EXECUTION_SLOTS.release()

    def _run_locked(self, code: str, limit: int) -> dict[str, Any]:
        """The real execution, holding a host-wide concurrency slot."""
        self._executions += 1
        before = self._snapshot()

        # The runner, guard and script live OUTSIDE the workspace so the
        # executed code cannot rewrite the harness that is confining it.
        staging = Path(tempfile.mkdtemp(prefix="agentexec-"))
        started = time.monotonic()
        try:
            guard_path = staging / "guard.py"
            runner_path = staging / "runner.py"
            script_path = staging / "script.py"
            guard_path.write_text(_GUARD, encoding="utf-8")
            runner_path.write_text(_RUNNER, encoding="utf-8")
            script_path.write_text(code, encoding="utf-8")
            out_path = staging / "stdout.txt"
            err_path = staging / "stderr.txt"

            timed_out = False
            with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
                try:
                    process = subprocess.Popen(
                        [
                            self.python,
                            "-B",  # no .pyc files written into the workspace
                            # Unbuffered as a FLAG, not via PYTHONUNBUFFERED:
                            # a timed-out run's partial output is usually the
                            # most useful thing it produced, and a buffer that
                            # never flushes loses it. Deliberately not using
                            # -E/-I here -- those ignore all PYTHON* variables,
                            # including the ones set below, and they protect
                            # against nothing given _child_env builds the
                            # environment from an explicit allowlist rather
                            # than inheriting the parent's.
                            "-u",
                            str(runner_path),
                            str(guard_path),
                            str(script_path),
                        ],
                        cwd=str(self.workspace_root),
                        env=self._child_env(),
                        stdout=out_handle,
                        stderr=err_handle,
                        stdin=subprocess.DEVNULL,
                        # Its own process group: a script that spawns a child
                        # (multiprocessing, a shell command) would otherwise
                        # leave orphans running after the kill below.
                        start_new_session=True,
                    )
                except OSError as exc:
                    return {
                        "ok": False,
                        "error": f"could not start the interpreter: {exc}",
                        "stdout": "",
                        "stderr": "",
                    }
                try:
                    exit_code = process.wait(timeout=limit)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._terminate(process)
                    exit_code = process.returncode

            duration = time.monotonic() - started
            stdout = self._read_bounded(out_path)
            stderr = self._read_bounded(err_path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        after = self._snapshot()
        written = sorted(
            path
            for path, mtime in after.items()
            if path not in before or before[path] != mtime
        )

        if timed_out:
            return {
                "ok": False,
                "error": (
                    f"Execution exceeded the {limit}s timeout and was stopped. "
                    "Any output produced before then is below. Do not re-run "
                    "the same code unchanged -- reduce the work or process the "
                    "data in smaller pieces."
                ),
                "timed_out": True,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "duration_seconds": round(duration, 3),
                "files_written": written[:50],
            }

        ok = exit_code == 0
        result: dict[str, Any] = {
            "ok": ok,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 3),
            "files_written": written[:50],
            "executions_remaining": MAX_EXECUTIONS_PER_RUN - self._executions,
        }
        if not ok:
            # The last traceback line is the exception itself; surfacing it
            # separately means the model does not have to parse stderr to
            # know what went wrong.
            tail = [line for line in stderr.strip().splitlines() if line.strip()]
            result["error"] = tail[-1] if tail else f"exited with code {exit_code}"
        return result

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Kill the child and everything it started (see terminate_process)."""
        terminate_process(process)


    @staticmethod
    def _read_bounded(path: Path) -> str:
        """Read at most MAX_OUTPUT_CHARS from a capture file.

        Reads bytes with a cap rather than the whole file: the point is that
        a program printing in an infinite loop until the timeout produces a
        multi-gigabyte file, and reading it to truncate it afterwards would
        be the out-of-memory this is preventing.
        """
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_OUTPUT_CHARS * 4)
                overflowed = bool(handle.read(1))
        except OSError:
            return ""
        text = raw.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT_CHARS or overflowed:
            text = (
                text[:MAX_OUTPUT_CHARS]
                + f"\n[... output truncated at {MAX_OUTPUT_CHARS} characters ...]"
            )
        return text

    def as_tools(self) -> list:
        return [self.run_python]


__all__ = [
    "CodeExecutionTools",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_EXECUTIONS_PER_RUN",
    "MAX_OUTPUT_CHARS",
    "MAX_TIMEOUT_SECONDS",
]


def terminate_process(process: subprocess.Popen) -> None:
    """Kill the child and everything it started.

    POSIX: SIGTERM to the group first (so a well-behaved child can
    clean up), then SIGKILL if it is still alive. killpg rather than
    kill because start_new_session put the child in its own group
    precisely so this would reach its descendants.

    Windows has neither signal.SIGKILL nor process-group signal
    delivery -- there is no graceful-then-forceful distinction for an
    arbitrary child there (TerminateProcess, which Popen.kill() already
    calls, is the only stop mechanism). `taskkill /T` is the standard
    way to reach the whole descendant tree by PID instead (Windows
    tracks parent/child PIDs regardless of process groups), so it takes
    over the "kill everything it started" job in one forceful call.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
