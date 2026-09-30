"""Source titles shown to the user, from titles scraped off real pages.

The cases here are the shapes that actually reached the sources list, not
invented ones: a cookie wall concatenated into <title>, a site name appended
after a separator, navigation flattened into one line.

The rule these all serve: the sources list shows the page's *title*. The URL
is already printed beside it, so anything that is the site rather than the
page is redundant at best and, in the banner case, wrong.
"""

from __future__ import annotations

from execution.langgraph_runtime.workforce import (
    _clean_source_title,
    _format_sources_lines,
    _format_sources_lines_plain,
)
from html_extract import extract_page


def test_consent_banner_is_cut_at_the_banner():
    """The original live failure, verbatim from the docstring it produced."""
    raw = (
        "Kanuni Sultan Süleyman Dönemi | PDF <!---->Close this consent "
        "banner<!----> <!---->"
    )
    assert _clean_source_title(raw) == "Kanuni Sultan Süleyman Dönemi - PDF"


def test_trailing_site_name_is_dropped_only_when_it_is_the_site():
    # The site's own name after a separator duplicates the URL beside it.
    assert (
        _clean_source_title("Enflasyon verileri açıklandı - TÜİK", "https://tuik.gov.tr/x")
        == "Enflasyon verileri açıklandı"
    )
    # ...but a title that IS the site name stays. Nothing else remains.
    assert _clean_source_title("BBC News", "https://bbc.co.uk/news") == "BBC News"
    # ...and a trailing segment that is not the site is content, not chrome.
    assert (
        _clean_source_title("Tarihçe - Osmanlı Devleti", "https://example.org/a")
        == "Tarihçe - Osmanlı Devleti"
    )


def test_navigation_segments_are_dropped():
    cleaned = _clean_source_title(
        "Home | Menu | 2024 Bütçe Raporu | Contact | Privacy",
        "https://site.example/rapor",
    )
    assert cleaned == "2024 Bütçe Raporu"


def test_title_that_is_only_navigation_falls_back_to_the_domain():
    # Showing "Home Menu Contact" as a source title is worse than showing
    # where it came from.
    assert (
        _clean_source_title("Home | Contact | About", "https://www.site.example/x")
        == "site.example"
    )


def test_a_real_title_containing_a_nav_word_is_kept():
    """The guard against over-trimming: these are content, not furniture."""
    for title in (
        "Contact tracing in Turkey",
        "About the 1999 earthquake",
        "İletişim kurma biçimleri üzerine bir inceleme",
    ):
        assert _clean_source_title(title, "https://x.example/p") == title


def test_overlong_title_is_capped_at_a_separator():
    raw = (
        "Türkiye'de tarımsal üretim ve gıda güvenliği üzerine kapsamlı bir "
        "değerlendirme: 2015-2024 dönemi - Tarım ve Orman Bakanlığı Yayınları"
    )
    cleaned = _clean_source_title(raw, "https://tarim.example/rapor")
    assert len(cleaned) <= 120
    assert not cleaned.endswith(("-", "–", ":", " "))


def test_empty_and_missing_titles_stay_empty():
    assert _clean_source_title("") == ""
    assert _clean_source_title(None) == ""
    # No URL to fall back to, so there is nothing honest to show.
    assert _clean_source_title("Home | Menu") == ""


def test_markdown_and_plain_source_lines_both_clean_the_title():
    sources = [
        {
            "url": "https://site.example/rapor",
            "title": "2024 Raporu | Close this consent banner",
        }
    ]
    assert _format_sources_lines(sources) == [
        "- [2024 Raporu](https://site.example/rapor)"
    ]
    assert _format_sources_lines_plain(sources) == [
        "- 2024 Raporu (https://site.example/rapor)"
    ]


def test_og_title_is_preferred_over_a_polluted_title_element():
    """The same class of bug, fixed one layer earlier.

    og:title is an attribute value, so a banner cannot be concatenated into
    it the way it can into <title>'s text nodes.
    """
    html = (
        '<html><head><meta property="og:title" content="Kanuni Sultan '
        'Süleyman Dönemi">'
        "<title>Kanuni Sultan Süleyman Dönemi "
        "<span>Close this consent banner</span></title></head>"
        "<body><p>" + ("Gövde metni. " * 40) + "</p></body></html>"
    )
    page = extract_page(
        url="https://site.example/p", status=200, html=html, content_type="text/html"
    )
    assert page.title == "Kanuni Sultan Süleyman Dönemi"


def test_generic_og_title_loses_to_a_specific_title_element():
    html = (
        '<html><head><meta property="og:title" content="Home">'
        "<title>2024 Bütçe Raporu</title></head>"
        "<body><p>" + ("Gövde metni. " * 40) + "</p></body></html>"
    )
    page = extract_page(
        url="https://site.example/p", status=200, html=html, content_type="text/html"
    )
    assert page.title == "2024 Bütçe Raporu"
