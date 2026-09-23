"""Retrieval eval for the workspace document search.

Indexes a folder of documents into a throwaway index, asks each question in
``knowledge_eval/cases.json``, and checks whether the passage that answers
it (a file plus a snippet it must contain) is among the excerpts the
Knowledge Agent would hand to the brief -- and at which label, [K1] being
rank 1. No brief is written; this measures retrieval only.

Four modes:

* ``keyword``  -- the question itself, FTS5 trigram search, keyword order.
* ``rerank``   -- the same hits re-ordered by the BGE cross-encoder.
* ``planned``  -- always wait for the PLAN call's TR/EN query variants,
  then search and rerank.
* ``adaptive`` -- what a job does (``acquire_knowledge_evidence`` without
  the brief): the fast path, and PLAN only when it is not confident. The
  report says how often PLAN was needed.

``planned`` and ``adaptive`` need MODEL_* and cost up to one model call per
question; they are skipped when MODEL_BASE_URL is unset.

The question this answers is whether keyword search + reranking finds enough
to make semantic (embedding) search unnecessary -- see docs/knowledge.md.
The bundled documents are synthetic; point ``--docs`` at real ones (PDFs
go through Docling like an upload) with a matching ``--cases`` file for the
real answer.

    docker compose exec backend python -m reliability_harness.knowledge_eval
    ... knowledge_eval --modes keyword,rerank --docs /data/eval-docs --cases my.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

EVAL_DIR = Path(__file__).with_name("knowledge_eval")
DOCS_DIR = EVAL_DIR / "docs"
CASES_PATH = EVAL_DIR / "cases.json"
MODES = ("keyword", "rerank", "planned", "adaptive")
_MODEL_MODES = ("planned", "adaptive")


@dataclass
class CaseResult:
    case_id: str
    mode: str
    rank: Optional[int]  # 1-based excerpt label that holds the answer
    seconds: float
    planned: bool = False
    excerpts: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        for key in ("id", "question", "file", "contains"):
            if not str(case.get(key) or "").strip():
                raise ValueError(f"case {case.get('id')!r} has no {key}")
    return cases


def build_index(docs_dir: Path, work_dir: Path) -> list[tuple[str, str]]:
    """Index every file in ``docs_dir``; file_id is the file name."""
    import knowledge_index
    from document_ingestion import DOCLING_EXTENSIONS, parse_document

    knowledge_index.DB_PATH = work_dir / "knowledge.db"
    files: list[tuple[str, str]] = []
    for path in sorted(p for p in docs_dir.iterdir() if p.is_file()):
        record: dict[str, Any] = {"file_id": path.name, "filename": path.name, "kind": "file"}
        if path.suffix.lower() in DOCLING_EXTENSIONS:
            out = work_dir / f"{path.name}.docling"
            parse_document(path, out, display_name=path.name)
            record["docling_dir"] = str(out)
        else:
            record["extracted_text"] = path.read_text(encoding="utf-8", errors="replace")
        status = knowledge_index.index_upload(record)
        print(f"  {path.name}: {status.status}, {status.passage_count} passages")
        if status.status == knowledge_index.STATUS_INDEXED:
            files.append((path.name, path.name))
    return files


def answer_rank(case: dict[str, Any], excerpts: Sequence[Any]) -> Optional[int]:
    import knowledge_index

    needle = knowledge_index.fold(case["contains"])
    for number, excerpt in enumerate(excerpts, start=1):
        if excerpt.filename == case["file"] and needle in knowledge_index.fold(excerpt.text):
            return number
    return None


async def run_case(
    case: dict[str, Any],
    mode: str,
    files: list[tuple[str, str]],
    *,
    complete: Any = None,
) -> CaseResult:
    import knowledge_controller as kc
    import knowledge_index
    import reranker

    started = time.monotonic()
    question = case["question"]
    if mode == "adaptive":
        result = await kc.acquire_knowledge_evidence(
            question, files=files, complete=complete, write_brief=False
        )
        return CaseResult(
            case_id=case["id"],
            mode=mode,
            rank=answer_rank(case, result.excerpts),
            seconds=time.monotonic() - started,
            planned=result.planned,
            excerpts=[excerpt.source_line() for excerpt in result.excerpts],
            queries=list(result.queries),
        )
    queries = [question]
    if mode == "planned":
        plan = await kc.plan_queries(question, complete=complete)
        queries = plan.queries
    file_ids = [file_id for file_id, _ in files]
    hits = knowledge_index.search(file_ids, queries, limit=kc.CANDIDATES)
    score = reranker.score_batched if mode in ("rerank", "planned") else None
    chosen, _, _ = kc._rank(hits, question, queries, deep_research=False, score=score)
    expanded = knowledge_index.passages_around(chosen[: kc.EXPAND_TOP], 1)
    excerpts = kc.build_excerpts(chosen, expanded)
    return CaseResult(
        case_id=case["id"],
        mode=mode,
        rank=answer_rank(case, excerpts),
        seconds=time.monotonic() - started,
        planned=mode == "planned",
        excerpts=[excerpt.source_line() for excerpt in excerpts],
        queries=list(queries),
    )


def summarize(results: Sequence[CaseResult], cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    tags = {case["id"]: case.get("tags") or [] for case in cases}
    summary: dict[str, Any] = {}
    for mode in dict.fromkeys(result.mode for result in results):
        rows = [r for r in results if r.mode == mode]

        def metrics(subset: Sequence[CaseResult]) -> dict[str, Any]:
            n = len(subset) or 1
            return {
                "cases": len(subset),
                "hit_at_1": round(sum(r.rank == 1 for r in subset) / n, 3),
                "hit_at_3": round(sum(r.rank is not None and r.rank <= 3 for r in subset) / n, 3),
                "recall": round(sum(r.rank is not None for r in subset) / n, 3),
                "mrr": round(sum(1 / r.rank for r in subset if r.rank) / n, 3),
                "mean_seconds": round(sum(r.seconds for r in subset) / n, 2),
                "planned": round(sum(r.planned for r in subset) / n, 3),
            }

        by_tag = {
            tag: metrics([r for r in rows if tag in tags.get(r.case_id, [])])
            for tag in sorted({t for case in cases for t in case.get("tags") or []})
        }
        summary[mode] = {
            **metrics(rows),
            "by_tag": by_tag,
            "misses": [r.case_id for r in rows if r.rank is None],
        }
    return summary


async def _run_all(
    cases: list[dict[str, Any]], modes: list[str], files: list[tuple[str, str]]
) -> list[CaseResult]:
    complete = None
    if any(mode in _MODEL_MODES for mode in modes):
        # Built inside this loop: the model client belongs to it.
        from execution.langgraph_runtime.workforce import _make_complete
        from model_config import UsageTracker

        complete = await _make_complete(UsageTracker(), stage_prefix="knowledge")
    results: list[CaseResult] = []
    for mode in modes:
        for case in cases:
            result = await run_case(case, mode, files, complete=complete)
            marker = f"K{result.rank}" if result.rank else "MISS"
            print(f"  [{mode:7}] {marker:5} {case['id']}")
            results.append(result)
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--docs", type=Path, default=DOCS_DIR)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--modes", default="keyword,rerank,adaptive")
    parser.add_argument("--tag", default="", help="only cases with this tag")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        parser.error(f"unknown mode(s): {', '.join(unknown)}")
    if any(m in _MODEL_MODES for m in modes) and not os.getenv("MODEL_BASE_URL"):
        print("MODEL_BASE_URL is not set; skipping the planned and adaptive modes.")
        modes = [m for m in modes if m not in _MODEL_MODES]
    if any(m != "keyword" for m in modes):
        import reranker

        if not reranker.enabled() or not reranker.is_available():
            print("Reranker unavailable (RERANKER_ENABLED / weights); rerank modes skipped.")
            modes = [m for m in modes if m == "keyword"]

    cases = load_cases(args.cases)
    if args.tag:
        cases = [case for case in cases if args.tag in (case.get("tags") or [])]
    with tempfile.TemporaryDirectory(prefix="knowledge_eval_") as work:
        print(f"Indexing {args.docs} ...")
        files = build_index(args.docs, Path(work))
        results = asyncio.run(_run_all(cases, modes, files))

    summary = summarize(results, cases)
    print()
    for mode, row in summary.items():
        print(
            f"{mode:8} hit@1 {row['hit_at_1']:.2f}  hit@3 {row['hit_at_3']:.2f}  "
            f"recall {row['recall']:.2f}  MRR {row['mrr']:.2f}  "
            f"{row['mean_seconds']:.2f}s/q  planned {row['planned']:.0%}  "
            f"misses: {', '.join(row['misses']) or '-'}"
        )
        for tag, metrics in row["by_tag"].items():
            print(f"         {tag:14} recall {metrics['recall']:.2f}  hit@1 {metrics['hit_at_1']:.2f}  (n={metrics['cases']})")
    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {"summary": summary, "results": [r.__dict__ for r in results]},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
