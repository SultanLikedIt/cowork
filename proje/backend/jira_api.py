"""``/integrations/jira``: can this deployment read Jira right now, and the
approve / cancel buttons for a change a run proposed (jira_actions.py).

Small, like ``social_api``. There is deliberately no endpoint
that sets a credential: this stack has no authentication, and a Jira token
typed into it would sit in a request body and a log. It belongs in ``.env``
(docs/jira.md).
"""

from __future__ import annotations

from typing import Any


def build_router() -> Any:
    import asyncio

    from fastapi import APIRouter, HTTPException

    import jira_actions
    import jira_client

    router = APIRouter(prefix="/integrations/jira", tags=["jira"])

    @router.get("/status")
    async def jira_status():
        report = await asyncio.to_thread(jira_client.check_health)
        return report.as_dict()

    @router.post("/recheck")
    async def jira_recheck():
        """Probe again now, ignoring the 60-second cache -- for the moment right
        after someone connects the VPN or fixes a token."""
        jira_client.reset_health_cache()
        report = await asyncio.to_thread(jira_client.check_health, use_cache=False)
        return report.as_dict()

    # The approval card's two buttons (jira_actions.py). Only a change a run
    # already proposed and stored can be applied -- there is no endpoint that
    # takes fields to write, so nothing here can be turned into a way to
    # write arbitrary content into Jira.
    @router.post("/actions/{action_id}/apply")
    async def jira_action_apply(action_id: str):
        try:
            return await asyncio.to_thread(jira_actions.apply, action_id)
        except jira_actions.ApplyError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @router.post("/actions/{action_id}/cancel")
    async def jira_action_cancel(action_id: str):
        try:
            return await asyncio.to_thread(jira_actions.cancel, action_id)
        except jira_actions.ApplyError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    return router
