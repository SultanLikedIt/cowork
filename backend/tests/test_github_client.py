"""The HTTP boundary: what is sent, rate limits, redirects, bounds, errors.

No network: a fake session records each request and replays canned
responses, so what is asserted is exactly what would have been sent.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
import requests

import github_client as gc


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


def _client(*responses: Any, token: str = "", use_cache: bool = False) -> tuple[gc.GitHubClient, _Session]:
    session = _Session(*responses)
    return gc.GitHubClient(gc.GitHubConfig(token=token), session=session, use_cache=use_cache), session


@pytest.fixture(autouse=True)
def _clean_cache():
    gc._CACHE.clear()
    gc.reset_health_cache()
    yield
    gc._CACHE.clear()


# --- configuration -----------------------------------------------------------


def test_reading_is_on_by_default_and_needs_no_token() -> None:
    cfg = gc.load_config({})
    assert cfg.enabled and cfg.configured and not cfg.authenticated
    assert cfg.api_url == "https://api.github.com"


def test_the_token_never_appears_in_repr() -> None:
    cfg = gc.load_config({"GITHUB_TOKEN": "github_pat_secret"})
    assert "github_pat_secret" not in repr(cfg)
    assert cfg.authenticated


def test_a_non_https_api_url_is_refused() -> None:
    assert not gc.load_config({"GITHUB_API_URL": "http://api.github.com"}).configured
    assert not gc.load_config({"GITHUB_ENABLED": "false"}).configured


def test_enterprise_links_point_at_the_web_host() -> None:
    cfg = gc.load_config({"GITHUB_API_URL": "https://git.sirket.intra/api/v3"})
    assert cfg.web_url == "https://git.sirket.intra"


# --- requests ----------------------------------------------------------------


def test_only_get_is_ever_sent_and_the_host_is_fixed() -> None:
    client, session = _client(_Response(200, {"full_name": "a/b"}))
    client.get_repo("a", "b")
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://api.github.com/repos/a/b"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["X-GitHub-Api-Version"]
    assert "Authorization" not in kwargs["headers"]


def test_the_client_has_no_write_methods() -> None:
    verbs = ("create", "delete", "update", "post", "put", "patch", "star", "fork", "comment", "merge")
    public = [name for name in dir(gc.GitHubClient) if not name.startswith("_")]
    assert not [name for name in public if name.startswith(verbs)]


def test_a_token_is_sent_as_bearer() -> None:
    client, session = _client(_Response(200, {}), token="github_pat_x")
    client.get_repo("a", "b")
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer github_pat_x"


def test_names_are_validated_before_any_request() -> None:
    client, session = _client()
    for owner, repo in (("a/../b", "c"), ("a", "../../x"), ("-a", "b")):
        with pytest.raises(Exception):
            client.get_repo(owner, repo)
    with pytest.raises(Exception):
        client.get_file("a", "b", "../secret")
    assert session.calls == []


def test_path_segments_are_quoted() -> None:
    client, session = _client(_Response(200, raw=b"hello"))
    client.get_file("a", "b", "docs/my file#1.md", ref="main")
    url = session.calls[0][1]
    assert url == "https://api.github.com/repos/a/b/contents/docs/my%20file%231.md"
    assert session.calls[0][2]["params"] == {"ref": "main"}
    assert session.calls[0][2]["headers"]["Accept"] == gc.ACCEPT_RAW


def test_search_parameters() -> None:
    client, session = _client(
        _Response(200, {"total_count": 2, "incomplete_results": False, "items": [{"full_name": "a/b"}]})
    )
    page = client.search_repositories("llm language:Python", sort="stars", limit=500)
    params = session.calls[0][2]["params"]
    assert params == {"q": "llm language:Python", "per_page": 50, "sort": "stars", "order": "desc"}
    assert page.total == 2 and page.items[0]["full_name"] == "a/b"


def test_best_match_sends_no_sort() -> None:
    client, session = _client(_Response(200, {"items": []}))
    client.search_repositories("x", sort="best_match")
    assert "sort" not in session.calls[0][2]["params"]


# --- files -------------------------------------------------------------------


def test_a_large_file_is_cut_not_refused() -> None:
    client, _ = _client(_Response(200, raw=b"x" * (gc.MAX_FILE_BYTES + 5000)))
    content = client.get_file("a", "b", "big.txt")
    assert content.truncated and len(content.text) == gc.MAX_FILE_BYTES


def test_a_binary_file_is_flagged() -> None:
    client, _ = _client(_Response(200, raw=b"\x89PNG\x00\x00binary"))
    assert client.get_file("a", "b", "logo.png").binary


def test_a_directory_behind_a_blob_link_is_detected() -> None:
    listing = [{"type": "file", "sha": "1", "path": "src/a.py", "name": "a.py", "size": 3}]
    client, _ = _client(_Response(200, listing))
    with pytest.raises(gc.GitHubError) as info:
        client.get_file("a", "b", "src")
    assert info.value.code == "not_a_file"


def test_a_json_array_file_is_still_a_file() -> None:
    client, _ = _client(_Response(200, raw=b'[1, 2, {"a": 3}]'))
    assert client.get_file("a", "b", "data.json").text.startswith("[1, 2")


def test_directory_listing_puts_directories_first() -> None:
    listing = [
        {"type": "file", "path": "README.md", "size": 10},
        {"type": "dir", "path": "src", "size": 0},
    ]
    client, _ = _client(_Response(200, listing))
    assert client.list_directory("a", "b").entries == [("src", "dir", 0), ("README.md", "file", 10)]


def test_a_missing_readme_is_empty_not_an_error() -> None:
    client, _ = _client(_Response(404, {"message": "Not Found"}))
    assert client.get_readme("a", "b") == ("", False)


def test_recent_comments_fetch_the_last_page() -> None:
    comments = [{"id": i} for i in range(20, 23)]
    earlier = [{"id": i} for i in range(12, 20)]
    client, session = _client(_Response(200, comments), _Response(200, earlier))
    got = client.recent_comments("a", "b", 7, total=19, limit=8)
    assert [c["id"] for c in got] == list(range(15, 23))
    assert session.calls[0][2]["params"] == {"per_page": 8, "page": 3}
    assert session.calls[1][2]["params"] == {"per_page": 8, "page": 2}


def test_no_comments_means_no_request() -> None:
    client, session = _client()
    assert client.recent_comments("a", "b", 1, total=0) == []
    assert session.calls == []


# --- redirects ---------------------------------------------------------------


def test_a_renamed_repository_redirect_is_followed_on_the_same_host() -> None:
    client, session = _client(
        _Response(301, headers={"Location": "https://api.github.com/repositories/42"}),
        _Response(200, {"full_name": "new/name"}),
    )
    assert client.get_repo("old", "name")["full_name"] == "new/name"
    assert session.calls[1][1] == "https://api.github.com/repositories/42"


def test_a_redirect_to_another_host_is_refused() -> None:
    client, session = _client(
        _Response(302, headers={"Location": "https://evil.example/steal"}),
    )
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert info.value.code == "redirect"
    assert len(session.calls) == 1


# --- errors ------------------------------------------------------------------


def test_an_exhausted_anonymous_limit_says_when_and_how_to_fix() -> None:
    reset = int(time.time()) + 600
    client, _ = _client(
        _Response(
            403,
            {"message": "API rate limit exceeded for 1.2.3.4."},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        )
    )
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert info.value.code == "rate_limited" and info.value.fatal
    assert "dakika" in info.value.detail
    assert "GITHUB_TOKEN" in info.value.remedy


def test_a_secondary_limit_is_a_rate_limit_too() -> None:
    client, _ = _client(
        _Response(403, {"message": "You have exceeded a secondary rate limit."}, headers={"Retry-After": "60"})
    )
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert info.value.code == "rate_limited"


def test_a_plain_403_is_not_a_rate_limit() -> None:
    client, _ = _client(_Response(403, {"message": "Resource not accessible"},
                                  headers={"X-RateLimit-Remaining": "55"}))
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert info.value.code == "forbidden" and not info.value.fatal


@pytest.mark.parametrize(
    "status, code, fatal",
    [(401, "auth", True), (404, "not_found", False), (422, "bad_query", False),
     (451, "blocked", False), (502, "server", False)],
)
def test_status_codes_are_classified(status, code, fatal) -> None:
    client, _ = _client(_Response(status, {"message": "x"}))
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert (info.value.code, info.value.fatal) == (code, fatal)


def test_network_failures_are_classified() -> None:
    client, _ = _client(requests.exceptions.ConnectionError("getaddrinfo failed"))
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert info.value.code == "dns"


def test_the_token_is_not_in_an_error() -> None:
    client, _ = _client(_Response(401, {"message": "Bad credentials"}), token="github_pat_secret")
    with pytest.raises(gc.GitHubError) as info:
        client.get_repo("a", "b")
    assert "github_pat_secret" not in str(info.value) + info.value.remedy


def test_rate_headers_are_remembered() -> None:
    client, _ = _client(_Response(200, {}, headers={"X-RateLimit-Remaining": "41", "X-RateLimit-Limit": "60"}))
    client.get_repo("a", "b")
    assert gc.GitHubClient.last_rate.remaining == 41


# --- cache -------------------------------------------------------------------


def test_a_fresh_response_is_served_from_cache() -> None:
    client, session = _client(
        _Response(200, {"full_name": "a/b"}, headers={"ETag": '"v1"'}), use_cache=True
    )
    client.get_repo("a", "b")
    assert client.get_repo("a", "b")["full_name"] == "a/b"
    assert len(session.calls) == 1


def test_a_stale_response_is_revalidated_with_its_etag(monkeypatch) -> None:
    client, session = _client(
        _Response(200, {"full_name": "a/b"}, headers={"ETag": '"v1"'}),
        _Response(304),
        use_cache=True,
    )
    client.get_repo("a", "b")
    for key, (stamp, etag, body) in list(gc._CACHE.items()):
        gc._CACHE[key] = (stamp - gc.CACHE_FRESH_SECONDS - 1, etag, body)
    assert client.get_repo("a", "b")["full_name"] == "a/b"
    assert session.calls[1][2]["headers"]["If-None-Match"] == '"v1"'


# --- health ------------------------------------------------------------------


def test_health_reports_remaining_budget() -> None:
    payload = {"resources": {"core": {"limit": 60, "remaining": 57}, "search": {"limit": 10, "remaining": 10}}}
    report = gc.check_health(use_cache=False, config=gc.GitHubConfig(), session=_Session(_Response(200, payload)))
    assert report.ok and report.core_remaining == 57 and report.search_limit == 10
    assert "anonim" in report.detail and "GITHUB_TOKEN" in report.remedy


def test_health_never_raises() -> None:
    report = gc.check_health(
        use_cache=False,
        config=gc.GitHubConfig(),
        session=_Session(requests.exceptions.ConnectTimeout()),
    )
    assert not report.ok and report.error_code == "unreachable"
