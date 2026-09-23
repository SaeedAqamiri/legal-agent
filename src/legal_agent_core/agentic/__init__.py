"""Tool-driven research agent over the canonical legal repository.

Read-only tools, honest pagination envelopes and a budgeted planner loop.
The publish gate (``CitationPipeline``) still applies to every answer this
loop produces; the agent can never bypass evidence verification.
"""

from .envelope import (
    ITEMS_IN_CONTEXT,
    Envelope,
    RefStore,
    ToolStatus,
    empty_envelope,
    make_envelope,
)
from .loop import (
    AgenticConfig,
    IncompleteReason,
    ToolCallTrace,
    ToolResearchLoop,
)
from .planner import LLMToolPlanner, PlannerDecision, PlannerTurn, ToolPlanner
from .skill_router import all_skills, select_skill
from .tools import LegalResearchTools, ToolResult

__all__ = [
    "ITEMS_IN_CONTEXT",
    "AgenticConfig",
    "Envelope",
    "IncompleteReason",
    "LLMToolPlanner",
    "LegalResearchTools",
    "PlannerDecision",
    "PlannerTurn",
    "RefStore",
    "ToolCallTrace",
    "ToolPlanner",
    "ToolResearchLoop",
    "ToolResult",
    "ToolStatus",
    "all_skills",
    "empty_envelope",
    "make_envelope",
    "select_skill",
]
