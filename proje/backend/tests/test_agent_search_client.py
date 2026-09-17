"""AgentSearch client + web_search routing."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import agent_search_client
import web_search


def test_agent_search_normalizes_hits() -> None:
    payload = {
        "results": [
            {
                "title": "TGDF",
                "url": "https://example.com/a",
                "snippet": "export 12.3 milyar",
                "engines": ["google", "brave"],
                "score": 2.0,
                "position": 1,
            }
        ],
        "meta": {"query": "gıda", "total": 1, "engines_used": ["google"]},
    }

    with patch.object(agent_search_client.requests, "get") as get:
        response = get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        hits = agent_search_client.search("gıda ihracat", limit=5, categories="general")

    assert len(hits) == 1
    assert hits[0]["url"] == "https://example.com/a"
    assert hits[0]["content"] == "export 12.3 milyar"
    assert hits[0]["description"] == "export 12.3 milyar"
    assert hits[0]["source"] == "agent-search"
    assert hits[0]["mode"] == "general"
    assert hits[0]["engines"] == ["google", "brave"]


def test_agent_search_maps_news_category_to_mode() -> None:
    with patch.object(agent_search_client.requests, "get") as get:
        response = get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {"results": []}
        agent_search_client.search("q", categories="news", limit=3)
        params = get.call_args.kwargs["params"]
        assert params["mode"] == "news"
        assert params["count"] == 3


def test_search_web_blends_agent_search_with_searxng() -> None:
    agent_hits: List[Dict[str, Any]] = [
        {
            "result_id": 1,
            "title": "A",
            "url": "https://a.example/",
            "content": "a",
            "description": "a",
            "source": "agent-search",
        }
    ]
    news_hits: List[Dict[str, Any]] = [
        {
            "result_id": 1,
            "title": "N",
            "url": "https://n.example/",
            "content": "n",
            "description": "n",
            "source": "agent-search",
        }
    ]
    searx_hits = [
        {
            "result_id": 1,
            "title": "S",
            "url": "https://s.example/",
            "content": "s",
            "description": "s",
        }
    ]

    def fake_agent(query, *, categories, limit):
        return news_hits if categories == "news" else agent_hits

    def fake_searx(query, *, categories, language, time_range, limit):
        return searx_hits

    with (
        patch.object(agent_search_client, "is_enabled", return_value=True),
        patch.object(web_search, "_agent_search_raw", side_effect=fake_agent),
        patch.object(web_search, "_searx_raw", side_effect=fake_searx) as searx,
        # The blend order across all four sources is what this test checks,
        # so the general AgentSearch request must be on.
        patch.object(web_search, "AGENT_SEARCH_GENERAL_MODE", "general"),
    ):
        # Ask for the news pack explicitly: an evergreen query like "q" now
        # skips it by design, so keyword detection must not decide the
        # ordering this test is about.
        out = web_search.search_web(
            "q", number_of_result_pages=5, merge_news=True
        )

    # News (agent) first, then SearXNG general, then agent general.
    assert [h["url"] for h in out] == [
        "https://n.example/",
        "https://s.example/",
        "https://a.example/",
    ]
    assert searx.call_count >= 1


def test_evergreen_query_skips_the_news_pack() -> None:
    """News engines time out often; only newsy asks should pay for them."""
    searx_hits = [
        {
            "result_id": 1,
            "title": "S",
            "url": "https://s.example/",
            "content": "s",
            "description": "s",
        }
    ]
    categories_seen: List[str] = []

    def fake_searx(query, *, categories, language, time_range, limit):
        categories_seen.append(categories)
        return searx_hits

    with (
        patch.object(agent_search_client, "is_enabled", return_value=False),
        patch.object(web_search, "_searx_raw", side_effect=fake_searx),
    ):
        web_search.search_web("what is photosynthesis", number_of_result_pages=5)

    assert categories_seen == ["general"]


def test_last_occurrence_query_gets_the_news_pack() -> None:
    """'son maç' style asks are answered by recaps, which live in news."""
    searx_hits = [
        {
            "result_id": 1,
            "title": "S",
            "url": "https://s.example/",
            "content": "s",
            "description": "s",
        }
    ]
    categories_seen: List[str] = []

    def fake_searx(query, *, categories, language, time_range, limit):
        categories_seen.append(categories)
        return searx_hits

    with (
        patch.object(agent_search_client, "is_enabled", return_value=False),
        patch.object(web_search, "_searx_raw", side_effect=fake_searx),
    ):
        web_search.search_web("Galatasaray son maç ilk 11", number_of_result_pages=5)

    assert "news" in categories_seen


def test_search_web_falls_back_to_searxng_on_agent_error() -> None:
    searx_hits = [
        {
            "result_id": 1,
            "title": "S",
            "url": "https://s.example/",
            "content": "s",
            "description": "s",
        }
    ]

    with (
        patch.object(agent_search_client, "is_enabled", return_value=True),
        patch.object(
            web_search,
            "_agent_search_raw",
            return_value=[{"error": "agent-search failed (general): down"}],
        ),
        patch.object(web_search, "_searx_raw", return_value=searx_hits) as searx,
    ):
        out = web_search.search_web(
            "q",
            number_of_result_pages=3,
            categories="science",
            merge_news=False,
        )

    assert out == searx_hits
    assert searx.call_count == 1


def test_search_web_skips_agent_when_disabled() -> None:
    searx_hits = [
        {
            "result_id": 1,
            "title": "S",
            "url": "https://s.example/",
            "content": "s",
            "description": "s",
        }
    ]

    with (
        patch.object(agent_search_client, "is_enabled", return_value=False),
        patch.object(web_search, "_agent_search_raw") as agent,
        patch.object(web_search, "_searx_raw", return_value=searx_hits),
    ):
        out = web_search.search_web("q", merge_news=False)

    assert out == searx_hits
    agent.assert_not_called()


if __name__ == "__main__":
    test_agent_search_normalizes_hits()
    test_agent_search_maps_news_category_to_mode()
    test_search_web_blends_agent_search_with_searxng()
    test_search_web_falls_back_to_searxng_on_agent_error()
    test_search_web_skips_agent_when_disabled()
    print("All agent-search routing checks passed")
