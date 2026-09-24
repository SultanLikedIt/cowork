"""Regression coverage for explicit Single and Workforce execution modes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import capabilities
import main
from artifacts import ArtifactError


DIYANET = "Diyanet'ten Ankara'nın aylık namaz vakitlerini bulup Excel yap"
YAYLA = "yayla gıdanın freeze dried ürünlerini fiyatlarıyla bul"
WRITING = "Write a short polite email confirming tomorrow's meeting."


import capability_routing


def _decision(mode: str, topic: str):
    if mode == "single":
        return main._routing_for_ui_mode(
            mode,
            topic,
            has_workspace_files=False,
            workspace_file_kinds=[],
        )
    base = capabilities.expand(
        capabilities.workforce_structural_requirements(topic, False, [])
    )
    wants = (
        capabilities.ARTIFACTS in capabilities.detect_artifact_request(topic)
        or "excel" in topic.lower()
    )
    needs_web = "araştır" in topic.lower() or "bul" in topic.lower() or wants
    effective = capabilities.apply_artifact_intent(
        base,
        wants_artifact=wants,
        needs_web_research=needs_web,
        text=topic,
    )
    return capability_routing.merge_decision(
        "workforce",
        effective,
        frozenset(),
        profile=capability_routing.infer_profile(topic, False, effective),
        reason="test",
        outcome=capability_routing.ROUTING_SKIPPED,
    )


def _bound(root: str, topic: str, *, enforce: bool = False):
    decision = _decision("multi", topic)
    return capabilities.resolve_tools(
        decision.capabilities,
        capabilities.ToolContext(
            workspace_root=root,
            grounding_text=topic,
            enforce_artifact_grounding=enforce,
        ),
    )


def _tool(bound, name: str):
    return next(
        tool for tool in bound.tools if capabilities._tool_name(tool) == name
    )


def test_ui_mode_is_the_authoritative_execution_strategy() -> None:
    for topic in (DIYANET, YAYLA, WRITING, "Explain photosynthesis"):
        assert _decision("single", topic).strategy == "single"
        assert _decision("multi", topic).strategy == "workforce"


def test_workforce_pool_supports_diyanet_artifact_handoff() -> None:
    decision = _decision("multi", DIYANET)
    assert capabilities.WEB_SEARCH in decision.capabilities
    assert capabilities.WEB_FETCH in decision.capabilities
    assert capabilities.ARTIFACTS in decision.capabilities


def test_workforce_pool_supports_yayla_research_worker() -> None:
    decision = _decision("multi", YAYLA)
    assert capabilities.WEB_SEARCH in decision.capabilities
    assert capabilities.ARTIFACTS not in decision.capabilities


def test_ungrounded_external_dataset_is_blocked_in_workforce_pool() -> None:
    fabricated = json.dumps(
        [
            {"date": "2026-08-16", "city": "Ankara", "imsak": "04:37"},
            {"date": "2026-08-17", "city": "Ankara", "imsak": "04:38"},
        ]
    )
    with tempfile.TemporaryDirectory() as root:
        create_xlsx = _tool(_bound(root, DIYANET, enforce=True), "create_xlsx_artifact")
        with pytest.raises(ArtifactError, match="ungrounded inline values"):
            create_xlsx(filename="ankara.xlsx", data=fabricated)


def test_user_inline_and_local_data_artifacts_still_succeed() -> None:
    user_data = json.dumps(
        [{"name": "Alice", "score": 10}, {"name": "Bob", "score": 12}]
    )
    topic = "Create an Excel file from Alice score 10 and Bob score 12."
    with tempfile.TemporaryDirectory() as root:
        create_xlsx = _tool(_bound(root, topic, enforce=True), "create_xlsx_artifact")
        assert create_xlsx(filename="scores.xlsx", data=user_data)["created"] is True

        source = Path(root) / "sources" / "local.json"
        source.parent.mkdir(parents=True)
        source.write_text(user_data, encoding="utf-8")
        assert create_xlsx(
            filename="local-scores.xlsx",
            from_path="sources/local.json",
        )["created"] is True
