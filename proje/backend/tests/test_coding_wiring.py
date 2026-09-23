"""The Coding Agent inside a real LangGraph run.

A scripted model writes the code; the code is executed for real by
code_exec in a temporary job workspace. What is asserted is the contract the
old text-only worker broke: the output the user sees is what the program
printed, the file it wrote is on disk, and nothing downstream rewrites it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import capabilities

pytest.importorskip("langgraph")

from execution.langgraph_runtime import workforce  # noqa: E402


class Callback:
    # No loop and no queue: _emit drops the UI events.
    _loop = None
    _queue = None


def _script(monkeypatch, replies: dict[str, list[str]]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}

    async def fake_complete_chat(messages, **kwargs):
        stage = kwargs.get("stage", "")
        seen.setdefault(stage, []).append(messages[-1]["content"])
        queue = replies.get(stage)
        if not queue:
            raise AssertionError(f"unexpected model call at stage {stage!r}")
        return queue.pop(0)

    monkeypatch.setattr(workforce, "complete_chat", fake_complete_chat)
    return seen


def _run(tmp_path: Path, request: str):
    root = tmp_path / "job"
    root.mkdir()
    return workforce.run_workforce_in_thread(
        Callback(),
        workforce.UsageTracker(),
        None,
        request,
        "job-code",
        lambda handle: None,
        workspace_root=str(root),
        job_capabilities=frozenset({capabilities.CODE_EXEC}),
        original_request=request,
    ), root


def test_the_code_really_runs_and_its_output_is_shown(tmp_path, monkeypatch) -> None:
    program = (
        "import pandas as pd\n"
        "df = pd.DataFrame({'bolge': ['A', 'B', 'A'], 'satis': [10, 20, 5]})\n"
        "ozet = df.groupby('bolge')['satis'].sum()\n"
        "ozet.to_csv('artifacts/ozet.csv')\n"
        "print(ozet.to_string())\n"
        "print('TOPLAM', int(df['satis'].sum()))\n"
    )
    seen = _script(
        monkeypatch,
        {
            "coding_write": [f"MODE: run\nPLAN: Bölge toplamlarını hesaplar.\n```python\n{program}```"],
            "coding_answer": ["Toplam satış 35; en yüksek bölge B."],
        },
    )
    result, root = _run(tmp_path, "bölge bazında satış toplamını hesapla ve csv ver")
    text = result.result

    assert result.state.value == "DONE"
    # The explanation was written from what the program actually printed.
    assert "TOPLAM 35" in seen["coding_answer"][0]
    assert text.startswith("Toplam satış 35; en yüksek bölge B.")
    assert "```python\nimport pandas as pd" in text
    assert "TOPLAM 35" in text
    assert "`ozet.csv`" in text
    assert (root / "artifacts" / "ozet.csv").read_text(encoding="utf-8").startswith("bolge,satis")
    # main skips its restating synthesis for this answer.
    assert result.verbatim is True


def test_a_crash_is_fixed_from_the_real_traceback(tmp_path, monkeypatch) -> None:
    seen = _script(
        monkeypatch,
        {
            "coding_write": ["MODE: run\nPLAN: x\n```python\nprint(1/0)\n```"],
            "coding_fix": ["MODE: run\nPLAN: Sıfıra bölme kaldırıldı.\n```python\nprint(1/2)\n```"],
            "coding_answer": ["Sonuç 0.5."],
        },
    )
    result, _ = _run(tmp_path, "bir hesap yap")
    assert "ZeroDivisionError" in seen["coding_fix"][0]
    assert "```text\n0.5\n```" in result.result
    assert "2 çalıştırma" in result.result


def test_the_network_is_off_inside_the_run(tmp_path, monkeypatch) -> None:
    program = "import socket\nsocket.create_connection(('example.com', 80))\n"
    _script(
        monkeypatch,
        {
            "coding_write": [f"MODE: run\nPLAN: x\n```python\n{program}```"],
            "coding_fix": ["MODE: write\nPLAN: Ağ gerekiyor.\n```python\nimport requests\n```"],
        },
    )
    result, _ = _run(tmp_path, "example.com'a bağlan")
    assert "burada çalıştırılamadı" in result.result


def test_urls_in_code_do_not_become_sources(tmp_path, monkeypatch) -> None:
    program = "# veri: https://data.example.org/satislar\nprint('ok')\n"
    _script(
        monkeypatch,
        {
            "coding_write": [f"MODE: run\nPLAN: x\n```python\n{program}```"],
            "coding_answer": ["Tamam."],
        },
    )
    result, _ = _run(tmp_path, "kod yaz ve çalıştır")
    assert "Kaynaklar" not in result.result
    assert "https://data.example.org/satislar" in result.result


def test_a_file_the_program_wrote_is_not_rewritten_by_document(tmp_path, monkeypatch) -> None:
    program = (
        "import openpyxl\n"
        "wb = openpyxl.Workbook()\n"
        "ws = wb.active\n"
        "ws.append(['ay', 'bakiye'])\n"
        "ws.append([1, 103000])\n"
        "wb.save('artifacts/faiz.xlsx')\n"
        "print('yazildi')\n"
    )
    # Any Document call would hit an unscripted stage and fail the run.
    _script(
        monkeypatch,
        {
            "coding_write": [f"MODE: run\nPLAN: Tablo.\n```python\n{program}```"],
            "coding_answer": ["Tablo faiz.xlsx dosyasında."],
        },
    )
    root = tmp_path / "job"
    root.mkdir()
    request = "aylık faiz tablosunu excel olarak ver"
    result = workforce.run_workforce_in_thread(
        Callback(),
        workforce.UsageTracker(),
        None,
        request,
        "job-xlsx",
        lambda handle: None,
        workspace_root=str(root),
        job_capabilities=frozenset({capabilities.CODE_EXEC, capabilities.ARTIFACTS}),
        original_request=request,
        wants_artifact=True,
        artifact_type="xlsx",
    )
    assert sorted(p.name for p in (root / "artifacts").iterdir()) == ["faiz.xlsx"]
    assert result.result.startswith("Tablo faiz.xlsx dosyasında.")
    assert result.verbatim is True


def test_document_still_runs_when_the_program_wrote_another_format(tmp_path) -> None:
    root = tmp_path / "job"
    (root / "artifacts").mkdir(parents=True)
    (root / "artifacts" / "veri.csv").write_text("a\n1\n", encoding="utf-8")
    assert not workforce._coding_delivered_requested_files(str(root), "pdf", "analiz edip pdf rapor ver")
    assert workforce._coding_delivered_requested_files(str(root), "csv", "csv ver")
    assert not workforce._coding_delivered_requested_files(str(tmp_path / "bos"), "csv", "csv ver")


def test_without_a_workspace_the_text_worker_remains(monkeypatch) -> None:
    source = inspect.getsource(workforce._build_graph)
    assert "elif role == ROLE_CODING and workspace_root:" in source


def test_present_answer_shields_code_fences() -> None:
    text = (
        "Açıklama.\n\n```python\n# https://a.example/x\nprint('cannot create file')\n```"
    )
    presented = workforce._present_workforce_answer(text)
    assert "print('cannot create file')" in presented
    assert "Kaynaklar" not in presented


def test_main_skips_synthesis_for_verbatim_results() -> None:
    import main

    source = inspect.getsource(main._run_job)
    assert 'getattr(result_task, "verbatim", False)' in source


def test_the_timing_panel_names_the_coding_steps() -> None:
    timing = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "TimingLog.jsx").read_text(encoding="utf-8")
    for key in ("llm:coding_write", "llm:coding_fix", "llm:coding_answer", "tool:run_python"):
        assert key in timing


def test_coding_is_an_activity_stage() -> None:
    from workforce.events import ActivityEvent

    assert ActivityEvent(task_id="t", stage="coding", detail="kod çalıştırılıyor").stage == "coding"
