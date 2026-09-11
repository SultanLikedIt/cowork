"""Bounded initial Workforce plan: targets × capabilities, not free XML.

Initial decomposition used CAMEL's <task> prompt with a 12k completion
budget and the full Workforce step timeout. That made planning wall time
a function of hidden reasoning tokens (10s–60s+) and collapsed
multi-target lookups into one Research subtask. This module is the
source of truth for the first split: a short JSON plan, a 25s timeout,
then deterministic packing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

import capabilities

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

# Keep in lockstep with camel_service.WORST_CASE_MODEL_CALL_SECONDS.
# This is the planning stage's own deadline; it must not inherit the
# ~1000s batch step_timeout.
PLANNING_STEP_TIMEOUT_SECONDS = 25
PLANNING_MAX_TOKENS = 4096
MAX_PLAN_TARGETS = 8

_WEB = {capabilities.WEB_SEARCH, capabilities.WEB_FETCH}
_CAP_ALIASES = {
    "web": capabilities.WEB_SEARCH,
    "web_search": capabilities.WEB_SEARCH,
    "web_fetch": capabilities.WEB_FETCH,
    "search": capabilities.WEB_SEARCH,
    "artifacts": capabilities.ARTIFACTS,
    "artifact": capabilities.ARTIFACTS,
    "files": capabilities.FILES,
    "file": capabilities.FILES,
    "code": capabilities.CODE_EXEC,
    "code_exec": capabilities.CODE_EXEC,
}


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


@dataclass(frozen=True)
class NormalizedStream:
    goal: str
    capabilities: frozenset[str]
    targets: tuple[str, ...]
    depends_on: tuple[int, ...]


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


def fallback_plan(request: str, required) -> PlanResult:
    caps = [
        name
        for name in (
            capabilities.WEB_SEARCH,
            capabilities.ARTIFACTS,
            capabilities.FILES,
            capabilities.CODE_EXEC,
        )
        if name in capabilities.expand(required or [])
    ]
    if not caps:
        caps = [capabilities.WEB_SEARCH]
    return PlanResult(
        targets=[],
        workstreams=[
            PlanWorkstream(
                goal=(request or "").strip(),
                capabilities=caps,
                target_names=[],
                depends_on=[],
            )
        ],
        source="fallback",
    )


def _expand_stream_caps(raw) -> frozenset[str]:
    names: set[str] = set()
    for item in raw or []:
        token = str(item or "").strip().casefold().replace(" ", "_")
        mapped = _CAP_ALIASES.get(token, token)
        names.add(mapped)
    return capabilities.expand(names)


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


def partition_targets(targets: list[str], slots: int) -> list[list[str]]:
    if not targets:
        return []
    slots = max(1, min(int(slots or 1), len(targets)))
    groups: list[list[str]] = [[] for _ in range(slots)]
    for index, name in enumerate(targets):
        groups[index % slots].append(name)
    return [group for group in groups if group]


def _research_goal(request: str, targets: list[str]) -> str:
    if not targets:
        return (
            "Gather verified current facts for the original request. Cover "
            "every named subject; do not stop after the first one that is "
            "easy to find.\n\n"
            f"ORIGINAL USER REQUEST:\n{request}"
        )
    listed = ", ".join(targets)
    scope = (
        f"This subtask covers ONLY: {listed}."
        if len(targets) == 1
        else f"This subtask covers ONLY these targets: {listed}."
    )
    return (
        f"Gather verified current facts for: {listed}. {scope} Preserve "
        "the original language, dates, and constraints. Do not skip a "
        "named target.\n\n"
        f"ORIGINAL USER REQUEST:\n{request}"
    )


def _artifact_goal(request: str) -> str:
    return (
        "Create the explicitly requested file/artifact for the original "
        "user request below. Consume the verified outputs, provenance, and "
        "data_path from prerequisite tasks directly; do not repeat web "
        "research and do not invent missing data.\n\n"
        f"ORIGINAL USER REQUEST:\n{request}"
    )


def _files_goal(request: str) -> str:
    return (
        "Read and analyze the uploaded workspace files needed to fulfill the "
        "original request below. Use the Docling-backed file tools to inspect "
        "and read the relevant material directly; do not search the web or "
        "invent content that is not present in the uploads.\n\n"
        f"ORIGINAL USER REQUEST:\n{request}"
    )


def expand_plan_for_coverage(
    request: str,
    plan: PlanResult,
    required,
    *,
    max_subtasks: int,
) -> list[NormalizedStream]:
    """Turn a JSON plan into bounded streams: targets × capability groups."""
    required = capabilities.expand(required or [])
    request = (request or "").strip()
    targets = unique_target_names(plan)
    # Job structural requirements are authoritative for whether web research
    # runs at all. A planner workstream tagged web_search must not override
    # a FILES-only job into Research.
    needs_web = bool(required & _WEB)
    needs_files_primary = capabilities.is_files_primary(required)
    needs_artifact = capabilities.ARTIFACTS in required
    needs_code = capabilities.CODE_EXEC in required
    # Planner workstreams must not override the submission-time effective contract.

    max_subtasks = max(1, int(max_subtasks or 1))
    streams: list[NormalizedStream] = []
    reserved = (
        int(needs_artifact)
        + int(needs_code and not needs_web)
        + int(needs_files_primary)
    )
    research_slots = max(1, max_subtasks - reserved) if needs_web else 0

    if needs_web:
        groups = partition_targets(targets, research_slots) if targets else [[]]
        if not groups:
            groups = [[]]
        for group in groups:
            streams.append(
                NormalizedStream(
                    _research_goal(request, group),
                    frozenset(_WEB),
                    tuple(group),
                    (),
                )
            )

    if needs_code and not needs_web:
        streams.append(
            NormalizedStream(
                request or "Fulfill the requested computation.",
                frozenset({capabilities.CODE_EXEC}),
                (),
                (),
            )
        )

    if needs_files_primary:
        streams.append(
            NormalizedStream(
                _files_goal(request),
                frozenset({capabilities.FILES}),
                tuple(targets) if targets else (),
                (),
            )
        )

    if needs_artifact:
        prereq = tuple(range(len(streams)))
        streams.append(
            NormalizedStream(
                _artifact_goal(request),
                frozenset({capabilities.ARTIFACTS}),
                tuple(targets),
                prereq,
            )
        )

    if not streams:
        streams.append(
            NormalizedStream(
                request,
                frozenset(),
                tuple(targets),
                (),
            )
        )
    return streams


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


def plan_request(
    request: str,
    *,
    agent: Any = None,
    required=None,
) -> PlanResult:
    """JSON plan from the management agent, or a one-stream fallback."""
    request = request or ""
    required = capabilities.expand(required or [])
    if agent is None:
        return fallback_plan(request, required)

    from camel.messages import BaseMessage

    original_timeout = getattr(agent, "step_timeout", None)
    had_timeout = hasattr(agent, "step_timeout")
    agent.step_timeout = PLANNING_STEP_TIMEOUT_SECONDS
    backend = getattr(agent, "model_backend", None)
    config = getattr(backend, "model_config_dict", None)
    original_max_tokens = None
    mutated_tokens = False
    if isinstance(config, dict):
        original_max_tokens = config.get("max_tokens")
        config["max_tokens"] = PLANNING_MAX_TOKENS
        mutated_tokens = True
    reset = getattr(agent, "reset", None)
    if callable(reset):
        reset()
    try:
        response = agent.step(
            BaseMessage.make_user_message(
                role_name="planner",
                content=(
                    f"{PLAN_SYSTEM_PROMPT}\n\nREQUEST:\n{request}\n"
                ),
            ),
            response_format=PlanResult,
        )
    except Exception:  # noqa: BLE001 - fail closed to a single stream
        stabilization_logger.warning(
            "Workforce planning failed; using single-stream fallback",
            exc_info=True,
        )
        return fallback_plan(request, required)
    finally:
        if had_timeout:
            agent.step_timeout = original_timeout
        if mutated_tokens and isinstance(config, dict):
            if original_max_tokens is None:
                config.pop("max_tokens", None)
            else:
                config["max_tokens"] = original_max_tokens

    parsed = response.msg.parsed if getattr(response, "msg", None) else None
    if isinstance(parsed, PlanResult):
        result = parsed
    elif isinstance(parsed, dict):
        try:
            result = PlanResult.model_validate(parsed)
        except ValidationError:
            result = None
    else:
        result = _recover(response.msg.content if getattr(response, "msg", None) else None)
    if result is None:
        stabilization_logger.warning(
            "Workforce planning returned no valid plan JSON; fallback"
        )
        return fallback_plan(request, required)
    result.source = "llm"
    return result


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
