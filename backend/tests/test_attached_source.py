"""An attached file is the source of a file made from it.

Live: "Yüklediğim belgeden Türkçe bir PowerPoint sunumu hazırla ... Ek:
Referans bölümünü son slayta koy" with a 13k-char .docx attached. "Referans"
scheduled a web search (4.5 minutes of Canva tutorials, then "your document
is not in the evidence"), and the classifier's reads_documents sent the
Knowledge Agent through the whole workspace library -- unrelated contracts
included -- for another 6 minutes. The user cancelled.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

import artifact_intent  # noqa: E402
import capabilities  # noqa: E402
from artifacts import ArtifactTools  # noqa: E402
from execution.langgraph_runtime import workforce  # noqa: E402

REQUEST = (
    "Yüklediğim belgeden Türkçe bir PowerPoint sunumu hazırla. Her başlık bir slayt olsun; "
    'önce/sonra tablolarını ve rakamları olduğu gibi koru. "Ek: Referans" bölümünü son slayta koy.'
)


def _record(text: str) -> dict:
    return {"file_id": "f1", "filename": "ozet.docx", "kind": "file", "extracted_text": text}


def test_attachments_read_whole() -> None:
    import main

    assert main._attachments_read_whole([_record("kısa belge")])
    assert not main._attachments_read_whole([])
    assert not main._attachments_read_whole([_record("x" * (main._CONTEXT_WHOLE_CHARS + 1))])


def _classified(**flags) -> artifact_intent.ArtifactIntent:
    base = dict(
        wants_artifact=False, artifact_type="none", needs_web_research=False,
        needs_code_execution=False, reuses_uploaded_document=False,
        opens_on_user_computer=False, reads_documents=False, source="llm",
    )
    base.update(flags)
    return artifact_intent.ArtifactIntent(**{k: v for k, v in base.items() if k in artifact_intent.ArtifactIntent.__dataclass_fields__})


def _route(monkeypatch: pytest.MonkeyPatch, intent, *, whole: bool):
    import main

    async def fake_classify(*args, **kwargs):
        return intent

    monkeypatch.setattr(artifact_intent, "classify_artifact_intent", fake_classify)
    decision, _ = asyncio.run(
        main._routing_for_workforce(
            REQUEST,
            has_workspace_files=True,
            workspace_file_kinds=["docx"],
            tracker=None,
            documents_available=True,
            attachments_read_whole=whole,
        )
    )
    return decision.declared if hasattr(decision, "declared") else decision.capabilities


def test_a_file_made_from_a_whole_attachment_does_not_search_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _classified(wants_artifact=True, artifact_type="pptx", reads_documents=True)
    caps = _route(monkeypatch, intent, whole=True)
    assert capabilities.DOCUMENTS_READ not in caps


def test_a_question_about_documents_still_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    intent = _classified(wants_artifact=False, reads_documents=True)
    caps = _route(monkeypatch, intent, whole=True)
    assert capabilities.DOCUMENTS_READ in caps


def test_an_attachment_too_large_to_read_whole_is_still_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _classified(wants_artifact=True, artifact_type="pptx", reads_documents=True)
    caps = _route(monkeypatch, intent, whole=False)
    assert capabilities.DOCUMENTS_READ in caps


class _Callback:
    _loop = None
    _queue = None


def test_a_sources_word_does_not_web_search_an_attached_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="attached_")).resolve()
    (root / "sources").mkdir(parents=True)
    (root / "sources" / "ozet.docx").write_bytes(b"docx")
    stages: list[str] = []

    slides = [
        {"layout": "bullets", "title": "Proje nedir?", "bullets": ["Tarayıcıdan konuşulan bir ajan uygulaması."]},
        {"layout": "bullets", "title": "Ek: Referans", "bullets": ["0836338: başlangıç noktası"]},
    ]

    async def fake_complete_chat(messages, **kwargs):
        stage = kwargs.get("stage", "")
        stages.append(stage)
        if stage == "document_agent":
            return json.dumps({"filename": "sunum.pptx", "title": "agent-webapp", "slides": slides}, ensure_ascii=False)
        raise AssertionError(f"unexpected model call: {stage}")

    monkeypatch.setattr(workforce, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(ArtifactTools, "_fetch_images", lambda self, *a, **k: [])
    workforce.run_workforce_in_thread(
        _Callback(),
        workforce.UsageTracker(),
        None,
        REQUEST + "\n\n[ozet.docx]\nagent-webapp tarayıcıdan konuşulan bir ajan uygulamasıdır.",
        "attached-1",
        lambda handle: None,
        workspace_root=str(root),
        job_capabilities=frozenset({capabilities.ARTIFACTS, capabilities.FILES}),
        original_request=REQUEST,
        wants_artifact=True,
        artifact_type="pptx",
    )
    assert stages == ["document_agent"], stages  # no web plan / search / answer
    assert [p.name for p in (root / "artifacts").glob("*.pptx")] == ["sunum.pptx"]
