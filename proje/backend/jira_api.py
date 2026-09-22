"""``/integrations/jira``: can this deployment read Jira right now.

Read-only and small, like ``social_api``. There is deliberately no endpoint
that sets a credential: this stack has no authentication, and a Jira token
typed into it would sit in a request body and a log. It belongs in ``.env``
(docs/jira.md).
"""

from __future__ import annotations

from typing import Any


def build_router() -> Any:
    import asyncio

    from fastapi import APIRouter

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

    return router
