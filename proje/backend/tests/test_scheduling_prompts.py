"""Prompt variables and previous-run context for scheduled tasks."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scheduling import prompts

IST = "Europe/Istanbul"


def _ts(*parts: int) -> float:
    return datetime(*parts, tzinfo=ZoneInfo(IST)).timestamp()


def test_variables_resolve_against_fire_time() -> None:
    text = prompts.render_prompt(
        "{tarih} tarihli rapor: {hafta} haftası, {ay}, son çalışma {son_calisma}",
        fire_time=_ts(2026, 9, 21, 9, 0),
        timezone=IST,
        last_run_at=_ts(2026, 9, 14, 9, 0),
    )
    assert text == (
        "21 Eylül 2026 tarihli rapor: 14–21 Eylül 2026 haftası, Eylül 2026, "
        "son çalışma 14 Eylül 2026"
    )


def test_first_run_has_no_previous_date() -> None:
    text = prompts.render_prompt(
        "{son_calisma} haberleri", fire_time=_ts(2026, 10, 3, 9, 0), timezone=IST, last_run_at=None
    )
    assert text.startswith("son 7 gün (26 Eylül 2026 sonrası)")


def test_week_across_months() -> None:
    text = prompts.render_prompt("{hafta}", fire_time=_ts(2026, 10, 2, 9, 0), timezone=IST, last_run_at=None)
    assert text == "25 Eylül – 2 Ekim 2026"


def test_unknown_braces_are_left_alone() -> None:
    text = prompts.render_prompt("{json} ve {tarih}", fire_time=_ts(2026, 9, 21, 9, 0), timezone=IST, last_run_at=None)
    assert text == "{json} ve 21 Eylül 2026"


def test_previous_summary_context() -> None:
    task = {"last_result_summary": "Faiz sabit kaldı.", "last_run_at": _ts(2026, 9, 14, 9, 0), "include_previous_summary": True}
    context = prompts.previous_summary_context(task, IST)
    assert "(14 Eylül 2026)" in context
    assert context.endswith("ÖNCEKİ ÖZET:\nFaiz sabit kaldı.")
    assert prompts.previous_summary_context({**task, "include_previous_summary": False}, IST) is None
    assert prompts.previous_summary_context({**task, "last_result_summary": ""}, IST) is None


def test_summary_drops_sources_and_is_bounded() -> None:
    result = "Başlık\n\nİlk paragraf.\n\n**Kaynaklar**\n- https://example.com/a\n- https://example.com/b"
    assert prompts.summarize_result(result, 500) == "Başlık\n\nİlk paragraf."
    long_text = "\n".join(f"satır {i} " + "x" * 50 for i in range(100))
    summary = prompts.summarize_result(long_text, 500)
    assert len(summary) <= 502
    assert summary.endswith("…")
