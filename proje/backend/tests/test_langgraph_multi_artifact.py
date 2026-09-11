"""Multi-deliverable kind inference for LangGraph Document Agent."""

from __future__ import annotations

from execution.langgraph_runtime import workforce as lg_wf


def test_word_plus_html_sunum() -> None:
    req = (
        "palantir maven arastirma yapip word dokumani yaz ve bu word "
        "dokumanindan html sunum hazirla"
    )
    kinds = lg_wf._infer_artifact_kinds("docx", req)
    assert kinds[0] == "docx", kinds
    assert "html" in kinds, kinds


def test_power_point_spaced_with_word_html() -> None:
    """Users often write 'power pointte' with a space — must still get pptx."""
    req = (
        "bana palantir maven sistemi ile ilgili araştırma yapıp bir word "
        "dökümanı yazmanı ve bu word dökümanından html sunum hazırlamanı "
        "istiyorum ayni zamanda power pointte de verebilirsen"
    )
    kinds = lg_wf._infer_artifact_kinds("docx", req)
    assert "docx" in kinds, kinds
    assert "html" in kinds, kinds
    assert "pptx" in kinds, kinds


def test_sunum_alone_is_html() -> None:
    kinds = lg_wf._infer_artifact_kinds("none", "kisa bir sunum hazirla")
    assert kinds == ["html"], kinds


def test_powerpoint_sunumu_does_not_also_add_html() -> None:
    """"PowerPoint sunumu" is the ordinary Turkish phrase for "a PowerPoint
    presentation" -- sunum here describes what the PowerPoint already is,
    not a request for a second HTML deck. Regression: before this fix, the
    "sunum" default only checked whether "html" was already found, not
    whether ANY kind (like pptx, from "PowerPoint") was -- so this exact
    phrase silently also tried to build an unwanted HTML deck alongside the
    .pptx, which crashed on oversized notes and reported the whole job as
    failed even though the .pptx itself had already been written."""
    kinds = lg_wf._infer_artifact_kinds(
        "pptx",
        "Ayasofya hakkında görsel içeren kısa bir PowerPoint sunumu hazırla",
    )
    assert kinds == ["pptx"], kinds


def test_html_deck_builds_without_llm() -> None:
    html = lg_wf._html_deck_from_payload(
        title="Maven",
        summary="Ozet",
        columns=["Alan", "Deger"],
        rows=[["Hiz", "Realtime"], ["Tehdit", "AI"]],
    )
    assert "Maven" in html
    assert "Realtime" in html
    assert 'class="hero"' in html
    assert "#07111d" in html
    assert "Hazırlanma Tarihi" in html


def test_html_deck_embeds_local_image(tmp_path) -> None:
    from document_export import ImageEmbed

    # Minimal valid JPEG (1x1)
    jpg = tmp_path / "t.jpg"
    jpg.write_bytes(
        bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300"
            "080606070605080707070909080a0c140d0c0b0b0c1912130f"
            "141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c3031"
            "34353636341f27393d38333c2e343636ffdb0043010909090c"
            "0b0c180d0d1832211c21323232323232323232323232323232"
            "32323232323232323232323232323232323232323232323232"
            "323232323232ffc00011080001000103011100021100031100"
            "ffc40014000100000000000000000000000000000000ffc400"
            "14100100000000000000000000000000000000ffda000c0301"
            "000211003f00bf80ffd9"
        )
    )
    html = lg_wf._html_deck_from_payload(
        title="Maven",
        summary="Ozet uzun metin icin yeterli karakter sayisi burada.",
        columns=[],
        rows=[],
        images=[ImageEmbed(path=jpg, attribution="Test credit")],
    )
    assert "data:image/jpeg;base64," in html
    assert "Test credit" in html
    assert "deck-photo" in html


def test_word_plus_powerpoint_stays_narrative_kinds() -> None:
    req = (
        "palantir maven arastirma yap word dokumani yaz html sunum ve "
        "powerpoint de ver gorsellerle"
    )
    kinds = lg_wf._infer_artifact_kinds("docx", req)
    assert "docx" in kinds
    assert "html" in kinds
    assert "pptx" in kinds
    # PPTX must not force the tabular Document JSON shape.
    needs_table = any(k in lg_wf._TABULAR_KINDS for k in kinds)
    assert needs_table is False


def test_carbone_table_from_narrative() -> None:
    cols, rows = lg_wf._ensure_carbone_table(
        [],
        [],
        title="Maven",
        summary="Birinci paragraf.\n\nIkinci paragraf uzun icerik.",
        body="",
    )
    assert cols == ["Bölüm", "İçerik"]
    assert len(rows) >= 1
    assert rows[0][1]


if __name__ == "__main__":
    test_word_plus_html_sunum()
    test_sunum_alone_is_html()
    test_html_deck_builds_without_llm()
    test_carbone_table_from_narrative()
    print("All multi-artifact checks passed")
