"""From the model's reading of a request to a JQL string.

The model never writes JQL. The PLAN phase fills in ``JiraQuery`` -- a small
set of typed fields -- and ``build_jql`` turns those into a query from fixed
templates, quoting every value. Three reasons:

* **It keeps working on a model that does not know JQL well.** Field names,
  operator rules and functions (``currentUser()``, ``openSprints()``) are
  the part models get wrong; a status category and a day count are not.
* **Values cannot change the query's shape.** A name containing ``"`` or
  ``OR project = SECRET`` is one quoted literal, not a new clause. (The
  account's own Jira permissions still decide what it can see -- this is
  about the query meaning what the user asked, not an access control.)
* **Two things are only accepted when the user typed them.** Issue keys
  (a model can "remember" PROJ-123 from nowhere) and raw JQL (for the user
  who writes their own). Same rule as mail recipients in
  ``artifact_intent._verified_recipients``: the request text is the only
  authority.

Localization, and why status *categories* are preferred
--------------------------------------------------------
Status, issue-type and priority names are per-instance and often translated
("Yapılıyor", "Hata"), so a guessed ``status = "In Progress"`` fails with a
400 on many instances. Status categories are fixed ids on every instance
(2 = To Do, 4 = In Progress, 3 = Done), so "açık işler" becomes
``statusCategory != 3`` and works everywhere. Name-based filters are still
supported but are "soft": ``build_jql(relaxed=True)`` drops them, and the
controller retries that way when Jira rejects the strict query.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

import jira_client

MAX_LIST_VALUES = 8
MAX_NAME_CHARS = 80
MAX_TEXT_CHARS = 120
MAX_JQL_CHARS = 1000
MAX_ISSUE_KEYS = 10
MAX_DAYS = 3650
# Cloud's search endpoint refuses a query with no restriction at all, and an
# unrestricted query is rarely what "Jira'da neler oluyor" means anyway.
DEFAULT_WINDOW_DAYS = 30

STATUS_CATEGORIES = ("open", "todo", "in_progress", "done")
_CATEGORY_JQL = {
    "open": "statusCategory != 3",
    "todo": "statusCategory = 2",
    "in_progress": "statusCategory = 4",
    "done": "statusCategory = 3",
}
ORDERS = {
    "updated": "updated DESC",
    "created": "created DESC",
    "priority": "priority DESC, updated DESC",
    "duedate": "duedate ASC",
}

# Candidate issue keys in free text: letters/digits/underscore, a dash, a
# number. Case-insensitive because people type "proj-12".
_KEY_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_]{0,29}-[1-9][0-9]{0,8})(?![A-Za-z0-9_-])")
# Lucene syntax inside `text ~ "..."`. Escaping these is fragile across Jira
# versions; removing them loses nothing a keyword search needed.
_LUCENE_SPECIAL_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


class JiraQuery(BaseModel):
    """What the user's request asks of Jira, as the PLAN phase reads it."""

    issue_keys: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    text: str = ""
    status_category: str = ""
    statuses: list[str] = Field(default_factory=list)
    issue_types: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    assignee: str = ""
    reporter: str = ""
    current_sprint: bool = False
    overdue: bool = False
    created_within_days: int = 0
    updated_within_days: int = 0
    resolved_within_days: int = 0
    order_by: str = ""
    needs_details: bool = False
    jql: str = ""

    def has_filters(self) -> bool:
        """Anything that calls for a search, as opposed to reading named keys."""
        return bool(
            self.jql
            or self.projects
            or self.text
            or self.status_category
            or self.statuses
            or self.issue_types
            or self.priorities
            or self.assignee
            or self.reporter
            or self.current_sprint
            or self.overdue
            or self.created_within_days
            or self.updated_within_days
            or self.resolved_within_days
        )


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _plain(value: Any, limit: int) -> str:
    """One line of text: no control characters, collapsed whitespace."""
    text = "".join(
        " " if unicodedata.category(ch).startswith("C") else ch for ch in str(value or "")
    )
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _names(values: Any) -> list[str]:
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        name = _plain(value, MAX_NAME_CHARS)
        if name and name.casefold() not in {n.casefold() for n in out}:
            out.append(name)
        if len(out) >= MAX_LIST_VALUES:
            break
    return out


def _days(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(number, MAX_DAYS))


def issue_keys_in_text(text: str) -> list[str]:
    """Issue-key-shaped tokens the user actually typed, uppercased, in order."""
    found: list[str] = []
    for match in _KEY_IN_TEXT_RE.findall(text or ""):
        key = match.upper()
        if jira_client.ISSUE_KEY_RE.match(key) and key not in found:
            found.append(key)
    return found


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def jql_typed_by_user(jql: str, request_text: str) -> str:
    """``jql`` if the user literally wrote it, else "".

    Compared after collapsing whitespace and case, so a query copied across
    lines still counts; anything the model composed or "improved" does not.
    """
    candidate = _plain(jql, MAX_JQL_CHARS)
    if not candidate:
        return ""
    return candidate if _squash(candidate) in _squash(request_text) else ""


def clean_text_query(text: Any) -> str:
    words = _LUCENE_SPECIAL_RE.sub(" ", _plain(text, MAX_TEXT_CHARS * 2))
    return re.sub(r"\s+", " ", words).strip()[:MAX_TEXT_CHARS]


def normalize_query(raw: Any, request_text: str) -> JiraQuery:
    """Bring the PLAN output inside the bounds ``build_jql`` relies on."""
    if not isinstance(raw, JiraQuery):
        raw = JiraQuery()
    typed_keys = issue_keys_in_text(request_text)
    claimed = {str(k).strip().upper() for k in raw.issue_keys or []}
    # Only keys the user typed; of those, only ones the model also read as
    # issue keys -- "COVID-19" or "UTF-8" in a request are key-shaped but the
    # model knows better. (A PLAN that failed outright never gets here:
    # fallback_query takes the typed keys as they are.)
    keys = [k for k in typed_keys if k in claimed]
    status_category = str(raw.status_category or "").strip().lower()
    order_by = str(raw.order_by or "").strip().lower()
    assignee = _plain(raw.assignee, MAX_NAME_CHARS)
    reporter = _plain(raw.reporter, MAX_NAME_CHARS)
    if reporter.casefold() == "unassigned":
        reporter = ""
    return JiraQuery(
        issue_keys=keys[:MAX_ISSUE_KEYS],
        projects=_names(raw.projects),
        text=clean_text_query(raw.text),
        status_category=status_category if status_category in STATUS_CATEGORIES else "",
        statuses=_names(raw.statuses),
        issue_types=_names(raw.issue_types),
        priorities=_names(raw.priorities),
        assignee=assignee,
        reporter=reporter,
        current_sprint=bool(raw.current_sprint),
        overdue=bool(raw.overdue),
        created_within_days=_days(raw.created_within_days),
        updated_within_days=_days(raw.updated_within_days),
        resolved_within_days=_days(raw.resolved_within_days),
        order_by=order_by if order_by in ORDERS else "",
        needs_details=bool(raw.needs_details) or bool(keys),
        jql=jql_typed_by_user(raw.jql, request_text),
    )


def fallback_query(request_text: str) -> JiraQuery:
    """When PLAN fails: the typed keys if there are any, else a recent-activity
    keyword search. Broad on purpose; a failed plan must not guess filters."""
    keys = issue_keys_in_text(request_text)[:MAX_ISSUE_KEYS]
    if keys:
        return JiraQuery(issue_keys=keys, needs_details=True)
    return JiraQuery(text=clean_text_query(request_text))


# --------------------------------------------------------------------------
# JQL
# --------------------------------------------------------------------------


def literal(value: str) -> str:
    """A JQL string literal. Backslash first, then the quote."""
    text = _plain(value, 200).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _in_clause(field_name: str, values: list[str]) -> str:
    if len(values) == 1:
        return f"{field_name} = {literal(values[0])}"
    return f"{field_name} in ({', '.join(literal(v) for v in values)})"


@dataclass
class People:
    """Assignee/reporter as JQL values, after the controller looked them up.

    ``None`` means "no filter" -- either none was asked, or the name could not
    be resolved to exactly one person, in which case the controller says so
    in the notes rather than filtering by a guess.
    """

    assignee: Optional[str] = None
    reporter: Optional[str] = None


def person_clause(field_name: str, requested: str, resolved: Optional[str]) -> str:
    who = (requested or "").strip().casefold()
    if who in {"me", "ben", "currentuser", "currentuser()"}:
        return f"{field_name} = currentUser()"
    if who in {"unassigned", "empty", "none"} and field_name == "assignee":
        return "assignee is EMPTY"
    if resolved:
        return f"{field_name} = {literal(resolved)}"
    return ""


@dataclass
class BuiltQuery:
    jql: str
    # Human-readable names of filters left out (relaxed retry, or defaults).
    dropped: list[str] = field(default_factory=list)
    defaulted_window: bool = False


def build_jql(
    query: JiraQuery, *, people: Optional[People] = None, relaxed: bool = False
) -> BuiltQuery:
    """Compose JQL from validated fields.

    ``relaxed`` leaves out the filters that depend on instance-specific
    names (status, issue type, priority, sprint), for the retry after Jira
    rejected them.
    """
    if query.jql:
        return BuiltQuery(jql=query.jql)
    people = people or People()
    clauses: list[str] = []
    dropped: list[str] = []

    if query.projects:
        clauses.append(_in_clause("project", query.projects))
    if query.text:
        clauses.append(f"text ~ {literal(query.text)}")
    if query.status_category:
        clauses.append(_CATEGORY_JQL[query.status_category])

    soft = [
        ("durum", "status", query.statuses),
        ("kayıt türü", "issuetype", query.issue_types),
        ("öncelik", "priority", query.priorities),
    ]
    for label, field_name, values in soft:
        if not values:
            continue
        if relaxed:
            dropped.append(label)
        else:
            clauses.append(_in_clause(field_name, values))
    if query.current_sprint:
        if relaxed:
            dropped.append("aktif sprint")
        else:
            clauses.append("sprint in openSprints()")

    for field_name, requested, resolved in (
        ("assignee", query.assignee, people.assignee),
        ("reporter", query.reporter, people.reporter),
    ):
        if requested:
            clause = person_clause(field_name, requested, resolved)
            if clause:
                clauses.append(clause)

    if query.overdue:
        clauses.append("duedate < startOfDay() AND statusCategory != 3")
    if query.created_within_days:
        clauses.append(f"created >= -{query.created_within_days}d")
    if query.updated_within_days:
        clauses.append(f"updated >= -{query.updated_within_days}d")
    if query.resolved_within_days:
        clauses.append(f"resolved >= -{query.resolved_within_days}d")

    defaulted = False
    if not clauses:
        clauses.append(f"updated >= -{DEFAULT_WINDOW_DAYS}d")
        defaulted = True

    order = ORDERS.get(query.order_by or "updated", ORDERS["updated"])
    return BuiltQuery(
        jql=" AND ".join(clauses) + f" ORDER BY {order}",
        dropped=dropped,
        defaulted_window=defaulted,
    )


def needs_person_lookup(requested: str) -> bool:
    who = (requested or "").strip().casefold()
    return bool(who) and who not in {
        "me",
        "ben",
        "currentuser",
        "currentuser()",
        "unassigned",
        "empty",
        "none",
    }


def pick_user(users: list[dict[str, Any]], requested: str) -> Optional[dict[str, Any]]:
    """Exactly one person, or None. Never an arbitrary pick among several."""
    wanted = (requested or "").strip().casefold()
    active = [u for u in users if u.get("active", True) is not False]
    exact = [
        u
        for u in active
        if any(
            str(u.get(k) or "").strip().casefold() == wanted
            for k in ("displayName", "name", "key", "emailAddress")
        )
    ]
    if len(exact) == 1:
        return exact[0]
    if not exact and len(active) == 1:
        return active[0]
    return None
