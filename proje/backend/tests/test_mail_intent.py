"""Email asked for in the request text itself.

Two paths: a scheduled task whose prompt names recipients (auto-sent), and a
chat run whose request does (opened as a draft in Outlook). Both take
recipients only from text a person wrote -- the guard that keeps a poisoned
web page or upload from adding one.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

import artifact_intent
import main
import outlook

NOW = 1_790_000_000.0


# -- the guard ---------------------------------------------------------------


def test_recipients_must_appear_in_the_request() -> None:
    text = "Haftalık raporu hazırla ve ali@sirket.com adresine gönder."
    assert artifact_intent._verified_recipients(["ali@sirket.com"], text) == ["ali@sirket.com"]
    # What a poisoned page or upload would try: an address the user never typed.
    assert artifact_intent._verified_recipients(["saldirgan@kotu.com"], text) == []
    assert artifact_intent._verified_recipients(["ALI@SIRKET.COM"], text) == ["ali@sirket.com"]


def test_addresses_in_text() -> None:
    found = artifact_intent.addresses_in_text("a@x.com, b@y.com; a@X.com ve c@z.com.")
    assert found == ["a@x.com", "b@y.com", "c@z.com"]
    assert artifact_intent.addresses_in_text("") == []


def test_intent_without_recipients_sends_nothing() -> None:
    intent = artifact_intent.ArtifactIntent(
        False, "none", "llm", sends_email=True, email_to=()
    ).normalized()
    assert intent.sends_email is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("haftalik raporu hazirla ve ali@sirket.com adresine gonder", True),
        ("rapor hazirla, ekip@firma.com a yolla", True),
        ("email pazarlama stratejilerini arastir", False),
        ("ali@sirket.com adresini rehbere ekle", False),
    ],
)
def test_regex_fallback(text: str, expected: bool) -> None:
    assert artifact_intent.fallback_from_regex(text).sends_email is expected


def test_persisted_intent_round_trip() -> None:
    usage = {
        "artifact_intent_source": "llm",
        "wants_artifact": True,
        "artifact_type": "pdf",
        "sends_email": True,
        "email_to": ["ali@sirket.com"],
    }
    restored = artifact_intent.from_persisted(usage)
    assert restored.sends_email is True
    assert restored.email_to == ("ali@sirket.com",)


# -- scheduled runs ----------------------------------------------------------


def _job(job_id: str, *, sends_email: bool, to: list[str], result: str = "Rapor metni.") -> dict[str, Any]:
    job = {
        "job_id": job_id,
        "status": "done",
        "result": result,
        "usage": {"sends_email": sends_email, "email_to": to},
    }
    main._JOBS[job_id] = job
    return job


def test_task_setting_wins_over_the_prompt() -> None:
    _job("job-a", sends_email=True, to=["prompt@x.com"])
    task = {"delivery": {"to": ["ayar@x.com"], "cc": [], "on": "success", "attach_files": True}}
    resolved = main._resolve_task_delivery(task, {"job_id": "job-a"})
    assert resolved["to"] == ["ayar@x.com"]
    assert resolved["source"] == "task"


def test_prompt_recipients_used_when_the_box_is_unticked() -> None:
    _job("job-b", sends_email=True, to=["ali@sirket.com"])
    resolved = main._resolve_task_delivery({"delivery": None}, {"job_id": "job-b"})
    assert resolved["to"] == ["ali@sirket.com"]
    assert resolved["source"] == "prompt"
    assert resolved["on"] == "success"


def test_no_setting_and_no_prompt_means_no_delivery() -> None:
    _job("job-c", sends_email=False, to=[])
    assert main._resolve_task_delivery({"delivery": None}, {"job_id": "job-c"}) is None
    assert main._resolve_task_delivery({"delivery": None}, {"job_id": None}) is None


def test_prompt_delivery_says_so_in_the_email(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        outlook, "send_mail", lambda **kwargs: (sent.append(kwargs), outlook.SendResult(True))[1]
    )
    _job("job-d", sends_email=True, to=["ali@sirket.com"], result="Çelik fiyatları arttı.")
    outcome = asyncio.run(
        main._deliver_scheduled_run(
            {"task_id": "t", "name": "Haftalık bülten", "timezone": "Europe/Istanbul", "delivery": None},
            {"run_id": "r", "job_id": "job-d", "status": "done", "scheduled_for": NOW},
        )
    )
    assert outcome.ok
    assert sent[0]["to"] == ["ali@sirket.com"]
    assert "görev metninde" in sent[0]["html_body"]


# -- chat runs ---------------------------------------------------------------


@pytest.fixture()
def chat_job(monkeypatch):
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        outlook, "send_mail", lambda **kwargs: (sent.append(kwargs), outlook.SendResult(True))[1]
    )
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")
    root = Path(tempfile.mkdtemp(prefix="mail_intent_"))
    (root / "artifacts").mkdir()
    (root / "artifacts" / "rapor.pdf").write_bytes(b"%PDF")
    job = {
        "job_id": "chat-1",
        "topic": "Zeytinyağı ihracatını araştır, PDF yap ve ali@sirket.com adresine gönder",
        "result": "## Özet\n\nİhracat arttı.",
        "workspace_root": str(root),
        "usage": {"sends_email": True, "email_to": ["ali@sirket.com"]},
    }
    main._JOBS[job["job_id"]] = job
    return job, sent


def test_chat_opens_a_draft_with_the_files(chat_job) -> None:
    job, sent = chat_job
    notice = asyncio.run(main._email_answer_from_prompt(job))
    assert sent[0]["action"] == "display"
    assert sent[0]["to"] == ["ali@sirket.com"]
    assert [name for name, _ in sent[0]["attachments"]] == ["rapor.pdf"]
    assert "İhracat arttı." in sent[0]["html_body"]
    assert "taslağı" in notice and "ali@sirket.com" in notice


def test_chat_sends_directly_when_configured(chat_job, monkeypatch) -> None:
    job, sent = chat_job
    monkeypatch.setenv("OUTLOOK_AGENT_ACTION", "send")
    notice = asyncio.run(main._email_answer_from_prompt(job))
    assert sent[0]["action"] == "send"
    assert "gönderildi" in notice


def test_chat_without_mail_intent_does_nothing(chat_job) -> None:
    job, sent = chat_job
    job["usage"] = {"sends_email": False, "email_to": []}
    assert asyncio.run(main._email_answer_from_prompt(job)) is None
    assert sent == []


def test_chat_says_so_when_outlook_is_off(chat_job, monkeypatch) -> None:
    job, sent = chat_job
    monkeypatch.delenv("OUTLOOK_ENABLED", raising=False)
    notice = asyncio.run(main._email_answer_from_prompt(job))
    assert "kapalı" in notice
    assert sent == []


def test_chat_reports_a_failed_send(chat_job, monkeypatch) -> None:
    job, _ = chat_job
    monkeypatch.setattr(
        outlook, "send_mail", lambda **kwargs: outlook.SendResult(False, "bridge_unreachable", "köprü kapalı")
    )
    assert "hazırlanamadı" in asyncio.run(main._email_answer_from_prompt(job))
