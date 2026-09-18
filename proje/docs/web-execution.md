# Web execution

Live web work exposes two model-facing tools: `web_search` (SearXNG
candidates) and `web_read` (open one URL). There is no combined
`web_acquire` tool. The agent chooses which search hit to open; this
layer does not rank, authorize, or rewrite sources.

Search is `web_search.py` against **AgentSearch** (preferred when
`AGENT_SEARCH_ENABLED=true`) with **SearXNG** as the shared engine backend
and fallback. See [agent-search.md](agent-search.md). Fetch is
`crawl4ai_engine.py`: a self-hosted Playwright/Chromium library -- no
external crawl API, service, or key. The engine runs in-process against
a locally installed browser binary, which is what makes it compatible
with an eventual airgapped deployment. A Playwright route hook
(`on_page_context_created`) applies public-network DNS/SSRF validation
(`web_security.py`) to every request the browser makes, including
redirect- and subresource-generated ones. Crawl depth and page count
are hard bounded.

This replaces the earlier Scrapy-based engine (`scrapy_engine.py`,
removed) and the single `web_acquire` tool. Scrapy never executed
JavaScript; Crawl4AI closes that gap by rendering the page.

Trafilatura extracts cleaned prose. Parsel/lxml handle HTML, XML,
links, and deterministic table extraction in `html_extract.py`.
Complete tables are saved to a server-side `data_path`; only bounded
previews and metadata enter model context. `transform_table` and the
XLSX/CSV/artifact pipeline consume that path directly.

JavaScript **is** executed (Crawl4AI/Playwright/Chromium). The bounded
`javascript_required` reason and its consent-banner/embedded-JSON-table
recovery heuristics remain in `html_extract.py` as defense-in-depth,
but are expected to fire far less often now that pages are actually
rendered rather than statically fetched.

Direct/Workforce routing, compact activity/SSE events, exact-call
memoization, and the per-agent search budget stay at the product
boundary. CAMEL `SearchToolkit` / `BrowserToolkit` are not bound.

## Deep research: survey vs iter

`depth="deep"` (the user's Deep research toggle, Workforce/LangGraph only)
has two strategies, picked by `DEEP_RESEARCH_MODE`:

- `survey` (default): plan 10-12 angles up front, one
  SEARCH→SELECT→FETCH per angle, coverage gap-fill, then one ANSWER call
  over every page trimmed to 1,200 characters (40 pages at most).
- `iter`: an IterResearch-style loop (`_acquire_iterative_evidence`).
  Each round the model sees only the question, its own report from the
  previous round and the last action's raw result, and returns
  `{think, report, action}`. The report is rewritten in full every round
  (sections: verified findings / open questions / contradictions / draft
  / confidence), and every finding cites an evidence id (`[E3]`). Actions:
  `search` (the model's query goes straight into the deterministic
  search→rerank→fetch round), `open` (a candidate from the last search
  only), `recall` (re-read an opened page by id, up to 8k characters),
  `finish` (refused before `ITER_MIN_SEARCHES` searches). Raw pages live
  in an evidence store outside the prompt, so the prompt stays the same
  size every round.

  Code checks the citations: ids that were never opened are removed, and
  a verified finding left without an id is dropped. ANSWER gets the final
  report plus only the pages it cites (and the last round's pages, which
  no report turn folded in), with evidence ids stripped from the reply.

Compare the two with `backend/scripts/deep_research_ab.py` (live model and
search; run inside the backend container).
