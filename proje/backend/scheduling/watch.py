"""Watch tasks: report only when something changed.

Two strategies, configured per task in ``watch_json``:

``url_hash``  Before the run, open each watched URL with the same Crawl4AI
              engine web research uses and hash its extracted main text.
              Unchanged hashes skip the job entirely -- no model call, no
              research. A changed (or first-seen) page runs the task.
``research_diff``  Run the task, then one small model call compares the new
              answer with the previous run's summary against the task's
              criteria and decides whether anything material changed.

Both can be combined: url_hash gates whether the job runs, research_diff
judges what it found.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("agent_webapp.scheduling")

STRATEGIES = ("url_hash", "research_diff")
MAX_WATCH_URLS = 5


class WatchConfig(BaseModel):
    strategies: list[str] = Field(default_factory=lambda: ["research_diff"])
    urls: list[str] = Field(default_factory=list, max_length=MAX_WATCH_URLS)
    criteria: str = ""


class DiffResult(BaseModel):
    material_change: bool = False
    summary: str = ""


def normalize_config(raw: Optional[dict[str, Any]]) -> Optional[WatchConfig]:
    if not raw:
        return None
    config = WatchConfig.model_validate(raw)
    strategies = [s for s in dict.fromkeys(config.strategies) if s in STRATEGIES]
    if not strategies:
        raise ValueError("watch needs at least one strategy: url_hash or research_diff")
    urls = [u.strip() for u in config.urls if u and u.strip()]
    if "url_hash" in strategies and not urls:
        raise ValueError("url_hash watch needs at least one URL")
    return WatchConfig(strategies=strategies, urls=urls, criteria=config.criteria.strip())


def content_hash(text: str) -> str:
    normalized = " ".join(str(text or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fetch_page_text(url: str) -> str:
    """Main text of one page through the shared Crawl4AI engine (SSRF-checked)."""
    from crawl4ai_engine import get_crawl4ai_engine

    acquisition = get_crawl4ai_engine().acquire(url, "")
    page = acquisition.pages[0]
    text = page.text or ""
    if not text.strip() and page.tables:
        text = "\n".join(str(table) for table in page.tables)
    return text


@dataclass
class HashCheck:
    changed: bool
    baseline: bool
    hashes: dict[str, str]
    errors: dict[str, str]


async def check_url_hashes(
    urls: list[str],
    previous: dict[str, str],
    *,
    fetch: Callable[[str], str] = fetch_page_text,
) -> HashCheck:
    async def one(url: str) -> tuple[str, Optional[str], Optional[str]]:
        try:
            text = await asyncio.to_thread(fetch, url)
        except Exception as exc:  # noqa: BLE001 - one bad URL must not fail the check
            return url, None, f"{type(exc).__name__}: {exc}"
        if not text.strip():
            return url, None, "page had no extractable text"
        return url, content_hash(text), None

    results = await asyncio.gather(*(one(url) for url in urls))
    # Only watched URLs are kept, so removing a URL from the task drops its hash.
    hashes = {url: digest for url, digest in previous.items() if url in urls}
    errors: dict[str, str] = {}
    changed = False
    compared = False
    for url, digest, error in results:
        if error is not None:
            errors[url] = error
            continue
        if url in previous:
            compared = True
            if previous[url] != digest:
                changed = True
        # A URL never hashed before has nothing to compare against: it is
        # recorded now as its own baseline, not reported as a change.
        hashes[url] = digest
    return HashCheck(changed=changed, baseline=not compared, hashes=hashes, errors=errors)


DIFF_SYSTEM = (
    "You compare two reports produced by the same recurring research task and "
    "decide whether the NEW one contains a material change compared with the "
    "PREVIOUS one. Respond with JSON only: "
    '{"material_change": true|false, "summary": "..."}.\n'
    "material_change is true only for new facts that matter under the WATCH "
    "CRITERIA: a new regulation, a changed figure, a new announcement, a "
    "reversed decision. Rewording, reordering, different sources for the same "
    "facts, or a newer date on unchanged information is NOT a material change. "
    "summary: when true, 1-4 short bullet lines in the report's language naming "
    "exactly what changed; when false, one short sentence saying nothing "
    "material changed."
)

CompleteFn = Callable[..., Awaitable[Any]]


async def diff_results(
    previous_summary: str,
    new_result: str,
    criteria: str,
    *,
    complete: Optional[CompleteFn] = None,
) -> Optional[DiffResult]:
    """None when there is nothing to compare against (first run) or the call failed."""
    if not str(previous_summary or "").strip() or not str(new_result or "").strip():
        return None
    if complete is None:
        from execution.langgraph_runtime.openai_chat import complete_chat as complete
    user = (
        f"WATCH CRITERIA:\n{criteria or '(none given: any material change in the topic)'}\n\n"
        f"PREVIOUS REPORT SUMMARY:\n{previous_summary[:6000]}\n\n"
        f"NEW REPORT:\n{new_result[:12000]}"
    )
    try:
        parsed = await complete(
            [
                {"role": "system", "content": DIFF_SYSTEM},
                {"role": "user", "content": user},
            ],
            stage="watch_diff",
            max_tokens=2048,
            temperature=0,
            response_format=DiffResult,
        )
    except Exception:  # noqa: BLE001 - a failed diff reports the run instead of hiding it
        logger.warning("watch diff call failed", exc_info=True)
        return None
    if isinstance(parsed, DiffResult):
        return parsed
    if isinstance(parsed, dict):
        try:
            return DiffResult.model_validate(parsed)
        except Exception:  # noqa: BLE001
            return None
    return None
