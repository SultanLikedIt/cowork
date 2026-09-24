"""Regressions for the two blockers found in the final live smoke run.

BLOCKER 1 -- raw streaming protocol reached assistant-visible output:

    <｜DSML｜tool_calls>
    <｜DSML｜invoke name="web_read">
    <｜DSML｜parameter name="url">https://www.yayla.com

BLOCKER 2 (historical, pre-web_search/web_read split) -- the same run called
the old single web_acquire tool with model-invented URLs (https://www.yayla.com,
https://www.yayla.com.tr, a Trendyol search URL, a Migros search URL) embedded
in its free-text `need`, instead of letting search resolve the candidates.
That whole class of bug is now structural rather than a guard: web_search
takes no URL at all, and web_read's `url` is always an explicit argument the
model chooses from a web_search result (or the user's own text) -- there is
no free-text `need` blob left to smuggle a guessed URL into. What's still
tested below is source-pin enforcement, which is a real runtime check, not a
structural non-issue.

The replay test drives the real replay_final_answer_as_stream, and the tool
tests build tools through the real capabilities.resolve_tools, so both cover
the production path rather than a helper.

Run directly:  python test_live_blockers.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from unittest.mock import patch

_tmp_dir = tempfile.mkdtemp(prefix="agent_webapp_blockers_")
os.environ.setdefault("DATA_DIR", _tmp_dir)

from workforce.answer_format import replay_final_answer_as_stream  # noqa: E402
import capabilities  # noqa: E402
from html_extract import AcquiredPage, AcquisitionResult  # noqa: E402
from stream_protocol import sanitize_text  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


# ---------------------------------------------------------------------------
# The exact progressive sequence observed live.
#
# The provider emitted its tool call as ordinary content tokens, so the tool-call markup grows one fragment at a
# time -- including the "https" -> "https://www" -> "https://www.yayla.com"
# growth inside the url parameter that made complete-block regex stripping
# useless.
# ---------------------------------------------------------------------------
_OPEN = "I'll look that up.\n"
_CALL = (
    '<｜DSML｜tool_calls>\n'
    '<｜DSML｜invoke name="web_read">\n'
    '<｜DSML｜parameter name="url">'
)
_CLOSE = "</｜DSML｜tool_calls>"
_ANSWER = "Yayla freeze dried ürünleri: 4 doğrulanmış ürün bulundu."

LIVE_DSML_STREAM: list[str] = [
    _OPEN,
    _OPEN + "<",
    _OPEN + "<｜DSML｜",
    _OPEN + "<｜DSML｜tool_calls>",
    _OPEN + '<｜DSML｜tool_calls>\n<｜DSML｜invoke name="web_re',
    _OPEN + '<｜DSML｜tool_calls>\n<｜DSML｜invoke name="web_read">',
    _OPEN + _CALL,
    _OPEN + _CALL + "https",
    _OPEN + _CALL + "https://www",
    _OPEN + _CALL + "https://www.yayla.com",
    _OPEN + _CALL + "https://www.yayla.com</｜DSML｜parameter>",
    _OPEN + _CALL + "https://www.yayla.com</｜DSML｜parameter>" + _CLOSE,
    _OPEN + _CALL + "https://www.yayla.com</｜DSML｜parameter>" + _CLOSE + _ANSWER,
]

# Every substring below is protocol and must never reach the user.
PROTOCOL_FRAGMENTS = (
    "DSML",
    "｜",
    "tool_calls",
    "invoke",
    "parameter",
    "web_read",
    "yayla.com",
    "https",
    "<",
    ">",
)


def _assert_clean(label: str, emitted: str) -> None:
    leaked = [fragment for fragment in PROTOCOL_FRAGMENTS if fragment in emitted]
    check(f"{label}: zero protocol characters reach assistant output", not leaked, f"leaked {leaked} in {emitted!r}")


# ---------------------------------------------------------------------------
# BLOCKER 1 -- protocol markup is stripped before the user sees it
# ---------------------------------------------------------------------------


def test_tool_status_lines_are_filtered() -> None:
    check(
        "[TOOL RESULT]/[TOOL CALL] status lines stay internal",
        sanitize_text(
            "Before.\n[TOOL RESULT] web_read → {...}\n[TOOL CALL] x → {}\nAfter."
        )
        == "Before.\nAfter.",
        repr(sanitize_text("Before.\n[TOOL RESULT] web_read → {...}\n[TOOL CALL] x → {}\nAfter.")),
    )
    check(
        "Hermes-style <tool_call> blocks are still filtered",
        sanitize_text('A<tool_call>{"name":"x"}</tool_call>B') == "AB",
    )


def test_final_replay_path_is_filtered_by_the_same_rules() -> None:
    """Multi Agent replays an already-final string over the same pipe."""

    async def run() -> str:
        queue: "asyncio.Queue" = asyncio.Queue()
        await replay_final_answer_as_stream(
            LIVE_DSML_STREAM[-1], "job", queue
        )
        chunks = []
        while not queue.empty():
            chunks.append((await queue.get())["delta"])
        return "".join(chunks)

    replayed = asyncio.run(run())
    _assert_clean("replayed final answer", replayed)
    check(
        "the replayed answer keeps its real text",
        replayed == _OPEN + _ANSWER,
        repr(replayed),
    )


# ---------------------------------------------------------------------------
# BLOCKER 2 (historical, see the module docstring): with web_read(url) taking
# an explicit argument there is no free-text `need` blob left to smuggle a
# guessed URL into, so that whole class of bug is structural now. Source/
# location authorization was later removed from this tool entirely too (see
# web_tools.py's module docstring) -- relevance/authority/date/entity
# judgments are the agent's job, not a runtime check here. What's left worth
# testing at this layer is just the model-facing contract: tool names/shapes.
# ---------------------------------------------------------------------------


class _SearchFirstEngine:
    """Records what was searched and what was actually fetched."""

    def __init__(self, url: str = "https://yaylagida.com.tr/urunler") -> None:
        self.url = url
        self.searches: list[str] = []
        self.fetches: list[str] = []

    def search(self, query: str, **_kwargs):
        self.searches.append(query)
        return [
            {
                "url": self.url,
                "title": "Yayla freeze dried ürünleri",
                "content": "freeze dried ürün listesi ve fiyatları",
                "score": 1.0,
                "engines": ["test"],
            }
        ]

    def acquire(self, url: str, _need: str):
        self.fetches.append(url)
        return AcquisitionResult(
            pages=[
                AcquiredPage(
                    url=url,
                    status=200,
                    title="Yayla freeze dried",
                    text="freeze dried ürün listesi ve fiyatları",
                    records=[{"name": "freeze dried çorba", "price": "120 TL", "source_url": url}],
                )
            ]
        )


def _bind(engine: _SearchFirstEngine, root: str):
    """Bind web tools through the REAL resolve_tools, with a fake fetch engine.

    Only the Crawl4AI engine is substituted, so the tool names/schemas this
    checks are the production wiring rather than a reconstruction of it.
    """
    import web_tools as web_tools_module

    with patch.object(web_tools_module, "get_crawl4ai_engine", lambda: engine):
        context = capabilities.ToolContext(
            workspace_root=root,
            search_tool_factory=lambda: engine.search,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_FETCH}, context)
    search_tool = next(t for t in bound.tools if capabilities._tool_name(t) == "web_search")
    read_tool = next(t for t in bound.tools if capabilities._tool_name(t) == "web_read")
    return bound, search_tool, read_tool


def test_model_facing_schema_is_query_and_url_only() -> None:
    with tempfile.TemporaryDirectory() as root:
        engine = _SearchFirstEngine()
        bound, search_tool, read_tool = _bind(engine, root)

    check(
        "web_search takes only query and max_results",
        list(inspect.signature(search_tool).parameters) == ["query", "max_results"],
        f"got {list(inspect.signature(search_tool).parameters)}",
    )
    check(
        "web_read takes only url",
        list(inspect.signature(read_tool).parameters) == ["url"],
        f"got {list(inspect.signature(read_tool).parameters)}",
    )
    check(
        "web_search and web_read are the only model-facing web tools",
        {"web_search", "web_read"} <= set(bound.names)
        and "url_discovery" not in bound.names
        and "search_web" not in bound.names,
        str(sorted(bound.names)),
    )


def test_web_read_fetches_any_url_with_no_domain_judgment() -> None:
    """The new contract: web_read has no notion of an "authorized" domain --
    it fetches whatever URL it's given and lets the agent judge the source."""
    import web_tools as web_tools_module

    with tempfile.TemporaryDirectory() as root:
        engine = _SearchFirstEngine(url="https://unrelated-aggregator.example/prayer")
        # web_read looks up the engine at call time, so the fake must
        # still be patched when the tool actually runs.
        with patch.object(web_tools_module, "get_crawl4ai_engine", lambda: engine):
            _, _, read_tool = _bind(engine, root)
            result = read_tool(url=engine.url)

    check(
        "any URL is fetched, with no source/domain check in the way",
        engine.fetches == [engine.url] and bool(result.get("ok")),
        str(result)[:200],
    )


def _main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all blocker regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
