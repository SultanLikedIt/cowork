"""The doctor: installed, authenticated, and how old that cookie is.

Cookie age is the one that earns a module. Reddit's session lasts about a
week and cannot self-refresh without a browser, so in the container this
feature degrades on a timer -- and a scheduled task that starts failing at
3am should have been warned about at 6 days, not discovered at 8.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from social import health
from social.runner import RunResult


@pytest.fixture(autouse=True)
def _no_cache():
    health.reset_cache()
    yield
    health.reset_cache()


def _status(**data):
    payload = {
        "authenticated": True,
        "cookie_count": 3,
        "credential_file": "",
        "username": "u/test",
        "error": None,
    }
    payload.update(data)
    return lambda command: RunResult(ok=True, data=payload)


def test_a_missing_binary_is_reported_with_an_install_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda _: None)
    report = health.check("reddit", run=_status())
    assert not report.ok
    assert not report.installed
    assert "rdt-cli" in report.remedy


def test_an_unauthenticated_tool_points_at_the_cookie_not_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two failures have different fixes; conflating them wastes the user."""
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    report = health.check(
        "reddit",
        run=_status(authenticated=False, cookie_count=0, error="No credential loaded"),
    )
    assert report.installed and not report.authenticated
    assert not report.ok
    assert "Cookie-Editor" in report.remedy
    assert "No credential loaded" in report.detail


def test_a_working_session_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    report = health.check("reddit", run=_status())
    assert report.ok
    assert report.username == "u/test"
    assert not report.stale_soon


def test_an_aging_cookie_warns_before_it_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_text("{}", encoding="utf-8")
    old = time.time() - health.COOKIE_WARN_DAYS * 86400 - 60
    import os

    os.utime(credential, (old, old))

    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    report = health.check("reddit", run=_status(credential_file=str(credential)))
    assert report.ok  # still working
    assert report.stale_soon  # but say so now
    assert "yenilenmedi" in report.detail
    assert report.remedy


def test_a_fresh_cookie_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    report = health.check("reddit", run=_status(credential_file=str(credential)))
    assert report.ok and not report.stale_soon and not report.remedy


def test_the_credential_file_is_never_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Staleness comes from mtime. A path that reads the secret can log it."""
    secret = "reddit-session-value-must-not-appear"
    credential = tmp_path / "credential.json"
    credential.write_text(f'{{"cookies": {{"reddit_session": "{secret}"}}}}', encoding="utf-8")

    opened: list[str] = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    report = health.check("reddit", run=_status(credential_file=str(credential)))

    assert str(credential) not in opened
    assert secret not in repr(report)
    assert secret not in str(report.as_dict())


def test_a_failed_probe_is_a_report_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    report = health.check(
        "reddit",
        run=lambda command: RunResult(ok=False, error_code="timeout", detail="yanıt yok"),
    )
    assert report.installed and not report.ok
    assert "yanıt yok" in report.detail


def test_the_probe_is_cached_so_a_job_does_not_pay_for_it_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/rdt")
    calls: list[int] = []

    def counting(command):
        calls.append(1)
        return _status()(command)

    health.check("reddit", run=counting)
    health.check("reddit", run=counting)
    assert len(calls) == 1
    health.reset_cache()
    health.check("reddit", run=counting)
    assert len(calls) == 2


def test_status_is_not_a_verb_the_model_can_request() -> None:
    """The probe exists, but not through the command builder."""
    from social import registry

    assert "status" not in registry.REDDIT.verbs
    with pytest.raises(registry.ArgumentError):
        registry.build("reddit", "status")
    command = health.status_command(registry.REDDIT)
    assert command.argv == ("rdt", "status", "--json")


def test_the_report_shape_is_serializable_for_the_api() -> None:
    report = health.HealthReport(
        platform="reddit", installed=True, authenticated=True, cookie_age_days=6.44
    )
    payload = report.as_dict()
    assert payload["ok"] is True
    assert payload["cookie_age_days"] == 6.4
    assert payload["stale_soon"] is True


def test_a_transient_network_error_is_retried_once() -> None:
    """Observed right after `docker compose up`: the first probe failed with
    ConnectionError while DNS was still settling, and any job starting in
    that window would have been told the platform was unavailable."""
    calls: list[int] = []

    def flaky(command):
        calls.append(1)
        if len(calls) == 1:
            return RunResult(ok=False, error_code="network", detail="ConnectionError")
        return RunResult(ok=True, data={"hits": []})

    report = health.check("hackernews", run=flaky)
    assert report.ok
    assert len(calls) == 2


def test_an_auth_failure_is_not_retried() -> None:
    """A real answer; retrying it just spends another request."""
    calls: list[int] = []

    def failing(command):
        calls.append(1)
        return RunResult(ok=False, error_code="rate_limited", detail="429")

    report = health.check("hackernews", run=failing)
    assert not report.ok
    assert len(calls) == 1
