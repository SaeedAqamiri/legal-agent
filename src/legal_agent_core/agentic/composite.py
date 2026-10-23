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

from ..retrieval import search_tokens
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
from .tools import TOOL_SPECS, LegalResearchTools, ToolResult, ToolSpec
from .tree_tools import TREE_TOOL_SPECS, TreeTools

_GRAPH_TOOL_NAMES = frozenset(spec.name for spec in GRAPH_TOOL_SPECS)
_NOTEBOOK_TOOL_NAMES = frozenset(spec.name for spec in NOTEBOOK_TOOL_SPECS)
_TREE_TOOL_NAMES = frozenset(spec.name for spec in TREE_TOOL_SPECS)
_SKILL_TOOL = "skill"
_NAVIGATE_TOOL = "navigate"
_ROUTED_CANONICAL = frozenset({"read", "browse", "neighbors"})

NAVIGATE_SPEC = ToolSpec(
    "navigate",
    "ناوبری مفهومی با یک ورودی: mode=path مسیر ریشه→اسناد مرتبط با topic؛ "
    "mode=children فرزندان یک node_id؛ mode=notebook صفحهٔ دفترچه؛ mode=routes مسیرهای "
    "آموختهٔ یک موضوع. این لایه فقط کوتاه‌کنندهٔ مسیر است، نه شاهد.",
    ("topic?", "node_id?", "mode?", "limit?", "query?"),
)


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
        if name == _NAVIGATE_TOOL:
            return ToolResult(self._navigate(store, args), ())
        if name in _ROUTED_CANONICAL:
            return self._route(store, name, args)
        if self.tree is not None and name in _TREE_TOOL_NAMES:
            return ToolResult(self.tree.dispatch(store, name, args), ())
        if self.notebook is not None and name in _NOTEBOOK_TOOL_NAMES:
            return ToolResult(self.notebook.dispatch(store, name, args), ())
        if self.graph is not None and name in _GRAPH_TOOL_NAMES:
            envelope = self.graph.dispatch(store, name, args)
            return ToolResult(envelope, ())
        return self.canonical.dispatch(store, name, args)

    def _route(self, store: RefStore, name: str, args: dict[str, Any]) -> Any:
        """Send one unified call to canonical, graph, or a fallback.

        ``read``/``browse``/``neighbors`` hide three different backends behind
        one address space: a span/provision/evidence id reads canonical text,
        a judgment id reads the judicial layer, and an instrument id traverses
        the graph. The first backend that produces a real result wins.
        """
        if name == "read":
            result = self.canonical.dispatch(store, name, args)
            if (
                result.envelope.status in (ToolStatus.NOT_FOUND, ToolStatus.ERROR)
                and self.graph is not None
            ):
                ref = args.get("ref")
                if isinstance(ref, str) and ref.strip():
                    fallback = self.graph.dispatch(
                        store, "get_judgment_text", {"judgment_id": ref.strip()}
                    )
                    if fallback.status is ToolStatus.OK:
                        return ToolResult(fallback, ())
            return result
        if name == "browse":
            kind = str(args.get("kind") or "").strip().lower()
            query = args.get("query")
            if self.graph is not None and isinstance(query, str) and query.strip():
                if kind in {"judgment", "opinion", "ruling", "precedent"}:
                    payload = {"topic": query.strip(), "limit": args.get("limit")}
                    if kind in {"judgment", "opinion"}:
                        payload["kind"] = kind
                    return ToolResult(
                        self.graph.dispatch(
                            store, "search_judgments", self._compact(payload)
                        ),
                        (),
                    )
                if kind in {"law", "instrument", "statute"}:
                    # Catalog rows from canonical carry instrument_id/ref that
                    # outline()/search(document=...) can consume directly;
                    # graph search_laws is summary-only and produces no refs.
                    return self.canonical.dispatch(store, name, args)
            return self.canonical.dispatch(store, name, args)
        if name == "neighbors":
            ref = str(args.get("ref") or "")
            if self.graph is not None and ref.startswith("inst_"):
                return ToolResult(
                    self.graph.dispatch(
                        store,
                        "node_neighbors",
                        self._compact(
                            {
                                "node_id": ref,
                                "rel_types": args.get("rel_types"),
                                "limit": args.get("limit") or 50,
                            }
                        ),
                    ),
                    (),
                )
            return self.canonical.dispatch(store, name, args)
        return self.canonical.dispatch(store, name, args)

    @staticmethod
    def _compact(args: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in args.items() if value is not None}

    def _navigate(self, store: RefStore, args: dict[str, Any]) -> Any:
        topic = args.get("topic")
        node_id = args.get("node_id")
        query = args.get("query")
        mode = str(args.get("mode") or "").strip().lower()
        limit = args.get("limit") or 5
        if not mode:
            mode = "path" if isinstance(topic, str) and topic.strip() else "children"
        if mode in {"path", "walk"}:
            if self.tree is None:
                return empty_envelope(store, ToolStatus.ERROR, ("درخت مفهومی پیکربندی نشده است",))
            return self.tree.dispatch(
                store,
                "walk_tree",
                self._compact({"topic": topic, "limit": limit}),
            )
        if mode in {"children", "tree", "list"}:
            if self.tree is None:
                return empty_envelope(store, ToolStatus.ERROR, ("درخت مفهومی پیکربندی نشده است",))
            if isinstance(query, str) and query.strip() and self.notebook is not None:
                return self._children_with_notes(store, node_id, query)
            return self.tree.dispatch(
                store, "list_tree", self._compact({"node_id": node_id})
            )
        if mode in {"routes", "route"}:
            if self.notebook is None:
                return empty_envelope(store, ToolStatus.ERROR, ("دفترچهٔ پژوهش پیکربندی نشده است",))
            return self.notebook.dispatch(
                store,
                "notebook_route",
                self._compact(
                    {
                        "topic_id": node_id or topic or "",
                        "query": args.get("query"),
                    }
                ),
            )
        if mode in {"notebook", "page"}:
            if self.notebook is None:
                return empty_envelope(store, ToolStatus.ERROR, ("دفترچهٔ پژوهش پیکربندی نشده است",))
            if isinstance(node_id, str) and node_id.strip():
                return self.notebook.dispatch(
                    store, "open_notebook", {"node_id": node_id.strip()}
                )
            return self.notebook.dispatch(
                store,
                "notebook_index",
                self._compact({"domain": args.get("domain")}),
            )
        return empty_envelope(
            store, ToolStatus.ERROR, (f"unknown navigate mode {mode!r}",)
        )

    def _children_with_notes(
        self, store: RefStore, node_id: str | None, query: str
    ) -> Any:
        """Layer-by-layer descent: children + their notebook notes + a score.

        This is the opencode-notebook shape the agent needs at every level:
        read the notes of the children, pick the relevant subtree, then call it
        again for the next level instead of jumping to a global search.
        """
        env = self.tree.dispatch(
            store, "list_tree", self._compact({"node_id": node_id})
        )
        if env.status is not ToolStatus.OK or not env.items:
            return env
        parent = dict(env.items[0])
        children = list(parent.get("children") or [])
        query_tokens = search_tokens(query)
        enriched: list[dict[str, Any]] = []
        pages = self.notebook.page_map() if self.notebook is not None else {}
        for child in children:
            page_summary = ""
            child_id = str(child.get("id") or "")
            page = pages.get(child_id)
            if page is not None:
                page_summary = page.summary[:500]
            blob = " ".join(
                str(child.get(key) or "")
                for key in ("label", "title", "summary")
            ) + " " + page_summary
            score = (
                len(query_tokens & search_tokens(blob))
                if query_tokens
                else 0
            )
            item = dict(child)
            item["page_summary"] = page_summary
            item["score"] = score
            enriched.append(item)
        if query_tokens:
            enriched.sort(
                key=lambda item: (
                    -int(item.get("score") or 0),
                    str(item.get("label") or ""),
                )
            )
        parent["children"] = enriched[:12]
        parent["query"] = query
        parent["note"] = (
            "در هر لایه page_summary را بخوان و مرتبط‌ترین فرزند را انتخاب کن؛ "
            "برای یک سطح پایین‌تر دوباره "
            "navigate(mode=children, node_id=<id>, query=سؤال) را صدا بزن."
        )
        return make_envelope(store, (parent,), total_count=len(children))

    def spec_available(self, name: str) -> bool:
        if name in getattr(self.canonical, "hidden_tools", frozenset()):
            return False
        if name == _SKILL_TOOL:
            return True
        if name == _NAVIGATE_TOOL:
            return self.tree is not None or self.notebook is not None
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
        if self.spec_available(_NAVIGATE_TOOL):
            catalog += (
                "\n\n## ناوبری مفهومی (درخت + دفترچه — فقط راهنما)\n"
                + NAVIGATE_SPEC.signature()
                + " — "
                + NAVIGATE_SPEC.description
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
                if self.spec_available(schema.get("function", {}).get("name", ""))
                and (
                    names is None
                    or schema.get("function", {}).get("name") in names
                )
            )
        if self.tree is not None:
            schemas.extend(
                schema
                for schema in self._simple_schemas(TREE_TOOL_SPECS)
                if self.spec_available(schema.get("function", {}).get("name", ""))
                and (
                    names is None
                    or schema.get("function", {}).get("name") in names
                )
            )
        if self.notebook is not None:
            schemas.extend(
                schema
                for schema in self._simple_schemas(NOTEBOOK_TOOL_SPECS)
                if self.spec_available(schema.get("function", {}).get("name", ""))
                and (
                    names is None
                    or schema.get("function", {}).get("name") in names
                )
            )
        if self.spec_available(_NAVIGATE_TOOL):
            schemas.extend(
                schema
                for schema in self._simple_schemas((NAVIGATE_SPEC,))
                if names is None
                or schema.get("function", {}).get("name") in names
            )
        if names is None or _SKILL_TOOL in names:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": _SKILL_TOOL,
                        "description": (
                            "بارگذاری متن یک مهارت رویه‌ای (گام‌به‌گام) وقتی با "
                            "وظیفه‌ای هم‌خوان است."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    },
                }
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
            "query": {"type": "string"},
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
