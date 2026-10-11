"""Composite tool surface: canonical research tools + FalkorDB graph tools.

The research agent talks to one object; ``CompositeTools`` routes each call to
either :class:`~legal_agent_core.agentic.tools.LegalResearchTools` (evidence-
producing canonical reads) or :class:`~legal_agent_core.agentic.graph_tools.GraphTools`
(read-only graph navigation). Graph tools never produce Evidence — they are
consulted as context (topic tree, judgments, cypher) and their envelopes still
carry the executed Cypher plus the nodes/edges the UI highlights.
"""

from __future__ import annotations

from typing import Any

from .envelope import RefStore
from .graph_tools import GRAPH_TOOL_SPECS, GraphTools
from .tools import LegalResearchTools, ToolResult

_GRAPH_TOOL_NAMES = frozenset(spec.name for spec in GRAPH_TOOL_SPECS)


class CompositeTools:
    """One dispatch surface over the canonical and graph tool sets."""

    def __init__(
        self, canonical: LegalResearchTools, graph: GraphTools | None = None
    ) -> None:
        self.canonical = canonical
        self.graph = graph

    def dispatch(self, store: RefStore, name: str, args: dict[str, Any]) -> ToolResult:
        if self.graph is not None and name in _GRAPH_TOOL_NAMES:
            envelope = self.graph.dispatch(store, name, args)
            return ToolResult(envelope, ())
        return self.canonical.dispatch(store, name, args)

    def spec_available(self, name: str) -> bool:
        if name in getattr(self.canonical, "hidden_tools", frozenset()):
            return False
        if name in _GRAPH_TOOL_NAMES:
            return self.graph is not None
        return self.canonical.spec_available(name)

    def tool_catalog(self) -> str:
        catalog = self.canonical.tool_catalog()
        if self.graph is None:
            return catalog
        graph_lines = [
            spec.signature() + " — " + spec.description
            for spec in GRAPH_TOOL_SPECS
            if self.spec_available(spec.name)
        ]
        if not graph_lines:
            return catalog
        return (
            catalog
            + "\n\n## ناوبری گراف قوانین (FalkorDB، فقط-خواندن)\n"
            + "\n".join(graph_lines)
        )

    @property
    def evidence_index(self) -> dict[str, str]:
        return self.canonical.evidence_index


__all__ = ["CompositeTools"]
