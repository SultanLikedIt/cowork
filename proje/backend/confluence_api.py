"""``/integrations/confluence``: can this deployment read Confluence right now,
and the approve / cancel buttons for a change a run proposed
(confluence_actions.py).

Small, like ``jira_api``. There is deliberately no endpoint that
sets a credential or the space allowlist: both belong in ``.env``
(docs/confluence.md), not in a request body on a stack without login.
"""

from __future__ import annotations

from typing import Any


def build_router() -> Any:
    import asyncio

    from fastapi import APIRouter, HTTPException

    import confluence_actions
    import confluence_client

    router = APIRouter(prefix="/integrations/confluence", tags=["confluence"])

    @router.get("/status")
    async def confluence_status():
        report = await asyncio.to_thread(confluence_client.check_health)
        return report.as_dict()

    @router.post("/recheck")
    async def confluence_recheck():
        """Probe again now, ignoring the 60-second cache."""
        confluence_client.reset_health_cache()
        report = await asyncio.to_thread(confluence_client.check_health, use_cache=False)
        return report.as_dict()

    # The approval card's two buttons. Like Jira's, they take the id of a
    # change a run already proposed and stored -- never content to write.
    @router.post("/actions/{action_id}/apply")
    async def confluence_action_apply(action_id: str):
        try:
            return await asyncio.to_thread(confluence_actions.apply, action_id)
        except confluence_actions.ApplyError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @router.post("/actions/{action_id}/cancel")
    async def confluence_action_cancel(action_id: str):
        try:
            return await asyncio.to_thread(confluence_actions.cancel, action_id)
        except confluence_actions.ApplyError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    return router
