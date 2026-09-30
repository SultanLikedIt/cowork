"""Tests for runtime grounding: the date, the abilities, and the language.

Pins the live failure this exists for. On 14 August 2026 a user asked for the
13 August 2026 exchange rates as an Excel file; the run had WEB_FETCH and
ARTIFACTS bound, and the answer claimed (1) that 13 August 2026 was still in
the future, (2) that it could not access websites, and (3) that it could not
create Excel files. All three are properties of the RUN that the model had no
way to know, so all three are asserted here against the block that now tells
it.

Fully deterministic: `now` is injected, so nothing depends on the wall clock.

No pytest suite exists in this repo (see test_upload.py) -- run directly:
    python test_runtime_context.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="runtime_ctx_test_"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, ValueError):
    pass

import capabilities  # noqa: E402
import runtime_context  # noqa: E402
from capabilities import (  # noqa: E402
    ARTIFACTS,
    CODE_EXEC,
    FILES,
    WEB_FETCH,
    WEB_SEARCH,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


# The exact runtime of the live failure: 14 Aug 2026, UTC+03:00.
LIVE = datetime(2026, 8, 14, 9, 30, tzinfo=timezone(timedelta(hours=3)))
LIVE_REQUEST = (
    "tcmb'nin sitesinden 13 ağustos 2026 tarihinin kurlarını al ve bana "
    "excel olarak ver"
)


# ---------------------------------------------------------------------------
# Date grounding
# ---------------------------------------------------------------------------


def test_requested_past_date_is_grounded_as_past() -> None:
    block = runtime_context.runtime_block(
        {WEB_FETCH, ARTIFACTS}, request_text=LIVE_REQUEST, now=LIVE
    )
    check(
        "the real current date is stated",
        "2026-08-14" in block and "14 August 2026" in block,
        block[:200],
    )
    check(
        "the requested date (2026-08-13) is identifiable as yesterday",
        "yesterday = 2026-08-13" in block,
        block[:400],
    )
    check(
        "the block states that dates up to today are in the PAST",
        "on or before 2026-08-14 is in the PAST" in block,
        block[:500],
    )
    check(
        "tomorrow (2026-08-15) is named as the first future date",
        "tomorrow = 2026-08-15" in block,
        block[:400],
    )
    check(
        "the model is told not to trust its own sense of the date",
        "not reliable" in block and "training data" in block,
        block,
    )
    check(
        "the timezone offset is included, so date boundaries are unambiguous",
        "UTC+0300" in block,
        block[:200],
    )


def test_today_yesterday_tomorrow_track_the_injected_clock() -> None:
    """Grounding must follow the real clock, not a constant."""
    for moment, today, yesterday, tomorrow in (
        (LIVE, "2026-08-14", "2026-08-13", "2026-08-15"),
        # Month boundary -- the case a naive +/-1 on the day number breaks.
        (
            datetime(2027, 3, 1, 0, 5, tzinfo=timezone.utc),
            "2027-03-01",
            "2027-02-28",
            "2027-03-02",
        ),
        # Leap day.
        (
            datetime(2028, 2, 29, 23, 55, tzinfo=timezone.utc),
            "2028-02-29",
            "2028-02-28",
            "2028-03-01",
        ),
    ):
        block = runtime_context.runtime_block(frozenset(), now=moment)
        check(
            f"date grounding is correct at {today}",
            f"today = {today}" in block
            and f"yesterday = {yesterday}" in block
            and f"tomorrow = {tomorrow}" in block,
            block[:300],
        )


def test_relative_expressions_are_anchored() -> None:
    block = runtime_context.runtime_block(frozenset(), now=LIVE)
    check(
        "relative phrases are explicitly anchored to the stated date",
        "last week" in block and "Resolve every relative expression" in block,
        block[:600],
    )


def test_now_local_is_timezone_aware_and_not_hardcoded() -> None:
    first = runtime_context.now_local()
    check(
        "now_local() returns a timezone-aware datetime",
        first.tzinfo is not None and first.utcoffset() is not None,
        repr(first),
    )
    check(
        "now_local() reflects the real clock, not a constant",
        abs((datetime.now(timezone.utc) - first).total_seconds()) < 120,
        repr(first),
    )

    # An explicit application timezone is honoured when configured.
    previous = os.environ.get("AGENT_TIMEZONE")
    os.environ["AGENT_TIMEZONE"] = "Europe/Istanbul"
    try:
        istanbul = runtime_context.now_local()
        check(
            "AGENT_TIMEZONE is honoured",
            istanbul.utcoffset() == timedelta(hours=3),
            str(istanbul.utcoffset()),
        )
        # A nonsense zone must degrade, never raise.
        os.environ["AGENT_TIMEZONE"] = "Not/AZone"
        check(
            "an invalid timezone falls back instead of failing the job",
            runtime_context.now_local().tzinfo is not None,
            "raised",
        )
    finally:
        if previous is None:
            os.environ.pop("AGENT_TIMEZONE", None)
        else:
            os.environ["AGENT_TIMEZONE"] = previous


# ---------------------------------------------------------------------------
# Capability awareness
# ---------------------------------------------------------------------------


def test_bound_capabilities_are_stated_as_abilities() -> None:
    block = runtime_context.runtime_block(
        {WEB_FETCH, ARTIFACTS}, request_text=LIVE_REQUEST, now=LIVE
    )
    check(
        "WEB_FETCH bound -> web access is stated as available",
        "open and read specific web pages" in block,
        block,
    )
    check(
        "ARTIFACTS bound -> file creation is stated as available",
        "create real downloadable files" in block,
        block,
    )
    check(
        "the anti-disclaimer rule is present when abilities exist",
        "Do NOT claim you cannot access the internet" in block,
        block,
    )
    check(
        "a specific failure is still required to be reported honestly",
        "report that specific failure" in block,
        block,
    )


def test_absent_capabilities_are_never_claimed() -> None:
    """The other half: over-claiming is as wrong as under-claiming."""
    block = runtime_context.runtime_block({ARTIFACTS}, now=LIVE)
    check(
        "WEB_FETCH absent -> no web-access claim",
        "open and read specific web pages" not in block
        and "search the web" not in block,
        block,
    )
    check(
        "ARTIFACTS present alongside it is still stated",
        "create real downloadable files" in block,
        block,
    )

    block = runtime_context.runtime_block({WEB_FETCH}, now=LIVE)
    check(
        "ARTIFACTS absent -> no file-creation claim",
        "create real downloadable files" not in block,
        block,
    )
    check(
        "CODE_EXEC absent -> no code-execution claim",
        "run Python" not in block,
        block,
    )

    block = runtime_context.runtime_block({CODE_EXEC}, now=LIVE)
    check(
        "CODE_EXEC bound -> sandboxed execution is stated",
        "run Python in this job's sandboxed workspace" in block,
        block,
    )

    for capability, phrase in (
        (FILES, "read the files the user uploaded"),
        (WEB_SEARCH, "search the web"),
    ):
        block = runtime_context.runtime_block({capability}, now=LIVE)
        check(
            f"{capability} bound -> its ability is stated",
            phrase in block,
            block,
        )


def test_no_capabilities_says_so_plainly() -> None:
    block = runtime_context.runtime_block(frozenset(), now=LIVE)
    check(
        "with no tools, the block says so instead of implying ability",
        "Tools available in this run: NONE" in block,
        block,
    )
    check(
        "and it does not carry the anti-disclaimer rule (there is nothing to contradict)",
        "Do NOT claim you cannot access the internet" not in block,
        block,
    )


def test_an_already_read_source_is_not_a_run_without_tools() -> None:
    """Live: a Reddit-only job (capabilities ['social_read']) was told
    "Tools available in this run: NONE ... say so plainly", and the answer
    opened with "I have no tool to search Reddit; a previous worker's notes
    say ..." above the correct result."""
    block = runtime_context.runtime_block({capabilities.SOCIAL_READ}, now=LIVE)
    check("social read is not reported as no tools", "NONE" not in block, block)
    check("the Reddit posts are stated as available", "Reddit posts" in block, block)
    check(
        "the writer answers from the notes without a disclaimer",
        "answer from it directly" in block
        and "another worker's notes" in block,
        block,
    )
    for capability in (
        capabilities.MAIL_READ,
        capabilities.JIRA_READ,
        capabilities.CONFLUENCE_READ,
        capabilities.GITHUB_READ,
        capabilities.DOCUMENTS_READ,
    ):
        block = runtime_context.runtime_block({capability}, now=LIVE)
        check(f"{capability} is not reported as no tools", "NONE" not in block, block)
    block = runtime_context.runtime_block({WEB_FETCH}, now=LIVE)
    check(
        "a job that read nothing in advance gets no notes line",
        "answer from it directly" not in block,
        block,
    )


def test_capability_implication_is_respected() -> None:
    """WEB_SEARCH implies WEB_FETCH, so a search-only grant must advertise
    both -- the block is built from expand(), like every other consumer."""
    block = runtime_context.runtime_block({WEB_SEARCH}, now=LIVE)
    check(
        "web_search alone still advertises fetching, via the implication",
        "open and read specific web pages" in block,
        block,
    )


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


def test_language_selection() -> None:
    cases = [
        (LIVE_REQUEST, "Turkish"),
        ("Bana bu dosyanın özetini ver lütfen", "Turkish"),
        ("bugün dolar kuru nedir", "Turkish"),
        ("Give me the current exchange rates as an Excel file", "English"),
        ("summarize the attached report for me please", "English"),
    ]
    for text, expected in cases:
        found = runtime_context.dominant_language(text)
        check(
            f"language of {text[:38]!r} is {expected}",
            found == expected,
            str(found),
        )

    check(
        "an unrecognised language is not guessed at",
        runtime_context.dominant_language("请总结这份文件的要点") is None,
    )
    check(
        "empty input is not guessed at",
        runtime_context.dominant_language("") is None,
    )


def test_language_reaches_the_block() -> None:
    turkish = runtime_context.runtime_block(
        {WEB_FETCH}, request_text=LIVE_REQUEST, now=LIVE
    )
    check(
        "a Turkish request asks for a Turkish answer",
        "Write everything the user reads in Turkish" in turkish,
        turkish,
    )
    english = runtime_context.runtime_block(
        {WEB_FETCH}, request_text="fetch the current rates and give me a file", now=LIVE
    )
    check(
        "an English request asks for an English answer",
        "Write everything the user reads in English" in english,
        english,
    )
    other = runtime_context.runtime_block(
        {WEB_FETCH}, request_text="请总结这份文件的要点", now=LIVE
    )
    check(
        "an unrecognised language falls back to matching the request",
        "the same language the user wrote in" in other,
        other,
    )
    check(
        "technical identifiers are excluded from translation in every case",
        all(
            "do not translate those" in block
            for block in (turkish, english, other)
        ),
    )


def test_mixed_language_picks_the_dominant_side() -> None:
    mostly_turkish = (
        "Bu sayfadaki verileri al ve bana excel olarak ver, dosya adı "
        "report.xlsx olsun"
    )
    check(
        "a Turkish request containing English identifiers stays Turkish",
        runtime_context.dominant_language(mostly_turkish) == "Turkish",
        str(runtime_context.dominant_language(mostly_turkish)),
    )
    mostly_english = (
        "Please download the data from this page and give me an Excel file "
        "named kurlar.xlsx"
    )
    check(
        "an English request containing a Turkish filename stays English",
        runtime_context.dominant_language(mostly_english) == "English",
        str(runtime_context.dominant_language(mostly_english)),
    )


def test_block_stays_compact() -> None:
    """It is prepended to every model call, so its size is paid per turn."""
    block = runtime_context.runtime_block(
        set(capabilities.ALL_CAPABILITIES), request_text=LIVE_REQUEST, now=LIVE
    )
    check(
        "the fully-loaded block stays small",
        len(block) < 2200,
        f"{len(block)} chars",
    )


def main() -> None:
    test_requested_past_date_is_grounded_as_past()
    test_today_yesterday_tomorrow_track_the_injected_clock()
    test_relative_expressions_are_anchored()
    test_now_local_is_timezone_aware_and_not_hardcoded()
    test_bound_capabilities_are_stated_as_abilities()
    test_absent_capabilities_are_never_claimed()
    test_no_capabilities_says_so_plainly()
    test_capability_implication_is_respected()
    test_language_selection()
    test_language_reaches_the_block()
    test_mixed_language_picks_the_dominant_side()
    test_block_stays_compact()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All runtime-context checks passed")


if __name__ == "__main__":
    main()
