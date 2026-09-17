"""Email delivery of a scheduled run's result through Outlook.

A task's ``delivery`` setting names fixed recipients, chosen by the person
who created the task -- never by the model -- and when to send. When a run
settles and the condition holds, the run is marked ``delivery_status =
"pending"``; the scheduler tick then tries the send (runner.deliver_pending)
and retries with backoff while the bridge or Outlook is unavailable, because
the computer with Outlook may be asleep when the run finishes.

The send itself (building the message from the job, reading its files) is
main._deliver_scheduled_run, injected into the Scheduler like submit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import outlook
from scheduling import chains, config

CONDITIONS = chains.CONDITIONS
DEFAULT_CONDITION = "success"

# Retry delays after the 1st, 2nd, ... failed attempt; the last one repeats.
_BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)


@dataclass
class DeliveryOutcome:
    ok: bool
    error: Optional[str] = None
    retryable: bool = True
    notes: list[str] = field(default_factory=list)


def normalize(raw: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Validated delivery setting, or None for no email. Raises ValueError."""
    if not raw:
        return None
    try:
        to = outlook.parse_addresses(raw.get("to"))
        cc = outlook.parse_addresses(raw.get("cc"))
    except outlook.AddressError as exc:
        raise ValueError(str(exc)) from exc
    if not to:
        raise ValueError("Add at least one email recipient.")
    if len(to) + len(cc) > outlook.MAX_RECIPIENTS:
        raise ValueError(f"At most {outlook.MAX_RECIPIENTS} recipients in total.")
    on = str(raw.get("on") or DEFAULT_CONDITION)
    if on not in CONDITIONS:
        raise ValueError(f"Unknown delivery condition: {on}")
    return {"to": to, "cc": cc, "on": on, "attach_files": bool(raw.get("attach_files", True))}


def should_deliver(
    task: dict[str, Any],
    status: str,
    change_detected: Optional[bool],
    *,
    is_watch: bool,
    has_result: bool,
) -> bool:
    setting = task.get("delivery")
    if not setting or not setting.get("to"):
        return False
    # Someone stopped this run on purpose; nobody needs an email about it.
    if status == "cancelled":
        return False
    return chains.condition_met(
        str(setting.get("on") or DEFAULT_CONDITION),
        status,
        change_detected,
        upstream_is_watch=is_watch,
        has_result=has_result,
    )


def retry_delay(attempts: int) -> float:
    """Seconds before the next attempt, after ``attempts`` failed ones."""
    index = max(0, min(attempts, len(_BACKOFF_SECONDS)) - 1)
    return float(_BACKOFF_SECONDS[index])


def gives_up(attempts: int) -> bool:
    return attempts >= config.max_delivery_attempts()
