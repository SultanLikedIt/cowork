"""Confluence changes that a person approves: new pages and page comments.

No network and no model: a fake Confluence and scripted model replies.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from typing import Any

import pytest

import confluence_actions as ca
import confluence_client as cc
import db
import integration_actions

db.run_migrations()

BASE = "https://acme.atlassian.net/wiki"


def _config(**overrides: Any) -> cc.ConfluenceConfig:
    values = dict(
        enabled=True,
        write_enabled=True,
        base_url=BASE,
        deployment="cloud",
        spaces=("SALES", "ENG"),
        username="me@acme.com",
        api_token="secret",
    )
    values.update(overrides)
    return cc.ConfluenceConfig(**values)


def _page(page_id: str, title: str, space: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "title": title,
        "space": {"key": space},
        "_links": {"webui": f"/spaces/{space}/pages/{page_id}"},
    }


class FakeClient:
    def __init__(self, *, fail: cc.ConfluenceError | None = None) -> None:
        self.config = _config()
        self.fail = fail
        self.writes: list[tuple[str, Any]] = []
        self.pages = [
            _page("101", "Satış Süreci", "SALES"),
            _page("102", "Onboarding", "SALES"),
            _page("201", "Onboarding", "ENG"),
        ]

    def space(self, key):
        if key not in self.config.spaces:
            raise cc.ConfluenceError("forbidden", f"'{key}' izin verilen space'ler arasında değil.")
        return {"key": key, "name": {"SALES": "Satış Ekibi", "ENG": "Engineering"}[key], "id": {"SALES": "262148", "ENG": "9"}[key]}

    def get_page(self, page_id):
        return next((p for p in self.pages if p["id"] == page_id), None)

    def find_by_title(self, space, title):
        return next((p for p in self.pages if p["space"]["key"] == space and p["title"] == title), None)

    def search(self, cql, limit=25, with_body=False):
        title = re.search(r'title = "([^"]+)"', cql).group(1)
        space = re.match(r'space = "([^"]+)"', cql)
        found = [p for p in self.pages if p["title"] == title and (not space or p["space"]["key"] == space.group(1))]
        return cc.SearchPage(results=found[:limit])

    def create_page(self, **kwargs):
        if self.fail:
            raise self.fail
        self.writes.append(("create_page", kwargs))
        return {"id": "999", "url": f"{BASE}/spaces/{kwargs['space_key']}/pages/999"}

    def add_comment(self, page_id, storage, *, page_url=""):
        if self.fail:
            raise self.fail
        self.writes.append(("add_comment", (page_id, storage)))
        return {"id": "555", "url": page_url + "?focusedCommentId=555"}


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(cc, "is_write_enabled", lambda: True)


def _scripted(reply: Any):
    async def complete(messages, **kwargs):
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return complete


# --- the client's write requests -----------------------------------------------


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


def test_cloud_writes_use_api_v2() -> None:
    session = _Session(
        _Response(200, {"id": "7", "_links": {"webui": "/spaces/SALES/pages/7/Plan"}}),
        _Response(200, {"id": "8", "_links": {"webui": "/spaces/SALES/pages/7/Plan?focusedCommentId=8"}}),
    )
    client = cc.ConfluenceClient(_config(), session=session)
    page = client.create_page(space_key="SALES", space_id="262148", title="Plan", storage="<p>x</p>", parent_id="101")
    comment = client.add_comment("7", "<p>ok</p>")

    (m1, u1, k1), (m2, u2, k2) = session.calls
    assert (m1, u1) == ("POST", f"{BASE}/api/v2/pages")
    assert json.loads(k1["data"]) == {
        "spaceId": "262148", "status": "current", "title": "Plan", "parentId": "101",
        "body": {"representation": "storage", "value": "<p>x</p>"},
    }
    assert (m2, u2) == ("POST", f"{BASE}/api/v2/footer-comments")
    assert json.loads(k2["data"]) == {"pageId": "7", "body": {"representation": "storage", "value": "<p>ok</p>"}}
    assert page == {"id": "7", "url": f"{BASE}/spaces/SALES/pages/7/Plan"}
    assert comment["url"].endswith("focusedCommentId=8")
    assert all(k["allow_redirects"] is False for _, _, k in session.calls)


def test_server_writes_use_the_v1_content_endpoint() -> None:
    session = _Session(_Response(200, {"id": "7"}), _Response(200, {"id": "8"}))
    cfg = _config(base_url="https://wiki.sirket.intra", deployment="server", personal_token="pat", username="", api_token="")
    client = cc.ConfluenceClient(cfg, session=session)
    client.create_page(space_key="SALES", space_id="", title="Plan", storage="<p>x</p>", parent_id="101")
    client.add_comment("7", "<p>ok</p>", page_url="https://wiki.sirket.intra/x")
    (_, u1, k1), (_, u2, k2) = session.calls
    assert u1 == u2 == "https://wiki.sirket.intra/rest/api/content"
    assert json.loads(k1["data"])["ancestors"] == [{"id": "101"}]
    assert json.loads(k2["data"])["container"] == {"id": "7", "type": "page"}


def test_client_refuses_spaces_off_the_allowlist_before_any_request() -> None:
    session = _Session()
    client = cc.ConfluenceClient(_config(), session=session)
    with pytest.raises(cc.ConfluenceError):
        client.create_page(space_key="HR", space_id="1", title="x", storage="<p/>")
    with pytest.raises(cc.ConfluenceError):
        client.space("HR")
    with pytest.raises(cc.ConfluenceError):
        client.add_comment("12; DROP", "x")
    assert session.calls == []


def test_writing_is_off_unless_switched_on() -> None:
    base = {"CONFLUENCE_ENABLED": "true", "CONFLUENCE_SPACES": "SALES",
            "JIRA_URL": "https://x.atlassian.net", "JIRA_USERNAME": "a@b.com", "JIRA_API_TOKEN": "t"}
    assert cc.load_config(base).write_enabled is False
    assert cc.load_config({**base, "CONFLUENCE_WRITE_ENABLED": "true"}).write_enabled is True


# --- Markdown -> storage format ------------------------------------------------------


def test_storage_is_well_formed_and_keeps_structure() -> None:
    storage = ca.to_storage("## Hedefler\n\n- **birinci**\n- ikinci\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nSatır<br>")
    ET.fromstring(f"<root>{storage}</root>")
    assert "<h2>Hedefler</h2>" in storage
    assert "<strong>birinci</strong>" in storage
    assert "<table>" in storage and "<td>1</td>" in storage


def test_raw_html_and_macros_cannot_reach_the_page() -> None:
    storage = ca.to_storage(
        '<ac:structured-macro ac:name="html"><ac:plain-text-body>x</ac:plain-text-body></ac:structured-macro>\n\n'
        "<script>alert(1)</script>"
    )
    assert "<ac:" not in storage and "<script" not in storage
    assert "&lt;script&gt;" in storage


# --- the gate ---------------------------------------------------------------------------


def test_gate_needs_confluence_the_wiki_a_page_link_or_a_space_key(monkeypatch) -> None:
    monkeypatch.setattr(cc, "load_config", lambda environ=None: _config())
    monkeypatch.setattr(ca, "_known_page_titles", lambda cfg: ("template - business review",))
    assert ca.might_write("confluence'ta sayfa oluştur")
    assert ca.might_write("wiki'de yeni sayfa aç")
    assert ca.might_write("SALES space'ine bir sayfa ekle")
    assert ca.might_write(f"{BASE}/spaces/SALES/pages/101 sayfasına yorum ekle")
    assert not ca.might_write("İstanbul hakkında kısa bir PDF hazırla")
    # Live misses: a space key in ordinary case, and a page named only by title.
    assert ca.might_write("'Sales' sayfasının altına bir sayfa aç")
    assert ca.might_write("'Template - Business review' sayfasına yorum yaz: ok")


def test_page_titles_for_the_gate_are_cached_and_failures_remembered(monkeypatch) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, cfg):
            pass

        def search(self, cql, limit=25, with_body=False):
            calls.append(cql)
            if len(calls) == 1:
                raise cc.ConfluenceError("dns", "yok")
            return cc.SearchPage(results=[_page("1", "Plan", "SALES"), _page("2", "Satış Süreci", "SALES")])

    monkeypatch.setattr(cc, "ConfluenceClient", Client)
    ca._TITLE_CACHE.clear()
    cfg = _config()
    assert ca._known_page_titles(cfg) == ()
    assert ca._known_page_titles(cfg) == ()  # failure remembered, no second call
    assert len(calls) == 1
    ca._TITLE_CACHE.clear()
    assert ca._known_page_titles(cfg) == ("satış süreci",)  # "Plan" is too short to gate on
    assert ca._known_page_titles(cfg) == ("satış süreci",)
    assert len(calls) == 2
    ca._TITLE_CACHE.clear()


def test_detect_write_returns_the_kind_or_none() -> None:
    assert asyncio.run(ca.detect_write("x", complete=_scripted(ca.WriteIntent(action="create_page")))) == "create_page"
    assert asyncio.run(ca.detect_write("x", complete=_scripted(RuntimeError("boom")))) == "none"


# --- validation against Confluence ---------------------------------------------------------


def test_new_page_in_a_named_space_with_its_body_converted() -> None:
    checked = ca.validate(
        "create_page", ca.Proposal(space="sales", title="  Q4 Planı ", body="## Hedef\n- büyüme"),
        "SALES'e Q4 planı sayfası oluştur", FakeClient(),
    )
    assert checked.error == ""
    p = checked.payload
    assert (p["space_key"], p["space_id"], p["title"], p["parent_id"]) == ("SALES", "262148", "Q4 Planı", "")
    assert p["body_markdown"] == "## Hedef\n- büyüme"
    assert "<h2>Hedef</h2>" in p["storage"]


def test_space_by_name_and_refusals() -> None:
    by_name = ca.validate("create_page", ca.Proposal(space="Engineering", title="T", body="b"), "x", FakeClient())
    assert by_name.payload["space_key"] == "ENG"

    not_allowed = ca.validate("create_page", ca.Proposal(space="HR", title="T", body="b"), "HR'a sayfa", FakeClient())
    assert "izin verilen" in not_allowed.error

    unnamed = ca.validate("create_page", ca.Proposal(title="T", body="b"), "wiki'ye sayfa aç", FakeClient())
    assert "belirtilmedi" in unnamed.error and "SALES" in unnamed.error

    duplicate = ca.validate("create_page", ca.Proposal(space="SALES", title="Onboarding", body="b"), "x", FakeClient())
    assert "zaten var" in duplicate.error

    empty = ca.validate("create_page", ca.Proposal(space="SALES", title="T", body="  "), "x", FakeClient())
    assert "boş" in empty.error


def test_parent_page_must_be_one_the_user_named_or_linked() -> None:
    named = ca.validate(
        "create_page", ca.Proposal(title="Adım 1", parent_title="Satış Süreci", body="b"),
        "'Satış Süreci' sayfasının altına 'Adım 1' sayfası aç", FakeClient(),
    )
    assert named.payload["parent_id"] == "101" and named.payload["space_key"] == "SALES"

    invented = ca.validate(
        "create_page", ca.Proposal(space="SALES", title="Adım 1", parent_title="Satış Süreci", body="b"),
        "SALES'e Adım 1 sayfası aç", FakeClient(),
    )
    assert "istekte yazılan" in invented.error

    linked = ca.validate(
        "create_page", ca.Proposal(title="Alt sayfa", body="b"),
        f"{BASE}/spaces/SALES/pages/102/Onboarding altına sayfa aç", FakeClient(),
    )
    assert linked.payload["parent_id"] == "102"


def test_comment_on_a_linked_or_named_page_only() -> None:
    linked = ca.validate(
        "add_comment", ca.Proposal(body="Onaylandı"), f"{BASE}/spaces/SALES/pages/101 sayfasına yorum ekle", FakeClient()
    )
    assert linked.payload["page_id"] == "101" and "<p>Onaylandı</p>" in linked.payload["storage"]

    named = ca.validate("add_comment", ca.Proposal(page_title="Satış Süreci", body="ok"), "Satış Süreci sayfasına yorum", FakeClient())
    assert named.payload["page_id"] == "101"

    invented = ca.validate("add_comment", ca.Proposal(page_title="Satış Süreci", body="ok"), "yorum ekle", FakeClient())
    assert "istekte yazılan" in invented.error

    ambiguous = ca.validate("add_comment", ca.Proposal(page_title="Onboarding", body="ok"), "Onboarding sayfasına yorum", FakeClient())
    assert "birden fazla" in ambiguous.error

    outside = ca.validate("add_comment", ca.Proposal(body="ok"), f"{BASE}/spaces/HR/pages/999 yorum ekle", FakeClient())
    assert "bulunamadı" in outside.error


# --- propose, apply, cancel -------------------------------------------------------------------------


def test_propose_then_apply_once(monkeypatch, writes_on) -> None:
    fake = FakeClient()
    monkeypatch.setattr(cc, "ConfluenceClient", lambda *a, **k: fake)
    job = {
        "job_id": "cjob-1", "topic": "SALES'e sayfa", "result": "## Plan\n- a",
        "usage": {"confluence_write_action": "create_page", "resolved_request": "SALES'e 'Plan' sayfası oluştur"},
    }
    notice = asyncio.run(ca.propose_for_job(job, complete=_scripted(ca.Proposal(space="SALES", title="Plan", body="## Plan\n- a"))))
    assert "Onayınızı bekliyor" in notice and fake.writes == []

    (action,) = [a for a in integration_actions.actions_for_job("cjob-1") if a["system"] == "confluence"]
    done = ca.apply(action["action_id"], client=fake)
    assert done["status"] == "applied" and done["result"]["url"].endswith("/pages/999")
    assert fake.writes[0][1]["space_id"] == "262148"
    with pytest.raises(ca.ApplyError) as again:
        ca.apply(action["action_id"], client=fake)
    assert again.value.status == 409 and len(fake.writes) == 1


def test_a_jira_id_cannot_be_applied_as_confluence(writes_on) -> None:
    db.insert_integration_action("jira-only", "cjob-2", "jira", "add_comment", {}, 1.0)
    with pytest.raises(ca.ApplyError) as wrong:
        ca.apply("jira-only", client=FakeClient())
    assert wrong.value.status == 404


def test_rejected_write_fails_and_unsent_one_can_be_retried(writes_on) -> None:
    db.insert_integration_action("c-fail", "cjob-3", "confluence", "add_comment",
                                 {"page_id": "101", "storage": "<p>x</p>", "page_url": BASE}, 1.0)
    failed = ca.apply("c-fail", client=FakeClient(fail=cc.ConfluenceError("bad_query", "reddedildi")))
    assert failed["status"] == "failed"

    db.insert_integration_action("c-dns", "cjob-3", "confluence", "add_comment",
                                 {"page_id": "101", "storage": "<p>x</p>", "page_url": BASE}, 1.0)
    assert ca.apply("c-dns", client=FakeClient(fail=cc.ConfluenceError("dns", "yok")))["status"] == "proposed"
    assert ca.apply("c-dns", client=FakeClient())["status"] == "applied"


def test_nothing_is_written_when_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(cc, "is_write_enabled", lambda: False)
    db.insert_integration_action("c-off", "cjob-4", "confluence", "add_comment", {"page_id": "101", "storage": "x"}, 1.0)
    fake = FakeClient()
    with pytest.raises(ca.ApplyError):
        ca.apply("c-off", client=fake)
    assert fake.writes == []
    assert ca.cancel("c-off")["status"] == "cancelled"


def test_answer_instruction_makes_the_reply_the_page_draft() -> None:
    page = ca.answer_instruction("create_page")
    assert "YOUR REPLY IS THE DRAFT" in page and "Never say you cannot" in page
    comment = ca.answer_instruction("add_comment")
    assert "comment" in comment and "DRAFT" not in comment


def test_main_combines_both_instructions() -> None:
    import main

    both = main._write_answer_instruction({"jira_write_action": "create_issue", "confluence_write_action": "create_page"})
    assert "JIRA CHANGE" in both and "CONFLUENCE PAGE" in both
    assert main._write_answer_instruction({}) == ""
