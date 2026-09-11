"""Shared read_text_file pagination regression checks."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from workspace import MAX_TOOL_READ_CHARS, ReadOnlyWorkspaceTools

CheckFn = Callable[[str, bool, str], None]


def run_read_text_file_pagination_checks(
    check: CheckFn,
    *,
    tmp_root: Path | None = None,
) -> None:
    """Exercise offset pagination on ReadOnlyWorkspaceTools.read_text_file."""
    owned = tmp_root is None
    root = tmp_root or Path(tempfile.mkdtemp(prefix="workspace_pagination_"))
    try:
        small_path = root / "small.txt"
        small_path.write_text("hello world", encoding="utf-8")
        tools = ReadOnlyWorkspaceTools(root)
        result = tools.read_text_file("small.txt")
        check(
            "small file fits in one page with no pagination footer",
            result == "hello world",
            repr(result),
        )

        total_len = 2 * MAX_TOOL_READ_CHARS + 5_000
        content = "".join(chr(ord("a") + (i % 26)) for i in range(total_len))
        large_path = root / "large.extracted.txt"
        large_path.write_text(content, encoding="utf-8")
        tools = ReadOnlyWorkspaceTools(root)

        first = tools.read_text_file("large.extracted.txt")
        check(
            "first page is capped at MAX_TOOL_READ_CHARS of content",
            first.startswith(content[:MAX_TOOL_READ_CHARS]),
            f"len(first)={len(first)}",
        )
        check(
            "first page tells the model how to continue",
            f"call again with offset={MAX_TOOL_READ_CHARS}" in first,
            first[-200:],
        )
        check(
            "first page does not falsely claim end of file",
            "End of file reached" not in first,
        )

        second = tools.read_text_file(
            "large.extracted.txt", offset=MAX_TOOL_READ_CHARS
        )
        expected_second_chunk = content[MAX_TOOL_READ_CHARS: 2 * MAX_TOOL_READ_CHARS]
        check(
            "continuation page starts exactly where the first page ended",
            second.startswith(expected_second_chunk),
            f"len(expected)={len(expected_second_chunk)}",
        )

        final_offset = 2 * MAX_TOOL_READ_CHARS
        final = tools.read_text_file("large.extracted.txt", offset=final_offset)
        expected_final_chunk = content[final_offset:total_len]
        check(
            "final page contains the remaining tail content",
            final.startswith(expected_final_chunk),
            f"tail_len={len(expected_final_chunk)}",
        )
        check(
            "final page reports end of file, not a continuation",
            "End of file reached" in final and "call again" not in final,
            final[-200:],
        )

        beyond = tools.read_text_file("large.extracted.txt", offset=total_len + 500)
        check(
            "offset beyond EOF returns a clear no-more-content message",
            "at or past end of file" in beyond and str(total_len) in beyond,
            beyond,
        )

        tools_dup = ReadOnlyWorkspaceTools(root)
        r1 = tools_dup.read_text_file("large.extracted.txt")
        r2 = tools_dup.read_text_file("large.extracted.txt")
        check(
            "second identical read (same path+offset+max_chars) carries a duplicate note",
            "already" in r2 and "read" in r2 and r1 != r2,
            r2[:200],
        )
        check(
            "first read of a given page is not flagged as duplicate",
            "already" not in r1,
            r1[:200],
        )

        tools_walk = ReadOnlyWorkspaceTools(root)
        reconstructed = []
        offset = 0
        calls = 0
        max_calls = (total_len // MAX_TOOL_READ_CHARS) + 3
        while calls < max_calls:
            page = tools_walk.read_text_file("large.extracted.txt", offset=offset)
            calls += 1
            if "call again with offset=" in page:
                body, _, footer = page.rpartition("\n[Read characters")
                reconstructed.append(body)
                next_offset = int(
                    footer.split("offset=")[1].split(" ")[0].rstrip(".]")
                )
                offset = next_offset
            else:
                body, _, _ = page.rpartition("\n[Read characters")
                reconstructed.append(body if body else page)
                break
        check(
            "large extracted-XLSX-style companion file traverses fully across multiple reads",
            "".join(reconstructed) == content,
            f"reconstructed_len={len(''.join(reconstructed))} expected={total_len} calls={calls}",
        )
        check(
            "traversal used more than one call (i.e. pagination actually engaged)",
            calls > 1,
            f"calls={calls}",
        )
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)
