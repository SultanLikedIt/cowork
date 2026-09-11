"""OpenBot AG-UI bridge — expose LangGraph web research as a remote coworker.

The bridge is opt-in (``OPENBOT_AGUI_ENABLED``) and its ``ag-ui-protocol``
dependency is installed only in the Docker image. Importing ``openbot.router``
at module scope therefore took the whole backend down on any environment that
did not ship the optional package, even though the feature was switched off.
Resolution stays lazy: nothing from ``ag_ui`` is imported until the bridge is
actually enabled, and a missing package then fails loudly as the
misconfiguration it is.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

__all__ = ["mount_openbot_routes"]


def _bridge_enabled() -> bool:
    return os.environ.get(
        "OPENBOT_AGUI_ENABLED", "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}


def mount_openbot_routes(app) -> None:
    """Register the AG-UI routes when the bridge is enabled.

    No-op when disabled, without importing the optional dependency.
    """
    if not _bridge_enabled():
        return
    try:
        from openbot.router import mount_openbot_routes as _mount
    except ImportError:
        logger.exception(
            "OPENBOT_AGUI_ENABLED is set but the AG-UI bridge cannot be "
            "imported; install the 'ag-ui-protocol' package or unset the "
            "flag. Continuing with the bridge disabled."
        )
        return
    _mount(app)
