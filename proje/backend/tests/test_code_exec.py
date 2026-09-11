"""Tests for workspace-confined, bounded code execution (code_exec.py).

These run REAL subprocesses -- the whole value of the module is what actually
happens when code executes, and a mocked subprocess would test nothing about
confinement, timeouts or output bounds.

No pytest suite exists in this repo (see test_upload.py) -- run directly:
    python test_code_exec.py
"""

import json
import os
import tempfile
import time
from pathlib import Path

import code_exec
from code_exec import CodeExecutionTools

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def _workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="codeexec_ws_")).resolve()
    sources = root / "sources" / "abc123"
    sources.mkdir(parents=True)
    (sources / "data.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# It actually works
# ---------------------------------------------------------------------------


def test_basic_execution() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python("print(2 + 2)")
    check("exec: a simple program succeeds", result["ok"] is True, str(result))
    check("exec: stdout is captured", result["stdout"].strip() == "4", repr(result["stdout"]))
    check("exec: the exit code is reported", result["exit_code"] == 0, str(result["exit_code"]))
    check("exec: the duration is reported", result["duration_seconds"] >= 0, str(result))


def test_workspace_is_the_working_directory() -> None:
    root = _workspace()
    tools = CodeExecutionTools(root)
    result = tools.run_python(
        "import os, pathlib\n"
        "print(pathlib.Path.cwd())\n"
        "print(sorted(os.listdir('.')))\n"
        "print(open('sources/abc123/data.csv').read().strip())\n"
    )
    check("cwd: the program runs", result["ok"] is True, str(result))
    check(
        "cwd: the working directory IS the job workspace",
        str(root) in result["stdout"],
        result["stdout"][:300],
    )
    check(
        "cwd: uploaded sources are readable from it",
        "a,b" in result["stdout"] and "3,4" in result["stdout"],
        result["stdout"][:300],
    )
    check(
        "cwd: the artifacts directory exists before any code runs",
        "'artifacts'" in result["stdout"],
        result["stdout"][:300],
    )


def test_generated_files_are_reported_and_real() -> None:
    """The transform path: code writes an artifact, and it really exists."""
    root = _workspace()
    tools = CodeExecutionTools(root)
    result = tools.run_python(
        "import csv, json\n"
        "rows = list(csv.DictReader(open('sources/abc123/data.csv')))\n"
        "json.dump(rows, open('artifacts/data.json', 'w'))\n"
        "print('wrote', len(rows))\n"
    )
    written = root / "artifacts" / "data.json"
    check("files: the run succeeded", result["ok"] is True, str(result))
    check("files: the file really exists", written.is_file(), str(written))
    check(
        "files: the created file is listed in files_written",
        "artifacts/data.json" in result["files_written"],
        str(result["files_written"]),
    )
    check(
        "files: the generated JSON parses",
        json.loads(written.read_text()) == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}],
        written.read_text()[:120],
    )


def test_pandas_and_openpyxl_are_available() -> None:
    """The libraries the tool's own docstring promises must actually be there."""
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python(
        "import pandas, openpyxl\nprint('ok', bool(pandas.__version__), bool(openpyxl.__version__))"
    )
    check(
        "libs: pandas and openpyxl import inside the sandbox",
        result["ok"] is True and "ok True True" in result["stdout"],
        f"{result.get('error')} {result['stderr'][:200]}",
    )


def test_xlsx_generation_end_to_end() -> None:
    root = _workspace()
    tools = CodeExecutionTools(root)
    result = tools.run_python(
        "import openpyxl\n"
        "wb = openpyxl.Workbook()\n"
        "wb.active.append(['Tarih', 'İmsak'])\n"
        "wb.active.append(['1 Ağustos', '04:12'])\n"
        "wb.save('artifacts/out.xlsx')\n"
        "print('saved')\n"
    )
    written = root / "artifacts" / "out.xlsx"
    check("xlsx: the run succeeded", result["ok"] is True, f"{result.get('error')} {result['stderr'][:200]}")
    check("xlsx: the workbook file exists", written.is_file(), str(written))

    from openpyxl import load_workbook

    sheet = load_workbook(written).active
    check(
        "xlsx: the generated workbook reopens with the right contents",
        [cell.value for cell in next(sheet.iter_rows())] == ["Tarih", "İmsak"],
        str([cell.value for cell in next(sheet.iter_rows())]),
    )


# ---------------------------------------------------------------------------
# Bounds and failure modes
# ---------------------------------------------------------------------------


def test_timeout_is_enforced_and_explicit() -> None:
    tools = CodeExecutionTools(_workspace())
    started = time.monotonic()
    # No flush=True on purpose: this also asserts the child runs unbuffered,
    # which is what makes a timed-out run's partial output survive the kill.
    result = tools.run_python("print('before')\nwhile True: pass", timeout_seconds=2)
    elapsed = time.monotonic() - started

    check("timeout: the call returns rather than hanging", isinstance(result, dict))
    check("timeout: it is reported as a failure", result["ok"] is False, str(result)[:200])
    check("timeout: the timeout is named explicitly", result.get("timed_out") is True, str(result)[:200])
    check(
        "timeout: it stopped near the requested limit, not the default",
        elapsed < 20,
        f"{elapsed:.1f}s",
    )
    check(
        "timeout: output produced before the kill is preserved",
        "before" in result["stdout"],
        repr(result["stdout"][:120]),
    )
    check(
        "timeout: the message tells the model not to re-run unchanged",
        "Do not re-run the same code unchanged" in result["error"],
        result["error"],
    )


def test_timeout_is_clamped_to_the_hard_ceiling() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python("print('x')", timeout_seconds=99999)
    check("timeout: an absurd request still runs (clamped, not rejected)", result["ok"] is True, str(result))


def test_syntax_error_costs_no_process() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python("def broken(:\n    pass")
    check("syntax: reported as a failure", result["ok"] is False, str(result))
    check("syntax: identified as a SyntaxError", "SyntaxError" in result["error"], result["error"])
    check("syntax: the line number is reported", result.get("line") is not None, str(result))
    check(
        "syntax: no subprocess was spawned (no exit code)",
        result.get("exit_code") is None,
        str(result),
    )


def test_runtime_error_is_structured() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python("x = {}\nprint(x['missing'])")
    check("runtime: reported as a failure", result["ok"] is False, str(result)[:200])
    check("runtime: a non-zero exit code is reported", result["exit_code"] not in (0, None), str(result["exit_code"]))
    check("runtime: the exception is surfaced in `error`", "KeyError" in result["error"], result["error"])
    check("runtime: the full traceback is in stderr", "Traceback" in result["stderr"], result["stderr"][:200])


def test_output_is_bounded() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python("print('x' * 50_000_000)")
    check(
        "output: stdout is truncated to the bound",
        len(result["stdout"]) <= code_exec.MAX_OUTPUT_CHARS + 200,
        str(len(result["stdout"])),
    )
    check(
        "output: the truncation is stated, not silent",
        "truncated" in result["stdout"],
        result["stdout"][-120:],
    )


def test_code_size_is_bounded() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python("x = 1\n" + ("# padding\n" * (code_exec.MAX_CODE_CHARS // 5)))
    check("code size: an oversized program is refused", result["ok"] is False, str(result)[:160])
    check("code size: the limit is named", "limit" in result["error"], result["error"])


def test_execution_budget_per_run() -> None:
    tools = CodeExecutionTools(_workspace())
    for _ in range(code_exec.MAX_EXECUTIONS_PER_RUN):
        tools.run_python("pass")
    result = tools.run_python("print('one more')")
    check("budget: further executions are refused", result["ok"] is False, str(result)[:160])
    check("budget: it is flagged as budget exhaustion", result.get("budget_exhausted") is True, str(result))
    check(
        "budget: the message tells the model to answer from what it has",
        "write your final answer" in result["error"].lower(),
        result["error"],
    )


def test_empty_code() -> None:
    tools = CodeExecutionTools(_workspace())
    for value in ("", "   ", None):
        result = tools.run_python(value)
        check(f"empty code {value!r} fails cleanly", result["ok"] is False, str(result)[:120])


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_secrets_are_not_in_the_child_environment() -> None:
    """The backend's API key must not be readable by generated code.

    CAMEL's CodeExecutionToolkit passes the parent environment through, so
    this is a genuine difference in behaviour, not a theoretical one.
    """
    os.environ["MODEL_API_KEY"] = "sk-test-secret-value"
    os.environ["SOME_OTHER_SECRET"] = "another-secret"
    try:
        tools = CodeExecutionTools(_workspace())
        result = tools.run_python(
            "import os\nprint(os.environ.get('MODEL_API_KEY'))\n"
            "print(os.environ.get('SOME_OTHER_SECRET'))\n"
            "print('COUNT', len(os.environ))\n"
        )
    finally:
        os.environ.pop("MODEL_API_KEY", None)
        os.environ.pop("SOME_OTHER_SECRET", None)

    check("env: the run succeeded", result["ok"] is True, str(result)[:200])
    check(
        "env: MODEL_API_KEY is not visible to executed code",
        "sk-test-secret-value" not in result["stdout"],
        result["stdout"][:200],
    )
    check(
        "env: other host secrets are not visible either",
        "another-secret" not in result["stdout"],
        result["stdout"][:200],
    )
    check(
        "env: the child environment is a small allowlist, not the parent's",
        "None" in result["stdout"],
        result["stdout"][:200],
    )


def test_network_is_disabled_by_default() -> None:
    tools = CodeExecutionTools(_workspace())
    result = tools.run_python(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('93.184.216.34', 80), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except Exception as exc:\n"
        "    print('BLOCKED', type(exc).__name__)\n"
    )
    check(
        "network: an outbound connection does not succeed",
        "CONNECTED" not in result["stdout"],
        result["stdout"][:200],
    )
    check(
        "network: the failure explains that network access is disabled",
        "BLOCKED" in result["stdout"],
        result["stdout"][:200],
    )


def test_harness_files_are_outside_the_workspace() -> None:
    """Executed code must not be able to see or rewrite its own harness."""
    root = _workspace()
    tools = CodeExecutionTools(root)
    result = tools.run_python(
        "import os\n"
        "names = []\n"
        "for dirpath, dirnames, filenames in os.walk('.'):\n"
        "    names.extend(filenames)\n"
        "print(sorted(names))\n"
    )
    check(
        "harness: runner.py/guard.py/script.py are not inside the workspace",
        not any(name in result["stdout"] for name in ("runner.py", "guard.py", "script.py")),
        result["stdout"][:300],
    )


def test_a_killed_process_does_not_leave_children() -> None:
    """A timed-out script that spawned a child must not leave it running."""
    root = _workspace()
    tools = CodeExecutionTools(root)
    marker = root / "child_alive.txt"
    result = tools.run_python(
        "import subprocess, sys, time\n"
        f"code = \"import time\\nwhile True:\\n    open({str(marker)!r}, 'a').write('x')\\n    time.sleep(0.05)\"\n"
        "subprocess.Popen([sys.executable, '-c', code])\n"
        "time.sleep(30)\n",
        timeout_seconds=2,
    )
    check("children: the parent timed out", result.get("timed_out") is True, str(result)[:160])
    time.sleep(0.5)
    size_after_kill = marker.stat().st_size if marker.exists() else 0
    time.sleep(1.0)
    size_later = marker.stat().st_size if marker.exists() else 0
    check(
        "children: the spawned child was killed with its parent",
        size_later == size_after_kill,
        f"{size_after_kill} -> {size_later}",
    )


def main() -> None:
    test_basic_execution()
    test_workspace_is_the_working_directory()
    test_generated_files_are_reported_and_real()
    test_pandas_and_openpyxl_are_available()
    test_xlsx_generation_end_to_end()
    test_timeout_is_enforced_and_explicit()
    test_timeout_is_clamped_to_the_hard_ceiling()
    test_syntax_error_costs_no_process()
    test_runtime_error_is_structured()
    test_output_is_bounded()
    test_code_size_is_bounded()
    test_execution_budget_per_run()
    test_empty_code()
    test_secrets_are_not_in_the_child_environment()
    test_network_is_disabled_by_default()
    test_harness_files_are_outside_the_workspace()
    test_a_killed_process_does_not_leave_children()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All code-execution checks passed")


if __name__ == "__main__":
    main()
