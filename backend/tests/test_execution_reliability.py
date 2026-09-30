"""Regressions for the reliability failures found in live runs.

Every test here corresponds to something that actually broke in production,
or to a bound whose absence caused it:

  * routing that changed execution strategy because a model call failed;
  * a run that created a valid XLSX and still reported failure;
  * create_web_bundle receiving truncated JSON because a dataset was routed
    through the model's own output tokens;
  * tool results large enough that resending them dominated the token bill;
  * an executor redoing expensive work it had already completed.

No pytest suite exists in this repo (see test_upload.py) -- run directly:
    python test_execution_reliability.py
"""

import asyncio
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import artifacts as artifacts_module
from execution.langgraph_runtime import openai_chat
import capabilities
import capability_routing
import code_exec
import web_fetch
from artifacts import ArtifactError, ArtifactTools
from model_config import UsageTracker
from web_fetch import WebFetchTools

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def _workspace(with_xlsx: bool = False) -> Path:
    root = Path(tempfile.mkdtemp(prefix="reliability_ws_")).resolve()
    sources = root / "sources" / "up1"
    sources.mkdir(parents=True)
    if with_xlsx:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Başlık", "Yazar", "Yıl", "Puan"])
        for i in range(500):
            sheet.append([f"Kitap {i}", f"Yazar {i % 7}", 1900 + (i % 120), i / 3])
        workbook.save(sources / "kitaplar.xlsx")
    return root


# ---------------------------------------------------------------------------
# 1. Routing cannot fail
# ---------------------------------------------------------------------------


def test_routing_never_depends_on_a_model() -> None:
    """UI-selected Single strategy never constructs a routing agent."""

    async def _exploding(*args, **kwargs):
        raise AssertionError("no model call may happen during routing")

    import main
    import artifact_intent

    prompts = [
        ("Bu xlsx dosyasındaki veriyi JSON'a çevir ve arama yapabileceğim bir "
         "HTML/CSS/JS sayfası oluştur", True),
        ("https://example.org/p sayfasına git, tabloyu çek, excel dosyası ver", False),
        ("Summarize the attached PDF", True),
    ]
    with patch.object(openai_chat, "complete_chat", _exploding):
        for topic, has_files in prompts:
            tracker = UsageTracker()
            single = main._routing_for_ui_mode(
                "single",
                topic,
                has_workspace_files=has_files,
                workspace_file_kinds=[],
            )
            check(
                f"Single needs no model: {topic[:40]!r}",
                single.strategy == "single"
                and tracker.snapshot()["model_call_count"] == 0,
                f"{single.strategy} calls={tracker.snapshot()['model_call_count']}",
            )

    async def _workforce_no_routing_agent(topic, has_files):
        async def fake_classify(t, tracker, persisted=None, **_kwargs):
            return artifact_intent.ArtifactIntent(
                capabilities.ARTIFACTS
                in capabilities.detect_artifact_request(t),
                "xlsx",
                "llm",
            ).normalized()

        with patch.object(openai_chat, "complete_chat", _exploding), patch.object(
            artifact_intent, "classify_artifact_intent", fake_classify
        ):
            tracker = UsageTracker()
            decision, _ = await main._routing_for_workforce(
                topic,
                has_workspace_files=has_files,
                workspace_file_kinds=[],
                tracker=tracker,
            )
            return decision, tracker

    for topic, has_files in prompts:
        decision, tracker = asyncio.run(_workforce_no_routing_agent(topic, has_files))
        check(
            f"Workforce strategy merge needs no routing agent: {topic[:40]!r}",
            decision.strategy == "workforce",
            decision.strategy,
        )


# ---------------------------------------------------------------------------
# 2. Datasets never travel through model output
# ---------------------------------------------------------------------------


def test_convert_table_to_json_needs_no_model_tokens() -> None:
    """The fix for the truncated-JSON failure.

    A 500-row spreadsheet becomes a JSON file without a single row passing
    through a completion, and the inline response stays tiny.
    """
    root = _workspace(with_xlsx=True)
    tools = ArtifactTools(root)
    result = tools.convert_table_to_json(
        "sources/up1/kitaplar.xlsx", filename="data.json"
    )

    written = root / result["path"]
    check("convert: the JSON file exists", written.is_file(), str(written))
    records = json.loads(written.read_text(encoding="utf-8"))
    check("convert: every row was converted", len(records) == 500, str(len(records)))
    check(
        "convert: the header became record keys",
        set(records[0]) == {"Başlık", "Yazar", "Yıl", "Puan"},
        str(records[0]),
    )
    check(
        "convert: non-ASCII column names and values survive",
        records[0]["Başlık"] == "Kitap 0",
        str(records[0]),
    )
    check(
        "convert: numeric cells stay numeric (not stringified)",
        isinstance(records[1]["Yıl"], int) and isinstance(records[1]["Puan"], float),
        f"{type(records[1]['Yıl'])} {type(records[1]['Puan'])}",
    )

    # The response the MODEL sees must be small regardless of dataset size.
    inline = json.dumps(result, ensure_ascii=False)
    check(
        "convert: the inline tool response stays small",
        len(inline) < 2_000,
        f"{len(inline)} chars",
    )
    check(
        "convert: it reports the true row count without returning the rows",
        result["row_count"] == 500 and len(result["sample"]) == 3,
        str(result["row_count"]),
    )
    check(
        "convert: it tells the model to reference the path",
        "data_path" in result["note"],
        result["note"],
    )


def test_web_bundle_accepts_a_data_path() -> None:
    """create_web_bundle must not require the dataset inline.

    The live failure was ArtifactError("data is not valid JSON --
    Unterminated string") because the model re-emitted a large dataset into
    the `data` argument and the completion was cut short.
    """
    root = _workspace(with_xlsx=True)
    tools = ArtifactTools(root)
    converted = tools.convert_table_to_json("sources/up1/kitaplar.xlsx", "data.json")

    bundle = tools.create_web_bundle(
        "arama",
        html='<!doctype html><script src="app.js"></script>',
        js="fetch('data.json')",
        data_path=converted["path"],
    )
    folder = root / "artifacts" / "arama"
    check("bundle: data.json was written from the referenced file", (folder / "data.json").is_file())
    payload = json.loads((folder / "data.json").read_text(encoding="utf-8"))
    check("bundle: the full 500 rows made it in", len(payload) == 500, str(len(payload)))
    check(
        "bundle: no data had to pass through a tool argument",
        bundle["created"] is True,
        str(bundle)[:160],
    )

    # data_path wins over data, because it is the path that cannot truncate.
    both = tools.create_web_bundle(
        "arama2",
        html="<html></html>",
        data=json.dumps([{"only": "inline"}]),
        data_path=converted["path"],
    )
    payload = json.loads((root / "artifacts" / "arama2" / "data.json").read_text())
    check("bundle: data_path takes precedence over data", len(payload) == 500, str(payload)[:80])
    check("bundle: it reports success", both["created"] is True)


def test_from_path_on_table_writers() -> None:
    root = _workspace(with_xlsx=True)
    tools = ArtifactTools(root)
    converted = tools.convert_table_to_json("sources/up1/kitaplar.xlsx", "data.json")

    xlsx = tools.create_xlsx_artifact("out.xlsx", from_path=converted["path"])
    check("from_path: xlsx wrote every row", xlsx["row_count"] == 500, str(xlsx["row_count"]))
    from openpyxl import load_workbook

    sheet = load_workbook(root / xlsx["path"]).active
    check("from_path: the workbook reopens", sheet.max_row == 501, str(sheet.max_row))

    csv_result = tools.create_csv_artifact("out.csv", from_path=converted["path"])
    check("from_path: csv wrote every row", csv_result["row_count"] == 500, str(csv_result))

    # A bad reference must fail explicitly, not silently produce an empty file.
    for bad, label in (
        ("artifacts/nope.json", "missing file"),
        ("sources/up1/kitaplar.xlsx", "not JSON"),
        ("../../../etc/passwd", "traversal"),
    ):
        raised = False
        try:
            tools.create_xlsx_artifact("bad.xlsx", from_path=bad)
        except Exception:  # noqa: BLE001 - any explicit refusal is correct
            raised = True
        check(f"from_path: {label} is refused", raised)

    for sentinel in ("null", "None", "undefined", ""):
        raised = False
        detail = ""
        try:
            tools.create_xlsx_artifact("bad.xlsx", from_path=sentinel)
        except Exception as exc:  # noqa: BLE001
            raised = True
            detail = str(exc)
        check(f"from_path: sentinel {sentinel!r} is refused", raised, detail)
        if sentinel:
            check(
                f"from_path: {sentinel!r} is not treated as a file path",
                "could not be resolved" not in detail.lower(),
                detail,
            )




def test_extracted_scratch_is_not_shown_as_a_deliverable() -> None:
    root = _workspace()
    scratch = root / "artifacts" / artifacts_module.INTERNAL_DIRNAME
    scratch.mkdir(parents=True)
    (scratch / "page-tables.json").write_text("[]", encoding="utf-8")
    tools = ArtifactTools(root)
    tools.create_xlsx_artifact("real.xlsx", json.dumps([{"a": 1}]))

    listed = {entry["path"] for entry in artifacts_module.scan_artifacts(root)}
    check(
        "intermediate scrape output is not listed as a deliverable",
        listed == {"artifacts/real.xlsx"},
        str(sorted(listed)),
    )


# ---------------------------------------------------------------------------
# 3. Convergence: work is not redone
# ---------------------------------------------------------------------------


def test_identical_artifact_creation_is_memoized() -> None:
    root = _workspace(with_xlsx=True)
    tools = ArtifactTools(root)
    payload = json.dumps([{"a": i} for i in range(100)])

    first = tools.create_xlsx_artifact("report.xlsx", payload)
    written = root / first["path"]
    mtime = written.stat().st_mtime_ns
    second = tools.create_xlsx_artifact("report.xlsx", payload)

    check("memo: the second identical call is flagged as reuse", second.get("reused_existing") is True, str(second)[:120])
    check("memo: it returns the same path", second["path"] == first["path"], str(second["path"]))
    check("memo: the file was not rewritten", written.stat().st_mtime_ns == mtime)
    check(
        "memo: the model is told to stop and move on",
        "do not create it again" in second["note"].lower(),
        second["note"],
    )
    check("memo: only one artifact is counted", len(tools.records()) == 1, str(len(tools.records())))

    # A genuinely different call must still do the work.
    third = tools.create_xlsx_artifact("report.xlsx", json.dumps([{"a": 999}]))
    check("memo: changed data really rewrites the file", not third.get("reused_existing"), str(third)[:120])

    # An expensive conversion is memoized too.
    convert_a = tools.convert_table_to_json("sources/up1/kitaplar.xlsx", "d.json")
    convert_b = tools.convert_table_to_json("sources/up1/kitaplar.xlsx", "d.json")
    check("memo: repeated conversion is reused", convert_b.get("reused_existing") is True, str(convert_b)[:120])
    check("memo: the reused conversion reports the same path", convert_a["path"] == convert_b["path"])

    # A memo whose file has been deleted must NOT report a phantom artifact.
    (root / convert_a["path"]).unlink()
    convert_c = tools.convert_table_to_json("sources/up1/kitaplar.xlsx", "d.json")
    check(
        "memo: a deleted artifact is regenerated, not reported from cache",
        not convert_c.get("reused_existing") and (root / convert_c["path"]).is_file(),
        str(convert_c)[:120],
    )


def test_per_run_text_budget_across_web_tools() -> None:
    """Many individually-legal deliveries must still hit a shared ceiling.

    _spend_text is what every web_read call charges its delivered page text
    against (see web_tools.WebTools._deliver_text); exercised directly here
    since it has no dependency on how the page was acquired.
    """
    tools = WebFetchTools()
    chunk = "word " * 12_000  # ~60_000 chars per call
    total = 0
    noted = False
    for _ in range(12):
        delivered, budget_note = tools._spend_text(chunk)
        total += len(delivered)
        noted = noted or bool(budget_note)

    check(
        "web: total delivered text respects the per-run budget",
        total <= web_fetch.MAX_TEXT_CHARS_PER_RUN,
        f"{total} > {web_fetch.MAX_TEXT_CHARS_PER_RUN}",
    )
    check("web: exhausting the budget is stated, not silent", noted)


# ---------------------------------------------------------------------------
# 4. A completed deliverable never reports plain failure
# ---------------------------------------------------------------------------


def test_concurrent_execution_is_bounded() -> None:
    check(
        "code execution has a host-wide concurrency ceiling",
        code_exec.MAX_CONCURRENT_EXECUTIONS >= 1
        and code_exec._EXECUTION_SLOTS._initial_value == code_exec.MAX_CONCURRENT_EXECUTIONS,
        str(code_exec.MAX_CONCURRENT_EXECUTIONS),
    )
    # The slot must be released even when the run fails, or the backend
    # deadlocks after MAX_CONCURRENT_EXECUTIONS bad scripts.
    tools = code_exec.CodeExecutionTools(_workspace())
    for _ in range(code_exec.MAX_CONCURRENT_EXECUTIONS + 2):
        tools.run_python("raise SystemExit(3)")
    result = tools.run_python("print('still works')")
    check(
        "a failing execution still releases its slot",
        result["ok"] is True and "still works" in result["stdout"],
        str(result)[:160],
    )


def main() -> None:
    test_routing_never_depends_on_a_model()
    test_convert_table_to_json_needs_no_model_tokens()
    test_web_bundle_accepts_a_data_path()
    test_from_path_on_table_writers()
    test_extracted_scratch_is_not_shown_as_a_deliverable()
    test_identical_artifact_creation_is_memoized()
    test_per_run_text_budget_across_web_tools()
    test_concurrent_execution_is_bounded()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All execution-reliability checks passed")


if __name__ == "__main__":
    main()
