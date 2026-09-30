"""Upstream JSON to ``SocialRecord``, and nothing else.

Two shapes, because rdt-cli has two
-----------------------------------
Captured from rdt-cli 0.4.2 against live Reddit, 2026-09-21:

* ``search`` / ``sub`` / ``popular`` return Reddit's **raw API Listing**
  unmodified -- ``{"kind": "Listing", "data": {"children": [{"kind": "t3",
  "data": {...105 fields...}}], "after": ...}}``.
* ``read`` returns rdt's **own normalized** structure --
  ``{"post": {...15 fields...}, "comments": [...], "more_count": 256,
  "more_children": [...]}`` -- where each comment carries nested ``replies``.

Projecting both onto one record type is most of why this module exists. The
other reason is the 105 fields: passing those through would put an author's
flair colours, award metadata and moderation state into the model's context,
which costs tokens on every single post and tells the model nothing it was
asked about. Only the fields a reader would care about survive.

This module does no cleaning and no fencing. Everything it returns is
attacker-controlled text, and it stays that way until
``SocialRecord.to_quoted_item`` hands it to ``untrusted_content``.
"""

from __future__ import annotations

import html as html_module
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .registry import SocialRecord

# Reddit permalinks are site-relative ("/r/x/comments/...").
_BASE = "https://www.reddit.com"

# Comment placeholders. rdt leaves an entry whose author is "[more]" where a
# collapsed thread would be; quoting one as if it were a comment would put
# "[more]" in front of the model as a person's opinion.
_PLACEHOLDER_AUTHORS = {"[more]", "[deleted]", "[removed]"}

# How deep a comment tree is walked. Depth is where both the token cost and
# the injection surface live: the top-level replies to a popular post are
# read by thousands of people, while a reply five levels down is a private
# argument between two accounts -- a good place to park a payload.
MAX_COMMENT_DEPTH = 2
MAX_COMMENTS_PER_POST = 12


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _iso(created_utc: Any) -> str:
    """Reddit's float epoch to a readable UTC stamp.

    A raw 1789298287.0 in an evidence block is noise the model has to guess
    at; it routinely guesses wrong and writes a wrong date into the report.
    """
    try:
        stamp = float(created_utc)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return (
            datetime.fromtimestamp(stamp, tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC")
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _url_for(post: dict[str, Any]) -> str:
    """The canonical thread URL.

    ``permalink`` is preferred over ``url`` because for a link post ``url``
    points at the linked article, not at the Reddit discussion -- and the
    discussion is what was read, so the discussion is what a citation must
    point to.
    """
    permalink = _text(post.get("permalink"))
    if permalink:
        return _BASE + permalink if permalink.startswith("/") else permalink
    return _text(post.get("url"))


def _score(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _channel(post: dict[str, Any]) -> str:
    prefixed = _text(post.get("subreddit_name_prefixed"))
    if prefixed:
        return prefixed
    name = _text(post.get("subreddit"))
    return f"r/{name}" if name else ""


def _post_record(post: dict[str, Any], *, comments: tuple[SocialRecord, ...] = ()) -> SocialRecord:
    return SocialRecord(
        platform="reddit",
        kind="post",
        identifier=_text(post.get("id")),
        title=_text(post.get("title")),
        author=_author(post.get("author")),
        url=_url_for(post),
        score=_score(post.get("score")),
        created=_iso(post.get("created_utc")),
        channel=_channel(post),
        body=_text(post.get("selftext")),
        comment_count=_score(post.get("num_comments")),
        comments=comments,
    )


def _author(value: Any) -> str:
    name = _text(value).strip()
    if not name or name in _PLACEHOLDER_AUTHORS:
        return name or ""
    return name if name.startswith("u/") else f"u/{name}"


def parse_listing(data: Any, *, limit: int = 25) -> list[SocialRecord]:
    """``search`` / ``sub`` / ``popular`` -> records, newest field wins.

    Tolerant of the envelope having been unwrapped once or not at all: the
    runner returns ``payload["data"]``, but a caller holding the whole
    response should not get an empty list for it.
    """
    listing = data
    if isinstance(listing, dict) and "data" in listing and "kind" not in listing:
        listing = listing.get("data")
    if isinstance(listing, dict) and listing.get("kind") == "Listing":
        listing = listing.get("data") or {}
    children = listing.get("children") if isinstance(listing, dict) else None
    if not isinstance(children, list):
        return []

    records: list[SocialRecord] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        # t3 is a post. A Listing can also carry t1 (comment) entries, which
        # a search never returns but /r/all style endpoints can.
        if child.get("kind") not in (None, "t3"):
            continue
        post = child.get("data")
        if not isinstance(post, dict) or not post.get("id"):
            continue
        records.append(_post_record(post))
        if len(records) >= limit:
            break
    return records


def parse_post(data: Any, *, max_comments: int = MAX_COMMENTS_PER_POST) -> Optional[SocialRecord]:
    """``read`` -> one post with a bounded slice of its comment tree."""
    if not isinstance(data, dict):
        return None
    post = data.get("post")
    if not isinstance(post, dict) or not post.get("id"):
        return None
    comments = _walk_comments(
        data.get("comments"), depth=0, budget=[max_comments]
    )
    return _post_record(post, comments=tuple(comments))


def _walk_comments(
    raw: Any, *, depth: int, budget: list[int], parent: str = ""
) -> list[SocialRecord]:
    """Flatten the reply tree, depth-first, until the budget runs out.

    Flattened rather than nested because the evidence block is text: a nested
    structure would have to be rendered as indentation the model then has to
    interpret, while "reply to X" as a field says the same thing plainly.
    """
    if not isinstance(raw, list) or depth > MAX_COMMENT_DEPTH or budget[0] <= 0:
        return []
    out: list[SocialRecord] = []
    for item in raw:
        if budget[0] <= 0:
            break
        if not isinstance(item, dict):
            continue
        author = _author(item.get("author"))
        body = _text(item.get("body"))
        # "[more]" placeholders carry no text; quoting one as a comment would
        # put a UI artifact in front of the model as somebody's opinion.
        if author in _PLACEHOLDER_AUTHORS and not body.strip():
            continue
        budget[0] -= 1
        out.append(
            SocialRecord(
                platform="reddit",
                kind="comment",
                identifier=_text(item.get("id")),
                author=author,
                score=_score(item.get("score")),
                created=_iso(item.get("created_utc")),
                body=body,
                title=f"↳ {parent}" if parent else "",
            )
        )
        out.extend(
            _walk_comments(
                item.get("replies"),
                depth=depth + 1,
                budget=budget,
                parent=author or "üst yorum",
            )
        )
    return out


def parse(
    verb: str, data: Any, *, limit: int = 25, platform: str = "reddit"
) -> list[SocialRecord]:
    """Dispatch on the platform and the verb that produced ``data``."""
    if platform == "hackernews":
        if verb == "read":
            record = parse_hn_item(data)
            return [record] if record else []
        return parse_hn_search(data, limit=limit)
    if verb == "read":
        record = parse_post(data)
        return [record] if record else []
    return parse_listing(data, limit=limit)


# --------------------------------------------------------------------------
# HackerNews (Algolia API)
# --------------------------------------------------------------------------

_HN_THREAD = "https://news.ycombinator.com/item?id="

# HN comment bodies are HTML, not markdown -- verified on a live thread where
# 30 of 98 comments carried markup, with entities double-escaped
# ("&#x27;", "&#x2F;"). Handing that to a model wastes tokens on angle
# brackets and reads badly in a quoted block.
_HN_PARAGRAPH_RE = re.compile(r"</?p\s*/?>", re.IGNORECASE)
_HN_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HN_TAG_RE = re.compile(r"<[^>]{0,400}?>")


def hn_text(raw: Any) -> str:
    """HN's HTML comment body as plain text.

    Deliberately small rather than a real parser: these are short comment
    bodies with a handful of tags, and html_extract's trafilatura path is
    built for whole pages. Link text is kept and the href dropped -- a URL
    inside a stranger's comment is not something to put in front of a model
    as if it were a citation (extract_urls exists for reporting them).
    """
    text = str(raw or "")
    if not text:
        return ""
    text = _HN_PARAGRAPH_RE.sub("\n\n", text)
    text = _HN_BREAK_RE.sub("\n", text)
    text = _HN_TAG_RE.sub("", text)
    # Twice: HN escapes entities inside already-escaped text, so one pass
    # leaves "&#x27;" visible as "&#x27;" in perfectly ordinary comments.
    text = html_module.unescape(html_module.unescape(text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _hn_when(item: dict[str, Any]) -> str:
    """HN gives both an epoch int and an ISO string; prefer the int."""
    if "created_at_i" in item:
        return _iso(item.get("created_at_i"))
    raw = str(item.get("created_at") or "")
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _hn_record(item: dict[str, Any], *, comments: tuple[SocialRecord, ...] = ()) -> SocialRecord:
    identifier = str(item.get("objectID") or item.get("id") or "")
    return SocialRecord(
        platform="hackernews",
        kind="post",
        identifier=identifier,
        title=_text(item.get("title")),
        author=_text(item.get("author")),
        # The discussion, not the linked article -- same rule as Reddit's
        # permalink: the thread is what was read, so the thread is what a
        # citation must point at.
        url=f"{_HN_THREAD}{identifier}" if identifier else _text(item.get("url")),
        score=_score(item.get("points")),
        created=_hn_when(item),
        channel="Hacker News",
        body=hn_text(item.get("story_text") or item.get("text")),
        comment_count=_score(item.get("num_comments")),
        comments=comments,
    )


def parse_hn_search(data: Any, *, limit: int = 25) -> list[SocialRecord]:
    """``search`` / ``top`` -> records. Algolia wraps results in ``hits``."""
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return []
    records: list[SocialRecord] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        if not (hit.get("objectID") or hit.get("id")):
            continue
        records.append(_hn_record(hit))
        if len(records) >= limit:
            break
    return records


def parse_hn_item(data: Any, *, max_comments: int = MAX_COMMENTS_PER_POST) -> Optional[SocialRecord]:
    """``read`` -> one story with a bounded slice of its comment tree."""
    if not isinstance(data, dict) or not (data.get("id") or data.get("objectID")):
        return None
    comments = _walk_hn(data.get("children"), depth=0, budget=[max_comments])
    return _hn_record(data, comments=tuple(comments))


def _walk_hn(
    raw: Any, *, depth: int, budget: list[int], parent: str = ""
) -> list[SocialRecord]:
    """Flatten HN's recursive children, same shape as the Reddit walker."""
    if not isinstance(raw, list) or depth > MAX_COMMENT_DEPTH or budget[0] <= 0:
        return []
    out: list[SocialRecord] = []
    for item in raw:
        if budget[0] <= 0:
            break
        if not isinstance(item, dict) or item.get("type") == "story":
            continue
        body = hn_text(item.get("text"))
        author = _text(item.get("author"))
        # A deleted comment arrives with both null; quoting it would put an
        # empty attributed block in front of the model.
        if not body and not author:
            continue
        budget[0] -= 1
        out.append(
            SocialRecord(
                platform="hackernews",
                kind="comment",
                identifier=str(item.get("id") or ""),
                author=author or "(silinmiş)",
                score=_score(item.get("points")),
                created=_hn_when(item),
                body=body,
                title=f"↳ {parent}" if parent else "",
            )
        )
        out.extend(
            _walk_hn(
                item.get("children"),
                depth=depth + 1,
                budget=budget,
                parent=author or "üst yorum",
            )
        )
    return out
