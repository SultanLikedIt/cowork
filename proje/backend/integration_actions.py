"""The approve / cancel lifecycle shared by every outside system a run can
propose a change to (jira_actions.py, confluence_actions.py).

A proposal is a row in ``integration_actions`` (migration 0013), already
validated by the system's own module. This module only moves it:

    proposed -> applying -> applied | failed        (apply)
    proposed -> cancelled                            (cancel)

proposed -> applying is one conditional UPDATE (db.move_integration_action),
so two clicks, two tabs or a retried request can never write the same change
twice. What was sent and what came back is kept on the row.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

logger = logging.getLogger("agent_webapp.integration_actions")


class ApplyError(Exception):
    """Refused before anything was sent; ``status`` is the HTTP status for the API."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def public(action: dict[str, Any]) -> dict[str, Any]:
    """What the UI gets for one action. Nothing in a row is secret."""
    return {
        "action_id": action.get("action_id"),
        "system": action.get("system"),
        "kind": action.get("kind"),
        "status": action.get("status"),
        "payload": action.get("payload") or {},
        "result": action.get("result"),
        "error": action.get("error"),
        "created_at": action.get("created_at"),
        "updated_at": action.get("updated_at"),
    }


def _load(action_id: str, system: str) -> dict[str, Any]:
    import db

    action = db.get_integration_action(action_id)
    if action is None or action.get("system") != system:
        raise ApplyError(404, "Böyle bir değişiklik önerisi yok.")
    return action


def apply(
    action_id: str,
    *,
    system: str,
    enabled: bool,
    disabled_message: str,
    send: Callable[[dict[str, Any]], dict[str, Any]],
    error_types: tuple[type[BaseException], ...],
    not_sent_codes: Iterable[str],
) -> dict[str, Any]:
    """Write one approved change with ``send(action)``. Blocking.

    ``error_types`` are the system's own error classes (with ``code``,
    ``detail`` and ``remedy``); one whose code is in ``not_sent_codes`` never
    reached the server, so the action goes back to ``proposed`` and Approve
    can be pressed again. Any other failure is final: ``failed``.
    """
    import db

    action = _load(action_id, system)
    if not enabled:
        raise ApplyError(409, disabled_message)
    if not db.move_integration_action(
        action_id, from_status="proposed", to_status="applying", at=time.time()
    ):
        current = db.get_integration_action(action_id) or {}
        raise ApplyError(409, f"Bu değişiklik zaten işlendi (durum: {current.get('status')}).")
    try:
        result = send(action)
    except error_types as exc:
        code = str(getattr(exc, "code", ""))
        message = str(getattr(exc, "detail", exc))
        remedy = str(getattr(exc, "remedy", "") or "")
        db.move_integration_action(
            action_id,
            from_status="applying",
            to_status="proposed" if code in set(not_sent_codes) else "failed",
            at=time.time(),
            error=message + (f" {remedy}" if remedy else ""),
        )
        logger.warning("%s action %s failed (%s): %s", system, action_id, code, message)
    except Exception as exc:  # noqa: BLE001 - recorded on the row, never left "applying"
        db.move_integration_action(
            action_id,
            from_status="applying",
            to_status="failed",
            at=time.time(),
            error=f"Beklenmeyen hata ({type(exc).__name__}).",
        )
        logger.exception("%s action %s crashed", system, action_id)
    else:
        db.move_integration_action(
            action_id, from_status="applying", to_status="applied", at=time.time(), result=result
        )
        logger.info("%s action %s applied", system, action_id)
    return public(db.get_integration_action(action_id) or action)


def cancel(action_id: str, *, system: str) -> dict[str, Any]:
    import db

    action = _load(action_id, system)
    if not db.move_integration_action(
        action_id, from_status="proposed", to_status="cancelled", at=time.time()
    ):
        raise ApplyError(409, f"Bu değişiklik artık iptal edilemez (durum: {action.get('status')}).")
    return public(db.get_integration_action(action_id) or action)


def actions_for_job(job_id: str) -> list[dict[str, Any]]:
    """Every proposal this job made, any system, oldest first."""
    import db

    return [public(a) for a in db.list_integration_actions(job_id)]


__all__ = ["ApplyError", "actions_for_job", "apply", "cancel", "public"]
