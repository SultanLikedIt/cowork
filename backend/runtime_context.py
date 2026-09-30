"""The RUN CONTEXT block: what is true *right now*, for every model-facing call.

The failure this exists for
---------------------------
A user asked, on 14 August 2026, for the 13 August 2026 exchange rates as an
Excel file. The run had WEB_FETCH and ARTIFACTS bound. The answer was three
false statements in a row: that 13 August 2026 "is still in the future", that
it could not access websites, and that it could not create Excel files.

None of those are model-quality problems that a better prompt elsewhere would
have fixed, because none of them were knowable to the model:

* **The date.** A model has no clock. Left ungrounded it falls back to its
  training cutoff, so a date in the recent past reads as the future -- and
  once it "knows" the data cannot exist yet, refusing to look is the
  internally consistent thing to do. Every downstream tool being bound and
  working could not have helped.
* **The capabilities.** Per-capability guidance already told it *how* to use
  each tool it had, but nothing ever stated the negative: that a blanket
  "I cannot access websites" is false in this run. Generic assistant
  disclaimers are extremely well represented in pretraining, so they surface
  under any uncertainty unless something explicitly contradicts them.

Both are properties of the *run*, not of the request or the persona, so they
belong in one block assembled once and attached to every model-facing call --
rather than sentences bolted onto each prompt, which is how paths drift apart
and how a continuation prompt silently loses grounding the first call had.

Deliberately compact: this is prepended to every call, so its cost is paid on
every turn of every job.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import capabilities as _capabilities

# One short clause per capability, phrased as what the executor may DO rather
# than as a tool name -- the disclaimers being suppressed are about abilities
# ("I can't browse the web"), so the contradiction has to be stated in the
# same terms to land. Tool-level how-to stays in capabilities.REGISTRY's
# guidance; this is only the "you have this" half.
_ABILITY = {
    _capabilities.WEB_SEARCH: "search the web for pages and current facts",
    _capabilities.WEB_FETCH: (
        "open and read specific web pages, including their tables"
    ),
    _capabilities.FILES: "read the files the user uploaded",
    _capabilities.CODE_EXEC: "run Python in this job's sandboxed workspace",
    _capabilities.ARTIFACTS: (
        "create real downloadable files (xlsx, csv, json, md, html, docx, "
        "pptx) that the user receives"
    ),
    # Gather-only sources: another step of this run already read them and
    # handed the result over as notes. Without these, a Reddit-only job was
    # told "Tools available: NONE ... say so plainly", and its answer opened
    # with "I have no tool to search Reddit; a previous worker's notes
    # say..." above the correct result.
    _capabilities.MAIL_READ: "use the e-mails already read from the user's mailbox",
    _capabilities.SOCIAL_READ: "use the Reddit posts and comments already read",
    _capabilities.JIRA_READ: "use the Jira issues already read",
    _capabilities.CONFLUENCE_READ: "use the Confluence pages already read",
    _capabilities.GITHUB_READ: "use the GitHub content already read",
    _capabilities.DOCUMENTS_READ: "use the passages already found in the workspace documents",
}
_ALREADY_READ = frozenset(
    {
        _capabilities.MAIL_READ,
        _capabilities.SOCIAL_READ,
        _capabilities.JIRA_READ,
        _capabilities.CONFLUENCE_READ,
        _capabilities.GITHUB_READ,
        _capabilities.DOCUMENTS_READ,
    }
)

# Order the abilities are listed in, so the block is byte-stable for a given
# capability set (it is compared in tests and cached in prompts).
_ABILITY_ORDER = (
    _capabilities.WEB_SEARCH,
    _capabilities.WEB_FETCH,
    _capabilities.FILES,
    _capabilities.CODE_EXEC,
    _capabilities.ARTIFACTS,
    _capabilities.MAIL_READ,
    _capabilities.SOCIAL_READ,
    _capabilities.JIRA_READ,
    _capabilities.CONFLUENCE_READ,
    _capabilities.GITHUB_READ,
    _capabilities.DOCUMENTS_READ,
)


def now_local() -> datetime:
    """The application's real current time, timezone-aware.

    Honours TZ/AGENT_TIMEZONE when set (a container usually runs in UTC while
    its users do not), otherwise the host's local zone. Never hardcoded, and
    never naive -- a naive timestamp printed without an offset is how "is
    this date past or future" goes wrong by a day at the boundary.
    """
    name = os.environ.get("AGENT_TIMEZONE") or os.environ.get("TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(name))
        except Exception:  # noqa: BLE001 - a bad TZ must never fail a job
            pass
    return datetime.now().astimezone()


def _date_lines(now: datetime) -> list[str]:
    today = now.date()
    return [
        f"Current date and time: {now:%A, %d %B %Y, %H:%M} (UTC{now:%z}).",
        (
            f"today = {today.isoformat()}, "
            f"yesterday = {(today - timedelta(days=1)).isoformat()}, "
            f"tomorrow = {(today + timedelta(days=1)).isoformat()}."
        ),
        (
            f"Any date on or before {today.isoformat()} is in the PAST and "
            "its data may already exist and be published; only dates after "
            "it are in the future. Resolve every relative expression "
            "(\"today\", \"last week\", \"this month\") against the date "
            "above, never against your training data -- your own sense of "
            "the current date is not reliable and does not apply here."
        ),
    ]


def _ability_lines(resolved: frozenset) -> list[str]:
    available = [_ABILITY[name] for name in _ABILITY_ORDER if name in resolved]
    if not available:
        return [
            "Tools available in this run: NONE. You cannot browse, search, "
            "read uploads, run code, or create files. Say so plainly instead "
            "of implying otherwise."
        ]
    lines = [
        "You CAN, in this run: " + "; ".join(available) + ".",
        # The anti-disclaimer rule. Stated as a prohibition on a specific
        # sentence pattern, because that pattern is exactly what appeared.
        "Do NOT claim you cannot access the internet, cannot create or send "
        "files, or cannot run code when the matching ability is listed "
        "above -- those statements are false here. Use the tool instead. If "
        "a specific call fails, report that specific failure and what you "
        "tried; never generalise it into a claim about what you are able to "
        "do.",
    ]
    if resolved & _ALREADY_READ:
        lines.append(
            "What was already read is in the notes you were given: answer "
            "from it directly. Do not say you lack a tool to read it, and do "
            "not describe it as another worker's notes."
        )
    return lines


# --- Language -------------------------------------------------------------
#
# Deterministic and tiny: no model call, and no language-detection dependency
# for what is a one-bit decision in practice. Stopword hits are weighted far
# above diacritics because a single Turkish character can appear in an
# otherwise English sentence (and in proper nouns), whereas function words
# are what actually carry the language of a request.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

_TURKISH_WORDS = frozenset(
    """ve bir bu şu için ile bana benim sen siz olarak ver verir ver_bana al
    getir yap yapar oluştur indir kur kurları güncel tarih tarihinin dosya
    dosyayı dosyasını sitesinden sitesine nedir mısın misin musun mu mü
    lütfen sonra önce günün bugün dün yarın hangi kaç çok daha gibi ama
    veya ise değil var yok üzerinden hakkında sayfası söyle soyle tekrar
    cevap türkçe turkce türkcesini""".split()
)
_ENGLISH_WORDS = frozenset(
    """the and is are was were of to a an for me my you your it in on at from
    with please give get make create download file files current date today
    yesterday tomorrow what which how many can could would should this that
    site page website data""".split()
)
_TURKISH_CHARS = frozenset("çğıöşüÇĞİÖŞÜ")


def dominant_language(text: str) -> Optional[str]:
    """The language to answer in, or None when it is not clear enough to name.

    None is a real, useful outcome: the caller then falls back to "match the
    request's language", which is correct for every language this function
    does not explicitly know. Naming a language wrongly is worse than not
    naming one.
    """
    words = [word.lower() for word in _WORD_RE.findall(text or "")]
    if not words:
        return None
    turkish = sum(1 for word in words if word in _TURKISH_WORDS)
    english = sum(1 for word in words if word in _ENGLISH_WORDS)
    # Diacritics break ties only -- see above.
    if any(char in _TURKISH_CHARS for char in (text or "")):
        turkish += 0.5
    if turkish > english:
        return "Turkish"
    if english > turkish:
        return "English"
    return None


def _language_line(request_text: str) -> str:
    language = dominant_language(request_text)
    target = language or "the same language the user wrote in"
    return (
        f"Write everything the user reads in {target}. The English system "
        "prompt is not a reason to answer in English, and you are not "
        "restricted to English. If the latest user message is Turkish, "
        "answer in Turkish -- do not refuse and do not say you are only "
        "allowed to write English. Keep code, tool names, filenames, API "
        "identifiers and established technical terms as they are -- do not "
        "translate those."
    )


def runtime_block(
    job_capabilities: Any = None,
    request_text: str = "",
    now: Optional[datetime] = None,
) -> str:
    """The RUN CONTEXT block to append to a system prompt.

    `now` is injectable so tests can pin a date without touching the clock;
    production never passes it.
    """
    moment = now or now_local()
    resolved = _capabilities.expand(job_capabilities or frozenset())
    lines = (
        _date_lines(moment)
        + _ability_lines(resolved)
        + [_language_line(request_text)]
    )
    return (
        "\n\nRUN CONTEXT -- authoritative for this run; it overrides any "
        "assumption you have about the date or about what you are able to "
        "do:\n" + "\n".join(f"- {line}" for line in lines)
    )


__all__ = ["dominant_language", "now_local", "runtime_block"]
