"""Lookup-target plan (workforce_plan) and deck rendering regressions."""

from __future__ import annotations

import workforce_plan


def test_lookup_target_names_from_plan_compare_subjects() -> None:
    plan = workforce_plan.PlanResult(
        targets=[
            workforce_plan.PlanTarget(name="Türkiye"),
            workforce_plan.PlanTarget(name="Almanya"),
            workforce_plan.PlanTarget(name="Güney Kore"),
        ],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="karşılaştır",
                capabilities=["web_search"],
                target_names=["Türkiye", "Almanya", "Güney Kore"],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == [
        "Türkiye",
        "Almanya",
        "Güney Kore",
    ]


def test_lookup_target_names_empty_for_single_subject_plan() -> None:
    plan = workforce_plan.PlanResult(
        targets=[],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="Akdeniz diyeti nedir",
                capabilities=["web_search", "artifacts"],
                target_names=[],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == []


def test_lookup_target_names_empty_for_fx_rates_board_plan() -> None:
    """Same-day FX rates stay one subject — empty targets → single SEARCH."""
    plan = workforce_plan.PlanResult(
        targets=[],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="güncel dolar euro pound kurları",
                capabilities=["web_search"],
                target_names=[],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == []
    assert "dolar euro pound" in workforce_plan.PLAN_SYSTEM_PROMPT.casefold() or (
        "usd eur gbp" in workforce_plan.PLAN_SYSTEM_PROMPT.casefold()
    )


def test_plan_prompt_splits_metals_from_fx_and_also() -> None:
    """Metals + AUD are different boards; prompt must not merge them."""
    prompt = workforce_plan.PLAN_SYSTEM_PROMPT.casefold()
    assert "avustralya" in prompt
    assert "ve ayrıca" in prompt or "and also" in prompt
    assert "precious metals" in prompt or "altın gümüş" in prompt
    plan = workforce_plan.PlanResult(
        targets=[
            workforce_plan.PlanTarget(name="altın gümüş"),
            workforce_plan.PlanTarget(name="Avustralya doları"),
        ],
        workstreams=[
            workforce_plan.PlanWorkstream(
                goal="güncel fiyatlar",
                capabilities=["web_search"],
                target_names=["altın gümüş", "Avustralya doları"],
            )
        ],
        source="llm",
    )
    assert workforce_plan.lookup_target_names_from_plan(plan) == [
        "altın gümüş",
        "Avustralya doları",
    ]


def test_lookup_target_names_from_json_text() -> None:
    raw = (
        '{"targets":[{"name":"A"},{"name":"B"},{"name":"C"}],'
        '"workstreams":[{"goal":"compare","capabilities":["web_search"],'
        '"target_names":["A","B","C"],"depends_on":[]}]}'
    )
    assert workforce_plan.lookup_target_names_from_plan(raw) == ["A", "B", "C"]


def test_deck_renders_tables_instead_of_dropping_them() -> None:
    """A section that is a table must not become an empty slide.

    Live: "Sözleşme Yığını" and "Finansal Veriler" came out as a heading
    over 35 characters of nothing, while the .docx built from the same
    text carried both tables in full -- the deck skipped table blocks
    because the Word writer renders them from columns/rows.
    """
    import re

    from execution.langgraph_runtime.workforce import _html_deck_from_payload

    body = (
        "## Sozlesme Yigini\n\n"
        "| Tarih | Sozlesme | Kurum | Deger |\n"
        "|---|---|---|---|\n"
        "| Haziran 2023 | USSOCOM veri fuzyonu | ABD Ozel Harekat | 463 milyon |\n"
        "| Mayis 2024 | Maven IDIQ | DoD | 480 milyon |\n\n"
        "## Diger Bolum\n\n"
        "Duz metin burada yer alir ve yeterince uzundur.\n"
    )
    html = _html_deck_from_payload(
        title="Test", summary="", columns=[], rows=[], body=body
    )

    sections = re.findall(r"<section class='[^']*'>(.*?)</section>", html, re.S)
    table_slide = next(s for s in sections if "Sozlesme Yigini" in s)
    text = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", table_slide))

    # Every row reached the slide, as label/value pairs rather than pipes.
    assert "USSOCOM" in text and "463 milyon" in text
    assert "Maven IDIQ" in text and "480 milyon" in text
    assert "|" not in text
    assert len(text) > 150


def test_pptx_gets_the_full_body_like_html_does() -> None:
    """The deck needs the sections, and the summary has none.

    A model that returns a short summary alongside a long content built an
    eight-section HTML deck and a one-slide .pptx from the same answer,
    because the html branch read `body` and the pptx branch read `summary`.
    """
    import inspect

    from execution.langgraph_runtime import workforce

    src = inspect.getsource(workforce._write_artifact)
    pptx_branch = src[src.index('if kind == "pptx":') :]
    pptx_branch = pptx_branch[: pptx_branch.index("if kind == \"code\":")]
    assert "summary=body or summary" in pptx_branch
    assert "summary=summary or body" not in pptx_branch
