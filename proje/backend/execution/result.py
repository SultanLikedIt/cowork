"""Framework-agnostic execution result shaped like CAMEL Task for main.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def execution_result(
    *,
    task_id: str,
    content: str,
    result: str,
    state: str = "DONE",
    verbatim: bool = False,
) -> Any:
    """Object with ``.id``, ``.content``, ``.result``, ``.state.value``.

    ``main._run_job`` checks ``result_task.state.value == "DONE"`` and reads
    ``result_task.result``. Camel ``Task`` satisfies that; LangGraph paths
    return this lightweight stand-in so main stays framework-agnostic.

    ``verbatim`` marks a result that must reach the user unchanged -- the
    Coding Agent's reply quotes the code that ran and its real output, which
    main's restating synthesis step would otherwise rewrite.
    """
    return SimpleNamespace(
        id=task_id,
        content=content,
        result=result,
        state=SimpleNamespace(value=state),
        verbatim=verbatim,
    )
