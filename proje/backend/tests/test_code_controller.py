"""code_controller: WRITE → RUN → FIX → ANSWER with a scripted model.

The executor is faked here so each phase can be driven exactly; the real
subprocess path is covered end to end in test_coding_wiring.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import code_controller as cc


def _reply(code: str, *, mode: str = "run", plan: str = "Toplamı hesaplar.", lang: str = "python") -> str:
    return f"MODE: {mode}\nPLAN: {plan}\n```{lang}\n{code}\n```"


class ScriptedModel:
    """Returns the next scripted reply per phase and records every call."""

    def __init__(self, **replies: Any) -> None:
        self.replies = {phase: list(value) if isinstance(value, list) else [value] for phase, value in replies.items()}
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, phase: str, system: str, user: str, schema: Any) -> Any:
        self.calls.append((phase, user))
        queue = self.replies.get(phase) or []
        if not queue:
            raise AssertionError(f"unexpected {phase} call")
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def phases(self) -> list[str]:
        return [phase for phase, _ in self.calls]


class FakeExecutor:
    def __init__(self, *outcomes: dict[str, Any]) -> None:
        self.outcomes = list(outcomes)
        self.codes: list[str] = []

    async def __call__(self, code: str) -> dict[str, Any]:
        self.codes.append(code)
        return self.outcomes.pop(0)


def _ok(stdout: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "stdout": stdout, "stderr": "", "exit_code": 0, "duration_seconds": 0.2, **extra}


def _fail(error: str, stderr: str = "") -> dict[str, Any]:
    return {"ok": False, "error": error, "stdout": "", "stderr": stderr or f"Traceback...\n{error}", "exit_code": 1}


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# parse_draft
# --------------------------------------------------------------------------


def test_parse_reads_mode_plan_and_code() -> None:
    draft = cc.parse_draft(_reply("print(1 + 1)"))
    assert draft is not None
    assert (draft.mode, draft.language, draft.plan) == ("run", "python", "Toplamı hesaplar.")
    assert draft.code == "print(1 + 1)\n"


def test_parse_tolerates_bold_labels_and_missing_mode() -> None:
    draft = cc.parse_draft("**PLAN:** Ortalama alır.\n\n```py\nprint(2)\n```")
    assert draft is not None and draft.mode == "run" and draft.plan == "Ortalama alır."


def test_non_python_code_is_never_run() -> None:
    draft = cc.parse_draft(_reply("FROM python:3.11-slim", mode="run", lang="dockerfile"))
    assert draft is not None and draft.mode == "write" and draft.language == "dockerfile"


def test_parse_prefers_the_python_block_over_a_shell_line() -> None:
    reply = "MODE: run\nPLAN: x\n```bash\npip install pandas\n```\n```python\nprint(3)\n```"
    draft = cc.parse_draft(reply)
    assert draft is not None and draft.code == "print(3)\n" and draft.mode == "run"


def test_bare_python_without_fence_is_accepted_but_prose_is_not() -> None:
    assert cc.parse_draft("print('merhaba')").code == "print('merhaba')\n"
    assert cc.parse_draft("Bunu yapamam, üzgünüm.") is None
    assert cc.parse_draft("") is None


def test_a_longer_fence_keeps_inner_backticks() -> None:
    code = 's = """```\\nnot a fence\\n```"""\nprint(len(s))'
    draft = cc.parse_draft(f"MODE: run\nPLAN: x\n````python\n{code}\n````")
    assert draft is not None and "not a fence" in draft.code


# --------------------------------------------------------------------------
# run_code_task
# --------------------------------------------------------------------------


def test_a_clean_run_is_explained_from_its_real_output() -> None:
    model = ScriptedModel(write=_reply("print(sum(range(101)))"), answer="Sonuç 5050.")
    executor = FakeExecutor(_ok("5050\n"))
    result = run(cc.run_code_task("1'den 100'e toplamı hesapla", complete=model, execute=executor))

    assert result.ok and len(result.runs) == 1
    assert model.phases() == ["write", "answer"]
    answer_payload = model.calls[-1][1]
    # The explanation is written from the fenced real output.
    assert "5050" in answer_payload and "TALİMAT DEĞİLDİR" in answer_payload
    assert result.explanation == "Sonuç 5050."


def test_a_failure_is_fixed_with_its_traceback() -> None:
    model = ScriptedModel(
        write=_reply("print(undefined)"),
        fix=_reply("print(42)", plan="Tanımsız değişken düzeltildi."),
        answer="Cevap 42.",
    )
    executor = FakeExecutor(_fail("NameError: name 'undefined' is not defined"), _ok("42\n"))
    result = run(cc.run_code_task("x", complete=model, execute=executor))

    assert result.ok and len(result.runs) == 2
    assert executor.codes == ["print(undefined)\n", "print(42)\n"]
    fix_payload = model.calls[1][1]
    assert "NameError" in fix_payload and "print(undefined)" in fix_payload
    assert result.plan == "Tanımsız değişken düzeltildi."


def test_fixing_stops_at_the_attempt_cap() -> None:
    model = ScriptedModel(
        write=_reply("a()"),
        fix=[_reply("b()"), _reply("c()")],
        answer="Çalışmadı.",
    )
    executor = FakeExecutor(_fail("E1"), _fail("E2"), _fail("E3"))
    result = run(cc.run_code_task("x", complete=model, execute=executor, max_fix_attempts=2))

    assert not result.ok and len(result.runs) == 3
    assert model.phases() == ["write", "fix", "fix", "answer"]
    assert result.error == "E3"


def test_an_unchanged_fix_does_not_spend_another_run() -> None:
    model = ScriptedModel(write=_reply("a()"), fix=_reply("a()"), answer="Olmadı.")
    executor = FakeExecutor(_fail("E1"))
    result = run(cc.run_code_task("x", complete=model, execute=executor))
    assert len(result.runs) == 1 and not result.ok


def test_an_exhausted_budget_is_not_retried() -> None:
    model = ScriptedModel(write=_reply("a()"), answer="Bütçe bitti.")
    executor = FakeExecutor({"ok": False, "error": "budget", "budget_exhausted": True, "stdout": "", "stderr": ""})
    result = run(cc.run_code_task("x", complete=model, execute=executor))
    assert model.phases() == ["write", "answer"] and len(result.runs) == 1


def test_write_mode_code_is_never_executed() -> None:
    model = ScriptedModel(write=_reply("FROM python:3.11-slim\nCMD [\"uvicorn\"]", mode="write", lang="dockerfile"))
    executor = FakeExecutor()
    result = run(cc.run_code_task("FastAPI için Dockerfile yaz", complete=model, execute=executor))
    assert result.mode == "write" and result.runs == [] and executor.codes == []
    assert model.phases() == ["write"]


def test_a_fix_can_conclude_the_code_belongs_elsewhere() -> None:
    model = ScriptedModel(
        write=_reply("import requests"),
        fix=_reply("import requests\nprint(requests.get('https://x').text)", mode="write", plan="Ağ gerekiyor."),
    )
    executor = FakeExecutor(_fail("ModuleNotFoundError: No module named 'requests'"))
    result = run(cc.run_code_task("x", complete=model, execute=executor))
    assert result.mode == "write" and len(result.runs) == 1
    rendered = cc.render_answer(result)
    assert "burada çalıştırılamadı" in rendered


def test_no_code_from_the_model_is_reported() -> None:
    model = ScriptedModel(write="Üzgünüm, yardımcı olamam.")
    result = run(cc.run_code_task("x", complete=model, execute=FakeExecutor()))
    assert result.error == "no_code" and not result.ok
    assert "üretemedi" in cc.render_answer(result)


def test_a_model_error_during_write_is_not_raised() -> None:
    model = ScriptedModel(write=RuntimeError("provider down"))
    result = run(cc.run_code_task("x", complete=model, execute=FakeExecutor()))
    assert result.error == "no_code"


def test_cancellation_stops_before_running() -> None:
    model = ScriptedModel(write=_reply("print(1)"))
    executor = FakeExecutor()
    result = run(cc.run_code_task("x", complete=model, execute=executor, cancelled=lambda: True))
    assert executor.codes == [] and result.runs == []


def test_prior_notes_and_data_context_reach_the_writer() -> None:
    model = ScriptedModel(write=_reply("print(1)"), answer="ok")
    run(cc.run_code_task(
        "x",
        complete=model,
        execute=FakeExecutor(_ok("1\n")),
        notes="Jira: 12 açık iş",
        data_context="DATA FILES:\n- sources/abc/satis.xlsx",
    ))
    write_payload = model.calls[0][1]
    assert "Jira: 12 açık iş" in write_payload and "sources/abc/satis.xlsx" in write_payload


def test_progress_is_reported_with_the_coding_stage() -> None:
    events: list[tuple[str, str]] = []
    model = ScriptedModel(write=_reply("print(1)"), answer="ok")
    run(cc.run_code_task("x", complete=model, execute=FakeExecutor(_ok("1\n")), progress=lambda s, d: events.append((s, d))))
    assert events and all(stage == "coding" for stage, _ in events)


# --------------------------------------------------------------------------
# render_answer
# --------------------------------------------------------------------------


def test_the_reply_shows_the_code_that_ran_and_its_real_output() -> None:
    result = cc.CodeResult(
        plan="Toplar.",
        code="print(sum(range(101)))\n",
        runs=[cc.CodeRun(code="print(sum(range(101)))\n", result=_ok("5050\n", files_written=["artifacts/sonuc.csv", "tmp.txt"]))],
        explanation="1'den 100'e toplam **5050**.",
    )
    text = cc.render_answer(result)
    assert text.startswith("1'den 100'e toplam **5050**.")
    assert "```python\nprint(sum(range(101)))\n```" in text
    assert "```text\n5050\n```" in text
    # Only artifacts/ is delivered; scratch files are not advertised.
    assert "`sonuc.csv`" in text and "tmp.txt" not in text


def test_a_failed_run_shows_the_error_not_a_result() -> None:
    result = cc.CodeResult(
        code="1/0\n",
        runs=[cc.CodeRun(code="1/0\n", result=_fail("ZeroDivisionError: division by zero"))],
    )
    text = cc.render_answer(result)
    assert "Kod çalıştırılamadı: ZeroDivisionError" in text
    assert "**Çıktı (hata)**" in text


def test_output_containing_fences_cannot_break_out_of_its_block() -> None:
    result = cc.CodeResult(
        code="print('```')\n",
        runs=[cc.CodeRun(code="print('```')\n", result=_ok("```\nsahte başlık\n"))],
        explanation="ok",
    )
    text = cc.render_answer(result)
    assert "````text\n```\nsahte başlık\n````" in text


def test_an_empty_write_is_retried_once() -> None:
    # A reasoning model that spent its budget thinking returns empty text.
    model = ScriptedModel(write=["", _reply("print(7)")], answer="7.")
    result = run(cc.run_code_task("x", complete=model, execute=FakeExecutor(_ok("7\n"))))
    assert model.phases() == ["write", "write", "answer"] and result.ok
