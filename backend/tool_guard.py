"""A no-progress guard: stop re-running a call that has already failed the
same way.

The failure this exists for
---------------------------
A live run made 32 tool calls, 14 of which failed -- the same tool, the same
arguments, the same error, over and over -- until a 25-page fetch budget was
exhausted and the job produced nothing. Every individual layer behaved
"correctly": the tool raised a real error, CAMEL caught it
(chat_agent.py:_execute_tool) and turned it into
``"Tool execution failed: ..."``, and the model, seeing a generic failure
string with nothing in it to suggest the call was hopeless, tried again.
Nothing anywhere counted how many times that exact call had already failed.

A global budget is the wrong instrument for this. It is sized for how much
real work a job may do, so it only stops a loop after the loop has spent the
whole job's allowance -- and the run then ends with the budget gone and the
question unanswered, which is exactly what happened.

Design
------
Deliberately tiny, and deliberately generic rather than per-capability: the
failure mode is a property of "model + tool loop", not of the web tools that
happened to expose it this time. One dict of counters, one wrapper, applied
uniformly at the single seam every capability's tools already pass through
(capabilities.resolve_tools).

Two decisions worth stating:

* The guard returns a structured result instead of raising. Raising would be
  converted by CAMEL into another ``"Tool execution failed: ..."`` string --
  indistinguishable from the transient failures the model is right to retry,
  which is the very ambiguity that produced the loop. A dict saying "this
  has failed N times identically, it will not succeed, do something else"
  is a different instruction, and the model can act on it.
* Only *consecutive identical* failures count. A different error for the same
  arguments means something changed, so the counter resets: a genuinely
  transient failure that succeeds on the second try is unaffected, and only
  a stuck call is stopped.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

# How many identical failures to allow before refusing to run the call again.
# Two, so an ordinary transient blip still gets its natural retry, and a
# deterministic failure costs at most one repeat before it is contained --
# far below any per-run budget, which is the point: the guard must trip long
# before the budget does, or it adds nothing the budget was not already doing.
MAX_IDENTICAL_FAILURES = 2


def _key(name: str, args: tuple, kwargs: dict) -> Any:
    try:
        return (name, args, json.dumps(kwargs, sort_keys=True, default=str))
    except TypeError:
        # Unserializable argument: no stable identity, so this call simply is
        # not guarded. Never a reason to fail it.
        return None


class NoProgressGuard:
    """Per-run tracker of repeated identical failures, shared across tools."""

    def __init__(self, max_identical_failures: int = MAX_IDENTICAL_FAILURES) -> None:
        self.max_identical_failures = max_identical_failures
        # key -> (consecutive count, the repr of the error seen each time)
        self._failures: dict[Any, tuple[int, str]] = {}

    def wrap(self, func: Callable) -> Callable:
        """Guard one tool. @wraps keeps __name__/__doc__/__wrapped__, which
        CAMEL's FunctionTool needs to build the tool schema from the real
        signature."""
        name = getattr(func, "__name__", func.__class__.__name__)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = _key(name, args, kwargs)
            if key is not None:
                count, error = self._failures.get(key, (0, ""))
                if count >= self.max_identical_failures:
                    return self._blocked(name, count, error)

            try:
                result = func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised; only counted here
                if key is not None:
                    seen = repr(exc)
                    count, previous = self._failures.get(key, (0, ""))
                    # Same error again -> no progress. A different one means
                    # the situation changed; start counting over.
                    self._failures[key] = (
                        (count + 1, seen) if seen == previous else (1, seen)
                    )
                raise
            if key is not None:
                self._failures.pop(key, None)
            return result

        return wrapper

    def _blocked(self, name: str, count: int, error: str) -> dict[str, Any]:
        return {
            "no_progress": True,
            "tool": name,
            "identical_failures": count,
            "last_error": error[:500],
            "instruction": (
                f"This exact {name} call (identical arguments) already failed "
                f"{count} times with the same error, so it was NOT run again. "
                "Repeating it cannot succeed. Change the arguments, use a "
                "different tool, or write your final answer from what you "
                "already have -- and say plainly which part of the request "
                "you could not complete."
            ),
        }


__all__ = ["MAX_IDENTICAL_FAILURES", "NoProgressGuard"]
