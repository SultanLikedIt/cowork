"""Turn a saved task prompt into this run's request.

A scheduled prompt says "son bir haftanın haberleri"; the run needs to know
which week that is. Variables are resolved against the run's fire time in
the task's timezone, and the previous run's summary rides along as context
so the new report can focus on what is new.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from scheduling import cron

TR_MONTHS = (
    "Ocak",
    "Şubat",
    "Mart",
    "Nisan",
    "Mayıs",
    "Haziran",
    "Temmuz",
    "Ağustos",
    "Eylül",
    "Ekim",
    "Kasım",
    "Aralık",
)

VARIABLES = ("tarih", "hafta", "ay", "son_calisma")
_VARIABLE_RE = re.compile(r"\{(" + "|".join(VARIABLES) + r")\}")


def format_date(moment: datetime) -> str:
    return f"{moment.day} {TR_MONTHS[moment.month - 1]} {moment.year}"


def format_range(start: datetime, end: datetime) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {TR_MONTHS[end.month - 1]} {end.year}"
    if start.year == end.year:
        return f"{start.day} {TR_MONTHS[start.month - 1]} – {format_date(end)}"
    return f"{format_date(start)} – {format_date(end)}"


def render_prompt(
    prompt: str,
    *,
    fire_time: float,
    timezone: str,
    last_run_at: Optional[float],
) -> str:
    tz = cron.zone(timezone)
    now = datetime.fromtimestamp(fire_time, tz)
    week_start = now - timedelta(days=7)
    if last_run_at:
        since = format_date(datetime.fromtimestamp(last_run_at, tz))
    else:
        since = f"son 7 gün ({format_date(week_start)} sonrası)"
    values = {
        "tarih": format_date(now),
        "hafta": format_range(week_start, now),
        "ay": f"{TR_MONTHS[now.month - 1]} {now.year}",
        "son_calisma": since,
    }
    return _VARIABLE_RE.sub(lambda match: values[match.group(1)], prompt or "").strip()


def previous_summary_context(task: dict[str, Any], timezone: str) -> Optional[str]:
    summary = str(task.get("last_result_summary") or "").strip()
    if not summary or not task.get("include_previous_summary", True):
        return None
    when = ""
    if task.get("last_run_at"):
        when = " (" + format_date(datetime.fromtimestamp(float(task["last_run_at"]), cron.zone(timezone))) + ")"
    return (
        "Bu, düzenli çalışan bir görev. Önceki çalıştırmanın özeti" + when + " aşağıda. "
        "Yeni raporda önceki özette olmayan gelişmeleri öne çıkar; aynı bilgiyi "
        "yeni gibi tekrar sunma. Önceki özetteki bilgiyi kaynak olarak kullanma, "
        "yalnız neyin yeni olduğunu anlamak için kullan.\n\n"
        f"ÖNCEKİ ÖZET:\n{summary}"
    )


_SOURCES_RE = re.compile(r"(?ims)^\s*(?:#+\s*)?\*{0,2}(?:kaynaklar|sources)\*{0,2}\s*:?\s*$.*")


def summarize_result(result: str, max_chars: int) -> str:
    """Stored as the next run's context: the answer without its source list."""
    text = _SOURCES_RE.sub("", str(result or "")).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind("\n")
    if boundary > max_chars * 0.6:
        cut = cut[:boundary]
    return cut.rstrip() + " …"
