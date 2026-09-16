"""Unit tests for the LOCAL_COMPUTER capability (backend/local_computer.py)
and its request classifier (execution/langgraph_runtime/workforce.py's
_request_wants_local_open). No real bridge process or model call needed --
see docs/local-computer.md for the manual end-to-end verification steps
that DO need a real bridge running.
"""

from __future__ import annotations

import base64
import inspect
import tempfile
import time
import uuid
from pathlib import Path

import pytest

import capabilities
import db
import local_computer
from execution.langgraph_runtime.workforce import (
    _build_graph,
    _open_local_file_step,
    _request_wants_local_open,
)

db.run_migrations()


def test_build_graph_declares_every_flag_its_worker_loop_reads():
    """Regression: run_next read ``opens_on_user_computer`` to choose the
    local-open fast path, but ``_build_graph`` never declared it as a
    parameter, so the name resolved to nothing at runtime:

        NameError: name 'opens_on_user_computer' is not defined

    ``and`` short-circuits, so only jobs whose role was ROLE_GENERAL ever
    evaluated it. Research jobs passed and looked healthy, while every
    "summarize this attachment" job died in 0.02s with the UI showing
    only "This message could not be completed."
    """
    declared = inspect.signature(_build_graph).parameters
    for flag in ("reuses_uploaded_document", "opens_on_user_computer"):
        assert flag in declared, f"_build_graph must declare {flag}"


def _make_job(job_id: str, conversation_id: str, created_at: float) -> dict:
    return {
        "job_id": job_id,
        "workspace_id": "ws-" + conversation_id,
        "conversation_id": conversation_id,
        "topic": "test",
        "status": "done",
        "mode": "multi",
        "created_at": created_at,
    }


@pytest.mark.parametrize(
    "text",
    [
        "bunu bilgisayarımda aç",
        "bilgisayarımda aç bunu",
        "aç bunu bilgisayarımda",
        "dosyayı bilgisayarımda açar mısın",
        "open it on my computer",
        "open that file locally",
        "lokalde aç şunu",
        "yerelde ac",
        "macimde ac",
        "PDF olustu, bilgisayarimda ac",
        # "create AND open" in one message must still be detected as an
        # open-ask -- _run_workforce_async decides separately (via
        # wants_artifact) whether the file also needs creating first.
        "Efes antik kenti hakkında kısa bir PDF hazırla ve bilgisayarımda aç",
    ],
)
def test_request_wants_local_open_matches_expected_phrasing(text):
    assert _request_wants_local_open(text)


@pytest.mark.parametrize(
    "text",
    [
        "bir powerpoint yap",
        "kapıyı aç",
        "hesabımı aç",
        "yeni bir workspace ac",
        "bu konuda daha fazla arastir",
    ],
)
def test_request_wants_local_open_ignores_unrelated_asks(text):
    """"aç"/"open" alone is far too common to trigger on -- must require a
    clear on-my-machine signal alongside it."""
    assert not _request_wants_local_open(text)


def test_bridge_reachable_is_false_for_a_dead_url():
    assert local_computer.bridge_reachable("http://127.0.0.1:1") is False


def test_open_local_file_reports_bridge_unreachable_gracefully():
    tools = local_computer.LocalComputerTools(
        workspace_root=None, base_url="http://127.0.0.1:1"
    )
    result = tools.open_local_file("whatever.docx")
    assert result["ok"] is False
    assert result["error"] == "bridge_unreachable"


def test_open_local_file_without_a_filename_needs_a_workspace():
    tools = local_computer.LocalComputerTools(
        workspace_root=None, base_url="http://127.0.0.1:1"
    )
    result = tools.open_local_file()
    assert result == {
        "ok": False,
        "error": "no_filename",
        "detail": "No filename given and no artifact was found for this job.",
    }


def test_open_local_file_defaults_to_the_most_recently_created_artifact(monkeypatch):
    """Never guesses a filename -- reuses artifacts.scan_artifacts (the same
    disk-scan the /jobs API uses), picking the newest by created_at. Also
    sends the file's REAL bytes (via /save-and-open), not just its name --
    see the save-and-open tests below for why that matters."""
    with tempfile.TemporaryDirectory() as root:
        artifacts_dir = Path(root) / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "old.txt").write_text("old")
        (artifacts_dir / "new.txt").write_text("new")

        sent = {}

        class _FakeResponse:
            ok = True
            status_code = 200

            def json(self):
                return {"ok": True, "opened": sent["filename"]}

        def _fake_post(url, json, timeout):  # noqa: A002
            sent["url"] = url
            sent["filename"] = json["filename"]
            return _FakeResponse()

        monkeypatch.setattr(local_computer.requests, "post", _fake_post)
        # Force a known recency order regardless of filesystem mtime
        # resolution/clock granularity -- _latest_artifact_entry is the one
        # helper shared by both the filename- and disk-path-resolving
        # lookups, so patching it here covers both.
        monkeypatch.setattr(
            local_computer,
            "_latest_artifact_entry",
            lambda workspace_root: {
                "filename": "new.txt",
                "path": "artifacts/new.txt",
            },
        )

        tools = local_computer.LocalComputerTools(
            workspace_root=root, base_url="http://127.0.0.1:1"
        )
        result = tools.open_local_file()

    assert result == {"ok": True, "opened": "new.txt"}
    assert sent["filename"] == "new.txt"
    assert sent["url"].endswith("/save-and-open")


def test_open_local_file_never_sends_a_full_path_only_the_bare_filename(monkeypatch):
    """The container's workspace path is meaningless to the user's OS --
    only the filename may ever reach the bridge (see local_computer.py's
    open_local_file docstring)."""
    sent = {}

    class _FakeResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"ok": True, "opened": sent["filename"]}

    def _fake_post(url, json, timeout):  # noqa: A002
        sent["filename"] = json["filename"]
        return _FakeResponse()

    monkeypatch.setattr(local_computer.requests, "post", _fake_post)
    tools = local_computer.LocalComputerTools(
        workspace_root=None, base_url="http://127.0.0.1:1"
    )
    tools.open_local_file("/some/container/path/rapor.docx")
    assert sent["filename"] == "rapor.docx"


def test_open_local_file_falls_back_to_a_prior_conversation_job_when_current_is_empty(
    monkeypatch,
):
    """A follow-up like "bunu bilgisayarımda aç" runs as a brand-new job with
    its OWN empty workspace -- the file it means was written by an earlier
    job in the same conversation. See db.workspace_roots_for_conversation.
    Also verifies the REAL bytes of that prior job's file are what gets
    sent (via /save-and-open), not just its name."""
    with tempfile.TemporaryDirectory() as empty_root, tempfile.TemporaryDirectory() as prior_root:
        (Path(prior_root) / "artifacts").mkdir()
        (Path(prior_root) / "artifacts" / "rapor.pdf").write_text("pdf content")

        sent = {}

        class _FakeResponse:
            ok = True
            status_code = 200

            def json(self):
                return {"ok": True, "opened": sent["filename"]}

        def _fake_post(url, json, timeout):  # noqa: A002
            sent["url"] = url
            sent["filename"] = json["filename"]
            sent["content_base64"] = json.get("content_base64")
            return _FakeResponse()

        monkeypatch.setattr(local_computer.requests, "post", _fake_post)
        tools = local_computer.LocalComputerTools(
            workspace_root=empty_root,
            base_url="http://127.0.0.1:1",
            fallback_workspace_roots=[prior_root],
        )
        result = tools.open_local_file()

    assert result == {"ok": True, "opened": "rapor.pdf"}
    assert sent["filename"] == "rapor.pdf"
    assert sent["url"].endswith("/save-and-open")
    assert base64.b64decode(sent["content_base64"]) == b"pdf content"


def test_open_local_file_falls_back_to_by_name_when_bridge_predates_save_and_open(
    monkeypatch,
):
    """An older, not-yet-restarted bridge process (before /save-and-open
    existed) returns 404 for it -- that specific signal must fall back to
    the by-name /open-file call rather than being reported as a failure."""
    with tempfile.TemporaryDirectory() as root:
        artifacts_dir = Path(root) / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "rapor.pdf").write_text("pdf content")

        calls = []

        class _FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self.ok = status_code < 400
                self._payload = payload

            def json(self):
                return self._payload

        def _fake_post(url, json, timeout):  # noqa: A002
            calls.append(url)
            if url.endswith("/save-and-open"):
                return _FakeResponse(404, {"ok": False, "error": "not_found"})
            return _FakeResponse(200, {"ok": True, "opened": json["filename"]})

        monkeypatch.setattr(local_computer.requests, "post", _fake_post)
        tools = local_computer.LocalComputerTools(
            workspace_root=root, base_url="http://127.0.0.1:1"
        )
        result = tools.open_local_file()

    assert calls == [
        "http://127.0.0.1:1/save-and-open",
        "http://127.0.0.1:1/open-file",
    ]
    assert result == {"ok": True, "opened": "rapor.pdf"}


def test_open_local_file_falls_back_to_by_name_when_file_exceeds_size_cap(
    monkeypatch,
):
    """A file too large to comfortably inline as base64 skips /save-and-open
    entirely and goes straight to the by-name call -- best effort rather
    than a hard failure."""
    with tempfile.TemporaryDirectory() as root:
        artifacts_dir = Path(root) / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "huge.pdf").write_text("x")

        calls = []

        class _FakeResponse:
            ok = True
            status_code = 200

            def json(self):
                return {"ok": True, "opened": "huge.pdf"}

        def _fake_post(url, json, timeout):  # noqa: A002
            calls.append(url)
            return _FakeResponse()

        monkeypatch.setattr(local_computer.requests, "post", _fake_post)
        monkeypatch.setattr(local_computer, "_MAX_SAVE_AND_OPEN_BYTES", 0)
        tools = local_computer.LocalComputerTools(
            workspace_root=root, base_url="http://127.0.0.1:1"
        )
        result = tools.open_local_file()

    assert calls == ["http://127.0.0.1:1/open-file"]
    assert result == {"ok": True, "opened": "huge.pdf"}


def test_capability_reports_missing_when_disabled(monkeypatch):
    monkeypatch.setattr(local_computer, "LOCAL_COMPUTER_ENABLED", False)
    context = capabilities.ToolContext(workspace_root=None)
    bound = capabilities.resolve_tools({capabilities.LOCAL_COMPUTER}, context)
    assert bound.tools == ()
    assert bound.missing == (
        (
            capabilities.LOCAL_COMPUTER,
            "local computer access is disabled on this deployment",
        ),
    )


def test_capability_reports_missing_when_bridge_unreachable(monkeypatch):
    monkeypatch.setattr(local_computer, "LOCAL_COMPUTER_ENABLED", True)
    monkeypatch.setattr(local_computer, "bridge_reachable", lambda: False)
    context = capabilities.ToolContext(workspace_root=None)
    bound = capabilities.resolve_tools({capabilities.LOCAL_COMPUTER}, context)
    assert bound.tools == ()
    assert bound.missing[0][0] == capabilities.LOCAL_COMPUTER
    assert "bridge" in bound.missing[0][1]


def test_capability_binds_the_tool_when_enabled_and_reachable(monkeypatch):
    monkeypatch.setattr(local_computer, "LOCAL_COMPUTER_ENABLED", True)
    monkeypatch.setattr(local_computer, "bridge_reachable", lambda: True)
    context = capabilities.ToolContext(workspace_root=None)
    bound = capabilities.resolve_tools({capabilities.LOCAL_COMPUTER}, context)
    assert bound.missing == ()
    assert "open_local_file" in bound.names


def test_workspace_roots_for_conversation_orders_newest_first_and_skips_missing_dirs():
    conversation_id = f"conv-{uuid.uuid4()}"
    workspace_id = f"ws-{conversation_id}"
    now = time.time()
    db.create_workspace(workspace_id, None, now)
    db.create_conversation(conversation_id, workspace_id, None, now)

    import db as db_module

    with tempfile.TemporaryDirectory() as data_dir:
        original_data_dir = db_module.DATA_DIR
        db_module.DATA_DIR = Path(data_dir)
        try:
            workspaces_dir = Path(data_dir) / "workspaces"
            workspaces_dir.mkdir()
            older_job_id = f"job-older-{uuid.uuid4()}"
            newer_job_id = f"job-newer-{uuid.uuid4()}"
            gone_job_id = f"job-gone-{uuid.uuid4()}"  # never got a real directory
            for job_id, created_at in (
                (older_job_id, now - 20),
                (newer_job_id, now - 10),
                (gone_job_id, now - 5),
            ):
                db.save_job(_make_job(job_id, conversation_id, created_at))
            (workspaces_dir / older_job_id).mkdir()
            (workspaces_dir / newer_job_id).mkdir()
            # gone_job_id deliberately has no directory on disk.

            roots = db.workspace_roots_for_conversation(conversation_id)
        finally:
            db_module.DATA_DIR = original_data_dir

    assert roots == [
        str(workspaces_dir / newer_job_id),
        str(workspaces_dir / older_job_id),
    ]


def test_open_local_file_step_opens_a_prior_turns_artifact_in_the_same_conversation(
    monkeypatch,
):
    """End-to-end: job A (older) wrote an artifact; job B (current, empty
    workspace) is asked to open it with no filename given -- the same shape
    as a real "create it" then "bunu bilgisayarımda aç" pair of turns."""
    conversation_id = f"conv-{uuid.uuid4()}"
    workspace_id = f"ws-{conversation_id}"
    now = time.time()
    db.create_workspace(workspace_id, None, now)
    db.create_conversation(conversation_id, workspace_id, None, now)

    import db as db_module

    with tempfile.TemporaryDirectory() as data_dir:
        original_data_dir = db_module.DATA_DIR
        db_module.DATA_DIR = Path(data_dir)
        try:
            workspaces_dir = Path(data_dir) / "workspaces"
            workspaces_dir.mkdir()
            prior_job_id = f"job-prior-{uuid.uuid4()}"
            current_job_id = f"job-current-{uuid.uuid4()}"
            db.save_job(_make_job(prior_job_id, conversation_id, now - 30))
            db.save_job(_make_job(current_job_id, conversation_id, now))

            prior_root = workspaces_dir / prior_job_id
            (prior_root / "artifacts").mkdir(parents=True)
            (prior_root / "artifacts" / "rapor.pdf").write_text("pdf content")
            current_root = workspaces_dir / current_job_id
            current_root.mkdir()

            monkeypatch.setattr(local_computer, "LOCAL_COMPUTER_ENABLED", True)
            monkeypatch.setattr(local_computer, "bridge_reachable", lambda: True)
            opened = {}

            class _FakeResponse:
                ok = True
                status_code = 200

                def json(self):
                    return {"ok": True, "opened": opened["filename"]}

            def _fake_post(url, json, timeout):  # noqa: A002
                opened["url"] = url
                opened["filename"] = json["filename"]
                opened["content_base64"] = json.get("content_base64")
                return _FakeResponse()

            monkeypatch.setattr(local_computer.requests, "post", _fake_post)

            message = _open_local_file_step(
                workspace_root=str(current_root), conversation_id=conversation_id
            )
        finally:
            db_module.DATA_DIR = original_data_dir

    assert "rapor.pdf" in message
    assert opened["filename"] == "rapor.pdf"
    # The prior job's REAL bytes were sent -- not just its bare filename,
    # which is what let a stale, unrelated same-named file get opened
    # instead of the one this conversation actually just produced (real
    # bug: a Kapadokya Word report follow-up "aç" opened a week-old,
    # unrelated "rapor.docx" that merely happened to share the name).
    assert opened["url"].endswith("/save-and-open")
    assert base64.b64decode(opened["content_base64"]) == b"pdf content"


def test_football_score_request_does_not_trigger_a_local_open():
    """Live regression: "no_filename" on a request with no file in it.

    The detector had no word boundaries, so "m(ac)taki (mac) skorunu"
    satisfied verb-then-machine inside two ordinary Turkish words -- and
    Apple's Mac is spelled like "mac", Turkish for match typed without its
    diacritic. Asking for a scoreline tried to open a file nobody asked for.
    """
    from execution.langgraph_runtime.workforce import _request_wants_local_open

    for text in (
        "galatasaray goztepe en son oynanan mactaki mac skorunu ve ilk 11 "
        "leri getirir misin",
        "dun oynanan mac skoru neydi",
        "mac sonucunu acikla",
    ):
        assert not _request_wants_local_open(text), text

    for text in (
        "bunu bilgisayarimda ac",
        "kutup ayilari pdf ver bilgisayarimda da ac",
        "open it on my computer",
        "mac'imde ac",
        "masaustunde baslat",
    ):
        assert _request_wants_local_open(text), text
