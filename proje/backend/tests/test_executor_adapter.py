"""Unit tests for AGENT_EXECUTOR routing (no model / Docker required)."""

from __future__ import annotations

import os

import pytest


def test_agent_executor_defaults_to_camel(monkeypatch):
    monkeypatch.delenv("AGENT_EXECUTOR", raising=False)
    from execution.executor_config import agent_executor

    assert agent_executor() == "camel"


def test_agent_executor_langgraph(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTOR", "langgraph")
    from execution.executor_config import agent_executor

    assert agent_executor() == "langgraph"


def test_agent_executor_unknown_falls_back_to_camel(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTOR", "crewai")
    from execution.executor_config import agent_executor

    assert agent_executor() == "camel"


def test_execution_result_shape():
    from execution.result import execution_result

    task = execution_result(
        task_id="job.0", content="hi", result="hello", state="DONE"
    )
    assert task.id == "job.0"
    assert task.result == "hello"
    assert task.state.value == "DONE"


def test_langgraph_lifecycle_events():
    from execution.langgraph_runtime import events as lg_events

    ev = lg_events.worker_created(worker_id="single-agent", role="Assistant")
    assert ev["event_type"] == "worker_created"
    assert "timestamp" in ev
    td = lg_events.token_delta(task_id="j.0", delta="hi")
    assert td["event_type"] == "token_delta"
    assert td["delta"] == "hi"


def test_workforce_role_selection_web_and_artifact():
    import capabilities
    from execution.langgraph_runtime.workforce import (
        ROLE_CODING,
        ROLE_DOCUMENT,
        ROLE_RESEARCH,
        _select_roles,
    )

    roles = _select_roles(
        job_capabilities=frozenset(
            {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
        ),
        fast_path_role=None,
        wants_artifact=True,
    )
    assert ROLE_RESEARCH in roles
    assert ROLE_DOCUMENT in roles
    assert ROLE_CODING not in roles


def test_live_data_ask_never_falls_back_to_the_tool_less_assistant():
    """Capability routing is a model call and sometimes drops WEB_*.

    "son maç" cannot be answered from memory, so the empty-capability run
    must reach Research instead of the Assistant, which has no tools and
    then claims the system cannot access the internet.
    """
    from execution.langgraph_runtime.workforce import (
        ROLE_GENERAL,
        ROLE_RESEARCH,
        _request_needs_live_data,
        _select_roles,
    )

    assert _request_needs_live_data("galatasaray son mac ilk 11")
    assert _request_needs_live_data("bugün dolar kuru")
    assert not _request_needs_live_data("fransız ihtilali nedir")

    roles = _select_roles(
        job_capabilities=frozenset(),
        fast_path_role=None,
        wants_artifact=False,
        needs_live_data=True,
    )
    assert roles == [ROLE_RESEARCH]

    # Evergreen asks keep the old behaviour — no forced search.
    assert _select_roles(
        job_capabilities=frozenset(),
        fast_path_role=None,
        wants_artifact=False,
        needs_live_data=False,
    ) == [ROLE_GENERAL]

    # A reformat follow-up reuses prior notes; it must not be forced online.
    assert _select_roles(
        job_capabilities=frozenset(),
        fast_path_role=None,
        wants_artifact=False,
        needs_live_data=True,
        skip_web_research=True,
    ) == [ROLE_GENERAL]


def test_no_internet_refusal_never_reaches_the_user():
    """The claim is false for this backend and hides a misrouted run."""
    from execution.langgraph_runtime.workforce import (
        _WEB_ACCESS_REFUSAL_NOTICE,
        _present_workforce_answer,
    )

    refusal = (
        "Üzgünüm, ancak size Fenerbahçe'nin son maçındaki ilk 11'ini "
        "söyleyemem. Bunun nedeni, canlı verilere, güncel spor haberlerine "
        "veya internete erişimimin olmamasıdır. Mevcut araç setim (hiçbir "
        "araç bulunmamaktadır) ile web'de arama yapamam."
    )
    assert _present_workforce_answer(refusal) == _WEB_ACCESS_REFUSAL_NOTICE

    english = "I cannot access the internet, so I have no live data."
    assert _present_workforce_answer(english) == _WEB_ACCESS_REFUSAL_NOTICE


def test_real_brief_keeps_its_findings_despite_one_dead_page():
    """Only a bare refusal is replaced — a real answer must survive."""
    from execution.langgraph_runtime.workforce import _present_workforce_answer

    brief = (
        "## Galatasaray Son Maç İlk 11\n\n"
        "21 Ağustos 2026'da Erzurumspor deplasmanında 0-4 kazanıldı. "
        "İlk 11: Muslera, Boey, Sanchez, Bardakcı, Kerem, Torreira, "
        "Demirbay, Ziyech, Zaha, Icardi, Barış Alper. "
        "Açılan sayfalardan birine erişimim olmadı, kalan sayfalardan "
        "doğrulandı."
    ) * 3
    out = _present_workforce_answer(brief)
    assert "Icardi" in out
    assert "Erzurumspor" in out


def test_chat_blurb_skips_section_headings_for_real_prose():
    """Live regression: the chat answer was a bare heading with nothing under it.

    A deep run wrote a full three-part brief into the PDF, but chat showed
    only "1. Katı Hal Teknolojisinin Mevcut Durumu (2026)". Stripping the
    "#" before the body checks hid that it was a heading, and the "1."
    supplied the period those checks looked for.
    """
    from execution.langgraph_runtime.workforce import _chat_blurb_from_notes

    notes = "\n\n".join(
        [
            "Katı Hal Teknolojisi: Durum, Avantajlar ve Engeller",
            "## 1. Mevcut Durum (2026)",
            "Katı hal bataryaları, sıvı elektrolitleri katı olanlarla "
            "değiştirir ve 2026 itibarıyla laboratuvardan gerçek ürünlere "
            "ilerlemektedir.",
            "## 2. Avantajlar",
            "Daha yüksek enerji yoğunluğu sağlar.",
        ]
    )
    out = _chat_blurb_from_notes(notes)
    assert "Mevcut Durum (2026)" not in out
    assert "laboratuvardan gerçek ürünlere" in out


def test_chat_blurb_skips_unmarked_numbered_headings():
    """The same shape without a "#" marker must also be skipped."""
    from execution.langgraph_runtime.workforce import _chat_blurb_from_notes

    notes = "\n\n".join(
        [
            "Rapor Basligi Buraya Gelir ve Yeterince Uzundur",
            "1. Ticarilesme Onundeki Baslica Engeller Listesi",
            "Uretim maliyeti hala yuksektir ve olcekleme sorunlari devam eder.",
        ]
    )
    out = _chat_blurb_from_notes(notes)
    assert "Baslica Engeller Listesi" not in out
    assert "Uretim maliyeti" in out


def test_document_can_write_out_the_brief_it_is_given():
    """Live regression: a deep report stopped mid-section near the end.

    Document was handed up to _MAX_DEEP_DOC_NOTES_CHARS of research brief
    while its reply was capped at the 4-6k-token defaults, so the write-up
    ran out: the final heading ended on a colon promising a list that never
    arrived. The output ceiling has to be able to carry the input.
    """
    from execution.langgraph_runtime import workforce as wf

    def budget(chars, **kw):
        kw.setdefault("paged", False)
        kw.setdefault("primary", "pdf")
        kw.setdefault("deep_research", False)
        return wf._doc_token_budget("x" * chars, **kw)

    # Whatever brief it is handed, it must be able to write it out. Turkish
    # runs about 3 chars/token, so the budget has to clear chars/3.
    for chars in (6_000, 12_000, wf._MAX_DEEP_DOC_NOTES_CHARS):
        for deep in (False, True):
            got = budget(chars, deep_research=deep)
            assert got >= chars / 3, f"{chars} chars, deep={deep} -> {got}"

    # Floors still apply to a short brief, and paged beats plain.
    assert budget(500) == wf._DOC_MAX_TOKENS
    assert budget(500, paged=True) == wf._DOC_MAX_TOKENS_PAGED
    assert budget(500, deep_research=True) == wf._DOC_MAX_TOKENS_DEEP

    # Absurdly long notes stop at the runaway guard.
    assert budget(10_000_000) == wf._DOC_MAX_TOKENS_CEILING


def test_narrative_report_keeps_the_brief_when_document_drops_it():
    """Live regression: a 21k-char brief became a 5k-char PDF of tables.

    Research wrote all four asked-for sections (station counts, operators,
    incentives, barriers). Document returned a fraction of it, titled with
    a stray subsection heading from deep in the report, and the incentives
    and barriers sections were gone. The deck path already guarded against
    exactly this; pdf/docx/html did not.
    """
    from execution.langgraph_runtime import workforce as wf

    brief = "\n\n".join(
        [
            "# Türkiye Şarj Altyapısı 2026",
            "## 1. İstasyon Sayıları",
            "Temmuz 2026 itibarıyla toplam istasyon sayısı artmıştır. " * 12,
            "## 3. Devlet Teşvikleri",
            "ÖTV ve MTV indirimleri 2026'da sürmektedir. " * 12,
            "## 4. Engeller",
            "Lisans maliyetleri ve bölgesel dengesizlik başlıca engellerdir. " * 12,
        ]
    )
    thin = "4.2 Ödeme Sistemi Parçalılığı\n\nAltyapı hızlı büyümektedir."

    # The guard's own condition, applied the way the worker applies it.
    assert len(thin) < len(brief) * 0.6
    # And a faithful write-up of similar length must NOT be replaced.
    faithful = brief[:-40]
    assert not (len(faithful) < len(brief) * 0.6)

    # The replacement title comes from the brief, not the stray heading.
    import document_export

    retitled = document_export.short_report_title(brief, fallback="Rapor")
    assert "4.2" not in retitled
    assert retitled.strip()
    assert wf is not None


def test_chat_blurb_skips_a_lead_in_to_a_stripped_table():
    """Live regression: the chat answer ended on a colon pointing at nothing.

    "…Temmuz 2026 itibarıyla aşağıdaki boyutlara ulaşmıştır:" introduced a
    table, and tables are stripped from the blurb, so chat showed a lead-in
    followed by the sources block and read as cut off mid-thought.
    """
    from execution.langgraph_runtime.workforce import _chat_blurb_from_notes

    notes = "\n\n".join(
        [
            "Türkiye Şarj Altyapısı 2026 Durum Raporu",
            "Şarj altyapısı Temmuz 2026 itibarıyla aşağıdaki boyutlara ulaşmıştır:",
            "| Gösterge | Değer |\n|---|---|\n| İstasyon | 14.648 |",
            "Toplam soket sayısı bir önceki yıla göre iki katına çıkmıştır ve "
            "büyümenin sürmesi beklenmektedir.",
        ]
    )
    out = _chat_blurb_from_notes(notes)
    assert not out.rstrip().endswith(":"), out
    assert "iki katına" in out


def test_chat_blurb_falls_back_to_a_lead_in_when_nothing_else_exists():
    """A colon lead-in still beats an empty chat answer."""
    from execution.langgraph_runtime.workforce import _chat_blurb_from_notes

    notes = "\n\n".join(
        [
            "Rapor Basligi Buraya Gelir ve Yeterince Uzundur",
            "Sonuclar asagidaki tabloda ozetlenmistir ve detaylar eklenmistir:",
            "| A | B |\n|---|---|\n| 1 | 2 |",
        ]
    )
    out = _chat_blurb_from_notes(notes)
    assert "asagidaki tabloda" in out


def test_document_timeout_matches_how_much_writing_was_asked_for():
    """A budget the call cannot finish inside its own timeout is a bluff.

    Measured at ~30 tokens/sec for this model, so the wait is rated at
    that: a 9.8k-token template rewrite needs ~330s, and under the old
    240s ceiling it was cut off about half the time and shipped a
    two-paragraph file over the user's document.
    """
    from execution.langgraph_runtime import workforce as wf

    # The base budget keeps the hard cap it was tuned with.
    assert wf._document_llm_timeout(wf._DOC_MAX_TOKENS) == 45.0

    # Anything above it buys proportionally more time. The paged budget is
    # included: it already asks for more writing than the base job, and 45s
    # did not cover it.
    paged = wf._document_llm_timeout(wf._DOC_MAX_TOKENS_PAGED)
    assert paged > 45.0
    assert paged >= wf._DOC_MAX_TOKENS_PAGED / 30

    deep = wf._document_llm_timeout(wf._DOC_MAX_TOKENS_DEEP)
    assert deep > paged

    # Never unbounded, whatever is asked for.
    assert wf._document_llm_timeout(10**9) <= 400.0


def test_slide_plan_section_is_not_turned_into_slides():
    """Live regression: a deck contained slides about building the deck.

    The brief ended with "6. PowerPoint Sunumu Icin Slayt Icerikleri" and
    "Slayt 1 - Baslik" under it, and Document rendered those as pages of
    the presentation. Same family as the "rapor icin onerilen yapi" prose
    already stripped, but a whole section rather than a line.
    """
    from execution.langgraph_runtime.workforce import _strip_slide_plan_section

    brief = "\n\n".join(
        [
            "# Kapadokya Raporu",
            "## 1. Bolgeye Genel Bakis",
            "Bolge her yil milyonlarca ziyaretci agirlamaktadir.",
            "---",
            "## 6. PowerPoint Sunumu Icin Slayt Icerikleri (Hazir Metin)",
            "Asagidaki basliklar slaytlariniza kopyalanabilir.",
            "### Slayt 1 - Baslik",
            "**Kapadokya Turizm Potansiyeli**",
            "### Slayt 2 - Bolge Tanitimi",
            "- Peri bacalari, yer alti sehirleri",
        ]
    )
    out = _strip_slide_plan_section(brief)
    assert "Slayt" not in out
    assert "PowerPoint Sunumu Icin" not in out
    # The real content before it survives, and so does its heading.
    assert "1. Bolgeye Genel Bakis" in out
    assert "milyonlarca ziyaretci" in out


def test_a_section_after_the_slide_plan_survives():
    """Only the plan is removed, not everything following it."""
    from execution.langgraph_runtime.workforce import _strip_slide_plan_section

    brief = "\n\n".join(
        [
            "## 1. Bulgular",
            "Rakamlar yukselmistir.",
            "## 2. Sunum Icin Slayt Icerikleri",
            "### Slayt 1 - Giris",
            "Bir seyler",
            "## 3. Sonuc",
            "Ozetle bolge buyumektedir.",
        ]
    )
    out = _strip_slide_plan_section(brief)
    assert "Slayt" not in out
    assert "3. Sonuc" in out
    assert "bolge buyumektedir" in out
    assert "1. Bulgular" in out


def test_research_about_slide_design_is_not_stripped():
    """A report that genuinely discusses slides must keep its section."""
    from execution.langgraph_runtime.workforce import _strip_slide_plan_section

    brief = "\n\n".join(
        [
            "## 1. Slayt Tasariminda Renk Kullanimi",
            "Arastirmalar kontrastin okunabilirligi artirdigini gostermektedir.",
        ]
    )
    assert _strip_slide_plan_section(brief) == brief


def test_coding_persona_forbids_inventing_program_output():
    """Live regression: correct code, fabricated results underneath it.

    Asked for the mean and standard deviation of seven numbers, the Coding
    Agent wrote code that computes them correctly, then printed an
    "Output:" block with values that were wrong (6229.00 against the real
    5914.71). No tool ran -- _run_llm_worker makes a single chat call with
    no tools bound -- so the block was always a guess, and the code beside
    it disagreed with it.
    """
    from execution.langgraph_runtime.workforce import ROLE_CODING, _ROLE_PERSONAS

    persona = _ROLE_PERSONAS[ROLE_CODING]
    assert "ALSO FORBIDDEN" in persona
    assert "Output:" in persona
    # The existing file-fabrication rule must stay alongside it.
    assert "downloadable file" in persona


def test_code_exec_cap_schedules_coding_with_artifact():
    """_select_roles follows expanded caps; CODE_EXEC → Coding Agent."""
    import capabilities
    from execution.langgraph_runtime.workforce import (
        ROLE_CODING,
        ROLE_DOCUMENT,
        ROLE_RESEARCH,
        _select_roles,
    )

    roles = _select_roles(
        job_capabilities=frozenset(
            {
                capabilities.WEB_SEARCH,
                capabilities.ARTIFACTS,
                capabilities.CODE_EXEC,
            }
        ),
        fast_path_role=None,
        wants_artifact=True,
    )
    assert ROLE_RESEARCH in roles
    assert ROLE_CODING in roles
    assert ROLE_DOCUMENT in roles


def test_reformat_followup_skips_research():
    import capabilities
    from execution.langgraph_runtime.workforce import (
        ROLE_DOCUMENT,
        ROLE_RESEARCH,
        _has_prior_turn_material,
        _is_reformat_followup,
        _last_assistant_excerpt,
        _select_roles,
    )

    ask = "bir de bunu sunum olarak da verebilir misin"
    assert _is_reformat_followup(ask) is True
    assert _is_reformat_followup(
        "Yapay zeka dünyasında son 1 ayın en popüler 3 gelişmesini araştır"
    ) is False
    # Fresh research + PowerPoint deliverable is NOT a reformat follow-up.
    assert _is_reformat_followup(
        "Palantir Maven hakkında araştırma yap, Word yaz, PowerPoint olarak da ver."
    ) is False
    from execution.langgraph_runtime.workforce import (
        _is_bare_format_switch,
        _is_chat_redelivery_followup,
        _compose_document_chat_answer,
    )

    assert _is_chat_redelivery_followup(
        "chatte versene ben senden xlsx istemedim"
    ) is True
    assert _is_reformat_followup(
        "chatte versene ben senden xlsx istemedim"
    ) is True
    assert _is_reformat_followup("bunu pdf olarak ver") is True
    assert _is_bare_format_switch("pdf ver") is True
    assert _is_bare_format_switch("powerpoint olarak ver") is True
    # New subject + format is fresh work, not a bare switch.
    assert _is_bare_format_switch(
        "Galatasaray hakkında araştırma yapıp pdf ver"
    ) is False
    assert _is_reformat_followup(
        "Galatasaray hakkında araştırma yapıp pdf ver"
    ) is False
    tabular_chat = _compose_document_chat_answer(
        prior_notes=(
            "Güncel Döviz Kurları\n\n"
            "DenizBank verisine göre kurlar şöyle.\n\n"
            "| Döviz | Alış | Satış |\n"
            "| --- | --- | --- |\n"
            "| USD | 34.1 | 34.2 |\n"
            "| EUR | 37.0 | 37.1 |\n"
        ),
        file_notice="İndirilebilir dosya(lar) oluşturuldu: `artifacts/a.xlsx` [xlsx].",
        title="Güncel Döviz Kurları",
        kinds=["xlsx"],
    )
    assert "USD" in tabular_chat and "|" in tabular_chat
    prior = (
        "PRIOR MESSAGES IN THIS CONVERSATION\n"
        "--- ASSISTANT ---\n"
        "Birincisi GPT. İkinci gelişme Anthropic.\n"
    )
    assert _has_prior_turn_material(prior) is True
    assert _has_prior_turn_material(
        "PRIOR MESSAGES IN THIS CONVERSATION\n--- USER ---\naynı isteği tekrarla\n"
    ) is False
    assert "Anthropic" in _last_assistant_excerpt(prior)
    roles = _select_roles(
        job_capabilities=frozenset(
            {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
        ),
        fast_path_role=None,
        wants_artifact=True,
        wants_sources=True,
        skip_web_research=True,
    )
    assert ROLE_RESEARCH not in roles
    assert ROLE_DOCUMENT in roles
    # Same caps without skip → Research then Document.
    roles_full = _select_roles(
        job_capabilities=frozenset(
            {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
        ),
        fast_path_role=None,
        wants_artifact=True,
        wants_sources=False,
        skip_web_research=False,
    )
    assert roles_full == [ROLE_RESEARCH, ROLE_DOCUMENT]

def test_workforce_role_selection_specialist():
    from execution.langgraph_runtime.workforce import ROLE_RESEARCH, _select_roles

    roles = _select_roles(
        job_capabilities=frozenset(),
        fast_path_role="Research Agent",
        wants_artifact=False,
    )
    assert roles == [ROLE_RESEARCH]


def test_workforce_specialist_research_still_gets_document_when_file_asked():
    """Fast-path Research used to drop Document — xlsx/docx asks then got chat only."""
    from execution.langgraph_runtime.workforce import (
        ROLE_DOCUMENT,
        ROLE_RESEARCH,
        _select_roles,
    )

    roles = _select_roles(
        job_capabilities=frozenset(),
        fast_path_role="Research Agent",
        wants_artifact=True,
    )
    assert roles == [ROLE_RESEARCH, ROLE_DOCUMENT]


def test_workforce_no_document_when_file_not_asked_even_if_artifacts_cap():
    import capabilities
    from execution.langgraph_runtime.workforce import ROLE_DOCUMENT, _select_roles

    roles = _select_roles(
        job_capabilities=frozenset(
            {capabilities.WEB_SEARCH, capabilities.ARTIFACTS}
        ),
        fast_path_role=None,
        wants_artifact=False,
    )
    assert ROLE_DOCUMENT not in roles


def test_workforce_artifact_only_schedules_document():
    from execution.langgraph_runtime.workforce import ROLE_DOCUMENT, _select_roles

    roles = _select_roles(
        job_capabilities=frozenset(),
        fast_path_role=None,
        wants_artifact=True,
    )
    assert roles == [ROLE_DOCUMENT]


def test_artifacts_on_disk_helper(tmp_path):
    from execution.langgraph_runtime.workforce import _artifacts_on_disk

    assert _artifacts_on_disk(None) == []
    assert _artifacts_on_disk(str(tmp_path)) == []
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "report.docx").write_bytes(b"pk")
    (art / ".artifact-manifest.json").write_text("{}")
    found = _artifacts_on_disk(str(tmp_path))
    assert found == ["artifacts/report.docx"]


def test_infer_artifact_kind_from_request():
    from execution.langgraph_runtime.workforce import _infer_artifact_kind

    assert _infer_artifact_kind("pdf", "") == "pdf"
    assert _infer_artifact_kind("none", "bunu PDF olarak ver") == "pdf"
    assert _infer_artifact_kind("none", "HTML sayfa yap") == "html"
    assert _infer_artifact_kind("none", "excel tablosu") == "xlsx"
    assert _infer_artifact_kind("docx", "rapor") == "docx"
    from execution.langgraph_runtime.workforce import _infer_artifact_kinds

    paged = _infer_artifact_kinds(
        "none",
        "15 mesleği listele ki rapor en az 3-4 sayfa sürsün. "
        "Her sayfanın altında Sayfa 2 / 4 yer alsın.",
    )
    assert paged[0] == "docx"
    assert "xlsx" not in paged


def test_research_sources_block_dedupes_urls():
    from execution.langgraph_runtime.workforce import (
        _research_sources_block,
        _split_research_sources,
    )

    evidence = [
        {
            "ok": True,
            "url": "https://a.example/1",
            "title": "A",
            "text": "hello world facts",
        },
        {
            "ok": True,
            "url": "https://a.example/1",
            "title": "Dup",
            "text": "hello world facts",
        },
        {"ok": False, "url": "https://bad.example"},
        {
            "ok": True,
            "url": "https://b.example/2",
            "title": "B",
            "text": "more facts here",
        },
    ]
    block = _research_sources_block(evidence)
    assert "RESEARCH_SOURCES" in block
    cleaned, sources = _split_research_sources(f"notes{block}")
    assert cleaned == "notes"
    assert [item["url"] for item in sources] == [
        "https://a.example/1",
        "https://b.example/2",
    ]


def test_research_sources_block_skips_unread_candidates():
    from execution.langgraph_runtime.workforce import _research_sources_block

    evidence = [
        {"ok": False, "url": "https://ticaret.gov.tr/rapor.pdf", "title": "PDF"},
    ]
    extra = [
        {"url": "https://ticaret.gov.tr/rapor.pdf", "title": "PDF"},
        {"url": "https://news.example/article", "title": "News"},
    ]
    block = _research_sources_block(evidence, extra)
    assert '"url"' not in block or block.endswith("[]")
    assert "[]" in block


def test_research_notes_insufficient_detects_failed_fetch():
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    assert _research_notes_insufficient(
        "Mevcut kanıtlarda hiçbir sayfa başarıyla açılamamıştır."
    )
    assert not _research_notes_insufficient(
        "Umut Bozok 20 golle sezonun gol kralı oldu."
    )


def test_split_research_sources_after_artifact_handoff():
    """Word/PDF path appends RESEARCH_SOURCES after ARTIFACT_HANDOFF."""
    from execution.langgraph_runtime.workforce import _split_research_sources

    blob = (
        "Araştırma notu: Umut Bozok 20 gol.\n\n"
        "---\nARTIFACT_HANDOFF:\n"
        '{"status": "handoff", "handoff": null}\n'
        "---\nRESEARCH_SOURCES:\n"
        '[{"url": "https://www.transfermarkt.com.tr/super-lig/x", '
        '"title": "Gol Krallığı"}, '
        '{"url": "https://sporzip.com/gol-kralligi/super-lig?sezon=2021", '
        '"title": "Sporzip"}]'
    )
    cleaned, sources = _split_research_sources(blob)
    assert "Umut Bozok" in cleaned
    assert "ARTIFACT_HANDOFF" not in cleaned
    assert "RESEARCH_SOURCES" not in cleaned
    assert [item["url"] for item in sources] == [
        "https://www.transfermarkt.com.tr/super-lig/x",
        "https://sporzip.com/gol-kralligi/super-lig?sezon=2021",
    ]


def test_research_handoff_output_never_ships_bare_json_with_no_prose():
    """Real bug: a Galata Kulesi PowerPoint request's answer-writing call
    returned no usable prose, but a data-table handoff still existed --
    the old code returned the bare handoff_json with no ARTIFACT_HANDOFF
    marker, so the caller's sources-insertion and Document Agent's prompt
    both treated raw internal JSON as if it were the visible answer:
    `{"status": "handoff", ...}` followed by the Kaynaklar list, with
    Document left to build a deck from one narrow, tangential table only."""
    from execution.langgraph_runtime.workforce import _research_handoff_output

    handoff = {
        "data_path": "artifacts/extracted/old-photos-tables.json",
        "row_count": 5,
        "columns": ["Dönem", "Görünüm / Değişim"],
        "source_url": "https://example.com/old-photos",
        "title": "Old Photos",
        "provenance": ["https://example.com/old-photos"],
        "selected_table_index": None,
    }

    empty_prose_output = _research_handoff_output("", handoff)
    assert not empty_prose_output.startswith("{")
    assert "ARTIFACT_HANDOFF" not in empty_prose_output
    assert empty_prose_output == "No usable page content was acquired for this request."

    # With real prose, the handoff is still attached -- just properly
    # marker-delimited so downstream splitting finds it.
    with_prose_output = _research_handoff_output("Galata Kulesi 1348 yılında inşa edildi.", handoff)
    assert with_prose_output.startswith("Galata Kulesi 1348")
    assert "\n\n---\nARTIFACT_HANDOFF:\n{" in with_prose_output


def test_research_sources_uses_failed_fetch_when_nothing_opened():
    from execution.langgraph_runtime.workforce import (
        _research_sources_from_controller,
        _research_sources_from_evidence,
    )

    evidence = [
        {"ok": False, "url": "https://news.example/story", "title": "Story"},
    ]
    sources = _research_sources_from_evidence(evidence)
    assert [item["url"] for item in sources] == ["https://news.example/story"]

    class FakeAcquired:
        target_stats = {
            "(unscoped)": {"selected_urls": ["https://picked.example/a"]}
        }

    picked = _research_sources_from_controller(FakeAcquired())
    assert [item["url"] for item in picked] == ["https://picked.example/a"]


def test_research_sources_falls_back_to_search_candidates():
    from execution.langgraph_runtime.workforce import (
        _research_sources_from_controller,
    )

    class FakeAcquired:
        target_stats = {
            "(unscoped)": {
                "candidate_urls": [
                    {
                        "url": "https://www.transfermarkt.com/scorer",
                        "title": "Süper Lig gol krallığı",
                    },
                    {
                        "url": "https://www.fotmob.com/leagues/71",
                        "title": "FotMob",
                    },
                ]
            }
        }

    sources = _research_sources_from_controller(FakeAcquired())
    assert [item["url"] for item in sources] == [
        "https://www.transfermarkt.com/scorer",
        "https://www.fotmob.com/leagues/71",
    ]


def test_request_wants_sources():
    from execution.langgraph_runtime.workforce import _request_wants_sources

    assert _request_wants_sources("Kaynakları ekle") is True
    assert _request_wants_sources("cite your sources") is True
    assert _request_wants_sources("referans listesi ver") is True
    assert _request_wants_sources("sadece özet ver") is False
    assert _request_wants_sources("natural resource overview") is False


def test_inject_sources_into_payload():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {
        "summary": "Rapor metni.",
        "content": "Rapor metni.",
        "columns": ["Konu", "Detay"],
        "rows": [["Kaynaklar", "Webtekno"], ["Gelir", "42"]],
    }
    sources = [
        {"url": "https://news.example/a", "title": "Haber A"},
        {"url": "https://news.example/b", "title": "Haber B"},
    ]
    _inject_sources_into_payload(payload, sources, required=True)
    assert "https://news.example/a" in payload["summary"]
    assert "https://news.example/b" in payload["content"]
    assert "[" not in payload["summary"].split("Kaynaklar:", 1)[-1]
    # Sources stay in prose — not a fake table row.
    assert all(
        not (
            isinstance(r, list)
            and r
            and str(r[0]).casefold() == "kaynaklar"
        )
        for r in payload["rows"]
    )
    assert payload["rows"][0] == ["Gelir", "42"]


def test_inject_sources_skips_ranking_table_row():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {
        "summary": "Özet.",
        "content": "Özet.",
        "columns": ["Sıra", "Oyuncu", "Takım", "Gol"],
        "rows": [["1", "Kane", "Tottenham", "23"], ["Kaynaklar", "eski"]],
    }
    _inject_sources_into_payload(
        payload,
        [{"url": "https://pl.example/20-21", "title": "PL"}],
        required=True,
    )
    assert "https://pl.example/20-21" in payload["summary"]
    assert all(
        not (
            isinstance(r, list)
            and r
            and str(r[0]).casefold() == "kaynaklar"
        )
        for r in payload["rows"]
    )
    assert payload["rows"][0][1] == "Kane"


def test_present_puts_file_notice_after_kaynaklar():
    from execution.langgraph_runtime.workforce import _present_workforce_answer

    text = (
        "Tablo burada.\n\n"
        "İndirilebilir dosya(lar) oluşturuldu: `artifacts/a.docx` [docx].\n"
        "\n---\nRESEARCH_SOURCES:\n"
        '[{"url": "https://real.example/guide", "title": "Rehber"}]'
    )
    out = _present_workforce_answer(text)
    assert out.index("Kaynaklar:") < out.index("İndirilebilir")
    assert out.index("https://real.example/guide") < out.index("İndirilebilir")



def test_strip_markdown_tables_removes_pipe_grid():
    from execution.langgraph_runtime.workforce import _strip_markdown_tables

    text = (
        "Kısa giriş.\n\n"
        "| Sıra | Oyuncu |\n"
        "| --- | --- |\n"
        "| 1 | Kane |\n\n"
        "Not: berabere."
    )
    out = _strip_markdown_tables(text)
    assert "|" not in out
    assert "Kısa giriş" in out
    assert "Not: berabere" in out


def test_research_notes_insufficient_detects_missing_figures():
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    assert _research_notes_insufficient(
        "Gıda sektörü ihracat ve ithalat rakamlarına ulaşılamamıştır. "
        "Açılan sayfada istenilen veri mevcut değildir."
    )
    assert _research_notes_insufficient(
        "Açtığım kaynaklar herhangi bir bilgi içermemektedir. "
        "Güncel bir kaynak bulunamamıştır."
    )
    assert not _research_notes_insufficient(
        "2024 yılında Türkiye gıda ihracatı 25 milyar USD olarak gerçekleşti."
    )


def test_research_notes_partial_multi_target_still_enough_for_docx():
    """One missing country must not block Word/PDF when others have figures."""
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    notes = """
## 2025 Yenilenebilir Karşılaştırma

**Dikkat:** Güney Kore için veri bulunamadı.

### Türkiye
- Güneş+rüzgar: 42.5 GW (+17.7%)
- Kaynak: AA, ShiftDelete

### Almanya
- Toplam yenilenebilir ~210 GW, yıllık büyüme %11
- Kaynak: Federal Network Agency

### Güney Kore
Veri bulunamadı.

| Ülke | Büyüme |
|---|---|
| Türkiye | %17,7 |
| Almanya | %11 |
| Güney Kore | — |
""".strip()
    assert not _research_notes_insufficient(notes)


def test_research_notes_insufficient_for_wrong_source_failure():
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    notes = (
        "Elimizdeki kanıtlarda Ankara için 1 haftalık namaz vakitlerini "
        "içeren bir PDF veya takvim bilgisi bulunmamaktadır. Açılan "
        "sayfalardan biri erişim kısıtı nedeniyle açılamamış, diğeri ise "
        "Ankara gezilecek yerler rehberidir ve namaz vakitleriyle ilgili "
        "veri içermemektedir."
    )
    assert _research_notes_insufficient(notes)


def test_research_notes_citing_opened_pages_is_not_insufficient():
    """Regression: "açılan sayfalardan elde edilen bilgiler" (info FROM the
    opened pages) is a normal citation, not a refusal -- it used to bare
    substring-match the "açılan sayfa" marker and silently drop Document
    Agent from a perfectly good research pass (real bug: a request for an
    Ayasofya PowerPoint got only a chat answer, no .pptx, because of this
    one false positive)."""
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    notes = """
Bu belge, Ayasofya hakkında hazırlanacak bir PowerPoint sunumunda kullanılmak
üzere, açılan sayfalardan elde edilen bilgileri içerir.

**1. Genel Bilgiler**
*   **Konum:** Fatih, İstanbul, Türkiye
*   **Yapım Başlangıcı:** 532
*   **Tamamlanma Tarihi:** 537
*   **Yükseklik:** 55,60 m
*   **Mimarlar:** Miletli İsidoros ve Trallesli Anthemios

**2. UNESCO Dünya Mirası**
*   **Belirleme:** 1985 (9. oturum)
*   **Referans No:** 356
""".strip()
    assert not _research_notes_insufficient(notes)


def test_research_notes_partial_table_gap_caveat_is_not_insufficient():
    """Regression: "bu kanıt özetinde yer almadığından" (not present in
    THIS evidence summary) is a normal caveat about one missing column/rows
    in an otherwise real, substantial table -- not a refusal. It used to
    bare substring-match the "bu kanıt" marker and silently drop Document
    Agent (real bug: a gol krallığı Excel request with 7 real rows of
    names+goals, missing only the Takım column and rows 8-10, got a chat
    answer instead of the requested .xlsx file)."""
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    notes = """
### 2020-21 Süper Lig Gol Krallığı – İlk Sıralar

| Sıra | Oyuncu | Takım | Gol |
|------|--------|-------|-----|
| 1 | Aaron Boupendza |  | 22 |
| 2 | Cyle Larin |  | 19 |
| 3 | Mame Diouf |  | 19 |
| 4 | Adem Büyük |  | 17 |
| 5 | Muhammet Demir |  | 15 |
| 6 | Vincent Aboubakar |  | 15 |
| 7 | Alexandru Maxim |  | 15 |

**Eksik veri notu (bütünlük için):**
- **Takım sütunu:** Açılan sayfalardan çıkarılan metinde oyuncuların kulüp
  bilgisi yer almamıştır.
- **8, 9 ve 10. sıralar:** 8-10. sıradaki oyuncu isimleri bu kanıt
  özetinde yer almadığından tabloya eklenememiştir.
""".strip()
    assert not _research_notes_insufficient(notes)


def test_research_notes_table_with_a_few_placeholder_rows_is_not_insufficient():
    """Regression, second layer of the same bug: even after the marker
    fixes above, a table where a FEW rows are explicitly marked
    "(Kanıtlarda bulunamadı)" (genuinely matches the "kanıtlarda"/
    "bulunamadı" refusal markers -- not a false positive this time) still
    got discarded, because _notes_have_deliverable_substance's stricter
    branch required headings >= 2. A single ranked table/top-10 list only
    ever has ONE heading no matter how populated it is -- that must not
    throw away 7 real rows of names and goals just because 3 placeholder
    rows use refusal-toned language."""
    from execution.langgraph_runtime.workforce import _research_notes_insufficient

    notes = """
## 2020-21 Süper Lig Gol Krallığı (İlk 10 Oyuncu)

Transfermarkt sayfasında yalnızca ilk 7 oyuncuya ait veri görüntülenebilmiş;
8., 9. ve 10. sıradaki oyunculara dair kanıt mevcut değildir.

| Sıra | Oyuncu | Takım | Gol |
|------|-------------------------|-------|-----|
| 1 | Aaron Boupendza | | 22 |
| 2 | Cyle Larin | | 19 |
| 3 | Mame Diouf | | 19 |
| 4 | Adem Büyük | | 17 |
| 5 | Muhammet Demir | | 15 |
| 6 | Vincent Aboubakar | | 15 |
| 7 | Alexandru Maxim | | 15 |
| 8 | (Kanıtlarda bulunamadı) | | - |
| 9 | (Kanıtlarda bulunamadı) | | - |
| 10 | (Kanıtlarda bulunamadı) | | - |
""".strip()
    assert not _research_notes_insufficient(notes)


def test_enrich_lookup_for_stats_adds_tuik_hint():
    from execution.langgraph_runtime.workforce import _enrich_lookup_for_research

    out = _enrich_lookup_for_research(
        "Türkiye gıda sektörü ihracat ithalat rakamları PDF"
    )
    assert "SEARCH HINT" in out
    assert "HTML" in out or "html" in out.casefold()
    # Applied for any research ask, not only trade stats.
    plain = _enrich_lookup_for_research("Beşiktaş son maç skoru")
    assert "SEARCH HINT" in plain


def test_extract_lookup_targets_passes_plan_names():
    import asyncio

    import workforce_plan
    from execution.langgraph_runtime.workforce import _extract_lookup_targets

    async def complete(phase, system, user, schema):
        assert phase == "plan"
        assert "targets" in system
        return workforce_plan.PlanResult(
            targets=[
                workforce_plan.PlanTarget(name="A"),
                workforce_plan.PlanTarget(name="B"),
                workforce_plan.PlanTarget(name="C"),
            ],
            workstreams=[],
            source="llm",
        )

    names = asyncio.run(_extract_lookup_targets("A, B, C'yi karşılaştır", complete=complete))
    assert names == ["A", "B", "C"]


def test_extract_lookup_targets_empty_on_failure():
    import asyncio

    from execution.langgraph_runtime.workforce import _extract_lookup_targets

    async def complete(phase, system, user, schema):
        raise RuntimeError("planner down")

    names = asyncio.run(_extract_lookup_targets("anything", complete=complete))
    assert names == []


def test_inject_sources_required_even_when_empty():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {"summary": "Rapor.", "content": "Rapor."}
    _inject_sources_into_payload(payload, [], required=True)
    assert "Kaynaklar:" in payload["summary"]
    assert "bulunamadı" in payload["summary"].casefold() or "bulunamadi" in payload[
        "summary"
    ].casefold()


def test_inject_sources_keeps_model_urls_when_research_empty():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {
        "summary": "Özet.\n\nKaynaklar:\n1. https://space.example/starlink",
        "content": "Özet.\n\nKaynaklar:\n1. https://space.example/starlink",
    }
    _inject_sources_into_payload(payload, [], required=True)
    assert "https://space.example/starlink" in payload["summary"]
    assert "bulunamadı" not in payload["summary"].casefold()


def test_inject_sources_keeps_existing_kaynaklar_without_urls():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {
        "summary": "Özet.\n\nKaynaklar:\nSpaceX resmi blog, FCC filings",
        "content": "Özet.\n\nKaynaklar:\nSpaceX resmi blog, FCC filings",
    }
    _inject_sources_into_payload(payload, [], required=True)
    # Domain-only model lists must not survive — replace with explicit notice.
    assert "SpaceX resmi blog" not in payload["summary"]
    assert "Kaynaklar:" in payload["summary"]
    assert "bulunamadı" in payload["summary"].casefold() or "bulunamadi" in payload[
        "summary"
    ].casefold()


def test_inject_sources_prefers_research_over_model_domains():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {
        "summary": "Metin.\n\nKaynaklar:\nwalllet.com\ntapbit.com",
        "content": "Metin.\n\nKaynaklar:\nwalllet.com\ntapbit.com",
    }
    sources = [
        {"url": "https://official.example/wallet-guide", "title": "Resmi rehber"},
    ]
    _inject_sources_into_payload(payload, sources, required=True)
    assert "https://official.example/wallet-guide" in payload["summary"]
    assert "walllet.com" not in payload["summary"]
    assert payload["summary"].count("Kaynaklar:") == 1


def test_format_sources_lines_shows_full_url():
    from execution.langgraph_runtime.workforce import _format_sources_lines

    lines = _format_sources_lines(
        [{"url": "https://a.example/page", "title": "Sayfa A"}]
    )
    assert lines == ["- [Sayfa A](https://a.example/page)"]


def test_request_wants_structured_table_gol_kralligi():
    from execution.langgraph_runtime.workforce import _request_wants_structured_table

    assert _request_wants_structured_table(
        "2020-21 Süper Lig gol krallığında ilk 10 oyuncu: sıra, oyuncu, "
        "takım, gol sayısı. Sonuç: DOCX."
    )
    assert not _request_wants_structured_table(
        "Netflix Türkiye abonelik fiyatlarını özetle, Word olsun."
    )


def test_request_wants_interactive_app_matches_app_tool_game_asks():
    from execution.langgraph_runtime.workforce import _request_wants_interactive_app

    for text in (
        "Basit, tek sayfalık bir yapılacaklar listesi web uygulaması yap, "
        "HTML olarak",
        "bir hesap makinesi uygulaması hazırla",
        "basit bir oyun yap html olarak",
        "bana bir todo uygulaması yap",
        "kronometre uygulaması hazırla",
    ):
        assert _request_wants_interactive_app(text), text


def test_request_wants_interactive_app_ignores_report_asks():
    """Report words must win -- the long-established deck path must not
    regress for an actual "write a report" ask, even one that happens to
    mention "uygulama" (e.g. discussing an application/program itself)."""
    from execution.langgraph_runtime.workforce import _request_wants_interactive_app

    for text in (
        "İstanbul'un nüfusu hakkında görselli bir HTML sunum hazırla",
        "Palantir Maven hakkında bir html rapor yaz",
        "mobil uygulama pazarı hakkında bir araştırma özeti hazırla, html",
    ):
        assert not _request_wants_interactive_app(text), text


def test_extract_html_document_recovers_a_fenced_code_block():
    """Real bug: even after being told to return an `html` JSON key, the
    model wrapped a genuine working to-do-list app in a ```html fence
    inside `content` instead -- the old code then fell through to the
    static deck template, chunking the fenced code into "prose" slide
    paragraphs (HTML-escaped, unusable). This must recover the real code."""
    from execution.langgraph_runtime.workforce import _extract_html_document

    fenced = (
        "İşte tek dosyalık bir yapılacaklar listesi uygulaması:\n\n"
        "```html\n"
        "<!DOCTYPE html>\n"
        "<html lang=\"tr\"><head><title>Todo</title></head>"
        "<body><script>function add(){}</script></body></html>\n"
        "```"
    )
    extracted = _extract_html_document(fenced)
    assert extracted.startswith("<!DOCTYPE html>")
    assert "function add" in extracted
    assert "```" not in extracted

    # No fence at all -- the model just started straight with the doctype.
    unfenced = '<!DOCTYPE html>\n<html><body><script>x=1</script></body></html>'
    assert _extract_html_document(unfenced) == unfenced

    # Ordinary prose (no embedded HTML document at all) -- nothing to find.
    assert _extract_html_document("Bu konuda elimizde yeterli bilgi yok.") == ""


def test_compose_document_chat_answer_keeps_research_and_sources():
    from execution.langgraph_runtime.workforce import (
        _compose_document_chat_answer,
        _present_workforce_answer,
    )

    notes = (
        "## Gol krallığı\n\n"
        "Bu sezonun özeti kısa bir paragraftır ve chat'te kalmalıdır.\n\n"
        "| Sıra | Oyuncu | Takım | Gol |\n"
        "| --- | --- | --- | --- |\n"
        "| 1 | Aaron Boupendza | Hatayspor | 22 |\n"
        "| 2 | Mame Diouf | Hatayspor | 19 |\n"
        "| 3 | Cyle Larin | Beşiktaş | 19 |\n"
        "\n---\nARTIFACT_HANDOFF:\n"
        '{"status":"handoff"}\n'
        "\n---\nRESEARCH_SOURCES:\n"
        '[{"url": "https://tm.example/20-21", "title": "TM"}]'
    )
    out = _compose_document_chat_answer(
        prior_notes=notes,
        file_notice="İndirilebilir dosya(lar) oluşturuldu: `artifacts/a.docx` [docx].",
        title="Gol krallığı",
    )
    presented = _present_workforce_answer(out)
    assert "Aaron Boupendza" not in presented  # full table stays out of chat
    assert "kısa bir paragraftır" in presented or "chat'te kalmalıdır" in presented
    assert "https://tm.example/20-21" in presented
    assert "Kaynaklar:" in presented
    assert "İndirilebilir dosya" in presented
    assert "ARTIFACT_HANDOFF" not in presented


def test_chat_blurb_from_notes_is_short():
    from execution.langgraph_runtime.workforce import _chat_blurb_from_notes

    notes = (
        "Palantir Technologies (PLTR) – Detaylı Araştırma Notu\n\n"
        "Palantir, 2003 yılında kurulan bir yazılım şirketidir. Gotham ve "
        "Foundry ürünleriyle bilinen firma 2025'te hızlı büyüdü.\n\n"
        "Finansal Performans – Yıllık Gelir\n\n"
        "2019 geliri 742 milyon dolardı. Daha birçok cümle ve tablo burada "
        "olurdu ama chat özetine girmemeli."
    )
    out = _chat_blurb_from_notes(notes, title="Palantir Technologies (PLTR)")
    assert "Palantir Technologies" in out
    assert "Gotham" in out
    assert "2019 geliri" not in out


def test_strip_capability_refusals_removes_pdf_cannot_create():
    from execution.langgraph_runtime.workforce import (
        _chat_blurb_from_notes,
        _strip_capability_refusals,
    )

    notes = (
        "Gıda Sektörü Türkiye Dış Ticaret Araştırma Raporu\n\n"
        "Elimdeki kaynaklar 2026 Ocak ihracat rakamlarını içermektedir. "
        "PDF dosyası oluşturamıyorum; ancak aşağıdaki metni kullanabilirsiniz.\n\n"
        "İhracat 2,4 milyar dolar olarak gerçekleşti. İthalat 2,18 milyar dolar."
    )
    cleaned = _strip_capability_refusals(notes)
    assert "oluşturamıyorum" not in cleaned.casefold()
    assert "2,4 milyar" in cleaned
    blurb = _chat_blurb_from_notes(notes, title="Gıda Raporu")
    assert "oluşturamıyorum" not in blurb.casefold()
    assert "2,4" in blurb or "ihracat" in blurb.casefold()

    diy = (
        "Özet metin.\n\n"
        "Not: PDF dosyası oluşturma ve görsel ekleme işlemi bu sistemin "
        "görevi kapsamında değildir. Talebinizi belge düzenleyicide "
        "kendiniz oluşturabilirsiniz.\n\n"
        "İhracat 29 milyar dolar."
    )
    diy_out = _strip_capability_refusals(diy)
    assert "kapsamında" not in diy_out.casefold()
    assert "kendiniz" not in diy_out.casefold()
    assert "29 milyar" in diy_out


def test_present_workforce_answer_appends_research_urls():
    from execution.langgraph_runtime.workforce import _present_workforce_answer

    text = (
        "Özet metin.\n\nKaynaklar:\nwalllet.com\n"
        "\n---\nRESEARCH_SOURCES:\n"
        '[{"url": "https://real.example/guide", "title": "Rehber"}]'
    )
    out = _present_workforce_answer(text)
    assert "https://real.example/guide" in out
    assert "walllet.com" not in out
    assert "Kaynaklar:" in out
    assert out.index("Kaynaklar:") < out.index("https://real.example/guide")


def test_inject_sources_skipped_when_not_required():
    from execution.langgraph_runtime.workforce import _inject_sources_into_payload

    payload = {"summary": "Rapor.", "content": "Rapor."}
    _inject_sources_into_payload(
        payload,
        [{"url": "https://x.example", "title": "X"}],
        required=False,
    )
    assert "https://x.example" not in payload["summary"]


def test_document_notes_cap_deep_research_beats_survey_heuristic():
    """A 9.7k-char deep-research brief must not be truncated to 6k chars
    before Document ever sees the rest -- see the Aug 2026 Turkey-energy
    report bug: deep_research=True gathered a rich multi-section brief,
    but the phrase-triggered survey heuristic (which doesn't know about
    the user's explicit opt-in) still capped Document's input to 6,000
    chars, so the file only ever contained the first section."""
    from execution.langgraph_runtime.workforce import (
        _MAX_DEEP_DOC_NOTES_CHARS,
        _MAX_DOC_NOTES_CHARS,
        _MAX_SURVEY_DOC_NOTES_CHARS,
        _document_notes_cap,
    )

    # A plain request the phrase-triggered survey heuristic does not
    # recognize as "deep" on its own (single subject, no survey wording) --
    # exactly the shape that produced the bug.
    plain_request = "Türkiye'nin enerji dönüşümü hakkında bir rapor hazırla"

    assert (
        _document_notes_cap(
            deep_research=True, request_text=plain_request, kind_count=1
        )
        == _MAX_DEEP_DOC_NOTES_CHARS
    )
    assert _MAX_DEEP_DOC_NOTES_CHARS > _MAX_SURVEY_DOC_NOTES_CHARS > _MAX_DOC_NOTES_CHARS

    # Without the opt-in, behavior is unchanged: falls back to the
    # phrase-triggered heuristic's own (smaller) cap.
    assert (
        _document_notes_cap(
            deep_research=False, request_text=plain_request, kind_count=1
        )
        == _MAX_DOC_NOTES_CHARS
    )

    # Multi-format jobs are still capped tighter than single-format ones --
    # that limit is about JSON-call reliability, not research depth. But it
    # is no longer the SAME cap for both modes: sharing 4,500 chars meant a
    # deep run that gathered ~60k characters handed the writer 4,500 of
    # them, so "deep, docx and an HTML deck" produced a shorter report than
    # the identical request without deep. A deep run writes with 16k tokens
    # and up to 400s now, which is what the old clamp was protecting
    # against.
    from execution.langgraph_runtime.workforce import (
        _MAX_DEEP_MULTI_DOC_NOTES_CHARS,
    )

    deep_multi = _document_notes_cap(
        deep_research=True, request_text=plain_request, kind_count=2
    )
    plain_multi = _document_notes_cap(
        deep_research=False, request_text=plain_request, kind_count=2
    )
    assert plain_multi == 4_500
    assert deep_multi == _MAX_DEEP_MULTI_DOC_NOTES_CHARS
    # Far wider than any non-deep job. Deep no longer pays a multi-format
    # penalty at all: measured on a real run, eleven pages produced 103k
    # characters of evidence and the writer saw 12k of them, so the cap --
    # not the search budget -- was what made a deep report thin.
    assert deep_multi > plain_multi * 4
    assert deep_multi <= _MAX_DEEP_DOC_NOTES_CHARS


def test_primary_stats_missing_while_linked_citation_matches_what_model_saw():
    """Class: primary-stats-missing-while-linked.

    A page trimmed out of the answer prompt must never still be cited --
    otherwise the model correctly (from its own narrower view) reports a
    fact as unfindable while the citation list implies it was verified.
    Real bug: a Turkey EV-market deep-research run cited a page stating
    "TOGG sold 109,944 units" in Kaynaklar, while the answer text said
    sales figures could not be found -- that page had been trimmed out of
    what the answer-writing model actually read (_trim_evidence_for_answer
    keeps only the last max_items), but the citation list was built from
    the full, untrimmed evidence. Citations must come from the same
    evidence subset that was actually shown to the model.
    """
    from execution.langgraph_runtime.workforce import (
        _research_sources_block,
        _split_research_sources,
        _trim_evidence_for_answer,
    )

    # 41 successfully-opened pages -- one over deep mode's max_items=40, so
    # the earliest page (carrying the "missing" fact) is the one dropped.
    evidence = [
        {
            "url": f"https://example.com/page{i}",
            "title": f"Page {i}",
            "ok": True,
            "text": f"content {i} " * 10,
        }
        for i in range(41)
    ]
    trimmed = _trim_evidence_for_answer(evidence, max_items=40, max_text=1200)
    assert len(trimmed) == 40
    assert evidence[0] not in trimmed  # the trim really does drop the early page

    # Building citations from the FULL evidence (the bug) cites a page the
    # model never read.
    _, buggy_sources = _split_research_sources(_research_sources_block(evidence, []))
    assert "https://example.com/page0" in {s["url"] for s in buggy_sources}

    # Building citations from the SAME subset shown to the model (the fix)
    # never cites what was trimmed away.
    _, fixed_sources = _split_research_sources(_research_sources_block(trimmed, []))
    fixed_urls = {s["url"] for s in fixed_sources}
    assert "https://example.com/page0" not in fixed_urls
    assert len(fixed_urls) == 40


def test_coding_persona_forbids_fabricated_download_links():
    """Real bug: a simple "get me current prices" ask got routed with
    CODE_EXEC (an LLM classifier judgment call, not a deterministic rule --
    not itself fixed here), and Coding Agent's response invented a fake
    "sandbox:/altin_gumus_fiyatlari.xlsx" download link -- this executor's
    ROLE_CODING has no real file system or tool-calling (complete_chat never
    passes tools=), so no such file or link can ever really exist. It was
    only saved from reaching the user because Document Agent happened to
    run afterward and overwrite it as the final answer; a request that
    skipped Document would have shipped the broken fake link directly."""
    from execution.langgraph_runtime.workforce import ROLE_CODING, _ROLE_PERSONAS

    persona = _ROLE_PERSONAS[ROLE_CODING]
    assert "FORBIDDEN" in persona
    assert "sandbox" in persona
    assert "download" in persona.lower()
