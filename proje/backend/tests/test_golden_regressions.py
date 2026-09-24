"""The golden regression runner: every prompt in the bank, plus two end-to-end
walkthroughs of the mentor cases.

Two layers, deliberately:

  1. **Capability sufficiency** for every entry in golden_prompts.BANK. Fast,
     no model, no network -- asserts the run WOULD be able to satisfy the
     complete request, and that cheap requests stay cheap.

  2. **End-to-end execution** for the two mentor cases. These drive the REAL
     tools -- the real web extractor against a fixture, the real code
     executor, the real artifact writers -- and then open what was produced.
     The point is that "the chain is capable" and "the chain works" are
     different claims, and only the second one would have caught these bugs.

Nothing here touches the live mentor URL, or any network. Fixtures are local.

No pytest suite exists in this repo (see test_upload.py) -- run directly:
    python test_golden_regressions.py
"""

import asyncio
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import artifacts as artifacts_module
import capabilities
import golden_prompts
import web_fetch
from artifacts import ArtifactTools
from capabilities import ARTIFACTS, CODE_EXEC, FILES, WEB_FETCH
from model_config import UsageTracker
from openpyxl import load_workbook

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


# ---------------------------------------------------------------------------
# Layer 1: capability sufficiency for the whole bank
# ---------------------------------------------------------------------------



def _route_golden(entry, declared=None, execution=None):
    """Merge declared capabilities with the structural floor. No model."""
    import capability_routing

    declared_caps = (
        list(declared)
        if declared is not None
        else ([FILES] if entry.has_files else [])
    )
    structural = capabilities.structural_requirements(
        entry.prompt, entry.has_files, list(entry.file_kinds)
    )
    wants_artifact = capabilities.ARTIFACTS in entry.requires
    structural = capabilities.apply_artifact_intent(
        structural,
        wants_artifact=wants_artifact,
        text=entry.prompt,
        has_workspace_files=entry.has_files,
        workspace_file_kinds=list(entry.file_kinds),
    )
    return capability_routing.merge_decision(
        execution or "direct",
        declared_caps,
        structural,
        profile=capability_routing.infer_profile(
            entry.prompt, entry.has_files, structural
        ),
        reason="golden",
        outcome=capability_routing.ROUTING_SKIPPED,
        raw_declared=declared_caps,
    )


def test_bank_structural_floor() -> None:
    """With a USELESS router, everything provable from the text still holds.

    This is the assertion that would have caught both mentor failures: in
    each, the router's answer was reasonable-looking and incomplete, and
    nothing downstream noticed.
    """
    for entry in golden_prompts.BANK:
        decision = _route_golden(entry)
        floor = golden_prompts.floor_for(entry)
        check(
            f"{entry.prompt_id}: floor holds even with an unhelpful router",
            decision.can_satisfy(floor),
            f"floor={sorted(floor)} got={sorted(decision.capabilities)}",
        )


def test_bank_capability_sufficiency() -> None:
    """With a COMPETENT router, the complete request is satisfiable."""
    for entry in golden_prompts.BANK:
        decision = _route_golden(
            entry,
            declared=entry.requires,
            execution="workforce" if entry.allow_workforce else "direct",
        )
        label = entry.prompt_id

        check(
            f"{label}: the chosen execution can satisfy the COMPLETE request",
            decision.can_satisfy(entry.requires),
            f"requires={sorted(entry.requires)} got={sorted(decision.capabilities)}",
        )
        overreach = entry.forbids & decision.capabilities
        check(
            f"{label}: no capability it does not need was attached",
            not overreach,
            f"unexpected={sorted(overreach)}",
        )
        if not entry.allow_workforce:
            check(
                f"{label}: stays on the single-executor path",
                decision.strategy == "direct",
                decision.strategy,
            )
        check(
            f"{label}: nothing unsupported was requested",
            not decision.unsupported,
            str(decision.unsupported),
        )
        if entry.expects_artifact:
            check(
                f"{label}: a workspace will be provisioned for the deliverable",
                capabilities.requires_workspace(decision.capabilities),
                sorted(decision.capabilities),
            )


def test_mentor_cases_are_not_incapable_contexts() -> None:
    """Named explicitly, because these are THE two regressions."""
    url_case = golden_prompts.by_id("mentor.url-to-excel")
    decision = _route_golden(url_case)
    check(
        "mentor.url-to-excel: not a Research-only (search-only) context",
        decision.capabilities != frozenset({capabilities.WEB_SEARCH})
        and WEB_FETCH in decision.capabilities
        and ARTIFACTS in decision.capabilities,
        sorted(decision.capabilities),
    )

    xlsx_case = golden_prompts.by_id("mentor.xlsx-to-searchable-web")
    decision = _route_golden(xlsx_case)
    check(
        "mentor.xlsx-to-searchable-web: not a Document-only (read-only) context",
        decision.capabilities != frozenset({FILES})
        and ARTIFACTS in decision.capabilities
        and CODE_EXEC in decision.capabilities,
        sorted(decision.capabilities),
    )


def test_bound_tools_actually_cover_the_requirements() -> None:
    """Capability sufficiency is worthless if binding does not follow it.

    Resolves each entry's capabilities into REAL tool callables and asserts
    the tools that satisfy each requirement are present by name -- the last
    link between "the decision was right" and "the executor can act on it".
    """
    expected_tools = {
        # The web surface is two explicit tools now (see web_tools.py).
        WEB_FETCH: {"web_search", "web_read"},
        ARTIFACTS: {"create_xlsx_artifact", "create_web_bundle", "create_json_artifact"},
        CODE_EXEC: {"execute_code", "execute_command"},
        FILES: {"inspect_table", "read_text_file"},
    }
    for entry in golden_prompts.BANK:
        if not entry.requires:
            continue
        decision = _route_golden(entry, declared=entry.requires)
        root = Path(tempfile.mkdtemp(prefix="golden_bind_")).resolve()
        bound = capabilities.resolve_tools(
            decision.capabilities, capabilities.ToolContext(workspace_root=str(root))
        )
        names = set(bound.names)
        for capability in entry.requires:
            needed = expected_tools.get(capability, set())
            check(
                f"{entry.prompt_id}: {capability} is bound as real tools",
                needed <= names,
                f"missing={sorted(needed - names)}",
            )
        check(
            f"{entry.prompt_id}: nothing was reported as unbindable",
            not bound.missing,
            str(bound.missing),
        )


# ---------------------------------------------------------------------------
# Layer 2: end-to-end, mentor case 1 -- URL -> table -> real XLSX
#
# Crawl4AI is the only network-talking engine (web_fetch.py's module
# docstring); these drive the real web_read tool against a fake acquisition
# engine instead of a fake HTTP session, the same pattern test_web_tools.py
# uses. Ported from a pre-Crawl4AI fixture (requests.Session + raw HTML)
# when that static-HTTP fetch tier was removed as dead code.
# ---------------------------------------------------------------------------

from html_extract import AcquiredPage, AcquisitionResult

PRAYER_TIMES_HEADER = ["Tarih", "İmsak", "Güneş", "Öğle", "İkindi", "Akşam", "Yatsı"]
PRAYER_TIMES_ROWS = [
    ["01 Ağustos 2026", "04:12", "05:45", "13:01", "16:53", "20:07", "21:32"],
    ["02 Ağustos 2026", "04:13", "05:46", "13:01", "16:53", "20:06", "21:31"],
    ["03 Ağustos 2026", "04:15", "05:47", "13:01", "16:52", "20:05", "21:29"],
]

FIXTURE_URL = "https://namazvakitleri.example/tr-TR/9206/ankara-icin-namaz-vakti"


class _FakeEngine:
    """Same role as web_tools.get_crawl4ai_engine's real return -- see
    test_web_tools.py, which this mirrors (kept self-contained per this
    repo's convention of not sharing fixtures across test files)."""

    def __init__(self, pages_by_url: dict[str, AcquiredPage]) -> None:
        self.pages_by_url = pages_by_url
        self.fetches: list[str] = []

    def acquire(self, url: str, need: str) -> AcquisitionResult:
        self.fetches.append(url)
        page = self.pages_by_url.get(url)
        if page is None:
            raise AssertionError(f"unexpected URL (no network in tests): {url}")
        return AcquisitionResult(pages=[page])


def _web_read_result(engine: _FakeEngine, root: str, url: str) -> dict:
    """Resolve web_read against a fake engine and call it, all inside one
    scoped patch -- get_crawl4ai_engine is looked up fresh at call time
    inside web_tools._read (see test_web_tools.py's _bound_tools), so the
    patch has to stay active through the actual call, not just through
    resolve_tools. Scoped (context manager, not a raw assignment) so it
    can't leak into a later test file's own patch.object(...) in the same
    pytest session -- patch.object restores whatever was live when it
    entered, so an unrestored raw assignment here would get "restored" to
    by a later test that never intended to depend on it."""
    import web_tools

    with patch.object(web_tools, "get_crawl4ai_engine", lambda: engine):
        context = capabilities.ToolContext(
            search_tool_factory=lambda: (lambda *a, **k: []),
            workspace_root=root,
        )
        bound = capabilities.resolve_tools({capabilities.WEB_SEARCH}, context)
        web_read = dict(zip(bound.names, bound.tools))["web_read"]
        return web_read(url=url)


# ---------------------------------------------------------------------------
# End-to-end: the FETCH -> EXTRACT -> ARTIFACT pipeline, at realistic data
# volumes, asserting on what reaches the MODEL. Search-then-navigate is the
# agent's own loop now (web_tools.py's module docstring), not something
# deterministic code does, so these fixtures are single-page.
# ---------------------------------------------------------------------------
#
# These pin the property the whole subsystem exists for: the number of rows
# must not change how much passes through the model. Every check below is
# either "the file on disk is complete and correct" or "the tool responses
# stayed small" -- the two halves of "orchestrate, don't transport".

SEARCH_DATA_URL = "https://agency.example/vakitler/2026/yillik"


def _year_rows(rows: int) -> list[list[str]]:
    """A single large table sharing prayer-times-shaped columns."""
    return [
        [f"{day:04d}.01.2026", f"04:{day % 60:02d}", f"05:{day % 60:02d}", "13:01"]
        for day in range(1, rows + 1)
    ]


def _model_visible(*payloads) -> int:
    """Characters a model would actually receive from these tool results."""
    return sum(len(json.dumps(p, ensure_ascii=False, default=str)) for p in payloads)


def test_e2e_large_dataset_stays_reference_based() -> None:
    """A large dataset must cost the model no more than a small one does.

    Sized under MAX_TABLE_ROWS so this measures the REFERENCE property
    cleanly; the extractor's own row cap is a separate concern, asserted in
    test_e2e_oversized_table_reports_incompleteness below.
    """
    page = AcquiredPage(
        url=SEARCH_DATA_URL,
        status=200,
        title="Yillik",
        text="yillik",
        tables=[[PRAYER_TIMES_HEADER[:4]] + _year_rows(1_800)],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        extracted = _web_read_result(engine, root, page.url)

        tools = ArtifactTools(Path(root).resolve())
        produced = tools.create_xlsx_artifact("big.xlsx", from_path=extracted["data_path"])

        sheet = load_workbook(Path(root).resolve() / produced["path"]).active
        check(
            "e2e large: every one of the 1,800 rows is in the file",
            sheet.max_row - 1 == 1_800,
            f"{sheet.max_row - 1} rows",
        )
        check(
            "e2e large: the row count is reported without sending the rows",
            extracted["row_count"] == 1_800
            and len(extracted["preview"]) <= web_fetch.MAX_INLINE_TABLE_ROWS,
            str(extracted["row_count"]),
        )
        check(
            "e2e large: nothing was silently dropped",
            "row_limit_reached" not in extracted,
            str(extracted.get("incomplete_warning")),
        )
        visible = _model_visible(extracted, produced)
        check(
            "e2e large: model-visible payload is bounded and small",
            visible < 6_000,
            f"{visible} chars",
        )

        # And the reference is honoured on disk: the file holds everything.
        saved = json.loads((Path(root).resolve() / extracted["data_path"]).read_text(encoding="utf-8"))
        check(
            "e2e large: the saved reference file holds the complete dataset",
            isinstance(saved, list) and len(saved) == 1_800,
            str(len(saved)) if isinstance(saved, list) else type(saved).__name__,
        )


def test_e2e_oversized_table_reports_incompleteness() -> None:
    """A table past the extractor's per-table row cap must SAY it was cut.

    Previously the cap dropped rows during parsing and row_count then
    reported the truncated figure, so a 5,000-row page looked like a
    complete 2,000-row one and the spreadsheet built from it was silently
    missing 60% of the data.
    """
    page = AcquiredPage(
        url=SEARCH_DATA_URL,
        status=200,
        title="Yillik",
        text="yillik",
        tables=[[PRAYER_TIMES_HEADER[:4]] + _year_rows(5_000)],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root:
        extracted = _web_read_result(engine, root, page.url)

        check(
            "cap: hitting the per-table row limit is reported, not hidden",
            extracted.get("row_limit_reached") is True,
            str(list(extracted)),
        )
        check(
            "cap: the warning tells the model to say the data is incomplete",
            "INCOMPLETE" in extracted.get("incomplete_warning", ""),
            str(extracted.get("incomplete_warning"))[:160],
        )
        check(
            "cap: the response is still bounded despite the huge page",
            _model_visible(extracted) < 6_000,
            str(_model_visible(extracted)),
        )


def test_extracted_intermediates_are_reference_only() -> None:
    """The exact invalid sequence from the live run: read_table_rows, then
    inspect_file, then read_text_file on the JSON web_read just wrote --
    which is how a year of rows entered the model's context."""
    from workspace import ReadOnlyWorkspaceTools

    page = AcquiredPage(
        url=SEARCH_DATA_URL,
        status=200,
        title="Yillik",
        text="yillik",
        tables=[[PRAYER_TIMES_HEADER[:4]] + _year_rows(360)],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root_str:
        root = Path(root_str).resolve()
        (root / "sources").mkdir(parents=True, exist_ok=True)
        extracted = _web_read_result(engine, root_str, page.url)
        reference = extracted["data_path"]

        files = ReadOnlyWorkspaceTools(root)

        text = files.read_text_file(reference)
        check(
            "guard: read_text_file returns a redirect, not the dataset",
            "0001.01.2026" not in text and "not readable here" in text,
            text[:160],
        )
        check(
            "guard: the redirect names the tool that consumes the path",
            "create_xlsx_artifact" in text and reference in text,
            text[:300],
        )
        check(
            "guard: the redirect is short",
            len(text) < 1_200,
            f"{len(text)} chars",
        )

        raised = ""
        try:
            files.read_table_rows(reference)
        except Exception as exc:  # noqa: BLE001 - any refusal is acceptable
            raised = str(exc)
        check(
            "guard: a table tool on extracted JSON is redirected, not just rejected",
            "create_xlsx_artifact" in raised,
            raised[:200],
        )
        check(
            "guard: and it does not leak rows either",
            "0001.01.2026" not in raised,
            raised[:200],
        )

        # A real deliverable under artifacts/ is NOT affected by the guard.
        (root / "artifacts").mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "notes.md").write_text("real deliverable", encoding="utf-8")
        check(
            "guard: ordinary artifact files are still readable",
            "real deliverable" in files.read_text_file("artifacts/notes.md"),
        )


def test_end_to_end_url_to_excel() -> None:
    """Walk the full chain a capable executor would walk, with real tools.

    Uses the REFERENCE handoff throughout -- web_read saves the full table
    and create_xlsx_artifact reads it back by path -- because that is the
    chain production now takes, and the one whose cost does not grow with
    the number of rows.
    """
    page = AcquiredPage(
        url=FIXTURE_URL,
        status=200,
        title="Ankara Icin Namaz Vakti",
        text="ankara",
        tables=[[PRAYER_TIMES_HEADER] + PRAYER_TIMES_ROWS],
    )
    engine = _FakeEngine({page.url: page})
    with tempfile.TemporaryDirectory() as root_str:
        root = Path(root_str).resolve()
        artifact_tools = ArtifactTools(root)

        # Step 1 -- retrieve the SPECIFIC page named in the request.
        extracted = _web_read_result(engine, root_str, FIXTURE_URL)

        check(
            "e2e url->xlsx: the named page's table was extracted",
            extracted["row_count"] == 3,
            str(extracted)[:200],
        )
        check(
            "e2e url->xlsx: the header survived extraction",
            extracted["columns"][:3] == ["Tarih", "İmsak", "Güneş"],
            str(extracted["columns"]),
        )
        check(
            "e2e url->xlsx: the full table was saved for reference handoff",
            "data_path" in extracted,
            str(extracted.keys()),
        )

        # Step 2 -- write the REAL spreadsheet from the SAVED FILE. No row is
        # retyped into a tool argument, which is what makes this chain safe
        # for a table of any size.
        result = artifact_tools.create_xlsx_artifact(
            "ankara-namaz-vakitleri-agustos.xlsx",
            from_path=extracted["data_path"],
            sheet_name="Ağustos 2026",
        )
        written = root / result["path"]

        check("e2e url->xlsx: the artifact file exists on disk", written.is_file(), str(written))
        check("e2e url->xlsx: it is a real xlsx container", zipfile.is_zipfile(written))

        sheet = load_workbook(written).active
        values = [[cell.value for cell in row] for row in sheet.iter_rows()]
        check(
            "e2e url->xlsx: the workbook reopens with the extracted header",
            values[0][:3] == ["Tarih", "İmsak", "Güneş"],
            str(values[0]),
        )
        check(
            "e2e url->xlsx: every scraped row reached the spreadsheet",
            len(values) == 4 and values[1][1] == "04:12" and values[3][1] == "04:15",
            str(values),
        )
        check(
            "e2e url->xlsx: Turkish characters survived the whole chain",
            values[0][1] == "İmsak" and "Ağustos" in str(values[1][0]),
            str(values[1]),
        )

        # Step 4 -- it is discoverable through the same mechanism the API uses.
        listed = artifacts_module.scan_artifacts(root)
        check(
            "e2e url->xlsx: the artifact is listed for download",
            any(entry["path"] == result["path"] for entry in listed),
            str(listed),
        )

        # Bounded: this whole chain cost a small, countable number of fetches.
        check(
            "e2e url->xlsx: the chain used exactly one page fetch",
            len(engine.fetches) == 1,
            str(engine.fetches),
        )
        check(
            "e2e url->xlsx: the intermediate scrape file is not a deliverable",
            all(
                artifacts_module.INTERNAL_DIRNAME not in entry["path"]
                for entry in listed
            ),
            str(listed),
        )

        # Idempotence: an executor that repeats the final step -- which is
        # exactly what the live Case B run did -- must not redo the work.
        repeat = artifact_tools.create_xlsx_artifact(
            "ankara-namaz-vakitleri-agustos.xlsx",
            from_path=extracted["data_path"],
            sheet_name="Ağustos 2026",
        )
        check(
            "e2e url->xlsx: repeating the artifact call reuses the existing file",
            repeat.get("reused_existing") is True and repeat["path"] == result["path"],
            str(repeat)[:140],
        )


# ---------------------------------------------------------------------------
# Layer 2: end-to-end, mentor case 2 -- XLSX -> JSON -> searchable web bundle
# ---------------------------------------------------------------------------


def _workspace_with_xlsx() -> tuple[Path, Path]:
    """A job workspace holding a real uploaded .xlsx under sources/."""
    from openpyxl import Workbook

    root = Path(tempfile.mkdtemp(prefix="golden_xlsx_web_")).resolve()
    sources = root / "sources" / "upload01"
    sources.mkdir(parents=True)
    upload = sources / "kitaplar.xlsx"

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Başlık", "Yazar", "Yıl"])
    sheet.append(["Kürk Mantolu Madonna", "Sabahattin Ali", 1943])
    sheet.append(["Tutunamayanlar", "Oğuz Atay", 1972])
    sheet.append(["Saatleri Ayarlama Enstitüsü", "A. H. Tanpınar", 1961])
    workbook.save(upload)
    return root, upload


def test_end_to_end_xlsx_to_searchable_web() -> None:
    root, upload = _workspace_with_xlsx()
    before = upload.read_bytes()
    artifact_tools = ArtifactTools(root)

    # Step 1 -- convert the upload DETERMINISTICALLY. This is the shortest
    # correct path and costs zero model tokens per row: the server reads the
    # workbook and writes the JSON. The live failure took the long way round
    # (read rows into context, re-emit them into a tool argument) and was
    # truncated mid-string.
    converted = artifact_tools.convert_table_to_json(
        "sources/upload01/kitaplar.xlsx", filename="data.json"
    )
    check(
        "e2e xlsx->web: the conversion succeeded",
        converted["created"] is True,
        str(converted)[:160],
    )
    check(
        "e2e xlsx->web: the whole table was converted in ONE call",
        converted["row_count"] == 3,
        str(converted["row_count"]),
    )
    check(
        "e2e xlsx->web: the inline response does not carry the dataset",
        len(json.dumps(converted, ensure_ascii=False)) < 2_000,
        str(len(json.dumps(converted))),
    )

    produced = json.loads((root / "artifacts" / "data.json").read_text(encoding="utf-8"))
    check(
        "e2e xlsx->web: the generated JSON is valid and complete",
        len(produced) == 3 and produced[0]["Yazar"] == "Sabahattin Ali",
        str(produced)[:160],
    )

    # Step 2 -- build the searchable page as a real bundle.
    bundle = artifact_tools.create_web_bundle(
        "kitap-arama",
        html=(
            '<!doctype html><html lang="tr"><head><meta charset="utf-8">'
            '<title>Kitap Arama</title><link rel="stylesheet" href="styles.css">'
            '</head><body><input id="q" placeholder="Ara..."><ul id="results"></ul>'
            '<script src="app.js"></script></body></html>'
        ),
        css="body{font-family:system-ui;margin:2rem} #q{padding:.5rem;width:20rem}",
        js=(
            "let rows=[];fetch('data.json').then(r=>r.json()).then(d=>{rows=d;render(d)});"
            "document.getElementById('q').addEventListener('input',e=>{"
            "const q=e.target.value.toLowerCase();"
            "render(rows.filter(r=>JSON.stringify(r).toLowerCase().includes(q)))});"
            "function render(list){document.getElementById('results').innerHTML="
            "list.map(r=>`<li>${r['Başlık']} — ${r['Yazar']} (${r['Yıl']})</li>`).join('')}"
        ),
        # By REFERENCE. The live failure passed this dataset inline and the
        # argument was cut off mid-string ("Unterminated string").
        data_path=converted["path"],
    )

    folder = root / "artifacts" / "kitap-arama"
    for member in ("index.html", "styles.css", "app.js", "data.json"):
        check(f"e2e xlsx->web: {member} exists on disk", (folder / member).is_file(), str(folder / member))

    check(
        "e2e xlsx->web: the bundle names index.html as the entry point",
        bundle["entry_point"].endswith("kitap-arama/index.html"),
        bundle["entry_point"],
    )
    check(
        "e2e xlsx->web: the page loads its data by relative path",
        "fetch('data.json')" in (folder / "app.js").read_text(encoding="utf-8"),
    )
    check(
        "e2e xlsx->web: the page references its stylesheet and script",
        'href="styles.css"' in (folder / "index.html").read_text(encoding="utf-8")
        and 'src="app.js"' in (folder / "index.html").read_text(encoding="utf-8"),
    )
    bundle_data = json.loads((folder / "data.json").read_text(encoding="utf-8"))
    check(
        "e2e xlsx->web: the bundle's data.json is valid and matches the source",
        bundle_data == produced,
        str(bundle_data)[:160],
    )
    check(
        "e2e xlsx->web: Turkish characters survived the whole chain",
        bundle_data[1]["Yazar"] == "Oğuz Atay",
        str(bundle_data[1]),
    )

    # The invariant that matters most: the upload is untouched.
    check(
        "e2e xlsx->web: the uploaded source file is byte-identical",
        upload.read_bytes() == before,
    )
    check(
        "e2e xlsx->web: everything produced lives under artifacts/",
        all(
            entry["path"].startswith("artifacts/")
            for entry in artifacts_module.scan_artifacts(root)
        ),
        str(artifacts_module.scan_artifacts(root)),
    )

    # And it stayed cheap: the whole chain was two deterministic tool calls,
    # and no row ever entered a model response in either direction.
    # A bundle is one deliverable, not four unrelated ones: scan_artifacts
    # groups its members via the manifest create_web_bundle writes, so the
    # top level shows the standalone data.json plus ONE entry for the app,
    # not each of its four files individually.
    scanned = artifacts_module.scan_artifacts(root)
    bundle_entry = next(
        (e for e in scanned if e.get("bundle") == "artifacts/kitap-arama"), None
    )
    check(
        "e2e xlsx->web: the deliverables are exactly what was asked for",
        {entry["path"] for entry in scanned} == {"artifacts/data.json", "artifacts/kitap-arama/index.html"}
        and bundle_entry is not None
        and bundle_entry.get("entry_point") == "artifacts/kitap-arama/index.html"
        and {member["path"] for member in bundle_entry.get("files", [])}
        == {
            "artifacts/kitap-arama/index.html",
            "artifacts/kitap-arama/styles.css",
            "artifacts/kitap-arama/app.js",
            "artifacts/kitap-arama/data.json",
        },
        str(sorted(entry["path"] for entry in scanned)),
    )
    repeat = artifact_tools.convert_table_to_json(
        "sources/upload01/kitaplar.xlsx", filename="data.json"
    )
    check(
        "e2e xlsx->web: repeating the conversion reuses the existing file",
        repeat.get("reused_existing") is True,
        str(repeat)[:140],
    )


def test_no_runaway_on_the_xlsx_case() -> None:
    """The 430K-token failure, stated as a bound rather than a token count.

    A capable executor converts a spreadsheet in one call. The failure mode
    was an incapable one paging rows forever, so this asserts the budgets that
    make that impossible: both expensive tools have per-run caps.
    """
    import code_exec

    check(
        "runaway: code execution has a per-run ceiling",
        code_exec.MAX_EXECUTIONS_PER_RUN <= 20,
        str(code_exec.MAX_EXECUTIONS_PER_RUN),
    )
    import web_tools

    check(
        "runaway: web reading has a per-job ceiling",
        web_tools.MAX_WEB_READS_PER_JOB <= 50,
        str(web_tools.MAX_WEB_READS_PER_JOB),
    )
    check(
        "runaway: artifact creation has a per-run ceiling",
        artifacts_module.MAX_ARTIFACTS_PER_JOB <= 50,
        str(artifacts_module.MAX_ARTIFACTS_PER_JOB),
    )


def main() -> None:
    print(f"--- golden bank: {len(golden_prompts.BANK)} prompts ---")
    test_bank_structural_floor()
    test_bank_capability_sufficiency()
    test_mentor_cases_are_not_incapable_contexts()
    test_bound_tools_actually_cover_the_requirements()
    print("--- end-to-end: web pipeline (fetch -> extract -> artifact) ---")
    test_e2e_large_dataset_stays_reference_based()
    test_e2e_oversized_table_reports_incompleteness()
    test_extracted_intermediates_are_reference_only()
    print("--- end-to-end: mentor case 1 (url -> excel) ---")
    test_end_to_end_url_to_excel()
    print("--- end-to-end: mentor case 2 (xlsx -> searchable web) ---")
    test_end_to_end_xlsx_to_searchable_web()
    print("--- bounding ---")
    test_no_runaway_on_the_xlsx_case()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All golden regressions passed")


if __name__ == "__main__":
    main()
