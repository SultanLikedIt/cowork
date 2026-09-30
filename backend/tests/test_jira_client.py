"""The HTTP boundary: endpoints per deployment, auth, bounds and errors.

No network: a fake session records each request and replays canned
responses, so what is asserted is exactly what would have been sent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

import jira_client as jc


class _Response:
    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        *,
        headers: dict | None = None,
        raw: bytes | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
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


def _server(**overrides: Any) -> jc.JiraConfig:
    values = dict(
        enabled=True,
        base_url="https://jira.sirket.intra",
        deployment="server",
        personal_token="pat-secret",
    )
    values.update(overrides)
    return jc.JiraConfig(**values)


def _cloud(**overrides: Any) -> jc.JiraConfig:
    values = dict(
        enabled=True,
        base_url="https://acme.atlassian.net",
        deployment="cloud",
        username="me@acme.com",
        api_token="api-secret",
    )
    values.update(overrides)
    return jc.JiraConfig(**values)


# --- configuration -----------------------------------------------------------


def test_deployment_is_detected_from_the_url_and_can_be_forced() -> None:
    assert jc.load_config({"JIRA_URL": "https://acme.atlassian.net/"}).deployment == "cloud"
    assert jc.load_config({"JIRA_URL": "https://jira.sirket.intra"}).deployment == "server"
    forced = jc.load_config(
        {"JIRA_URL": "https://jira.sirket.intra", "JIRA_DEPLOYMENT": "cloud"}
    )
    assert forced.deployment == "cloud"
    assert jc.load_config({"JIRA_URL": "https://x.atlassian.net/"}).base_url == (
        "https://x.atlassian.net"
    )


def test_configuration_problems_are_reported_in_words() -> None:
    assert not jc.JiraConfig().configured
    assert "JIRA_ENABLED" in " ".join(jc.JiraConfig().problems())
    # Cloud does not take a Server PAT.
    cloud_with_pat = _cloud(username="", api_token="", personal_token="pat")
    assert "JIRA_API_TOKEN" in " ".join(cloud_with_pat.problems())
    assert _server().configured
    assert _cloud().configured
    assert not _server(base_url="jira.sirket.intra").configured


def test_secrets_stay_out_of_repr() -> None:
    text = repr(_cloud()) + repr(_server())
    assert "api-secret" not in text and "pat-secret" not in text


def test_timeouts_are_bounded() -> None:
    config = jc.load_config(
        {"JIRA_CONNECT_TIMEOUT_SECONDS": "0", "JIRA_TIMEOUT_SECONDS": "9999"}
    )
    assert config.connect_timeout_seconds == 1
    assert config.timeout_seconds == 120


# --- transport ---------------------------------------------------------------


def test_server_uses_a_bearer_pat_and_never_follows_redirects() -> None:
    session = _Session(_Response(200, {"displayName": "Ali"}))
    jc.JiraClient(_server(), session=session).myself()
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "https://jira.sirket.intra/rest/api/2/myself")
    assert kwargs["headers"]["Authorization"] == "Bearer pat-secret"
    assert kwargs["auth"] is None
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (jc.DEFAULT_CONNECT_TIMEOUT_SECONDS, jc.DEFAULT_TIMEOUT_SECONDS)


def test_cloud_uses_basic_auth_with_the_api_token() -> None:
    session = _Session(_Response(200, {"displayName": "Ali"}))
    jc.JiraClient(_cloud(), session=session).myself()
    _, _, kwargs = session.calls[0]
    assert kwargs["auth"] == ("me@acme.com", "api-secret")
    assert "Authorization" not in kwargs["headers"]


def test_nothing_is_sent_when_not_configured() -> None:
    session = _Session()
    with pytest.raises(jc.JiraError) as caught:
        jc.JiraClient(jc.JiraConfig(), session=session).myself()
    assert caught.value.code == "not_configured"
    assert session.calls == []


def test_cloud_search_uses_the_v3_jql_endpoint_with_cursor_paging() -> None:
    session = _Session(
        _Response(200, {"issues": [{"key": "A-1"}, {"key": "A-2"}], "nextPageToken": "t2", "isLast": False}),
        _Response(200, {"issues": [{"key": "A-3"}], "isLast": True}),
    )
    page = jc.JiraClient(_cloud(), session=session).search("project = A", limit=10)
    assert [i["key"] for i in page.issues] == ["A-1", "A-2", "A-3"]
    assert page.total is None and not page.truncated
    first, second = session.calls
    assert first[1] == "https://acme.atlassian.net/rest/api/3/search/jql"
    assert first[2]["params"]["jql"] == "project = A"
    assert "nextPageToken" not in first[2]["params"]
    assert second[2]["params"]["nextPageToken"] == "t2"


def test_cloud_search_reports_truncation_at_the_limit() -> None:
    session = _Session(
        _Response(200, {"issues": [{"key": "A-1"}, {"key": "A-2"}], "nextPageToken": "t2", "isLast": False}),
    )
    page = jc.JiraClient(_cloud(), session=session).search("x", limit=2)
    assert len(page.issues) == 2 and page.truncated
    assert len(session.calls) == 1


def test_server_search_uses_v2_with_start_at_and_total() -> None:
    session = _Session(
        _Response(200, {"issues": [{"key": "B-1"}, {"key": "B-2"}], "total": 3}),
        _Response(200, {"issues": [{"key": "B-3"}], "total": 3}),
    )
    page = jc.JiraClient(_server(), session=session).search("project = B", limit=50)
    assert [i["key"] for i in page.issues] == ["B-1", "B-2", "B-3"]
    assert page.total == 3 and not page.truncated
    assert session.calls[0][1] == "https://jira.sirket.intra/rest/api/2/search"
    assert session.calls[1][2]["params"]["startAt"] == 2


def test_count_is_cloud_only() -> None:
    session = _Session(_Response(200, {"count": 42}))
    assert jc.JiraClient(_cloud(), session=session).count("x") == 42
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url.endswith("/rest/api/3/search/approximate-count")
    assert json.loads(kwargs["data"]) == {"jql": "x"}
    assert jc.JiraClient(_server(), session=_Session()).count("x") is None


def test_recent_comments_fetches_the_newest_page() -> None:
    session = _Session(
        _Response(200, {"comments": [{"id": "1"}, {"id": "2"}], "total": 7}),
        _Response(200, {"comments": [{"id": "6"}, {"id": "7"}], "total": 7}),
    )
    comments, total = jc.JiraClient(_server(), session=session).recent_comments("A-1", limit=2)
    assert [c["id"] for c in comments] == ["6", "7"] and total == 7
    assert session.calls[1][2]["params"]["startAt"] == 5


def test_issue_keys_are_validated_before_any_request() -> None:
    session = _Session()
    client = jc.JiraClient(_server(), session=session)
    for bad in ("../../admin", "A-0", "a b-1", "PROJ-1/comment"):
        with pytest.raises(jc.JiraError):
            client.get_issue(bad)
    assert session.calls == []
    session = _Session(_Response(200, {"key": "PROJ-5"}))
    jc.JiraClient(_server(), session=session).get_issue("proj-5")
    assert session.calls[0][1] == "https://jira.sirket.intra/rest/api/2/issue/PROJ-5"


def test_user_search_parameter_differs_by_deployment() -> None:
    session = _Session(_Response(200, [{"accountId": "a1"}]))
    jc.JiraClient(_cloud(), session=session).find_users("Ahmet")
    assert session.calls[0][2]["params"]["query"] == "Ahmet"
    session = _Session(_Response(200, [{"name": "ahmet"}]))
    jc.JiraClient(_server(), session=session).find_users("Ahmet")
    assert session.calls[0][2]["params"]["username"] == "Ahmet"
    assert jc.user_literal({"accountId": "a1", "name": "n"}, cloud=True) == "a1"
    assert jc.user_literal({"accountId": "a1", "name": "n"}, cloud=False) == "n"


# --- errors ------------------------------------------------------------------


def _error_for(*responses: Any, config: jc.JiraConfig | None = None) -> jc.JiraError:
    client = jc.JiraClient(config or _server(), session=_Session(*responses))
    with pytest.raises(jc.JiraError) as caught:
        client.myself()
    return caught.value


def test_http_errors_are_classified_with_a_remedy() -> None:
    auth = _error_for(_Response(401, {}))
    assert auth.code == "auth" and "JIRA_PERSONAL_TOKEN" in auth.remedy and auth.fatal
    cloud_auth = _error_for(_Response(401, {}), config=_cloud())
    assert "JIRA_API_TOKEN" in cloud_auth.remedy
    bad = _error_for(_Response(400, {"errorMessages": ["Field 'sprint' does not exist."]}))
    assert bad.code == "bad_query" and "sprint" in bad.detail and not bad.fatal
    assert _error_for(_Response(404, {})).code == "not_found"
    assert _error_for(_Response(403, {})).code == "forbidden"
    assert _error_for(_Response(429, {})).code == "rate_limited"
    assert _error_for(_Response(503, {})).code == "server"


def test_a_captcha_lock_is_told_apart_from_a_bad_token() -> None:
    error = _error_for(
        _Response(403, {}, headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"})
    )
    assert error.code == "captcha" and error.fatal


def test_a_redirect_is_an_error_not_a_hop() -> None:
    error = _error_for(_Response(302, raw=b"<html>login</html>", headers={"Location": "https://sso"}))
    assert error.code == "redirect" and error.fatal


def test_network_failures_point_at_the_vpn() -> None:
    dns = _error_for(
        requests.exceptions.ConnectionError(
            "HTTPSConnectionPool: NameResolutionError: Failed to resolve 'jira.sirket.intra'"
        )
    )
    assert dns.code == "dns" and "VPN" in dns.remedy and dns.fatal
    down = _error_for(requests.exceptions.ConnectTimeout("timed out"))
    assert down.code == "unreachable" and "VPN" in down.remedy
    refused = _error_for(requests.exceptions.ConnectionError("Connection refused"))
    assert refused.code == "unreachable"
    assert _error_for(requests.exceptions.SSLError("cert")).code == "tls"
    assert _error_for(requests.exceptions.ReadTimeout("slow")).code == "timeout"


def test_an_html_page_is_not_parsed_as_data() -> None:
    error = _error_for(_Response(200, raw=b"<html>SSO</html>"))
    assert error.code == "bad_output"


def test_responses_are_size_bounded(monkeypatch) -> None:
    monkeypatch.setattr(jc, "MAX_RESPONSE_BYTES", 100)
    error = _error_for(_Response(200, raw=b"x" * 500))
    assert error.code == "too_large"


def test_tokens_never_appear_in_errors() -> None:
    for response in (
        _Response(401, {}),
        requests.exceptions.ConnectionError("NameResolutionError"),
        _Response(400, {"errorMessages": ["bad"]}),
    ):
        error = _error_for(response)
        assert "pat-secret" not in error.detail + error.remedy


# --- health ------------------------------------------------------------------


def test_health_reports_the_connected_user() -> None:
    report = jc.check_health(
        use_cache=False, config=_server(), session=_Session(_Response(200, {"displayName": "Ali Veli"}))
    )
    assert report.ok and report.user == "Ali Veli"
    assert report.as_dict()["ok"] is True


def test_health_reports_why_it_is_not_usable() -> None:
    unconfigured = jc.check_health(use_cache=False, config=jc.JiraConfig())
    assert not unconfigured.ok and unconfigured.error_code == "not_configured"
    offline = jc.check_health(
        use_cache=False,
        config=_server(),
        session=_Session(requests.exceptions.ConnectTimeout("x")),
    )
    assert offline.configured and not offline.reachable
    assert offline.error_code == "unreachable" and "VPN" in offline.remedy
