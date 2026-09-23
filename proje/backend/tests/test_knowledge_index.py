"""Workspace document index: passages, Turkish folding, keyword search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import knowledge_index as ki


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ki, "DB_PATH", tmp_path / "knowledge.db")


def _chunk(text: str, headings: list[str] | None = None, pages: list[int] | None = None) -> dict:
    return {
        "text": text,
        "meta": {
            "headings": headings or [],
            "doc_items": [{"prov": [{"page_no": page}]} for page in (pages or [])],
        },
    }


def _docling_upload(tmp_path: Path, file_id: str, filename: str, chunks: list[dict]) -> dict:
    folder = tmp_path / f"{file_id}.docling"
    folder.mkdir()
    (folder / "chunks.jsonl").write_text(
        "\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks),
        encoding="utf-8",
    )
    return {"file_id": file_id, "filename": filename, "kind": "file", "docling_dir": str(folder)}


def _text_upload(file_id: str, filename: str, text: str) -> dict:
    return {"file_id": file_id, "filename": filename, "kind": "file", "extracted_text": text}


# -- folding ---------------------------------------------------------------


def test_fold_erases_turkish_letters_and_the_dotless_i() -> None:
    assert ki.fold("SÖZLEŞME Işık İğne ÇAĞRI") == "sozlesme isik igne cagri"


def test_fold_makes_ocr_misreads_of_dotless_i_equal() -> None:
    # PaddleOCR reads "ı" as "i"; both spellings must index identically.
    assert ki.fold("kısıtlama") == ki.fold("kisitlama")


# -- passages --------------------------------------------------------------


def test_small_chunks_under_one_heading_are_merged() -> None:
    passages = ki.passages_from_chunks(
        [
            _chunk("Madde 1.", ["Kira Sözleşmesi"], [1]),
            _chunk("Taraflar aşağıda belirtilmiştir.", ["Kira Sözleşmesi"], [1]),
            _chunk("Kiracı: Ali Veli", ["Kira Sözleşmesi"], [2]),
        ]
    )
    assert len(passages) == 1
    assert passages[0].heading == "Kira Sözleşmesi"
    assert (passages[0].page_from, passages[0].page_to) == (1, 2)
    assert "Taraflar" in passages[0].text and "Kiracı" in passages[0].text


def test_a_new_heading_starts_a_new_passage() -> None:
    passages = ki.passages_from_chunks(
        [
            _chunk("Birinci bölüm metni.", ["Giriş"], [1]),
            _chunk("İkinci bölüm metni.", ["Fesih"], [3]),
        ]
    )
    assert [p.heading for p in passages] == ["Giriş", "Fesih"]
    assert [p.ordinal for p in passages] == [0, 1]
    assert passages[1].page_from == 3


def test_nested_headings_are_joined() -> None:
    (passage,) = ki.passages_from_chunks([_chunk("metin", ["Rapor", "Bulgular"])])
    assert passage.heading == "Rapor > Bulgular"


def test_passages_never_exceed_the_size_limit() -> None:
    sentence = "Kiracı otuz gün önceden yazılı bildirimde bulunmalıdır. "
    passages = ki.passages_from_chunks(
        [_chunk(sentence * 40, ["Fesih"], [4])]
        + [_chunk(f"Kısa madde {i}.", ["Fesih"], [5]) for i in range(80)]
    )
    assert len(passages) > 3
    assert all(len(p.text) <= ki.MAX_PASSAGE_CHARS for p in passages)
    assert all(p.heading == "Fesih" for p in passages)


def test_an_unbroken_run_is_cut_into_overlapping_windows() -> None:
    blob = "x" * (ki.MAX_PASSAGE_CHARS * 3)
    passages = ki.passages_from_chunks([_chunk(blob)])
    assert len(passages) >= 3
    assert all(len(p.text) <= ki.MAX_PASSAGE_CHARS for p in passages)
    assert sum(len(p.text) for p in passages) >= len(blob)


def test_empty_chunks_produce_nothing() -> None:
    assert ki.passages_from_chunks([_chunk(""), _chunk("   ")]) == []


def test_markdown_headings_become_passage_headings() -> None:
    passages = ki.passages_from_text(
        "# Toplantı Notları\n\nBütçe onaylandı.\n\n## Aksiyonlar\n\nAyşe raporu yazacak."
    )
    assert [p.heading for p in passages] == ["Toplantı Notları", "Aksiyonlar"]
    assert passages[1].text == "Ayşe raporu yazacak."
    assert passages[0].page_from is None


# -- query terms -----------------------------------------------------------


def test_query_terms_drop_stopwords_and_short_words() -> None:
    assert ki.query_terms("Yüklediğim belgede fesih süresi ne?") == ["fesih", "suresi"]


def test_long_inflected_words_are_cut_to_a_stem() -> None:
    # The query form is longer than the text form; the stem is not.
    assert ki.query_terms("sözleşmesinde") == ["sozlesm"]


def test_a_query_of_only_stopwords_has_no_expression() -> None:
    assert ki.match_expression("bu ne?") is None


# -- index and search ------------------------------------------------------


def test_turkish_suffixes_match_across_query_and_text(tmp_path: Path) -> None:
    record = _docling_upload(
        tmp_path,
        "f1",
        "kira.pdf",
        [
            _chunk("Kiracı, sözleşmenin feshi için otuz gün önceden bildirimde bulunur.", ["Fesih"], [7]),
            _chunk("Kira bedeli her ayın beşinde ödenir.", ["Ödeme"], [3]),
        ],
    )
    status = ki.index_upload(record)
    assert status.status == ki.STATUS_INDEXED and status.passage_count == 2

    hits = ki.search(["f1"], ["sözleşmesinde fesih bildirimi"])
    assert hits and hits[0].heading == "Fesih"
    assert hits[0].page_from == 7 and hits[0].filename == "kira.pdf"
    # The original text comes back, not the folded index form.
    assert "sözleşmenin" in hits[0].text


def test_ocr_dotless_i_errors_still_match(tmp_path: Path) -> None:
    # The scan was read with "i" for every "ı"; the user types it correctly.
    ki.index_upload(_text_upload("f1", "tarama.txt", "Yapi kisitlamalari ve izin sureci"))
    assert ki.search(["f1"], ["yapı kısıtlamaları"])


def test_search_never_leaves_the_given_files() -> None:
    ki.index_upload(_text_upload("mine", "a.txt", "Proje bütçesi iki milyon lira."))
    ki.index_upload(_text_upload("theirs", "b.txt", "Proje bütçesi beş milyon lira."))
    hits = ki.search(["mine"], ["proje bütçesi"])
    assert [hit.file_id for hit in hits] == ["mine"]
    assert ki.search([], ["proje bütçesi"]) == []


def test_the_same_document_uploaded_twice_returns_each_passage_once() -> None:
    ki.index_upload(_text_upload("first", "rapor.txt", "Deprem sonrası hasar tespiti."))
    ki.index_upload(_text_upload("again", "rapor.txt", "Deprem sonrası hasar tespiti."))
    hits = ki.search(["first", "again"], ["deprem hasar"])
    assert len(hits) == 1


def test_passages_several_queries_agree_on_rank_first() -> None:
    ki.index_upload(
        _text_upload(
            "f1",
            "notes.md",
            "# A\n\nTermination requires thirty days notice.\n\n"
            "# B\n\nNotice boards are in the hallway.\n\n"
            "# C\n\nTermination fees apply after the first year.",
        )
    )
    hits = ki.search(["f1"], ["termination notice", "fesih bildirimi termination notice period"])
    assert hits[0].heading == "A"


def test_reindexing_replaces_old_passages() -> None:
    ki.index_upload(_text_upload("f1", "a.txt", "eski metin kelimesi"))
    ki.index_upload(_text_upload("f1", "a.txt", "yeni metin sözcüğü"))
    assert ki.search(["f1"], ["kelimesi"]) == []
    assert ki.search(["f1"], ["sözcüğü"])
    assert ki.file_statuses(["f1"])["f1"].passage_count == 1


def test_drop_files_removes_passages_and_status() -> None:
    ki.index_upload(_text_upload("f1", "a.txt", "silinecek içerik burada"))
    ki.drop_files(["f1"])
    assert ki.search(["f1"], ["silinecek"]) == []
    assert ki.file_statuses(["f1"]) == {}


def test_neighbours_come_back_in_document_order(tmp_path: Path) -> None:
    record = _docling_upload(
        tmp_path,
        "f1",
        "rapor.pdf",
        [_chunk(f"Bölüm {i} içeriği {'hedef' if i == 2 else 'dolgu'}.", [f"H{i}"], [i]) for i in range(5)],
    )
    ki.index_upload(record)
    hits = ki.search(["f1"], ["hedef"])
    assert [h.ordinal for h in hits] == [2]
    around = ki.passages_around(hits, radius=1)
    assert [h.ordinal for h in around] == [1, 2, 3]
    assert around[1].score == hits[0].score and around[0].score == 0.0


# -- what gets indexed -----------------------------------------------------


def test_zip_and_csv_are_skipped_with_a_reason() -> None:
    zipped = ki.index_upload({"file_id": "z", "filename": "a.zip", "kind": "zip"})
    csv = ki.index_upload(_text_upload("c", "data.csv", "a,b\n1,2"))
    assert zipped.status == csv.status == ki.STATUS_SKIPPED
    assert zipped.reason and csv.reason


def test_a_document_without_text_is_skipped() -> None:
    status = ki.index_upload(_text_upload("f1", "empty.txt", "  \n\n "))
    assert status.status == ki.STATUS_SKIPPED
    assert status.reason == "no readable text"


def test_an_unreadable_chunk_file_is_recorded_as_failed(tmp_path: Path) -> None:
    folder = tmp_path / "broken.docling"
    folder.mkdir()
    (folder / "chunks.jsonl").write_bytes(b"\xff\xfe\xfa not utf-8")
    status = ki.index_upload(
        {"file_id": "f1", "filename": "x.pdf", "kind": "file", "docling_dir": str(folder)}
    )
    assert status.status == ki.STATUS_FAILED and status.reason


def test_statuses_report_the_schema_version() -> None:
    ki.index_upload(_text_upload("f1", "a.txt", "içerik"))
    assert ki.file_statuses(["f1", "missing"])["f1"].current is True


# -- background indexing ---------------------------------------------------


def test_submit_indexes_in_the_background() -> None:
    future = ki.submit(_text_upload("f1", "a.txt", "arka planda indekslenen metin"))
    assert future is not None
    assert future.result(timeout=10).status == ki.STATUS_INDEXED
    assert ki.search(["f1"], ["indekslenen"])
    assert not ki.is_pending("f1")


def test_submit_does_nothing_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    assert ki.submit(_text_upload("f1", "a.txt", "metin")) is None
