"""Charts: the Coding Agent saves them, the chat previews them, the report
embeds them.

The end-to-end test draws a real matplotlib chart in a subprocess and has the
Document Agent write a real PDF from a scripted reply; the PDF is then opened
and checked for the embedded image.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import capabilities
import code_controller as cc


# --------------------------------------------------------------------------
# CHART lines
# --------------------------------------------------------------------------


def test_chart_lines_are_parsed_in_order_and_deduped() -> None:
    stdout = (
        "Toplam: 35\n"
        "CHART: artifacts/bolge.png | Bölgelere göre ciro\n"
        "CHART: artifacts/trend.jpg\n"
        "CHART: artifacts/bolge.png | tekrar\n"
    )
    assert cc.parse_charts(stdout) == [
        ("artifacts/bolge.png", "Bölgelere göre ciro"),
        ("artifacts/trend.jpg", ""),
    ]


def test_chart_lines_outside_artifacts_are_ignored() -> None:
    stdout = (
        "CHART: sources/x/gizli.png | no\n"
        "CHART: artifacts/../sources/x/gizli.png | no\n"
        "CHART: /etc/passwd.png | no\n"
        "CHART: artifacts/rapor.pdf | not an image\n"
    )
    assert cc.parse_charts(stdout) == []


def test_at_most_six_charts() -> None:
    stdout = "\n".join(f"CHART: artifacts/g{i}.png | {i}" for i in range(10))
    assert len(cc.parse_charts(stdout)) == cc.MAX_CHARTS


def test_the_prompt_offers_charts_only_when_matplotlib_exists() -> None:
    if cc.HAS_MATPLOTLIB:
        assert "CHART: artifacts/<name>.png" in cc.WRITE_SYSTEM_PROMPT
        assert "never plt.show()" in cc.WRITE_SYSTEM_PROMPT
    else:
        assert "do not produce charts" in cc.WRITE_SYSTEM_PROMPT


def test_the_child_has_a_headless_plot_backend(tmp_path) -> None:
    from code_exec import CodeExecutionTools

    env = CodeExecutionTools(tmp_path)._child_env()
    assert env["MPLBACKEND"] == "Agg"
    assert "MODEL_API_KEY" not in env


def test_charts_are_served_as_images_not_svg() -> None:
    import main

    assert main.ARTIFACT_MEDIA_TYPES[".png"] == "image/png"
    assert main.ARTIFACT_MEDIA_TYPES[".jpg"] == "image/jpeg"
    assert ".svg" not in main.ARTIFACT_MEDIA_TYPES


def test_the_chat_previews_image_artifacts() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "ChatExchange.jsx"
    ).read_text(encoding="utf-8")
    assert "PREVIEW_KINDS = new Set(['png', 'jpg', 'jpeg'])" in source
    assert "<img" in source


# --------------------------------------------------------------------------
# Document side
# --------------------------------------------------------------------------

pytest.importorskip("langgraph")
from execution.langgraph_runtime import workforce  # noqa: E402


def test_only_charts_really_on_disk_under_artifacts_are_embedded(tmp_path) -> None:
    root = tmp_path / "job"
    (root / "artifacts").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "artifacts" / "var.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "sources" / "upload.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    notes = (
        "CHART: artifacts/var.png | Var olan\n"
        "CHART: artifacts/yok.png | Diskte yok\n"
    )
    charts = workforce._coding_charts(str(root), notes)
    assert [(Path(c.path).name, c.attribution) for c in charts] == [("var.png", "Var olan")]
    assert workforce._coding_charts(None, notes) == []


def test_a_computed_chart_is_embedded_in_the_pdf_report(tmp_path, monkeypatch) -> None:
    pytest.importorskip("matplotlib")
    pypdf = pytest.importorskip("pypdf")
    # This test is about embedding, not about the PDF engine.
    monkeypatch.setenv("DOCUMENT_PDF_ENGINE", "reportlab")
    # The shipped setting (docker-compose): reports are written as typed blocks.
    monkeypatch.setenv("DOCUMENT_BLOCKS", "true")

    program = (
        "import matplotlib.pyplot as plt\n"
        "bolgeler = ['Marmara', 'Ege', 'Akdeniz']\n"
        "ciro = [120, 80, 45]\n"
        "plt.figure(figsize=(5, 3))\n"
        "plt.bar(bolgeler, ciro)\n"
        "plt.title('Bölgelere göre ciro')\n"
        "plt.savefig('artifacts/bolge-ciro.png', dpi=100, bbox_inches='tight')\n"
        "plt.close()\n"
        "print('Marmara 120, Ege 80, Akdeniz 45')\n"
        "print('CHART: artifacts/bolge-ciro.png | Bölgelere göre ciro (bin TL)')\n"
    )
    document_reply = json.dumps(
        {
            "filename": "ciro-raporu.pdf",
            "title": "Bölgesel Ciro Raporu",
            "summary": "Marmara en yüksek ciroya sahip.",
            "blocks": [
                {"type": "heading", "level": 1, "text": "Bölgesel ciro"},
                {"type": "paragraph", "text": "Marmara 120 bin TL ile ilk sırada, Ege 80, Akdeniz 45."},
                {"type": "image", "caption": ""},
                {"type": "heading", "level": 1, "text": "Sonuç"},
                {"type": "paragraph", "text": "Toplam ciro 245 bin TL."},
            ],
        },
        ensure_ascii=False,
    )
    systems: dict[str, str] = {}
    replies = {
        "coding_write": [f"MODE: run\nPLAN: Grafik.\n```python\n{program}```"],
        "coding_answer": ["Marmara 120, Ege 80, Akdeniz 45 bin TL."],
        "document_agent": [document_reply],
    }

    async def fake_complete_chat(messages, **kwargs):
        stage = kwargs.get("stage", "")
        systems[stage] = messages[0]["content"]
        queue = replies.get(stage)
        if not queue:
            raise AssertionError(f"unexpected model call at stage {stage!r}")
        return queue.pop(0)

    monkeypatch.setattr(workforce, "complete_chat", fake_complete_chat)

    class Callback:
        _loop = None
        _queue = None

    root = tmp_path / "job"
    root.mkdir()
    request = "bölgelere göre ciroyu hesapla, grafikli pdf rapor ver"
    workforce.run_workforce_in_thread(
        Callback(),
        workforce.UsageTracker(),
        None,
        request,
        "job-chart",
        lambda handle: None,
        workspace_root=str(root),
        job_capabilities=frozenset({capabilities.CODE_EXEC, capabilities.ARTIFACTS}),
        original_request=request,
        wants_artifact=True,
        artifact_type="pdf",
    )

    document_system = systems["document_agent"]
    assert "CHARTS computed from the data" in document_system
    assert "1. Bölgelere göre ciro (bin TL)" in document_system
    assert "Do not reproduce the source code" in document_system
    # Computed notes are material, not a failed search.
    assert "CRITICAL: Research notes say sources failed" not in document_system

    names = sorted(p.name for p in (root / "artifacts").iterdir())
    assert "bolge-ciro.png" in names
    pdf_path = next((root / "artifacts").glob("*.pdf"))
    reader = pypdf.PdfReader(str(pdf_path))
    embedded = sum(len(page.images) for page in reader.pages)
    assert embedded >= 1
    text = "".join(page.extract_text() or "" for page in reader.pages)
    # The concise write-up survived: it was not swapped for the raw notes,
    # which would have printed the program into the report.
    assert "import matplotlib" not in text
    assert "Toplam ciro 245" in text


def test_coding_prose_drops_code_output_and_labels() -> None:
    reply = (
        "Marmara 120 ile önde.\n\n**Kod**\n\n```python\nprint(1)\n```\n\n"
        "**Çıktı**\n\n```text\n1\n```\n\n_1 çalıştırma, son çalıştırma 0.1 sn_\n\n"
        "📎 Oluşturulan dosyalar: `a.png`"
    )
    assert workforce._coding_notes_prose(reply) == "Marmara 120 ile önde."


def test_coding_notes_never_take_the_copy_notes_into_the_file_shortcut() -> None:
    import inspect

    source = inspect.getsource(workforce._run_document_worker)
    assert "not coding_ran and _notes_ready_for_direct_deliverable(" in source


def test_a_program_written_pdf_does_not_replace_the_designed_report(tmp_path) -> None:
    root = tmp_path / "job"
    (root / "artifacts").mkdir(parents=True)
    (root / "artifacts" / "grafikler.pdf").write_bytes(b"%PDF-1.4")
    assert not workforce._coding_delivered_requested_files(str(root), "pdf", "grafikli pdf rapor ver")


def _fake_response(content, reasoning, finish="stop"):
    from types import SimpleNamespace

    message = SimpleNamespace(content=content, reasoning=reasoning, model_extra={})
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=None,
        model_dump=lambda: {},
    )


@pytest.mark.parametrize(
    "enabled, finish, expected",
    [
        (True, "stop", "MODE: run\n```python\nprint(1)\n```"),
        (False, "stop", ""),
        (True, "length", ""),
    ],
)
def test_reasoning_is_used_only_when_asked_and_complete(monkeypatch, enabled, finish, expected) -> None:
    import asyncio

    from execution.langgraph_runtime import openai_chat

    response = _fake_response(None, "MODE: run\n```python\nprint(1)\n```", finish)

    async def fake_create(client, kwargs, *, stage):
        return response

    monkeypatch.setattr(openai_chat, "_create_with_retry", fake_create)
    monkeypatch.setattr(openai_chat, "_client", lambda: None)
    monkeypatch.setattr(openai_chat, "_log_call", lambda *a, **k: None)
    text = asyncio.run(
        openai_chat.complete_chat(
            [{"role": "user", "content": "x"}], stage="coding_write", reasoning_fallback=enabled
        )
    )
    assert text == expected
