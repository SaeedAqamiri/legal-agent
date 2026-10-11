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
from .graph_chat import (
    GraphChatConfig,
    GraphChatLoop,
    GraphChatResult,
    GraphChatService,
)
from .graph_tools import GRAPH_TOOL_SPECS, GraphTools
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
    "GRAPH_TOOL_SPECS",
    "ITEMS_IN_CONTEXT",
    "AgenticConfig",
    "Envelope",
    "GraphChatConfig",
    "GraphChatLoop",
    "GraphChatResult",
    "GraphChatService",
    "GraphTools",
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
