"""Task chains: a task that runs after another task's run finishes.

A downstream task has ``trigger_kind="after_task"`` and ``upstream_task_id``.
It never has a next_run_at of its own; the runner queues it when an upstream
run settles and ``condition_met`` holds (runner._trigger_downstream).
Several tasks may follow the same upstream (fan-out); waiting on several
upstreams (fan-in) is not supported.
"""

from __future__ import annotations

from typing import Any, Optional

from scheduling import config, store

CONDITIONS = ("success", "change", "always")
DEFAULT_CONDITION = "success"

# Run statuses that are not an outcome of the upstream work itself: nothing
# ran, so nothing downstream should react to them.
_NOT_AN_OUTCOME = frozenset({"skipped_overlap", "missed", "queued", "running", "skipped_condition"})

# Bounds for what a downstream run receives from its upstream.
MAX_UPSTREAM_CONTEXT_CHARS = 8000


class ChainError(ValueError):
    """An upstream link that is not allowed (cycle, depth, workspace)."""


def condition_of(task: dict[str, Any]) -> str:
    on = str((task.get("condition") or {}).get("on") or DEFAULT_CONDITION)
    return on if on in CONDITIONS else DEFAULT_CONDITION


def condition_met(
    condition: str,
    run_status: str,
    change_detected: Optional[bool],
    *,
    upstream_is_watch: bool,
    has_result: bool,
) -> bool:
    """Whether a downstream task runs after this upstream run.

    ``has_result``: the run produced a report. A research_diff watch that
    found nothing new still wrote one ("no_change" with a job); a url_hash
    watch that saw no change never started a job.
    """
    if run_status in _NOT_AN_OUTCOME:
        return False
    if condition == "always":
        return True
    if condition == "change":
        if upstream_is_watch:
            return change_detected is True
        # An upstream that does not watch for changes has no notion of one;
        # every finished report counts as new.
        return run_status in {"done", "partial"}
    return has_result and run_status in {"done", "partial", "no_change"}


def condition_reason(condition: str, run_status: str) -> str:
    if run_status in _NOT_AN_OUTCOME:
        return f"The upstream run did not produce a result ({run_status})."
    if condition == "change":
        return "The upstream run found no change."
    if run_status == "no_change":
        return "The upstream run found no change and produced no report."
    return f"The upstream run did not succeed ({run_status})."


def ancestors(task_id: str) -> list[dict[str, Any]]:
    """Upstream tasks from the direct parent to the chain root."""
    chain: list[dict[str, Any]] = []
    seen = {task_id}
    current = store.get_task(task_id)
    while current and current.get("upstream_task_id"):
        parent_id = current["upstream_task_id"]
        if parent_id in seen:  # corrupt data; never loop forever
            break
        seen.add(parent_id)
        current = store.get_task(parent_id)
        if current:
            chain.append(current)
    return chain


def chain_root_id(task_id: str) -> str:
    chain = ancestors(task_id)
    return chain[-1]["task_id"] if chain else task_id


def descendant_ids(task_id: str) -> list[str]:
    found: list[str] = []
    frontier = [task_id]
    seen = {task_id}
    while frontier:
        current = frontier.pop()
        for child in store.downstream_tasks(current):
            if child["task_id"] in seen:
                continue
            seen.add(child["task_id"])
            found.append(child["task_id"])
            frontier.append(child["task_id"])
    return found


def validate_upstream(task_id: Optional[str], upstream_id: str, workspace_id: str) -> dict[str, Any]:
    """Check a proposed link ``upstream_id -> task_id``; returns the upstream task.

    ``task_id`` is None for a task being created (it has no descendants yet).
    """
    upstream = store.get_task(upstream_id)
    if upstream is None:
        raise ChainError("The upstream task does not exist.")
    if upstream["workspace_id"] != workspace_id:
        raise ChainError("A task can only follow a task in the same workspace.")
    if task_id is not None:
        if upstream_id == task_id or upstream_id in descendant_ids(task_id):
            raise ChainError("That link would make the chain loop back on itself.")
    # Depth of the new task = upstream's depth + 1, plus the deepest subtree
    # hanging off the task being edited.
    new_depth = len(ancestors(upstream_id)) + 1 + (_subtree_height(task_id) if task_id else 0)
    if new_depth > config.max_chain_depth():
        raise ChainError(
            f"Chains can be at most {config.max_chain_depth()} tasks deep after the first one."
        )
    return upstream


def _subtree_height(task_id: str) -> int:
    children = store.downstream_tasks(task_id)
    if not children:
        return 0
    return 1 + max(_subtree_height(child["task_id"]) for child in children)


def chain_member_ids(task_id: str) -> list[str]:
    root = chain_root_id(task_id)
    return [root, *descendant_ids(root)]
