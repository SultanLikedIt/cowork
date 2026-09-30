"""Strip tool-call protocol markup from finished model text.

Tool calls are supposed to arrive in the provider's STRUCTURED channel
(``delta.tool_calls``). Some providers instead emit them as ordinary CONTENT
tokens -- DeepSeek's DSML dialect is the one observed live:

    <｜DSML｜tool_calls>
    <｜DSML｜invoke name="web_read">
    <｜DSML｜parameter name="url">https://www.yayla.com

Once that markup is in the content channel it is, as far as every layer
downstream is concerned, assistant text, and would reach the user's screen.
``sanitize_text`` removes every known protocol block (and everything after an
unterminated one) from a complete piece of text before it is shown.
"""

from __future__ import annotations

import re

__all__ = [
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
    # Line-terminated tool status lines ("[TOOL RESULT] web_read -> ...")
    # that agent frameworks write into the content channel.
    ("[tool result]", ("\n",)),
    ("[tool call]", ("\n",)),
)


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


def sanitize_text(text: str) -> str:
    """Strip protocol markup from a COMPLETE (non-streaming) piece of text.

    Used for already-finished text -- a synthesized final answer, a replayed
    result -- where nothing further can arrive, so a trailing fragment is
    ordinary text rather than a possible marker.
    """
    return split_visible(text or "")[0]
