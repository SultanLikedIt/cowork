"""Tests for the artifact API surface in main.py.

Covers the three pieces of wiring that only exist at the HTTP layer, and that
the unit suites cannot reach:

  1. A job whose DELIVERABLE is a file gets a workspace even with no uploads.
     Before this, a workspace existed only to hold attachments, so a
     "give me the Excel file" request with nothing attached had nowhere to
     write it -- the artifact capability would bind to nothing and the run
     would be back to describing a file it could not create.
  2. A job's artifacts are listed from DISK, so a fabricated claim produces
     no listing.
  3. The download endpoint is confined to the job's artifacts directory --
     the one place a user-supplied string becomes a filesystem read.

No pytest suite exists in this repo (see test_upload.py) -- run directly:
    python test_artifact_api.py
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import uuid

# Isolated DB/log directory, set BEFORE importing main/db so their
# module-level DATA_DIR reads pick it up.
_tmp_dir = tempfile.mkdtemp(prefix="artifact_api_test_")
os.environ["DATA_DIR"] = _tmp_dir

from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from fastapi import HTTPException  # noqa: E402

import camel_service  # noqa: E402
import capabilities  # noqa: E402
import capability_routing  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
import artifact_intent  # noqa: E402
from artifacts import ArtifactTools  # noqa: E402
from camel.tasks.task import Task, TaskState  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


async def _fake_intent(topic, tracker, *, persisted=None, **_kwargs):
    return artifact_intent.fallback_from_regex(topic)


async def _fake_run_single_agent(
    content,
    tracker,
    task_id,
    queue,
    skills=None,
    workspace_root=None,
    tool_call_recorder=None,
    job_capabilities=None,
    profile=None,
    request_topic=None,
    **kwargs,
):
    """Stands in for the chatbot executor, and records what it was handed."""
    _fake_run_single_agent.seen = {
        "workspace_root": workspace_root,
        "job_capabilities": job_capabilities,
        "profile": profile,
    }
    return Task(content=content, id=task_id, state=TaskState.DONE, result="done")


def _fake_workforce(
    callback,
    tracker,
    recorder,
    content,
    task_id,
    on_ready,
    workspace_root=None,
    *args,
    **kwargs,
):
    _fake_workforce.seen = {"workspace_root": workspace_root}
    if workspace_root:
        tools = ArtifactTools(Path(workspace_root))
        tools.create_xlsx_artifact("results.xlsx", json.dumps([{"a": 1, "b": 2}]))
        tools.create_json_artifact("results.json", json.dumps({"ok": True}))
    return Task(content=content, id=task_id, state=TaskState.DONE, result="done")


async def _fake_synth(task, topic, tracker, **kwargs):
    return task.result or "", False


async def _submit(topic: str, declared: list, mode: str = "single") -> str:
    """Submit through the HTTP layer with executors stubbed."""
    original_mode_routing = main._routing_for_ui_mode

    def mode_routing(ui_mode, request_topic, **kwargs):
        if declared:
            return capability_routing.merge_decision(
                ui_mode if ui_mode == "single" else "workforce",
                declared=declared,
                structural=frozenset(),
                raw_declared=declared,
                reason="test unsupported capability",
            )
        return original_mode_routing(ui_mode, request_topic, **kwargs)

    with patch.object(
        camel_service, "run_single_agent", _fake_run_single_agent
    ), patch.object(
        camel_service, "run_workforce_in_thread", _fake_workforce
    ), patch.object(
        camel_service, "synthesize_workforce_result", _fake_synth
    ), patch.object(
        main.artifact_intent, "classify_artifact_intent", _fake_intent
    ), patch.object(main, "_routing_for_ui_mode", mode_routing):
        response = await main.run_pipeline(
            main.RunPipelineRequest(topic=topic, mode=mode)
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        return response.job_id


# ---------------------------------------------------------------------------


async def _run() -> None:
    db.run_migrations()

    # --- 1. Single excel is a chatbot; Workforce gets the workspace ------
    single_id = await _submit("Give me the top 20 results as an Excel file", [])
    check(
        "Single excel does not provision a workspace",
        not main._JOBS[single_id].get("workspace_root"),
        str(main._JOBS[single_id].get("workspace_root")),
    )
    check(
        "Single excel does not bind artifacts",
        "artifacts" not in (main._JOBS[single_id]["usage"].get("capabilities") or []),
        str(main._JOBS[single_id]["usage"].get("capabilities")),
    )

    job_id = await _submit(
        "Give me the top 20 results as an Excel file", [], mode="multi"
    )
    job = main._JOBS[job_id]
    seen = _fake_workforce.seen

    check(
        "a no-upload job that must produce a file gets a workspace",
        bool(job.get("workspace_root")) and Path(job["workspace_root"]).is_dir(),
        str(job.get("workspace_root")),
    )
    check(
        "the artifact capability reached the Workforce job pool",
        "artifacts" in (job["usage"].get("capabilities") or []),
        str(job["usage"].get("capabilities")),
    )
    check(
        "the workspace directory is job-isolated",
        Path(job["workspace_root"]).name == job_id,
        str(job.get("workspace_root")),
    )
    if sys.platform == "win32":
        # mkdir(mode=0o700) has no effect on Windows -- NTFS permissions
        # are ACL-based, not POSIX mode bits, and Python's os.mkdir mode
        # argument is documented as ignored there. Confinement on Windows
        # comes from the path itself being job-scoped (already checked
        # above), not from permission bits, so this assertion is not
        # meaningful on this platform -- skip it rather than assert
        # something the OS was never asked to guarantee.
        check("the workspace is created 0o700, like an upload workspace (skipped on Windows: mkdir mode is a no-op)", True)
    else:
        check(
            "the workspace is created 0o700, like an upload workspace",
            (Path(job["workspace_root"]).stat().st_mode & 0o777) == 0o700,
            oct(Path(job["workspace_root"]).stat().st_mode & 0o777),
        )
    check(
        "capabilities are persisted on the job's telemetry",
        "artifacts" in (job["usage"].get("capabilities") or []),
        str(job["usage"].get("capabilities")),
    )

    # --- 2. Artifacts are listed from disk ---------------------------------
    snapshot = main._job_snapshot(job_id)
    check(
        "the terminal snapshot is not served from a stale cache",
        len(snapshot.artifacts) == 2,
        str(len(snapshot.artifacts)),
    )
    paths = {artifact.path for artifact in snapshot.artifacts}
    check(
        "the job snapshot lists the real artifacts",
        paths == {"artifacts/results.xlsx", "artifacts/results.json"},
        str(sorted(paths)),
    )
    check(
        "each listed artifact carries a usable download path",
        all(artifact.download_path for artifact in snapshot.artifacts),
        str([a.download_path for a in snapshot.artifacts]),
    )
    check(
        "sizes are read from the filesystem, not reported by the model",
        all(artifact.size_bytes > 0 for artifact in snapshot.artifacts),
        str([a.size_bytes for a in snapshot.artifacts]),
    )

    # A file the agent only CLAIMED to write is simply not there.
    check(
        "a fabricated artifact does not appear in the listing",
        "artifacts/imaginary-report.xlsx" not in paths,
    )

    # --- 3. A job that needs no file gets no workspace ---------------------
    plain_id = await _submit("Explain how photosynthesis works", [])
    check(
        "a plain question gets no workspace (unchanged, cheap behaviour)",
        not main._JOBS[plain_id].get("workspace_root"),
        str(main._JOBS[plain_id].get("workspace_root")),
    )
    check(
        "a plain question's snapshot lists no artifacts",
        main._job_snapshot(plain_id).artifacts == [],
    )

    # --- 4. Download endpoint ---------------------------------------------
    response = main.download_artifact(job_id, "results.xlsx")
    check(
        "an artifact downloads with the correct spreadsheet media type",
        response.media_type
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        str(response.media_type),
    )
    check(
        "the served path is the real file on disk",
        Path(response.path).is_file()
        and Path(response.path).name == "results.xlsx",
        str(response.path),
    )
    check(
        "the download carries nosniff",
        response.headers.get("x-content-type-options") == "nosniff",
        str(dict(response.headers)),
    )
    check(
        "generated output is served under a restrictive CSP",
        "frame-ancestors 'self'" in (response.headers.get("content-security-policy") or ""),
        str(response.headers.get("content-security-policy")),
    )

    json_response = main.download_artifact(job_id, "results.json")
    check(
        "a JSON artifact gets the JSON media type",
        json_response.media_type == "application/json",
        str(json_response.media_type),
    )

    # --- 4b. Bundle ZIP download -------------------------------------------
    import io  # noqa: E402
    import zipfile  # noqa: E402

    bundle_tools = ArtifactTools(Path(job["workspace_root"]))
    bundle_tools.create_code_bundle(
        "prayer-api",
        files=json.dumps(
            {
                "server.js": "console.log('hi')",
                "package.json": json.dumps({"name": "prayer-api"}),
            }
        ),
    )
    zip_response = main.download_artifact_bundle(job_id, "prayer-api")
    check(
        "bundle zip: correct media type",
        zip_response.media_type == "application/zip",
        str(zip_response.media_type),
    )
    check(
        "bundle zip: filename in content-disposition",
        'filename="prayer-api.zip"' in (zip_response.headers.get("content-disposition") or ""),
        str(zip_response.headers.get("content-disposition")),
    )
    check(
        "bundle zip: carries nosniff",
        zip_response.headers.get("x-content-type-options") == "nosniff",
    )
    with zipfile.ZipFile(io.BytesIO(zip_response.body)) as archive:
        names = set(archive.namelist())
        check(
            "bundle zip: contains exactly the bundle's real members, rooted at its folder",
            names == {"prayer-api/server.js", "prayer-api/package.json"},
            str(sorted(names)),
        )
        check(
            "bundle zip: member content survived",
            archive.read("prayer-api/server.js").decode("utf-8") == "console.log('hi')",
        )

    # A standalone file (not a bundle) has no manifest -> not zippable.
    status = None
    try:
        main.download_artifact_bundle(job_id, "results.xlsx")
    except HTTPException as exc:
        status = exc.status_code
    check("bundle zip: a non-bundle path 404s rather than archiving anything", status == 404, str(status))

    for attack in (
        "../sources",
        "../../outside",
        "/etc",
        "prayer-api/../../sources",
    ):
        status = None
        try:
            main.download_artifact_bundle(job_id, attack)
        except HTTPException as exc:
            status = exc.status_code
        except Exception as exc:  # noqa: BLE001 - any refusal is acceptable
            status = type(exc).__name__
        check(f"bundle zip: traversal rejected: {attack!r}", status is not None, str(status))

    # --- 5. Download endpoint confinement ---------------------------------
    workspace_root = Path(job["workspace_root"])
    (workspace_root / "sources").mkdir(parents=True, exist_ok=True)
    (workspace_root / "sources" / "secret.txt").write_text("UPLOADED SECRET")
    outside = Path(_tmp_dir) / "outside-secret.txt"
    outside.write_text("HOST SECRET")

    attacks = [
        "../sources/secret.txt",
        "../../outside-secret.txt",
        "..%2f..%2foutside-secret.txt",
        "/etc/passwd",
        "....//....//outside-secret.txt",
        "..\\..\\outside-secret.txt",
        "sources/secret.txt",
        "../../../../../../etc/passwd",
    ]
    for attack in attacks:
        status = None
        try:
            main.download_artifact(job_id, attack)
        except HTTPException as exc:
            status = exc.status_code
        except Exception as exc:  # noqa: BLE001 - any refusal is acceptable
            status = type(exc).__name__
        check(f"download traversal rejected: {attack!r}", status is not None, str(status))

    # An unknown job and a job with no workspace both 404 rather than leaking.
    for label, call in (
        ("unknown job", lambda: main.download_artifact(str(uuid.uuid4()), "x.json")),
        ("job with no workspace", lambda: main.download_artifact(plain_id, "x.json")),
        ("nonexistent artifact", lambda: main.download_artifact(job_id, "nope.xlsx")),
    ):
        status = None
        try:
            call()
        except HTTPException as exc:
            status = exc.status_code
        check(f"download 404s for {label}", status == 404, str(status))

    # --- 6. Unsupported capability fails early and explicitly -------------
    #
    # Only reachable when something NAMES a capability outside the registry.
    # Deterministic resolution can only ever emit registry names, so in
    # production this path is unreachable by construction -- which is a
    # stronger guarantee than the check itself. It is exercised through the
    # retained router, the one component that can still invent a name.
    unsupported_id = await _submit(
        "Render a 3D animation of this",
        ["holographic_projection"],
    )
    unsupported_job = main._JOBS[unsupported_id]
    check(
        "an unsupported capability fails the job explicitly",
        unsupported_job["status"] == "failed",
        unsupported_job["status"],
    )
    check(
        "the failure names the missing capability",
        "holographic_projection" in (unsupported_job.get("error") or ""),
        str(unsupported_job.get("error")),
    )
    check(
        "the user still gets a real answer explaining why",
        "Nothing was executed" in (unsupported_job.get("result") or ""),
        str(unsupported_job.get("result"))[:160],
    )
    check(
        "no executor was started for an impossible request",
        _fake_run_single_agent.seen.get("workspace_root")
        != unsupported_job.get("workspace_root")
        or unsupported_job.get("result", "").startswith("This request needs"),
        "the executor should not have run",
    )

    # --- 7. A delivered artifact never reports plain failure --------------
    #
    # The live Case B: a valid XLSX was created and shown in the UI, and the
    # job still said "failed" because the executor ran out of turns before
    # writing a summary. From the user's side that is not a failure.
    def _fails_after_producing(
        callback, tracker, recorder, content, task_id, on_ready,
        workspace_root=None, *args, **kwargs,
    ):
        ArtifactTools(Path(workspace_root)).create_xlsx_artifact(
            "delivered.xlsx", json.dumps([{"a": 1}])
        )
        return Task(content=content, id=task_id, state=TaskState.FAILED, result="")

    with patch.object(
        camel_service, "run_workforce_in_thread", _fails_after_producing
    ), patch.object(
        camel_service, "synthesize_workforce_result", _fake_synth
    ), patch.object(
        main.artifact_intent, "classify_artifact_intent", _fake_intent
    ):
        response = await main.run_pipeline(
            main.RunPipelineRequest(
                topic="Give me the results as an Excel file", mode="multi"
            )
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        delivered = main._JOBS[response.job_id]

    check(
        "a run that produced a real file is not reported as failed",
        delivered["status"] == "partial",
        delivered["status"],
    )
    snapshot = main._job_snapshot(response.job_id)
    check(
        "the delivered file is listed for download",
        [a.filename for a in snapshot.artifacts] == ["delivered.xlsx"],
        str([a.filename for a in snapshot.artifacts]),
    )
    check(
        "the terminal reason says a partial run still delivered files",
        snapshot.usage.terminal_reason == "partial_with_artifacts",
        str(snapshot.usage.terminal_reason),
    )
    check(
        "the user is told the files are usable",
        "ready to download" in (delivered.get("error") or ""),
        str(delivered.get("error")),
    )

    # A run that produced NOTHING must still fail plainly.
    async def _fails_with_nothing(content, tracker, task_id, queue, **kwargs):
        return Task(content=content, id=task_id, state=TaskState.FAILED, result="")

    with patch.object(camel_service, "run_single_agent", _fails_with_nothing):
        response = await main.run_pipeline(
            main.RunPipelineRequest(topic="Explain quantum tunnelling", mode="single")
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    check(
        "a run that produced nothing still reports failure",
        main._JOBS[response.job_id]["status"] == "failed",
        main._JOBS[response.job_id]["status"],
    )

    # --- 8. A job can never be left "running" forever ---------------------
    #
    # _run_job wraps its body, but the lines before that try -- and a
    # cancellation delivered outside it -- are uncovered. asyncio surfaces
    # such an exception only if someone retrieves it, so without the done
    # callback the row stays "running" and the UI spins with no error.
    logging.getLogger("agent_webapp.jobs").setLevel(logging.CRITICAL)
    try:
        stuck_id = str(uuid.uuid4())
        main._JOBS[stuck_id] = {
            **main._JOBS[response.job_id],
            "job_id": stuck_id,
            "status": "running",
            "error": None,
        }

        async def _explode():
            raise RuntimeError("died before the try block")

        task = asyncio.create_task(_explode())
        task._agent_job_id = stuck_id
        task.add_done_callback(main._job_task_finished)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        logging.getLogger("agent_webapp.jobs").setLevel(logging.WARNING)

    check(
        "an exception outside _run_job's guard still terminates the job",
        main._JOBS[stuck_id]["status"] == "error",
        main._JOBS[stuck_id]["status"],
    )
    check(
        "and the cause is recorded rather than swallowed",
        "died before the try block" in (main._JOBS[stuck_id].get("error") or ""),
        str(main._JOBS[stuck_id].get("error")),
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All artifact-API checks passed")


if __name__ == "__main__":
    asyncio.run(_run())
