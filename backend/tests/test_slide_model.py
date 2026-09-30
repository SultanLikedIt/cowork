"""Typed slides: parsing, degrading, number grounding, payload plumbing.

The fixtures are the two live decks this module was written for: a gold
price report whose 20-row January table came out as six slides of clipped
bullets, and a giraffe report whose nested habitat list fell apart.
"""

from __future__ import annotations

import json

import pytest

import document_model
import slide_model
from slide_model import Deck, Slide, deck_from_slides

JANUARY = [
    ("1 Oca", "5.997,11", "5.497,35"),
    ("2 Oca", "5.994,66", "5.495,11"),
    ("28 Oca", "7.566,27", "6.935,75"),
]
NOTES = (
    "Gram altın Ocak 2026'da 5.994,66 TL ile 7.566,27 TL arasında değişti; "
    "ay içinde %26 yükseldi. 7 Eylül'de ons altın 4.393,76 dolar.\n"
    + "\n".join(" | ".join(row) for row in JANUARY)
)


def _deck(*slides: dict, title: str = "Altın 2026") -> Deck:
    deck = deck_from_slides(title, list(slides))
    assert deck is not None
    return deck


BULLETS = {"layout": "bullets", "title": "Genel görünüm", "bullets": ["Fiyatlar dalgalı seyretti."]}


# --- parsing -------------------------------------------------------------------


def test_aliases_and_shapes_small_models_send_are_understood() -> None:
    deck = _deck(
        {"type": "kpi", "title": "Öne çıkanlar", "stats": ["5,5 m — yetişkin boyu", "1.200 kg: ağırlık"]},
        {"layout": "comparison", "title": "Karşılaştırma", "left": ["a", "b"], "right": {"title": "Sağ", "items": ["c"]}},
        {"title": "Ocak", "rows": [{"Tarih": "1 Oca", "24 Ayar": "5.997,11"}]},
        {"layout": "graph", "title": "Seyir", "chart": {"kind": "trend", "labels": ["1 Oca", "2 Oca"], "values": ["5.997,11", "5.994,66"]}},
        {"layout": "sonuç", "title": "Sonuç", "points": "Birinci\nİkinci"},
    )
    layouts = [s.layout for s in deck.slides]
    assert layouts == ["stats", "two_column", "table", "chart", "takeaways"]
    stats = deck.slides[0].stats
    assert (stats[0].value, stats[0].label) == ("5,5 m", "yetişkin boyu")
    assert (stats[1].value, stats[1].label) == ("1.200 kg", "ağırlık")
    assert deck.slides[1].right.heading == "Sağ"
    assert deck.slides[2].columns == ["Tarih", "24 Ayar"]
    chart = deck.slides[3].chart
    assert chart.kind == "line" and chart.series[0].values == [5997.11, 5994.66]
    assert [b.text for b in deck.slides[4].bullets] == ["Birinci", "İkinci"]


def test_nested_bullets_keep_their_parent() -> None:
    """The giraffe deck printed "Başlıca habitatlar:" and its items as
    siblings, split across two slides."""
    deck = _deck(
        {
            "layout": "bullets",
            "title": "Yaşam alanı",
            "bullets": [
                "Zürafalar yalnızca Afrika'da yaşar.",
                {"text": "Başlıca habitatlar:", "items": ["Savanlar", "Otlaklar"]},
            ],
        },
        BULLETS,
    )
    bullets = deck.slides[0].bullets
    assert [(b.text, b.level) for b in bullets] == [
        ("Zürafalar yalnızca Afrika'da yaşar.", 0),
        ("Başlıca habitatlar:", 0),
        ("Savanlar", 1),
        ("Otlaklar", 1),
    ]


def test_a_slide_that_lost_its_content_degrades_instead_of_failing() -> None:
    deck = _deck(
        {"layout": "chart", "title": "Grafik", "chart": {"categories": ["a"], "series": []}, "bullets": ["Veri yetersiz."]},
        {"layout": "stats", "title": "Tek rakam", "stats": [{"value": "%26", "label": "Ocak artışı"}]},
        {"layout": "two_column", "title": "Yarım", "left": {"heading": "Sol", "bullets": ["x"]}},
        {"layout": "chart", "title": "Boş grafik"},
        BULLETS,
    )
    assert [s.layout for s in deck.slides] == ["bullets", "bullets", "bullets", "bullets"]
    assert deck.slides[1].bullets[0].text == "**%26** Ocak artışı"
    assert [b.text for b in deck.slides[2].bullets] == ["Sol", "x"]


def test_model_written_sources_and_title_slides_are_dropped() -> None:
    deck = _deck(
        {"layout": "section", "title": "Altın 2026"},
        BULLETS,
        {"layout": "bullets", "title": "Kaynaklar", "bullets": ["exchange-rates.org"]},
        {"layout": "takeaways", "title": "Sonuç", "bullets": ["Ocak sert yükseldi."]},
    )
    assert [s.title for s in deck.slides] == ["Genel görünüm", "Sonuç"]


def test_too_little_to_be_a_deck_is_none() -> None:
    assert deck_from_slides("x", [BULLETS]) is None
    assert deck_from_slides("x", "not json") is None
    assert deck_from_slides("x", json.dumps({"slides": [BULLETS, BULLETS]})) is not None


def test_a_pie_that_cannot_be_a_pie_becomes_a_bar() -> None:
    deck = _deck(
        {"layout": "chart", "title": "Pay", "chart": {"type": "pie", "categories": ["a", "b"], "series": [{"values": [60, -5]}]}},
        BULLETS,
    )
    assert deck.slides[0].chart.kind == "bar"


# --- numbers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, value",
    [
        ("6.842,40 TL", 6842.4),
        ("6,842.40", 6842.4),
        ("1.200 kg", 1200.0),
        ("1,200", 1200.0),
        ("12,5", 12.5),
        ("4393.76", 4393.76),
        ("%26", 26.0),
        ("-3,5%", -3.5),
        (7, 7.0),
        ("yok", None),
    ],
)
def test_numbers_are_read_the_way_they_are_written(text, value) -> None:
    assert slide_model.parse_number(text) == value


def test_format_number_is_turkish() -> None:
    assert slide_model.format_number(6842.4) == "6.842,4"
    assert slide_model.format_number(1200.0) == "1.200"


# --- grounding -----------------------------------------------------------------


def test_stats_not_in_the_notes_are_dropped() -> None:
    deck = _deck(
        {
            "layout": "stats",
            "title": "Rakamlar",
            "stats": [
                {"value": "%26", "label": "Ocak artışı"},
                {"value": "4.393,76 $", "label": "Ons"},
                {"value": "%31", "label": "Yıllık artış"},  # never in the notes
            ],
        },
        BULLETS,
    )
    logged: list[str] = []
    slide_model.ground_deck(deck, NOTES, log=logged.append)
    assert [s.value for s in deck.slides[0].stats] == ["%26", "4.393,76 $"]
    assert logged and "%31" in logged[0]


def test_a_chart_with_invented_values_is_dropped() -> None:
    deck = _deck(
        {
            "layout": "chart",
            "title": "Ocak",
            "chart": {"type": "line", "categories": ["1 Oca", "2 Oca", "28 Oca"], "series": [{"values": [6000, 6100, 7600]}]},
            "bullets": ["Ay boyunca yükseldi."],
        },
        BULLETS,
    )
    slide_model.ground_deck(deck, NOTES)
    assert deck.slides[0].layout == "bullets" and deck.slides[0].chart is None


def test_a_chart_of_the_notes_own_figures_survives() -> None:
    deck = _deck(
        {
            "layout": "chart",
            "title": "Ocak",
            "chart": {"type": "line", "categories": [r[0] for r in JANUARY], "series": [{"values": [r[1] for r in JANUARY]}]},
        },
        BULLETS,
    )
    slide_model.ground_deck(deck, NOTES)
    assert deck.slides[0].layout == "chart"


def test_without_a_source_nothing_is_checked() -> None:
    deck = _deck({"layout": "stats", "title": "R", "stats": ["%31 — a", "%40 — b"]}, BULLETS)
    slide_model.ground_deck(deck, "")
    assert len(deck.slides[0].stats) == 2


# --- payload plumbing ------------------------------------------------------------


def _payload() -> dict:
    return {
        "title": "Altın 2026",
        "subtitle": "Ocak ve Eylül verileri",
        "slides": [
            BULLETS,
            {"layout": "table", "title": "Ocak", "columns": ["Tarih", "24 Ayar", "22 Ayar"], "rows": [list(r) for r in JANUARY]},
        ],
    }


def test_slides_are_mirrored_into_content_and_survive_the_sources_tail() -> None:
    payload = _payload()
    deck = slide_model.attach_slides_to_payload(payload)
    assert deck is not None
    assert "## Ocak" in payload["content"] and "| 28 Oca | 7.566,27 | 6.935,75 |" in payload["content"]
    payload["content"] += "\n\nKaynaklar:\n- Altın fiyatları (https://example.com/altin)"
    resolved = slide_model.resolve_payload_deck(payload)
    assert resolved is not None and [s.layout for s in resolved.slides] == ["bullets", "table"]
    assert resolved.subtitle == "Ocak ve Eylül verileri"


def test_replaced_content_means_the_slides_are_stale() -> None:
    payload = _payload()
    slide_model.attach_slides_to_payload(payload)
    payload["content"] = "Tamamen başka bir metin."
    assert slide_model.resolve_payload_deck(payload) is None


def test_sources_are_read_back_from_the_server_written_tail() -> None:
    text = (
        "## Bölüm\n- madde\n\nKaynaklar:\n"
        "- Türkiye Altın Fiyatları 2026 (https://www.exchange-rates.org/tr/altin)\n"
        "- https://finans.mynet.com/altin/572900/\n"
        "- Türkiye Altın Fiyatları 2026 (https://www.exchange-rates.org/tr/altin)\n"
    )
    assert slide_model.sources_from_text(text) == [
        ("Türkiye Altın Fiyatları 2026", "https://www.exchange-rates.org/tr/altin"),
        ("", "https://finans.mynet.com/altin/572900/"),
    ]


# --- from typed blocks -------------------------------------------------------------


def test_a_word_report_becomes_one_slide_per_section() -> None:
    document = document_model.document_from_blocks(
        "Zürafa",
        [
            {"type": "heading", "level": 1, "text": "Fiziksel özellikler"},
            {"type": "paragraph", "text": "Boyları 5,5 metreye ulaşır. Ağırlıkları 1.200 kg civarındadır."},
            {"type": "heading", "level": 1, "text": "Taksonomi"},
            {"type": "table", "columns": ["Aşama", "Ad"], "rows": [["Sınıf", "Mammalia"], ["Familya", "Giraffidae"]]},
            {"type": "heading", "level": 1, "text": "Kaynaklar"},
            {"type": "list", "items": ["https://tr.wikipedia.org/wiki/Zürafa"]},
        ],
    )
    deck = slide_model.deck_from_document(document)
    assert deck is not None
    assert [(s.layout, s.title) for s in deck.slides] == [
        ("bullets", "Fiziksel özellikler"),
        ("table", "Taksonomi"),
    ]
    # Whole sentences, one per bullet -- never a clipped fragment.
    assert [b.text for b in deck.slides[0].bullets] == [
        "Boyları 5,5 metreye ulaşır.",
        "Ağırlıkları 1.200 kg civarındadır.",
    ]


def test_the_flag_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCUMENT_SLIDES", raising=False)
    assert slide_model.slides_enabled()
    monkeypatch.setenv("DOCUMENT_SLIDES", "false")
    assert not slide_model.slides_enabled()


def test_slide_is_a_plain_model() -> None:
    assert Slide(title="x").layout == "bullets"


def test_an_invented_quote_is_dropped_and_a_real_one_kept() -> None:
    """Live: the giraffe deck quoted "doğa belgeseli anlatıcıları" saying
    something the notes never contained."""
    notes = "Zürafa karada yaşayan en uzun memelidir. Uzmanlara göre boyun omurları olağanüstü uzundur."
    deck = _deck(
        {"layout": "quote", "text": "Zürafa, doğanın en zarif canlısıdır; her adımı bir dans.", "attribution": "bilinmiyor"},
        {"layout": "quote", "title": "Uzman görüşü", "text": "Boyun omurları olağanüstü uzundur.", "attribution": "Uzmanlar"},
        BULLETS,
    )
    logged: list[str] = []
    slide_model.ground_deck(deck, notes, log=logged.append)
    assert [s.text for s in deck.slides if s.layout == "quote"] == ["Boyun omurları olağanüstü uzundur."]
    assert logged and "quote dropped" in logged[0]
