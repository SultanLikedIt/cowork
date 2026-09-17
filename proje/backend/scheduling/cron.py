"""Five-field cron expressions evaluated in a named timezone.

Written here instead of taking croniter: the backend image installs its
Python dependencies from an offline wheel bundle (see Dockerfile), and the
subset scheduled tasks need is small.

Supported: ``minute hour day-of-month month day-of-week`` with ``*``,
numbers, ranges ``a-b``, steps ``*/n`` / ``a-b/n`` / ``a/n``, lists, and
English month/day names (JAN..DEC, SUN..SAT). Day of week 0 and 7 are both
Sunday. As in classic cron, when both day-of-month and day-of-week are
restricted a day matches if EITHER does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_DAY_NAMES = {name: index for index, name in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}

# How far ahead next_fire_after looks before deciding an expression never
# fires (e.g. "0 0 31 2 *"). Leap-day schedules need up to four years.
_MAX_LOOKAHEAD_DAYS = 366 * 5


class CronError(ValueError):
    """The expression or timezone is not valid."""


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Monday ... 6 = Sunday (Python convention)
    dom_restricted: bool
    dow_restricted: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        dow_ok = day.weekday() in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def _value(token: str, names: dict[str, int], low: int, high: int) -> int:
    token = token.strip().upper()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise CronError(f"invalid cron value {token!r}")
    value = int(token)
    if not low <= value <= high:
        raise CronError(f"cron value {value} outside {low}-{high}")
    return value


def _field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> tuple[frozenset[int], bool]:
    names = names or {}
    values: set[int] = set()
    restricted = text.strip() != "*"
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError("empty cron list item")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"invalid cron step {step_text!r}")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start = _value(start_text, names, low, high)
            end = _value(end_text, names, low, high)
            if start > end:
                raise CronError(f"invalid cron range {part!r}")
        else:
            start = _value(part, names, low, high)
            # "5/15" means from 5 to the end in steps of 15.
            end = high if step > 1 else start
        values.update(range(start, end + 1, step))
    return frozenset(values), restricted


def parse(expression: str) -> CronSpec:
    fields = (expression or "").split()
    if len(fields) != 5:
        raise CronError("cron expression must have 5 fields: minute hour day month weekday")
    minutes, _ = _field(fields[0], 0, 59)
    hours, _ = _field(fields[1], 0, 23)
    days, dom_restricted = _field(fields[2], 1, 31)
    months, _ = _field(fields[3], 1, 12, _MONTH_NAMES)
    cron_weekdays, dow_restricted = _field(fields[4], 0, 7, _DAY_NAMES)
    # cron: 0/7 = Sunday, 1 = Monday. Python: 0 = Monday, 6 = Sunday.
    weekdays = frozenset((value - 1) % 7 for value in cron_weekdays)
    return CronSpec(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
    )


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone {name!r}") from exc


def next_fire_after(expression: str | CronSpec, after: float, timezone: str) -> Optional[float]:
    """First fire time strictly after ``after`` (epoch seconds), or None."""
    spec = parse(expression) if isinstance(expression, str) else expression
    tz = zone(timezone)
    start = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)
    day = start.date()
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        if spec.day_matches(day):
            for hour in hours:
                for minute in minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                    timestamp = candidate.timestamp()
                    if timestamp > after:
                        return timestamp
        day += timedelta(days=1)
    return None


def next_fires(expression: str, after: float, timezone: str, count: int) -> list[float]:
    spec = parse(expression)
    fires: list[float] = []
    cursor = after
    for _ in range(max(0, count)):
        fire = next_fire_after(spec, cursor, timezone)
        if fire is None:
            break
        fires.append(fire)
        cursor = fire
    return fires


def min_interval_seconds(expression: str, after: float, timezone: str, samples: int = 8) -> Optional[float]:
    """Smallest gap between consecutive fires over the next ``samples`` runs."""
    fires = next_fires(expression, after, timezone, samples)
    if len(fires) < 2:
        return None
    return min(later - earlier for earlier, later in zip(fires, fires[1:]))
