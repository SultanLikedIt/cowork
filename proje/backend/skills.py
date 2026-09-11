"""Built-in Skills: reusable execution methods layered onto agents.

An **agent** is *who* does the work -- Research Agent, Document Agent, a
dynamically created specialist. A **skill** is *how* the work is done: a
method, mindset, or delivery style that applies on top of whoever is
executing. That distinction is what keeps this catalog from becoming a
second, parallel set of agents:

  * Nothing here means "do research" or "write a document" -- those are
    already fixed workers (camel_service.WORKER_ROLES), and a skill that
    only renamed one would be a worse way to reach the same agent.
  * Every entry has to make sense applied to a Research Agent, a Document
    Agent, a dynamic worker, AND a single direct agent. If guidance only
    reads sensibly for one of them, it is describing a role, not a skill.

The seven below are deliberately one per *phase of engineering thought* --
diagnose, defend, contextualise, decide, verify, plan, communicate -- which
is what keeps them non-overlapping. They are product-owned and read-only in
v1: users select from this list, they cannot author instructions, so no
user-supplied text ever reaches a system prompt through this path.

This module is deliberately CAMEL-free (same rule as capability_routing.py)
so main.py and camel_service.py can both import it.

ORCHESTRATION BOUNDARY -- skills are strictly downstream of routing.
Nothing in this module is read by the router, the planner, the coordinator,
the recovery policy, or tool selection. See camel_service for where the
rendered guidance is actually attached, and test_skills.py for the
regression tests that hold that boundary in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Skill:
    skill_id: str
    name: str
    # One line, shown in the composer's picker. Written for the person
    # choosing, not for the model.
    description: str
    # What is actually appended to an executing agent's system message.
    # Imperative, bounded, and written to compose with the other six: no
    # entry may contradict another when two or three are active together.
    guidance: str


BUILT_IN_SKILLS: tuple[Skill, ...] = (
    Skill(
        skill_id="root_cause",
        name="Root Cause Analysis",
        description="Trace symptoms to their underlying cause before proposing a fix.",
        guidance=(
            "Work backwards from the symptom to its cause before proposing "
            "any fix. State what you would expect to observe if each "
            "candidate cause were the real one, and say which observation "
            "would rule it out. Distinguish the proximate cause from the "
            "condition that allowed it. Never present a mitigation for a "
            "symptom as if it were a fix for the cause."
        ),
    ),
    Skill(
        skill_id="risk_analysis",
        name="Risk & Failure Analysis",
        description="Surface failure modes, blast radius, and mitigations up front.",
        guidance=(
            "Treat the proposal as something that will be operated, not just "
            "built. Name the ways it can fail, what each failure would take "
            "down with it, and what makes each one more or less likely. "
            "Prefer a small number of concrete, plausible failures over an "
            "exhaustive list. For each one that matters, give the mitigation "
            "or say plainly that there isn't a good one."
        ),
    ),
    Skill(
        skill_id="systems_thinking",
        name="Systems Thinking",
        description="Account for second-order effects, interfaces, and feedback loops.",
        guidance=(
            "Place the work in the system around it. Identify what it "
            "depends on, what depends on it, and which boundaries it "
            "crosses. Call out second-order effects -- what changes "
            "elsewhere as a consequence, including effects that only appear "
            "under load, over time, or during partial failure. Flag feedback "
            "loops where an effect feeds back into its own cause."
        ),
    ),
    Skill(
        skill_id="tradeoffs",
        name="Tradeoff Analysis",
        description="Compare real alternatives on explicit criteria, then recommend one.",
        guidance=(
            "Do not present a single option as if it were the only one. Name "
            "the genuine alternatives, state the criteria you are judging "
            "them on before judging, and compare them on those criteria. "
            "End with one recommendation and say explicitly what is being "
            "given up by taking it. If the alternatives are close, say so "
            "rather than manufacturing a winner."
        ),
    ),
    Skill(
        skill_id="verification",
        name="Evidence & Verification",
        description="Separate verified fact from inference, and label confidence.",
        guidance=(
            "Keep three things visibly distinct: what you verified, what you "
            "inferred, and what you assumed. Attach your confidence to any "
            "claim a reader would act on, and state what would have to be "
            "true for the claim to be wrong. Where you could not verify "
            "something, say so in place of it -- an acknowledged gap is more "
            "useful than a confident guess."
        ),
    ),
    Skill(
        skill_id="prioritization",
        name="Prioritization & Sequencing",
        description="Order the work by impact, dependency, and what unblocks the most.",
        guidance=(
            "Turn the work into an ordered sequence rather than a flat list. "
            "Order by what unblocks the most, what carries the most risk if "
            "deferred, and what must precede what. Say which items are "
            "genuinely independent and could proceed at the same time. Mark "
            "anything that is not worth doing now, and why."
        ),
    ),
    Skill(
        skill_id="exec_brief",
        name="Executive Brief",
        description="Lead with the conclusion and the decision needed; detail below.",
        guidance=(
            "Lead with the answer, the decision required, or the "
            "recommendation -- not with the background that produced it. "
            "Put anything the reader must act on in the first few lines, in "
            "plain language, without internal jargon. Supporting detail, "
            "caveats and workings come afterwards, so a reader who stops "
            "early still has the part that matters."
        ),
    ),
)

_BY_ID = {skill.skill_id: skill for skill in BUILT_IN_SKILLS}

# A ceiling, not a preference. Every active skill is appended verbatim to an
# executing agent's system message, so N skills means N sets of instructions
# competing for the same response: past a small number the agent satisfies
# all of them shallowly instead of any of them well, and the prompt cost is
# paid on every worker turn. Three composes cleanly (a method + a mindset +
# a delivery style is the common useful combination) and keeps the picker a
# choice rather than a configuration screen. Enforced server-side in
# resolve_skills so it holds regardless of what a client sends.
MAX_ACTIVE_SKILLS = 3


def resolve_skills(skill_ids: Optional[Iterable[str]]) -> list[Skill]:
    """Map requested ids to catalog entries, in catalog order.

    Unknown ids are dropped rather than raising: a stale id from an older
    client, a replayed history entry, or a retired skill must not fail an
    otherwise valid run. Duplicates collapse, and the result is capped at
    MAX_ACTIVE_SKILLS. Catalog order (not request order) is what makes the
    rendered prompt block byte-identical for the same selection, which is
    what lets a persisted run be reproduced exactly.
    """
    if not skill_ids:
        return []
    requested = {str(skill_id) for skill_id in skill_ids}
    resolved = [skill for skill in BUILT_IN_SKILLS if skill.skill_id in requested]
    return resolved[:MAX_ACTIVE_SKILLS]


def skill_ids(skills: Iterable[Skill]) -> list[str]:
    return [skill.skill_id for skill in skills]


def render_guidance(skills: Iterable[Skill]) -> str:
    """The block appended to an executing agent's system message.

    Returns "" for an empty selection, so every call site can append
    unconditionally and a run with no skills is byte-identical to one from
    before this feature existed.

    The preamble is doing real work. Without it a skill reads as a second
    task ("analyse the risks") and agents start answering the skill instead
    of the request; and a skill must never be mistaken for a grant of
    capability, since selecting one gives an agent no tools, no sources and
    no permissions it did not already have.
    """
    # None as well as [] -- every execution entry point declares
    # `skills: Optional[list] = None`, so an existing caller that never
    # passes the argument (and every test double that predates it) reaches
    # here with None. That must render exactly like an empty selection, not
    # raise.
    active = list(skills or ())
    if not active:
        return ""
    lines = "\n".join(f"- {skill.name}: {skill.guidance}" for skill in active)
    return (
        "\n\nAPPLIED SKILLS\n"
        "The following are methods to apply to how you carry out the task "
        "and present the result. They do NOT change what you were asked to "
        "produce, they are not additional tasks to answer separately, and "
        "they grant no new tools, data sources, or permissions. Apply them "
        "to the work you were already given.\n"
        f"{lines}"
    )


def catalog_payload() -> list[dict[str, str]]:
    """The catalog as the frontend picker consumes it.

    `guidance` is deliberately excluded: it is prompt text, it is not
    useful to a person choosing a skill, and shipping it to the client
    would invite treating it as editable -- which v1 explicitly is not.
    """
    return [
        {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
        }
        for skill in BUILT_IN_SKILLS
    ]
