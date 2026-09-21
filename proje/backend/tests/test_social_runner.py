"""The process boundary: what the child can see, and how failures come back.

These run a real subprocess rather than mocking ``subprocess.Popen``. The
claim under test is "this child cannot read MODEL_API_KEY", and a mock that
returns whatever it was told proves nothing about that -- the whole bug class
lives in the arguments actually passed to Popen.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from social import registry
from social.runner import RunResult, run


def _python_command(script: str, *, timeout: int = 30, credential_env=()) -> registry.Command:
    """A Command that runs this interpreter as a stand-in for an upstream CLI."""
    return registry.Command(
        platform="reddit",
        verb="search",
        argv=(sys.executable, "-c", script),
        timeout_seconds=timeout,
        credential_env=tuple(credential_env),
    )


# -- environment scrubbing ----------------------------------------------------


def test_the_child_cannot_see_the_model_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason this module exists.

    `subprocess.run(argv)` with no env= hands the child everything this
    process holds -- the model key, the bridge token, the mailbox path.
    """
    monkeypatch.setenv("MODEL_API_KEY", "sk-should-never-be-visible")
    monkeypatch.setenv("LOCAL_COMPUTER_TOKEN", "bridge-token")
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")

    script = (
        "import json, os;"
        "print(json.dumps({'ok': True, 'data': sorted(os.environ)}))"
    )
    result = run(_python_command(script))

    assert result.ok, result.detail
    assert "MODEL_API_KEY" not in result.data
    assert "LOCAL_COMPUTER_TOKEN" not in result.data
    assert "OUTLOOK_ENABLED" not in result.data
    # PATH still has to be there or nothing would start at all.
    assert "PATH" in result.data


def test_only_this_commands_own_credentials_are_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reddit's call must not carry X's token, and vice versa."""
    monkeypatch.setenv("REDDIT_THING", "reddit-value")
    monkeypatch.setenv("TWITTER_AUTH_TOKEN", "twitter-value")

    script = (
        "import json, os;"
        "print(json.dumps({'ok': True, 'data': {"
        "'reddit': os.environ.get('REDDIT_THING'),"
        "'twitter': os.environ.get('TWITTER_AUTH_TOKEN')}}))"
    )
    result = run(_python_command(script, credential_env=("REDDIT_THING",)))

    assert result.data == {"reddit": "reddit-value", "twitter": None}


def test_home_is_redirected_so_the_cookie_survives_a_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The container's /root is wiped by every --build; /data is not."""
    monkeypatch.setenv("SOCIAL_HOME", str(tmp_path / "social-home"))
    script = "import json, os; print(json.dumps({'ok': True, 'data': os.environ['HOME']}))"
    result = run(_python_command(script))
    assert result.data == str(tmp_path / "social-home")
    assert (tmp_path / "social-home").is_dir()


# -- bounds -------------------------------------------------------------------


def test_a_hanging_tool_is_killed_at_the_timeout() -> None:
    script = "import time; time.sleep(60)"
    result = run(_python_command(script, timeout=2))
    assert not result.ok
    assert result.error_code == "timeout"
    assert result.duration_ms < 30_000


def test_a_missing_executable_is_a_setup_problem_not_a_crash() -> None:
    command = registry.Command(
        platform="reddit",
        verb="search",
        argv=("definitely-not-installed-xyz", "search", "x"),
    )
    result = run(command)
    assert result.error_code == "not_installed"
    assert result.needs_setup


# -- envelope handling --------------------------------------------------------


def test_a_successful_envelope_is_unwrapped() -> None:
    payload = {"ok": True, "schema_version": "1.0", "data": {"posts": [1, 2]}}
    script = f"print({json.dumps(json.dumps(payload))})"
    result = run(_python_command(script))
    assert result.ok and result.data == {"posts": [1, 2]}


def test_a_failed_envelope_is_classified() -> None:
    payload = {"ok": False, "error": {"message": "login required: session expired"}}
    script = f"print({json.dumps(json.dumps(payload))})"
    result = run(_python_command(script))
    assert not result.ok
    assert result.error_code == "auth_required"
    assert result.needs_setup


def test_a_payload_without_the_envelope_is_still_returned() -> None:
    """Upstream version drift is likelier than an attack; don't hard-fail."""
    script = "print('[{\"id\": 1}]')"
    result = run(_python_command(script))
    assert result.ok and result.data == [{"id": 1}]


def test_non_json_output_is_reported_not_parsed() -> None:
    script = "print('Traceback (most recent call last): boom')"
    result = run(_python_command(script))
    assert not result.ok and result.error_code == "bad_output"


def test_empty_output_is_reported() -> None:
    result = run(_python_command("pass"))
    assert not result.ok and result.error_code == "bad_output"


# -- failure classification and redaction -------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 401 Unauthorized", "auth_required"),
        ("rate limit exceeded, slow down", "rate_limited"),
        ("Connection timed out after 3 retries", "network"),
        ("something else went wrong", "failed"),
    ],
)
def test_stderr_decides_why_a_call_failed(stderr: str, expected: str) -> None:
    script = (
        "import sys;"
        f"sys.stderr.write({json.dumps(stderr)});"
        "sys.exit(1)"
    )
    result = run(_python_command(script))
    assert result.error_code == expected
    assert stderr.splitlines()[-1][:40] in result.detail


def test_a_credential_printed_by_the_tool_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A traceback that echoes the cookie would otherwise reach the log.

    And from the log, the prompt: failure detail is shown to the model so it
    can explain what went wrong.
    """
    secret = "reddit-session-cookie-value-1234"
    monkeypatch.setenv("REDDIT_SESSION", secret)
    script = (
        "import os, sys;"
        "sys.stderr.write('auth failed with cookie ' + os.environ['REDDIT_SESSION']);"
        "sys.exit(1)"
    )
    result = run(_python_command(script, credential_env=("REDDIT_SESSION",)))
    assert secret not in result.detail
    assert "gizlendi" in result.detail


# -- the slot is always given back --------------------------------------------


def test_the_concurrency_slot_is_released_after_every_outcome() -> None:
    """A leaked semaphore would wedge the whole backend after a few failures."""
    for script, timeout in (
        ("print('{\"ok\": true, \"data\": 1}')", 30),
        ("import sys; sys.exit(1)", 30),
        ("import time; time.sleep(30)", 1),
    ):
        run(_python_command(script, timeout=timeout))
    result = run(_python_command("print('{\"ok\": true, \"data\": \"done\"}')"))
    assert result.ok and result.data == "done"


def test_oversized_output_is_refused_rather_than_half_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncated JSON cannot be parsed, and repairing it would invent data."""
    from social import runner as runner_module

    monkeypatch.setattr(runner_module, "MAX_OUTPUT_BYTES", 128)
    script = "print('{\"ok\": true, \"data\": \"' + 'x' * 5000 + '\"}')"
    result = run(_python_command(script))
    assert not result.ok
    assert result.error_code == "too_large"


def test_the_real_unauthenticated_reddit_payload_is_classified() -> None:
    """Captured from rdt-cli 0.4.2 against live Reddit, 2026-09-21.

    Two things this pins, both of which a hand-written fake would have got
    wrong: rdt exits **0** on failure (so returncode is not the signal), and
    it reports a machine-readable `error.code` that is more stable than the
    message beside it.
    """
    payload = {
        "ok": False,
        "schema_version": "1",
        "error": {"code": "forbidden", "message": "Search failed: Access forbidden: Resource"},
    }
    script = f"print({json.dumps(json.dumps(payload))})"
    result = run(_python_command(script))
    assert not result.ok
    assert result.error_code == "auth_required"
    assert result.needs_setup


def test_an_upstream_code_beats_a_misleading_message() -> None:
    """"not_found" must not be read as auth just because the prose says so."""
    payload = {
        "ok": False,
        "error": {"code": "not_found", "message": "403 forbidden-looking prose"},
    }
    script = f"print({json.dumps(json.dumps(payload))})"
    result = run(_python_command(script))
    assert result.error_code == "not_found"
    assert not result.needs_setup


def test_the_child_runs_in_utf8_mode() -> None:
    """Measured, not assumed: PYTHONIOENCODING alone is not enough.

    On a Windows host a single 📣 in a post title made rdt-cli exit 1 with
    zero bytes of output, losing an entire search. UTF-8 mode covers the
    handles PYTHONIOENCODING misses.
    """
    script = (
        "import json, os, sys;"
        "print(json.dumps({'ok': True, 'data': {"
        "'utf8_mode': os.environ.get('PYTHONUTF8'),"
        "'io': os.environ.get('PYTHONIOENCODING'),"
        "'stdout': (sys.stdout.encoding or '').lower()}}))"
    )
    result = run(_python_command(script))
    assert result.data["utf8_mode"] == "1"
    assert result.data["io"] == "utf-8"
    assert "utf-8" in result.data["stdout"]


def test_an_emoji_in_the_payload_survives_the_round_trip() -> None:
    """The exact character that broke a live search."""
    script = "import json; print(json.dumps({'ok': True, 'data': 'title \U0001f4e3 here'}))"
    result = run(_python_command(script))
    assert result.ok
    assert "\U0001f4e3" in result.data


def test_the_slot_survives_an_unexpected_error_before_the_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked slot is permanent: after MAX_CONCURRENT_CALLS of them, no job
    can read social content again until the backend restarts."""
    from social import runner as runner_module

    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(runner_module, "social_home", boom)
    for _ in range(runner_module.MAX_CONCURRENT_CALLS + 1):
        with pytest.raises(RuntimeError):
            run(_python_command("pass"))

    monkeypatch.undo()
    result = run(_python_command("print('{\"ok\": true, \"data\": \"alive\"}')"))
    assert result.ok and result.data == "alive"
