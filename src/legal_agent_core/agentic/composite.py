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

from .envelope import RefStore, ToolStatus, empty_envelope, make_envelope
from .graph_tools import (
    GRAPH_TOOL_SPECS,
    GraphToolError,
    GraphTools,
    graph_tool_schemas,
)
from .mcp import tool_schema
from .notebook_tools import NOTEBOOK_TOOL_SPECS, NotebookTools
from .skill_router import load_skill
from .tools import TOOL_SPECS, LegalResearchTools, ToolResult
from .tree_tools import TREE_TOOL_SPECS, TreeTools

_GRAPH_TOOL_NAMES = frozenset(spec.name for spec in GRAPH_TOOL_SPECS)
_NOTEBOOK_TOOL_NAMES = frozenset(spec.name for spec in NOTEBOOK_TOOL_SPECS)
_TREE_TOOL_NAMES = frozenset(spec.name for spec in TREE_TOOL_SPECS)
_SKILL_TOOL = "skill"


class CompositeTools:
    """One dispatch surface over the canonical and graph tool sets."""

    def __init__(
        self,
        canonical: LegalResearchTools,
        graph: GraphTools | None = None,
        notebook: NotebookTools | None = None,
        tree: TreeTools | None = None,
    ) -> None:
        self.canonical = canonical
        self.graph = graph
        self.notebook = notebook
        self.tree = tree

    def dispatch(self, store: RefStore, name: str, args: dict[str, Any]) -> ToolResult:
        if name == _SKILL_TOOL:
            return ToolResult(self._skill(store, args), ())
        if self.tree is not None and name in _TREE_TOOL_NAMES:
            return ToolResult(self.tree.dispatch(store, name, args), ())
        if self.notebook is not None and name in _NOTEBOOK_TOOL_NAMES:
            return ToolResult(self.notebook.dispatch(store, name, args), ())
        if self.graph is not None and name in _GRAPH_TOOL_NAMES:
            envelope = self.graph.dispatch(store, name, args)
            return ToolResult(envelope, ())
        return self.canonical.dispatch(store, name, args)

    def spec_available(self, name: str) -> bool:
        if name in getattr(self.canonical, "hidden_tools", frozenset()):
            return False
        if name == _SKILL_TOOL:
            return True
        if name in _TREE_TOOL_NAMES:
            return self.tree is not None
        if name in _NOTEBOOK_TOOL_NAMES:
            return self.notebook is not None
        if name in _GRAPH_TOOL_NAMES:
            return self.graph is not None
        return self.canonical.spec_available(name)

    def tool_catalog(self) -> str:
        catalog = self.canonical.tool_catalog()
        catalog += (
            "\n\n## مهارت‌ها\n"
            "skill(name) — بارگذاری متن یک مهارت (رویهٔ گام‌به‌گام) وقتی با وظیفه‌ای "
            "هم‌خوان است؛ نام‌ها در فهرست مهارت‌های پرامپت آمده‌اند."
        )
        if self.tree is not None:
            tree_lines = [
                spec.signature() + " — " + spec.description
                for spec in TREE_TOOL_SPECS
                if self.spec_available(spec.name)
            ]
            if tree_lines:
                catalog += (
                    "\n\n## ناوبری درخت مفهومی قوانین (شاخه→موضوع→سند)\n"
                    + "\n".join(tree_lines)
                )
        if self.graph is not None:
            graph_lines = [
                spec.signature() + " — " + spec.description
                for spec in GRAPH_TOOL_SPECS
                if self.spec_available(spec.name)
            ]
            if graph_lines:
                catalog += (
                    "\n\n## ناوبری گراف قوانین (FalkorDB، فقط-خواندن)\n"
                    + "\n".join(graph_lines)
                )
        if self.notebook is not None:
            notebook_lines = [
                spec.signature() + " — " + spec.description
                for spec in NOTEBOOK_TOOL_SPECS
                if self.spec_available(spec.name)
            ]
            if notebook_lines:
                catalog += (
                    "\n\n## دفترچهٔ پژوهش (مسیرهای آموخته — کوتاه‌کنندهٔ ناوبری، نه استناد)\n"
                    + "\n".join(notebook_lines)
                )
        return catalog

    def native_tool_schemas(
        self, names: frozenset[str] | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Provider-native schemas for the tools available in this composite.

        GLM is substantially more reliable when a tool decision is emitted as
        a native function call instead of hand-written JSON. Final answers stay
        in the explicit answer/claims contract.
        """
        schemas: list[dict[str, Any]] = []
        for spec in TOOL_SPECS:
            if not self.spec_available(spec.name) or (
                names is not None and spec.name not in names
            ):
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": tool_schema(spec.name),
                    },
                }
            )
        if self.graph is not None:
            schemas.extend(
                schema
                for schema in graph_tool_schemas()
                if names is None
                or schema.get("function", {}).get("name") in names
            )
        if self.tree is not None:
            schemas.extend(
                schema
                for schema in self._simple_schemas(TREE_TOOL_SPECS)
                if names is None
                or schema.get("function", {}).get("name") in names
            )
        if self.notebook is not None:
            schemas.extend(
                schema
                for schema in self._simple_schemas(NOTEBOOK_TOOL_SPECS)
                if names is None
                or schema.get("function", {}).get("name") in names
            )
        return tuple(schemas)

    def compact_tool_catalog(self, names: frozenset[str]) -> str:
        """Avoid repeating native schema descriptions in the system prompt."""

        available = sorted(name for name in names if self.spec_available(name))
        return (
            "ابزارهای بومی فعال (schema دقیق آرگومان‌ها همراه درخواست ارسال شده است): "
            + ", ".join(available)
        )

    @staticmethod
    def _simple_schemas(specs: tuple[Any, ...]) -> list[dict[str, Any]]:
        properties_by_name: dict[str, dict[str, Any]] = {
            "node_id": {"type": "string"},
            "topic": {"type": "string"},
            "topic_id": {"type": "string"},
            "domain": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }
        schemas: list[dict[str, Any]] = []
        for spec in specs:
            properties: dict[str, Any] = {}
            required: list[str] = []
            for raw in spec.args:
                optional = raw.endswith("?")
                name = raw.removesuffix("?")
                properties[name] = properties_by_name.get(name, {"type": "string"})
                if not optional:
                    required.append(name)
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return schemas

    def _skill(self, store: RefStore, args: dict[str, Any]) -> Any:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GraphToolError("skill requires a non-empty 'name'")
        body = load_skill(name.strip())
        if body is None:
            return empty_envelope(
                store, ToolStatus.NOT_FOUND, (f"unknown skill {name!r}",)
            )
        return make_envelope(store, ({"name": name.strip(), "body": body},))

    @property
    def evidence_index(self) -> dict[str, str]:
        return self.canonical.evidence_index


__all__ = ["CompositeTools"]
