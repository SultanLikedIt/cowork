"""News+general merge for SearXNG discovery."""

from __future__ import annotations

from unittest import mock

import web_search


def test_merge_prefers_news_and_dedupes() -> None:
    news = [
        {
            "url": "https://news.example/lineup",
            "title": "XI belli oldu",
            "content": "names",
        },
        {
            "url": "https://hub.example/club/",
            "title": "Hub also in news",
            "content": "x",
        },
    ]
    general = [
        {
            "url": "https://hub.example/club/",
            "title": "Hub",
            "content": "list",
        },
        {
            "url": "https://general.example/other",
            "title": "Other",
            "content": "y",
        },
    ]
    merged = web_search._merge_category_results(news, general, limit=10)
    assert [m["url"] for m in merged] == [
        "https://news.example/lineup",
        "https://hub.example/club/",
        "https://general.example/other",
    ]
    assert merged[0]["result_id"] == 1


def test_merge_returns_error_only_when_empty() -> None:
    merged = web_search._merge_category_results(
        [{"error": "news down"}],
        [{"error": "general down"}],
        limit=5,
    )
    assert len(merged) == 1
    assert "error" in merged[0]


def test_evergreen_query_skips_news_blend_by_default() -> None:
    assert not web_search._query_wants_news_blend(
        "Akdeniz diyeti örnek günlük menü"
    )
    assert web_search._query_wants_news_blend(
        "bugün Ankaragücü maç sonucu haber"
    )


def test_search_web_evergreen_does_not_call_news_engines() -> None:
    calls: list[str] = []

    def fake_searx(query, *, categories, language, time_range, limit):
        calls.append(f"searx:{categories}")
        return [
            {
                "result_id": 1,
                "title": "Diet",
                "description": "x",
                "url": "https://example.com/diet",
                "content": "x",
            }
        ]

    with mock.patch.object(web_search, "_searx_raw", side_effect=fake_searx), mock.patch(
        "web_search.agent_search_client.is_enabled", return_value=False
    ):
        hits = web_search.search_web("Akdeniz diyeti nedir örnek menü", 5)
    assert "searx:general" in calls
    assert "searx:news" not in calls
    assert hits and hits[0]["url"].endswith("/diet")


def test_search_web_newsy_query_still_merges_news() -> None:
    calls: list[str] = []

    def fake_searx(query, *, categories, language, time_range, limit):
        calls.append(f"searx:{categories}")
        return [
            {
                "result_id": 1,
                "title": categories,
                "description": "x",
                "url": f"https://example.com/{categories}",
                "content": "x",
            }
        ]

    with mock.patch.object(web_search, "_searx_raw", side_effect=fake_searx), mock.patch(
        "web_search.agent_search_client.is_enabled", return_value=False
    ):
        web_search.search_web("bugün son dakika transfer haberi", 5)
    assert "searx:general" in calls
    assert "searx:news" in calls


if __name__ == "__main__":
    test_merge_prefers_news_and_dedupes()
    test_merge_returns_error_only_when_empty()
    test_evergreen_query_skips_news_blend_by_default()
    test_search_web_evergreen_does_not_call_news_engines()
    test_search_web_newsy_query_still_merges_news()
    print("All web_search merge checks passed")
