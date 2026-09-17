"""Environment settings for scheduled tasks, read at call time so tests can
override them with monkeypatch.setenv."""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _number(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name) or default))
    except ValueError:
        return default


def enabled() -> bool:
    return _flag("SCHEDULING_ENABLED", True)


def autostart() -> bool:
    """Start the loop with the app. Tests turn this off (tests/conftest.py)."""
    return _flag("SCHEDULER_AUTOSTART", True)


def tick_seconds() -> float:
    return _number("SCHEDULE_TICK_SECONDS", 30, 1)


def max_concurrent() -> int:
    return int(_number("SCHEDULE_MAX_CONCURRENT", 2, 1))


def catch_up_window_seconds() -> float:
    return _number("SCHEDULE_CATCHUP_WINDOW_HOURS", 6, 0) * 3600


def max_tasks_per_workspace() -> int:
    return int(_number("SCHEDULE_MAX_TASKS_PER_WORKSPACE", 20, 1))


def min_interval_seconds() -> float:
    return _number("SCHEDULE_MIN_INTERVAL_MINUTES", 60, 1) * 60


def min_deep_interval_seconds() -> float:
    return _number("SCHEDULE_MIN_DEEP_INTERVAL_HOURS", 24, 0) * 3600


def max_consecutive_failures() -> int:
    return int(_number("SCHEDULE_MAX_CONSECUTIVE_FAILURES", 3, 1))


def default_timezone() -> str:
    return (os.environ.get("SCHEDULE_DEFAULT_TIMEZONE") or "Europe/Istanbul").strip()


def summary_max_chars() -> int:
    return int(_number("SCHEDULE_SUMMARY_MAX_CHARS", 2000, 200))


def max_chain_depth() -> int:
    """How many after_task links may follow a chain's root task."""
    return int(_number("SCHEDULE_MAX_CHAIN_DEPTH", 3, 1))


def max_chained_runs_per_day() -> int:
    """Chained runs started per chain (root and all descendants) in 24 hours."""
    return int(_number("SCHEDULE_MAX_CHAINED_RUNS_PER_DAY", 10, 1))
