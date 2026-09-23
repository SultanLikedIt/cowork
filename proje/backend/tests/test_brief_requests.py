"""A pasted brief: the request carries the material the file is made from.

Live: a 13k-char "Aşağıdaki bilgileri kullanarak Türkçe bir döküman hazırla"
brief about this product. Every keyword detector read the material as the
instruction -- "Word / PDF / PowerPoint / Excel / HTML dosyası üretir" gave
three files, "Kaynak kod" / "Ek: Referans" scheduled a web search that
appended two unrelated sources, "görsel yükleme" added a stock photo -- and
with three formats the notes were cut to 4.5k chars, so after a Document
timeout the .docx stopped mid-list with "…[truncated]" printed in it.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from docx import Document

pytest.importorskip("langgraph")

import capabilities  # noqa: E402
from artifacts import ArtifactTools  # noqa: E402
from execution.langgraph_runtime import workforce  # noqa: E402

SECTIONS = [
    ("Proje nedir", "agent-webapp tarayıcıdan konuşulan bir ajan uygulamasıdır; Word / PDF / PowerPoint / Excel / HTML dosyası üretir."),
    ("Rakamlar", "Kaynak kod ~70 bin satırdan ~102 bin satıra çıktı. Test sayısı 624'ten 1.243'e yükseldi."),
    ("Devralınan temel", "Docling ile belge okuma vardı ama OCR kapalıydı; görsel yükleme reddediliyordu."),
    ("Önce / Sonra", "Veri kaynakları yalnız web ve dosyaydı; şimdi Outlook, Reddit, Jira ve GitHub da var."),
    ("Güvenlik", "Web kaynak seçimi artık yerel reranker ile yapılıyor; dış içerik izole ediliyor."),
    ("Sıradaki adımlar", "Knowledge dalının ana dala alınması ve Outlook takvim entegrasyonu."),
]
BRIEF = (
    "Aşağıdaki bilgileri kullanarak **Türkçe bir döküman** hazırla. Commit "
    "hash'lerini istersen en sona \"Ek: Referans\" olarak ekle.\n\n"
    + "\n\n".join(f"## {title}\n{body} " + "Ayrıntı cümlesi. " * 25 for title, body in SECTIONS)
)


class _Callback:
    _loop = None
    _queue = None


def test_the_brief_is_long_enough_to_be_one() -> None:
    assert len(BRIEF) >= workforce._BRIEF_MIN_CHARS
    assert workforce._request_is_brief(BRIEF)
    assert not workforce._request_is_brief("İstanbul hakkında kısa bir PDF hazırla")


def test_formats_named_in_a_brief_are_content_not_orders() -> None:
    assert workforce._infer_artifact_kinds("docx", BRIEF) == ["docx"]


def test_a_short_request_still_gets_every_format_it_names() -> None:
    assert workforce._infer_artifact_kinds("docx", "raporu word ve pdf olarak ver") == ["docx", "pdf"]


def test_a_list_of_formats_is_not_an_order_for_each() -> None:
    text = "uygulamamız Word, PDF, PowerPoint, Excel ve HTML üretir; bunu anlatan bir docx hazırla"
    assert workforce._infer_artifact_kinds("docx", text) == ["docx"]


def test_a_brief_becomes_one_complete_document_without_a_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="brief_")).resolve()
    (root / "sources").mkdir(parents=True)
    stages: list[str] = []
    fetched: list[str] = []

    blocks = []
    for title, body in SECTIONS:
        blocks.append({"type": "heading", "level": 1, "text": title})
        blocks.append({"type": "paragraph", "text": body})

    async def fake_complete_chat(messages, **kwargs):
        stage = kwargs.get("stage", "")
        stages.append(stage)
        if stage == "document_agent":
            # The Document writer is given the whole brief, not a cut copy.
            assert "Sıradaki adımlar" in messages[-1]["content"]
            return json.dumps({"filename": "gelisim.docx", "title": "Gelişim Özeti", "blocks": blocks}, ensure_ascii=False)
        raise AssertionError(f"unexpected model call: {stage}")

    def no_images(self, *args, **kwargs):
        fetched.append(str(args[:1]))
        return ArtifactTools._fetch_images_original(self, *args, **kwargs)

    monkeypatch.setenv("DOCUMENT_BLOCKS", "true")
    monkeypatch.setattr(workforce, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(ArtifactTools, "_fetch_images_original", ArtifactTools._fetch_images, raising=False)
    monkeypatch.setattr(
        "image_fetch.fetch_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a photo was fetched")),
    )

    result = workforce.run_workforce_in_thread(
        _Callback(),
        workforce.UsageTracker(),
        None,
        BRIEF,
        "brief-1",
        lambda handle: None,
        workspace_root=str(root),
        job_capabilities=frozenset({capabilities.ARTIFACTS}),
        original_request=BRIEF,
        wants_artifact=True,
        artifact_type="docx",
    )

    assert stages == ["document_agent"], stages  # no web plan/search/answer
    files = sorted(p.name for p in (root / "artifacts").iterdir() if p.is_file())
    assert files == ["gelisim.docx"], files
    text = "\n".join(p.text for p in Document(str(root / "artifacts" / "gelisim.docx")).paragraphs)
    for title, _ in SECTIONS:
        assert title in text
    assert "…[truncated]" not in text
    assert "doğrulanamadı" not in text
    # Nothing was researched, so there is nothing to cite -- and no
    # "no verified page found" notice either.
    assert "Kaynaklar" not in text and "bulunamadı" not in text
    assert result is not None


def test_a_document_timeout_writes_the_whole_notes_not_the_cut_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="brief_fallback_")).resolve()
    (root / "sources").mkdir(parents=True)
    notes = "\n\n".join(f"## Bölüm {n}\n" + f"Bölüm {n} içeriği. " * 60 for n in range(1, 13))
    assert len(notes) > 12_000
    notes += "\n\n---\nRESEARCH_SOURCES: " + json.dumps(
        [{"url": "https://example.com/rapor", "title": "Örnek kaynak"}]
    )

    async def empty_reply(messages, **kwargs):
        return ""

    monkeypatch.setenv("DOCUMENT_BLOCKS", "true")
    monkeypatch.setattr(workforce, "complete_chat", empty_reply)
    monkeypatch.setattr(ArtifactTools, "_fetch_images", lambda self, *a, **k: [])
    asyncio.run(
        workforce._run_document_worker(
            content="bu notlardan word ve pdf raporu hazırla",
            original_request="bu notlardan word ve pdf raporu hazırla",
            prior_notes=notes,
            tracker=workforce.UsageTracker(),
            callback=_Callback(),
            skills=None,
            handle=workforce.CancellableWorkforce(),
            unit_task_id="t.0",
            workspace_root=str(root),
            artifact_type="docx",
            tool_call_recorder=None,
        )
    )
    docx_files = list((root / "artifacts").glob("*.docx"))
    assert docx_files
    text = "\n".join(p.text for p in Document(str(docx_files[0])).paragraphs)
    assert "Bölüm 12" in text
    assert "[truncated]" not in text
