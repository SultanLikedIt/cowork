"""Company brand (brand.py): loading rules, and every writer wearing it.

The renderers read the brand once, at import. The branded checks therefore
run in a child interpreter with BRAND_DIR set, so this process's modules --
shared with every other test -- keep the built-in look.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import brand

BACKEND = Path(__file__).resolve().parents[1]
REPO_BRAND = BACKEND.parent / "brand"


def _png(width: int = 40, height: int = 10) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (0, 20, 137)).save(buffer, format="PNG")
    return buffer.getvalue()


def _brand_dir(tmp_path: Path, config: object, *, logo: bool = True) -> Path:
    (tmp_path / "brand.json").write_text(
        config if isinstance(config, str) else json.dumps(config), encoding="utf-8"
    )
    if logo:
        (tmp_path / "logo.png").write_bytes(_png())
    return tmp_path


COMPANY = {
    "name": "Örnek A.Ş.",
    "primary": "#001489",
    "secondary": "#DA291C",
    "logo": "logo.png",
}


# --- loading ------------------------------------------------------------------


def test_no_directory_or_no_config_is_the_built_in_look(tmp_path) -> None:
    assert brand.load(None) is brand.INACTIVE
    assert brand.load("") is brand.INACTIVE
    assert brand.load(tmp_path) is brand.INACTIVE
    assert brand.INACTIVE.primary == brand.DEFAULT_PRIMARY


def test_a_valid_brand_is_read(tmp_path) -> None:
    loaded = brand.load(_brand_dir(tmp_path, COMPANY))
    assert loaded.active
    assert (loaded.name, loaded.primary, loaded.secondary) == ("Örnek A.Ş.", "001489", "DA291C")
    assert loaded.logo_path == (tmp_path / "logo.png").resolve()
    data, mime = loaded.logo()
    assert mime == "image/png" and data.startswith(b"\x89PNG")


def test_the_repo_brand_carries_the_company_colours() -> None:
    loaded = brand.load(REPO_BRAND)
    # PANTONE Reflex Blue C and PANTONE 485 C.
    assert (loaded.primary, loaded.secondary) == ("001489", "DA291C")


@pytest.mark.parametrize(
    "config",
    ["{not json", "[1, 2]", json.dumps({"primary": "blue"}), json.dumps({"name": "x"})],
)
def test_a_broken_config_falls_back_instead_of_failing(tmp_path, config) -> None:
    assert brand.load(_brand_dir(tmp_path, config)) is brand.INACTIVE


def test_secondary_defaults_to_primary(tmp_path) -> None:
    loaded = brand.load(_brand_dir(tmp_path, {"primary": "001489"}, logo=False))
    assert loaded.secondary == "001489"
    assert loaded.logo_path is None and loaded.logo() is None


@pytest.mark.parametrize("name", ["../logo.png", "sub/logo.png", "logo.svg", "missing.png"])
def test_the_logo_must_be_a_png_or_jpeg_inside_the_folder(tmp_path, name) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "logo.png").write_bytes(_png())
    (tmp_path / "logo.svg").write_text("<svg/>", encoding="utf-8")
    loaded = brand.load(_brand_dir(tmp_path, {**COMPANY, "logo": name}, logo=False))
    assert loaded.active and loaded.logo_path is None


def test_an_oversized_logo_is_skipped(tmp_path) -> None:
    loaded = brand.load(_brand_dir(tmp_path, COMPANY))
    (tmp_path / "logo.png").write_bytes(b"\x89PNG" + b"0" * brand.MAX_LOGO_BYTES)
    assert loaded.logo() is None


def test_the_font_name_cannot_break_out_of_css(tmp_path) -> None:
    loaded = brand.load(_brand_dir(tmp_path, {**COMPANY, "font": "Arial'; } body { x"}))
    assert "'" not in loaded.font and "}" not in loaded.font and ";" not in loaded.font


def test_tints() -> None:
    assert brand.mix("001489", "FFFFFF", 0) == "001489"
    assert brand.mix("001489", "FFFFFF", 1) == "FFFFFF"
    company = brand.Brand(active=True, primary="001489", secondary="DA291C")
    assert company.soft != company.primary and company.strong != company.primary


def test_without_a_brand_the_writers_keep_their_look() -> None:
    import document_docx
    import document_export
    import document_html

    assert not brand.current().active
    assert document_docx.ACCENT == "2F5D8A"
    assert document_export._PPTX_BG == "07111D"
    assert document_html._brand_css(brand.current()) == ""


# --- every writer, under a brand -------------------------------------------------


BRANDED_SCRIPT = textwrap.dedent(
    """
    import io, json, tempfile
    from pathlib import Path

    import brand
    import document_docx, document_html, document_model, document_pptx
    from artifacts import ArtifactTools
    from execution.langgraph_runtime import workforce
    from slide_model import deck_from_slides

    out = {}
    doc = document_model.document_from_blocks("Pazar Raporu", [
        {"type": "heading", "level": 1, "text": "Giriş"},
        {"type": "paragraph", "text": "Satışlar **arttı**."},
        {"type": "table", "columns": ["Yıl", "Adet"], "rows": [["2025", "10"]]},
    ])

    page = document_html.render_html(doc)
    out["html_accent"] = "--accent: #001489" in page
    out["html_cover"] = "--brand-2: #DA291C" in page
    out["html_logo"] = 'class="brand-logo"' in page
    out["footer_company"] = "Örnek A.Ş. · Pazar Raporu" in document_html.footer_template("Pazar Raporu")
    deck_page = document_html.render_deck(doc)
    out["deck_accent"] = "--accent: #001489" in deck_page and 'class="brand-logo"' in deck_page

    from docx import Document
    word = Document(io.BytesIO(document_docx.build_docx(doc)))
    out["docx_heading"] = str(word.styles["Heading 1"].font.color.rgb)
    out["docx_eyebrow"] = next(
        str(run.font.color.rgb) for p in word.paragraphs for run in p.runs if run.text == "RAPOR"
    )
    out["docx_logo"] = len(word.inline_shapes)
    out["docx_footer"] = word.sections[0].footer.paragraphs[0].text

    from pptx import Presentation
    deck = deck_from_slides("Pazar Sunumu", [
        {"layout": "bullets", "title": "Özet", "bullets": ["Satışlar arttı.", "Maliyet düştü."]},
        {"layout": "bullets", "title": "Plan", "bullets": ["Yeni bayiler.", "İhracat."]},
    ])
    assert deck is not None
    prs = Presentation(io.BytesIO(document_pptx.build_pptx(deck)))
    first = prs.slides[0]
    out["pptx_background"] = str(first.background.fill.fore_color.rgb)
    out["pptx_logo"] = sum(1 for shape in first.shapes if shape.shape_type == 13)
    out["pptx_series"] = list(document_pptx.SERIES_COLORS[:2])

    root = Path(tempfile.mkdtemp())
    result = ArtifactTools(root).create_xlsx_artifact(
        filename="tablo", data=json.dumps([{"Yıl": 2025, "Adet": 10}])
    )
    from openpyxl import load_workbook
    sheet = load_workbook(root / result["path"]).active
    out["xlsx_fill"] = sheet["A1"].fill.fgColor.rgb
    out["xlsx_values"] = [[c.value for c in row] for row in sheet.iter_rows()]
    out["xlsx_frozen"] = sheet.freeze_panes

    style, logo = workforce._deck_brand()
    out["fallback_deck"] = "--cyan: #001489" in style and "brand-logo" in logo

    print(json.dumps(out, ensure_ascii=False))
    """
)


@pytest.fixture(scope="module")
def branded(tmp_path_factory) -> dict:
    directory = _brand_dir(tmp_path_factory.mktemp("brand"), COMPANY)
    env = {**os.environ, "BRAND_DIR": str(directory), "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, "-c", BRANDED_SCRIPT],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_pdf_report_and_deck_wear_the_brand(branded) -> None:
    assert branded["html_accent"] and branded["html_cover"] and branded["html_logo"]
    assert branded["footer_company"]
    assert branded["deck_accent"]
    assert branded["fallback_deck"]


def test_word_wears_the_brand(branded) -> None:
    assert branded["docx_heading"] == "001489"
    assert branded["docx_eyebrow"] == "DA291C"
    assert branded["docx_logo"] == 1
    assert branded["docx_footer"].startswith("Örnek A.Ş. · Pazar Raporu")


def test_powerpoint_turns_light_and_carries_the_logo(branded) -> None:
    assert branded["pptx_background"] == "FFFFFF"
    assert branded["pptx_logo"] == 1
    assert branded["pptx_series"] == ["001489", "DA291C"]


def test_excel_header_is_styled_and_the_data_is_unchanged(branded) -> None:
    assert branded["xlsx_fill"].endswith("001489")
    assert branded["xlsx_values"] == [["Yıl", "Adet"], [2025, 10]]
    assert branded["xlsx_frozen"] == "A2"
