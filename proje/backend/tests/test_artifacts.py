"""Tests for real artifact creation (artifacts.py).

The point of every assertion here is the same: the system must produce a REAL
file, not a claim that it produced one. So these tests open what was written,
parse it, and check the bytes -- they never assert on a tool's return value
alone, because a return value saying "created: true" is exactly the kind of
evidence this module exists to stop trusting.

No pytest suite exists in this repo (see test_upload.py) -- run directly:
    python test_artifacts.py
"""

import csv
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import artifacts
from artifacts import ArtifactError, ArtifactTools
from workspace import WorkspaceSecurityError

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def _workspace() -> Path:
    """A job workspace shaped like a real one: sources/ plus an upload."""
    root = Path(tempfile.mkdtemp(prefix="artifact_ws_")).resolve()
    sources = root / "sources" / "abc123"
    sources.mkdir(parents=True)
    (sources / "input.xlsx").write_bytes(b"ORIGINAL UPLOAD BYTES")
    (sources / "notes.txt").write_text("original notes", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Real files exist and are valid
# ---------------------------------------------------------------------------


def test_json_artifact_is_real_and_parseable() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_json_artifact(
        "prayer-times.json",
        json.dumps([{"date": "2026-08-01", "fajr": "04:12"}], ensure_ascii=False),
    )

    path = root / result["path"]
    check("json: the file exists on disk", path.is_file(), str(path))
    check("json: it is inside artifacts/", result["path"].startswith("artifacts/"), result["path"])

    # The whole promise: what was written parses.
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        parsed_ok = parsed == [{"date": "2026-08-01", "fajr": "04:12"}]
    except json.JSONDecodeError as exc:
        parsed, parsed_ok = None, False
        print(f"       json decode error: {exc}")
    check("json: the written file parses and round-trips", parsed_ok, str(parsed))
    check(
        "json: the reported size matches the real file",
        result["size_bytes"] == path.stat().st_size,
        f"{result['size_bytes']} vs {path.stat().st_size}",
    )


def test_json_artifact_rejects_malformed_payload() -> None:
    """A bad payload must fail the CALL, never produce a broken file."""
    root = _workspace()
    tools = ArtifactTools(root)
    raised = ""
    try:
        tools.create_json_artifact("bad.json", "{not valid json,,,}")
    except ArtifactError as exc:
        raised = str(exc)
    check("json: malformed input raises ArtifactError", bool(raised), raised)
    check(
        "json: the error says what was wrong",
        "not valid JSON" in raised,
        raised,
    )
    check(
        "json: no partial file was left behind",
        not (root / "artifacts" / "bad.json").exists(),
    )


def test_xlsx_artifact_reopens() -> None:
    """The generated workbook must open in a real xlsx reader.

    This is the assertion that makes "the model must never fabricate a binary
    format" enforceable rather than aspirational.
    """
    root = _workspace()
    tools = ArtifactTools(root)
    rows = [
        {"Tarih": "2026-08-01", "İmsak": "04:12", "Güneş": "05:45"},
        {"Tarih": "2026-08-02", "İmsak": "04:13", "Güneş": "05:46"},
    ]
    result = tools.create_xlsx_artifact(
        "ankara-namaz.xlsx", json.dumps(rows, ensure_ascii=False), sheet_name="Ağustos"
    )
    path = root / result["path"]

    check("xlsx: the file exists on disk", path.is_file(), str(path))
    check(
        "xlsx: it is a real ZIP container (the OOXML envelope)",
        zipfile.is_zipfile(path),
    )

    from openpyxl import load_workbook

    workbook = load_workbook(path)
    sheet = workbook.active
    values = [[cell.value for cell in row] for row in sheet.iter_rows()]

    check("xlsx: it reopens with openpyxl", sheet is not None)
    check(
        "xlsx: the header row is the record keys",
        values[0] == ["Tarih", "İmsak", "Güneş"],
        str(values[0]),
    )
    check(
        "xlsx: every data row survived",
        values[1] == ["2026-08-01", "04:12", "05:45"]
        and values[2] == ["2026-08-02", "04:13", "05:46"],
        str(values[1:]),
    )
    check(
        "xlsx: non-ASCII sheet names and cell values survive",
        sheet.title == "Ağustos" and values[0][1] == "İmsak",
        f"{sheet.title} / {values[0]}",
    )
    check(
        "xlsx: the reported row count matches what was written",
        result["row_count"] == 2 and result["column_count"] == 3,
        str(result),
    )
    check(
        "xlsx: tool result has no href-shaped download_path for the model to paste",
        "download_path" not in result,
        str(result),
    )
    check(
        "xlsx: note says the UI lists the file",
        "UI lists" in str(result.get("note") or ""),
        str(result.get("note")),
    )


def test_csv_artifact_is_excel_readable() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_csv_artifact(
        "data.csv", json.dumps([{"ad": "Ömer", "şehir": "İstanbul"}], ensure_ascii=False)
    )
    path = root / result["path"]
    raw = path.read_bytes()
    check("csv: the file exists", path.is_file())
    check(
        "csv: it carries a UTF-8 BOM so Excel does not mangle non-ASCII",
        raw.startswith(b"\xef\xbb\xbf"),
        repr(raw[:8]),
    )
    text = raw.decode("utf-8-sig")
    check(
        "csv: header and row are present and correctly encoded",
        text.splitlines() == ["ad,şehir", "Ömer,İstanbul"],
        repr(text),
    )


def test_row_array_and_envelope_shapes() -> None:
    """Models send tabular data in several shapes; all must work."""
    root = _workspace()
    tools = ArtifactTools(root)

    header_first = json.dumps([["a", "b"], [1, 2], [3, 4]])
    result = tools.create_csv_artifact("rows.csv", header_first)
    check("shapes: header-as-first-row is accepted", result["row_count"] == 2, str(result))

    explicit = tools.create_csv_artifact(
        "explicit.csv", json.dumps([[1, 2], [3, 4]]), columns=json.dumps(["a", "b"])
    )
    check("shapes: explicit columns are accepted", explicit["row_count"] == 2, str(explicit))

    envelope = tools.create_csv_artifact(
        "envelope.csv", json.dumps({"columns": ["a", "b"], "rows": [[1, 2]]})
    )
    check("shapes: {columns, rows} envelope is accepted", envelope["row_count"] == 1, str(envelope))

    fenced = tools.create_json_artifact("fenced.json", '```json\n{"ok": true}\n```')
    check(
        "shapes: a ```json fence is unwrapped, not written literally",
        json.loads((root / fenced["path"]).read_text())["ok"] is True,
    )

    ragged = tools.create_csv_artifact(
        "ragged.csv", json.dumps([["a", "b", "c"], [1], [2, 3, 4]])
    )
    ragged_text = (root / ragged["path"]).read_text(encoding="utf-8-sig")
    check(
        "shapes: a short row is padded, never column-shifted",
        ragged_text.splitlines()[1] == "1,,",
        repr(ragged_text),
    )


def test_pdf_artifact_is_a_real_pdf() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_pdf_artifact(
        "briefing.pdf",
        title="Q1 Briefing",
        content="# Summary\n\n- Revenue grew.\n- Costs fell.",
    )
    path = root / result["path"]
    check("pdf: the file exists on disk", path.is_file(), str(path))
    check("pdf: kind is pdf", result.get("kind") == "pdf", str(result))
    check(
        "pdf: bytes start with the PDF magic",
        path.read_bytes()[:5] == b"%PDF-",
        repr(path.read_bytes()[:16]),
    )
    check("pdf: filename ends .pdf", path.name.endswith(".pdf"), path.name)
    source = root / "artifacts" / "notes.md"
    source.write_text("Body from disk.", encoding="utf-8")
    from_disk = tools.create_pdf_artifact(
        "from-notes.pdf", title="Notes", from_path="artifacts/notes.md"
    )
    check(
        "pdf: from_path renders a second file",
        (root / from_disk["path"]).read_bytes()[:5] == b"%PDF-",
        str(from_disk),
    )


def test_web_bundle_is_a_real_working_folder() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_web_bundle(
        "search-app",
        html='<!doctype html><link rel="stylesheet" href="styles.css"><script src="app.js"></script>',
        css="body { font-family: system-ui; }",
        js="fetch('data.json').then(r => r.json());",
        data=json.dumps([{"id": 1, "name": "row"}]),
    )
    folder = root / "artifacts" / "search-app"

    for member in ("index.html", "styles.css", "app.js", "data.json"):
        check(f"bundle: {member} exists", (folder / member).is_file(), str(folder / member))
    check(
        "bundle: the entry point is index.html",
        result["entry_point"].endswith("index.html"),
        result["entry_point"],
    )
    check(
        "bundle: data.json is valid JSON",
        json.loads((folder / "data.json").read_text()) == [{"id": 1, "name": "row"}],
    )
    check(
        "bundle: the html references its siblings by relative path",
        'href="styles.css"' in (folder / "index.html").read_text(),
    )
    check(
        "bundle: tool result has no href-shaped download_path",
        "download_path" not in result,
        str(result),
    )


def test_code_bundle_writes_a_real_project() -> None:
    """create_code_bundle: an arbitrary multi-file project, code and data kept
    separate -- data_files copies bytes server-side, never through a tool
    argument."""
    root = _workspace()
    tools = ArtifactTools(root)
    dataset = [{"id": i, "name": f"row-{i}"} for i in range(365)]
    saved = tools.create_json_artifact("prayer-times.json", json.dumps(dataset))

    result = tools.create_code_bundle(
        "prayer-api",
        files=json.dumps(
            {
                "server.js": (
                    "const data = require('./data/prayer-times.json');\n"
                    "console.log(data.length);\n"
                ),
                "package.json": json.dumps({"name": "prayer-api", "version": "1.0.0"}),
            }
        ),
        data_files=json.dumps({"data/prayer-times.json": saved["path"]}),
        entry_point="server.js",
    )
    folder = root / "artifacts" / "prayer-api"

    for member in ("server.js", "package.json", "data/prayer-times.json"):
        check(f"code bundle: {member} exists", (folder / member).is_file(), str(folder / member))
    check(
        "code bundle: the entry point is server.js",
        result["entry_point"].endswith("server.js"),
        result["entry_point"],
    )
    check(
        "code bundle: the data file has all 365 rows, byte-identical to the source",
        (folder / "data" / "prayer-times.json").read_bytes()
        == (root / saved["path"]).read_bytes(),
    )
    check(
        "code bundle: the generated source does not contain the dataset inline",
        "row-364" not in (folder / "server.js").read_text(encoding="utf-8")
        and "row-0" not in (folder / "server.js").read_text(encoding="utf-8"),
    )
    check(
        "code bundle: the tool response does not carry the dataset either",
        "row-364" not in json.dumps(result),
        str(result)[:200],
    )


def test_code_bundle_rejects_traversal_and_missing_source() -> None:
    root = _workspace()
    tools = ArtifactTools(root)

    raised = ""
    try:
        tools.create_code_bundle(
            "evil", files=json.dumps({"../outside.js": "x = 1"})
        )
    except (ArtifactError, WorkspaceSecurityError) as exc:
        raised = str(exc) or exc.__class__.__name__
    check("code bundle: traversal in a source path is rejected", bool(raised), raised)

    raised = ""
    try:
        tools.create_code_bundle(
            "evil2",
            files=json.dumps({"index.js": "x = 1"}),
            data_files=json.dumps({"data.json": "../../sources/abc123/input.xlsx"}),
        )
    except (ArtifactError, WorkspaceSecurityError) as exc:
        raised = str(exc) or exc.__class__.__name__
    check("code bundle: traversal in a data_files source is rejected", bool(raised), raised)

    raised = ""
    try:
        tools.create_code_bundle(
            "evil3",
            files=json.dumps({"index.js": "x = 1"}),
            data_files=json.dumps({"data.json": "artifacts/does-not-exist.json"}),
        )
    except (ArtifactError, WorkspaceSecurityError) as exc:
        raised = str(exc) or exc.__class__.__name__
    check("code bundle: a missing data_files source fails clearly", bool(raised), raised)


def test_code_bundle_enforces_file_count_limit() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    too_many = {f"file{i}.txt": "x" for i in range(artifacts.MAX_BUNDLE_FILES + 5)}
    raised = ""
    try:
        tools.create_code_bundle("huge-project", files=json.dumps(too_many))
    except ArtifactError as exc:
        raised = str(exc)
    check("code bundle: the per-bundle file count limit is enforced", "limit" in raised, raised)


def test_bundle_provenance_and_manifest_grouping() -> None:
    """scan_artifacts must report a bundle as ONE deliverable (via its
    manifest), with real provenance/timestamps, not N unrelated files."""
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_json_artifact("standalone.json", json.dumps({"a": 1}))
    tools.create_web_bundle("web-app", html="<html></html>", js="x=1")
    tools.create_code_bundle(
        "api-app", files=json.dumps({"server.js": "console.log(1)"})
    )

    found = artifacts.scan_artifacts(root)
    by_path = {entry["path"]: entry for entry in found}

    standalone = by_path.get("artifacts/standalone.json")
    check(
        "provenance: a standalone artifact reports its producing tool",
        standalone is not None and standalone.get("producer") == "create_json_artifact",
        str(standalone),
    )
    check(
        "provenance: a standalone artifact reports a created_at timestamp",
        standalone is not None and bool(standalone.get("created_at")),
        str(standalone),
    )

    web_entry = by_path.get("artifacts/web-app/index.html")
    check(
        "provenance: a web bundle is reported as ONE grouped entry",
        web_entry is not None and len(web_entry.get("files", [])) == 2,
        str(web_entry),
    )
    check(
        "provenance: the web bundle's producer and manifest fields are real",
        web_entry is not None
        and web_entry.get("producer") == "create_web_bundle"
        and web_entry.get("bundle") == "artifacts/web-app",
        str(web_entry),
    )

    code_entry = by_path.get("artifacts/api-app/server.js")
    check(
        "provenance: a code bundle is reported as ONE grouped entry with its producer",
        code_entry is not None
        and code_entry.get("producer") == "create_code_bundle"
        and code_entry.get("entry_point") == "artifacts/api-app/server.js",
        str(code_entry),
    )

    # Exactly 3 top-level deliverables: the standalone file plus one entry
    # per bundle -- never the bundles' 3 member files individually.
    check(
        "provenance: bundle members do not also appear as separate top-level entries",
        len(found) == 3,
        str(sorted(by_path)),
    )


def test_bundle_manifest_reflects_disk_truth_when_a_member_is_removed() -> None:
    """A manifest naming a file that is no longer there must not be reported
    as if it still is -- scan_artifacts always re-derives from disk."""
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_web_bundle(
        "web-app", html="<html></html>", css="body{}", js="x=1"
    )
    (root / "artifacts" / "web-app" / "styles.css").unlink()

    found = artifacts.scan_artifacts(root)
    entry = next((e for e in found if e.get("bundle") == "artifacts/web-app"), None)
    check(
        "manifest disk-truth: a removed member is not listed as present",
        entry is not None
        and "artifacts/web-app/styles.css" not in {m["path"] for m in entry.get("files", [])},
        str(entry),
    )


def test_convert_table_to_json_preserves_large_row_counts() -> None:
    """table -> JSON at both a mid-size (365) and large (5,000) row count:
    every row survives, and the tool RESPONSE stays small regardless -- the
    reference-based handoff this whole module exists for."""
    root = _workspace()
    tools = ArtifactTools(root)

    for row_count in (365, 5_000):
        rows = [{"id": i, "value": f"v{i}"} for i in range(row_count)]
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["id", "value"])
        for row in rows:
            writer.writerow([row["id"], row["value"]])
        source = root / "sources" / "abc123" / f"table-{row_count}.csv"
        source.write_text(csv_buffer.getvalue(), encoding="utf-8")

        result = tools.convert_table_to_json(
            f"sources/abc123/table-{row_count}.csv", filename=f"data-{row_count}.json"
        )
        check(
            f"convert: {row_count} rows reported correctly",
            result["row_count"] == row_count,
            str(result["row_count"]),
        )
        check(
            f"convert: the tool response stays small for {row_count} rows",
            len(json.dumps(result, ensure_ascii=False)) < 5_000,
            str(len(json.dumps(result))),
        )
        written = json.loads((root / result["path"]).read_text(encoding="utf-8"))
        check(
            f"convert: all {row_count} rows actually landed in the file",
            len(written) == row_count and written[-1]["id"] == row_count - 1,
            str(len(written)),
        )


# ---------------------------------------------------------------------------
# Bundle ZIP download
# ---------------------------------------------------------------------------


def test_bundle_zip_preserves_nested_structure_and_excludes_scratch() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    # A standalone artifact, an unrelated intermediate/scratch file, and the
    # bundle itself all coexist -- the zip must contain only the bundle.
    tools.create_json_artifact("standalone.json", json.dumps({"a": 1}))
    (root / "artifacts" / artifacts.INTERNAL_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / artifacts.INTERNAL_DIRNAME / "scratch.json").write_text("[]")

    saved = tools.create_json_artifact("prayer-times.json", json.dumps([{"id": 1}]))
    tools.create_code_bundle(
        "prayer-api",
        files=json.dumps(
            {
                "server.js": "console.log(1)",
                "src/routes/index.js": "module.exports = {}",
            }
        ),
        data_files=json.dumps({"data/prayer-times.json": saved["path"]}),
    )

    bundle = artifacts.resolve_bundle_for_zip(root, "prayer-api")
    payload = artifacts.build_bundle_zip(bundle)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())

    check(
        "bundle zip: nested directory structure is preserved",
        "prayer-api/src/routes/index.js" in names,
        str(sorted(names)),
    )
    check(
        "bundle zip: contains exactly the bundle's members, nothing else",
        names
        == {
            "prayer-api/server.js",
            "prayer-api/src/routes/index.js",
            "prayer-api/data/prayer-times.json",
        },
        str(sorted(names)),
    )
    check(
        "bundle zip: the standalone artifact is excluded",
        not any("standalone" in n for n in names),
        str(sorted(names)),
    )
    check(
        "bundle zip: scratch/intermediate files are excluded",
        not any("scratch" in n for n in names),
        str(sorted(names)),
    )
    check(
        "bundle zip: source uploads are excluded",
        not any(n.startswith("sources/") or "/sources/" in n for n in names),
        str(sorted(names)),
    )


def test_bundle_zip_rejects_traversal_and_non_bundles() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_json_artifact("standalone.json", json.dumps({"a": 1}))
    tools.create_web_bundle("web-app", html="<html></html>", js="x=1")

    for attack in ("../sources", "../../etc", "/etc", "web-app/../../sources"):
        raised = ""
        try:
            artifacts.resolve_bundle_for_zip(root, attack)
        except (ArtifactError, WorkspaceSecurityError) as exc:
            raised = str(exc) or exc.__class__.__name__
        check(f"bundle zip: traversal rejected: {attack!r}", bool(raised), raised)

    raised = ""
    try:
        artifacts.resolve_bundle_for_zip(root, "standalone.json")
    except ArtifactError as exc:
        raised = str(exc)
    check(
        "bundle zip: a plain file path (not a directory) is rejected",
        bool(raised),
        raised,
    )

    raised = ""
    try:
        artifacts.resolve_bundle_for_zip(root, "does-not-exist")
    except (ArtifactError, WorkspaceSecurityError) as exc:
        raised = str(exc) or exc.__class__.__name__
    check("bundle zip: a nonexistent bundle 404s (raises)", bool(raised), raised)

    # A directory with no manifest is not a bundle, even though it is real.
    (root / "artifacts" / "not-a-bundle").mkdir()
    (root / "artifacts" / "not-a-bundle" / "file.txt").write_text("x")
    raised = ""
    try:
        artifacts.resolve_bundle_for_zip(root, "not-a-bundle")
    except ArtifactError as exc:
        raised = str(exc)
    check(
        "bundle zip: a directory without a manifest is rejected, not silently zipped",
        bool(raised) and "manifest" in raised,
        raised,
    )


def test_bundle_zip_handles_missing_members_and_stale_manifest() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_web_bundle("web-app", html="<html></html>", css="body{}", js="x=1")
    (root / "artifacts" / "web-app" / "styles.css").unlink()

    bundle = artifacts.resolve_bundle_for_zip(root, "web-app")
    check(
        "bundle zip: a member removed after the manifest was written is honestly excluded",
        {m.relative for m in bundle.members} == {"index.html", "app.js"},
        str([m.relative for m in bundle.members]),
    )

    # A manifest naming a file OUTSIDE the bundle folder (hand-tampered, or a
    # future bug) must not let that file leak into the archive -- it must be
    # dropped, not resolved and included.
    manifest_path = root / "artifacts" / "web-app" / artifacts._MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outside_secret = root / "sources" / "abc123" / "input.xlsx"
    manifest["files"].append("sources/abc123/input.xlsx")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    tampered = artifacts.resolve_bundle_for_zip(root, "web-app")
    check(
        "bundle zip: a manifest entry pointing outside the bundle is not included",
        outside_secret.name not in {Path(m.relative).name for m in tampered.members},
        str([m.relative for m in tampered.members]),
    )

    # Every member gone -> nothing to download, not an empty/broken zip.
    for member in ("index.html", "app.js"):
        (root / "artifacts" / "web-app" / member).unlink()
    raised = ""
    try:
        artifacts.resolve_bundle_for_zip(root, "web-app")
    except ArtifactError as exc:
        raised = str(exc)
    check(
        "bundle zip: a bundle with no files left fails clearly instead of zipping empty",
        bool(raised),
        raised,
    )


def test_bundle_zip_skips_symlinked_members() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_code_bundle(
        "linked-app", files=json.dumps({"server.js": "console.log(1)"})
    )
    target = root / "sources" / "abc123" / "input.xlsx"
    link = root / "artifacts" / "linked-app" / "linked.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        check(
            "bundle zip: symlinked members are excluded (skipped: platform does not support symlinks here)",
            True,
        )
        return

    manifest_path = root / "artifacts" / "linked-app" / artifacts._MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append("artifacts/linked-app/linked.txt")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bundle = artifacts.resolve_bundle_for_zip(root, "linked-app")
    check(
        "bundle zip: a symlinked member is never followed into the archive",
        "linked.txt" not in {m.relative for m in bundle.members},
        str([m.relative for m in bundle.members]),
    )


def test_bundle_zip_enforces_member_and_size_limits() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_code_bundle(
        "small-app", files=json.dumps({"server.js": "console.log(1)"})
    )

    # Hand-tamper the manifest to name more files than the zip limit allows,
    # since create_code_bundle itself already refuses to write that many.
    manifest_path = root / "artifacts" / "small-app" / artifacts._MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extra_dir = root / "artifacts" / "small-app" / "many"
    extra_dir.mkdir()
    for i in range(artifacts.MAX_ZIP_MEMBERS + 5):
        (extra_dir / f"f{i}.txt").write_text("x")
        manifest["files"].append(f"artifacts/small-app/many/f{i}.txt")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    raised = ""
    try:
        artifacts.resolve_bundle_for_zip(root, "small-app")
    except ArtifactError as exc:
        raised = str(exc)
    check(
        "bundle zip: the member-count limit is enforced",
        "files" in raised or "limit" in raised,
        raised,
    )


def test_create_docx_from_template_preserves_upload_and_writes_new_body() -> None:
    """create_docx_from_template must clone the uploaded .docx's own
    formatting for new content, and must never touch the upload itself."""
    from docx import Document
    from docx.shared import Pt

    root = _workspace()
    template_dir = root / "sources" / "tmpl1"
    template_dir.mkdir(parents=True)
    src = Document()
    src.styles["Normal"].font.name = "Georgia"
    src.styles["Normal"].font.size = Pt(13)
    src.add_paragraph("original template body")
    template_path = template_dir / "sablon.docx"
    src.save(template_path)
    original_bytes = template_path.read_bytes()

    tools = ArtifactTools(root)
    result = tools.create_docx_from_template(
        "kira-sozlesmesi.docx",
        "sources/tmpl1/sablon.docx",
        title="Kira Sözleşmesi",
        content="Yeni sözleşme metni burada.",
    )

    out_path = root / result["path"]
    check("template docx: output file exists", out_path.is_file(), str(out_path))
    reopened = Document(str(out_path))
    check(
        "template docx: preserves the template's font",
        reopened.styles["Normal"].font.name == "Georgia",
    )
    body_text = "\n".join(p.text for p in reopened.paragraphs)
    check(
        "template docx: has the new content",
        "Yeni sözleşme metni" in body_text,
        body_text,
    )
    check(
        "template docx: does not carry over the old body",
        "original template body" not in body_text,
        body_text,
    )
    check(
        "template docx: original template bytes on disk are untouched",
        template_path.read_bytes() == original_bytes,
    )

    raised = ""
    try:
        tools.create_docx_from_template(
            "evil.docx", "../../etc/passwd", title="x", content="y"
        )
    except (ArtifactError, WorkspaceSecurityError) as exc:
        raised = str(exc)
    check(
        "template docx: out-of-workspace template_path raises",
        bool(raised),
        raised,
    )


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


def test_source_uploads_are_never_touched() -> None:
    """The strongest guarantee: writing artifacts cannot alter an upload.

    Attempted deliberately, with names chosen to collide with the upload, and
    with traversal sequences aimed straight at it.
    """
    root = _workspace()
    source = root / "sources" / "abc123" / "input.xlsx"
    before = source.read_bytes()
    tools = ArtifactTools(root)

    # Same filename as the upload -- lands in artifacts/, not on top of it.
    tools.create_xlsx_artifact("input.xlsx", json.dumps([{"a": 1}]))
    check(
        "sources: an artifact with the same name does not overwrite the upload",
        source.read_bytes() == before,
    )
    check(
        "sources: it landed in artifacts/ instead",
        (root / "artifacts" / "input.xlsx").is_file(),
    )

    for attack in (
        "../sources/abc123/input.xlsx",
        "../../sources/abc123/input.xlsx",
        "sources/abc123/input.xlsx",
        "/etc/passwd",
        "..\\..\\sources\\abc123\\input.xlsx",
    ):
        rejected_or_contained = False
        try:
            result = tools.create_text_artifact(attack, "malicious content")
            # Not raising is acceptable ONLY if the name was flattened into
            # the artifacts directory rather than escaping it.
            written = (root / result["path"]).resolve()
            rejected_or_contained = written.is_relative_to(
                (root / "artifacts").resolve()
            )
        except (ArtifactError, WorkspaceSecurityError, ValueError):
            rejected_or_contained = True
        check(f"traversal rejected or contained: {attack!r}", rejected_or_contained)

    check(
        "sources: the upload is byte-identical after every attempt",
        source.read_bytes() == before,
    )
    check(
        "sources: the sibling upload is untouched too",
        (root / "sources" / "abc123" / "notes.txt").read_text() == "original notes",
    )


def test_filenames_are_made_safe() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    result = tools.create_json_artifact("we ird/na\x00me.json", "{}")
    written = (root / result["path"]).resolve()
    check(
        "filenames: a directory component is stripped",
        written.parent == (root / "artifacts").resolve(),
        str(written),
    )
    check("filenames: the file really exists", written.is_file(), str(written))
    check(
        "filenames: control characters are removed",
        "\x00" not in written.name,
        repr(written.name),
    )

    # A wrong extension is corrected rather than rejected: failing the call
    # over it would waste a whole model turn.
    corrected = tools.create_xlsx_artifact("report.txt", json.dumps([{"a": 1}]))
    check(
        "filenames: the extension is corrected to match the writer",
        corrected["filename"].endswith(".xlsx"),
        corrected["filename"],
    )


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_bounds_are_enforced() -> None:
    root = _workspace()
    tools = ArtifactTools(root)

    too_many_rows = json.dumps([{"a": i} for i in range(artifacts.MAX_ARTIFACT_ROWS + 1)])
    raised = ""
    try:
        tools.create_csv_artifact("big.csv", too_many_rows)
    except ArtifactError as exc:
        raised = str(exc)
    check("bounds: the row limit is enforced", "limit" in raised, raised)

    raised = ""
    try:
        tools.create_text_artifact("huge.md", "x" * (artifacts.MAX_PAYLOAD_CHARS + 1))
    except ArtifactError as exc:
        raised = str(exc)
    check("bounds: the per-call payload limit is enforced", "limit" in raised, raised)

    raised = ""
    try:
        tools.create_csv_artifact(
            "wide.csv",
            json.dumps([{f"c{i}": i for i in range(artifacts.MAX_ARTIFACT_COLUMNS + 5)}]),
        )
    except ArtifactError as exc:
        raised = str(exc)
    check("bounds: the column limit is enforced", "columns" in raised, raised)

    # Artifact count. Rewriting the SAME name must not consume budget, since a
    # model correcting its own output should not be charged twice.
    fresh = ArtifactTools(_workspace())
    for i in range(artifacts.MAX_ARTIFACTS_PER_JOB):
        fresh.create_text_artifact(f"note{i}.md", "x")
    for _ in range(3):
        fresh.create_text_artifact("note0.md", "rewritten")
    check(
        "bounds: rewriting an existing artifact does not consume budget",
        len(fresh.records()) == artifacts.MAX_ARTIFACTS_PER_JOB,
        str(len(fresh.records())),
    )
    raised = ""
    try:
        fresh.create_text_artifact("one-too-many.md", "x")
    except ArtifactError as exc:
        raised = str(exc)
    check("bounds: the artifact count limit is enforced", "limit" in raised, raised)


def test_empty_and_missing_payloads_fail_clearly() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    for label, call in (
        ("empty json", lambda: tools.create_json_artifact("a.json", "")),
        ("empty text", lambda: tools.create_text_artifact("a.md", "   ")),
        ("empty html", lambda: tools.create_html_artifact("a.html", "")),
        ("no rows", lambda: tools.create_csv_artifact("a.csv", "[]")),
        ("wrong type", lambda: tools.create_csv_artifact("a.csv", '"a string"')),
    ):
        raised = False
        try:
            call()
        except ArtifactError:
            raised = True
        check(f"explicit failure for {label}", raised)


# ---------------------------------------------------------------------------
# Discovery from disk
# ---------------------------------------------------------------------------


def test_scan_artifacts_reports_only_real_files() -> None:
    root = _workspace()
    tools = ArtifactTools(root)
    tools.create_json_artifact("a.json", "{}")
    tools.create_web_bundle("app", html="<html></html>", js="x=1")

    found = artifacts.scan_artifacts(root)
    paths = {entry["path"] for entry in found}

    check(
        "scan: it lists the files that were actually written",
        "artifacts/a.json" in paths and "artifacts/app/index.html" in paths,
        str(sorted(paths)),
    )
    check(
        "scan: it never lists an upload",
        not any("sources/" in path for path in paths),
        str(sorted(paths)),
    )
    check(
        "scan: a bundle index is marked as the entry point",
        any(entry.get("entry_point") for entry in found),
        str(found),
    )
    check(
        "scan: a workspace with no artifacts directory returns nothing",
        artifacts.scan_artifacts(tempfile.mkdtemp()) == [],
    )

    # The core anti-fabrication property: a file the model only CLAIMED to
    # write does not appear, because scanning reads the disk.
    check(
        "scan: a file that was never written is not reported",
        "artifacts/imaginary.xlsx" not in paths,
    )


def main() -> None:
    scratch: list[str] = []
    try:
        test_json_artifact_is_real_and_parseable()
        test_json_artifact_rejects_malformed_payload()
        test_xlsx_artifact_reopens()
        test_csv_artifact_is_excel_readable()
        test_row_array_and_envelope_shapes()
        test_pdf_artifact_is_a_real_pdf()
        test_web_bundle_is_a_real_working_folder()
        test_code_bundle_writes_a_real_project()
        test_code_bundle_rejects_traversal_and_missing_source()
        test_code_bundle_enforces_file_count_limit()
        test_bundle_provenance_and_manifest_grouping()
        test_bundle_manifest_reflects_disk_truth_when_a_member_is_removed()
        test_convert_table_to_json_preserves_large_row_counts()
        test_create_docx_from_template_preserves_upload_and_writes_new_body()
        test_bundle_zip_preserves_nested_structure_and_excludes_scratch()
        test_bundle_zip_rejects_traversal_and_non_bundles()
        test_bundle_zip_handles_missing_members_and_stale_manifest()
        test_bundle_zip_skips_symlinked_members()
        test_bundle_zip_enforces_member_and_size_limits()
        test_source_uploads_are_never_touched()
        test_filenames_are_made_safe()
        test_bounds_are_enforced()
        test_empty_and_missing_payloads_fail_clearly()
        test_scan_artifacts_reports_only_real_files()
    finally:
        for path in scratch:
            shutil.rmtree(path, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All artifact checks passed")


if __name__ == "__main__":
    main()
