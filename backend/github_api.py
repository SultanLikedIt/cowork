"""``/integrations/github``: can this deployment read GitHub right now.

Read-only and small, like ``jira_api``. There is deliberately no endpoint
that sets the token: this stack has no authentication, and a token typed
into it would sit in a request body and a log. It belongs in ``.env``
(docs/github.md).
"""

from __future__ import annotations

from typing import Any


def build_router() -> Any:
    import asyncio

    from fastapi import APIRouter

    import github_client

    router = APIRouter(prefix="/integrations/github", tags=["github"])

    @router.get("/status")
    async def github_status():
        report = await asyncio.to_thread(github_client.check_health)
        return report.as_dict()

    @router.post("/recheck")
    async def github_recheck():
        """Probe again now, ignoring the 60-second cache -- for the moment
        right after a token is added or a rate limit resets."""
        github_client.reset_health_cache()
        report = await asyncio.to_thread(github_client.check_health, use_cache=False)
        return report.as_dict()

    return router
