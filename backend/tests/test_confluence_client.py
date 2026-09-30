"""The Confluence HTTP boundary: config, allowlist, requests and errors.

No network: a fake session records each request and replays canned
responses, so what is asserted is exactly what would have been sent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

import confluence_client as cc


class _Response:
    def __init__(self, status: int = 200, payload: Any = None, *, raw: bytes | None = None) -> None:
        self.status_code = status
        self.headers: dict = {}
        if raw is None:
            raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self._raw = raw

    def iter_content(self, size: int):
        for start in range(0, len(self._raw), size):
            yield self._raw[start : start + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Session:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs: Any):
        self.calls.append((method, url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _cloud(**overrides: Any) -> cc.ConfluenceConfig:
    values = dict(
        enabled=True,
        base_url="https://acme.atlassian.net/wiki",
        deployment="cloud",
        spaces=("ENG", "HR"),
        username="me@acme.com",
        api_token="api-secret",
    )
    values.update(overrides)
    return cc.ConfluenceConfig(**values)


def _content(page_id: str, space: str, title: str = "Sayfa") -> dict:
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"key": space, "name": space},
        "version": {"when": "2026-09-01T10:00:00.000Z", "number": 3, "by": {"displayName": "Ayşe"}},
        "_links": {"webui": f"/spaces/{space}/pages/{page_id}/{title}"},
        "body": {"storage": {"value": "<p>Merhaba</p>"}},
    }


# --- configuration -------------------------------------------------------------


def test_cloud_confluence_comes_from_the_jira_site_and_account() -> None:
    config = cc.load_config(
        {
            "CONFLUENCE_ENABLED": "true",
            "CONFLUENCE_SPACES": "eng, hr ; ~ali",
            "JIRA_URL": "https://acme.atlassian.net",
            "JIRA_USERNAME": "me@acme.com",
            "JIRA_API_TOKEN": "t",
        }
    )
    assert config.base_url == "https://acme.atlassian.net/wiki"
    assert config.deployment == "cloud" and config.auth_mode == "basic"
    assert config.spaces == ("ENG", "HR", "~ali")
    assert config.configured


def test_confluence_settings_override_jira_ones() -> None:
    config = cc.load_config(
        {
            "CONFLUENCE_ENABLED": "true",
            "CONFLUENCE_SPACES": "ENG",
            "CONFLUENCE_URL": "https://wiki.sirket.intra/",
            "CONFLUENCE_PERSONAL_TOKEN": "conf-pat",
            "JIRA_URL": "https://jira.sirket.intra",
            "JIRA_PERSONAL_TOKEN": "jira-pat",
        }
    )
    assert config.base_url == "https://wiki.sirket.intra"
    assert config.deployment == "server"
    assert config.personal_token == "conf-pat" and config.auth_mode == "pat"


def test_a_server_jira_url_is_not_guessed_into_a_confluence_url() -> None:
    config = cc.load_config(
        {"CONFLUENCE_ENABLED": "true", "CONFLUENCE_SPACES": "ENG", "JIRA_URL": "https://jira.sirket.intra"}
    )
    assert config.base_url == ""
    assert not config.configured


def test_no_space_allowlist_means_not_configured() -> None:
    config = _cloud(spaces=())
    assert not config.configured
    assert any("CONFLUENCE_SPACES" in p for p in config.problems())


def test_the_token_is_not_in_repr() -> None:
    assert "api-secret" not in repr(_cloud())


def test_invalid_space_keys_are_dropped() -> None:
    # Quotes and brackets never survive into a key, so nothing here can
    # reach a CQL literal unquoted.
    assert cc.parse_spaces('ENG, "; DROP, a b, x)') == ("ENG", "DROP", "A", "B")


# --- requests -------------------------------------------------------------------


def test_search_is_a_get_with_the_allowlist_and_no_redirects() -> None:
    session = _Session(_Response(200, {"results": [_content("1", "ENG")], "totalSize": 1}))
    client = cc.ConfluenceClient(_cloud(), session=session)
    cql = f'{cc.build_space_clause(client.config.spaces)} AND text ~ "izin"'
    page = client.search(cql, limit=5)
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://acme.atlassian.net/wiki/rest/api/content/search"
    assert kwargs["params"]["cql"] == cql
    assert kwargs["allow_redirects"] is False
    assert kwargs["auth"] == ("me@acme.com", "api-secret")
    assert [r["id"] for r in page.results] == ["1"]


def test_a_query_without_the_allowlist_is_never_sent() -> None:
    session = _Session()
    client = cc.ConfluenceClient(_cloud(), session=session)
    with pytest.raises(cc.ConfluenceError) as exc:
        client.search('text ~ "izin"')
    assert exc.value.code == "bad_request"
    assert session.calls == []


def test_results_outside_the_allowlist_are_dropped() -> None:
    session = _Session(
        _Response(200, {"results": [_content("1", "ENG"), _content("2", "SECRET")]})
    )
    client = cc.ConfluenceClient(_cloud(), session=session)
    page = client.search(cc.build_space_clause(client.config.spaces))
    assert [r["id"] for r in page.results] == ["1"]


def test_get_page_is_scoped_to_the_allowlist() -> None:
    session = _Session(_Response(200, {"results": [_content("42", "HR")]}))
    client = cc.ConfluenceClient(_cloud(), session=session)
    page = client.get_page("42")
    cql = session.calls[0][2]["params"]["cql"]
    assert cql.startswith("id = 42 AND space in (")
    assert session.calls[0][2]["params"]["expand"].endswith("body.storage")
    assert page["id"] == "42"


def test_a_page_id_must_be_numeric() -> None:
    client = cc.ConfluenceClient(_cloud(), session=_Session())
    with pytest.raises(cc.ConfluenceError):
        client.get_page("42 OR space = SECRET")


def test_find_by_title_refuses_a_space_outside_the_allowlist() -> None:
    session = _Session()
    client = cc.ConfluenceClient(_cloud(), session=session)
    assert client.find_by_title("SECRET", "Maaşlar") is None
    assert session.calls == []


def test_cql_literals_cannot_break_out() -> None:
    assert cc.cql_string('a" OR space = "X') == '"a\\" OR space = \\"X"'


def test_a_personal_token_is_sent_as_bearer() -> None:
    session = _Session(_Response(200, {"results": []}))
    config = _cloud(deployment="server", base_url="https://wiki.sirket.intra", username="", api_token="", personal_token="pat")
    client = cc.ConfluenceClient(config, session=session)
    client.search(cc.build_space_clause(config.spaces))
    kwargs = session.calls[0][2]
    assert kwargs["headers"]["Authorization"] == "Bearer pat"
    assert kwargs["auth"] is None


# --- errors ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, code",
    [
        (_Response(302), "redirect"),
        (_Response(401), "auth"),
        (_Response(403, {"message": "no"}), "forbidden"),
        (_Response(400, {"message": "bad cql"}), "bad_query"),
        (_Response(429), "rate_limited"),
        (_Response(503), "server"),
        (_Response(200, raw=b"<html>login</html>"), "bad_output"),
    ],
)
def test_errors_are_classified(response, code) -> None:
    client = cc.ConfluenceClient(_cloud(), session=_Session(response))
    with pytest.raises(cc.ConfluenceError) as exc:
        client.search(cc.build_space_clause(client.config.spaces))
    assert exc.value.code == code
    assert "api-secret" not in exc.value.detail


def test_network_failures_say_what_to_fix() -> None:
    client = cc.ConfluenceClient(
        _cloud(), session=_Session(requests.exceptions.ConnectTimeout("x"))
    )
    with pytest.raises(cc.ConfluenceError) as exc:
        client.search(cc.build_space_clause(client.config.spaces))
    assert exc.value.code == "unreachable" and exc.value.fatal
    assert "VPN" in exc.value.remedy


def test_health_reports_missing_configuration_without_a_request() -> None:
    report = cc.check_health(use_cache=False, config=_cloud(spaces=()), session=_Session())
    assert not report.ok and report.error_code == "not_configured"


def test_health_reports_the_user_and_the_spaces() -> None:
    session = _Session(_Response(200, {"displayName": "Burak"}))
    report = cc.check_health(use_cache=False, config=_cloud(), session=session)
    assert report.ok and report.user == "Burak"
    assert report.as_dict()["spaces"] == ["ENG", "HR"]
    assert session.calls[0][1].endswith("/rest/api/user/current")
