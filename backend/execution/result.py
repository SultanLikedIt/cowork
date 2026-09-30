"""Execution result object main.py reads for every job."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def execution_result(
    *,
    task_id: str,
    content: str,
    result: str,
    state: str = "DONE",
) -> Any:
    """Object with ``.id``, ``.content``, ``.result``, ``.state.value``.

    ``main._run_job`` checks ``result_task.state.value == "DONE"`` and reads
    ``result_task.result``.
    """
    return SimpleNamespace(
        id=task_id,
        content=content,
        result=result,
        state=SimpleNamespace(value=state),
    )
