"""Protocol-aware filtering of streamed model text, at the stream boundary.

Why this exists
---------------
Tool calls are supposed to arrive in the provider's STRUCTURED channel
(``delta.tool_calls``), which CAMEL accumulates separately from text and
never puts into ``msg.content``. Some providers instead emit tool calls as
ordinary CONTENT tokens -- DeepSeek's DSML dialect is the one observed live:

    <｜DSML｜tool_calls>
    <｜DSML｜invoke name="web_read">
    <｜DSML｜parameter name="url">https://www.yayla.com

Once that markup is in the content channel it is, as far as every layer
downstream is concerned, assistant text. It reaches the token_delta pipe, the
SSE snapshot and the user's screen.

Why regex post-processing is the wrong abstraction
--------------------------------------------------
The markup arrives one token at a time, so at various instants the cumulative
text ends mid-marker::

    ...name="url">https
    ...name="url">https://www
    ...name="url">https://www.yayla.com

A regex that strips COMPLETE blocks after accumulation cannot help here: at
each of those instants there is no complete block to match, so the fragment is
emitted, and the user sees protocol characters. Worse, when the block finally
completes and the regex does match, the cumulative visible text SHRINKS, which
looks like the stream resetting.

What this module does instead
-----------------------------
It is a small state machine over the text stream that classifies text as
either assistant-visible or protocol BEFORE anything is accumulated or
emitted, and that never emits a character it might later have to retract:

  * text inside a protocol block is discarded as protocol;
  * an unterminated block suppresses everything after its opening marker,
    however many chunks that spans;
  * a trailing fragment that could still grow into an opening marker is held
    back until the next chunk proves what it is.

Because the decision is made before emission, no downstream layer needs to
know protocol markup exists.
"""

from __future__ import annotations

import re

__all__ = [
    "StreamTextFilter",
    "sanitize_text",
    "split_visible",
]


def _ascii_lower(value: str) -> str:
    """Lowercase ASCII letters only, preserving length exactly.

    ``str.lower()``/``casefold()`` can CHANGE THE LENGTH of non-ASCII text --
    Turkish "İ" lowercases to two code points -- which would corrupt the
    offsets this module relies on. Every protocol marker below is ASCII plus
    a few case-stable symbols, so folding just the ASCII range is both
    sufficient and safe on the Turkish content this app handles.
    """
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in value)


# (opening marker, markers that end the block). Matching is case-insensitive.
#
# A block whose opener is seen but whose closer is not suppresses everything
# from the opener onward, so a tool-call section that runs to the end of the
# message never leaks even a single character.
_PROTOCOL_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # DeepSeek DSML, the dialect observed leaking live. Every variant
    # (tool_calls / invoke / parameter) shares one opening marker, so a single
    # entry covers the whole family including ones not yet seen.
    (
        "<｜dsml｜",
        ("<｜dsml｜tool_calls_end｜>", "</｜dsml｜tool_calls>", "<｜dsml｜end｜>"),
    ),
    # Same dialect from providers that use an ASCII pipe instead of U+FF5C.
    (
        "<|dsml|",
        ("<|dsml|tool_calls_end|>", "</|dsml|tool_calls>", "<|dsml|end|>"),
    ),
    ("<｜tool▁calls▁begin｜>", ("<｜tool▁calls▁end｜>",)),
    # Hermes/Qwen style, already seen before this module existed.
    ("<tool_call", ("</tool_call>",)),
    ("<function_call", ("</function_call>",)),
    # CAMEL's OWN accumulator status lines (chat_agent.py) -- internal
    # telemetry that is concatenated into msg.content and is line-terminated.
    ("[tool result]", ("\n",)),
    ("[tool call]", ("\n",)),
)

_MAX_OPENER_LEN = max(len(opener) for opener, _ in _PROTOCOL_BLOCKS)

# Searched against the ORIGINAL string (never a case-folded copy) so match
# offsets always line up with the text being split.
_OPENER_RE = re.compile(
    "|".join(re.escape(opener) for opener, _ in _PROTOCOL_BLOCKS),
    re.IGNORECASE,
)
_CLOSER_RE_BY_OPENER = {
    opener: re.compile(
        "|".join(re.escape(closer) for closer in closers), re.IGNORECASE
    )
    for opener, closers in _PROTOCOL_BLOCKS
}


def split_visible(text: str) -> tuple[str, bool]:
    """Split ``text`` into assistant-visible content and protocol.

    Returns ``(visible_text, inside_unterminated_block)``. When the flag is
    true an opening marker was found with no closing marker, so everything
    after it is withheld -- it is either protocol still arriving, or a
    tool-call section that runs to the end of the message.
    """
    if not text:
        return "", False

    visible: list[str] = []
    position = 0
    while True:
        opener_match = _OPENER_RE.search(text, position)
        if opener_match is None:
            visible.append(text[position:])
            return "".join(visible), False

        visible.append(text[position : opener_match.start()])
        closer_re = _CLOSER_RE_BY_OPENER[_ascii_lower(opener_match.group(0))]
        closer_match = closer_re.search(text, opener_match.end())
        if closer_match is None:
            return "".join(visible), True
        position = closer_match.end()


def _held_fragment_len(text: str) -> int:
    """Length of a trailing fragment that could still become an opener.

    ``"...<to"`` must not be emitted: the next chunk may turn it into
    ``"<tool_call"``. Holding it costs one chunk of latency and is what makes
    emission irreversible.
    """
    limit = min(len(text), _MAX_OPENER_LEN - 1)
    for size in range(limit, 0, -1):
        tail = _ascii_lower(text[-size:])
        for opener, _ in _PROTOCOL_BLOCKS:
            if len(opener) > size and opener.startswith(tail):
                return size
    return 0


def sanitize_text(text: str) -> str:
    """Strip protocol markup from a COMPLETE (non-streaming) piece of text.

    Used for already-finished text -- a synthesized final answer, a replayed
    result -- where nothing further can arrive, so a trailing fragment is
    ordinary text rather than a possible marker.
    """
    return split_visible(text or "")[0]


class StreamTextFilter:
    """One stream's protocol state. Emits only text that can never be retracted.

    CAMEL yields CUMULATIVE content, so ``feed_cumulative`` is the primary
    entry point; ``feed_delta`` is provided for callers that receive
    increments. Both return the newly-visible text to emit, which is ``""``
    whenever the stream is currently inside -- or possibly entering -- a
    protocol block.
    """

    def __init__(self) -> None:
        self._cumulative = ""
        self._emitted = ""

    @property
    def visible_text(self) -> str:
        """Everything visible so far, protocol removed."""
        return sanitize_text(self._cumulative)

    def feed_cumulative(self, cumulative: str) -> str:
        self._cumulative = cumulative or ""
        visible, inside_block = split_visible(self._cumulative)
        if not inside_block:
            held = _held_fragment_len(visible)
            if held:
                visible = visible[:-held]
        return self._advance(visible)

    def feed_delta(self, delta: str) -> str:
        return self.feed_cumulative(self._cumulative + (delta or ""))

    def flush(self) -> str:
        """Emit anything still held once the stream is known to be finished.

        A fragment held back as a possible opening marker can no longer become
        one, so it is released as ordinary text. An opened protocol block stays
        suppressed: it never was assistant text.
        """
        return self._advance(split_visible(self._cumulative)[0])

    def _advance(self, visible: str) -> str:
        if visible == self._emitted:
            return ""
        if visible.startswith(self._emitted):
            delta = visible[len(self._emitted) :]
            self._emitted = visible
            return delta
        if self._emitted.startswith(visible):
            # Cumulative text shrank. CAMEL clears its streaming buffer
            # between tool iterations, so this is expected; the user keeps
            # what they already have and the next chunk grows past it.
            return ""
        # The provider replaced the message wholesale. Preserve the
        # pre-existing behaviour of showing the replacement.
        self._emitted = visible
        return visible
