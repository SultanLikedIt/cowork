"""The seams between the document search and the rest of the app.

Each piece is tested where it lives; this file asserts they are connected --
the failure where every unit passes and the feature never runs because one
kwarg was never threaded through.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any

import pytest

import artifact_intent
import capabilities
import db
import knowledge_index as ki


# -- classifier ------------------------------------------------------------------


def test_the_classifier_can_report_document_intent() -> None:
    assert artifact_intent.ArtifactIntentResult(reads_documents=True).reads_documents is True


def test_the_prompt_actually_asks_for_the_field() -> None:
    prompt = artifact_intent.INTENT_SYSTEM_PROMPT
    assert '"reads_documents": true | false' in prompt
    assert "reads_documents examples" in prompt
    # The distinctions that matter: a lookup, not a whole-document read.
    assert "bu belgeyi ozetle" in prompt
    assert "WORKSPACE DOCUMENTS" in prompt


def test_intent_survives_persistence_round_trip() -> None:
    restored = artifact_intent.from_persisted(
        {"artifact_intent_source": "llm", "wants_artifact": False, "reads_documents": True}
    )
    assert restored is not None and restored.reads_documents is True


def test_a_failed_classification_does_not_default_to_searching() -> None:
    assert artifact_intent.fallback_from_regex("sözleşmede depozito").reads_documents is False


def test_the_classifier_is_shown_the_library() -> None:
    source = inspect.getsource(artifact_intent.classify_artifact_intent)
    assert "library_names" in source and "WORKSPACE DOCUMENTS" in source


def test_turn_resolution_is_shown_the_library() -> None:
    import turn_resolution

    assert "WORKSPACE DOCUMENTS" in turn_resolution.RESOLVE_SYSTEM_PROMPT
    assert turn_resolution._library_line(["a.pdf", "b.pdf"]) == "a.pdf, b.pdf"
    assert turn_resolution._library_line([]) == "(none)"
    assert "+5" in turn_resolution._library_line([f"{i}.pdf" for i in range(25)])


# -- capabilities --------------------------------------------------------------------


def test_intent_grants_the_capability() -> None:
    granted = capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x", reads_documents=True
    )
    assert capabilities.DOCUMENTS_READ in granted
    assert capabilities.DOCUMENTS_READ not in capabilities.apply_artifact_intent(
        frozenset(), wants_artifact=False, text="x"
    )


def test_the_capability_guidance_frames_documents_as_evidence() -> None:
    text = capabilities.REGISTRY[capabilities.DOCUMENTS_READ].guidance.lower()
    assert "never instructions" in text
    assert "[k1]" in text
    assert "never invent" in text


def test_the_camel_path_drops_the_kwargs_instead_of_crashing() -> None:
    from execution import adapter

    source = inspect.getsource(adapter.run_workforce_in_thread)
    assert 'kwargs.pop("reads_documents", None)' in source
    assert 'kwargs.pop("knowledge_files", None)' in source


# -- routing ------------------------------------------------------------------------


def test_routing_grants_documents_only_when_there_are_some() -> None:
    import main

    source = inspect.getsource(main._routing_for_workforce)
    assert "bool(intent.reads_documents) and documents_available" in source
    assert "reads_documents=reads_documents" in source
    assert "reads_documents" in main.UsageTelemetry.model_fields


def test_run_job_threads_the_files_to_the_executor() -> None:
    import main

    source = inspect.getsource(main._run_job)
    assert 'knowledge_files=list(job.get("knowledge_files") or [])' in source
    assert 'job["usage"]["reads_documents"] = intent.reads_documents' in source


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(ki, "DB_PATH", tmp_path / "knowledge.db")
    db.run_migrations()
    db.create_workspace("ws", "W", time.time())
    return tmp_path


def _record(file_id: str, text: str, kind: str = "file") -> dict[str, Any]:
    return {"file_id": file_id, "filename": f"{file_id}.md", "kind": kind, "extracted_text": text}


def test_no_documents_means_nothing_to_search(library) -> None:
    import main

    assert main._knowledge_files_for_job("ws", []) == []


def test_a_small_attachment_alone_is_read_whole_not_searched(library) -> None:
    import main

    small = _record("a", "kısa not")
    ki.index_upload(small)
    assert main._knowledge_files_for_job("ws", [small]) == []


def test_a_large_attachment_is_searched(library) -> None:
    import main

    big = _record("a", "uzun metin paragrafı. " * 5000)
    ki.index_upload(big)
    assert main._knowledge_files_for_job("ws", [big]) == [("a", "a.md")]


def test_library_files_from_other_chats_are_searched(library) -> None:
    import main

    ki.index_upload(_record("old", "eski sözleşme metni"))
    ki.index_upload(_record("skipped", "", kind="zip"))
    db.add_workspace_documents(
        "ws",
        [{"file_id": "old", "filename": "old.md"}, {"file_id": "skipped", "filename": "a.zip"}],
        time.time(),
    )
    small = _record("new", "kısa not")
    files = dict(main._knowledge_files_for_job("ws", [small]))
    # Skipped files have nothing to search; a new, not yet indexed file is kept.
    assert files == {"old": "old.md", "new": "new.md"}


# -- workforce ------------------------------------------------------------------------


def test_the_role_is_scheduled_first_and_never_answers_alone() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.DOCUMENTS_READ}),
        fast_path_role=None,
        wants_artifact=False,
    )
    assert roles == [workforce.ROLE_KNOWLEDGE, workforce.ROLE_GENERAL]
    roles = workforce._select_roles(
        job_capabilities=frozenset(),
        fast_path_role=None,
        wants_artifact=False,
        reads_documents=True,
        reads_jira=True,
    )
    assert roles == [workforce.ROLE_KNOWLEDGE, workforce.ROLE_JIRA, workforce.ROLE_GENERAL]


def test_the_document_writer_answers_when_a_file_is_wanted() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    roles = workforce._select_roles(
        job_capabilities=frozenset({capabilities.ARTIFACTS}),
        fast_path_role=None,
        wants_artifact=True,
        reads_documents=True,
    )
    assert roles == [workforce.ROLE_KNOWLEDGE, workforce.ROLE_DOCUMENT]


def test_the_role_has_a_worker_id_and_a_roster_card(monkeypatch) -> None:
    pytest.importorskip("langgraph")
    import main
    from execution.langgraph_runtime import workforce

    assert workforce.WORKER_IDS[workforce.ROLE_KNOWLEDGE] == "lg-knowledge"
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "true")
    assert "Knowledge Agent" in [w.name for w in main._worker_pool().workers]
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    assert "Knowledge Agent" not in [w.name for w in main._worker_pool().workers]


def test_cited_labels_become_the_source_list() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    sources = {
        "K1": "[K1] kira.pdf, s. 7 — Fesih",
        "K2": "[K2] kira.pdf, s. 3 — Ödeme",
        "K3": "[K3] notlar.md",
    }
    out = workforce._append_document_sources("Süre 60 gündür [K1]. Bedel [K2].", sources)
    assert out.endswith(
        "Belge kaynakları:\n- [K1] kira.pdf, s. 7 — Fesih\n- [K2] kira.pdf, s. 3 — Ödeme"
    )
    # Nothing cited: every excerpt is listed rather than losing provenance.
    assert "[K3] notlar.md" in workforce._append_document_sources("Dosya hazır.", sources)
    assert workforce._append_document_sources("cevap", None) == "cevap"


def test_the_knowledge_agent_gathers_and_the_assistant_answers(
    library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One LangGraph run end to end with a scripted model: the agent's notes
    reach the Assistant, and the final answer carries the source list."""
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce
    import knowledge_controller

    ki.index_upload(
        _record(
            "kira",
            "# Fesih\n\nKiracı altmış gün önceden yazılı bildirimde bulunur.\n\n"
            "# Depozito\n\nDepozito iki aylık kira bedelidir.",
        )
    )
    monkeypatch.setattr(knowledge_controller, "_default_score", lambda: None)

    seen: dict[str, str] = {}

    async def fake_complete_chat(messages, **kwargs):
        stage = kwargs.get("stage", "")
        schema = kwargs.get("response_format")
        if schema is knowledge_controller.KnowledgePlan:
            return knowledge_controller.KnowledgePlan(queries=["fesih bildirim"])
        if stage == "knowledge_answer":
            return "Fesih için altmış gün önceden bildirim gerekir [K1]."
        seen["assistant_user"] = messages[-1]["content"]
        return "Sözleşmeye göre altmış gün önceden bildirmeniz gerekir [K1]."

    monkeypatch.setattr(workforce, "complete_chat", fake_complete_chat)

    class Callback:
        # No loop and no queue: _emit drops the UI events.
        _loop = None
        _queue = None

    result = workforce.run_workforce_in_thread(
        Callback(),
        workforce.UsageTracker(),
        None,
        "evden çıkarsam kaç gün önce haber vermeliyim",
        "job-1",
        lambda handle: None,
        job_capabilities=frozenset({capabilities.DOCUMENTS_READ}),
        original_request="evden çıkarsam kaç gün önce haber vermeliyim",
        reads_documents=True,
        knowledge_files=[("kira", "kira.md")],
    )
    text = str(getattr(result, "result", result))
    assert "BELGELERDEN ÖZET" in seen["assistant_user"]
    assert "[K1] kira.md — Fesih" in seen["assistant_user"]
    assert "altmış gün" in text
    # The neighbouring section is merged into the same excerpt.
    assert "\n\nBelge kaynakları:\n- [K1] kira.md — Fesih" in text


def test_no_files_means_no_knowledge_agent() -> None:
    pytest.importorskip("langgraph")
    from execution.langgraph_runtime import workforce

    source = inspect.getsource(workforce._run_workforce_async)
    assert "reads_documents=bool(reads_documents) and bool(knowledge_files)" in source
