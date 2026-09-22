"""JQL composition: what the model may influence, and what it may not.

The model fills typed fields; build_jql owns the syntax. These tests pin the
properties that make that split worth having -- quoting, the "only if the
user typed it" rules for keys and raw JQL, and the relaxed retry.
"""

from __future__ import annotations

import jira_query as jq
from jira_query import JiraQuery


def test_literal_escapes_backslash_then_quote_and_drops_control_chars() -> None:
    assert jq.literal('a"b\\c') == '"a\\"b\\\\c"'
    assert jq.literal("line\nbreak\ttab") == '"line break tab"'


def test_a_value_cannot_add_a_clause() -> None:
    """A project "name" carrying JQL stays one quoted literal."""
    query = JiraQuery(projects=['X" OR project = SECRET OR project = "Y'])
    built = jq.build_jql(query)
    assert built.jql.startswith('project = "X\\" OR project = SECRET OR project = \\"Y"')
    # Exactly one top-level clause plus the ORDER BY.
    assert " AND " not in built.jql


def test_filters_compose_with_fixed_templates() -> None:
    query = JiraQuery(
        projects=["MOB", "Web Portal"],
        text="ödeme hatası",
        status_category="open",
        assignee="me",
        updated_within_days=7,
        order_by="priority",
    )
    jql = jq.build_jql(query).jql
    assert 'project in ("MOB", "Web Portal")' in jql
    assert 'text ~ "ödeme hatası"' in jql
    assert "statusCategory != 3" in jql
    assert "assignee = currentUser()" in jql
    assert "updated >= -7d" in jql
    assert jql.endswith("ORDER BY priority DESC, updated DESC")


def test_status_categories_use_language_independent_ids() -> None:
    for category, expected in (
        ("todo", "statusCategory = 2"),
        ("in_progress", "statusCategory = 4"),
        ("done", "statusCategory = 3"),
    ):
        assert expected in jq.build_jql(JiraQuery(status_category=category)).jql


def test_unassigned_and_person_clauses() -> None:
    assert "assignee is EMPTY" in jq.build_jql(JiraQuery(assignee="unassigned")).jql
    people = jq.People(assignee="557058:abc")
    jql = jq.build_jql(JiraQuery(assignee="Ahmet Yılmaz"), people=people).jql
    assert 'assignee = "557058:abc"' in jql
    # An unresolved name adds no clause rather than filtering by a guess.
    unresolved = jq.build_jql(JiraQuery(assignee="Ahmet", status_category="open")).jql
    assert "assignee" not in unresolved


def test_relaxed_drops_only_the_instance_specific_filters() -> None:
    query = JiraQuery(
        projects=["MOB"],
        statuses=["Code Review"],
        issue_types=["Bug"],
        priorities=["High"],
        current_sprint=True,
        status_category="open",
    )
    strict = jq.build_jql(query)
    assert 'status = "Code Review"' in strict.jql
    assert "sprint in openSprints()" in strict.jql
    relaxed = jq.build_jql(query, relaxed=True)
    assert 'project = "MOB"' in relaxed.jql
    assert "statusCategory != 3" in relaxed.jql
    for gone in ("status =", "issuetype", "priority =", "openSprints"):
        assert gone not in relaxed.jql
    assert relaxed.dropped == ["durum", "kayıt türü", "öncelik", "aktif sprint"]


def test_an_empty_query_is_bounded_by_default() -> None:
    """Cloud rejects unbounded JQL; and 'what is going on' means recently."""
    built = jq.build_jql(JiraQuery())
    assert built.defaulted_window
    assert built.jql.startswith(f"updated >= -{jq.DEFAULT_WINDOW_DAYS}d")


def test_overdue_excludes_done_issues() -> None:
    jql = jq.build_jql(JiraQuery(overdue=True)).jql
    assert "duedate < startOfDay() AND statusCategory != 3" in jql


def test_issue_keys_must_be_typed_by_the_user() -> None:
    raw = JiraQuery(issue_keys=["PROJ-12", "PROJ-99"])
    query = jq.normalize_query(raw, "proj-12 ne durumda?")
    # Typed in lowercase still counts; the invented one does not.
    assert query.issue_keys == ["PROJ-12"]
    assert query.needs_details


def test_key_shaped_words_the_model_rejected_are_not_read() -> None:
    raw = JiraQuery(text="covid", issue_keys=[])
    query = jq.normalize_query(raw, "COVID-19 ile ilgili açık işler")
    assert query.issue_keys == []


def test_raw_jql_only_when_the_user_wrote_it() -> None:
    typed = 'project = MOB AND status = "In Progress"'
    request = f"şu sorguyu çalıştır:   {typed.replace(' AND ', '  and ')}"
    query = jq.normalize_query(JiraQuery(jql=typed), request)
    assert query.jql == typed
    composed = jq.normalize_query(
        JiraQuery(jql="project = MOB ORDER BY created"), "MOB projesindeki işler"
    )
    assert composed.jql == ""
    # Raw JQL is used as-is.
    assert jq.build_jql(query).jql == typed


def test_text_loses_lucene_syntax_and_is_bounded() -> None:
    assert jq.clean_text_query('login* "hata" (500) -auth') == "login hata 500 auth"
    assert len(jq.clean_text_query("x" * 1000)) == jq.MAX_TEXT_CHARS


def test_normalization_rejects_unknown_enums_and_clamps_days() -> None:
    raw = JiraQuery(
        status_category="closed-ish",
        order_by="random",
        created_within_days=999_999,
        updated_within_days=-5,
        reporter="unassigned",
    )
    query = jq.normalize_query(raw, "x")
    assert query.status_category == ""
    assert query.order_by == ""
    assert query.created_within_days == jq.MAX_DAYS
    assert query.updated_within_days == 0
    assert query.reporter == ""


def test_fallback_reads_typed_keys_or_searches_the_words() -> None:
    by_key = jq.fallback_query("ABC-7 ve abc-8 i özetle")
    assert by_key.issue_keys == ["ABC-7", "ABC-8"]
    assert by_key.needs_details
    by_text = jq.fallback_query("ödeme sayfası hatası")
    assert by_text.issue_keys == [] and by_text.text == "ödeme sayfası hatası"


def test_pick_user_never_picks_among_several() -> None:
    users = [
        {"displayName": "Ahmet Yılmaz", "accountId": "a1"},
        {"displayName": "Ahmet Kaya", "accountId": "a2"},
    ]
    assert jq.pick_user(users, "Ahmet") is None
    assert jq.pick_user(users, "ahmet kaya")["accountId"] == "a2"
    assert jq.pick_user([users[0]], "Ahmet")["accountId"] == "a1"
    inactive = [{"displayName": "Ahmet", "accountId": "x", "active": False}]
    assert jq.pick_user(inactive, "Ahmet") is None
