"""main._deliver_scheduled_run: the email built from a settled run."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

import main
import outlook

NOW = 1_790_000_000.0


@pytest.fixture()
def sent(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_send(**kwargs: Any) -> outlook.SendResult:
        calls.append(kwargs)
        return outlook.SendResult(True)

    monkeypatch.setattr(outlook, "send_mail", fake_send)
    return calls


def _job(result: str, files: dict[str, bytes], status: str = "done") -> str:
    job_id = f"job-mail-{len(main._JOBS)}-{NOW}"
    root = Path(tempfile.mkdtemp(prefix="delivery_msg_"))
    (root / "artifacts").mkdir()
    for name, content in files.items():
        (root / "artifacts" / name).write_bytes(content)
    main._JOBS[job_id] = {"job_id": job_id, "status": status, "result": result, "workspace_root": str(root)}
    return job_id


def _task(**delivery: Any) -> dict[str, Any]:
    return {
        "task_id": "t1",
        "name": "Haftalık bülten",
        "timezone": "Europe/Istanbul",
        "delivery": {"to": ["ali@example.com"], "cc": [], "on": "success", "attach_files": True, **delivery},
    }


def _deliver(task, run):
    return asyncio.run(main._deliver_scheduled_run(task, run))


def test_result_and_files_are_sent(sent) -> None:
    job_id = _job("## Özet\n\nÇelik fiyatları arttı.", {"rapor.pdf": b"%PDF"})
    outcome = _deliver(
        _task(),
        {"run_id": "r1", "job_id": job_id, "status": "done", "scheduled_for": NOW,
         "change_detected": True, "change_summary": "Yeni tebliğ"},
    )
    assert outcome.ok
    [message] = sent
    assert message["to"] == ["ali@example.com"]
    assert message["subject"].startswith("Haftalık bülten — ")
    assert "Çelik fiyatları arttı." in message["html_body"]
    assert "Değişiklik: Yeni tebliğ" in message["html_body"]
    assert message["attachments"] == [("rapor.pdf", b"%PDF")]


def test_attach_files_off(sent) -> None:
    job_id = _job("Rapor", {"rapor.pdf": b"%PDF"})
    _deliver(_task(attach_files=False), {"run_id": "r1", "job_id": job_id, "status": "done", "scheduled_for": NOW})
    assert sent[0]["attachments"] == []


def test_oversized_files_are_noted_in_the_body(sent, monkeypatch) -> None:
    monkeypatch.setattr(outlook, "MAX_ATTACHMENT_BYTES", 5)
    job_id = _job("Rapor", {"a.txt": b"123", "b.txt": b"123456"})
    _deliver(_task(), {"run_id": "r1", "job_id": job_id, "status": "done", "scheduled_for": NOW})
    assert [name for name, _ in sent[0]["attachments"]] == ["a.txt"]
    assert "b.txt eklenmedi" in sent[0]["html_body"]


def test_failed_run_sends_a_notice(sent) -> None:
    outcome = _deliver(
        _task(on="always"),
        {"run_id": "r1", "job_id": None, "status": "error", "scheduled_for": NOW,
         "error": "The job could not be started: model down"},
    )
    assert outcome.ok
    body = sent[0]["html_body"]
    assert "hata verdi" in body
    assert "model down" in body
    assert sent[0]["attachments"] == []


def test_missing_job_is_not_retried(sent) -> None:
    outcome = _deliver(_task(), {"run_id": "r1", "job_id": "gone", "status": "done", "scheduled_for": NOW})
    assert (outcome.ok, outcome.retryable) == (False, False)
    assert sent == []
