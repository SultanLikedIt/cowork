"""``/integrations/confluence``: can this deployment read Confluence right now.

Read-only and small, like ``jira_api``. There is deliberately no endpoint that
sets a credential or the space allowlist: both belong in ``.env``
(docs/confluence.md), not in a request body on a stack without login.
"""

from __future__ import annotations

from typing import Any


def build_router() -> Any:
    import asyncio

    from fastapi import APIRouter

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

    return router
