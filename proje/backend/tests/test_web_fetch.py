"""Tests for the small standalone utilities in web_fetch.py.

web_fetch.py used to be a full static-HTTP fetch/crawl engine (fetch_url,
crawl_site, extract_tables, list_links, backed by _get's own requests-based
transport); that whole tier was orphaned when Crawl4AI became the only
network-talking engine (see web_fetch.py's module docstring) and has been
removed. Its table/record shaping (_table_payloads, _shape_tables,
_save_tables, _save_records, _spend_text) is exercised through the live
web_read path in test_web_tools.py instead. What remains here is genuinely
standalone: find_urls and _tables_may_combine take plain data in, return
plain data out, with no fetching involved.
"""

import web_fetch

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def test_find_urls() -> None:
    cases = [
        ("go to https://example.com/page and get the table", ["https://example.com/page"]),
        ("see https://a.example/x, then https://b.example/y", ["https://a.example/x", "https://b.example/y"]),
        ("https://x.example/p. Thanks!", ["https://x.example/p"]),
        ("no url here at all", []),
        ("mention of example.com without a scheme", []),
        ("https://a.example/x and https://a.example/x again", ["https://a.example/x"]),
    ]
    for text, expected in cases:
        found = web_fetch.find_urls(text)
        check(f"find_urls: {text[:40]!r}", found == expected, f"{found} != {expected}")


def test_tables_may_combine_year_conflict_rule() -> None:
    cases = [
        ("no year anywhere", [{"page_title": "A"}, {"page_title": "B"}], True),
        (
            "same year, different months (ordinary pagination)",
            [{"page_title": "Ağustos 2026"}, {"page_title": "Eylül 2026"}],
            True,
        ),
        (
            "year stated once, on the first page only",
            [{"page_title": "2026 Yıllık Rapor"}, {"page_title": ""}],
            True,
        ),
        (
            "consecutive seasons sharing a boundary year",
            [{"page_title": "2025-2026"}, {"page_title": "2026-2027"}],
            False,
        ),
        (
            "same calendar month a year apart",
            [{"page_title": "Ağustos 2026"}, {"page_title": "Ağustos 2025"}],
            False,
        ),
    ]
    for name, metadata, expected in cases:
        result = web_fetch._tables_may_combine(metadata)
        check(f"_tables_may_combine: {name}", result == expected, (metadata, result))


def main() -> None:
    test_find_urls()
    test_tables_may_combine_year_conflict_rule()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All web-fetch checks passed")


if __name__ == "__main__":
    main()


def test_turkish_cookie_wall_is_not_page_content() -> None:
    """Live regression: a squad page was cited while its lineup was "missing".

    sahadan.com returned only its consent notice -- 535 characters, past the
    400-char limit the banner test used -- so it counted as a successfully
    read page. The answer then said the lineup was not in the evidence
    (true: the evidence was the cookie notice) while citing the URL that
    has it.
    """
    from html_extract import is_consent_boilerplate

    banner = (
        "Mackolik İnternet Hizmetleri Ticaret Anonim Şirketi olarak kişisel "
        "verilerinize önem veriyoruz ve internet sitemizi ziyaret edenlerin "
        "gizliliğini korumayı ilke olarak benimsemiş bulunuyoruz. Bu "
        "doğrultuda, internet sitemizin çalışması, analitik çalışmaların "
        "yürütülmesi ve tercihiniz doğrultusunda çerezler kullanmaktayız. "
        "Çerezlere dair tercihlerinizi aşağıdaki butonlar vasıtasıyla "
        "yönetebilirsiniz. Detaylı bilgi için Çerez Politikasına İlişkin "
        "Aydınlatma Metni'ni inceleyebilirsiniz."
    )
    assert len(banner) > 400
    assert is_consent_boilerplate(banner)

    # The page we actually wanted must not be mistaken for one.
    lineup = (
        "Galatasaray ilk 11: Muslera, Boey, Sanchez, Bardakci, Kerem, "
        "Torreira, Demirbay, Ziyech, Zaha, Icardi, Baris Alper. "
        "Gol: Icardi 23. dakika. Mac 3-1 bitti."
    )
    assert not is_consent_boilerplate(lineup)

    # Nor an article that genuinely discusses cookie law at length.
    article = (
        "Cerez hukuku Avrupa Birliginde ePrivacy direktifiyle sekillenmistir "
        "ve kisisel veri korumasi acisindan onemlidir. "
    ) * 16
    assert len(article) > 1500
    assert not is_consent_boilerplate(article)
