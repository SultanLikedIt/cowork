"""Unit tests for Commons image query resolution + relevance scoring."""

from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

import image_fetch


def _mock_llm_response(text: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=text))]
    return response


def test_llm_verify_relevance_accepts_matching_candidate() -> None:
    candidates = [
        {"title": "File:Electric vehicle charging.jpg"},
        {"title": "File:Pazari i Ri.jpg"},
    ]
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.return_value = _mock_llm_response("0")
        idx = image_fetch._llm_verify_relevance("Electric vehicle market", candidates)
    assert idx == 0


def test_llm_verify_relevance_rejects_when_model_says_none() -> None:
    """Live bug: 'pazari'/'turizmi' score 1.0 lexically but depict an
    unrelated Balkan bazaar/hotel. The LLM check is the backstop that
    catches what pure substring scoring cannot."""
    candidates = [{"title": "File:Pazari i Ri.jpg"}]
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.return_value = _mock_llm_response(
            "none"
        )
        idx = image_fetch._llm_verify_relevance("Electric vehicle market", candidates)
    assert idx is None


def test_llm_verify_relevance_fails_safe_on_error() -> None:
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.side_effect = RuntimeError("timeout")
        idx = image_fetch._llm_verify_relevance("anything", [{"title": "File:x.jpg"}])
    assert idx is None


def test_llm_verify_relevance_fails_safe_on_garbage_reply() -> None:
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.return_value = _mock_llm_response(
            "I'm not sure, maybe?"
        )
        idx = image_fetch._llm_verify_relevance("anything", [{"title": "File:x.jpg"}])
    assert idx is None


def test_llm_verify_relevance_empty_candidates_short_circuits() -> None:
    with patch("openai.OpenAI") as mock_cls:
        idx = image_fetch._llm_verify_relevance("anything", [])
    assert idx is None
    mock_cls.assert_not_called()


def test_llm_translate_query_returns_english_phrase() -> None:
    """Live bug: 'uzay turizmi' (Turkish) finds zero real Commons hits,
    but Commons has several genuine matches under the English name
    ("EADS Astrium Space Tourism exterior.jpg", "Space Tourism The New
    Frontier" x4) -- verified live. Translation is what lets a real match
    be found at all, not just what stops a wrong one."""
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.return_value = _mock_llm_response(
            "space tourism"
        )
        translated = image_fetch._llm_translate_query("Uzay turizmi")
    assert translated == "space tourism"


def test_llm_translate_query_fails_safe_on_error() -> None:
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.side_effect = RuntimeError("timeout")
        translated = image_fetch._llm_translate_query("anything")
    assert translated == ""


def test_user_subject_beats_wrong_model_query() -> None:
    req = (
        "Türkiye'de zeytin yetiştiriciliğini kısaca anlat. "
        "Metnin altına telifsiz bir zeytin ağacı görseli ekle. PDF ver."
    )
    resolved = image_fetch.resolve_image_query(
        req,
        model_query="Afrin Operasyonu TSK F-16",
        fallback="Rapor",
    )
    assert "zeytin" in resolved.casefold()
    assert "f-16" not in resolved.casefold()
    assert "afrin" not in resolved.casefold()


def test_enrich_images_uses_document_topic_not_sunumu() -> None:
    """'görsellerle zenginleştir' must not become 'html sunum' / football noise."""
    req = (
        "bana palantir maven sistemi ile ilgili araştırma yapıp bu sistemin "
        "gizli detaylarını veren bir word dökümanı yazmanı ve bu word "
        "dökümanından html sunum hazırlamanı ve bu sunumu bilgisayarımda "
        "açmanı istiyorum mümkünse sunumu görsellerle zenginleştir"
    )
    assert image_fetch.request_wants_image(req) is True
    resolved = image_fetch.resolve_image_query(
        req, model_query="", fallback="Palantir Maven Raporu"
    )
    low = resolved.casefold()
    assert "palantir" in low or "maven" in low, resolved
    assert "sunum" not in low, resolved
    assert "html" not in low, resolved


def test_polar_bear_image_query_not_ternetten() -> None:
    req = (
        "Küresel ısınmanın kutup ayıları üzerindeki etkisini araştır. "
        "Kısa bir özet metin yaz. Metnin altına internetten bulduğun "
        "telifsiz bir kutup ayısı veya buz kütlesi görseli ekle. "
        "Görselin döküman sınırlarını aşmadığından emin ol. pdf olarak ver"
    )
    resolved = image_fetch.resolve_image_query(req, model_query="", fallback="Rapor")
    low = image_fetch._fold(resolved)
    assert "ternet" not in low, resolved
    assert "internet" not in low, resolved
    assert "kutup" in low or "buz" in low, resolved
    variants = image_fetch._query_variants(resolved)
    # Both OR alternatives tried; never a lone first token.
    assert any("kutup" in image_fetch._fold(v) for v in variants), variants
    assert all(len(image_fetch._content_tokens(v)) >= 1 for v in variants)
    assert not any(
        image_fetch._content_tokens(v) == ["kutup"] for v in variants
    ), variants


def test_relevance_requires_all_tokens_for_short_queries() -> None:
    """Generic: 2-token query must not match a page that only has token #1."""
    page = {"title": "File:Acme Brand Storefront.jpg", "imageinfo": [{}]}
    assert image_fetch._relevance_score(page, "acme widget") == 0.0
    page2 = {"title": "File:Acme Widget Demo.jpg", "imageinfo": [{}]}
    assert image_fetch._relevance_score(page2, "acme widget") > 0
    page = {"title": "File:Afrin Operasyonu TSK F-16s.jpg", "imageinfo": [{}]}
    assert image_fetch._relevance_score(page, "zeytin agaci") == 0.0


def test_relevance_accepts_matching_title() -> None:
    page = {"title": "File:Zeytin ağacı, Karaisalı 01.jpg", "imageinfo": [{}]}
    score = image_fetch._relevance_score(page, "zeytin agaci")
    assert score > 0


def test_rank_pages_orders_by_score() -> None:
    pages = [
        {"title": "File:Random landscape.jpg", "imageinfo": [{}]},
        {"title": "File:Zeytin agaci Bahce.jpg", "imageinfo": [{}]},
        {"title": "File:F-16 jet.jpg", "imageinfo": [{}]},
    ]
    ranked = image_fetch._rank_pages(pages, "zeytin agaci")
    assert len(ranked) == 1
    assert "Zeytin" in ranked[0]["title"]


def test_sanitize_strips_html_img() -> None:
    import document_export

    dirty = (
        "Özet paragraf.\n\n"
        '<img src="https://images.pexels.com/photos/1.jpeg" '
        'alt="x" style="max-width:80%" />\n\n'
        "Sonuç."
    )
    clean = document_export.sanitize_report_prose(dirty)
    assert "<img" not in clean.casefold()
    assert "pexels.com" not in clean
    assert "Özet" in clean


def test_inject_strips_kaynaklar_without_colon() -> None:
    try:
        from execution.langgraph_runtime.workforce import _inject_sources_into_payload
    except ModuleNotFoundError:
        print("skip inject test (langgraph not installed locally)")
        return

    payload = {
        "summary": "Metin.\n\nKaynaklar\n1 Eski kaynak",
        "content": "Metin.\n\nKaynaklar\n1 Eski kaynak",
    }
    _inject_sources_into_payload(
        payload,
        [{"url": "https://news.example/a", "title": "Haber"}],
        required=True,
    )
    assert payload["summary"].count("Kaynaklar") == 1
    assert "https://news.example/a" in payload["summary"]
    assert "Eski kaynak" not in payload["summary"]


def test_starlink_prompt_not_altina_query() -> None:
    req = (
        "Starlink’in (SpaceX uydu interneti) teknik yapısını, kullanım "
        "alanlarını ve jeopolitik etkilerini kısaca araştırıp özet bir "
        "rapor yaz; metnin altına telifsiz bir uydu veya anten görseli "
        "ekle, kaynakları da koy ve PDF olarak ver."
    )
    resolved = image_fetch.resolve_image_query(
        req, model_query="", fallback="Starlink Raporu"
    )
    low = image_fetch._fold(resolved)
    assert "altina" not in low, resolved
    assert "starlink" in low or "uydu" in low or "anten" in low, resolved
    assert not image_fetch._is_weak_image_query(resolved) or "starlink" in low


def test_palantir_maven_variants_include_brand() -> None:
    """Commons has no 'palantir maven' hit; fallback must try 'palantir'."""
    variants = image_fetch._query_variants("palantir maven")
    folded = [image_fetch._fold(v) for v in variants]
    assert "palantir maven" in folded
    assert "palantir" in folded
    assert "maven" not in folded  # NASA MAVEN trap


def test_ev_market_variants_skip_lone_pazar() -> None:
    """Live bug: 'elektrikli araç pazarı' fell through every real-word
    variant to lone 'pazar(i)', which Commons resolves to Tirana/Novi
    Pazar bazaar-town photos, not an EV market. The lone token must never
    be offered as a fallback."""
    variants = image_fetch._query_variants("Elektrikli araç pazarı")
    folded = [image_fetch._fold(v) for v in variants]
    assert "elektrikli arac pazari" in folded
    assert not any(
        image_fetch._content_tokens(v) in (["pazar"], ["pazari"]) for v in variants
    )


def test_pazar_bazaar_photo_rejected_as_ambiguous_token() -> None:
    page = {
        "title": "File:Pazari i Ri.jpg",
        "imageinfo": [
            {
                "extmetadata": {
                    "ImageDescription": {"value": "New bazaar market in Tirana, Albania"}
                }
            }
        ],
    }
    # The relevance scorer itself is purely lexical and still matches --
    # the fix is that "pazar"/"pazari" never reach it as a lone variant
    # (see test_ev_market_variants_skip_lone_pazar).
    assert image_fetch._relevance_score(page, "pazari") > 0.0
    assert "pazari" in image_fetch._AMBIGUOUS_IMAGE_TOKENS
    assert "pazar" in image_fetch._AMBIGUOUS_IMAGE_TOKENS


def test_anten_railway_station_rejected() -> None:
    page = {
        "title": "File:Anten station.JPG",
        "imageinfo": [
            {
                "extmetadata": {
                    "ImageDescription": {
                        "value": "Railway station Anten, kilom. från Göteborg"
                    }
                }
            }
        ],
    }
    assert image_fetch._relevance_score(page, "anten") == 0.0
    assert image_fetch._relevance_score(page, "antenna") == 0.0


def test_weak_anten_query_strengthened_with_starlink() -> None:
    req = (
        "Starlink’in teknik yapısını kısaca araştırıp özet yaz; "
        "metnin altına telifsiz bir anten görseli ekle; PDF ver."
    )
    resolved = image_fetch.resolve_image_query(
        req, model_query="anten", fallback="Starlink Raporu"
    )
    low = image_fetch._fold(resolved)
    assert "starlink" in low, resolved
    assert not image_fetch._is_weak_image_query(resolved), resolved


def test_or_variants_skip_lone_weak_anten() -> None:
    variants = image_fetch._query_variants("starlink veya anten")
    folded = [image_fetch._fold(v) for v in variants]
    assert any("starlink" in v for v in folded)
    assert not any(image_fetch._content_tokens(v) == ["anten"] for v in variants)


def _searxng_payload(*results: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"results": list(results)}
    resp.raise_for_status.return_value = None
    return resp


def test_searxng_results_become_rankable_pages() -> None:
    """The web engines are only useful if the existing ranking, LLM
    verification, and download stages can read their output unchanged."""
    resp = _searxng_payload(
        {
            "engine": "bing images",
            "title": "Zeytinyagi uretimi Turkiye",
            "content": "olive harvest",
            "img_src": "https://cdn.example.com/olive.jpg",
            "url": "https://example.com/olive-story",
            "source": "example.com",
        }
    )
    with patch("image_fetch.requests.get", return_value=resp):
        pages = image_fetch._searxng_pages("zeytinyagi uretimi")

    assert len(pages) == 1
    page = pages[0]
    # Shaped like a Commons page, so _rank_pages/_page_text_blob/
    # _pick_download_url all work without a second code path.
    assert image_fetch._pick_download_url(page["imageinfo"][0])
    assert image_fetch._relevance_score(page, "zeytinyagi uretimi") > 0
    assert page["_page_url"] == "https://example.com/olive-story"


def test_pinterest_results_are_dropped() -> None:
    """Its CDN refuses non-browser referers, so those candidates can only
    occupy a slot and then fail to download."""
    resp = _searxng_payload(
        {
            "engine": "pinterest",
            "title": "zeytinyagi",
            "img_src": "https://i.pinimg.com/originals/x.jpg",
        }
    )
    with patch("image_fetch.requests.get", return_value=resp):
        assert image_fetch._searxng_pages("zeytinyagi") == []


def test_web_sourced_image_is_not_claimed_as_openly_licensed() -> None:
    """Commons publishes a licence for every file; the web image engines
    publish none. Calling the latter "açık lisans" would assert a
    permission nobody granted."""
    _, web = image_fetch._attribution("Olive harvest", {}, "https://example.com/a", "example.com")
    assert "lisans belirtilmemiş" in web
    assert "example.com üzerinden" in web

    _, commons = image_fetch._attribution("File:Olive.jpg", {}, "https://commons.example/x")
    assert "açık lisans" in commons
    assert "Wikimedia Commons üzerinden" in commons


def test_non_image_payload_is_rejected_before_it_is_saved() -> None:
    """A dead hotlink that answers 200 with an HTML error page must not
    reach the normalize fallback, which would write that HTML out as .jpg."""
    assert not image_fetch._looks_like_image(b"<!DOCTYPE html><html>gone</html>")
    assert image_fetch._looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    assert image_fetch._looks_like_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")


def test_clean_licence_matches_a_category_qualified_engine_name() -> None:
    """SearXNG reports the Commons engine as "wikicommons.images". An
    equality test never fired for exactly the sources the set exists to
    favour."""
    assert image_fetch._has_clean_license("wikicommons.images")
    assert image_fetch._has_clean_license("pexels")
    assert not image_fetch._has_clean_license("bing images")


def test_english_translation_is_tried_before_the_native_query() -> None:
    """The English query must run first, not as a fallback.

    It was a fallback at first, which cannot work: the failure it has to
    catch is not "found nothing" but "found something bad and stopped
    looking". Live, "bal ureticiligi" succeeded immediately on a news
    story headlined "Süt ve bal üreticiliği için destek" whose photo
    illustrates the milk half of it, so a honey report got a cow -- and
    the fallback never ran, because nothing had failed.
    """
    seen: list[str] = []

    def record(query: str) -> list[dict]:
        seen.append(query)
        return [{"title": query}]

    # _PAGE_SOURCES holds the function objects themselves, captured at
    # import, so patching the names they were bound from would not reach it.
    with (
        patch("image_fetch._PAGE_SOURCES", (record,)),
        patch("image_fetch._llm_verify_relevance", return_value=0),
        patch("image_fetch._download_first", return_value=MagicMock()),
        patch("image_fetch._llm_translate_query", return_value="honey production"),
    ):
        image_fetch.fetch_image("bal ureticiligi", Path("/tmp"), topic="bal ureticiligi")

    assert seen[0] == "honey production", seen


def test_native_variants_still_run_after_the_translation() -> None:
    """A subject that only exists under its Turkish name must not be lost
    to the English-first ordering."""
    seen: list[str] = []

    with (
        patch("image_fetch._PAGE_SOURCES", (lambda q: seen.append(q) or [],)),
        patch("image_fetch._llm_translate_query", return_value="honey production"),
    ):
        image_fetch.fetch_image("bal ureticiligi", Path("/tmp"), topic="bal ureticiligi")

    assert "honey production" in seen
    assert "bal ureticiligi" in seen
    assert seen.index("honey production") < seen.index("bal ureticiligi")


def test_clean_licence_engine_only_breaks_a_tie() -> None:
    """The bonus must not let a weaker match outrank a stronger one --
    relevance stays the primary sort."""
    strong = {"title": "zeytinyagi uretimi", "_engine": "bing images"}
    weak = {"title": "zeytinyagi baska konu", "_engine": "pexels"}
    ranked = image_fetch._rank_pages([weak, strong], "zeytinyagi uretimi")
    assert ranked and ranked[0] is strong


if __name__ == "__main__":
    test_user_subject_beats_wrong_model_query()
    test_enrich_images_uses_document_topic_not_sunumu()
    test_polar_bear_image_query_not_ternetten()
    test_relevance_requires_all_tokens_for_short_queries()
    test_relevance_accepts_matching_title()
    test_rank_pages_orders_by_score()
    test_sanitize_strips_html_img()
    test_inject_strips_kaynaklar_without_colon()
    test_anten_railway_station_rejected()
    test_weak_anten_query_strengthened_with_starlink()
    test_or_variants_skip_lone_weak_anten()
    test_starlink_prompt_not_altina_query()
    print("All image_fetch / export checks passed")


def test_model_image_query_is_checked_against_the_topic() -> None:
    """Live regression: a polar-bear report searched Commons for the tail
    of its own delivery instruction and saved da-ac-bu.jpg.

    resolve_image_query only derived the topic when the user had explicitly
    asked for a picture, so on every report nobody asked to illustrate the
    model's invented query won unchecked -- the exact case the function's
    docstring says it guards against.
    """
    import image_fetch

    request = (
        "kutup ayilarinin uyku duzenleri hakkinda bir arastirma yap ve "
        "pdf olarak bana ver bilgisayarimda da ac bu pdfi"
    )
    assert not image_fetch.request_wants_image(request)

    for junk in ("bilgisayarimda da ac bu", "da ac bu", "pdf olarak ver"):
        picked = image_fetch.resolve_image_query(
            request, model_query=junk, fallback=""
        )
        assert "bilgisayar" not in picked, picked
        assert "kutup" in picked, picked


def test_photo_with_no_named_subject_falls_back_to_the_topic() -> None:
    """Live regression: Commons was searched for "da ac bu".

    _IMAGE_OBJECT_TR reads Turkish "X fotografi" as photo-of-X, taking the
    words before the noun as the subject. "fotograf da koy pdfe" puts the
    noun first with no subject, so it swept up the tail of the delivery
    instruction instead ("bilgisayarimda da ac bu pdfi") and saved the
    result as da-ac-bu.jpg. With no subject named, the report topic is what
    the docstring says should be used.
    """
    import image_fetch

    request = (
        "kutup ayilarinin uyku duzenleri hakkinda bir arastirma yap ve pdf "
        "olarak bana ver bilgisayarimda da ac bu pdfi ayni zamanda fotograf "
        "da koy pdfe"
    )
    assert image_fetch.request_wants_image(request)
    derived = image_fetch.image_query_from_request(request, fallback="")
    assert "kutup" in derived, derived
    for junk in ("da ac", "bilgisayar", "pdf"):
        assert junk not in derived, derived


def test_named_photo_subject_still_wins() -> None:
    """The subject the user actually named must not be replaced."""
    import image_fetch

    assert (
        image_fetch.image_query_from_request("kutup ayisi fotografi ekle", fallback="")
        == "kutup ayisi"
    )
    assert (
        image_fetch.image_query_from_request("zeytin agaci gorseli koy", fallback="")
        == "zeytin agaci"
    )


def test_turkish_case_endings_get_a_stemmed_variant() -> None:
    """Live regression: a Turkish coffee report was illustrated with cooking.

    Commons indexes nominative forms, so "turk kahvesinin" matched almost
    nothing and the fallback chain threw words away until a generic one
    ("pisirme") finally hit -- that became both the search and the filename.
    Dropping the case ending keeps the subject; dropping a word does not.
    """
    from image_fetch import _query_variants, _tr_stem

    assert _tr_stem("kahvesinin") == "kahvesi"
    assert _tr_stem("ankaradan") == "ankara"
    # Short words keep their ending rather than being stemmed to nothing.
    assert _tr_stem("evin") == "evin"
    # Bare "-da"/"-de" are left alone: they are also standalone particles,
    # and stripping them would eat words like "moda".
    assert _tr_stem("moda") == "moda"

    variants = _query_variants("turk kahvesinin tarihi pisirme")
    assert "turk kahvesi" in variants
    # The subject-bearing variant must be tried before the generic word
    # that happened to have pictures.
    assert variants.index("turk kahvesi") < variants.index("pisirme")


def test_image_query_survives_undotted_turkish_request() -> None:
    """Live bug: asked for "misir uretilicegiligi ... gorsellerle
    zenginlestir", the model answered with image_query "mısır tarlası".
    The guard that keeps an image query on topic compared raw text, so the
    model's proper Turkish matched nothing in the user's undotted request
    and the query was silently emptied -- which makes the pdf/docx branch
    skip images entirely. No image came, twice, including on the follow-up
    asking why."""
    import re

    from execution.langgraph_runtime.workforce import _fold_tr

    ask = _fold_tr(
        "misir uretilicegiligi ile alakali bana bir arastirma yap "
        "nerelerde yapilir falan sonrasinda bunu docx olarak ver "
        "gorsellerle zenginlestir"
    )

    def survives(image_query: str) -> bool:
        tokens = [
            t
            for t in re.findall(r"[a-z0-9]{4,}", _fold_tr(image_query))
            if t not in {"turkiye", "turkey", "report", "rapor", "image", "photo"}
        ]
        return not tokens or any(t in ask for t in tokens)

    assert survives("mısır tarlası")
    assert survives("mısır üretimi Türkiye")
    assert survives("misir tarlasi")
    # Still drops a query with nothing to do with the request.
    assert not survives("elektrikli otomobil")


def test_watermarked_stock_agencies_are_rejected() -> None:
    """The picture is often right -- an Alamy hit for "honey beekeeping
    anatolia" really was an Anatolian honey bee -- but it arrives stamped
    "alamy" across the frame and "Stock Photo" along the bottom."""
    assert image_fetch._is_watermarked_host("https://c8.alamy.com/comp/x/y.jpg")
    assert image_fetch._is_watermarked_host("", "https://www.shutterstock.com/image/1")
    assert not image_fetch._is_watermarked_host("https://images.pexels.com/photos/1.jpg")


def test_icon_libraries_are_dropped() -> None:
    """devicons/lucide are icon sets. Their single-word names match on
    fragments no photo search would -- "bal üreticiliği" pulled in
    "ballerina" and "balloon", which then took slots in the bounded
    candidate list ahead of real photographs."""
    resp = _searxng_payload(
        {"engine": "lucide", "title": "balloon", "img_src": "https://x/i.svg"},
        {"engine": "devicons", "title": "ballerina", "img_src": "https://x/b.svg"},
    )
    with patch("image_fetch.requests.get", return_value=resp):
        assert image_fetch._searxng_pages("bal ureticiligi") == []


def test_listing_thumbnails_are_too_small_to_embed() -> None:
    """A correct "woman harvesting olives in İzmir" came back 100x130 and
    would have gone into the PDF as a postage stamp."""
    from io import BytesIO

    from PIL import Image

    def encode(width: int, height: int) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (width, height), "white").save(buf, format="JPEG")
        return buf.getvalue()

    assert image_fetch._too_small_to_embed(encode(100, 130))
    assert not image_fetch._too_small_to_embed(encode(800, 600))
    # Fails open on anything it cannot decode -- the magic-byte and
    # normalize checks own that decision.
    assert not image_fetch._too_small_to_embed(b"not an image")


def test_a_solid_colour_image_is_rejected() -> None:
    """A valid JPEG can still show nothing. A live report embedded a
    668-byte 250x250 image whose average colour was pure black: it decoded,
    it cleared the size floor, it was not HTML -- every check it passed was
    structural, and none of them asked whether it depicted anything."""
    import random
    from io import BytesIO

    from PIL import Image

    def encode(image: "Image.Image") -> bytes:
        buf = BytesIO()
        image.save(buf, format="JPEG")
        return buf.getvalue()

    assert image_fetch._is_blank_image(encode(Image.new("RGB", (400, 400), "black")))
    assert image_fetch._is_blank_image(encode(Image.new("RGB", (400, 400), "white")))

    noisy = Image.new("RGB", (400, 400))
    noisy.putdata([(random.randint(0, 255),) * 3 for _ in range(400 * 400)])
    assert not image_fetch._is_blank_image(encode(noisy))

    # A dark but detailed photograph must survive. Measuring extrema on a
    # downsample averaged this into a solid black and rejected it.
    dark = Image.new("RGB", (400, 400))
    dark.putdata([(random.randint(0, 60),) * 3 for _ in range(400 * 400)])
    assert not image_fetch._is_blank_image(encode(dark))

    # Undecodable bytes are left to the other checks.
    assert not image_fetch._is_blank_image(b"not an image")
