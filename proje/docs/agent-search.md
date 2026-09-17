# AgentSearch sidecar (brcrusoe72/agent-search)


Integration mode: **blend**, not replace. SearXNG stays the discovery baseline; AgentSearch adds multi-engine/news strategies on top. Deep research still uses the survey controller on top of this search layer.
Self-hosted search API in front of our SearXNG: multi-engine strategies,
scoring, and dedupe. Agents still only see `web_search` / `web_read`; this
is an internal discovery backend.

Upstream: https://github.com/brcrusoe72/agent-search  
Vendored as git submodule: `vendor/agent-search`

## Architecture

```
web_search (model tool / controller search_raw)
    → SearXNG :8080 general (+ news for newsy asks)   always, waited for
    → AgentSearch :3939 news (newsy asks)              started at the same time,
      AgentSearch :3939 general (only if                waited AGENT_SEARCH_GRACE_SECONDS
        AGENT_SEARCH_GENERAL_MODE != off)               past SearXNG, then left out
web_read
    → Crawl4AI            (unchanged page open)
```

All requests of one search start concurrently (`web_search._SEARCH_POOL`).
AgentSearch is additive: a slow or failing sidecar costs at most the grace
period, never the search.

`AGENT_SEARCH_GENERAL_MODE` defaults to `off`. AgentSearch's own `general`
strategy queries Bing/DuckDuckGo/Brave through the same SearXNG instance,
so it duplicated work: on six measured Turkish/English queries 0–3 of its 10
URLs were new, mostly off-topic. `reference` (Wikipedia + Wikidata) returned
0–1 hits. Measure with `backend/scripts/latency_bench.py search` before
turning either back on.

We reuse **our** SearXNG container (`SEARXNG_URL=http://searxng:8080`). We do
**not** start AgentSearch’s bundled SearXNG, so engine config stays in
`searxng/settings.yml`.

## Setup

```bash
git submodule update --init --recursive
docker compose up -d --build agent-search backend
```

Smoke:

```bash
curl -s http://localhost:3939/health
curl -s 'http://localhost:3939/search?q=turkey+food+export&count=5&mode=general' | head
```

Disable without removing the container:

```bash
AGENT_SEARCH_ENABLED=false docker compose up -d backend
```

## Env

| Variable | Default (compose) | Notes |
|---|---|---|
| `AGENT_SEARCH_ENABLED` | `true` | Prefer sidecar in `search_web` |
| `AGENT_SEARCH_BASE_URL` | `http://agent-search:3939` | In-network; host: `http://localhost:3939` |
| `AGENT_SEARCH_TOKEN` | empty | Optional bearer; set on both api + backend |
| `AGENT_SEARCH_TIMEOUT_SECONDS` | `20` | HTTP timeout for discovery |
| `AGENT_SEARCH_GRACE_SECONDS` | `1.5` | How long past SearXNG the sidecar is waited for |
| `AGENT_SEARCH_GENERAL_MODE` | `off` | `off` \| `reference` \| `general` for general-category searches |

## Cursor MCP (optional)

AgentSearch ships an MCP server under `vendor/agent-search/mcp-server/`.
Point it at the running API if you want Cursor tools to call the same
search stack (separate from LangGraph `web_search`):

```json
{
  "mcpServers": {
    "agent-search": {
      "command": "python",
      "args": ["vendor/agent-search/mcp-server/server.py"],
      "env": {
        "AGENT_SEARCH_URL": "http://127.0.0.1:3939",
        "AGENT_SEARCH_TOKEN": ""
      }
    }
  }
}
```

See `vendor/agent-search/mcp-server/README.md` for the full tool list.

## Notes

- `fetch=true` on AgentSearch is **not** used by our backend: page content
  stays on Crawl4AI via `web_read`.
- Category → strategy: `general`→`general`, `news`→`news`,
  `science`/`academic`→`academic`, `it`/`code`→`code`.
- Empty, late or error responses from the sidecar are simply left out; SearXNG results always come through.