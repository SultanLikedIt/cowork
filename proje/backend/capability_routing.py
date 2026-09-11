"""UI-mode execution descriptors and persona inference.

Strategy is chosen by the user (Single vs Workforce), not by a model or a
text classifier. This module still holds RoutingDecision (persisted on jobs),
merge_decision (union declared + structural floors for Workforce), and
infer_profile (persona only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import capabilities

# Retained for the persisted jobs and the fixed-worker roster that still use
# them; nothing here selects a role anymore.
RESEARCH_ROLE = "Research Agent"
DOCUMENT_ROLE = "Document Agent"

# "single" | "workforce" | "direct" | "specialist"
# "direct"/"specialist" remain accepted so persisted jobs still load.
Strategy = str

# Why a job ended up with the strategy it has. Distinguishing these matters
# operationally. ROUTING_SKIPPED is the production outcome: the user picked
# Single or Workforce in the UI.
ROUTING_OK = "ok"
ROUTING_TIMEOUT = "timeout"
ROUTING_ERROR = "error"
ROUTING_SKIPPED = "not_called"
# Workforce thread chose Direct because this turn only needs the existing
# conversation (shorten / translate / "who were the top three"). Not a
# replacement for UI mode and not a worker/tool graph.
ROUTING_TURN_RESOLUTION = "turn_resolution"
# Historical rows from the retired deterministic classifier. Not produced.
ROUTING_DETERMINISTIC = "deterministic"

# Execution profiles. A profile is a PERSONA and an output convention --
# nothing else. It deliberately carries no toolset: tools come from
# capabilities, so a profile can never be the reason an executor cannot
# finish.
PROFILE_GENERAL = "general"
PROFILE_RESEARCH = "research"
PROFILE_DOCUMENT = "document"
PROFILE_REVIEW = "review"      # kept for backward compat with persisted jobs
PROFILE_OUTLINE = "outline"    # kept for backward compat with persisted jobs
PROFILE_CODING = "coding"
PROFILES = (
    PROFILE_GENERAL,
    PROFILE_RESEARCH,
    PROFILE_DOCUMENT,
    PROFILE_REVIEW,
    PROFILE_OUTLINE,
    PROFILE_CODING,
)


@dataclass(frozen=True)
class RoutingDecision:
    strategy: Strategy
    role: Optional[str] = None
    # Human-readable, for logs/tests -- not shown to the end user.
    reason: str = ""
    # One of the ROUTING_* constants above; surfaced as
    # usage.routing_outcome (see main.UsageTelemetry).
    outcome: str = ROUTING_OK
    # What the executor must be able to DO -- inputs and deliverable both.
    capabilities: frozenset[str] = field(default_factory=frozenset)
    # DIRECT/Single persona; one of PROFILES.
    profile: str = PROFILE_GENERAL
    # Requested capabilities this system does not implement. Non-empty means
    # the job must fail early and explicitly rather than attempt the work.
    unsupported: tuple[str, ...] = ()
    # Set only on historical shadow-mode rows. Never affects execution.
    advisory_strategy: Optional[str] = None
    advisory_capabilities: frozenset[str] = field(default_factory=frozenset)

    def can_satisfy(self, required) -> bool:
        """Whether this decision's executor covers everything `required` needs."""
        return capabilities.expand(required) <= capabilities.expand(self.capabilities)


def merge_decision(
    strategy: Strategy,
    declared,
    structural: frozenset[str],
    *,
    profile: str = PROFILE_GENERAL,
    reason: str = "",
    outcome: str = ROUTING_OK,
    role: Optional[str] = None,
    raw_declared=None,
) -> RoutingDecision:
    """Combine declared capabilities with what the request provably needs.

    UNION, never intersection. The structural signals are a floor: they are
    facts about the request (a URL is present, files are attached, a file
    format was named as the output), so omitting one is wrong, and adding a
    capability on top is at worst slightly generous.
    """
    combined = capabilities.expand(set(declared) | set(structural))
    missing_from_router = capabilities.expand(structural) - capabilities.expand(declared)
    if missing_from_router and reason:
        reason = f"{reason}; structural: +{','.join(sorted(missing_from_router))}"
    return RoutingDecision(
        strategy=strategy,
        role=role,
        reason=reason,
        outcome=outcome,
        capabilities=combined,
        profile=profile if profile in PROFILES else PROFILE_GENERAL,
        unsupported=tuple(
            capabilities.unsupported(
                raw_declared if raw_declared is not None else declared
            )
        ),
    )


# ---------------------------------------------------------------------------
# Profile inference
# ---------------------------------------------------------------------------
#
# Persona only. Deliberately cannot affect capabilities.

_REVIEW_RE = re.compile(
    r"("
    r"\b(?:critique|proofread)\b"
    r"|\breview (?:this|my|the (?:draft|text|copy|document|attached|above))\b"
    r"|\bedit (?:this|my|the (?:draft|text|copy))\b"
    r"|\bcheck (?:this|the) (?:draft|text|copy|wording|grammar)\b"
    r"|\bfeedback on (?:this|my|the)\b"
    r"|\bdüzelt\w*|\bgözden geçir\w*|\bredaksiyon\b"
    r")",
    re.IGNORECASE,
)
_OUTLINE_RE = re.compile(
    r"\b(outline|structure (?:this|the)|table of contents|section breakdown|"
    r"taslak|ana hatlar|başlıklar)\b",
    re.IGNORECASE,
)
_RESEARCH_PROFILE_RE = re.compile(
    r"\b(research|search|look up|find out|latest|current|recent|news|compare "
    r"(?:options|vendors|tools)|araştır|güncel|son durum)\b",
    re.IGNORECASE,
)


_CODING_RE = re.compile(
    r"\b(write code|implement|debug|fix (?:the |this |a )?bug|refactor|"
    r"unit test|test suite|dockerfile|ci.?cd|docker|deploy|build|compile|"
    r"kod yaz|hata düzelt|geliştir)\b",
    re.IGNORECASE,
)


def infer_profile(text: str, has_workspace_files: bool, resolved) -> str:
    """Pick the persona that best fits the request.

    Ordered most-specific first. Files present with no other signal means the
    work is about the supplied material, which is the document persona.
    """
    body = text or ""
    resolved = capabilities.expand(resolved)
    if _CODING_RE.search(body) or capabilities.CODE_EXEC in resolved:
        return PROFILE_CODING
    if has_workspace_files or capabilities.FILES in resolved:
        return PROFILE_DOCUMENT
    if (
        capabilities.WEB_SEARCH in resolved
        or capabilities.WEB_FETCH in resolved
        or _RESEARCH_PROFILE_RE.search(body)
    ):
        return PROFILE_RESEARCH
    return PROFILE_GENERAL


__all__ = [
    "DOCUMENT_ROLE",
    "infer_profile",
    "PROFILES",
    "PROFILE_CODING",
    "PROFILE_DOCUMENT",
    "PROFILE_GENERAL",
    "PROFILE_OUTLINE",
    "PROFILE_RESEARCH",
    "PROFILE_REVIEW",
    "RESEARCH_ROLE",
    "ROUTING_DETERMINISTIC",
    "ROUTING_ERROR",
    "ROUTING_OK",
    "ROUTING_SKIPPED",
    "ROUTING_TIMEOUT",
    "ROUTING_TURN_RESOLUTION",
    "RoutingDecision",
    "Strategy",
    "merge_decision",
]
