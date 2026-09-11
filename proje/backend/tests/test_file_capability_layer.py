"""Regression suite for the file-handling capability layer (workspace.py).

The goal this protects is NOT "every format is deeply understood" -- it is
that an arbitrary mix of user uploads can never make a job run away: no
unbounded tool output, no repeated identical reads that silently return the
same thing forever, no crash on an empty/unsupported/malformed file, and a
clear, non-retryable message whenever a file has no readable representation.

Exercises the real workspace.py: build_job_workspace (the same function
main.py's upload path calls) followed by ReadOnlyWorkspaceTools (the same
class camel_service.py wires into every worker). No model, no network, no
FastAPI app import -- upload records are constructed directly, exactly in
the shape main.py produces them (file_id/filename/path/extracted_text/kind).

Run directly:
    python test_file_capability_layer.py
"""

import csv
import shutil
import tempfile
import uuid
from pathlib import Path

import pandas as pd

import workspace
from workspace import ReadOnlyWorkspaceTools, WorkspaceSecurityError

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


# --------------------------------------------------------------------------
# Upload-record builders (mirror what main.py's _store_upload would have
# produced -- a real file on disk plus, for extraction-eligible types, the
# text main.py's _extract_text would have already computed).
# --------------------------------------------------------------------------

_SCRATCH = Path(tempfile.mkdtemp(prefix="file_capability_sources_"))


def _record(filename: str, content: bytes, extracted_text: str | None = None) -> dict:
    file_id = uuid.uuid4().hex
    path = _SCRATCH / f"{file_id}_{filename}"
    path.write_bytes(content)
    record = {"file_id": file_id, "filename": filename, "path": str(path)}
    if extracted_text is not None:
        record["extracted_text"] = extracted_text
    return record


def _text_record(filename: str, text: str) -> dict:
    return _record(filename, text.encode("utf-8"), extracted_text=None)


def _csv_record(filename: str, rows: list[dict]) -> dict:
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else [])
    writer.writeheader()
    writer.writerows(rows)
    content = buf.getvalue().encode("utf-8")
    # Mirrors main.py's _extract_tabular: a full to_string() dump.
    extracted = pd.DataFrame(rows).to_string(index=False) if rows else ""
    return _record(filename, content, extracted_text=extracted)


def _xlsx_record(filename: str, sheets: dict[str, list[dict]]) -> dict:
    file_id = uuid.uuid4().hex
    path = _SCRATCH / f"{file_id}_{filename}"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=sheet_name, index=False)
    extracted = "\n\n".join(
        f"## Sheet: {name}\n\n{pd.DataFrame(rows).to_string(index=False)}"
        for name, rows in sheets.items()
    )
    record = {"file_id": file_id, "filename": filename, "path": str(path)}
    record["extracted_text"] = extracted
    return record


def _pdf_like_record(filename: str, extracted_text: str) -> dict:
    # A real PDF binary isn't needed here -- build_job_workspace trusts
    # whatever extracted_text the upload step already computed, it never
    # re-parses the source. Only the workspace/tool layer is under test.
    return _record(filename, b"%PDF-1.4 fake binary content, not parsed here", extracted_text)


def _empty_record(filename: str) -> dict:
    return _record(filename, b"", extracted_text=None)


def _unsupported_record(filename: str) -> dict:
    return _record(filename, b"\x89PNG\r\n\x1a\nfake binary payload", extracted_text=None)


def _build(records: list[dict]) -> tuple[dict, str, Path]:
    workspace_root = Path(tempfile.mkdtemp(prefix="file_capability_ws_"))
    job_id = uuid.uuid4().hex
    info, context = workspace.build_job_workspace(job_id, records, workspace_root)
    return info, context, Path(info["root"])


def _cleanup(root: Path) -> None:
    shutil.rmtree(root.parent, ignore_errors=True)


def _find(files: list[dict], predicate) -> dict:
    matches = [f for f in files if predicate(f)]
    assert len(matches) == 1, f"expected exactly one match, got {len(matches)}: {matches}"
    return matches[0]


# --------------------------------------------------------------------------
# 1. TXT + PDF + CSV
# --------------------------------------------------------------------------


def test_txt_pdf_csv() -> None:
    txt = _text_record("notes.txt", "line one\nline two\nsearchable-marker\n")
    pdf = _pdf_like_record("report.pdf", "Executive summary.\n\n" + ("Detail. " * 50))
    csv_rec = _csv_record("rows.csv", [{"id": i, "value": i * 2} for i in range(20)])
    info, context, root = _build([txt, pdf, csv_rec])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]

        txt_entry = _find(listing, lambda f: f["path"].endswith("notes.txt"))
        check("1: .txt classified as text", txt_entry["file_type"] == "text", txt_entry)

        pdf_raw_entry = _find(
            listing, lambda f: f["path"].endswith("report.pdf") and not f["path"].endswith(".extracted.txt")
        )
        check("1: raw .pdf classified as document", pdf_raw_entry["file_type"] == "document", pdf_raw_entry)
        check(
            "1: raw .pdf points at its extracted-text companion",
            pdf_raw_entry.get("extracted_text_path", "").endswith("report.pdf.extracted.txt"),
            pdf_raw_entry,
        )

        pdf_extracted_entry = _find(listing, lambda f: f["path"].endswith("report.pdf.extracted.txt"))
        check(
            "1: extracted .pdf companion classified as extracted_document_text",
            pdf_extracted_entry["file_type"] == "extracted_document_text",
            pdf_extracted_entry,
        )

        csv_entry = _find(listing, lambda f: f["path"].endswith("rows.csv"))
        check("1: .csv classified as table", csv_entry["file_type"] == "table", csv_entry)

        # Raw .pdf itself is not readable -- must fail gracefully, not crash.
        try:
            tools.read_text_file(pdf_raw_entry["path"])
            check("1: reading raw .pdf directly raises", False, "no exception raised")
        except WorkspaceSecurityError:
            check("1: reading raw .pdf directly raises", True)

        # But its extracted companion IS readable.
        page = tools.read_text_file(pdf_extracted_entry["path"])
        check("1: extracted .pdf text is readable", "Executive summary" in page, page[:80])

        # CSV should be readable via table tools.
        inspected = tools.inspect_table(csv_entry["path"])
        check(
            "1: inspect_table sees all 20 csv rows",
            inspected["sheets"][0]["row_count"] == 20,
            inspected,
        )

        # search_text still works across plain text/csv (unchanged tool).
        found = tools.search_text("searchable-marker")
        check("1: search_text finds the txt marker", found["match_count"] == 1, found)
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 2. XLSX + CSV
# --------------------------------------------------------------------------


def test_xlsx_and_csv() -> None:
    xlsx = _xlsx_record(
        "book.xlsx",
        {
            "Sheet1": [{"a": i, "b": f"row{i}"} for i in range(15)],
            "Sheet2": [{"x": i} for i in range(5)],
        },
    )
    csv_rec = _csv_record("data.csv", [{"k": i, "v": i * i} for i in range(12)])
    info, context, root = _build([xlsx, csv_rec])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]
        xlsx_entry = _find(listing, lambda f: f["path"].endswith("book.xlsx"))
        csv_entry = _find(listing, lambda f: f["path"].endswith("data.csv") and not f["path"].endswith(".extracted.txt"))

        check("2: xlsx classified as table", xlsx_entry["file_type"] == "table", xlsx_entry)
        check("2: csv classified as table", csv_entry["file_type"] == "table", csv_entry)

        # The initial workspace-context preview must steer to inspect_table,
        # not dump the flattened .extracted.txt content for either file.
        check(
            "2: context preview for xlsx recommends inspect_table, not a text dump",
            "inspect_table" in context and "row0" not in context,
            context[:4000],
        )

        inspected_xlsx = tools.inspect_table(xlsx_entry["path"])
        sheet_names = {s["sheet"] for s in inspected_xlsx["sheets"]}
        check("2: both xlsx sheets discovered", sheet_names == {"Sheet1", "Sheet2"}, sheet_names)

        rows_sheet2 = tools.read_table_rows(xlsx_entry["path"], sheet="Sheet2")
        check("2: read_table_rows honors sheet selection", rows_sheet2["total_rows"] == 5, rows_sheet2)

        # Raw .xlsx must NOT be readable via read_text_file (binary format).
        try:
            tools.read_text_file(xlsx_entry["path"])
            check("2: read_text_file on raw .xlsx raises", False, "no exception")
        except WorkspaceSecurityError:
            check("2: read_text_file on raw .xlsx raises", True)

        # But raw .csv still technically works via read_text_file (it's a
        # real text file too) -- table tools are recommended, not exclusive.
        page = tools.read_text_file(csv_entry["path"])
        check("2: raw .csv is still readable as plain text", "k,v" in page or "k" in page, page[:80])
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 3. Two large CSVs -- bounded traversal, never touches extracted text
# --------------------------------------------------------------------------


def test_two_large_csvs() -> None:
    # Sized to stay well under MAX_TABLE_ROWS_PER_RUN combined (see
    # test_file_subsystem_hardening.py for the dedicated test proving the
    # per-run row budget itself kicks in and halts a genuinely runaway
    # traversal) -- this test's own invariant is narrower: bounded
    # multi-page read_table_rows traversal works correctly across two
    # files sharing one ReadOnlyWorkspaceTools instance.
    big_a = _csv_record("big_a.csv", [{"id": i, "name": f"item-{i}"} for i in range(420)])
    big_b = _csv_record("big_b.csv", [{"id": i, "score": i * 3.14} for i in range(310)])
    info, context, root = _build([big_a, big_b])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]
        entry_a = _find(listing, lambda f: f["path"].endswith("big_a.csv") and not f["path"].endswith(".extracted.txt"))
        entry_b = _find(listing, lambda f: f["path"].endswith("big_b.csv") and not f["path"].endswith(".extracted.txt"))

        for label, entry, expected_rows in (("A", entry_a, 420), ("B", entry_b, 310)):
            offset = 0
            calls = 0
            seen_rows = 0
            max_calls = (expected_rows // workspace.MAX_TABLE_ROWS_PER_READ) + 3
            while calls < max_calls:
                page = tools.read_table_rows(entry["path"], offset=offset)
                calls += 1
                seen_rows += page["returned_rows"]
                check(
                    f"3: {label} page {calls} never exceeds MAX_TABLE_ROWS_PER_READ",
                    page["returned_rows"] <= workspace.MAX_TABLE_ROWS_PER_READ,
                    page["returned_rows"],
                )
                if not page["truncated"]:
                    break
                offset = page["next_offset"]
            check(f"3: {label} full table traversed via read_table_rows", seen_rows == expected_rows, seen_rows)
            check(
                f"3: {label} traversal used a bounded number of calls",
                calls <= max_calls,
                calls,
            )
            check(f"3: {label} traversal used more than one call", calls > 1, calls)
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 4. Empty file
# --------------------------------------------------------------------------


def test_empty_file() -> None:
    empty_txt = _empty_record("blank.txt")
    empty_csv = _empty_record("blank.csv")
    info, context, root = _build([empty_txt, empty_csv])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]

        txt_entry = _find(listing, lambda f: f["path"].endswith("blank.txt"))
        check("4: empty .txt flagged as empty in notes", any("empty" in n.lower() for n in txt_entry["notes"]), txt_entry)
        page = tools.read_text_file(txt_entry["path"])
        check("4: reading an empty text file returns empty string, not an error", page == "", repr(page))

        csv_entry = _find(listing, lambda f: f["path"].endswith("blank.csv"))
        check("4: empty .csv still classified as table", csv_entry["file_type"] == "table", csv_entry)
        try:
            tools.inspect_table(csv_entry["path"])
            check("4: inspect_table on empty csv fails gracefully", False, "did not raise")
        except ValueError as exc:
            check(
                "4: inspect_table on empty csv fails gracefully with a clear message",
                "could not parse" in str(exc).lower() or "do not retry" in str(exc).lower(),
                str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            check("4: inspect_table on empty csv fails gracefully (no raw crash)", False, repr(exc))
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 5. Unsupported extension
# --------------------------------------------------------------------------


def test_unsupported_extension() -> None:
    unsupported = _unsupported_record("photo.png")
    txt = _text_record("readme.txt", "hello")
    info, context, root = _build([unsupported, txt])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]
        png_entry = _find(listing, lambda f: f["path"].endswith("photo.png"))
        check("5: .png classified as unsupported", png_entry["file_type"] == "unsupported", png_entry)
        check(
            "5: unsupported note tells the agent not to retry",
            any("do not retry" in n.lower() for n in png_entry["notes"]),
            png_entry["notes"],
        )
        check("5: unsupported file has no supported operations", png_entry["supported_operations"] == [], png_entry)

        try:
            tools.read_text_file(png_entry["path"])
            check("5: read_text_file on .png raises", False, "no exception")
        except WorkspaceSecurityError:
            check("5: read_text_file on .png raises", True)

        try:
            tools.inspect_table(png_entry["path"])
            check("5: inspect_table on .png raises with a clear message", False, "no exception")
        except ValueError as exc:
            check(
                "5: inspect_table on .png raises with a clear message",
                "not a table file" in str(exc),
                str(exc),
            )

        # Repeated attempts stay equally clear -- no crash escalation, no
        # state corruption from a prior failed call.
        try:
            tools.read_text_file(png_entry["path"])
            check("5: second read_text_file attempt on .png still raises", False, "no exception")
        except WorkspaceSecurityError:
            check("5: second read_text_file attempt on .png still raises", True)

        # The rest of the workspace is unaffected by the unsupported file.
        page = tools.read_text_file(_find(listing, lambda f: f["path"].endswith("readme.txt"))["path"])
        check("5: unrelated readable file still works fine", page == "hello", repr(page))
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 6. Duplicate filenames across two different sources
# --------------------------------------------------------------------------


def test_duplicate_filenames() -> None:
    csv_a = _csv_record("data.csv", [{"id": i} for i in range(5)])
    csv_b = _csv_record("data.csv", [{"id": i} for i in range(5, 15)])
    info, context, root = _build([csv_a, csv_b])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]
        csv_entries = [
            f for f in listing if f["path"].endswith("data.csv") and not f["path"].endswith(".extracted.txt")
        ]
        check("6: both same-named sources kept as distinct paths", len(csv_entries) == 2, csv_entries)
        check(
            "6: duplicate-named sources have different workspace paths",
            csv_entries[0]["path"] != csv_entries[1]["path"],
            csv_entries,
        )

        # list_files sorts by workspace path, which is derived from each
        # source's random file_id -- not upload order -- so identify "A"
        # (5 rows) and "B" (10 rows) by content, not list position.
        rows_by_path = {
            entry["path"]: tools.read_table_rows(entry["path"])
            for entry in csv_entries
        }
        row_counts = sorted(r["total_rows"] for r in rows_by_path.values())
        check(
            "6: reading each source returns only that source's own rows",
            row_counts == [5, 10],
            row_counts,
        )
        path_a = next(p for p, r in rows_by_path.items() if r["total_rows"] == 5)
        path_b = next(p for p, r in rows_by_path.items() if r["total_rows"] == 10)

        # Re-reading source A with the same arguments must be flagged as a
        # duplicate; source B must NOT be flagged by A's read (proves the
        # repeat-tracking key includes the full path, not just the filename).
        repeat_a = tools.read_table_rows(path_a)
        check("6: repeating source A's read is flagged", "note" in repeat_a, repeat_a)
        fresh_b = tools.read_table_rows(path_b, limit=1)
        check(
            "6: a differently-shaped call on source B is not falsely flagged",
            "note" not in fresh_b,
            fresh_b,
        )
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 7. Large extracted document
# --------------------------------------------------------------------------


def test_large_extracted_document() -> None:
    total_len = 2 * workspace.MAX_TOOL_READ_CHARS + 7_000
    big_text = "".join(chr(ord("a") + (i % 26)) for i in range(total_len))
    pdf = _pdf_like_record("huge.pdf", big_text)
    info, context, root = _build([pdf])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]
        extracted_entry = _find(listing, lambda f: f["path"].endswith("huge.pdf.extracted.txt"))
        check(
            "7: large extracted document classified correctly, not as a table",
            extracted_entry["file_type"] == "extracted_document_text",
            extracted_entry,
        )

        offset = 0
        chunks = []
        calls = 0
        max_calls = (total_len // workspace.MAX_TOOL_READ_CHARS) + 3
        while calls < max_calls:
            page = tools.read_text_file(extracted_entry["path"], offset=offset)
            calls += 1
            if "call again with offset=" in page:
                body, _, footer = page.rpartition("\n[Read characters")
                chunks.append(body)
                offset = int(footer.split("offset=")[1].split(" ")[0].rstrip(".]"))
            else:
                body, _, _ = page.rpartition("\n[Read characters")
                chunks.append(body if body else page)
                break
        check(
            "7: large extracted document fully traversed across bounded calls",
            "".join(chunks) == big_text,
            f"reconstructed_len={len(''.join(chunks))} expected={total_len}",
        )
        check("7: traversal took more than one call", calls > 1, calls)
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# 8. 5-6 mixed files in one workspace
# --------------------------------------------------------------------------


def test_mixed_workspace() -> None:
    records = [
        _text_record("readme.md", "# Title\n\nSome content."),
        _pdf_like_record("brief.pdf", "Brief content. " * 30),
        _csv_record("metrics.csv", [{"day": i, "count": i * 7} for i in range(25)]),
        _xlsx_record("book.xlsx", {"Sheet1": [{"a": i} for i in range(8)]}),
        _empty_record("placeholder.log"),
        _unsupported_record("diagram.svgz"),
    ]
    info, context, root = _build(records)
    try:
        tools = ReadOnlyWorkspaceTools(root)
        listing = tools.list_files()["files"]
        # readme.md, brief.pdf + its .extracted.txt, metrics.csv + its
        # .extracted.txt, book.xlsx + its .extracted.txt, placeholder.log,
        # diagram.svgz = 9 filesystem entries (csv/xlsx still get a
        # companion written at extraction time; the capability layer just
        # steers tool usage away from it, it doesn't remove the file).
        check("8: all mixed files are enumerated in one bounded call", len(listing) == 9, len(listing))

        by_suffix = {}
        for entry in listing:
            by_suffix.setdefault(Path(entry["path"]).name, entry)

        check("8: markdown text file", by_suffix["readme.md"]["file_type"] == "text")
        check("8: raw pdf is a document", by_suffix["brief.pdf"]["file_type"] == "document")
        check(
            "8: extracted pdf companion is extracted_document_text",
            by_suffix["brief.pdf.extracted.txt"]["file_type"] == "extracted_document_text",
        )
        check("8: csv is a table", by_suffix["metrics.csv"]["file_type"] == "table")
        check("8: xlsx is a table", by_suffix["book.xlsx"]["file_type"] == "table")
        check("8: empty log flagged empty", any("empty" in n.lower() for n in by_suffix["placeholder.log"]["notes"]))
        check("8: unsupported extension flagged unsupported", by_suffix["diagram.svgz"]["file_type"] == "unsupported")

        # A representative operation on every readable file succeeds without
        # any tool call ever exceeding its bound.
        md_page = tools.read_text_file(by_suffix["readme.md"]["path"])
        check("8: text file reads fine", "Title" in md_page)

        pdf_page = tools.read_text_file(by_suffix["brief.pdf.extracted.txt"]["path"])
        check("8: extracted document reads fine", "Brief content" in pdf_page)

        csv_rows = tools.read_table_rows(by_suffix["metrics.csv"]["path"])
        check("8: csv table reads fine", csv_rows["total_rows"] == 25)

        xlsx_summary = tools.table_summary(by_suffix["book.xlsx"]["path"])
        check("8: xlsx summary computed fine", xlsx_summary["row_count"] == 8)

        # Unsupported/empty files never raise anything uncaught when
        # merely listed/inspected -- only an actual mismatched read raises,
        # and that raise is the documented graceful ValueError/security error.
        check(
            "8: workspace context built without error for the full mixed set",
            isinstance(context, str) and len(context) > 0,
        )
    finally:
        _cleanup(root)


# --------------------------------------------------------------------------
# Hard safety invariants (cross-cutting, not tied to one numbered scenario)
# --------------------------------------------------------------------------


def test_bounded_rows_and_matches() -> None:
    rows = [{"id": i, "note": f"n{i}"} for i in range(500)]
    csv_rec = _csv_record("wide.csv", rows)
    info, context, root = _build([csv_rec])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        entry = tools.list_files()["files"]
        path = _find(entry, lambda f: f["path"].endswith("wide.csv") and not f["path"].endswith(".extracted.txt"))["path"]

        page = tools.read_table_rows(path, limit=10_000)
        check(
            "invariant: read_table_rows clamps an oversized limit request",
            page["returned_rows"] == workspace.MAX_TABLE_ROWS_PER_READ,
            page["returned_rows"],
        )

        matches = tools.search_table(path, "n1", limit=10_000)
        check(
            "invariant: search_table clamps an oversized limit request",
            len(matches["matches"]) <= workspace.MAX_TABLE_SEARCH_MATCHES,
            len(matches["matches"]),
        )
    finally:
        _cleanup(root)


def test_no_arbitrary_filesystem_access() -> None:
    csv_rec = _csv_record("safe.csv", [{"id": 1}])
    info, context, root = _build([csv_rec])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        for attack_path in ("../../etc/passwd", "/etc/passwd", "..\\..\\windows\\win.ini"):
            for method, kwargs in (
                (tools.inspect_table, {}),
                (tools.read_table_rows, {}),
                (tools.search_table, {"query": "x"}),
                (tools.table_summary, {}),
                (tools.inspect_file, {}),
                (tools.read_text_file, {}),
            ):
                try:
                    method(attack_path, **kwargs)
                    check(
                        f"invariant: {method.__name__} rejects {attack_path!r}",
                        False,
                        "no exception raised",
                    )
                except WorkspaceSecurityError:
                    check(f"invariant: {method.__name__} rejects {attack_path!r}", True)
                except Exception as exc:  # noqa: BLE001
                    check(
                        f"invariant: {method.__name__} rejects {attack_path!r} via WorkspaceSecurityError",
                        False,
                        f"raised {type(exc).__name__} instead: {exc}",
                    )
    finally:
        _cleanup(root)


def test_no_source_mutation() -> None:
    original_rows = [{"id": i, "value": f"v{i}"} for i in range(30)]
    csv_rec = _csv_record("immutable.csv", original_rows)
    info, context, root = _build([csv_rec])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        path = _find(
            tools.list_files()["files"],
            lambda f: f["path"].endswith("immutable.csv") and not f["path"].endswith(".extracted.txt"),
        )["path"]
        raw_before = (root / path).read_bytes()

        tools.inspect_table(path)
        tools.read_table_rows(path, limit=5)
        tools.read_table_rows(path, offset=5, limit=5)
        tools.search_table(path, "v1")
        tools.table_summary(path)

        raw_after = (root / path).read_bytes()
        check("invariant: source bytes are byte-for-byte unchanged after tool use", raw_before == raw_after)

        reread = pd.read_csv(root / path)
        check("invariant: source row count is unchanged after tool use", len(reread) == len(original_rows), len(reread))
    finally:
        _cleanup(root)


def test_repeated_identical_reads_detected_across_tools() -> None:
    csv_rec = _csv_record("dup.csv", [{"id": i} for i in range(50)])
    info, context, root = _build([csv_rec])
    try:
        tools = ReadOnlyWorkspaceTools(root)
        path = _find(
            tools.list_files()["files"],
            lambda f: f["path"].endswith("dup.csv") and not f["path"].endswith(".extracted.txt"),
        )["path"]

        first = tools.read_table_rows(path, limit=10)
        second = tools.read_table_rows(path, limit=10)
        check("invariant: first read_table_rows call carries no repeat note", "note" not in first, first)
        check("invariant: second identical read_table_rows call is flagged", "note" in second, second)

        r1 = tools.search_table(path, "1")
        r2 = tools.search_table(path, "1")
        check(
            "invariant: repeated identical search_table returns the same matches (idempotent, still bounded)",
            r1["matches"] == r2["matches"],
        )
    finally:
        _cleanup(root)


def test_workforce_wiring_unaffected() -> None:
    """The new tools are additive to the existing workspace_tools seam --
    Workforce/routing/assignment code is untouched. Cheap smoke check that
    the tool list is still a plain list of bound callables usable exactly
    where camel_service.py wires workspace_tools in (no signature/shape
    change that would ripple into worker construction)."""
    csv_rec = _csv_record("x.csv", [{"a": 1}])
    info, context, root = _build([csv_rec])
    try:
        tools_list = ReadOnlyWorkspaceTools(root).as_tools()
        check("invariant: as_tools returns only callables", all(callable(t) for t in tools_list), tools_list)
        names = {t.__name__ for t in tools_list}
        check(
            "invariant: as_tools exposes both text and table tool families",
            {"list_files", "inspect_file", "read_text_file", "search_text"} <= names
            and {"inspect_table", "read_table_rows", "search_table", "table_summary"} <= names,
            names,
        )
    finally:
        _cleanup(root)


def main() -> None:
    try:
        test_txt_pdf_csv()
        test_xlsx_and_csv()
        test_two_large_csvs()
        test_empty_file()
        test_unsupported_extension()
        test_duplicate_filenames()
        test_large_extracted_document()
        test_mixed_workspace()
        test_bounded_rows_and_matches()
        test_no_arbitrary_filesystem_access()
        test_no_source_mutation()
        test_repeated_identical_reads_detected_across_tools()
        test_workforce_wiring_unaffected()
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All file-capability-layer checks passed")


if __name__ == "__main__":
    main()
