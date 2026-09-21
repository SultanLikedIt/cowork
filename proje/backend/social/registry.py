"""Which social commands can exist at all, and what their arguments may be.

The security argument for this module
-------------------------------------
Every argument that reaches an upstream CLI here was written either by the
model (a search query it composed from the user's request) or by the user.
Neither is trusted input for a process boundary, so nothing builds a command
string: a ``Command`` is an argv *list* assembled from a fixed template plus
values that passed a per-field validator, and ``runner`` never sees a shell.

Three specific attacks this shape closes:

* **Shell metacharacters.** With argv there is no shell to interpret them, so
  ``"; rm -rf /"`` is a search query and nothing else.
* **Argument injection.** A query beginning with ``-`` would be read by the
  CLI as a flag -- ``--output``, ``--config`` -- turning a search into a file
  write. ``_query`` strips leading dashes, and every enum-shaped field
  (sort, time window) is matched against an allowlist rather than passed
  through.
* **Write operations.** rdt-cli can vote, save, subscribe and comment as the
  logged-in account. None of those verbs exist in ``_REDDIT_VERBS``, so no
  sequence of model output can produce one: the registry cannot emit a
  command that writes to Reddit. That is asserted in the tests, because it
  is the property most worth keeping while this file grows.

Where the argv shapes come from
-------------------------------
Verified against rdt-cli 0.4.2 itself on 2026-09-21 -- ``--help`` for every
verb, then each built command run against live Reddit to confirm it parses.
Three things that exercise corrected an assumption taken from the docs:

* sort values are per-subcommand (``sub`` has no ``relevance``), so one
  shared list would have produced usage errors on reasonable requests;
* a query of ``-o x`` really is consumed as ``--output`` -- confirmed, not
  theorised -- hence ``_query`` plus the ``--`` separator;
* ``--full-text`` changes only the human table, not ``--json``, so it was
  removed again rather than carried as cargo.

``social.health`` is what catches the next drift: a flag rename should show
up as a failed probe, not as a silently wrong search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

# Bounds. Deliberately small: every item costs an upstream request against a
# platform that rate-limits and bans, and the evidence block downstream has
# its own budget (untrusted_content.MAX_BLOCK_CHARS) that more items would
# only overflow.
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
MAX_QUERY_CHARS = 200
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 120

# Sort values are per-command, not global: rdt rejects a sort its subcommand
# does not define, so one shared list would turn a model's reasonable
# "sort by relevance while browsing a subreddit" into a usage error. Verified
# against rdt-cli 0.4.2 `--help` output, 2026-09-21.
_SEARCH_SORTS = ("relevance", "hot", "top", "new", "comments")
_SUB_SORTS = ("hot", "new", "top", "rising", "controversial", "best")
_TIMES = ("hour", "day", "week", "month", "year", "all")

# Reddit's own naming rules: 3-21 characters, letters/digits/underscore.
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{1,20}$")
# Base-36 post ids, with or without the "t3_" type prefix.
_POST_ID_RE = re.compile(r"^(?:t3_)?[a-z0-9]{4,13}$", re.IGNORECASE)
_PERMALINK_RE = re.compile(
    r"/(?:r/[A-Za-z0-9_]{2,21}/)?comments/([a-z0-9]{4,13})", re.IGNORECASE
)


class ArgumentError(ValueError):
    """A parameter failed validation. Never reaches a subprocess."""


@dataclass(frozen=True)
class Command:
    """One fully-validated invocation, ready for ``runner.run``."""

    platform: str
    verb: str
    argv: tuple[str, ...]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # Env var names this command's tool needs beyond the base allowlist. The
    # runner passes ONLY these, so a Reddit call never carries X's token and
    # neither ever carries MODEL_API_KEY.
    credential_env: tuple[str, ...] = ()

    @property
    def executable(self) -> str:
        return self.argv[0]

    def describe(self) -> str:
        """Loggable form. Safe by construction: no credential is in argv."""
        return " ".join(self.argv)


# --------------------------------------------------------------------------
# Field validators
# --------------------------------------------------------------------------


def _query(value: Any) -> str:
    """A free-text search query, made safe to pass as one argv element.

    Newlines and control characters go first: they cannot help a query, and
    they are what makes a log line forgeable (a query containing "\\n2026-01-01
    ERROR ..." writes a fake log entry). Leading dashes go next, so the value
    can never be read as a flag.
    """
    text = str(value or "")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.lstrip("-").strip()
    if not text:
        raise ArgumentError("arama sorgusu boş olamaz")
    return text[:MAX_QUERY_CHARS]


def _subreddit(value: Any) -> str:
    """A subreddit name, with the r/ prefix and a full URL both accepted."""
    text = str(value or "").strip()
    if "://" in text:
        text = urlparse(text).path
    text = text.strip("/")
    for prefix in ("r/", "/r/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("/")[0]
    if not _SUBREDDIT_RE.match(text):
        raise ArgumentError(f"geçersiz subreddit adı: {text[:40]!r}")
    return text


def _post_id(value: Any) -> str:
    """A post id, accepting the permalink the search results actually carry."""
    text = str(value or "").strip()
    if "://" in text or "/comments/" in text:
        match = _PERMALINK_RE.search(urlparse(text).path if "://" in text else text)
        if not match:
            raise ArgumentError(f"gönderi kimliği çıkarılamadı: {text[:60]!r}")
        text = match.group(1)
    if not _POST_ID_RE.match(text):
        raise ArgumentError(f"geçersiz gönderi kimliği: {text[:40]!r}")
    return text.lower()


def _limit(value: Any, *, default: int = DEFAULT_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, MAX_LIMIT))


def _choice(value: Any, allowed: tuple[str, ...], *, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


# --------------------------------------------------------------------------
# Platforms
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Platform:
    name: str
    executable: str
    hosts: tuple[str, ...]
    verbs: frozenset[str]
    credential_env: tuple[str, ...] = ()
    # Human-facing, shown when the tool is missing or unauthenticated.
    setup_hint: str = ""

    def can_handle(self, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return False
        return any(host == h or host.endswith("." + h) for h in self.hosts)


REDDIT = Platform(
    name="reddit",
    executable="rdt",
    hosts=("reddit.com", "redd.it"),
    # Read verbs only. See the module docstring: adding a write verb here is
    # the single change that would turn this feature into an account risk.
    verbs=frozenset({"search", "read", "subreddit", "popular"}),
    credential_env=(),
    setup_hint=(
        "Reddit okuması için rdt-cli kurulu ve oturum çerezi geçerli olmalı. "
        "Kurulum ve çerez yenileme: docs/social-reach.md"
    ),
)

PLATFORMS: dict[str, Platform] = {REDDIT.name: REDDIT}


def platform_for_url(url: str) -> Optional[Platform]:
    """Which platform owns this URL, if any.

    Used to route a URL that turned up in an ordinary web search to the
    social reader instead of Crawl4AI -- reddit.com blocks the crawler, so
    today those results are dead ends.
    """
    for platform in PLATFORMS.values():
        if platform.can_handle(url):
            return platform
    return None


# --------------------------------------------------------------------------
# Command building
# --------------------------------------------------------------------------


def build(platform: str, verb: str, /, **params: Any) -> Command:
    """Validate ``params`` and return the argv for one read.

    Raises ``ArgumentError`` for anything unknown or malformed, which the
    controller turns into "this could not be looked up" rather than a failed
    job -- an invalid subreddit name is a bad guess by the model, not a
    crash.
    """
    known = PLATFORMS.get(str(platform or "").strip().lower())
    if known is None:
        raise ArgumentError(f"bilinmeyen platform: {platform!r}")
    name = str(verb or "").strip().lower()
    if name not in known.verbs:
        raise ArgumentError(f"{known.name} için izin verilmeyen işlem: {verb!r}")
    if known.name == "reddit":
        return _build_reddit(known, name, params)
    raise ArgumentError(f"{known.name} için komut üreteci yok")


def _build_reddit(platform: Platform, verb: str, params: dict[str, Any]) -> Command:
    argv: list[str] = [platform.executable]
    # Positional values go after a "--" separator, so options are finished
    # before the attacker-influenced part of the line begins. Verified
    # against rdt-cli: a query of "-o pwned.json" IS otherwise consumed as
    # the --output flag (Click then reports the query missing), which would
    # let a search write a file. _query already strips leading dashes; this
    # is the second lock on the same door. Note the ordering rdt requires --
    # every flag must precede the separator.
    positional: list[str] = []

    if verb == "search":
        argv += ["search"]
        positional = [_query(params.get("query"))]
        argv += ["--limit", str(_limit(params.get("limit")))]
        sort = _choice(params.get("sort"), _SEARCH_SORTS, default="relevance")
        if sort != "relevance":
            argv += ["-s", sort]
        window = _choice(params.get("time"), _TIMES, default="all")
        if window != "all":
            argv += ["-t", window]
    elif verb == "read":
        argv += ["read"]
        positional = [_post_id(params.get("post_id"))]
        # Comment trees are where the injection payloads live and where the
        # token cost explodes; the default stays shallow and the controller
        # has to ask for more.
        if params.get("expand_comments"):
            argv += ["--expand-more"]
        argv += ["--limit", str(_limit(params.get("comment_limit")))]
    elif verb == "subreddit":
        argv += ["sub"]
        positional = [_subreddit(params.get("subreddit"))]
        argv += ["--limit", str(_limit(params.get("limit")))]
        sort = _choice(params.get("sort"), _SUB_SORTS, default="hot")
        if sort != "hot":
            argv += ["-s", sort]
        window = _choice(params.get("time"), _TIMES, default="all")
        if window != "all" and sort in ("top", "controversial"):
            argv += ["-t", window]
    elif verb == "popular":
        argv += ["popular", "--limit", str(_limit(params.get("limit")))]
    else:  # pragma: no cover - build() already gated the verb
        raise ArgumentError(f"reddit için komut üreteci yok: {verb!r}")

    # Structured output for every call: the parser should never have to read
    # a human-formatted table, and rdt-cli switches to YAML on a non-TTY
    # stdout, which would make behaviour differ between a terminal and this
    # backend (confirmed: `rdt status` with no --json prints YAML).
    argv += ["--json"]
    if positional:
        argv += ["--", *positional]

    timeout = params.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    return Command(
        platform=platform.name,
        verb=verb,
        argv=tuple(argv),
        timeout_seconds=max(5, min(int(timeout), MAX_TIMEOUT_SECONDS)),
        credential_env=platform.credential_env,
    )


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def _shorten(text: str, limit: int) -> str:
    """Cut at a word boundary where one is near, and say that it was cut."""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    cut = value[:limit]
    space = cut.rfind(" ")
    if space > limit - 120:
        cut = cut[:space]
    return cut.rstrip() + " […]"


@dataclass(frozen=True)
class SocialRecord:
    """One post or comment, normalized across platforms.

    ``to_quoted_item`` is the only intended way to put one of these in front
    of a model: it routes every attacker-controlled field -- body, title,
    author, subreddit -- through ``untrusted_content``. Formatting a record
    by hand somewhere else is how the fence gets skipped, so nothing else
    here returns display text.
    """

    platform: str
    kind: str  # "post" | "comment"
    identifier: str = ""
    title: str = ""
    author: str = ""
    url: str = ""
    score: Optional[int] = None
    created: str = ""
    channel: str = ""  # subreddit, and whatever the equivalent is elsewhere
    body: str = ""
    comments: tuple["SocialRecord", ...] = field(default_factory=tuple)

    def to_quoted_item(self, *, body_chars: int = 1500, comment_chars: int = 500) -> Any:
        """Render as one quoted item, giving comments their own room.

        The two budgets are separate for a reason found by reading a live
        block: the post body and the comments used to be concatenated and
        then truncated as one string at ``MAX_ITEM_CHARS``. A 5,944-character
        post -- ordinary on Reddit -- therefore consumed the whole allowance
        and every comment was cut, while the summary line above still
        announced "16 comments quoted". The run had paid for those reads and
        told the model they were there.

        Comments are usually where the answer is (the post asks, the replies
        answer), so the post's share is the one that gets capped first.
        """
        from untrusted_content import QuotedItem

        meta: dict[str, str] = {}
        if self.title:
            meta["Başlık"] = self.title
        if self.channel:
            meta["Kanal"] = self.channel
        if self.author:
            meta["Yazar"] = self.author
        if self.created:
            meta["Tarih"] = self.created
        if self.score is not None:
            meta["Puan"] = str(self.score)
        if self.url:
            meta["Bağlantı"] = self.url
        body = _shorten(self.body, body_chars)
        if self.comments:
            body += "\n\n" + "\n\n".join(
                f"[yorum] {c.author or 'anonim'}: {_shorten(c.body, comment_chars)}"
                for c in self.comments
            )
        return QuotedItem(body=body, meta=meta)

    def source_line(self) -> str:
        """A 'Kaynaklar' entry, so a report cites what it actually read."""
        label = self.title or self.identifier or self.url
        where = f"{self.platform}/{self.channel}" if self.channel else self.platform
        return f"- {label} ({where}) {self.url}".rstrip()
