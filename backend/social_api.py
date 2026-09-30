"""``/integrations/social``: what the UI shows about social reading.

Deliberately read-only and deliberately small. It answers two questions --
"can this deployment read Reddit right now" and "how close is that session
cookie to expiring" -- because in a container the cookie cannot refresh
itself, and the only person who can fix it is the one looking at this page.

There is no endpoint here that sets a credential. Pasting a session cookie
through the web app would put an account credential in an HTTP body, a
request log and a browser's memory, on a stack that has no authentication
at all; the file on disk is a better place for it, and the setup is
documented in docs/social-reach.md.
"""

from __future__ import annotations

from typing import Any


def build_router() -> Any:
    import asyncio

    from fastapi import APIRouter

    from social import PLATFORMS
    from social import health as social_health

    router = APIRouter(prefix="/integrations/social", tags=["social"])

    @router.get("/status")
    async def social_status():
        reports = await asyncio.to_thread(social_health.check_all)
        return {
            "platforms": [report.as_dict() for report in reports],
            # The UI badge only needs one boolean, and "any platform usable"
            # is the honest reading of it while Reddit is the only one.
            "ok": any(report.ok for report in reports),
            "configured": sorted(PLATFORMS),
        }

    @router.post("/recheck")
    async def social_recheck():
        """Probe again now, ignoring the 60-second cache.

        For the moment right after someone pastes a fresh cookie: waiting a
        minute to find out whether it worked is exactly when a user assumes
        it did not.
        """
        social_health.reset_cache()
        reports = await asyncio.to_thread(social_health.check_all)
        return {"platforms": [report.as_dict() for report in reports]}

    return router
