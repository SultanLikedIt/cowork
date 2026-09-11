"""Tests for document_export structuring + DOCX/PDF writers."""

from __future__ import annotations

import io

from docx import Document

import document_export

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f" -- {detail}"))
    if not condition:
        failures.append(name)


def test_structure_prose_splits_paren_enums() -> None:
    wall = (
        "Giriş cümlesi burada. Raporda senaryolar: (1) Fatura işler. "
        "(2) WhatsApp yanıtlar. (3) Teklif yazar."
    )
    out = document_export.structure_prose(wall)
    check("structure: has blank-line paragraphs", "\n\n" in out or "\n1." in out, repr(out[:200]))
    check("structure: numbered 1.", "1." in out and "Fatura" in out, out)
    check("structure: numbered 2.", "2." in out and "WhatsApp" in out, out)


def test_build_docx_multiple_paragraphs_from_wall() -> None:
    wall = (
        "Bu rapor KOBİ’ler içindir. Raporda sekiz senaryo vardır: "
        "(1) Fatura – tasarruf. (2) WhatsApp – lead. (3) Teklif – hız. "
        "Kritik başarı faktörleri pilot-first yaklaşımıdır."
    )
    raw = document_export.build_docx("KOBİ Raporu", wall)
    doc = Document(__import__("io").BytesIO(raw))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    check("docx: title present", paras[0].startswith("KOBİ"), paras[:2])
    check("docx: more than title+one body", len(paras) >= 4, paras)


def test_synthetic_table_skipped() -> None:
    raw = document_export.build_docx(
        "Only Prose",
        "Kısa bir paragraf. İkinci cümle de burada kalır ve uzar biraz daha.",
        header=["Bölüm", "İçerik"],
        rows=[["1", "parça"]],
    )
    doc = Document(__import__("io").BytesIO(raw))
    check("docx: synthetic Bölüm table omitted", len(doc.tables) == 0, str(len(doc.tables)))


def test_real_table_kept() -> None:
    raw = document_export.build_docx(
        "With Table",
        "Özet paragrafı.",
        header=["Senaryo", "Etki"],
        rows=[["Fatura", "6 saat"]],
    )
    doc = Document(__import__("io").BytesIO(raw))
    check("docx: real table kept", len(doc.tables) == 1)
    check("docx: header cell", doc.tables[0].rows[0].cells[0].text == "Senaryo")


def test_sanitize_strips_chat_and_markdown() -> None:
    dirty = (
        "Elbette, aşağıda Palantir hakkında kapsamlı araştırma raporu "
        "bulunmaktadır. Bu metin, bir Word belgesi ve HTML sunum hazırlamak "
        "için kullanılabilir.\n\n"
        "### Palantir Maven\n\n"
        "---\n"
        "Sistem **güçlü** bir platformdur. "
        "Detay için [kaynak](https://example.com/a).\n"
    )
    clean = document_export.sanitize_report_prose(dirty)
    check("sanitize: no Elbette", "Elbette" not in clean, clean[:160])
    check("sanitize: no chat meta", "kullanılabilir" not in clean.casefold(), clean[:200])
    check("sanitize: no **", "**" not in clean, clean[:200])
    check("sanitize: no hr line", "---" not in clean, clean)
    check("sanitize: keeps URL", "https://example.com/a" in clean, clean)
    check("sanitize: keeps topic", "Palantir" in clean, clean)

    raw = document_export.build_docx("Maven", dirty)
    doc = Document(__import__("io").BytesIO(raw))
    joined = "\n".join(p.text for p in doc.paragraphs)
    check("docx: no Elbette", "Elbette" not in joined, joined[:200])
    check("docx: no ### hashes", "###" not in joined, joined[:200])
    check("docx: heading text kept", "Palantir Maven" in joined, joined[:200])


def test_short_title_rejects_user_prompt() -> None:
    title = document_export.short_report_title(
        "bana palantir maven sistemi ile ilgili araştırma yapıp word yazmanı istiyorum",
        fallback="Rapor",
    )
    check("title: fallback on prompt echo", title == "Rapor", title)


def test_short_title_rejects_thinking_leak() -> None:
    dirty = (
        "Elimdeki kanıtlarda Palantir Maven Sistemi hakkında çeşitli bilgiler "
        "var. Bunları kullanarak detaylı bir araştırma metni oluşturabilirim.\n"
        "Raporu yapılandıracağım: Kısa bir girişle başlayıp…\n"
        "Palantir Maven Sistemi Araştırma Raporu – Detaylı İnceleme\n"
        "Giriş\n"
        "Palantir Maven bir platformdur."
    )
    title = document_export.short_report_title(dirty, fallback="Rapor")
    check("title: not thinking", "Elimdeki" not in title, title)
    check("title: not yapılandır", "yapılandır" not in title.casefold(), title)
    check(
        "title: real heading",
        "Palantir Maven" in title and "Raporu" in title,
        title,
    )
    clean = document_export.sanitize_report_prose(dirty)
    check("body: no Elimdeki", "Elimdeki" not in clean, clean[:120])
    check("body: keeps real heading", "Detaylı İnceleme" in clean, clean[:200])
    raw = document_export.build_docx(dirty.split("\n")[0], dirty)
    doc = Document(__import__("io").BytesIO(raw))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    check("docx title not thinking", paras and "Elimdeki" not in paras[0], paras[:2])


def test_short_title_rejects_kaynaklar_heading() -> None:
    body = (
        "5. Kaynaklar\n"
        "Giriş\n"
        "Küresel ısınma kutup ayılarını etkiler. Daha uzun bir cümle burada kalır.\n"
        "Popülasyon Durumu\n"
        "Küresel kutup ayısı popülasyonu azalmaktadır."
    )
    title = document_export.short_report_title(body, fallback="Rapor")
    check("title: not Kaynaklar", "kaynak" not in title.casefold(), title)
    check("title: not bare Giriş", title.casefold() != "giriş", title)
    check("title: has topic word", "kutup" in title.casefold() or "popülasyon" in title.casefold() or title == "Rapor", title)


def test_sanitize_strips_gorsel_notu() -> None:
    dirty = (
        "Özet paragrafı burada.\n\n"
        "Görsel Notu\n"
        "Bu belgeye telifsiz bir kutup ayısı görseli eklenmesi önerilir. "
        "Görselin Unsplash, Pexels veya Pixabay gibi ücretsiz platformlardan "
        "temin edilmesi uygundur.\n\n"
        "Kaynaklar\n"
        "1 https://example.com/a\n"
    )
    clean = document_export.sanitize_report_prose(dirty)
    check("sanitize: no Görsel Notu", "Görsel Notu" not in clean, clean)
    check("sanitize: no Unsplash advice", "Unsplash" not in clean, clean)
    check("sanitize: keeps ozet", "Özet" in clean, clean)


def test_build_pdf_sanitizes_preamble() -> None:
    raw = document_export.build_pdf(
        "Maven Raporu",
        "Elbette, aşağıda rapor bulunmaktadır.\n\nGiriş\n\nMaven bir sistemdir.",
    )
    check("pdf: non-empty bytes", len(raw) > 200, str(len(raw)))
    # PDF binary should not contain the Turkish chat opener as UTF-8 run.
    check("pdf: no Elbette utf-8", b"Elbette" not in raw, "found Elbette")


def test_docx_footer_has_page_fields() -> None:
    raw = document_export.build_docx("Başlık", "Bir paragraf. İkinci cümle burada.")
    xml = __import__("zipfile").ZipFile(__import__("io").BytesIO(raw)).read(
        "word/footer1.xml"
    ).decode("utf-8", "ignore")
    check("docx footer: PAGE field", "PAGE" in xml, xml[:200])
    check("docx footer: NUMPAGES field", "NUMPAGES" in xml, xml[:200])
    check("docx footer: Sayfa label", "Sayfa" in xml, xml[:200])


def test_markdown_pipe_table_becomes_docx_table() -> None:
    body = (
        "6. Operasyonel Hacim\n\n"
        "| Tarih / Dönem | Gelişme |\n"
        "|---------------|---------|\n"
        "| Kasım 2025 | NATO, MSS'yi satın alır |\n"
        "| Nisan 2026 | 11000 saldırı bildirilir |\n"
    )
    raw = document_export.build_docx("Maven", body)
    doc = Document(__import__("io").BytesIO(raw))
    check("md table: docx has table", len(doc.tables) == 1, str(len(doc.tables)))
    if doc.tables:
        cells = [[c.text for c in row.cells] for row in doc.tables[0].rows]
        check("md table: header", cells[0][0].startswith("Tarih"), cells[0])
        check("md table: row", "NATO" in cells[1][1], cells)
        joined = "\n".join(p.text for p in doc.paragraphs)
        check("md table: no raw pipes in prose", "|---" not in joined, joined)


def test_collapsed_markdown_table_expands() -> None:
    blob = (
        "| Tarih / Dönem | Gelişme | |-------------|-------| "
        "| Kasım 2025 | NATO alır | | Nisan 2026 | 11000 saldırı |"
    )
    blocks = document_export._blocks(blob)
    tables = [b for b in blocks if b[0] == "table"]
    check("collapsed: one table block", len(tables) == 1, str(blocks))
    if tables:
        matrix = tables[0][1]
        check("collapsed: header+rows", len(matrix) >= 3, str(matrix))
        check("collapsed: Kasım present", any("Kasım" in r[0] for r in matrix), str(matrix))


def test_build_pptx_dark_deck() -> None:
    from pptx import Presentation
    from io import BytesIO

    raw = document_export.build_pptx(
        "Maven Smart System",
        "## Giriş\n\n"
        "- Palantir Maven operasyonel karar destek sistemidir.\n"
        "- Gerçek zamanlı istihbarat birleştirir.\n\n"
        "## Entegrasyon\n\n"
        "- NATO ile entegre çalışır.\n"
        "- Pentagon iş akışlarına bağlanır.\n\n"
        "Kaynaklar:\n"
        "- https://example.com/maven\n"
        "- https://example.com/nato\n",
        header=["Alan", "Değer"],
        rows=[["Kapsam", "Realtime"], ["Tehdit", "AI"]],
    )
    prs = Presentation(BytesIO(raw))
    # title + agenda + 2 sections + table + sources
    check("pptx: has structured slides", len(prs.slides) >= 5, str(len(prs.slides)))
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
    joined = "\n".join(texts)
    check("pptx: title present", "Maven" in joined, joined[:200])
    check("pptx: section present", "Giriş" in joined or "Entegrasyon" in joined, joined[:300])
    check("pptx: agenda slide", "Gündem" in joined, joined[:400])
    check("pptx: bullets not wall", "●" in joined or "• " in joined or "- " in joined, joined[:400])
    check("pptx: sources slide", "Kaynaklar" in joined or "example.com" in joined, joined[:500])
    has_table = any(
        shape.has_table for slide in prs.slides for shape in slide.shapes
    )
    check("pptx: data table slide", has_table)


def test_build_pptx_wall_of_text_becomes_bullet_slides() -> None:
    from pptx import Presentation
    from io import BytesIO

    wall = (
        "Büyük dil modellerinde RAG 2021 sonrası hızla olgunlaştı. "
        "Dense retrieval semantik benzerlikte güçlüdür. "
        "Sparse BM25 anahtar kelime eşlemesinde ucuz ve hızlıdır. "
        "Hybrid arama ikisini birleştirerek production'da standarttır. "
        "Hallucination azaltmada grounding kritik kalır. "
        "Değerlendirme benchmark'ları production hatalarının yarısını kaçırır. "
        "Maliyet performans dengesi açık ağırlıklı modellerle değişir. "
        "Çok dilli retrieval hâlâ zayıf kalır. "
        "Sessiz hatalar ekipler tarafından geç fark edilir. "
        "Kaynak doğrulama olmadan sayı uydurma riski artar."
    )
    raw = document_export.build_pptx("RAG Literatür Özeti", wall)
    prs = Presentation(BytesIO(raw))
    check(
        "wall pptx: multiple slides",
        len(prs.slides) >= 3,
        str(len(prs.slides)),
    )
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
    joined = "\n".join(texts)
    bullet_lines = [
        ln
        for ln in joined.splitlines()
        if ln.strip().startswith(("•", "●", "-"))
    ]
    check("wall pptx: has bullets", len(bullet_lines) >= 3, str(len(bullet_lines)))
    check(
        "wall pptx: bullets stay short",
        all(len(ln) <= 120 for ln in bullet_lines),
        str(max((len(ln) for ln in bullet_lines), default=0)),
    )


def test_presentation_title_from_request_strips_fluff() -> None:
    title = document_export.presentation_title_from_request(
        "yapay zeka hakkinda detayli arastirma yapip bana bunun hakkinda "
        "sunum hazirlar misin pptx"
    )
    check("pptx title has topic", "yapay" in title.casefold() or "zeka" in title.casefold(), title)
    check("pptx title not prompt", "hazirla" not in title.casefold(), title)
    check("pptx title not pptx word", "pptx" not in title.casefold(), title)

    chatty = document_export.resolve_presentation_title(
        "galatasaray hakkinda arastirma yap ve bana pptx ver "
        "guzel bir sekilde hadi bakiyim",
        body=(
            "## Galatasaray Spor Kulübü — Araştırma Raporu\n\n"
            "- Kulüp 1905'te kuruldu.\n"
            "- UEFA Kupası 2000.\n"
        ),
    )
    check(
        "chatty ask uses body title",
        "galatasaray" in chatty.casefold() and "hadi" not in chatty.casefold(),
        chatty,
    )
    check(
        "chatty ask not leftover fluff",
        "bakiyim" not in chatty.casefold() and "guzel" not in chatty.casefold(),
        chatty,
    )
    stripped = document_export.presentation_title_from_request(
        "galatasaray hakkinda arastirma yap ve bana pptx ver "
        "guzel bir sekilde hadi bakiyim"
    )
    check(
        "strip leaves topic only",
        stripped.casefold().startswith("galatasaray")
        and "hadi" not in stripped.casefold(),
        stripped,
    )

    bad = document_export.build_pptx(
        "galatasaray guzel sekilde hadi bakiyim",
        "## Galatasaray Spor Kulübü — Araştırma Raporu\n\n"
        "- Kulüp 1905'te kuruldu.\n"
        "- UEFA Kupası 2000.\n\n"
        "## Başarılar\n\n"
        "- Süper Lig şampiyonlukları.\n\n"
        "Kaynaklar:\n- https://example.com/gs\n",
    )
    from pptx import Presentation
    from io import BytesIO

    prs = Presentation(BytesIO(bad))
    title_slide_texts = []
    for shape in prs.slides[0].shapes:
        if shape.has_text_frame and shape.text_frame.text.strip():
            title_slide_texts.append(shape.text_frame.text)
    title_slide = "\n".join(title_slide_texts)
    check(
        "build_pptx recovers from chatty title",
        "hadi" not in title_slide.casefold()
        and "galatasaray" in title_slide.casefold(),
        title_slide[:120],
    )

    bad = document_export.build_pptx(
        "- Ekosistem Çağrısı: akıllı üretim",
        "## Microsoft Trendleri\n\n"
        "- YZ ajanları iş gücüne katılıyor.\n"
        "- İnsan-YZ iş birliği artıyor.\n\n"
        "## Türkiye Planı\n\n"
        "- Eylem planı açıklandı.\n"
        "- TÜBİTAK çağrıları ekosistemi hareketlendiriyor.\n\n"
        "## Uygulama Alanları\n\n"
        "- Üretim ve finans öne çıkıyor.\n"
        "- Eğitim ve iklim projeleri büyüyor.\n\n"
        "Kaynaklar:\n- https://example.com/ai-trends\n",
    )
    from pptx import Presentation
    from io import BytesIO

    prs = Presentation(BytesIO(bad))
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
    joined = "\n".join(texts)
    check(
        "bad bullet not title",
        "Ekosistem" not in (texts[0] if texts else ""),
        texts[0] if texts else "",
    )
    check("has multiple content slides", len(prs.slides) >= 4, str(len(prs.slides)))
    check("has microsoft section", "Microsoft" in joined or "Trend" in joined, joined[:400])
    check(
        "sources clean",
        "##" not in joined.split("Kaynaklar")[-1] if "Kaynaklar" in joined else True,
    )


def test_build_pptx_title_has_no_overlapping_photo(tmp_path) -> None:
    from pptx import Presentation
    from io import BytesIO
    from pathlib import Path

    # Minimal 1x1 PNG
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img_path = Path(tmp_path) / "t.png"
    img_path.write_bytes(png)
    embed = document_export.ImageEmbed(path=img_path, attribution="x")
    raw = document_export.build_pptx(
        "Yapay Zeka",
        "## Nedir\n\n- Makinelerin ogrenme yetenegidir.\n\n"
        "## Neden\n\n- Is sureclerini hizlandirir.\n\n"
        "Kaynaklar:\n- https://example.com/ai\n",
        images=[embed],
    )
    prs = Presentation(BytesIO(raw))
    title = prs.slides[0]
    has_pic = any(
        shape.shape_type is not None and "PICTURE" in str(shape.shape_type)
        for shape in title.shapes
    )
    check("title slide: no photo overlap", not has_pic)
    texts = "\n".join(
        s.text_frame.text for s in title.shapes if s.has_text_frame
    )
    check("title slide: title visible", "Yapay" in texts or "Zeka" in texts, texts[:120])
    last = prs.slides[-1]
    last_text = "\n".join(
        s.text_frame.text for s in last.shapes if s.has_text_frame
    )
    check("sources last", "Kaynaklar" in last_text or "example.com" in last_text, last_text[:200])


def test_build_pptx_embeds_photo_on_its_own_slide(tmp_path) -> None:
    """Regression: build_pptx used to accept `images` and then `del images`
    without ever calling add_picture anywhere -- a fetched, correct topic
    photo was silently dropped from every PPTX. Fixed via
    _pptx_add_photo_slide, called right after the title slide."""
    from pptx import Presentation
    from io import BytesIO
    from pathlib import Path

    # Minimal 1x1 PNG (same fixture as the title-overlap test above).
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img_path = Path(tmp_path) / "t.png"
    img_path.write_bytes(png)
    embed = document_export.ImageEmbed(path=img_path, attribution="Test credit")
    raw = document_export.build_pptx(
        "Yapay Zeka",
        "## Nedir\n\n- Makinelerin ogrenme yetenegidir.\n\n"
        "## Neden\n\n- Is sureclerini hizlandirir.\n\n"
        "Kaynaklar:\n- https://example.com/ai\n",
        images=[embed],
    )
    prs = Presentation(BytesIO(raw))

    def _has_picture(slide) -> bool:
        return any(
            shape.shape_type is not None and "PICTURE" in str(shape.shape_type)
            for shape in slide.shapes
        )

    rest = list(prs.slides)[1:]
    assert not _has_picture(prs.slides[0]), "title slide must still have no photo"
    assert any(_has_picture(s) for s in rest), (
        "the fetched photo must land on some non-title slide, not be dropped"
    )
    photo_slide = next(s for s in rest if _has_picture(s))
    captions = "\n".join(
        s.text_frame.text for s in photo_slide.shapes if s.has_text_frame
    )
    assert "Test credit" in captions


def main() -> None:
    test_structure_prose_splits_paren_enums()
    test_build_docx_multiple_paragraphs_from_wall()
    test_synthetic_table_skipped()
    test_real_table_kept()
    test_sanitize_strips_chat_and_markdown()
    test_short_title_rejects_user_prompt()
    test_short_title_rejects_thinking_leak()
    test_short_title_rejects_kaynaklar_heading()
    test_sanitize_strips_gorsel_notu()
    test_build_pdf_sanitizes_preamble()
    test_docx_footer_has_page_fields()
    test_markdown_pipe_table_becomes_docx_table()
    test_collapsed_markdown_table_expands()
    test_build_pptx_dark_deck()
    test_build_pptx_wall_of_text_becomes_bullet_slides()
    test_presentation_title_from_request_strips_fluff()
    from pathlib import Path as _P
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        test_build_pptx_title_has_no_overlapping_photo(_P(_td))
    with tempfile.TemporaryDirectory() as _td:
        test_build_pptx_embeds_photo_on_its_own_slide(_P(_td))
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        raise SystemExit(1)
    print("All document_export checks passed")


if __name__ == "__main__":
    main()


def test_hard_line_breaks_survive_into_report_blocks() -> None:
    """Live regression: labelled figures ran together in the PDF.

    Research wrote each metric on its own line using markdown's hard break
    (two trailing spaces). sanitize_report_prose strips that whitespace and
    _blocks joins consecutive text lines on purpose (for hard-wrapped
    prose), so section 1 of a report reached the page as one wall of text
    with the station and socket counts running into each other.
    """
    import document_export

    body = (
        "Temmuz 2026 itibarıyla:\n\n"
        "**Toplam istasyon:** 12.687  \n"
        "**Toplam soket:** 45.660  \n"
        "**DC soket:** 19.903\n\n"
        "Bu paragraf hard-wrapped yazilmistir\n"
        "ve tek parca kalmalidir."
    )
    texts = [t for kind, t, _ in document_export.iter_body_blocks(body) if kind == "text"]

    assert "Toplam istasyon: 12.687" in texts
    assert "Toplam soket: 45.660" in texts
    assert "DC soket: 19.903" in texts
    # Ordinary wrapped prose must still be joined, which is why _blocks
    # merges consecutive lines in the first place.
    assert any("hard-wrapped yazilmistir ve tek parca" in t for t in texts)


def test_fill_docx_template_preserves_font_and_footer(tmp_path) -> None:
    """Template-match request: the output must keep the UPLOADED file's
    own font/footer, not document_export's usual from-scratch styling."""
    from docx.shared import Pt

    src = Document()
    src.styles["Normal"].font.name = "Georgia"
    src.styles["Normal"].font.size = Pt(13)
    src.sections[0].footer.paragraphs[0].text = "Şirket Adı A.Ş."
    src.add_paragraph("placeholder body text")
    path = tmp_path / "template.docx"
    src.save(path)

    out = document_export.fill_docx_template(
        path, "Yeni Başlık", "İçerik metni burada."
    )
    result = Document(io.BytesIO(out))

    assert result.styles["Normal"].font.name == "Georgia"
    assert result.styles["Normal"].font.size == Pt(13)
    assert result.sections[0].footer.paragraphs[0].text == "Şirket Adı A.Ş."
    all_text = "\n".join(p.text for p in result.paragraphs)
    assert "placeholder body text" not in all_text
    assert "İçerik metni" in all_text


def test_template_headings_use_the_templates_own_style_names() -> None:
    """Style names cannot be guessed from English.

    A Word file authored in Turkish names its heading styles "Başlık 1"
    (id Balk1). Looking up "Heading 1" found nothing, add_heading raised
    KeyError, and every heading collapsed to plain body text -- measured
    on a live template: 17 styled paragraphs in, zero out, with the text
    itself perfectly correct. So read the styles off the template's own
    paragraphs instead of naming them.
    """
    import io
    import re
    import zipfile
    from pathlib import Path

    import docx

    import document_export

    # A template whose heading style is named the way Turkish Word names it.
    source = docx.Document()
    style = source.styles.add_style("Başlık 1", 1)  # 1 = WD_STYLE_TYPE.PARAGRAPH
    heading = source.add_paragraph("Var olan baslik")
    heading.style = style
    source.add_paragraph("Govde metni")

    tmp = Path(io.BytesIO() and "/tmp/_tmpl_style_test.docx")
    source.save(str(tmp))

    observed = document_export._observed_template_styles(docx.Document(str(tmp)))
    assert "Başlık 1" in observed["headings"]

    out = document_export.fill_docx_template(tmp, "", "## Yeni baslik\n\nYeni metin.")
    xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode("utf8")
    used = set(re.findall(r'w:pStyle w:val="([^"]+)"', xml))
    assert any("Balk" in u or "Ba" in u for u in used), used


def test_markdown_table_rows_become_readable_slide_bullets() -> None:
    """A slide has no table, so a row that reaches the bullet builder used
    to be shown with its pipes intact -- "| Tür | Devlet üniversitesi |"
    on screen, which reads as leaked markup rather than the label/value
    pair it is."""
    import document_export

    assert (
        document_export._md_table_row_to_bullet("| Tür | Devlet üniversitesi |")
        == "Tür: Devlet üniversitesi"
    )
    assert document_export._md_table_row_to_bullet("| a | b | c |") == "a · b · c"
    # Separator rows carry nothing to show.
    assert document_export._md_table_row_to_bullet("|---|---|") == ""

    # A separator marks the line above it as the header, which carries only
    # column labels -- dropped. Data rows survive.
    bullets = document_export._pptx_paras_to_bullets(
        ["| Tür | Devlet üniversitesi |\n|---|---|\n| Rektör | Lord Patten |"]
    )
    assert bullets == ["Rektör: Lord Patten"]

    # With no separator there is no header to identify, so every row shows.
    assert document_export._pptx_paras_to_bullets(
        ["| Tür | Devlet üniversitesi |\n| Rektör | Lord Patten |"]
    ) == ["Tür: Devlet üniversitesi", "Rektör: Lord Patten"]


def test_prose_containing_a_pipe_is_left_alone() -> None:
    """Only actual table rows are touched -- the real layout must not
    change for anything else."""
    import document_export

    for line in ("Bu normal bir cumle.", "- madde bir", "Fiyat 100 TL | indirimli"):
        assert document_export._md_table_row_to_bullet(line) is None

    assert document_export._pptx_paras_to_bullets(["Fiyat 100 TL | indirimli"]) == [
        "Fiyat 100 TL | indirimli"
    ]


def test_template_rewrite_keeps_bold_and_the_templates_own_numbering() -> None:
    """A template with 33 bold runs and 81 numbered items came back with
    none of either: the words were right and every one of them was plain.

    Bold is lost because markdown markers are stripped before any writer
    sees them (correct for writers that cannot render them, wrong here).
    Numbering is lost because it lives on the paragraph, not in a style
    name -- the template numbers through a numId no style references.
    """
    import re
    import zipfile
    from io import BytesIO
    from pathlib import Path

    import docx

    import document_export

    source = docx.Document()
    para = source.add_paragraph("Numarali madde")
    ppr = para._p.get_or_add_pPr()
    num_pr = ppr.makeelement(
        f"{{{document_export._W_NS}}}numPr", {}
    )
    num_id = num_pr.makeelement(f"{{{document_export._W_NS}}}numId", {})
    num_id.set(f"{{{document_export._W_NS}}}val", "5")
    num_pr.append(num_id)
    ppr.append(num_pr)

    tmpl = Path("/tmp/_tmpl_bold_num.docx")
    source.save(str(tmpl))

    body = (
        "Bu metin **kalin** icerir.\n\n"
        "1. Tum imalatlar ilgili TSE standartlarina uygun olarak yapilacaktir.\n"
        "2. Malzemeler belgeli olacak ve denetimde ibraz edilecektir.\n"
    )
    out = document_export.fill_docx_template(tmpl, "", body)
    xml = zipfile.ZipFile(BytesIO(out)).read("word/document.xml").decode("utf8")

    assert xml.count("<w:b/>") >= 1, "bold run missing"
    assert xml.count("<w:numPr>") >= 2, "template numbering not applied"
    assert "5" in re.findall(r'w:numId w:val="(\d+)"', xml)
    # The markers themselves must not survive as literal text.
    assert "**" not in re.sub(r"<[^>]+>", "", xml)


def test_default_writers_still_strip_bold_markers() -> None:
    """keep_bold is opt-in. Writers that cannot render bold must keep
    stripping, or the asterisks show up in the file."""
    import document_export

    assert document_export._blocks("Bu **kalin** metin.") == [
        ("text", "Bu kalin metin.", 0)
    ]
    assert document_export._blocks("Bu **kalin** metin.", keep_bold=True) == [
        ("text", "Bu **kalin** metin.", 0)
    ]


def test_table_style_falls_back_instead_of_failing_the_whole_document() -> None:
    """A template with no "Light Grid Accent 1" lost its entire deliverable.

    python-docx's default styles include the English name; an uploaded
    template carries the user's own, and a Turkish one has none of them.
    The write raised "no style with name 'Light Grid Accent 1'" and the
    document was discarded -- over a table border.
    """
    import io
    import zipfile
    from pathlib import Path

    import docx

    import document_export

    body = "Giris.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nSon."

    # Default document: the known style exists and is used.
    plain = Path("/tmp/_tbl_default.docx")
    docx.Document().save(str(plain))
    xml = zipfile.ZipFile(
        io.BytesIO(document_export.fill_docx_template(plain, "T", body))
    ).read("word/document.xml").decode("utf8")
    assert xml.count("<w:tbl>") == 1

    # A template with every table style removed still produces the table.
    stripped_doc = docx.Document()
    for style in list(stripped_doc.styles):
        try:
            if style.type is not None and int(style.type) == 3:
                style.delete()
        except Exception:  # noqa: BLE001
            continue
    stripped = Path("/tmp/_tbl_none.docx")
    stripped_doc.save(str(stripped))
    xml = zipfile.ZipFile(
        io.BytesIO(document_export.fill_docx_template(stripped, "T", body))
    ).read("word/document.xml").decode("utf8")
    assert xml.count("<w:tbl>") == 1


def test_a_truncated_table_row_is_still_a_table_row() -> None:
    """A row loses its closing pipe whenever the text is clipped at a
    character cap mid-table, and the strict form stopped recognising it --
    so "| Konum | Thames Nehri kıyısı, İngiltere'ni" reached a slide with
    its pipes showing while the row above it converted cleanly."""
    import document_export

    assert (
        document_export._md_table_row_to_bullet("| Konum | Thames Nehri kiyisi, Ing")
        == "Konum: Thames Nehri kiyisi, Ing"
    )
    assert (
        document_export._md_table_row_to_bullet("| Konum | Thames Nehri kiyisi |")
        == "Konum: Thames Nehri kiyisi"
    )
    # Prose that merely contains a pipe is still left alone.
    assert document_export._md_table_row_to_bullet("Fiyat 100 TL | indirimli") is None
    assert document_export._md_table_row_to_bullet("normal bir cumle") is None


def test_table_header_row_is_not_a_slide_bullet() -> None:
    """The separator row says the line before it was the header, which as a
    bullet is only column labels -- a live slide opened with
    "Özellik: Detay", which names nothing."""
    import document_export

    bullets = document_export._pptx_paras_to_bullets(
        [
            "| Ozellik | Detay |",
            "|---|---|",
            "| Konum | Thames kiyisi |",
            "| Nufus | 9 milyon |",
            "Normal bir cumle burada.",
        ]
    )
    assert "Ozellik: Detay" not in bullets
    assert "Konum: Thames kiyisi" in bullets
    assert "Nufus: 9 milyon" in bullets
    assert "Normal bir cumle burada." in bullets
