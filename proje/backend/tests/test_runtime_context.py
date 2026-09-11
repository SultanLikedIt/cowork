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


# ---------------------------------------------------------------------------
# Wiring: every model-facing path actually receives the block
# ---------------------------------------------------------------------------
#
# The block being correct is half the fix; the paths that lose it are how a
# grounded first call becomes an ungrounded second one. These capture the
# system messages actually handed to ChatAgent.


class _CapturingChatAgent:
    """Records every system message a run constructs. No model, no network."""

    captured: list = []

    def __init__(self, system_message=None, **kwargs):
        content = getattr(system_message, "content", "") or ""
        _CapturingChatAgent.captured.append(content)
        self.kwargs = kwargs
        self._internal_tools = {}

    def remove_tools(self, names):
        return None

    async def astep(self, *args, **kwargs):
        raise RuntimeError("stop here: only the system message is under test")

    def reset(self):
        return None


def _capture_direct(
    topic: str,
    job_capabilities,
    workspace_root=None,
    *,
    content=None,
    request_topic=None,
) -> str:
    import asyncio

    from unittest.mock import patch

    import camel_service

    import web_controller

    _CapturingChatAgent.captured = []
    queue: "asyncio.Queue" = asyncio.Queue()

    async def _noop_acquire(*_args, **_kwargs):
        return web_controller.ControllerResult()

    async def _drive():
        with patch.object(camel_service, "ChatAgent", _CapturingChatAgent), patch.object(
            camel_service, "get_model", lambda *a, **k: None
        ), patch.object(
            camel_service.web_controller, "acquire_web_evidence", _noop_acquire
        ):
            try:
                await camel_service.run_single_agent(
                    content if content is not None else topic,
                    __import__("model_config").UsageTracker(),
                    "job-1",
                    queue,
                    workspace_root=workspace_root,
                    job_capabilities=job_capabilities,
                    request_topic=request_topic,
                )
            except Exception:  # noqa: BLE001 - astep deliberately stops the run
                pass

    asyncio.run(_drive())
    return "\n".join(_CapturingChatAgent.captured)


def test_direct_path_receives_the_runtime_block() -> None:
    workspace = tempfile.mkdtemp(prefix="runtime_direct_ws_")
    message = _capture_direct(LIVE_REQUEST, {WEB_FETCH, ARTIFACTS}, workspace)

    check(
        "DIRECT: the system message carries RUN CONTEXT",
        "RUN CONTEXT" in message,
        message[-400:],
    )
    check(
        "DIRECT: it carries a real current date",
        "Current date and time:" in message and "today = " in message,
        message[-400:],
    )
    check(
        "DIRECT: WEB_FETCH is not advertised on the chatbot path",
        "open and read specific web pages" not in message,
        message[-600:],
    )
    check(
        "DIRECT: leftover job_capabilities cannot re-arm web search/fetch",
        "Web pages for this request have already been opened" not in message,
        message[-600:],
    )
    check(
        "DIRECT: ARTIFACTS is not advertised on the chatbot path",
        "create real downloadable files" not in message,
        message[-600:],
    )
    check(
        "DIRECT: the anti-disclaimer rule reaches the executor",
        "Do NOT claim you cannot access the internet" in message,
        message[-600:],
    )
    check(
        "DIRECT: a Turkish request is answered in Turkish",
        "Write everything the user reads in Turkish" in message,
        message[-300:],
    )


def test_direct_language_uses_the_user_topic_not_prior_english() -> None:
    """A Turkish follow-up must stay Turkish even when prior assistant
    replies in the combined prompt are a long English dump -- the live
    failure this pins: 'türkçe verir misin' after an English Galatasaray
    answer was instructed to write English."""
    english_history = (
        "Galatasaray is a Turkish professional football club based in "
        "Istanbul. The and is are was were of to a an for me my you your "
        "it in on at from with please give get make create download file "
        "files current date today yesterday tomorrow what which how many "
        "can could would should this that site page website data. "
        * 3
    )
    combined = (
        "türkçe verir misin\n\n"
        "PRIOR MESSAGES IN THIS CONVERSATION:\n--- ASSISTANT ---\n"
        + english_history
    )
    message = _capture_direct(
        "türkçe verir misin",
        frozenset(),
        content=combined,
        request_topic="türkçe verir misin",
    )
    check(
        "DIRECT: a Turkish follow-up is not flipped to English by prior replies",
        "Write everything the user reads in Turkish" in message,
        message[-400:],
    )
    check(
        "DIRECT: the English history did not win language selection",
        "Write everything the user reads in English" not in message,
        message[-400:],
    )


def test_direct_path_does_not_overclaim_without_capabilities() -> None:
    message = _capture_direct("Explain how photosynthesis works", frozenset())
    check(
        "DIRECT: a tool-free run claims no web access",
        "open and read specific web pages" not in message,
        message[-500:],
    )
    check(
        "DIRECT: a tool-free run claims no file creation",
        "create real downloadable files" not in message,
        message[-500:],
    )
    check(
        "DIRECT: a tool-free run is still date-grounded",
        "Current date and time:" in message,
        message[-400:],
    )
    check(
        "DIRECT: an English request is answered in English",
        "Write everything the user reads in English" in message,
        message[-300:],
    )


def test_every_direct_profile_receives_it() -> None:
    """Grounding is a property of the run, not the persona -- no profile may
    be the one that loses it."""
    import capability_routing

    for profile in capability_routing.PROFILES:
        import asyncio

        from unittest.mock import patch

        import camel_service

        _CapturingChatAgent.captured = []

        async def _drive(profile=profile):
            with patch.object(
                camel_service, "ChatAgent", _CapturingChatAgent
            ), patch.object(camel_service, "get_model", lambda *a, **k: None):
                try:
                    await camel_service.run_single_agent(
                        "Bana bu verileri excel olarak ver",
                        __import__("model_config").UsageTracker(),
                        "job-1",
                        asyncio.Queue(),
                        job_capabilities={ARTIFACTS},
                        profile=profile,
                    )
                except Exception:  # noqa: BLE001
                    pass

        asyncio.run(_drive())
        message = "\n".join(_CapturingChatAgent.captured)
        check(
            f"DIRECT profile {profile!r} receives RUN CONTEXT",
            "RUN CONTEXT" in message and "Current date and time:" in message,
            message[-300:],
        )


def test_workforce_workers_and_synthesis_receive_it() -> None:
    import asyncio

    from unittest.mock import patch

    import camel_service

    # --- workers -------------------------------------------------------
    # A REAL Workforce with its real fixed workers, as test_skills.py builds
    # one: the assertion is about the system messages those workers actually
    # carry, so faking the construction would test nothing.
    from camel.societies.workforce.workforce_callback import WorkforceCallback

    class _Callback(WorkforceCallback):
        """Workforce validates the callback's type, so subclass the real
        interface rather than duck-typing it (as test_skills.py does)."""

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

        def log_task_created(self, event): pass
        def log_task_decomposed(self, event): pass
        def log_task_assigned(self, event): pass
        def log_task_started(self, event): pass
        def log_task_completed(self, event): pass
        def log_task_failed(self, event): pass
        def log_worker_created(self, event): pass
        def log_worker_deleted(self, event): pass
        def log_all_tasks_completed(self, event): pass

    workforce = camel_service.build_workforce(
        _Callback(),
        __import__("model_config").UsageTracker(),
        job_capabilities=frozenset({WEB_SEARCH}),
        request_text=LIVE_REQUEST,
    )
    worker_messages = [
        child.worker.system_message.content
        for child in workforce._children
        if getattr(child, "worker", None) is not None
        and child.worker.system_message is not None
    ]
    check(
        "WORKFORCE: workers were constructed and captured",
        len(worker_messages) > 0,
        str(len(worker_messages)),
    )
    check(
        "WORKFORCE: every worker receives RUN CONTEXT",
        all("RUN CONTEXT" in message for message in worker_messages),
        f"{sum('RUN CONTEXT' in m for m in worker_messages)}/{len(worker_messages)}",
    )
    check(
        "WORKFORCE: every worker is date-grounded",
        all("Current date and time:" in message for message in worker_messages),
        str(len(worker_messages)),
    )
    check(
        "WORKFORCE: the user's language reaches the workers",
        all(
            "Write everything the user reads in Turkish" in message
            for message in worker_messages
        ),
        str(len(worker_messages)),
    )
    check(
        "WORKFORCE: the research worker advertises its real web ability",
        any("open and read specific web pages" in m for m in worker_messages),
        "no worker advertised web access",
    )
    check(
        "WORKFORCE: tool-free workers do not claim web access",
        any("Tools available in this run: NONE" in m for m in worker_messages),
        "no worker reported a tool-free run",
    )

    # --- synthesis -----------------------------------------------------
    _CapturingChatAgent.captured = []

    # A real, DONE Task: synthesis short-circuits to a deterministic
    # assembly when there is no successful work, and would never build the
    # agent this test is about.
    from camel.tasks.task import Task, TaskState

    # TWO successful leaves: a single-leaf result is handed through
    # unchanged (there is nothing to reconcile), so synthesis would never
    # run at all.
    done = Task(content="root", id="t0")
    done.state = TaskState.DONE
    done.result = "root"
    done.subtasks = []
    for index in (1, 2):
        leaf = Task(content=f"subtask {index}", id=f"t{index}")
        leaf.state = TaskState.DONE
        leaf.result = f"Part {index}: the 2026-08-13 rates were retrieved."
        done.subtasks.append(leaf)

    with patch.object(camel_service, "ChatAgent", _CapturingChatAgent), patch.object(
        camel_service, "get_model", lambda *a, **k: None
    ):
        try:
            asyncio.run(
                camel_service.synthesize_workforce_result(
                    done,
                    LIVE_REQUEST,
                    __import__("model_config").UsageTracker(),
                    job_capabilities=frozenset({WEB_FETCH, ARTIFACTS}),
                )
            )
        except Exception:  # noqa: BLE001 - astep deliberately stops it
            pass

    synthesis = "\n".join(_CapturingChatAgent.captured)
    check(
        "SYNTHESIS: the final-answer call receives RUN CONTEXT",
        "RUN CONTEXT" in synthesis,
        synthesis[-400:],
    )
    check(
        "SYNTHESIS: it is date-grounded",
        "Current date and time:" in synthesis,
        synthesis[-400:],
    )
    check(
        "SYNTHESIS: it answers in the user's language",
        "Write everything the user reads in Turkish" in synthesis,
        synthesis[-300:],
    )
    check(
        "SYNTHESIS: it does not disclaim abilities the run actually had",
        "Do NOT claim you cannot access the internet" in synthesis
        and "Tools available in this run: NONE" not in synthesis,
        synthesis[-600:],
    )


def test_finalization_keeps_the_grounding_it_already_had() -> None:
    """_finalize_without_tools reuses the SAME agent, so the system message
    -- and its RUN CONTEXT -- is still in memory. This pins that the prompt
    does not undo it by inviting a generic capability disclaimer."""
    import inspect

    import camel_service

    source = inspect.getsource(camel_service._finalize_without_tools)
    check(
        "FINALIZATION: the same agent is reused, so grounding persists",
        "agent.astep" in source and "ChatAgent(" not in source,
        "finalization built a new agent",
    )
    check(
        "FINALIZATION: the prompt forbids generalising a failure into a disclaimer",
        "do not turn it into a general" in source,
        source[-600:],
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
    test_direct_path_receives_the_runtime_block()
    test_direct_language_uses_the_user_topic_not_prior_english()
    test_direct_path_does_not_overclaim_without_capabilities()
    test_every_direct_profile_receives_it()
    test_workforce_workers_and_synthesis_receive_it()
    test_finalization_keeps_the_grounding_it_already_had()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All runtime-context checks passed")


if __name__ == "__main__":
    main()
