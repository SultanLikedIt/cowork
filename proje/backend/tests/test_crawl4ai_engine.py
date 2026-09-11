"""Crawl4AI shared-browser crash recovery.

Before this fix, a crashed/IPC-broken Chromium (real symptoms seen in
production logs: "Invalid InterceptionId", "pipe closed by peer") kept
being reused by every subsequent acquire() call forever -- every fetch
failed as a false "network error" until someone restarted the whole
backend process by hand. These checks pin the classification and the
identity-guarded runtime invalidation that fixes that.
"""

import crawl4ai_engine as engine

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def test_dead_browser_markers_are_recognized() -> None:
    for message in [
        "Error: Invalid InterceptionId",
        "Target page, context or browser has been closed",
        "Target closed",
        "Browser has been closed",
        "pipe closed by peer or os.write(pipe, data) raised exception",
    ]:
        check(
            f"recognized as a dead-browser signature: {message!r}",
            engine._looks_like_dead_browser(message),
            message,
        )


def test_ordinary_page_errors_are_not_treated_as_dead_browser() -> None:
    for message in [
        "net::ERR_CONNECTION_TIMED_OUT",
        "404 Not Found",
        "DNS lookup failed",
    ]:
        check(
            f"NOT treated as a dead-browser signature: {message!r}",
            not engine._looks_like_dead_browser(message),
            message,
        )


def test_classified_runtime_error_flags_browser_crashed() -> None:
    err = engine._classified_runtime_error("Error: Invalid InterceptionId")
    check("reason is browser_crashed", err.reason == "browser_crashed", err.reason)
    check("browser_crashed is retryable", err.retryable is True, err.retryable)

    transient = engine._classified_runtime_error("net::ERR_CONNECTION_TIMED_OUT")
    check(
        "an ordinary transient error keeps its own reason",
        transient.reason == "network_failure",
        transient.reason,
    )


def test_invalidate_runtime_only_clears_the_dead_instance() -> None:
    Cls = engine.Crawl4AIAcquisitionEngine
    saved = Cls._runtime
    try:
        dead = object()
        Cls._runtime = dead
        Cls._invalidate_runtime(dead)
        check(
            "the dead instance is cleared so the next acquire() rebuilds it",
            Cls._runtime is None,
            Cls._runtime,
        )

        current = object()
        stale = object()
        Cls._runtime = current
        Cls._invalidate_runtime(stale)
        check(
            "a stale/already-replaced instance does not clobber the live one",
            Cls._runtime is current,
            Cls._runtime,
        )
    finally:
        Cls._runtime = saved


def main() -> None:
    test_dead_browser_markers_are_recognized()
    test_ordinary_page_errors_are_not_treated_as_dead_browser()
    test_classified_runtime_error_flags_browser_crashed()
    test_invalidate_runtime_only_clears_the_dead_instance()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All crawl4ai engine checks passed")


if __name__ == "__main__":
    main()
