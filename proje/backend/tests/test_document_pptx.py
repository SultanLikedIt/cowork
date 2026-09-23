"""document_pptx: fitting, pagination and the drawn file, read back.

Every file assertion reopens the real .pptx with python-pptx.
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

import pytest
from pptx import Presentation

import document_pptx
import document_verify
import slide_model
from artifacts import ArtifactTools
from slide_model import Bullet, Slide, deck_from_slides

# The live gold deck's January table: 20 rows that became six slides of
# bullets clipped at "…", losing the 14K and 10K columns entirely.
JANUARY = [
    (f"{day} Oca", f"{5990 + i * 83},{i:02d}", f"{5490 + i * 76},{i:02d}", f"{4490 + i * 62},00", f"{3490 + i * 48},00", f"{2490 + i * 34},00")
    for i, day in enumerate([1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28])
]
COLUMNS = ["Tarih", "24 Ayar", "22 Ayar", "18 Ayar", "14 Ayar", "10 Ayar"]
SOURCES = [
    ("Türkiye Altın Fiyatları 2026", "https://www.exchange-rates.org/tr/altin/2026"),
    ("", "https://finans.mynet.com/haber/detay/altin/572900/"),
]


def _all_layouts_deck():
    return deck_from_slides(
        "Türkiye Altın Fiyatları 2026",
        [
            {"layout": "bullets", "title": "Genel görünüm", "bullets": ["Fiyatlar **dalgalı** seyretti.", {"text": "Ocak:", "items": ["En düşük 2 Ocak", "En yüksek 28 Ocak"]}], "notes": "Yılın genel çerçevesi."},
            {"layout": "stats", "title": "Eylül fiyatları", "stats": [{"value": "6.843 TL", "label": "Gram altın"}, {"value": "%26", "label": "Ocak artışı"}]},
            {"layout": "chart", "title": "Ocak seyri", "chart": {"type": "line", "categories": [r[0] for r in JANUARY], "series": [{"name": "24 ayar", "values": [r[1] for r in JANUARY]}], "unit": "TL/gr"}, "bullets": ["Ay boyunca yükseldi."]},
            {"layout": "table", "title": "Ocak günlük fiyatlar", "columns": COLUMNS, "rows": [list(r) for r in JANUARY]},
            {"layout": "two_column", "title": "Makas", "left": {"heading": "Dar", "bullets": ["Gram"]}, "right": {"heading": "Geniş", "bullets": ["Çeyrek"]}},
            {"layout": "quote", "text": "Altın ilk tercih olmaya devam ediyor.", "attribution": "Piyasa yorumu"},
            {"layout": "section", "title": "Sonuçlar", "subtitle": "Ne öğrendik"},
            {"layout": "takeaways", "title": "Öne çıkanlar", "bullets": ["Ocak sert yükseldi.", "Eylül dengelendi."]},
        ],
        subtitle="Ocak ve Eylül 2026 verileri",
    )


def _open(data: bytes) -> Presentation:
    return Presentation(io.BytesIO(data))


def _slide_text(slide) -> str:
    parts = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            parts.append(shape.text_frame.text)
        if getattr(shape, "has_table", False) and shape.has_table:
            parts.extend(cell.text for row in shape.table.rows for cell in row.cells)
    return "\n".join(parts)


# --- measurement and splitting ------------------------------------------------------


def test_wrapping_grows_with_text_and_shrinks_with_width() -> None:
    short = document_pptx.wrap_count("Kısa bir cümle.", 20, 11.0)
    long = document_pptx.wrap_count("Uzun bir cümle " * 20, 20, 11.0)
    narrow = document_pptx.wrap_count("Uzun bir cümle " * 20, 20, 4.0)
    assert short == 1 and long > 1 and narrow > long


def test_split_balanced_evens_pages_out() -> None:
    assert document_pptx.split_balanced([1.0] * 20, 12.0) == [list(range(10)), list(range(10, 20))]
    chunks = document_pptx.split_balanced([1.0] * 9, 4.0)
    assert [len(c) for c in chunks] == [3, 3, 3]
    assert document_pptx.split_balanced([], 4.0) == []


def test_the_gold_table_is_two_even_table_slides_with_every_row() -> None:
    deck = deck_from_slides(
        "Altın",
        [
            {"layout": "table", "title": "Ocak günlük fiyatlar", "columns": COLUMNS, "rows": [list(r) for r in JANUARY]},
            {"layout": "bullets", "title": "Not", "bullets": ["Veriler exchange-rates.org'dan."]},
        ],
    )
    pages = [p for p in document_pptx.plan_deck(deck) if p.kind == "table"]
    assert [len(p.rows) for p in pages] == [10, 10]
    assert [p.continued for p in pages] == [False, True]
    assert all(len(p.col_widths) == 6 for p in pages)

    prs = _open(document_pptx.build_pptx(deck))
    tables = [s for s in prs.slides if any(getattr(sh, "has_table", False) and sh.has_table for sh in s.shapes)]
    text = "\n".join(_slide_text(s) for s in tables)
    for row in JANUARY:
        assert all(cell in text for cell in row), row
    assert "…" not in text
    # The header is repeated on the continuation slide.
    for slide in tables:
        table = next(sh.table for sh in slide.shapes if getattr(sh, "has_table", False) and sh.has_table)
        assert [c.text for c in table.rows[0].cells] == COLUMNS


def test_a_long_list_splits_without_orphans_or_orphaned_sub_bullets() -> None:
    bullets = []
    for n in range(12):
        bullets.append(Bullet(text=f"Madde {n}: " + "uzun bir açıklama cümlesi " * 3))
        if n % 3 == 0:
            bullets.extend([Bullet(text="alt madde bir", level=1), Bullet(text="alt madde iki", level=1)])
    slide = Slide(layout="bullets", title="Uzun liste", bullets=bullets)
    pages = document_pptx._plan_bullets(slide)
    assert len(pages) >= 2
    counts = [sum(1 for b in p.bullets if b.level == 0) for p in pages]
    assert min(counts) >= max(counts) - 2, counts
    for page in pages:
        assert page.bullets[0].level == 0  # a page never starts with a sub-bullet
    assert [b for p in pages for b in p.bullets] == bullets


def test_short_slides_get_large_type() -> None:
    slide = Slide(layout="bullets", title="Kısa", bullets=[Bullet(text="Tek cümle.")])
    assert document_pptx._plan_bullets(slide)[0].size == document_pptx.BULLET_SIZES[0]


def test_the_line_axis_starts_near_the_data() -> None:
    assert document_pptx.axis_floor([5994.66, 7566.27]) == pytest.approx(5500)
    assert document_pptx.axis_floor([2, 90]) is None  # far from zero already
    assert document_pptx.axis_floor([-1, 5]) is None


# --- the drawn file -------------------------------------------------------------------


def test_every_layout_draws_and_passes_verification() -> None:
    deck = _all_layouts_deck()
    data = document_pptx.build_pptx(deck, sources=SOURCES)
    prs = _open(data)
    planned = document_pptx.plan_deck(deck, sources=SOURCES)
    assert len(prs.slides) == len(planned)
    kinds = [p.kind for p in planned]
    assert kinds[:2] == ["title", "agenda"] and kinds[-1] == "sources"
    assert {"stats", "chart", "table", "two_column", "quote", "section", "takeaways"} <= set(kinds)

    report = document_verify.verify_document(
        "pptx", data, expected_headings=deck.slide_titles(), expect_sources=True
    )
    assert report.ok, report.problems

    # Footer page numbers, a native chart with the notes' data, speaker
    # notes, and clickable sources.
    assert f"2 / {len(planned)}" in _slide_text(prs.slides[1])
    chart_slide = prs.slides[kinds.index("chart")]
    chart = next(sh.chart for sh in chart_slide.shapes if sh.has_chart)
    assert list(chart.plots[0].categories) == [r[0] for r in JANUARY]
    assert chart.value_axis.minimum_scale is not None
    first_content = prs.slides[kinds.index("bullets")]
    assert first_content.notes_slide.notes_text_frame.text == "Yılın genel çerçevesi."
    links = [
        run.hyperlink.address
        for sh in prs.slides[-1].shapes if sh.has_text_frame
        for p in sh.text_frame.paragraphs for run in p.runs if run.hyperlink.address
    ]
    assert links == [url for _, url in SOURCES]


def test_bold_marks_become_bold_runs_not_asterisks() -> None:
    deck = _all_layouts_deck()
    prs = _open(document_pptx.build_pptx(deck))
    slide = prs.slides[2]
    runs = [r for sh in slide.shapes if sh.has_text_frame for p in sh.text_frame.paragraphs for r in p.runs]
    assert any(r.text == "dalgalı" and r.font.bold for r in runs)
    assert "**" not in _slide_text(slide)


def test_long_sentences_are_never_clipped() -> None:
    sentence = "Eylül 2026 itibarıyla gram altın 6.842-6.843 TL bandında işlem görüyor ve bu seviye Ocak zirvesinin belirgin biçimde altında kalıyor."
    deck = deck_from_slides("Altın", [
        {"layout": "bullets", "title": "Eylül", "bullets": [sentence]},
        {"layout": "bullets", "title": "Not", "bullets": ["Kısa."]},
    ])
    text = "\n".join(_slide_text(s) for s in _open(document_pptx.build_pptx(deck)).slides)
    assert sentence in text


def test_verification_catches_text_that_overflows_its_box() -> None:
    from pptx.util import Inches, Pt

    prs = Presentation()
    for _ in range(2):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(0.5))
        box.text_frame.word_wrap = True
        run = box.text_frame.paragraphs[0].add_run()
        run.text = "Bu kutuya sığmayacak kadar uzun bir metin. " * 6
        run.font.size = Pt(20)
    out = io.BytesIO()
    prs.save(out)
    report = document_verify.verify_document("pptx", out.getvalue())
    assert not report.ok
    assert any("overflows" in p for p in report.problems)


# --- through the artifact tool ------------------------------------------------------------


def _tools() -> tuple[ArtifactTools, Path]:
    root = Path(tempfile.mkdtemp(prefix="pptx_ws_")).resolve()
    (root / "sources").mkdir(parents=True)
    tools = ArtifactTools(root)
    tools._fetch_images = lambda *a, **k: []  # no network in tests
    return tools, root


def test_create_pptx_artifact_draws_typed_slides() -> None:
    tools, root = _tools()
    deck = _all_layouts_deck()
    result = tools.create_pptx_artifact(
        "altin.pptx",
        title="Türkiye Altın Fiyatları 2026",
        summary=deck.to_markdown()
        + "\n\nKaynaklar:\n- Türkiye Altın Fiyatları 2026 (https://www.exchange-rates.org/tr/altin/2026)",
        slides=json.dumps([s.model_dump() for s in deck.slides], ensure_ascii=False),
        subtitle=deck.subtitle,
    )
    assert result["verification"]["ok"] is True and result["verification"]["path"] == "slides"
    prs = Presentation(str(root / result["path"]))
    assert "Kaynaklar" in _slide_text(prs.slides[-1])
    assert "exchange-rates.org" in _slide_text(prs.slides[-1])


def test_create_pptx_artifact_without_slides_keeps_the_notes_driven_deck() -> None:
    tools, root = _tools()
    result = tools.create_pptx_artifact(
        "eski.pptx",
        title="Zürafa",
        summary="## Fiziksel özellikler\n- Boyları 5,5 metreye ulaşır.\n- Ağırlıkları 1.200 kg.\n\n## Beslenme\n- Akasya yaprakları.",
    )
    assert result["verification"]["path"] == "legacy_pptx"
    prs = Presentation(str(root / result["path"]))
    assert len(prs.slides) >= 3


def test_create_pptx_artifact_from_blocks_is_one_slide_per_section() -> None:
    tools, root = _tools()
    blocks = [
        {"type": "heading", "level": 1, "text": "Fiziksel özellikler"},
        {"type": "paragraph", "text": "Boyları 5,5 metreye ulaşır."},
        {"type": "heading", "level": 1, "text": "Beslenme"},
        {"type": "list", "items": ["Akasya yaprakları", "Tohum kabukları"]},
    ]
    result = tools.create_pptx_artifact(
        "zurafa.pptx", title="Zürafa", summary="x", blocks=json.dumps(blocks, ensure_ascii=False)
    )
    assert result["verification"]["path"] == "slides"
    text = "\n".join(_slide_text(s) for s in Presentation(str(root / result["path"])).slides)
    assert "Fiziksel özellikler" in text and "Tohum kabukları" in text


def test_a_renderer_crash_falls_back_to_the_notes_driven_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    tools, root = _tools()

    def boom(*_a, **_k):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(document_pptx, "build_pptx", boom)
    deck = _all_layouts_deck()
    result = tools.create_pptx_artifact(
        "altin.pptx",
        title="Altın",
        summary=deck.to_markdown(),
        slides=json.dumps([s.model_dump() for s in deck.slides], ensure_ascii=False),
    )
    assert result["verification"]["path"] == "legacy_pptx"
    assert (root / result["path"]).is_file()


def test_slide_model_roundtrip_is_what_the_worker_sends() -> None:
    """_write_artifact sends model_dump() output; it must parse back as-is."""
    deck = _all_layouts_deck()
    again = slide_model.deck_from_slides(deck.title, [s.model_dump() for s in deck.slides])
    assert [s.layout for s in again.slides] == [s.layout for s in deck.slides]


def test_a_stat_too_long_for_its_card_wraps_instead_of_overflowing() -> None:
    """Live: "800–1.200 kg" on a four-card slide did not fit one line even at
    the smallest size, and its box was sized for one."""
    deck = deck_from_slides(
        "Zürafa",
        [
            {
                "layout": "stats",
                "title": "Fiziksel özellikler",
                "stats": [
                    {"value": "4,4–5,5 metre", "label": "Boy"},
                    {"value": "800–1.200 kilogram", "label": "Ağırlık"},
                    {"value": "45–50 cm", "label": "Dil"},
                    {"value": "7 omur", "label": "Boyun"},
                ],
            },
            {"layout": "bullets", "title": "Not", "bullets": ["Kısa."]},
        ],
    )
    data = document_pptx.build_pptx(deck)
    report = document_verify.verify_document("pptx", data, expected_headings=deck.slide_titles())
    assert report.ok, report.problems
