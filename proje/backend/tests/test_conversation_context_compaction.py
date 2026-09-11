"""Regression test for conversation context compaction.

main._build_conversation_context used to silently DROP whole older turns
once CONVERSATION_CONTEXT_MAX_CHARS was exceeded, leaving a one-line
"[Earlier conversation history truncated at bounded limit.]" note. This
covers the replacement: dropped turns get folded into a per-conversation
running summary (db.conversations.summary) instead, so the model still sees
what happened earlier even once raw turns stop fitting.

No model or network: camel_service.summarize_conversation_history is
monkeypatched to a deterministic stub.

    python test_conversation_context_compaction.py
"""

import asyncio
import os
import tempfile
import time
import uuid

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import db  # noqa: E402
import main  # noqa: E402
import camel_service  # noqa: E402
from model_config import UsageTracker  # noqa: E402

db.run_migrations()

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def _new_conversation() -> tuple[str, str]:
    workspace_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    now = time.time()
    db.create_workspace(workspace_id, name=None, created_at=now)
    db.create_conversation(conversation_id, workspace_id, "Test conversation", now)
    return workspace_id, conversation_id


def _insert_messages(workspace_id: str, conversation_id: str, count: int, size: int) -> None:
    base = time.time() - count  # ascending created_at, oldest first
    for i in range(count):
        db.insert_message(
            workspace_id,
            conversation_id,
            job_id=None,
            role="user" if i % 2 == 0 else "assistant",
            content=f"turn {i}: " + ("x" * size),
            created_at=base + i,
        )


async def _fake_summarize(previous_summary, dropped_entries, tracker) -> str:
    marker = "PREV+" if previous_summary else ""
    return f"{marker}SUMMARY[{len(dropped_entries)} entries folded]"


def test_short_conversation_has_no_summary() -> None:
    workspace_id, conversation_id = _new_conversation()
    _insert_messages(workspace_id, conversation_id, count=3, size=50)

    tracker = UsageTracker()
    context = asyncio.run(main._build_conversation_context(conversation_id, tracker))

    check("short conversation returns raw context", context is not None, context)
    check(
        "no summary block for a conversation well under budget",
        "[Summary of earlier conversation:]" not in (context or ""),
        context,
    )
    check(
        "all three turns present verbatim",
        all(f"turn {i}:" in (context or "") for i in range(3)),
        context,
    )


def test_long_conversation_gets_summarized_not_dropped() -> None:
    workspace_id, conversation_id = _new_conversation()
    # Each entry is ~4KB with wrapper text; 10 of them clears the 20K budget,
    # forcing the oldest ones to be dropped -> folded into a summary instead.
    _insert_messages(workspace_id, conversation_id, count=10, size=4_000)

    original = camel_service.summarize_conversation_history
    camel_service.summarize_conversation_history = _fake_summarize
    try:
        tracker = UsageTracker()
        context = asyncio.run(
            main._build_conversation_context(conversation_id, tracker)
        )
    finally:
        camel_service.summarize_conversation_history = original

    check("long conversation still returns context", context is not None, context)
    check(
        "a summary block was injected instead of a bare truncation note",
        "[Summary of earlier conversation:]" in (context or ""),
        context,
    )
    check(
        "the fake summarizer's output made it into the context",
        "SUMMARY[" in (context or ""),
        context,
    )
    check(
        "the most recent turn is present verbatim",
        "turn 9:" in (context or ""),
        context,
    )

    stored = db.get_conversation(conversation_id)
    check(
        "the summary was persisted to the conversation row",
        bool(stored and stored.get("summary")),
        stored,
    )
    check(
        "summarized_through_created_at was recorded",
        bool(stored and stored.get("summarized_through_created_at")),
        stored,
    )


def test_second_overflow_only_folds_new_turns() -> None:
    """Incrementality: a summary already covering turns up to N must not be
    re-summarized from scratch when the conversation grows further -- only
    the newly-overflowing turns should reach the summarizer."""
    workspace_id, conversation_id = _new_conversation()
    _insert_messages(workspace_id, conversation_id, count=10, size=4_000)

    seen_calls: list[int] = []

    async def _counting_summarize(previous_summary, dropped_entries, tracker) -> str:
        seen_calls.append(len(dropped_entries))
        prefix = "PREV+" if previous_summary else ""
        return f"{prefix}SUMMARY[{len(dropped_entries)}]"

    original = camel_service.summarize_conversation_history
    camel_service.summarize_conversation_history = _counting_summarize
    try:
        tracker = UsageTracker()
        asyncio.run(main._build_conversation_context(conversation_id, tracker))
        first_call_size = seen_calls[-1] if seen_calls else None

        # Conversation grows; re-run context assembly.
        _insert_messages(workspace_id, conversation_id, count=3, size=4_000)
        asyncio.run(main._build_conversation_context(conversation_id, tracker))
    finally:
        camel_service.summarize_conversation_history = original

    check(
        "summarizer was called on both overflow events",
        len(seen_calls) == 2,
        seen_calls,
    )
    check(
        "the second call folded only newly-overflowing turns, not the whole history again",
        bool(seen_calls) and seen_calls[-1] < (first_call_size or 0) + 10,
        seen_calls,
    )
    stored = db.get_conversation(conversation_id)
    check(
        "second call's summary carried the PREV+ marker (built on the existing summary)",
        bool(stored and str(stored.get("summary") or "").startswith("PREV+")),
        stored,
    )


def main_test() -> None:
    test_short_conversation_has_no_summary()
    test_long_conversation_gets_summarized_not_dropped()
    test_second_overflow_only_folds_new_turns()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All conversation-compaction checks passed")


if __name__ == "__main__":
    main_test()
