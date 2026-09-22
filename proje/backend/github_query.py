"""What a GitHub read may ask for: validated names, parsed links, search text.

Everything that reaches ``github_client`` passes through here first. The
values come from two places, and neither is trusted at a process or URL
boundary:

* **Links the user typed** (``find_targets``). Parsed into owner / repo /
  ref / path / number, each checked against GitHub's own naming rules, so
  that ``github.com/a/b/blob/main/../../../x`` is a bad link rather than a
  path that walks somewhere.
* **The PLAN model's output** (``RepoQuery``). Keywords, a language, a
  minimum star count. These end up only in the ``q`` query-string value of a
  fixed search endpoint -- the host and path never come from here -- but
  they are still bounded and allowlisted, because a query that asks GitHub
  for a million-result sort is a rate-limit problem even when it is not a
  security one.

Repositories named without a link ("facebook/react ile vuejs/core'u
karşılaştır") are accepted only when that ``owner/repo`` text literally
appears in the request (``verified_repos``) -- the same rule
``jira_query`` applies to issue keys, and for the same reason: the model
must not be able to invent something to open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import BaseModel, Field

# GitHub's rules: a user/org login is 1-39 characters, alphanumeric or
# single hyphens, not starting with a hyphen. Repository names allow
# letters, digits, '.', '-' and '_' up to 100 characters.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Branch / tag / sha. Git allows more than this; what it allows beyond it
# (spaces, '~', '^', ':') never appears in a link people paste, and every
# character left out here is one fewer way to build a surprising path.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_PATH_SEGMENT_RE = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,255}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9+#. -]{1,30}$")
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

MAX_PATH_CHARS = 400
MAX_KEYWORDS_CHARS = 200
MAX_TARGETS = 5
MAX_NAMED_REPOS = 5
MAX_SEARCH_LIMIT = 50

GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
RAW_HOSTS = frozenset({"raw.githubusercontent.com"})

# First path segments on github.com that are site pages, not users. A link
# to github.com/settings/... is not a repository called "settings".
_RESERVED_OWNERS = frozenset(
    {
        "about", "apps", "codespaces", "collections", "contact", "customer-stories",
        "enterprise", "events", "explore", "features", "join", "login", "logout",
        "marketplace", "new", "notifications", "orgs", "organizations", "pricing",
        "pulls", "issues", "search", "security", "settings", "site", "sponsors",
        "team", "topics", "trending", "readme", "resources", "solutions", "signup",
        "stars", "dashboard", "watching", "account", "copilot", "discussions",
    }
)

TargetKind = Literal["repo", "file", "dir", "issue", "pull", "trending"]


class QueryError(ValueError):
    """A name, path or link failed validation. Never reaches the network."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_owner(value: Any) -> str:
    owner = str(value or "").strip()
    if not _OWNER_RE.match(owner) or "--" in owner or owner.casefold() in _RESERVED_OWNERS:
        raise QueryError(f"geçersiz GitHub kullanıcı/kurum adı: {owner[:40]!r}")
    return owner


def validate_repo(value: Any) -> str:
    repo = str(value or "").strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO_RE.match(repo) or repo in {".", ".."}:
        raise QueryError(f"geçersiz GitHub depo adı: {repo[:40]!r}")
    return repo


def validate_ref(value: Any) -> str:
    ref = str(value or "").strip()
    if (
        not _REF_RE.match(ref)
        or ref.startswith(("-", "/", "."))
        or ref.endswith(("/", ".lock"))
        or ".." in ref
        or "//" in ref
    ):
        raise QueryError(f"geçersiz dal/etiket: {ref[:40]!r}")
    return ref


def validate_path(value: Any) -> str:
    """A repository-relative file path. Empty means the repository root.

    Rejects anything that is not plainly a path inside the repository:
    '..' and '.' segments, backslashes, control characters, a leading '/'.
    The client additionally URL-quotes each segment, so even a segment this
    lets through cannot add a path level or a query string.
    """
    path = str(value or "").strip()
    if not path:
        return ""
    if len(path) > MAX_PATH_CHARS or path.startswith("/") or "\\" in path:
        raise QueryError(f"geçersiz dosya yolu: {path[:60]!r}")
    segments = path.rstrip("/").split("/")
    for segment in segments:
        if segment in {"", ".", ".."} or not _PATH_SEGMENT_RE.match(segment):
            raise QueryError(f"geçersiz dosya yolu: {path[:60]!r}")
    return "/".join(segments)


def validate_number(value: Any) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise QueryError(f"geçersiz numara: {str(value)[:20]!r}") from None
    if not 1 <= number <= 10_000_000:
        raise QueryError(f"geçersiz numara: {number}")
    return number


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubTarget:
    """One thing a link points at. Every field is already validated."""

    kind: TargetKind
    owner: str = ""
    repo: str = ""
    # For file/dir: the link's ref and path as written. A branch name may
    # itself contain '/', so "blob/feature/x/src/a.py" is ambiguous; the
    # controller resolves that with ``ref_candidates``.
    ref: str = ""
    path: str = ""
    number: int = 0
    # trending only
    language: str = ""
    since: str = ""
    url: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}" if self.owner and self.repo else ""

    def label(self) -> str:
        if self.kind == "issue":
            return f"{self.full_name}#{self.number}"
        if self.kind == "pull":
            return f"{self.full_name} PR #{self.number}"
        if self.kind in {"file", "dir"} and self.path:
            return f"{self.full_name}/{self.path}"
        if self.kind == "trending":
            return "GitHub trending" + (f" ({self.language})" if self.language else "")
        return self.full_name

    def ref_candidates(self) -> list[tuple[str, str]]:
        """(ref, path) splits to try, most likely first.

        "tree/feature/login/src" could be ref "feature" + path "login/src" or
        ref "feature/login" + path "src". GitHub's own UI resolves this
        against the branch list; the controller instead tries the first
        split and, on "not found", the next -- at most three, since branch
        names with more than two slashes are rare and each try is a request.
        """
        if not self.ref:
            return [("", self.path)]
        segments = [s for s in (self.ref + "/" + self.path).split("/") if s]
        splits: list[tuple[str, str]] = []
        for cut in range(1, min(len(segments), 3) + 1):
            ref = "/".join(segments[:cut])
            path = "/".join(segments[cut:])
            try:
                splits.append((validate_ref(ref), validate_path(path)))
            except QueryError:
                continue
        return splits or [(self.ref, self.path)]


_URL_IN_TEXT_RE = re.compile(
    # The lookbehind keeps "notgithub.com/x/y" and "api.github.com/..." out.
    r"(?<![\w.-])(?:https?://)?(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/[^\s<>\"'`)\]]+",
    re.IGNORECASE,
)


def parse_url(url: str) -> Optional[GitHubTarget]:
    """The target a github.com / raw.githubusercontent.com link names, or None.

    None means "not a GitHub link this reader handles" -- a user profile, a
    settings page, a gist, another host -- so the caller can fall back to
    the ordinary web reader rather than fail.
    """
    text = str(url or "").strip().rstrip(".,;:!?")
    if not text:
        return None
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.port not in (None, 443, 80):
        return None
    segments = [unquote(s) for s in parts.path.split("/") if s]
    canonical = f"https://{host}{parts.path}".rstrip("/")
    try:
        if host in RAW_HOSTS:
            # raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>
            if len(segments) < 4:
                return None
            owner, repo = validate_owner(segments[0]), validate_repo(segments[1])
            # "refs/heads/main/x" is how newer raw links spell the branch.
            rest = segments[2:]
            if rest[:2] == ["refs", "heads"] and len(rest) > 3:
                rest = rest[2:]
            ref = validate_ref(rest[0])
            path = validate_path("/".join(rest[1:]))
            return GitHubTarget(
                "file", owner, repo, ref=ref, path=path,
                url=f"https://github.com/{owner}/{repo}/blob/{ref}/{path}",
            )
        if host not in GITHUB_HOSTS:
            return None
        if not segments:
            return None
        if segments[0].casefold() == "trending":
            query = parse_qs(parts.query)
            since = (query.get("since") or [""])[0].casefold()
            language = segments[1] if len(segments) > 1 else ""
            if language and not _LANGUAGE_RE.match(language):
                language = ""
            return GitHubTarget(
                "trending",
                language=language,
                since=since if since in {"daily", "weekly", "monthly"} else "daily",
                url=canonical,
            )
        if len(segments) < 2:
            return None  # a user or org profile: not handled in this phase
        owner, repo = validate_owner(segments[0]), validate_repo(segments[1])
        base = f"https://github.com/{owner}/{repo}"
        rest = segments[2:]
        if not rest:
            return GitHubTarget("repo", owner, repo, url=base)
        section = rest[0].casefold()
        if section in {"issues", "pull", "pulls"} and len(rest) >= 2 and rest[1].isdigit():
            number = validate_number(rest[1])
            kind: TargetKind = "issue" if section == "issues" else "pull"
            path = "issues" if kind == "issue" else "pull"
            return GitHubTarget(kind, owner, repo, number=number, url=f"{base}/{path}/{number}")
        if section in {"blob", "tree"} and len(rest) >= 2:
            ref = validate_ref(rest[1])
            path = validate_path("/".join(rest[2:]))
            if section == "tree" and not path:
                # A branch view of the whole repository.
                return GitHubTarget("repo", owner, repo, ref=ref, url=f"{base}/tree/{ref}")
            kind = "file" if section == "blob" else "dir"
            return GitHubTarget(
                kind, owner, repo, ref=ref, path=path, url=f"{base}/{section}/{ref}/{path}"
            )
        # Everything else under a repository -- releases, wiki, actions,
        # commits, the issues list -- is best answered by the overview.
        return GitHubTarget("repo", owner, repo, url=base)
    except QueryError:
        return None


def find_targets(text: str, *, limit: int = MAX_TARGETS) -> list[GitHubTarget]:
    """Every distinct GitHub target linked in ``text``, in order."""
    found: list[GitHubTarget] = []
    seen: set[tuple] = set()
    for match in _URL_IN_TEXT_RE.finditer(text or ""):
        target = parse_url(match.group(0))
        if target is None:
            continue
        key = (target.kind, target.owner.casefold(), target.repo.casefold(),
               target.ref, target.path, target.number, target.language)
        if key in seen:
            continue
        seen.add(key)
        found.append(target)
        if len(found) >= limit:
            break
    return found


def has_github_link(text: str) -> bool:
    return bool(find_targets(text, limit=1))


# --------------------------------------------------------------------------
# PLAN output
# --------------------------------------------------------------------------

SortKey = Literal["stars", "forks", "updated", "best_match"]


class RepoQuery(BaseModel):
    """What PLAN extracts from the user's request. Nothing here is trusted
    until ``normalize_query`` has checked it against the request itself."""

    # Repositories the user named as "owner/repo" without a link.
    repos: list[str] = Field(default_factory=list)
    # Discovery: find repositories rather than read named ones.
    search: bool = False
    keywords: str = ""
    language: str = ""
    topic: str = ""
    min_stars: int = 0
    created_within_days: int = 0
    pushed_within_days: int = 0
    sort: SortKey = "best_match"
    limit: int = 10
    # True when the answer depends on what repositories SAY (README, files,
    # issue text) rather than on the list fields alone.
    needs_details: bool = False


def verified_repos(names: Any, request_text: str) -> list[tuple[str, str]]:
    """``owner/repo`` pairs the model returned that the user literally wrote."""
    body = (request_text or "").casefold()
    found: list[tuple[str, str]] = []
    for raw in names or []:
        value = str(raw or "").strip().strip("/")
        if value.casefold().startswith(("https://", "http://")):
            target = parse_url(value)
            value = target.full_name if target else ""
        value = re.sub(r"^(?:www\.)?github\.com/", "", value, flags=re.IGNORECASE)
        if value.count("/") != 1:
            continue
        owner_raw, repo_raw = value.split("/")
        try:
            owner, repo = validate_owner(owner_raw), validate_repo(repo_raw)
        except QueryError:
            continue
        if f"{owner}/{repo}".casefold() not in body:
            continue
        if (owner, repo) not in found:
            found.append((owner, repo))
        if len(found) >= MAX_NAMED_REPOS:
            break
    return found


def _clean_keywords(value: Any) -> str:
    text = " ".join(str(value or "").split())
    # Qualifiers are built by this module, not taken from the model: strip
    # anything that looks like one ("user:x", "repo:y", "is:private") so the
    # keyword string can only ever be keywords.
    text = re.sub(r"\b[a-z_]+:[^\s]+", " ", text, flags=re.IGNORECASE)
    text = text.replace('"', " ")
    text = " ".join(word.lstrip("-") for word in text.split())
    return text.strip()[:MAX_KEYWORDS_CHARS]


def normalize_query(parsed: RepoQuery, request_text: str) -> RepoQuery:
    language = str(parsed.language or "").strip()
    if not _LANGUAGE_RE.match(language):
        language = ""
    topic = str(parsed.topic or "").strip().casefold().replace(" ", "-")
    if not _TOPIC_RE.match(topic):
        topic = ""
    sort = parsed.sort if parsed.sort in {"stars", "forks", "updated", "best_match"} else "best_match"
    return RepoQuery(
        repos=[f"{o}/{r}" for o, r in verified_repos(parsed.repos, request_text)],
        search=bool(parsed.search),
        keywords=_clean_keywords(parsed.keywords),
        language=language,
        topic=topic,
        min_stars=max(0, min(int(parsed.min_stars or 0), 1_000_000)),
        created_within_days=max(0, min(int(parsed.created_within_days or 0), 3650)),
        pushed_within_days=max(0, min(int(parsed.pushed_within_days or 0), 3650)),
        sort=sort,
        limit=max(1, min(int(parsed.limit or 10), MAX_SEARCH_LIMIT)),
        needs_details=bool(parsed.needs_details),
    )


def fallback_query(request_text: str) -> RepoQuery:
    """When PLAN fails: read what was linked or named, search nothing.

    A failed plan must not turn into a broad search -- anonymous search is
    ten requests a minute, and guessing keywords from a sentence mostly
    finds noise.
    """
    # Links are read on their own (find_targets); left in, "github.com/a/b"
    # would also offer "com/a" as a name.
    without_links = re.sub(r"\S*(?:https?://|github\.com/|githubusercontent\.com/)\S*", " ", request_text or "")
    names = [
        name
        for name in re.findall(
            r"\b[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\b", without_links
        )
        # "2024/2025" and "1/2" are not repositories; without the model to
        # tell, a name must at least contain letters on both sides.
        if all(re.search(r"[A-Za-z]", part) for part in name.split("/"))
    ]
    return RepoQuery(
        repos=[f"{o}/{r}" for o, r in verified_repos(names, request_text)],
        needs_details=True,
    )


# Trending has no API. "Trending this week" is approximated the way people
# usually do it: repositories created recently, most stars first.
TRENDING_WINDOWS = {"daily": 7, "weekly": 30, "monthly": 90}
TRENDING_MIN_STARS = 20


def trending_query(target: GitHubTarget) -> RepoQuery:
    return RepoQuery(
        search=True,
        language=target.language if _LANGUAGE_RE.match(target.language or "") else "",
        created_within_days=TRENDING_WINDOWS.get(target.since, 7),
        min_stars=TRENDING_MIN_STARS,
        sort="stars",
        limit=15,
    )


def _qualifier_value(value: str) -> str:
    return f'"{value}"' if " " in value else value


def build_search_q(query: RepoQuery, *, today: Optional[date] = None) -> str:
    """The ``q`` value for /search/repositories.

    A bare "popular repositories" request with no keyword at all still needs
    something to search for -- GitHub rejects an empty q -- so a star floor
    stands in for it.
    """
    today = today or date.today()
    parts: list[str] = []
    if query.keywords:
        parts.append(query.keywords)
    if query.language:
        parts.append(f"language:{_qualifier_value(query.language)}")
    if query.topic:
        parts.append(f"topic:{query.topic}")
    stars = query.min_stars
    if not parts and not stars and not query.created_within_days:
        stars = 1000
    if stars:
        parts.append(f"stars:>={stars}")
    if query.created_within_days:
        since = today - timedelta(days=query.created_within_days)
        parts.append(f"created:>={since.isoformat()}")
    if query.pushed_within_days:
        since = today - timedelta(days=query.pushed_within_days)
        parts.append(f"pushed:>={since.isoformat()}")
    if not parts:
        parts.append("stars:>=1000")
    # Archived projects are rarely what "popular" or "trending" means.
    parts.append("archived:false")
    return " ".join(parts)
