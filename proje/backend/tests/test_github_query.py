"""Link parsing, name validation and search text: everything that decides
what ``github_client`` may be asked for. No network, no model."""

from __future__ import annotations

from datetime import date

import pytest

import github_query as gq
from github_query import RepoQuery


# --- links -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, kind, owner, repo",
    [
        ("https://github.com/astral-sh/uv", "repo", "astral-sh", "uv"),
        ("github.com/astral-sh/uv", "repo", "astral-sh", "uv"),
        ("https://www.github.com/astral-sh/uv/", "repo", "astral-sh", "uv"),
        ("https://github.com/astral-sh/uv.git", "repo", "astral-sh", "uv"),
        ("https://github.com/astral-sh/uv/releases", "repo", "astral-sh", "uv"),
        ("https://github.com/astral-sh/uv/issues", "repo", "astral-sh", "uv"),
        ("https://github.com/pallets/flask/issues/5000", "issue", "pallets", "flask"),
        ("https://github.com/pallets/flask/pull/5001/files", "pull", "pallets", "flask"),
        ("https://github.com/pallets/flask/blob/main/src/flask/app.py", "file", "pallets", "flask"),
        ("https://github.com/pallets/flask/tree/main/src", "dir", "pallets", "flask"),
    ],
)
def test_links_are_parsed_into_targets(url, kind, owner, repo) -> None:
    target = gq.parse_url(url)
    assert target is not None
    assert (target.kind, target.owner, target.repo) == (kind, owner, repo)


def test_a_file_link_keeps_ref_and_path() -> None:
    target = gq.parse_url("https://github.com/pallets/flask/blob/2.3.x/src/flask/app.py#L10")
    assert (target.ref, target.path) == ("2.3.x", "src/flask/app.py")


def test_a_branch_view_is_the_repository_at_that_ref() -> None:
    target = gq.parse_url("https://github.com/pallets/flask/tree/stable")
    assert (target.kind, target.ref, target.path) == ("repo", "stable", "")


def test_raw_links_become_file_targets() -> None:
    target = gq.parse_url("https://raw.githubusercontent.com/pallets/flask/main/README.md")
    assert (target.kind, target.ref, target.path) == ("file", "main", "README.md")
    newer = gq.parse_url(
        "https://raw.githubusercontent.com/pallets/flask/refs/heads/main/src/a.py"
    )
    assert (newer.ref, newer.path) == ("main", "src/a.py")


def test_trending_links_carry_language_and_window() -> None:
    target = gq.parse_url("https://github.com/trending/rust?since=weekly")
    assert (target.kind, target.language, target.since) == ("trending", "rust", "weekly")
    assert gq.parse_url("https://github.com/trending").since == "daily"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/torvalds",  # a profile, not handled this phase
        "https://github.com/settings/tokens",
        "https://github.com/orgs/python/repositories",
        "https://gist.github.com/someone/abc123",
        "https://api.github.com/repos/a/b",
        "https://notgithub.com/a/b",
        "https://github.com.evil.example/a/b",
        "https://github.com:8443/a/b",
        "https://github.com/-bad/repo",
        "https://github.com/a/..",
        "",
    ],
)
def test_links_this_reader_does_not_handle_return_none(url) -> None:
    assert gq.parse_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/b/blob/main/../../../etc/passwd",
        "https://github.com/a/b/blob/main/src/%2e%2e/x",
        "https://github.com/a/b/blob/main/a\\b",
        "https://github.com/a/b/blob/-x/a.py",
        "https://github.com/a/b/blob/ma..in/a.py",
    ],
)
def test_path_tricks_are_not_links(url) -> None:
    assert gq.parse_url(url) is None


def test_find_targets_dedupes_and_ignores_lookalike_hosts() -> None:
    text = (
        "şuna bak https://github.com/astral-sh/uv ve github.com/astral-sh/uv, "
        "bir de https://api.github.com/repos/x/y ile notgithub.com/a/b; "
        "sonra https://github.com/pallets/flask/issues/1."
    )
    targets = gq.find_targets(text)
    assert [t.label() for t in targets] == ["astral-sh/uv", "pallets/flask#1"]


def test_find_targets_is_bounded() -> None:
    text = " ".join(f"https://github.com/o/r{i}" for i in range(20))
    assert len(gq.find_targets(text)) == gq.MAX_TARGETS


def test_ambiguous_refs_offer_a_few_splits_in_order() -> None:
    target = gq.parse_url("https://github.com/a/b/tree/feature/login/src/app")
    assert target.ref_candidates() == [
        ("feature", "login/src/app"),
        ("feature/login", "src/app"),
        ("feature/login/src", "app"),
    ]


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "a" * 40, "-lead", "trail--x", "settings", "a b"])
def test_bad_owners_are_refused(value) -> None:
    with pytest.raises(gq.QueryError):
        gq.validate_owner(value)


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a b", "x" * 101])
def test_bad_repos_are_refused(value) -> None:
    with pytest.raises(gq.QueryError):
        gq.validate_repo(value)


@pytest.mark.parametrize("value", ["/abs", "a/../b", "a//b", "./a", "a\\b", "a\x00b", "x/" * 300])
def test_bad_paths_are_refused(value) -> None:
    with pytest.raises(gq.QueryError):
        gq.validate_path(value)


def test_an_empty_path_is_the_root() -> None:
    assert gq.validate_path("") == ""
    assert gq.validate_path("src/app/") == "src/app"


# --- named repositories ------------------------------------------------------


def test_named_repos_are_kept_only_when_the_user_wrote_them() -> None:
    request = "facebook/react ile vuejs/core'u karşılaştır"
    kept = gq.verified_repos(["facebook/react", "vuejs/core", "sveltejs/svelte"], request)
    assert kept == [("facebook", "react"), ("vuejs", "core")]


def test_named_repos_accept_links_and_refuse_junk() -> None:
    request = "github.com/facebook/react hakkında"
    assert gq.verified_repos(["https://github.com/facebook/react"], request) == [
        ("facebook", "react")
    ]
    assert gq.verified_repos(["../etc", "a/b/c", ""], "a/b/c ../etc") == []


# --- search text -------------------------------------------------------------


def test_the_search_is_built_from_allowlisted_parts() -> None:
    query = gq.normalize_query(
        RepoQuery(
            search=True,
            keywords="web framework",
            language="Rust",
            min_stars=1000,
            pushed_within_days=180,
            sort="stars",
        ),
        "popüler rust web framework",
    )
    q = gq.build_search_q(query, today=date(2026, 9, 22))
    assert q == "web framework language:Rust stars:>=1000 pushed:>=2026-03-26 archived:false"


def test_model_supplied_qualifiers_are_stripped() -> None:
    query = gq.normalize_query(
        RepoQuery(search=True, keywords='user:victim is:private "exact" -o repo:x/y llm'),
        "llm",
    )
    assert query.keywords == "exact o llm"


def test_a_language_or_topic_that_is_not_one_is_dropped() -> None:
    query = gq.normalize_query(
        RepoQuery(search=True, language="python stars:>0", topic="Machine Learning"),
        "x",
    )
    assert query.language == ""
    assert query.topic == "machine-learning"


def test_a_bare_popular_request_still_searches_something() -> None:
    q = gq.build_search_q(RepoQuery(search=True), today=date(2026, 9, 22))
    assert q == "stars:>=1000 archived:false"


def test_bounds_are_applied() -> None:
    query = gq.normalize_query(
        RepoQuery(limit=10_000, min_stars=-5, created_within_days=99_999), "x"
    )
    assert query.limit == gq.MAX_SEARCH_LIMIT
    assert query.min_stars == 0
    assert query.created_within_days == 3650


def test_trending_is_approximated_by_recent_and_starred() -> None:
    target = gq.parse_url("https://github.com/trending/python?since=weekly")
    query = gq.trending_query(target)
    q = gq.build_search_q(query, today=date(2026, 9, 22))
    assert q == "language:python stars:>=20 created:>=2026-08-23 archived:false"
    assert query.sort == "stars"


def test_a_failed_plan_reads_only_what_was_named() -> None:
    query = gq.fallback_query("astral-sh/uv nedir, 2024/2025 ve 1/2 karşılaştır")
    assert query.repos == ["astral-sh/uv"]
    assert query.search is False
