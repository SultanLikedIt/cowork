"""Fixed Workforce worker definitions and runtime tool contracts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import camel_service
import capabilities


def _build(root: str, **kwargs):
    captured = {}
    workforce_children: list = []

    class FakeWorkforce:
        def __init__(self, *args, **kwargs):
            self.coordinator_agent = kwargs["coordinator_agent"]
            self.task_agent = kwargs["task_agent"]
            self._children = workforce_children

        def add_single_agent_worker(self, description, worker=None, **kwargs):
            role = camel_service.WORKER_DISPLAY_NAMES.get(description, description)
            captured[role] = (description, worker)
            self._children.append(
                SimpleNamespace(
                    description=description,
                    worker=worker,
                )
            )
            return self

    original = camel_service.Workforce
    camel_service.Workforce = FakeWorkforce
    params = {
        "workspace_root": root,
        "job_capabilities": frozenset(capabilities.ALL_CAPABILITIES),
        "request_text": "Use the supplied material to produce the requested output.",
    }
    params.update(kwargs)
    try:
        camel_service.build_workforce(
            callback=object(),
            tracker=camel_service.UsageTracker(),
            **params,
        )
    finally:
        camel_service.Workforce = original
    captured["_children"] = workforce_children
    return captured


def _names(captured, role: str) -> set[str]:
    return set(captured[role][1].tool_dict)


def test_fixed_workers_receive_role_specific_tools() -> None:
    with tempfile.TemporaryDirectory() as root:
        captured = _build(root)
    assert set(captured) >= {
        "Research Agent",
        "Document Agent",
        "Coding Agent",
        "_children",
    }

    research = _names(captured, "Research Agent")
    assert "web_search" not in research
    assert "web_read" not in research
    assert "transform_table" not in research
    assert "create_xlsx_artifact" not in research
    assert "create_docx_artifact" not in research
    assert "execute_code" not in research
    research_child = next(
        child
        for child in captured["_children"]
        if child.worker is captured["Research Agent"][1]
    )
    assert getattr(research_child, "_web_controller_cfg", None)
    assert getattr(captured["Research Agent"][1], "_web_controller_cfg", None) is None

    document = _names(captured, "Document Agent")
    assert {
        "list_files",
        "inspect_file",
        "read_text_file",
        "inspect_table",
        "create_xlsx_artifact",
        "create_docx_artifact",
        "create_pptx_artifact",
        "create_pdf_artifact",
        "create_csv_artifact",
        "create_json_artifact",
        "create_text_artifact",
    } <= document
    assert "transform_table" not in document
    assert "web_search" not in document and "web_read" not in document
    assert "execute_code" not in document
    assert "create_code_bundle" not in document
    assert "create_web_bundle" not in document

    coding = _names(captured, "Coding Agent")
    assert "execute_code" in coding
    assert "execute_command" in coding
    assert "create_xlsx_artifact" not in coding
    assert "web_search" not in coding


def test_document_agent_uses_upstream_data_path_to_create_xlsx() -> None:
    with tempfile.TemporaryDirectory() as root:
        source = Path(root) / "artifacts" / "extracted" / "verified.json"
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps([{"city": "Ankara", "time": "05:42"}]),
            encoding="utf-8",
        )
        captured = _build(root)
        tool = captured["Document Agent"][1].tool_dict[
            "create_xlsx_artifact"
        ].func
        result = tool(filename="prayer-times.xlsx", from_path="artifacts/extracted/verified.json")
        assert result["created"] is True
        assert (Path(root) / result["path"]).is_file()


def test_worker_descriptions_advertise_distinct_responsibilities() -> None:
    roles = {
        role: (description, prompt)
        for description, role, prompt in camel_service.WORKER_ROLES
    }
    assert "reusable evidence/data_path/provenance" in roles["Research Agent"][0]
    assert camel_service.ARTIFACT_EXCLUSIONS in roles["Research Agent"][0]

    document_description, document_prompt = roles["Document Agent"]
    assert "Docling-backed workspace references" in document_description
    assert "creates normal downloadable artifacts" in document_description
    assert camel_service.ARTIFACT_EXCLUSIONS not in document_description
    assert "do not repeat research" in document_prompt
    assert "product UI lists downloads" in document_prompt
    assert "never null" in document_prompt
    assert ".docling/content.md" in camel_service.WORKSPACE_TOOLS_CAVEAT
    assert ".extracted.txt companion" not in camel_service.WORKSPACE_TOOLS_CAVEAT

    coding_description, coding_prompt = roles["Coding Agent"]
    assert "coding and software-engineering" in coding_description
    assert "execute_code" in coding_prompt
    assert camel_service.ARTIFACT_EXCLUSIONS not in coding_description


def test_coordinator_sees_actual_document_and_research_capabilities() -> None:
    with tempfile.TemporaryDirectory() as root:
        captured = _build(root)
    original = camel_service._ORIGINAL_GET_NODE_INFO
    camel_service._ORIGINAL_GET_NODE_INFO = (
        lambda self, node: node.description
    )
    try:
        document = camel_service._patched_get_node_info(
            None,
            SimpleNamespace(
                description="Document Agent",
                worker=captured["Document Agent"][1],
            ),
        )
        research = camel_service._patched_get_node_info(
            None,
            next(
                child
                for child in captured["_children"]
                if child.worker is captured["Research Agent"][1]
            ),
        )
    finally:
        camel_service._ORIGINAL_GET_NODE_INFO = original
    assert "CAN create normal downloadable" in document
    assert "Docling-backed workspace" in document
    assert "CANNOT search the web" in document
    assert "CANNOT execute code" in document
    assert "CAN acquire web information" in research
    assert "CANNOT create files" in research


def test_only_the_document_worker_holds_the_artifact_capability() -> None:
    """Assignment matching (test_workforce_worker_matching.py) relies on
    this: artifact tools live on exactly one fixed worker."""
    with tempfile.TemporaryDirectory() as root:
        captured = _build(root)
    holders = {
        role
        for role in captured
        if role != "_children"
        if capabilities.ARTIFACTS
        in camel_service.capabilities_from_tool_names(_names(captured, role))
    }
    assert holders == {"Document Agent"}


def test_xlsx_intent_binds_only_the_xlsx_create_tool() -> None:
    """A known Excel deliverable must not expose json/md creators.

    Live failure: Document called create_json_artifact (ungrounded), then
    create_text_artifact, then create_xlsx_artifact(from_path='null').
    """
    with tempfile.TemporaryDirectory() as root:
        captured = _build(root, wants_artifact=True, artifact_type="xlsx")
    names = _names(captured, "Document Agent")
    assert "create_xlsx_artifact" in names
    assert "transform_table" not in names
    assert "create_json_artifact" not in names
    assert "create_text_artifact" not in names
    assert "create_csv_artifact" not in names
    assert "create_docx_artifact" not in names
    assert "list_artifacts" not in names
    assert "create_xlsx_artifact" not in _names(captured, "Research Agent")


def test_excel_request_text_binds_xlsx_when_intent_says_xlsx() -> None:
    """Explicit xlsx intent must bind only the xlsx creator."""
    with tempfile.TemporaryDirectory() as root:
        captured = _build(
            root,
            wants_artifact=True,
            artifact_type="xlsx",
            request_text="süper lig puan durumunu excel olarak ver",
        )
    names = _names(captured, "Document Agent")
    assert "create_xlsx_artifact" in names
    assert "create_text_artifact" not in names
    assert "create_json_artifact" not in names
    assert "transform_table" not in names


def test_analyze_excel_does_not_force_xlsx_only_bind() -> None:
    with tempfile.TemporaryDirectory() as root:
        captured = _build(
            root,
            wants_artifact=False,
            artifact_type="none",
            request_text="Excel dosyasını analiz et",
        )
    names = _names(captured, "Document Agent")
    assert "create_xlsx_artifact" in names
    assert "create_text_artifact" in names
