"""Prove LangGraph workforce path emits lg-* worker ids."""
from __future__ import annotations

import time

from execution.executor_config import agent_executor
from execution.langgraph_runtime.workforce import WORKER_IDS, run_workforce_in_thread
from model_config import UsageTracker


class FakeCB:
    def __init__(self) -> None:
        self.events: list = []
        self._loop = type("L", (), {})()

        def call_soon_threadsafe(fn, *args):
            fn(*args)

        self._loop.call_soon_threadsafe = call_soon_threadsafe  # type: ignore[attr-defined]
        self._queue = type("Q", (), {})()

        def put_nowait(ev):
            self.events.append(ev)

        self._queue.put_nowait = put_nowait  # type: ignore[attr-defined]


def main() -> None:
    print("executor=", agent_executor())
    print("WORKER_IDS=", WORKER_IDS)
    cb = FakeCB()
    handle_box: dict = {}

    def ready(h):
        handle_box["h"] = h
        print(
            "handle_type=",
            type(h).__name__,
            "has_stop=",
            hasattr(h, "stop_gracefully"),
        )

    t0 = time.time()
    result = run_workforce_in_thread(
        cb,
        UsageTracker(),
        None,
        "Reply with exactly: LANGGRAPH_WF_OK",
        "proof-job",
        ready,
        None,
        None,
        None,
        frozenset(),
        "Reply with exactly: LANGGRAPH_WF_OK",
        False,
        "none",
    )
    print("elapsed=", round(time.time() - t0, 2))
    print("result=", (result.result or "")[:200])
    print("state=", result.state.value)
    print("event_types=", [e.get("event_type") for e in cb.events])
    worker_ids = [e.get("worker_id") for e in cb.events if e.get("worker_id")]
    print("worker_ids=", worker_ids)
    print("roles=", [e.get("role") for e in cb.events if e.get("role")])
    assert any(
        wid and str(wid).startswith("lg-") for wid in worker_ids
    ), "NO lg- worker ids"
    assert type(handle_box["h"]).__name__ == "CancellableWorkforce"
    print("PROOF_OK_LANGGRAPH_WORKFORCE")


if __name__ == "__main__":
    main()
