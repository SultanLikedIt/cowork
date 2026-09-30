"""Reading public social platforms through pinned upstream CLIs.

Layering, because the split is the security design and not a preference:

    registry.py  -- which commands may exist; validates every argument
    runner.py    -- the process boundary: scrubbed env, no shell, bounds
    parse.py     -- upstream JSON -> SocialRecord, two shapes per platform
    health.py    -- installed? authenticated? how stale is the cookie?
    (caller)     -- untrusted_content.render before any of it reaches a model

Nothing here formats text for a prompt. ``SocialRecord.to_quoted_item`` is
the seam, and it goes through ``untrusted_content``.
"""

from .registry import (
    ArgumentError,
    Command,
    PLATFORMS,
    Platform,
    SocialRecord,
    build,
    platform_for_url,
)
from .health import HealthReport, check, check_all
from .runner import RunResult, run, social_home

# `parse` is deliberately NOT re-exported here. The submodule is named
# `parse` and its main function is also `parse`, so `from .parse import
# parse` rebinds the package attribute from the module to the function --
# and whether a caller then gets one or the other depends on import order,
# which made a whole test file pass alone and fail in a suite. Callers use
# `from social import parse` and get the module.

__all__ = [
    "ArgumentError",
    "HealthReport",
    "Command",
    "PLATFORMS",
    "Platform",
    "RunResult",
    "SocialRecord",
    "build",
    "check",
    "check_all",
    "platform_for_url",
    "run",
    "social_home",
]
