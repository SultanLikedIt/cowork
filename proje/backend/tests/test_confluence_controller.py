"""Confluence query building, page parsing and the phase controller.

The controller runs against a fake client and a scripted ``complete`` -- no
network, no model -- so every assertion is about what this code decides.
"""

from __future__ import annotations

import asyncio
from typing import Any

import confluence_client as cc
import confluence_controller as ctl
import confluence_query as cq
from confluence_query import ConfluenceQuery

ALLOWED = ("ENG", "HR")


def _config(**overrides: Any) -> cc.ConfluenceConfig:
    values = dict(
        enabled=True,
        base_url="https://acme.atlassian.net/wiki",
        deployment="cloud",
        spaces=ALLOWED,
        username="me@acme.com",
        api_token="t",
    )
    values.update(overrides)
    return cc.ConfluenceConfig(**values)


def _content(page_id: str, space: str = "ENG", title: str = "Sayfa", body: str = "<p>İçerik</p>") -> dict:
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"key": space, "name": space},
        "version": {"when": "2026-09-01T10:00:00Z", "number": 2, "by": {"displayName": "Ayşe"}},
        "ancestors": [{"title": "Kılavuzlar"}],
        "_links": {"webui": f"/spaces/{space}/pages/{page_id}/{title}"},
        "body": {"storage": {"value": body}},
    }


class _Client:
    def __init__(self, *, search: list[dict] | None = None, pages: dict[str, dict] | None = None) -> None:
        self.config = _config()
        self._search = search or []
        self._pages = pages or {}
        self.queries: list[str] = []
        self.opened: list[str] = []

    def search(self, cql: str, *, limit: int = 25, with_body: bool = False) -> cc.SearchPage:
        self.queries.append(cql)
        return cc.SearchPage(results=list(self._search), total=len(self._search))

    def get_page(self, page_id: str):
        self.opened.append(page_id)
        return self._pages.get(page_id)

    def find_by_title(self, space: str, title: str):
        self.opened.append(f"{space}:{title}")
        return next((p for p in self._pages.values() if p["title"] == title), None)


def _completer(plan: dict, *, select: list[int] | None = None, answer: Any = "Özet.", seen: list | None = None):
    async def complete(phase: str, system: str, user: str, schema: Any):
        if seen is not None:
            seen.append((phase, user))
        if phase == "plan":
            return ConfluenceQuery(**plan)
        if phase == "select":
            return ctl.PageSelection(numbers=select or [1])
        if phase == "answer":
            if isinstance(answer, BaseException):
                raise answer
            return answer
        raise AssertionError(phase)

    return complete


def _run(coro):
    return asyncio.run(coro)


# --- CQL -----------------------------------------------------------------------


def test_every_query_carries_the_allowlist() -> None:
    built = cq.build_cql(ConfluenceQuery(text="izin politikası"), ALLOWED)
    assert built.cql.startswith(cc.build_space_clause(ALLOWED))
    assert 'text ~ "izin politikası"' in built.cql
    assert "type in (page, blogpost)" in built.cql
    assert "order by" not in built.cql  # a keyword search stays relevance-ranked


def test_a_named_space_narrows_but_cannot_widen() -> None:
    built = cq.build_cql(ConfluenceQuery(spaces=["HR", "SECRET"], text="izin"), ALLOWED)
    assert 'space in ("HR")' in built.cql
    assert "SECRET" not in built.cql
    assert built.refused_spaces == ["SECRET"]


def test_keywords_cannot_change_the_query_shape() -> None:
    built = cq.build_cql(ConfluenceQuery(text='x" OR space = "SECRET'), ALLOWED)
    assert "SECRET" in built.cql  # as a word inside the literal...
    assert 'OR space = "SECRET' not in built.cql  # ...never as a clause


def test_a_bare_listing_gets_a_recent_window() -> None:
    built = cq.build_cql(ConfluenceQuery(), ALLOWED)
    assert built.defaulted_window
    assert 'lastmodified >= now("-30d")' in built.cql
    assert built.cql.endswith("order by lastmodified desc")


# --- links and bodies --------------------------------------------------------------


def test_page_links_on_this_confluence_are_read() -> None:
    text = (
        "şuna bak https://acme.atlassian.net/wiki/spaces/ENG/pages/12345/Deploy+Runbook "
        "ve https://evil.example.com/wiki/spaces/ENG/pages/999/X "
        "ve https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=777"
    )
    refs = cq.page_refs(text, _config())
    assert refs == [cq.PageRef(page_id="12345", space="ENG"), cq.PageRef(page_id="777")]


def test_server_display_links_become_title_lookups() -> None:
    config = _config(base_url="https://wiki.sirket.intra", deployment="server")
    refs = cq.page_refs("https://wiki.sirket.intra/display/HR/Izin+Politikasi", config)
    assert refs == [cq.PageRef(space="HR", title="Izin Politikasi")]


def test_storage_format_becomes_readable_text() -> None:
    storage = (
        "<h2>Kurulum</h2><p>Önce <strong>VPN</strong> açılır.</p>"
        "<ul><li>Adım bir</li><li>Adım iki</li></ul>"
        "<table><tbody><tr><th>Ad</th><th>Değer</th></tr><tr><td>port</td><td>443</td></tr></tbody></table>"
        '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter>'
        "<ac:plain-text-body><![CDATA[kubectl apply -f x.yaml]]></ac:plain-text-body></ac:structured-macro>"
    )
    text = cq.storage_to_text(storage)
    assert "## Kurulum" in text
    assert "Önce VPN açılır." in text
    assert "- Adım bir" in text and "- Adım iki" in text
    assert "Ad | Değer" in text and "port | 443" in text
    assert "kubectl apply -f x.yaml" in text
    assert "bash" not in text  # macro parameters are not content


# --- controller -------------------------------------------------------------------


def test_search_select_read_answer() -> None:
    # More matches than MAX_DETAIL_PAGES, so SELECT actually chooses.
    matches = [_content(str(n), title=f"Sayfa {n}") for n in range(1, 7)]
    matches[2] = _content("3", title="İzin Politikası")
    client = _Client(
        search=matches,
        pages={"3": _content("3", title="İzin Politikası", body="<p>Yıllık izin 14 gündür.</p>")},
    )
    seen: list = []
    result = _run(
        ctl.acquire_confluence_evidence(
            "izin politikası ne diyor",
            complete=_completer({"text": "izin politikası"}, select=[3], answer="14 gün.", seen=seen),
            client=client,
        )
    )
    assert result.ok
    assert len(result.pages) == 6
    assert client.opened == ["3"]
    assert result.brief == "14 gün."
    answer_prompt = next(user for phase, user in seen if phase == "answer")
    assert "Yıllık izin 14 gündür." in answer_prompt
    assert "ALINTI" in answer_prompt  # page text reaches the model fenced


def test_a_linked_page_is_read_without_a_search() -> None:
    client = _Client(pages={"55": _content("55", title="Runbook")})
    result = _run(
        ctl.acquire_confluence_evidence(
            "https://acme.atlassian.net/wiki/spaces/ENG/pages/55/Runbook özetle",
            complete=_completer({}),
            client=client,
        )
    )
    assert client.queries == []
    assert [p.page_id for p in result.details] == ["55"]


def test_a_linked_page_in_another_space_is_refused() -> None:
    client = _Client(pages={"9": _content("9", space="SECRET")})
    result = _run(
        ctl.acquire_confluence_evidence(
            "https://acme.atlassian.net/wiki/spaces/SECRET/pages/9/Maas özetle",
            complete=_completer({}),
            client=client,
        )
    )
    assert client.opened == []
    assert result.details == []
    assert any("SECRET" in r and "izinli" in r for r in result.remarks)


def test_a_listing_opens_nothing() -> None:
    client = _Client(search=[_content("1"), _content("2")])
    result = _run(
        ctl.acquire_confluence_evidence(
            "ENG'de son güncellenen sayfalar",
            complete=_completer({"spaces": ["ENG"], "needs_details": False}),
            client=client,
        )
    )
    assert client.opened == [] and result.details == []
    notes = ctl.render_notes(result)
    assert "| Başlık | Space | Güncellendi | Değiştiren |" in notes
    assert "Eşleşen sayfa: 2." in notes


def test_a_refused_space_is_reported() -> None:
    client = _Client(search=[])
    result = _run(
        ctl.acquire_confluence_evidence(
            "SECRET space'inde maaş", complete=_completer({"spaces": ["SECRET"], "text": "maaş"}), client=client
        )
    )
    assert any("SECRET" in r for r in result.remarks)
    assert "SECRET" not in client.queries[0]


def test_a_failed_answer_falls_back_to_page_facts_not_bodies() -> None:
    client = _Client(
        search=[_content("1", title="Gizli")],
        pages={"1": _content("1", title="Gizli", body="<p>gizli_kalmali</p>")},
    )
    result = _run(
        ctl.acquire_confluence_evidence(
            "gizli sayfa ne diyor",
            complete=_completer({"text": "gizli"}, answer=RuntimeError("down")),
            client=client,
        )
    )
    assert "gizli_kalmali" not in result.brief
    assert "Gizli" in result.brief


def test_not_configured_is_an_explained_failure(monkeypatch) -> None:
    for name in ("CONFLUENCE_ENABLED", "CONFLUENCE_SPACES", "JIRA_URL"):
        monkeypatch.delenv(name, raising=False)

    async def complete(*args):
        raise AssertionError("no model call expected")

    result = _run(ctl.acquire_confluence_evidence("izin politikası", complete=complete))
    assert not result.ok and result.error_code == "not_configured"
    assert "CONFLUENCE OKUNAMADI" in ctl.render_notes(result)


def test_notes_carry_sources_and_the_allowlist() -> None:
    client = _Client(
        search=[_content("1", title="Runbook")],
        pages={"1": _content("1", title="Runbook")},
    )
    result = _run(
        ctl.acquire_confluence_evidence("runbook özetle", complete=_completer({"text": "runbook"}), client=client)
    )
    notes = ctl.render_notes(result)
    assert "yalnız izinli space'ler: ENG, HR" in notes
    assert "https://acme.atlassian.net/wiki/spaces/ENG/pages/1/Runbook" in notes
