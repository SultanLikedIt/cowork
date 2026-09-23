"""Keyword index over uploaded documents, for the workspace document search.

Uploads reach a LangGraph worker only as the first workspace.MAX_CONTEXT_CHARS
of the prompt -- the workers have no file tools -- so anything past roughly
the fortieth page of a PDF is invisible to them. This module turns every
upload into short passages that can be searched instead of pasted.

Layering:

    knowledge_index      -- passages, the SQLite FTS5 store, keyword search
    knowledge_controller -- query planning, reranking, notes for a worker
    db.workspace_documents -- which files belong to which workspace

The index is keyed by file_id only and knows nothing about workspaces:
membership is user data and lives in app.db, while everything here is
derived from files already on disk. Deleting ``knowledge.db`` loses nothing;
the startup backfill in main.py rebuilds it.

Search is lexical on purpose. FTS5's trigram tokenizer matches inside words,
which covers Turkish suffixes ("sözleşme" finds "sözleşmesinde"), and
``fold`` erases the difference between ı and i on both sides, which also
cancels PaddleOCR's habit of reading ı as i. Meaning-level matching is the
cross-encoder's job in knowledge_controller, run over what this finds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

_logger = logging.getLogger("agent_webapp.knowledge")

DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)
# Module-level so tests can point it at a temporary file.
DB_PATH = DATA_DIR / "knowledge.db"

# Bump when passage building changes; the startup backfill re-indexes every
# file stored under an older version.
SCHEMA_VERSION = 1

# The reranker scores (query, passage) pairs and costs about 0.35 s per pair
# at 240 tokens on this CPU, so passages are kept near 200 tokens. Measured
# on the uploads in this deployment: 3.2 characters per XLM-R token.
MAX_PASSAGE_CHARS = 640
PASSAGE_OVERLAP_CHARS = 80
MAX_PASSAGES_PER_FILE = 20_000

# Trigram matching needs at least three characters per term, and a long
# inflected query word must not be longer than the base form in the text:
# "sözleşmesinde" (query) would never match "sözleşme" (text). Cutting query
# terms to a prefix keeps the stem and drops most suffixes.
MIN_TERM_CHARS = 3
MAX_TERM_CHARS = 7
MAX_TERMS_PER_QUERY = 12
# Reciprocal rank fusion constant, the usual value from the RRF paper.
RRF_K = 60

STATUS_INDEXED = "indexed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# Words that appear in questions about documents but say nothing about which
# passage answers them. Folded spelling, three or more characters only
# (shorter terms are dropped anyway).
_STOPWORDS = frozenset(
    {
        # Turkish
        "ve", "ile", "icin", "bir", "bu", "su", "gibi", "olan", "olarak",
        "daha", "cok", "veya", "ama", "kadar", "sonra", "once", "var", "yok",
        "midir", "nedir", "neler", "nelerdir", "nasil", "hangi", "hangisi",
        "nerede", "neden", "niye", "kac", "mi", "mu", "ne", "icinde",
        "belge", "belgede", "belgeler", "belgelerde", "belgelerim",
        "dosya", "dosyada", "dosyalar", "dosyalarda", "dosyalarim",
        "yukledigim", "yuklenen", "yukledigimiz", "gore", "hakkinda",
        "yazan", "yaziyor", "diyor", "denmis", "geciyor", "soyle", "bana",
        "benim", "bizim", "lutfen",
        # English
        "the", "and", "for", "with", "what", "which", "how", "does", "did",
        "this", "that", "these", "those", "from", "are", "was", "were",
        "document", "documents", "file", "files", "uploaded", "about",
        "into", "there", "their", "have", "has", "say", "says", "tell",
        "please", "can", "you", "any", "where", "when", "who", "why",
    }
)

_TR_FOLD = str.maketrans({"ı": "i", "İ": "i", "I": "i"})
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?…:;])\s+|\n+")
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_BLANK_LINES_RE = re.compile(r"\n\s*\n")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    passage_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    indexed_at REAL NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
    passage_id INTEGER PRIMARY KEY,
    file_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    page_from INTEGER,
    page_to INTEGER,
    text TEXT NOT NULL,
    UNIQUE (file_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_passages_file ON passages(file_id, ordinal);
-- rowid = passages.passage_id. Holds the folded heading + text; the
-- original text stays in passages for display and reranking.
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    body, tokenize = 'trigram'
);
"""

_write_lock = threading.Lock()
_initialized: set[str] = set()
_init_lock = threading.Lock()


# --------------------------------------------------------------------------
# Text folding and passages
# --------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lowercase, ı/İ -> i, accents stripped: ş->s, ğ->g, ç->c, ö->o, ü->u.

    Applied to the index and to every query, so the two only ever meet in
    this one spelling. Losing the ı/i distinction costs almost nothing for
    search and fixes OCR output that confuses them.
    """
    decomposed = unicodedata.normalize("NFKD", str(text or "").translate(_TR_FOLD))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


@dataclass(frozen=True)
class Passage:
    ordinal: int
    heading: str
    page_from: Optional[int]
    page_to: Optional[int]
    text: str


def _pages(meta: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for item in meta.get("doc_items") or []:
        if not isinstance(item, dict):
            continue
        for prov in item.get("prov") or []:
            if isinstance(prov, dict) and isinstance(prov.get("page_no"), int):
                pages.append(prov["page_no"])
    return pages


def _split_long(text: str) -> list[str]:
    """Pieces of at most MAX_PASSAGE_CHARS, cut at sentence ends when possible."""
    pieces: list[str] = []
    for sentence in _SENTENCE_BREAK_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= MAX_PASSAGE_CHARS:
            pieces.append(sentence)
            continue
        # One sentence longer than a passage (a table row dump, a run-on
        # clause): hard windows with overlap so a fact on the cut survives
        # whole in at least one of them.
        step = MAX_PASSAGE_CHARS - PASSAGE_OVERLAP_CHARS
        for start in range(0, len(sentence), step):
            pieces.append(sentence[start : start + MAX_PASSAGE_CHARS])
            if start + MAX_PASSAGE_CHARS >= len(sentence):
                break

    windows: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > MAX_PASSAGE_CHARS:
            windows.append(current)
            current = piece
        else:
            current = f"{current} {piece}" if current else piece
    if current:
        windows.append(current)
    return windows


def _build_passages(units: Iterable[tuple[str, str, list[int]]]) -> list[Passage]:
    """Merge consecutive (heading, text, pages) units into passages.

    Docling's hierarchical chunks are one per paragraph, list item or table
    -- median 49 characters here -- which is too little text for either a
    keyword hit or the reranker to judge. Units under the same heading are
    joined until the passage would pass MAX_PASSAGE_CHARS.
    """
    passages: list[Passage] = []
    heading = ""
    parts: list[str] = []
    pages: list[int] = []
    size = 0

    def emit(text: str, page_list: list[int]) -> None:
        if len(passages) >= MAX_PASSAGES_PER_FILE:
            return
        passages.append(
            Passage(
                ordinal=len(passages),
                heading=heading,
                page_from=min(page_list) if page_list else None,
                page_to=max(page_list) if page_list else None,
                text=text,
            )
        )

    def flush() -> None:
        nonlocal parts, pages, size
        if parts:
            emit("\n".join(parts), pages)
        parts, pages, size = [], [], 0

    for unit_heading, text, unit_pages in units:
        text = (text or "").strip()
        if not text:
            continue
        if unit_heading != heading:
            flush()
            heading = unit_heading
        if len(text) > MAX_PASSAGE_CHARS:
            flush()
            for window in _split_long(text):
                emit(window, unit_pages)
            continue
        if parts and size + 1 + len(text) > MAX_PASSAGE_CHARS:
            flush()
        parts.append(text)
        pages.extend(unit_pages)
        size += len(text) + (1 if size else 0)
    flush()
    return passages


def passages_from_chunks(records: Iterable[dict[str, Any]]) -> list[Passage]:
    """Passages from Docling's chunks.jsonl records (see document_ingestion)."""

    def units() -> Iterable[tuple[str, str, list[int]]]:
        for record in records:
            meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
            headings = [str(h).strip() for h in (meta.get("headings") or []) if str(h).strip()]
            yield " > ".join(headings), str(record.get("text") or ""), _pages(meta)

    return _build_passages(units())


def passages_from_text(text: str) -> list[Passage]:
    """Passages from plain or Markdown text; ``#`` lines become headings."""

    def units() -> Iterable[tuple[str, str, list[int]]]:
        heading = ""
        for block in _BLANK_LINES_RE.split(str(text or "")):
            lines: list[str] = []
            for line in block.splitlines():
                match = _MARKDOWN_HEADING_RE.match(line)
                if match:
                    if lines:
                        yield heading, "\n".join(lines), []
                        lines = []
                    heading = match.group(2).strip()
                elif line.strip():
                    lines.append(line.rstrip())
            if lines:
                yield heading, "\n".join(lines), []

    return _build_passages(units())


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    key = str(path.resolve())
    if key not in _initialized:
        with _init_lock:
            if key not in _initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                _initialized.add(key)
    return conn


@dataclass(frozen=True)
class FileStatus:
    file_id: str
    filename: str
    status: str
    passage_count: int
    reason: Optional[str]
    indexed_at: float
    schema_version: int

    @property
    def current(self) -> bool:
        return self.schema_version >= SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "status": self.status,
            "passage_count": self.passage_count,
            "reason": self.reason,
            "indexed_at": self.indexed_at,
        }


def _write(
    file_id: str,
    filename: str,
    status: str,
    passages: Sequence[Passage],
    reason: Optional[str] = None,
) -> FileStatus:
    now = time.time()
    with _write_lock, _connect() as conn:
        old = [
            row["passage_id"]
            for row in conn.execute(
                "SELECT passage_id FROM passages WHERE file_id = ?", (file_id,)
            )
        ]
        if old:
            conn.executemany(
                "DELETE FROM passages_fts WHERE rowid = ?", [(pid,) for pid in old]
            )
            conn.execute("DELETE FROM passages WHERE file_id = ?", (file_id,))
        for passage in passages:
            cursor = conn.execute(
                """
                INSERT INTO passages (file_id, ordinal, heading, page_from, page_to, text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    passage.ordinal,
                    passage.heading,
                    passage.page_from,
                    passage.page_to,
                    passage.text,
                ),
            )
            conn.execute(
                "INSERT INTO passages_fts (rowid, body) VALUES (?, ?)",
                (cursor.lastrowid, fold(f"{passage.heading}\n{passage.text}")),
            )
        conn.execute(
            """
            INSERT INTO files (
                file_id, filename, status, passage_count, reason, indexed_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                filename = excluded.filename,
                status = excluded.status,
                passage_count = excluded.passage_count,
                reason = excluded.reason,
                indexed_at = excluded.indexed_at,
                schema_version = excluded.schema_version
            """,
            (file_id, filename, status, len(passages), reason, now, SCHEMA_VERSION),
        )
    return FileStatus(
        file_id, filename, status, len(passages), reason, now, SCHEMA_VERSION
    )


def _read_chunks(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


# Extensions indexed from their extracted text. CSV is left out on purpose:
# a spreadsheet question ("toplam satış") needs the rows, not the three
# passages that happen to contain the words.
_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".xml", ".rst"}
)


def index_upload(record: dict[str, Any]) -> FileStatus:
    """(Re)index one upload record as saved by main._store_upload.

    Never raises: a file that cannot be indexed is recorded as failed or
    skipped with the reason, and the rest of the library keeps working.
    """
    file_id = str(record.get("file_id") or "").strip()
    filename = str(record.get("filename") or file_id)
    if not file_id:
        raise ValueError("upload record has no file_id")
    try:
        if record.get("kind") == "zip":
            return _write(file_id, filename, STATUS_SKIPPED, [], "zip archives are not indexed")
        docling_dir = record.get("docling_dir")
        chunks_path = Path(str(docling_dir)) / "chunks.jsonl" if docling_dir else None
        if chunks_path is not None and chunks_path.is_file():
            passages = passages_from_chunks(_read_chunks(chunks_path))
        else:
            extension = Path(filename).suffix.lower()
            if extension not in _TEXT_EXTENSIONS:
                return _write(
                    file_id, filename, STATUS_SKIPPED, [],
                    f"{extension or 'this file type'} is not indexed",
                )
            passages = passages_from_text(str(record.get("extracted_text") or ""))
        if not passages:
            return _write(file_id, filename, STATUS_SKIPPED, [], "no readable text")
        return _write(file_id, filename, STATUS_INDEXED, passages)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
        _logger.warning("indexing %s (%s) failed", filename, file_id, exc_info=True)
        try:
            return _write(file_id, filename, STATUS_FAILED, [], f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            return FileStatus(file_id, filename, STATUS_FAILED, 0, str(exc), time.time(), 0)


def drop_files(file_ids: Iterable[str]) -> None:
    ids = [str(fid) for fid in file_ids if str(fid or "").strip()]
    if not ids:
        return
    with _write_lock, _connect() as conn:
        for file_id in ids:
            old = [
                (row["passage_id"],)
                for row in conn.execute(
                    "SELECT passage_id FROM passages WHERE file_id = ?", (file_id,)
                )
            ]
            conn.executemany("DELETE FROM passages_fts WHERE rowid = ?", old)
            conn.execute("DELETE FROM passages WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))


def file_statuses(file_ids: Iterable[str]) -> dict[str, FileStatus]:
    ids = list(dict.fromkeys(str(fid) for fid in file_ids if fid))
    if not ids:
        return {}
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM files WHERE file_id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    finally:
        conn.close()
    return {
        row["file_id"]: FileStatus(
            row["file_id"],
            row["filename"],
            row["status"],
            row["passage_count"],
            row["reason"],
            row["indexed_at"],
            row["schema_version"],
        )
        for row in rows
    }


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    passage_id: int
    file_id: str
    filename: str
    ordinal: int
    heading: str
    page_from: Optional[int]
    page_to: Optional[int]
    text: str
    score: float = 0.0


def query_terms(query: str) -> list[str]:
    """Folded, stemmed, deduplicated search terms of a query."""
    terms: list[str] = []
    for word in _WORD_RE.findall(fold(query)):
        if len(word) < MIN_TERM_CHARS or word in _STOPWORDS:
            continue
        term = word[:MAX_TERM_CHARS]
        if term not in terms:
            terms.append(term)
        if len(terms) >= MAX_TERMS_PER_QUERY:
            break
    return terms


def match_expression(query: str) -> Optional[str]:
    """FTS5 MATCH string: any term, each as a quoted substring."""
    terms = query_terms(query)
    if not terms:
        return None
    # \w+ never yields a double quote, so quoting needs no escaping.
    return " OR ".join(f'"{term}"' for term in terms)


def _hit(row: sqlite3.Row, score: float = 0.0) -> Hit:
    return Hit(
        passage_id=row["passage_id"],
        file_id=row["file_id"],
        filename=row["filename"],
        ordinal=row["ordinal"],
        heading=row["heading"],
        page_from=row["page_from"],
        page_to=row["page_to"],
        text=row["text"],
        score=score,
    )


_HIT_COLUMNS = """
    p.passage_id, p.file_id, COALESCE(f.filename, p.file_id) AS filename,
    p.ordinal, p.heading, p.page_from, p.page_to, p.text
"""


def search(
    file_ids: Sequence[str],
    queries: Sequence[str],
    *,
    limit: int = 40,
    per_query: int = 30,
) -> list[Hit]:
    """Best passages for any of ``queries``, only inside ``file_ids``.

    Each query is ranked by BM25 on its own; the lists are merged with
    reciprocal rank fusion, so a passage several query variants agree on
    rises above one that a single variant ranked first. ``score`` is that
    fused value, useful only for ordering.
    """
    ids = list(dict.fromkeys(str(fid) for fid in file_ids if fid))
    expressions = [expr for expr in (match_expression(q) for q in queries) if expr]
    if not ids or not expressions:
        return []
    placeholders = ",".join("?" * len(ids))
    fused: dict[int, float] = {}
    conn = _connect()
    try:
        for expression in dict.fromkeys(expressions):
            try:
                rows = conn.execute(
                    f"""
                    SELECT p.passage_id
                    FROM passages_fts
                    JOIN passages p ON p.passage_id = passages_fts.rowid
                    WHERE passages_fts MATCH ? AND p.file_id IN ({placeholders})
                    ORDER BY bm25(passages_fts)
                    LIMIT ?
                    """,
                    [expression, *ids, per_query],
                ).fetchall()
            except sqlite3.OperationalError:
                _logger.warning("FTS query failed: %r", expression, exc_info=True)
                continue
            for rank, row in enumerate(rows):
                pid = row["passage_id"]
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
        if not fused:
            return []
        best = sorted(fused, key=lambda pid: fused[pid], reverse=True)[:limit]
        rows = conn.execute(
            f"""
            SELECT {_HIT_COLUMNS}
            FROM passages p LEFT JOIN files f ON f.file_id = p.file_id
            WHERE p.passage_id IN ({','.join('?' * len(best))})
            """,
            best,
        ).fetchall()
    finally:
        conn.close()
    by_id = {row["passage_id"]: row for row in rows}
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for pid in best:
        row = by_id.get(pid)
        if row is None:
            continue
        # The same document uploaded twice (common: re-attached to a new
        # chat) would otherwise return every passage twice and spend the
        # reranker's budget scoring copies.
        key = (fold(row["heading"]), fold(row["text"]))
        if key in seen:
            continue
        seen.add(key)
        hits.append(_hit(row, fused[pid]))
    return hits


def passages_around(hits: Sequence[Hit], radius: int = 1) -> list[Hit]:
    """The hits plus their neighbours in the same file, in document order.

    A fact often sits one passage away from the words that found it (the
    heading matched, the number is in the next paragraph).
    """
    if not hits or radius < 0:
        return list(hits)
    wanted: dict[str, set[int]] = {}
    for hit in hits:
        span = range(hit.ordinal - radius, hit.ordinal + radius + 1)
        wanted.setdefault(hit.file_id, set()).update(o for o in span if o >= 0)
    conn = _connect()
    try:
        rows: list[sqlite3.Row] = []
        for file_id, ordinals in wanted.items():
            ordered = sorted(ordinals)
            rows.extend(
                conn.execute(
                    f"""
                    SELECT {_HIT_COLUMNS}
                    FROM passages p LEFT JOIN files f ON f.file_id = p.file_id
                    WHERE p.file_id = ? AND p.ordinal IN ({','.join('?' * len(ordered))})
                    ORDER BY p.ordinal
                    """,
                    [file_id, *ordered],
                ).fetchall()
            )
    finally:
        conn.close()
    scores = {hit.passage_id: hit.score for hit in hits}
    return [_hit(row, scores.get(row["passage_id"], 0.0)) for row in rows]


# --------------------------------------------------------------------------
# Background indexing
# --------------------------------------------------------------------------


def enabled() -> bool:
    flag = os.getenv("KNOWLEDGE_ENABLED", "true").strip().casefold()
    return flag not in {"0", "false", "no", "off"}


# One worker: indexing is a burst of SQLite writes, and a second writer
# would only wait on _write_lock. It also keeps a large upload from
# competing with request handling for more than one core.
_executor: Optional[ThreadPoolExecutor] = None
_pending: dict[str, Future] = {}
_pending_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _pending_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge-index")
        return _executor


def submit(record: dict[str, Any]) -> Optional[Future]:
    """Index ``record`` in the background; one queued job per file at a time."""
    if not enabled():
        return None
    file_id = str(record.get("file_id") or "").strip()
    if not file_id:
        return None
    executor = _get_executor()
    with _pending_lock:
        existing = _pending.get(file_id)
        if existing is not None and not existing.done():
            return existing
        future = executor.submit(index_upload, dict(record))
        _pending[file_id] = future

    def _forget(done: Future, file_id: str = file_id) -> None:
        with _pending_lock:
            if _pending.get(file_id) is done:
                _pending.pop(file_id, None)

    future.add_done_callback(_forget)
    return future


def pending(file_ids: Iterable[str]) -> list[Future]:
    """Futures of files among ``file_ids`` still being indexed."""
    with _pending_lock:
        return [
            future
            for fid in file_ids
            if (future := _pending.get(str(fid))) is not None and not future.done()
        ]


def is_pending(file_id: str) -> bool:
    return bool(pending([file_id]))


__all__ = [
    "FileStatus",
    "Hit",
    "Passage",
    "SCHEMA_VERSION",
    "drop_files",
    "enabled",
    "file_statuses",
    "fold",
    "index_upload",
    "is_pending",
    "match_expression",
    "passages_around",
    "passages_from_chunks",
    "passages_from_text",
    "pending",
    "query_terms",
    "search",
    "submit",
]
