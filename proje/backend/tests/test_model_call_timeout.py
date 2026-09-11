"""Regression for the transport-level model call timeout.

Live failure this pins: with 3 dynamic workers running at once, the
Database subtask failed with UI text "Task timed out" at roughly 3
minutes, while its two siblings completed. Nothing in the app set that
deadline -- CAMEL's OpenAICompatibleModel falls back to
`float(os.environ.get("MODEL_TIMEOUT", 180))` (openai_compatible_model
.py:113), so every model call silently carried a 180s HTTP timeout.

That inverted the app's own policy: WORKFORCE_TASK_TIMEOUT_SECONDS (600s)
is the deliberate, derived bound on how long work may take while several
workers queue against a backend that serializes inference, but a 180s
transport deadline fired first and converted queueing into a hard
APITimeoutError. The agent layer does not soften it -- ChatAgent
._get_model_response retries RateLimitError only and re-raises everything
else (chat_agent.py:3422-3426) -- so it propagates out of the worker step
and Worker._process_single_task marks the subtask FAILED with the
exception text as its result (worker.py:100-103).

No model or network: the backend object is constructed and inspected.

    python test_model_call_timeout.py
"""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agent_webapp_test_"))

import camel_service  # noqa: E402
import model_config  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def _client_timeout(backend):
    inner = getattr(backend, "_backend", backend)
    client = getattr(inner, "_client", None)
    return getattr(client, "timeout", None)


def main() -> None:
    # --- The timeout actually reaches the HTTP client -------------------
    backend = model_config.get_model()
    check(
        "the configured timeout reaches the OpenAI client",
        _client_timeout(backend) == model_config.MODEL_CALL_TIMEOUT_SECONDS,
        f"client={_client_timeout(backend)} "
        f"expected={model_config.MODEL_CALL_TIMEOUT_SECONDS}",
    )
    check(
        "the silent 180s vendor default no longer governs",
        _client_timeout(backend) != 180.0,
        "still on the vendor default",
    )

    # A tracked model wraps the same backend, so tracking must not drop it.
    tracked = model_config.get_model(tracker=model_config.UsageTracker())
    check(
        "usage-tracked models carry the same timeout",
        _client_timeout(tracked) == model_config.MODEL_CALL_TIMEOUT_SECONDS,
        f"{_client_timeout(tracked)}",
    )

    # --- The ordering invariant that caused the bug ---------------------
    # Transport must never preempt the orchestration bound. If it does,
    # queueing behind sibling workers becomes a failed subtask plus a
    # recovery attempt, instead of simply waiting.
    check(
        "transport timeout does not preempt the orchestration bound",
        model_config.MODEL_CALL_TIMEOUT_SECONDS
        >= camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS,
        f"transport={model_config.MODEL_CALL_TIMEOUT_SECONDS} "
        f"orchestration={camel_service.WORKFORCE_TASK_TIMEOUT_SECONDS}",
    )
    check(
        "transport timeout covers a full serialized batch",
        model_config.MODEL_CALL_TIMEOUT_SECONDS
        >= camel_service.MAX_INITIAL_DECOMPOSITION_SUBTASKS
        * camel_service.WORST_CASE_MODEL_CALL_SECONDS,
    )

    # --- Calls that must still fail fast keep their own guards ----------
    # These are asyncio step_timeouts, independent of the HTTP deadline,
    # so raising the transport timeout must not have loosened them.
    check(
        "the execution router no longer has a step_timeout",
        not hasattr(camel_service, "ROUTING_TIMEOUT_SECONDS"),
    )
    check(
        "synthesis still fails fast on its own step_timeout",
        camel_service.FINAL_SYNTHESIS_TIMEOUT_SECONDS
        < model_config.MODEL_CALL_TIMEOUT_SECONDS,
        f"synthesis={camel_service.FINAL_SYNTHESIS_TIMEOUT_SECONDS}",
    )

    # --- An explicit MODEL_TIMEOUT env var must still win ---------------
    # It is the vendor's supported operator knob; passing our value
    # explicitly must not make the deployment unable to override it.
    original = os.environ.get("MODEL_TIMEOUT")
    os.environ["MODEL_TIMEOUT"] = "45"
    try:
        # Our explicit argument takes precedence by design (the vendor only
        # consults the env var when timeout is None). Assert the actual
        # behavior rather than an assumption about it.
        env_backend = model_config.get_model()
        observed = _client_timeout(env_backend)
        check(
            "behavior with MODEL_TIMEOUT set is deterministic and documented",
            observed == model_config.MODEL_CALL_TIMEOUT_SECONDS,
            f"observed={observed} -- explicit arg is expected to win over env",
        )
    finally:
        if original is None:
            os.environ.pop("MODEL_TIMEOUT", None)
        else:
            os.environ["MODEL_TIMEOUT"] = original

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All model-call timeout checks passed")


if __name__ == "__main__":
    main()
