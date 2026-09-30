"""Cron parsing and next-fire computation for scheduled tasks."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduling import cron

IST = "Europe/Istanbul"


def _ts(*parts: int, tz: str = IST) -> float:
    return datetime(*parts, tzinfo=ZoneInfo(tz)).timestamp()


def _local(timestamp: float, tz: str = IST) -> datetime:
    return datetime.fromtimestamp(timestamp, ZoneInfo(tz))


def test_every_monday_nine_in_istanbul() -> None:
    # 2026-09-17 is a Thursday.
    fire = cron.next_fire_after("0 9 * * 1", _ts(2026, 9, 17, 10, 0), IST)
    assert _local(fire) == datetime(2026, 9, 21, 9, 0, tzinfo=ZoneInfo(IST))


def test_next_fire_is_strictly_after() -> None:
    at_fire = _ts(2026, 9, 21, 9, 0)
    fire = cron.next_fire_after("0 9 * * 1", at_fire, IST)
    assert _local(fire).date().isoformat() == "2026-09-28"


def test_same_day_later_time() -> None:
    fire = cron.next_fire_after("30 17 * * *", _ts(2026, 9, 17, 10, 0), IST)
    assert _local(fire) == datetime(2026, 9, 17, 17, 30, tzinfo=ZoneInfo(IST))


def test_timezone_changes_the_absolute_moment() -> None:
    after = _ts(2026, 9, 17, 0, 0, tz="UTC")
    istanbul = cron.next_fire_after("0 9 * * *", after, IST)
    utc = cron.next_fire_after("0 9 * * *", after, "UTC")
    assert utc - istanbul == 3 * 3600


def test_ranges_steps_lists_and_names() -> None:
    spec = cron.parse("*/15 9-17 * JAN,MAR mon-fri")
    assert spec.minutes == frozenset({0, 15, 30, 45})
    assert spec.hours == frozenset(range(9, 18))
    assert spec.months == frozenset({1, 3})
    assert spec.weekdays == frozenset({0, 1, 2, 3, 4})


def test_sunday_is_zero_and_seven() -> None:
    assert cron.parse("0 0 * * 0").weekdays == cron.parse("0 0 * * 7").weekdays == frozenset({6})


def test_day_of_month_or_day_of_week_when_both_restricted() -> None:
    # The 1st of the month OR any Friday. 2026-09-17 is a Thursday.
    fire = cron.next_fire_after("0 8 1 * 5", _ts(2026, 9, 17, 12, 0), IST)
    assert _local(fire).date().isoformat() == "2026-09-18"


def test_first_of_month() -> None:
    fire = cron.next_fire_after("0 9 1 * *", _ts(2026, 9, 17, 12, 0), IST)
    assert _local(fire) == datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo(IST))


def test_leap_day_schedule_found_years_ahead() -> None:
    fire = cron.next_fire_after("0 9 29 2 *", _ts(2026, 9, 17, 12, 0), IST)
    assert _local(fire).date().isoformat() == "2028-02-29"


def test_impossible_schedule_returns_none() -> None:
    assert cron.next_fire_after("0 9 31 2 *", _ts(2026, 9, 17, 12, 0), IST) is None


@pytest.mark.parametrize(
    "expression",
    ["", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *", "5-1 * * * *", "x * * * *"],
)
def test_invalid_expressions(expression: str) -> None:
    with pytest.raises(cron.CronError):
        cron.parse(expression)


def test_unknown_timezone() -> None:
    with pytest.raises(cron.CronError):
        cron.next_fire_after("0 9 * * *", 0, "Mars/Olympus")


def test_min_interval() -> None:
    now = _ts(2026, 9, 17, 12, 0)
    assert cron.min_interval_seconds("*/30 * * * *", now, IST) == 1800
    assert cron.min_interval_seconds("0 9 * * 1-5", now, IST) == 24 * 3600
    assert cron.next_fires("0 9 * * 1", now, IST, 3)[2] - cron.next_fires("0 9 * * 1", now, IST, 3)[0] == 14 * 86400
