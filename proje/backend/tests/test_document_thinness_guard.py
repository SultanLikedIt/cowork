"""When a Document write-up is thin enough to throw away.

The guard exists for a real failure: a 21k-character research report came
back as 5k of tables under a stray subsection heading, with whole asked-for
sections missing, and replacing it with the notes was the better document.

What it must not do is fire on a good report, because the replacement costs
more than text. Swapping `content` breaks the fingerprint in
``document_model.resolve_payload_document``, the typed blocks are discarded,
and the PDF falls to the markdown writer -- reportlab, with no cover, no
accent headings and plain tables. The design is collateral damage from a
length comparison.

Measured on a live deep run: 46 typed blocks, 10,614 characters of markdown,
against 18,134 characters of notes. At a flat 0.6 ratio the bar was 10,880,
so the report lost by 266 characters and the user got an undesigned PDF.
"""

from __future__ import annotations

import pytest


def _floor(notes_chars: int, *, has_blocks: bool) -> float:
    """The thinness floor, mirroring the branch in _run_document_worker."""
    if has_blocks:
        return min(notes_chars * 0.35, 4_000)
    return notes_chars * 0.6


def test_the_live_failure_no_longer_trips_the_guard() -> None:
    """46 blocks / 10,614 chars against 18,134 chars of notes."""
    assert 10_614 >= _floor(18_134, has_blocks=True)
    # And it really did trip under the old flat ratio.
    assert 10_614 < 18_134 * 0.6


def test_a_genuinely_lost_brief_still_trips_it() -> None:
    """The failure the guard was written for: most of the report missing."""
    assert 2_000 < _floor(18_134, has_blocks=True)


def test_deep_mode_no_longer_raises_the_bar_without_limit() -> None:
    """More notes used to mean a higher bar for the write-up to clear, so
    better gathering made losing the design MORE likely."""
    small = _floor(8_000, has_blocks=True)
    huge = _floor(120_000, has_blocks=True)
    assert huge == 4_000
    assert small < huge
    # A 6k report survives both, where the flat ratio would fail the second.
    assert 6_000 >= huge
    assert 6_000 < 120_000 * 0.6


def test_without_blocks_the_original_ratio_is_unchanged() -> None:
    """No typed blocks means no design at stake, so nothing is being
    protected and the old behaviour is still the right one."""
    assert _floor(10_000, has_blocks=False) == 6_000
    assert 5_000 < _floor(10_000, has_blocks=False)


def test_the_guard_matches_the_implementation() -> None:
    """Pins the helper above to the real branch, so this file cannot drift
    into testing arithmetic nobody runs."""
    pytest.importorskip("langgraph")
    import inspect

    from execution.langgraph_runtime import workforce

    source = inspect.getsource(workforce._run_document_worker)
    assert "thinness_floor = min(len(notes_body) * 0.35, 4_000)" in source
    assert "thinness_floor = len(notes_body) * 0.6" in source
    assert "len(body) < thinness_floor" in source
