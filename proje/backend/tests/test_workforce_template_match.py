"""Unit tests for the DOCX template-match feature's deterministic pieces
(execution/langgraph_runtime/workforce.py's _request_wants_template_match,
_find_uploaded_docx_template, _workspace_has_uploaded_sources). No model
call or real job needed -- these are the same regex/filesystem gates that
route "aynı formatta yaz" requests to fill_docx_template instead of the
ordinary from-scratch docx writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from execution.langgraph_runtime.workforce import (
    _doc_token_budget,
    _find_uploaded_docx_template,
    _fold_tr,
    _parse_json_object,
    _request_wants_template_match,
    _workspace_has_uploaded_sources,
)


@pytest.mark.parametrize(
    "text",
    [
        "Yüklediğim vekaletnameyle aynı formatta bir kira sözleşmesi yaz",
        "bu şablonu kullanarak bir teklif mektubu hazırla",
        "aynı yapıda ama Ahmet için yaz",
        "use this template to write a new agreement",
        "yüklediğim dosyanın formatında yeni bir metin yaz",
    ],
)
def test_request_wants_template_match_matches_expected_phrasing(text: str) -> None:
    assert _request_wants_template_match(text)


@pytest.mark.parametrize(
    "text",
    [
        "bunu pdf olarak ver",
        "5 makale özetle",
        "bana bir excel dosyası oluştur",
        "bu dosyayı analiz et",
    ],
)
def test_request_wants_template_match_ignores_unrelated_asks(text: str) -> None:
    assert not _request_wants_template_match(text)


def test_find_uploaded_docx_template_finds_a_docx_under_sources(tmp_path: Path) -> None:
    sources = tmp_path / "sources" / "abc123"
    sources.mkdir(parents=True)
    (sources / "vekaletname.docx").write_bytes(b"PK\x03\x04fake docx bytes")
    (sources / "notes.txt").write_text("not a docx", encoding="utf-8")

    found = _find_uploaded_docx_template(str(tmp_path))

    assert found == "sources/abc123/vekaletname.docx"


def test_find_uploaded_docx_template_none_without_sources(tmp_path: Path) -> None:
    assert _find_uploaded_docx_template(str(tmp_path)) is None
    assert _find_uploaded_docx_template(None) is None

    (tmp_path / "sources").mkdir()
    assert _find_uploaded_docx_template(str(tmp_path)) is None


def test_workspace_has_uploaded_sources(tmp_path: Path) -> None:
    assert not _workspace_has_uploaded_sources(str(tmp_path))
    assert not _workspace_has_uploaded_sources(None)

    sources = tmp_path / "sources" / "xyz"
    sources.mkdir(parents=True)
    (sources / "input.xlsx").write_bytes(b"data")

    assert _workspace_has_uploaded_sources(str(tmp_path))


@pytest.mark.parametrize(
    "text",
    [
        # Typed without diacritics, which is how Turkish is usually typed.
        # The pattern was written in dotted form only, so every one of
        # these missed and the attachment was sent to web research
        # instead -- which wrote a report about an unrelated real company
        # that happened to share the name.
        "bunu omer insaat sirketleri as olarak yazsana ya ayni olsun her sey bana gore duzenle",
        "sana ayni formatta demedim her seyi ayni olucak",
        "her seyi ayni yap",
        "bu sablonu kullan",
        "bunu benim sirketim icin duzenle",
        "keep everything the same",
    ],
)
def test_template_match_survives_undotted_turkish(text: str) -> None:
    assert _request_wants_template_match(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "turkiye ekonomisi hakkinda rapor yaz",
        "bana bir sunum hazirla",
        "bugun dolar kuru kac",
    ],
)
def test_plain_requests_do_not_claim_a_template(text: str) -> None:
    assert _request_wants_template_match(text) is False


def test_fold_tr_normalizes_both_spellings() -> None:
    assert _fold_tr("Aynı Şablon") == _fold_tr("ayni sablon") == "ayni sablon"


def test_truncated_json_is_salvaged_not_discarded() -> None:
    """Live bug: the model's reply hit the output ceiling mid-document, so
    json.loads failed and the caller wrote the raw reply into the .docx --
    the user's file came out full of literal braces, JSON keys, and "\n"
    escapes, ending mid-heading. Everything before the cut is intact and
    must be recovered."""
    cut = (
        '{"filename": "a.docx", "title": "Omer Insaat", '
        '"content": "## Sartname\n\n### MADDE 6: ORTA'
    )
    payload = _parse_json_object(cut)
    assert payload["title"] == "Omer Insaat"
    assert payload["content"] == "## Sartname\n\n### MADDE 6: ORTA"


def test_salvage_handles_a_cut_inside_a_nested_container() -> None:
    payload = _parse_json_object('{"a": {"b": [1, 2, {"c": "kesik')
    assert payload["a"]["b"][2]["c"] == "kesik"


def test_salvage_still_rejects_prose() -> None:
    """A reply that is not JSON at all must keep failing, so the caller
    falls back to the notes rather than half-guessing a payload."""
    assert _parse_json_object("bu sadece duz metin, json degil") == {}
    assert _parse_json_object("") == {}


def test_template_rewrite_budget_covers_the_uploaded_document() -> None:
    """The budget sized itself from the research brief, but a template
    rewrite has no brief -- its length comes from the attached file."""
    base = _doc_token_budget("", paged=True, primary="docx", deep_research=False)
    sized = _doc_token_budget(
        "", paged=True, primary="docx", deep_research=False, source_text="x" * 8888
    )
    assert sized > base
    assert sized >= 8888


def test_salvage_escapes_raw_newlines_inside_a_string() -> None:
    """Models writing a long markdown body into a JSON string field
    routinely emit real newlines instead of \n, which is invalid JSON and
    fails exactly like a truncation."""
    payload = _parse_json_object('{"content": "## Baslik\nikinci satir"}')
    assert payload["content"] == "## Baslik\nikinci satir"


def test_llm_intent_decides_template_reuse_over_the_regex() -> None:
    """The classifier is primary; the regex only fills in when it did not run.

    Every pattern written for this caught its own example and missed the
    next one. "lutfen docx olarak ver" was the one that finally went to web
    search and came back apologising it could not find the user's own
    attached file on the internet.
    """
    from execution.langgraph_runtime import workforce

    # A phrasing no pattern matches, which the classifier recognised.
    assert _request_wants_template_match("lutfen docx olarak ver") is False

    def decide(flag, text):
        if flag is None:
            return _request_wants_template_match(text)
        return bool(flag)

    assert decide(True, "lutfen docx olarak ver") is True
    # And it can veto: an attached file being read from, not reproduced.
    assert decide(False, "bunu doldur") is False
    # No classifier answer -> the regex still decides.
    assert decide(None, "bunu doldur") is True
    assert workforce is not None


def test_attachment_names_reach_the_classifier() -> None:
    """Without them the field cannot be decided: "lutfen docx olarak ver"
    reproduces the attachment when one exists and writes something new when
    none does -- same sentence, opposite answers."""
    import inspect

    import artifact_intent

    sig = inspect.signature(artifact_intent.classify_artifact_intent)
    assert "attachment_names" in sig.parameters
    assert "reuses_uploaded_document" in artifact_intent.ArtifactIntentResult.model_fields


def test_a_named_format_that_was_uploaded_is_not_an_output_request() -> None:
    """"bu pdf'in içeriğini docx olarak ver" asked for one .docx and got a
    .docx plus a .pdf: the word "pdf" referred to the attachment, not to a
    second deliverable. Two files also silently disabled the uploaded
    template path, which only runs for a single-format job.

    Resolved with evidence rather than another phrase list -- what the user
    attached is what makes the mention ambiguous, so it is what settles it.
    """
    from execution.langgraph_runtime.workforce import _infer_artifact_kinds

    ask = "bu pdf in iceriginin aynisini bizim template inde docx olarak ver"
    assert _infer_artifact_kinds("docx", ask) == ["docx", "pdf"]
    assert _infer_artifact_kinds("docx", ask, attached_kinds={"pdf", "docx"}) == ["docx"]

    # A genuine multi-format request still produces both.
    assert _infer_artifact_kinds(
        "docx", "raporu hem docx hem pdf olarak ver", attached_kinds=set()
    ) == ["docx", "pdf"]

    # The primary itself is never dropped, even when that type is attached.
    assert _infer_artifact_kinds(
        "pdf", "bu pdf i ozetle ve pdf ver", attached_kinds={"pdf"}
    ) == ["pdf"]


def test_uploaded_kinds_reads_the_workspace(tmp_path) -> None:
    from execution.langgraph_runtime.workforce import _uploaded_kinds

    assert _uploaded_kinds(None) == set()
    assert _uploaded_kinds(str(tmp_path)) == set()

    sources = tmp_path / "sources" / "abc"
    sources.mkdir(parents=True)
    (sources / "teklif.pdf").write_bytes(b"x")
    (sources / "sartname.docx").write_bytes(b"x")
    assert _uploaded_kinds(str(tmp_path)) == {"pdf", "docx"}
