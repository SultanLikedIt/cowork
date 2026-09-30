"""The Document worker on a .pptx ask, end to end with a scripted model.

Before typed slides, the worker asked for a markdown outline, then almost
always replaced it with the (longer) research notes, and build_pptx cut
those into four clipped bullets per slide. These tests pin the new
contract: the model's slides are what gets drawn, their figures are
checked against the notes, and a reply that is not a deck still produces
a file.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from pptx import Presentation

pytest.importorskip("langgraph")

from artifacts import ArtifactTools  # noqa: E402
from execution.langgraph_runtime import workforce  # noqa: E402

ROWS = [("1 Oca", "5.997,11"), ("2 Oca", "5.994,66"), ("5 Oca", "6.156,99"), ("28 Oca", "7.566,27")]
NOTES = (
    "## Genel görünüm\nGram altın Ocak 2026'da 5.994,66 TL ile 7.566,27 TL arasında "
    "değişti ve ay içinde %26 yükseldi. Eylül'de gram altın 6.843,34 TL.\n\n"
    "## Ocak verileri\n| Tarih | 24 Ayar |\n|---|---|\n"
    + "\n".join(f"| {d} | {v} |" for d, v in ROWS)
    + "\n\n---\nRESEARCH_SOURCES: "
    + json.dumps([{"url": "https://www.exchange-rates.org/tr/altin/2026", "title": "Altın Fiyatları 2026"}])
)
REQUEST = "türkiye altın fiyatları hakkında powerpoint sunumu hazırla"
REPLY = {
    "filename": "altin.pptx",
    "title": "Türkiye Altın Fiyatları 2026",
    "subtitle": "Ocak ve Eylül 2026 fiyatları",
    "slides": [
        {"layout": "bullets", "title": "Genel görünüm", "bullets": ["Gram altın Ocak'ta **%26** yükseldi."], "notes": "Çerçeve."},
        {
            "layout": "stats",
            "title": "Öne çıkan rakamlar",
            "stats": [
                {"value": "%26", "label": "Ocak artışı"},
                {"value": "6.843,34 TL", "label": "Eylül gram altın"},
                {"value": "%41", "label": "Yıllık artış"},  # not in the notes
            ],
        },
        {"layout": "table", "title": "Ocak verileri", "columns": ["Tarih", "24 Ayar"], "rows": [list(r) for r in ROWS]},
        {"layout": "takeaways", "title": "Sonuç", "bullets": ["Ocak sert yükseldi.", "Eylül'de fiyatlar dengelendi."]},
    ],
}


class _Callback:
    _loop = None
    _queue = None


def _run(monkeypatch: pytest.MonkeyPatch, reply: str) -> tuple[Path, str, dict]:
    root = Path(tempfile.mkdtemp(prefix="slides_worker_")).resolve()
    (root / "sources").mkdir(parents=True)
    seen: dict = {}

    async def fake_complete_chat(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["kwargs"] = kwargs
        return reply

    monkeypatch.setattr(workforce, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(ArtifactTools, "_fetch_images", lambda self, *a, **k: [])
    output = asyncio.run(
        workforce._run_document_worker(
            content=REQUEST,
            original_request=REQUEST,
            prior_notes=NOTES,
            tracker=workforce.UsageTracker(),
            callback=_Callback(),
            skills=None,
            handle=workforce.CancellableWorkforce(),
            unit_task_id="t.0",
            workspace_root=str(root),
            artifact_type="pptx",
            tool_call_recorder=None,
        )
    )
    return root, output, seen


def _deck_text(path: Path) -> str:
    parts = []
    for slide in Presentation(str(path)).slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                parts.extend(c.text for row in shape.table.rows for c in row.cells)
    return "\n".join(parts)


def test_the_models_slides_are_what_gets_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCUMENT_SLIDES", raising=False)
    root, output, seen = _run(monkeypatch, json.dumps(REPLY, ensure_ascii=False))

    assert '"layout": "stats"' in seen["system"] and seen["kwargs"]["json_mode"] is True
    files = list((root / "artifacts").glob("*.pptx"))
    assert len(files) == 1, output
    prs = Presentation(str(files[0]))
    text = _deck_text(files[0])

    # A real table with every row, not bullets built from the notes.
    tables = [
        shape.table
        for slide in prs.slides
        for shape in slide.shapes
        if getattr(shape, "has_table", False) and shape.has_table
    ]
    assert len(tables) == 1 and len(tables[0].rows) == len(ROWS) + 1
    # The invented figure was dropped; the notes' own figures stayed.
    assert "%41" not in text and "Yıllık artış" not in text
    assert "6.843,34 TL" in text and "%26" in text
    # Title, subtitle, server-built sources slide.
    assert "Türkiye Altın Fiyatları 2026" in text and "Ocak ve Eylül 2026 fiyatları" in text
    assert "Kaynaklar" in text and "exchange-rates.org" in text
    assert prs.slides[2].notes_slide.notes_text_frame.text == "Çerçeve."


def test_a_reply_that_is_not_a_deck_still_writes_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCUMENT_SLIDES", raising=False)
    root, output, _ = _run(monkeypatch, json.dumps({"filename": "altin.pptx", "title": "Altın", "slides": []}))
    files = list((root / "artifacts").glob("*.pptx"))
    assert len(files) == 1, output
    # The notes-driven deck: built from the research notes.
    assert "7.566,27" in _deck_text(files[0])


def test_the_flag_off_keeps_the_old_outline_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCUMENT_SLIDES", "false")
    _root, _output, seen = _run(
        monkeypatch, json.dumps({"filename": "a.pptx", "title": "Altın", "content": "## A\n- b"})
    )
    assert "PowerPoint outline" in seen["system"]
    assert '"layout": "stats"' not in seen["system"]


def test_the_models_deck_title_is_not_replaced_by_a_slide_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live: a giraffe deck titled "Dolaşım Sistemi Uyumları" (one of its
    slides) over the model's own "Zürafalar" -- the payload sanitizer
    re-derived the title from title + body, and a deck's body is its
    slide titles."""
    monkeypatch.delenv("DOCUMENT_SLIDES", raising=False)
    reply = dict(REPLY, title="Zürafalar")
    reply["slides"] = [
        {"layout": "bullets", "title": "Dolaşım Sistemi Uyumları", "bullets": ["Güçlü kalp kanı beyne pompalar."]},
        {"layout": "bullets", "title": "Beslenme", "bullets": ["Akasya yaprakları ile beslenir."]},
    ]
    root, output, _ = _run(monkeypatch, json.dumps(reply, ensure_ascii=False))
    files = list((root / "artifacts").glob("*.pptx"))
    assert len(files) == 1, output
    first = Presentation(str(files[0])).slides[0]
    title_text = "\n".join(s.text_frame.text for s in first.shapes if s.has_text_frame)
    assert "Zürafalar" in title_text
    assert "Dolaşım" not in title_text
