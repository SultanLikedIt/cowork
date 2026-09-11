"""LangGraph research must not search from conversation-bloated content."""

from __future__ import annotations

from execution.langgraph_runtime import workforce as lg_wf


def test_web_lookup_prefers_original_request() -> None:
    bloated = (
        "galatasaray ilk 11\n\n"
        "PRIOR MESSAGES IN THIS CONVERSATION (most recent tasks first "
        "below, but presented in chronological order):\n"
        "\n--- USER ---\nusd try kuru\n"
        "\n--- ASSISTANT ---\nNTV.com.tr döviz: USD 48\n"
    )
    got = lg_wf._web_lookup_text(
        "galatasarayin en son oynadigi macin ilk 11 ini bana getirebilir misin",
        bloated,
    )
    assert "döviz" not in got.casefold()
    assert "NTV" not in got
    assert "galatasaray" in got.casefold()


def test_web_lookup_strips_prior_when_original_missing() -> None:
    bloated = (
        "current ask only\n\n"
        "PRIOR MESSAGES IN THIS CONVERSATION (most recent tasks first "
        "below, but presented in chronological order):\n"
        "\n--- USER ---\nold fx question\n"
    )
    got = lg_wf._web_lookup_text("", bloated)
    assert got == "current ask only"
    assert "old fx" not in got


if __name__ == "__main__":
    test_web_lookup_prefers_original_request()
    test_web_lookup_strips_prior_when_original_missing()
    print("All langgraph web lookup checks passed")
