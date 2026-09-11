"""Turn resolver: expand follow-up references and decide Direct vs work."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import turn_resolution
from model_config import UsageTracker

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)
        raise AssertionError(f"{name} -- {detail}")


class _Msg:
    def __init__(self, parsed=None, content=""):
        self.parsed = parsed
        self.content = content


class _Response:
    def __init__(self, parsed=None, content=""):
        self.msg = _Msg(parsed, content)


class _ScriptedAgent:
    parsed = None
    content = ""
    error = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def astep(self, prompt, response_format=None):
        if _ScriptedAgent.error is not None:
            raise _ScriptedAgent.error
        return _Response(_ScriptedAgent.parsed, _ScriptedAgent.content)


def _resolve(topic, parsed=None, content="", error=None, prior=None, catalog=None):
    _ScriptedAgent.parsed = parsed
    _ScriptedAgent.content = content
    _ScriptedAgent.error = error
    prior = prior if prior is not None else [
        {"role": "user", "content": "Süper Lig puan durumu"},
        {"role": "assistant", "content": "Galatasaray, Fenerbahçe, Beşiktaş"},
    ]
    catalog = catalog if catalog is not None else []
    with patch("camel.agents.ChatAgent", _ScriptedAgent), patch(
        "model_config.get_model", lambda *a, **k: None
    ):
        return asyncio.run(
            turn_resolution.resolve_turn(
                topic,
                tracker=UsageTracker(),
                prior_messages=prior,
                catalog=catalog,
            )
        )


def test_first_turn_and_single_skip_llm() -> None:
    first = turn_resolution.first_turn("  araştır  ")
    check("first turn keeps topic", first.resolved_request == "araştır")
    check("first turn needs external work", first.needs_external_work is True)
    check("first turn source", first.source == "first_turn")

    single = turn_resolution.single_mode("kısalt")
    check("single never upgrades", single.needs_external_work is False)
    check("single source", single.source == "single_mode")

    empty = asyncio.run(
        turn_resolution.resolve_turn(
            "ilk iş",
            tracker=UsageTracker(),
            prior_messages=[],
            catalog=[],
        )
    )
    check("empty history skips LLM", empty.source == "first_turn")


def test_fallback_fails_closed_to_external_work() -> None:
    fb = turn_resolution.fallback("ilk üçü kim?")
    check("fallback keeps topic", fb.resolved_request == "ilk üçü kim?")
    check("fallback needs work", fb.needs_external_work is True)
    check("fallback source", fb.source == "fallback")

    intent = _resolve("ilk üçü kim?", error=RuntimeError("timeout"))
    check("LLM error uses fallback", intent.source == "fallback")
    check("LLM error does not silent-Direct", intent.needs_external_work is True)

    intent = _resolve("ilk üçü kim?", content="not json")
    check("bad payload uses fallback", intent.source == "fallback")
    check("bad payload needs work", intent.needs_external_work is True)


def test_llm_result_and_filename_bind() -> None:
    catalog = [
        {"file_id": "abc", "filename": "notes.pdf"},
        {"file_id": "zzz", "filename": "other.xlsx"},
    ]
    parsed = turn_resolution.TurnResolutionResult(
        resolved_request="Az önce yüklenen notes.pdf dosyasını özetle",
        needs_external_work=True,
        bind_file_ids=["notes.pdf", "invented"],
    )
    intent = _resolve(
        "az önce yüklediğim dosyayı özetle",
        parsed=parsed,
        catalog=catalog,
    )
    check("llm source", intent.source == "llm")
    check("filename maps to id", intent.bind_file_ids == ("abc",), intent.bind_file_ids)
    check("unknown ids dropped", "invented" not in intent.bind_file_ids)
    check("resolved kept", "notes.pdf" in intent.resolved_request)


def test_empty_resolved_falls_back_to_topic() -> None:
    parsed = turn_resolution.TurnResolutionResult(
        resolved_request="  ",
        needs_external_work=False,
        bind_file_ids=[],
    )
    intent = _resolve("bunu kısalt", parsed=parsed)
    check("blank resolved uses topic", intent.resolved_request == "bunu kısalt")
    check("false external work is kept", intent.needs_external_work is False)


def test_recover_fenced_json() -> None:
    content = '```json\n{"resolved_request": "MotoGP 2026 araştır", "needs_external_work": true, "bind_file_ids": []}\n```'
    intent = _resolve("aynısını MotoGP için", content=content)
    check("recovered source", intent.source == "llm")
    check("recovered text", intent.resolved_request == "MotoGP 2026 araştır")
    check("recovered work flag", intent.needs_external_work is True)


def test_format_recent_turns_keeps_newest() -> None:
    huge = "x" * (turn_resolution.MAX_RECENT_TURNS_CHARS + 10)
    text = turn_resolution.format_recent_turns(
        [
            {"role": "user", "content": "şimdi"},
            {"role": "assistant", "content": huge},
        ]
    )
    check("newest turn kept", "şimdi" in text)
    check("oversized older turn dropped", huge not in text)


def main() -> None:
    test_first_turn_and_single_skip_llm()
    test_fallback_fails_closed_to_external_work()
    test_llm_result_and_filename_bind()
    test_empty_resolved_falls_back_to_topic()
    test_recover_fenced_json()
    test_format_recent_turns_keeps_newest()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All turn-resolution checks passed")


if __name__ == "__main__":
    main()
