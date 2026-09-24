"""The Workforce's lookup plan: which named subjects need their own search.

The LangGraph Research step asks the model for a short JSON plan
(PLAN_SYSTEM_PROMPT -> PlanResult) and reads only its target names:
independent lookup subjects each get their own SEARCH, an empty list means
one search for one subject.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

MAX_PLAN_TARGETS = 8


class PlanTarget(BaseModel):
    name: str = ""


class PlanWorkstream(BaseModel):
    goal: str = ""
    capabilities: list[str] = Field(default_factory=list)
    target_names: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)


class PlanResult(BaseModel):
    targets: list[PlanTarget] = Field(default_factory=list)
    workstreams: list[PlanWorkstream] = Field(default_factory=list)
    source: str = "llm"


PLAN_SYSTEM_PROMPT = (
    "You plan ONE user request for a workforce. Respond with JSON only: "
    '{"targets": [{"name": "..."}], "workstreams": [{"goal": "...", '
    '"capabilities": ["web_search"|"artifacts"|"files"|"code_exec"], '
    '"target_names": ["..."], "depends_on": []}]}.\n\n'
    "targets lists every distinct lookup subject that needs its own facts "
    "(cities, teams, products, people, sites, time ranges that are separate "
    "lookups). Empty when the request is one subject or a design/assessment "
    "whose parts constrain each other.\n\n"
    "workstreams are capability responsibilities, not the final combined "
    "answer. Independent lookups of the SAME kind that need SEPARATE sources "
    "(three cities' weather, three countries' ministry stats, three teams) "
    "MUST each appear in target_names even if you emit a single web "
    "workstream. Do not merge those away because the user asked for one "
    "comparison or one table. Artifact production is a separate "
    "workstream that depends on the gathering streams.\n\n"
    "Do NOT split items that are normally one quote/board/page: same-day "
    "FX rates of the SAME class (USD/EUR/GBP together), a single standings "
    "table, or one market ticker list stay ONE subject — leave targets "
    "empty (or one umbrella name). Different boards MUST stay separate: "
    "precious metals vs FX, weather vs sports, one country vs another. "
    "Phrases like 've ayrıca' / 'and also' / 'plus' mark an added subject "
    "that needs its own target when it is not on the same page type.\n"
    "Do NOT split facets of one architecture/schema/design. Do not answer "
    "the user. Keep the user's language in each goal.\n\n"
    "Examples:\n"
    '- "Süper Lig puan durumu" -> targets [{"name":"Süper Lig"}], one web '
    "workstream.\n"
    '- "güncel dolar euro pound kurları" / "USD EUR GBP today" -> targets '
    "[], one web workstream (one FX rates board).\n"
    '- "güncel altın gümüş ve ayrıca Avustralya doları" -> targets '
    '[{"name":"altın gümüş"},{"name":"Avustralya doları"}] (metals page '
    "does not cover AUD — AUD needs its own SEARCH).\n"
    '- "İstanbul, Ankara, İzmir hava durumu" -> three targets, web covers '
    "those names.\n"
    '- "A, B, C\'yi karşılaştır" when A/B/C need separate sources -> three '
    "targets named A, B, C.\n"
    '- "standings as Excel" -> web stream plus artifacts stream depending '
    "on it.\n"
    '- "JWT auth design for FastAPI" -> no extra targets, one workstream.\n'
    "Output ONLY the JSON object."
)


def unique_target_names(plan: PlanResult) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for target in list(plan.targets) + [
        PlanTarget(name=item)
        for stream in plan.workstreams
        for item in stream.target_names
    ]:
        name = " ".join(str(target.name or "").split())
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= MAX_PLAN_TARGETS:
            break
    return names


def _recover(response_text: Any) -> Optional[PlanResult]:
    raw = (response_text or "").strip() if isinstance(response_text, str) else ""
    if not raw:
        return None
    fenced = raw
    if fenced.startswith("```"):
        fenced = fenced.split("\n", 1)[-1]
        if fenced.endswith("```"):
            fenced = fenced[:-3]
        fenced = fenced.strip()
    start = fenced.find("{")
    if start < 0:
        return None
    try:
        decoded, _ = json.JSONDecoder().raw_decode(fenced[start:])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    try:
        return PlanResult.model_validate(decoded)
    except ValidationError:
        return None


def parse_plan_result(raw: Any) -> Optional[PlanResult]:
    """Accept a PlanResult, dict, or JSON text blob."""
    if isinstance(raw, PlanResult):
        return raw
    if isinstance(raw, dict):
        try:
            return PlanResult.model_validate(raw)
        except ValidationError:
            return None
    if isinstance(raw, str):
        return _recover(raw)
    return None


def lookup_target_names_from_plan(raw: Any) -> list[str]:
    """Named lookup subjects from a plan payload (empty = single search)."""
    plan = parse_plan_result(raw)
    if plan is None:
        return []
    return unique_target_names(plan)
