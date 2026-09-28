"""Jira changes that a person approves: detect, propose, validate, apply.

No network and no model: a fake Jira client and scripted model replies. What
is asserted is what would be written, and that nothing is written without
the approval step.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import db
import jira_actions as ja
import jira_client as jc

db.run_migrations()


# --- fakes ---------------------------------------------------------------------


class FakeClient:
    def __init__(self, *, fail: jc.JiraError | None = None) -> None:
        self.config = jc.JiraConfig(
            enabled=True,
            write_enabled=True,
            base_url="https://acme.atlassian.net",
            deployment="cloud",
            username="me@acme.com",
            api_token="secret",
        )
        self.fail = fail
        self.writes: list[tuple[str, Any]] = []
        self.statuses = {"KAN-7": "To Do", "KAN-12": "In Progress"}

    def projects(self):
        return [
            {"key": "KAN", "name": "Kanban Board", "id": "1"},
            {"key": "SAM1", "name": "Sample", "id": "2"},
        ]

    def project_issue_types(self, key):
        return [{"id": "10001", "name": "Task"}, {"id": "10002", "name": "Hata"}, {"id": "10000", "name": "Epic"}]

    def get_issue(self, key, fields=()):
        if key not in self.statuses:
            raise jc.JiraError("not_found", "Aranan Jira kaydı bulunamadı veya erişim yok.")
        return {"key": key, "fields": {"summary": f"{key} başlığı", "status": {"name": self.statuses[key]}}}

    def transitions(self, key):
        return [
            {"id": "11", "name": "To Do", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
            {"id": "21", "name": "In Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
        ]

    def create_issue(self, **kwargs):
        if self.fail:
            raise self.fail
        self.writes.append(("create_issue", kwargs))
        return {"id": "100", "key": "KAN-99"}

    def add_comment(self, key, body):
        if self.fail:
            raise self.fail
        self.writes.append(("add_comment", (key, body)))
        return {"id": "555"}

    def transition(self, key, transition_id):
        if self.fail:
            raise self.fail
        self.writes.append(("transition", (key, transition_id)))


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(jc, "is_write_enabled", lambda: True)


def _scripted(reply: Any):
    calls: list[dict] = []

    async def complete(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    complete.calls = calls
    return complete


def _store(kind: str, payload: dict[str, Any], job_id: str = "job-1") -> str:
    action_id = f"act-{kind}-{len(db.list_integration_actions(job_id))}-{job_id}"
    db.insert_integration_action(action_id, job_id, "jira", kind, payload, 1.0)
    return action_id


# --- the client's write calls -----------------------------------------------------


class _Response:
    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status_code = status
        self.headers: dict = {}
        self._raw = json.dumps(payload).encode() if payload is not None else b""

    def iter_content(self, size):
        yield self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Session:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_client_writes_use_api_v2_with_exact_bodies() -> None:
    session = _Session(
        _Response(201, {"id": "1", "key": "KAN-1"}),
        _Response(201, {"id": "9"}),
        _Response(204),
    )
    client = jc.JiraClient(FakeClient().config, session=session)
    assert client.create_issue(
        project_key="kan", issue_type_id="10001", summary="Başlık", description="Ayrıntı"
    )["key"] == "KAN-1"
    client.add_comment("kan-1", "not")
    client.transition("KAN-1", "31")

    (m1, u1, k1), (m2, u2, k2), (m3, u3, k3) = session.calls
    assert (m1, u1) == ("POST", "https://acme.atlassian.net/rest/api/2/issue")
    assert json.loads(k1["data"]) == {
        "fields": {
            "project": {"key": "KAN"},
            "issuetype": {"id": "10001"},
            "summary": "Başlık",
            "description": "Ayrıntı",
        }
    }
    assert (m2, u2) == ("POST", "https://acme.atlassian.net/rest/api/2/issue/KAN-1/comment")
    assert json.loads(k2["data"]) == {"body": "not"}
    assert (m3, u3) == ("POST", "https://acme.atlassian.net/rest/api/2/issue/KAN-1/transitions")
    assert json.loads(k3["data"]) == {"transition": {"id": "31"}}
    assert all(k["allow_redirects"] is False for _, _, k in session.calls)


def test_client_rejects_malformed_keys_before_any_request() -> None:
    session = _Session()
    client = jc.JiraClient(FakeClient().config, session=session)
    with pytest.raises(jc.JiraError):
        client.add_comment("KAN-1/../../admin", "x")
    with pytest.raises(jc.JiraError):
        client.transition("KAN-1", "31; DROP")
    with pytest.raises(jc.JiraError):
        client.create_issue(project_key="K A N", issue_type_id="1", summary="s")
    assert session.calls == []


def test_writing_is_off_unless_switched_on() -> None:
    base = {"JIRA_ENABLED": "true", "JIRA_URL": "https://x.atlassian.net",
            "JIRA_USERNAME": "a@b.com", "JIRA_API_TOKEN": "t"}
    assert jc.load_config(base).write_enabled is False
    assert jc.load_config({**base, "JIRA_WRITE_ENABLED": "true"}).write_enabled is True


# --- markdown -> wiki markup ----------------------------------------------------------


def test_to_wiki_converts_what_comments_use() -> None:
    text = "## Özet\n- **birinci** madde\n  - alt madde\n1. adım\n`kod` ve [link](https://a.com/x)\n```\nprint(1)\n```"
    assert ja.to_wiki(text) == (
        "h2. Özet\n* *birinci* madde\n** alt madde\n# adım\n{{kod}} ve [link|https://a.com/x]\n"
        "{code}\nprint(1)\n{code}"
    )


# --- the gate and the detection call -------------------------------------------------


def test_gate_needs_jira_an_issue_key_or_a_project_key(monkeypatch) -> None:
    monkeypatch.setattr(ja, "_known_project_keys", lambda: ("KAN", "SAM1"))
    assert ja.might_write("jira'da bug aç")
    assert ja.might_write("kan-12'ye yorum ekle")
    assert ja.might_write("KAN'da task aç: giriş sayfası")
    assert not ja.might_write("İstanbul hakkında kısa bir PDF hazırla")
    assert not ja.might_write("kanıt topla")  # not the key KAN as a word


def test_detect_write_returns_the_kind_or_none() -> None:
    assert asyncio.run(ja.detect_write("x", complete=_scripted(ja.WriteIntent(action="transition")))) == "transition"
    assert asyncio.run(ja.detect_write("x", complete=_scripted(RuntimeError("boom")))) == "none"
    assert asyncio.run(ja.detect_write("x", complete=_scripted("not json"))) == "none"


# --- validation against Jira ---------------------------------------------------------------


def test_create_resolves_project_and_localized_type() -> None:
    checked = ja.validate(
        "create_issue",
        ja.Proposal(project="kan", issue_type="bug", summary="  Giriş   sayfası çöküyor ", description="- a\n- b"),
        "KAN'da bug aç",
        FakeClient(),
    )
    assert checked.error == ""
    assert checked.payload["project_key"] == "KAN"
    assert checked.payload["issue_type"] == "Hata"  # "bug" found as the Turkish type
    assert checked.payload["issue_type_id"] == "10002"
    assert checked.payload["summary"] == "Giriş sayfası çöküyor"
    assert checked.payload["description"] == "* a\n* b"


def test_create_falls_back_to_task_with_a_note_and_needs_a_project() -> None:
    checked = ja.validate(
        "create_issue", ja.Proposal(project="Kanban Board", issue_type="story", summary="S"), "x", FakeClient()
    )
    assert checked.payload["issue_type"] == "Task"
    assert "story" in checked.payload["note"]

    missing = ja.validate("create_issue", ja.Proposal(summary="S"), "task aç", FakeClient())
    assert "hangi projede" in missing.error and "KAN" in missing.error
    unknown = ja.validate("create_issue", ja.Proposal(project="XYZ", summary="S"), "x", FakeClient())
    assert "XYZ" in unknown.error


def test_comment_only_on_a_key_the_user_typed() -> None:
    ok = ja.validate("add_comment", ja.Proposal(comment="**Düzeldi**"), "KAN-12'ye yorum ekle", FakeClient())
    assert ok.error == "" and ok.payload["issue_key"] == "KAN-12" and ok.payload["body"] == "*Düzeldi*"

    invented = ja.validate(
        "add_comment", ja.Proposal(issue_key="KAN-7", comment="x"), "KAN-12'ye yorum ekle", FakeClient()
    )
    assert "KAN-7" in invented.error and invented.payload == {}

    none_typed = ja.validate("add_comment", ja.Proposal(issue_key="KAN-12", comment="x"), "yorum ekle", FakeClient())
    assert none_typed.error

    empty = ja.validate("add_comment", ja.Proposal(comment="  "), "KAN-12 yorum", FakeClient())
    assert "boş" in empty.error


def test_transition_by_name_by_turkish_word_and_refusals() -> None:
    by_name = ja.validate("transition", ja.Proposal(target_status="done"), "KAN-7'yi Done'a çek", FakeClient())
    assert by_name.payload["transition_id"] == "31" and by_name.payload["current_status"] == "To Do"

    by_word = ja.validate("transition", ja.Proposal(target_status="tamamlandı"), "KAN-7 tamamlandı yap", FakeClient())
    assert by_word.payload["to_status"] == "Done"

    already = ja.validate("transition", ja.Proposal(target_status="In Progress"), "KAN-12 in progress", FakeClient())
    assert "zaten" in already.error

    impossible = ja.validate("transition", ja.Proposal(target_status="Arşiv"), "KAN-7 arşivle", FakeClient())
    assert "Mümkün durumlar" in impossible.error

    missing_issue = ja.validate("transition", ja.Proposal(target_status="Done"), "KAN-404 done", FakeClient())
    assert "bulunamadı" in missing_issue.error


def test_transition_target_is_read_from_the_request_when_the_model_left_it_empty() -> None:
    # Live: "SAM1-8'i In Progress'e çek" came back with target_status "".
    checked = ja.validate("transition", ja.Proposal(), "KAN-7'yi In Progress'e çek", FakeClient())
    assert checked.error == "" and checked.payload["transition_id"] == "21"
    nothing_named = ja.validate("transition", ja.Proposal(), "KAN-7'yi taşı", FakeClient())
    assert "belirtilmedi" in nothing_named.error


def test_a_jira_change_request_never_runs_the_coding_agent(monkeypatch) -> None:
    """Live: "KAN-1'e yorum ekle" went to Coding, which printed "yorum eklendi"."""
    import main

    async def fake_classify(*args, **kwargs):
        import artifact_intent

        return artifact_intent.ArtifactIntent(
            wants_artifact=False, artifact_type="none", source="llm",
            needs_web_research=False, needs_code_execution=True,
            reuses_uploaded_document=False,
        )

    async def fake_detect(*args, **kwargs):
        return "add_comment"

    started: list[str] = []

    async def fake_run_job(job_id, content, mode):
        started.append(job_id)

    monkeypatch.setattr(main.artifact_intent, "classify_artifact_intent", fake_classify)
    monkeypatch.setattr(jc, "is_write_enabled", lambda: True)
    monkeypatch.setattr(ja, "might_write", lambda text: True)
    monkeypatch.setattr(ja, "detect_write", fake_detect)
    monkeypatch.setattr(main, "_run_job", fake_run_job)

    response = asyncio.run(main.submit_job(main.RunPipelineRequest(
        topic="KAN-1'e yorum ekle: test", mode="multi",
    )))
    usage = main._JOBS[response.job_id]["usage"]
    assert usage["jira_write_action"] == "add_comment"
    assert "code_exec" not in usage["capabilities"]


# --- propose after the run -------------------------------------------------------------------


def test_propose_stores_a_validated_change_and_writes_nothing(monkeypatch, writes_on) -> None:
    fake = FakeClient()
    monkeypatch.setattr(jc, "JiraClient", lambda *a, **k: fake)
    job = {
        "job_id": "job-propose",
        "topic": "KAN'da task aç: rapor sayfası yavaş",
        "result": "Tamam.",
        "usage": {"jira_write_action": "create_issue", "resolved_request": "KAN'da task aç: rapor sayfası yavaş"},
    }
    complete = _scripted(ja.Proposal(project="KAN", summary="Rapor sayfası yavaş"))
    notice = asyncio.run(ja.propose_for_job(job, complete=complete))

    assert "Onayınızı bekliyor" in notice and "KAN" in notice
    (action,) = ja.actions_for_job("job-propose")
    assert action["status"] == "proposed"
    assert action["payload"]["summary"] == "Rapor sayfası yavaş"
    assert fake.writes == []
    # The run's answer reaches the model fenced as material, not as instructions.
    assert "MATERIAL" in complete.calls[0]["messages"][1]["content"]


def test_propose_reports_instead_of_storing_an_invalid_change(monkeypatch, writes_on) -> None:
    monkeypatch.setattr(jc, "JiraClient", lambda *a, **k: FakeClient())
    job = {"job_id": "job-bad", "topic": "yorum ekle", "result": "",
           "usage": {"jira_write_action": "add_comment", "resolved_request": "yorum ekle"}}
    notice = asyncio.run(ja.propose_for_job(job, complete=_scripted(ja.Proposal(issue_key="KAN-7", comment="x"))))
    assert notice.startswith("🔧 Jira değişikliği hazırlanamadı")
    assert ja.actions_for_job("job-bad") == []


def test_propose_does_nothing_for_jobs_that_asked_for_no_change() -> None:
    assert asyncio.run(ja.propose_for_job({"job_id": "j", "usage": {}})) is None


def test_propose_never_raises(monkeypatch, writes_on) -> None:
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(jc, "JiraClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr(db, "insert_integration_action", boom)
    job = {"job_id": "j2", "topic": "KAN-12 yorum", "result": "",
           "usage": {"jira_write_action": "add_comment", "resolved_request": "KAN-12 yorum ekle: ok"}}
    notice = asyncio.run(ja.propose_for_job(job, complete=_scripted(ja.Proposal(comment="ok"))))
    assert "hazırlanamadı" in notice


# --- apply, after approval --------------------------------------------------------------------


def test_apply_writes_once_and_a_second_approval_is_refused(writes_on) -> None:
    fake = FakeClient()
    action_id = _store("create_issue", {
        "project_key": "KAN", "issue_type_id": "10001", "summary": "S", "description": "D",
    }, job_id="job-apply")
    done = ja.apply(action_id, client=fake)
    assert done["status"] == "applied"
    assert done["result"] == {"key": "KAN-99", "url": "https://acme.atlassian.net/browse/KAN-99"}
    assert fake.writes == [("create_issue", {
        "project_key": "KAN", "issue_type_id": "10001", "summary": "S", "description": "D",
    })]
    with pytest.raises(ja.ApplyError) as refused:
        ja.apply(action_id, client=fake)
    assert refused.value.status == 409
    assert len(fake.writes) == 1


def test_comment_and_transition_results_link_to_the_issue(writes_on) -> None:
    comment = ja.apply(_store("add_comment", {"issue_key": "KAN-12", "body": "b"}, "job-c"), client=FakeClient())
    assert comment["result"]["url"].endswith("/browse/KAN-12?focusedCommentId=555")
    moved = ja.apply(
        _store("transition", {"issue_key": "KAN-7", "transition_id": "31", "to_status": "Done"}, "job-t"),
        client=FakeClient(),
    )
    assert moved["result"]["status"] == "Done"


def test_a_rejected_write_fails_but_an_unsent_one_can_be_retried(writes_on) -> None:
    rejected = ja.apply(
        _store("add_comment", {"issue_key": "KAN-12", "body": "b"}, "job-f1"),
        client=FakeClient(fail=jc.JiraError("bad_query", "Jira sorguyu reddetti.")),
    )
    assert rejected["status"] == "failed" and "reddetti" in rejected["error"]

    unsent_id = _store("add_comment", {"issue_key": "KAN-12", "body": "b"}, "job-f2")
    unsent = ja.apply(unsent_id, client=FakeClient(fail=jc.JiraError("dns", "ad çözümlenemedi")))
    assert unsent["status"] == "proposed"
    assert ja.apply(unsent_id, client=FakeClient())["status"] == "applied"


def test_nothing_is_applied_when_writing_is_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(jc, "is_write_enabled", lambda: False)
    fake = FakeClient()
    action_id = _store("add_comment", {"issue_key": "KAN-12", "body": "b"}, "job-off")
    with pytest.raises(ja.ApplyError) as off:
        ja.apply(action_id, client=fake)
    assert off.value.status == 409 and fake.writes == []
    assert db.get_integration_action(action_id)["status"] == "proposed"


def test_cancel_only_from_proposed(writes_on) -> None:
    action_id = _store("add_comment", {"issue_key": "KAN-12", "body": "b"}, "job-x")
    assert ja.cancel(action_id)["status"] == "cancelled"
    with pytest.raises(ja.ApplyError):
        ja.apply(action_id, client=FakeClient())
    with pytest.raises(ja.ApplyError) as missing:
        ja.cancel("no-such-action")
    assert missing.value.status == 404


# --- the answering worker is told -------------------------------------------------------------


def test_answer_instruction_forbids_refusing_and_claiming_it_is_done() -> None:
    text = ja.answer_instruction("create_issue")
    assert "never say you cannot" in text and "never claim the change is already done" in text


def test_workforce_passes_the_instruction_to_the_assistant(monkeypatch) -> None:
    from execution.langgraph_runtime import workforce

    seen: dict[str, str] = {}

    async def fake_worker(**kwargs):
        seen["extra"] = kwargs.get("system_extra") or ""
        return "Onayınıza sunuldu."

    monkeypatch.setattr(workforce, "_run_llm_worker", fake_worker)
    from model_config import UsageTracker

    result = workforce.run_workforce_in_thread(
        None, UsageTracker(), None, "KAN'da task aç", "task-1", lambda handle: None,
        answer_instruction=ja.answer_instruction("create_issue"),
    )
    assert "JIRA CHANGE" in seen["extra"]
    assert "Onayınıza sunuldu." in (result.result or "")
