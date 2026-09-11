"""Probe SearXNG for queries that surface match-detail / lineup pages."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

base = (os.environ.get("SEARXNG_BASE_URL") or "http://searxng:8080").rstrip("/")
queries = [
    "Galatasaray Corum ilk 11 kadro",
    "Galatasaray Corum FK mac ozeti ilk 11",
    "Galatasaray Corum starting lineup",
    "site:flashscore.com Galatasaray Corum",
    "site:sahadan.com Galatasaray Corum ilk 11",
    "site:mackolik.com Galatasaray Corum",
    "Galatasaray Corum FK mac raporu",
    "Galatasaray 2-2 Corum ilk 11",
    "Galatasaray Corum FK kadro 14 Agustos",
]


def main() -> None:
    for q in queries:
        url = base + "/search?" + urllib.parse.urlencode({"q": q, "format": "json"})
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("results") or []
        print("===", q)
        for item in results[:5]:
            title = (item.get("title") or "")[:75]
            href = (item.get("url") or "")[:120]
            print(" -", title)
            print("  ", href)


if __name__ == "__main__":
    main()
