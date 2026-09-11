"""The golden regression bank: real prompts real people actually sent.

Why a bank rather than more unit tests
--------------------------------------
Both failures this pass exists to fix were found in production, not in
testing, and neither was exotic -- "give me the Excel file" and "build me a
page I can search this in" are ordinary asks. What made them expensive was
that nothing in the test suite described a COMPLETE user request: tests
covered routing, tools, and files separately, so a request whose input half
and output half were each individually supported could still be unsatisfiable
end to end, and no test would notice.

Each entry here is one whole request plus the properties its execution must
have. They are deliberately stated as properties, not as an expected literal
plan, so the bank keeps passing when the architecture changes and keeps
failing when the capability chain breaks.

Adding a prompt
---------------
Append a `GoldenPrompt`. Only `prompt_id`, `prompt` and `requires` are
mandatory:

    GoldenPrompt(
        prompt_id="mentor.some-new-case",
        prompt="what the user actually typed, verbatim, in their language",
        has_files=True,
        file_kinds=["pdf"],
        requires={FILES, ARTIFACTS},
        note="one line on what went wrong / what must not regress",
    )

`requires` is the important field: the capabilities WITHOUT WHICH the request
cannot be satisfied. Write it by asking "what would this run be unable to
produce if it lacked X", not by describing how you imagine it will run.

Fixtures live with the tests (test_golden_regressions.py), never on the live
network -- the mentor URL is not a dependency of this suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from capabilities import (
    ARTIFACTS,
    CODE_EXEC,
    FILES,
    WEB_FETCH,
    WEB_SEARCH,
)


@dataclass(frozen=True)
class GoldenPrompt:
    prompt_id: str
    prompt: str
    # What the request cannot be satisfied without.
    requires: frozenset[str] = field(default_factory=frozenset)
    # The subset of `requires` that must be derivable with NO model call --
    # facts about the request (a URL is present, files are attached, a
    # writable format was named as the output). Defaults to all of `requires`.
    #
    # The distinction is real and worth keeping honest: "this names a URL" is
    # a fact, while "this needs a web search" is a judgement about whether the
    # model already knows the answer. Only the first belongs in the
    # deterministic floor, so entries whose requirement is the second kind
    # (an ordinary lookup, a literature survey) set this explicitly and let
    # the router earn it.
    structurally_required: Optional[frozenset[str]] = None
    # What must NOT be pulled in -- the guard against a resolver that fixes
    # under-equipping by over-equipping everything.
    forbids: frozenset[str] = field(default_factory=frozenset)
    has_files: bool = False
    file_kinds: tuple[str, ...] = ()
    # False for requests that must stay on the single-executor path. A
    # sequential multi-tool chain is NOT a reason to orchestrate.
    allow_workforce: bool = False
    # True when the deliverable is a real file on disk.
    expects_artifact: bool = True
    # Why this prompt is in the bank.
    note: str = ""


BANK: tuple[GoldenPrompt, ...] = (
    # ---------------------------------------------------------------- CASE 1
    GoldenPrompt(
        prompt_id="mentor.url-to-excel",
        prompt=(
            "https://namazvakitleri.diyanet.gov.tr/tr-TR/9206/ankara-icin-"
            "namaz-vakti bu sayfaya git, aylık Ankara namaz vakitlerini çek, "
            "excel'e parse et ve bana excel dosyasını ver"
        ),
        requires=frozenset({WEB_FETCH, ARTIFACTS}),
        forbids=frozenset(),
        has_files=False,
        allow_workforce=False,
        expects_artifact=True,
        note=(
            "Routed to a search-only Research Agent. Web search worked; the "
            "Excel artifact could not be produced, so the job failed. The "
            "named URL requires web_fetch (search cannot substitute for a page "
            "you were handed), and the deliverable requires artifacts."
        ),
    ),
    GoldenPrompt(
        prompt_id="mentor.url-to-excel.english",
        prompt=(
            "Go to https://example.org/prayer-times/ankara , extract the "
            "monthly prayer time table, parse it into Excel and give me the "
            "Excel file."
        ),
        requires=frozenset({WEB_FETCH, ARTIFACTS}),
        forbids=frozenset(),
        allow_workforce=False,
        note="The same request in English -- capability resolution must not be language-specific.",
    ),
    # ---------------------------------------------------------------- CASE 2
    GoldenPrompt(
        prompt_id="mentor.xlsx-to-searchable-web",
        prompt=(
            "Bu xlsx dosyasındaki veriyi JSON'a dönüştür ve ekranda arama "
            "yapabilmem için HTML/CSS/JS oluştur"
        ),
        requires=frozenset({FILES, ARTIFACTS, CODE_EXEC}),
        forbids=frozenset(),
        has_files=True,
        file_kinds=("xlsx",),
        allow_workforce=False,
        expects_artifact=True,
        note=(
            "Routed to a read-only Document Agent. The XLSX could be read but "
            "the transformation, the code and the artifact were all outside "
            "that execution context, so it looped reading rows for ~430K "
            "tokens and failed. Reading is not enough: this needs code "
            "execution and artifact creation."
        ),
    ),
    GoldenPrompt(
        prompt_id="mentor.xlsx-to-searchable-web.english",
        prompt=(
            "Convert this data to JSON and create HTML/CSS/JS so I can search "
            "it on screen."
        ),
        requires=frozenset({FILES, ARTIFACTS, CODE_EXEC}),
        has_files=True,
        file_kinds=("xlsx",),
        allow_workforce=False,
        note="The same request in English.",
    ),
    # ------------------------------------------------- cheap must stay cheap
    GoldenPrompt(
        prompt_id="baseline.pdf-summary",
        prompt="Summarize the attached PDF in a few paragraphs.",
        requires=frozenset({FILES}),
        forbids=frozenset({ARTIFACTS, CODE_EXEC, WEB_FETCH}),
        has_files=True,
        file_kinds=("pdf",),
        allow_workforce=False,
        expects_artifact=False,
        note=(
            "The counterweight to both cases above. A resolver that fixes "
            "under-equipping by equipping everything has traded one failure "
            "for a more expensive one."
        ),
    ),
    GoldenPrompt(
        prompt_id="baseline.current-web-lookup",
        prompt="What is the latest stable version of Python?",
        requires=frozenset({WEB_SEARCH}),
        # Deliberately empty: whether a plain question needs a live lookup is
        # a judgement about what the model already knows, not a fact about the
        # request text. The router decides it; the floor must not guess.
        structurally_required=frozenset(),
        forbids=frozenset({ARTIFACTS, CODE_EXEC, FILES}),
        allow_workforce=False,
        expects_artifact=False,
        note="An ordinary lookup must remain one cheap search, not a research project.",
    ),
    GoldenPrompt(
        prompt_id="baseline.plain-question",
        prompt="Fotosentezin nasıl çalıştığını kısaca açıklar mısın?",
        requires=frozenset(),
        forbids=frozenset({ARTIFACTS, CODE_EXEC, FILES, WEB_FETCH}),
        allow_workforce=False,
        expects_artifact=False,
        note="Pure reasoning. No capability should be attached at all.",
    ),
    GoldenPrompt(
        prompt_id="baseline.xlsx-question-only",
        prompt="Bu excel dosyasında kaç satır var?",
        requires=frozenset({FILES}),
        forbids=frozenset({ARTIFACTS, WEB_FETCH}),
        has_files=True,
        file_kinds=("xlsx",),
        allow_workforce=False,
        expects_artifact=False,
        note=(
            "A spreadsheet upload alone must not imply code execution or "
            "artifacts -- only a request that transforms or produces does."
        ),
    ),
    # ------------------------------------------------------- other real work
    GoldenPrompt(
        prompt_id="survey.physical-ai",
        prompt=(
            "Review the important 2024-2026 work on Physical AI / "
            "Vision-Language-Action systems, and compare the approaches, "
            "datasets, results and limitations."
        ),
        requires=frozenset({WEB_SEARCH}),
        forbids=frozenset({FILES}),
        # Survey/literature detector is deterministic and now floors WEB_SEARCH.
        structurally_required=frozenset({WEB_SEARCH}),
        allow_workforce=True,
        expects_artifact=False,
        note=(
            "A survey still needs the web; there is no separate deep_research "
            "capability. web_search implies web_fetch."
        ),
    ),
    GoldenPrompt(
        prompt_id="artifact.csv-from-analysis",
        prompt=(
            "Analyze the uploaded sales spreadsheet, compute the monthly "
            "totals, and give me the results as a CSV I can download."
        ),
        requires=frozenset({FILES, ARTIFACTS, CODE_EXEC}),
        has_files=True,
        file_kinds=("xlsx",),
        allow_workforce=False,
        note="Upload + computation + file deliverable, with no URL involved.",
    ),
    GoldenPrompt(
        prompt_id="workforce.independent-technical-domains",
        prompt=(
            "Design the Postgres schema, configure the Kubernetes deployment, "
            "set up CI/CD, and run a security review. These are independent "
            "workstreams and each needs different expertise."
        ),
        requires=frozenset(),
        allow_workforce=True,
        expects_artifact=False,
        note=(
            "The one shape that genuinely justifies orchestration -- kept in "
            "the bank so the anti-escalation work above does not make "
            "WORKFORCE unreachable."
        ),
    ),
)


def floor_for(entry: GoldenPrompt) -> frozenset[str]:
    """What this entry's requirements must hold even with a useless router."""
    if entry.structurally_required is not None:
        return entry.structurally_required
    return entry.requires


def by_id(prompt_id: str) -> Optional[GoldenPrompt]:
    for entry in BANK:
        if entry.prompt_id == prompt_id:
            return entry
    return None


__all__ = ["BANK", "GoldenPrompt", "by_id"]
