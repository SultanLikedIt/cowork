"""Execution facade: Camel (default) or LangGraph via AGENT_EXECUTOR.

Phase 2+: ``run_single_agent`` branches.
Phase 4+: ``run_workforce_in_thread`` branches.
Unknown AGENT_EXECUTOR values fall back to Camel.
"""

from __future__ import annotations

from typing import Any

from execution.executor_config import agent_executor


async def run_single_agent(*args: Any, **kwargs: Any) -> Any:
    if agent_executor() == "langgraph":
        from execution.langgraph_runtime.single import run_single_agent as impl

        return await impl(*args, **kwargs)
    import camel_service

    return await camel_service.run_single_agent(*args, **kwargs)


def run_workforce_in_thread(*args: Any, **kwargs: Any) -> Any:
    if agent_executor() == "langgraph":
        from execution.langgraph_runtime.workforce import (
            run_workforce_in_thread as impl,
        )

        return impl(*args, **kwargs)
    import camel_service

    # Dropped rather than forwarded: this is a LangGraph-only routing hint
    # (the document worker's template path), and camel_service's entrypoint
    # does not accept it. Silently ignoring it here keeps the Camel
    # fallback working instead of turning an unknown keyword into a
    # TypeError the moment AGENT_EXECUTOR is anything else.
    kwargs.pop("reuses_uploaded_document", None)
    kwargs.pop("opens_on_user_computer", None)
    # Same reason, and the same consequence worth knowing about: under
    # AGENT_EXECUTOR=camel there is no Mail Agent, so "read my mailbox"
    # quietly becomes an ordinary chat answer rather than an error.
    kwargs.pop("reads_email", None)
    return camel_service.run_workforce_in_thread(*args, **kwargs)
