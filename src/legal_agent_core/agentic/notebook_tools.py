"""Read-only notebook tools for the research/graph agent.

Docs/qavanin/06_law_notebook.md §8. The notebook only *shortens the path*; it
never produces Evidence and is never cited — the agent re-reads canonical text
for any answer. Every tool returns the shared :class:`Envelope` contract.
"""

from __future__ import annotations

from typing import Any

from ..notebook import NotebookPage, NotebookRoute
from ..repositories import NotebookRepository
from .envelope import RefStore, ToolStatus, empty_envelope, make_envelope
from .graph_tools import GraphToolError, require_str
from .tools import ToolSpec

NOTEBOOK_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "notebook_index",
        "نقشهٔ موضوعی دفترچه: شاخه‌ها و موضوعات با خلاصهٔ کوتاه — نقطهٔ شروع ناوبری",
        ("domain?",),
    ),
    ToolSpec(
        "open_notebook",
        "خواندن صفحهٔ دفترچهٔ یک گره (شاخه/موضوع/سند): خلاصه + مسیرهای پژوهش پیشنهادی",
        ("node_id",),
    ),
    ToolSpec(
        "notebook_route",
        "مسیرهای پژوهش ثبت‌شده برای یک موضوع، مرتب بر اساس confirmed_hits (هر مسیر status دارد؛ "
        "candidate یعنی هنوز بازبینی نشده) — کوتاه‌کنندهٔ ناوبری، نه استناد",
        ("topic_id",),
    ),
)

NOTEBOOK_TOOL_NAMES = frozenset(spec.name for spec in NOTEBOOK_TOOL_SPECS)


class NotebookTools:
    """Deterministic read-only notebook tools."""

    def __init__(self, repository: NotebookRepository, organization_id: str) -> None:
        self.repository = repository
        self.organization_id = organization_id

    def dispatch(self, store: RefStore, name: str, args: dict[str, Any]) -> Any:
        if name not in NOTEBOOK_TOOL_NAMES:
            raise GraphToolError(f"unknown notebook tool {name!r}")
        handler = getattr(self, f"_{name}")
        return handler(store, args)

    def tool_catalog(self) -> str:
        return "\n".join(f"{spec.signature()} — {spec.description}" for spec in NOTEBOOK_TOOL_SPECS)

    def _notebook_index(self, store: RefStore, args: dict[str, Any]) -> Any:
        domain = args.get("domain")
        if domain is not None and not isinstance(domain, str):
            raise GraphToolError("domain must be a string")
        pages = self.repository.pages(self.organization_id)
        rows = [p for p in pages if p.kind in {"branch", "domain"}]
        if domain:
            rows = [p for p in rows if domain in p.node_id or domain in p.summary]
        if not rows:
            return empty_envelope(store, ToolStatus.EMPTY, ("دفترچه خالی است یا موضوعی یافت نشد",))
        items = tuple(
            {"node_id": p.node_id, "kind": p.kind, "summary": p.summary[:400]} for p in rows
        )
        return make_envelope(store, items, total_count=len(items))

    def _open_notebook(self, store: RefStore, args: dict[str, Any]) -> Any:
        node_id = require_str(args, "node_id")
        page = self.repository.get_page(self.organization_id, node_id)
        if page is None:
            return empty_envelope(
                store, ToolStatus.NOT_FOUND, (f"صفحهٔ دفترچه برای {node_id!r} یافت نشد",)
            )
        routes = self.repository.routes(self.organization_id)
        related = [r for r in routes if r.to_node_id == node_id]
        item = {
            "node_id": page.node_id,
            "kind": page.kind,
            "summary": page.summary,
            "needs_revalidation": page.needs_revalidation,
            "routes": [self._route(r) for r in related],
            "note": "دفترچه فقط کوتاه‌کنندهٔ مسیر است؛ برای پاسخ، متن canonical را دوباره بخوان.",
        }
        return make_envelope(store, (item,))

    def _notebook_route(self, store: RefStore, args: dict[str, Any]) -> Any:
        topic_id = require_str(args, "topic_id")
        routes = self.repository.routes(self.organization_id, topic_id)
        if not routes:
            return empty_envelope(
                store, ToolStatus.EMPTY, (f"برای موضوع {topic_id!r} مسیر ثبت‌شده‌ای نیست",)
            )
        items = tuple(self._route(r) for r in routes)
        return make_envelope(store, items, total_count=len(items))

    @staticmethod
    def _route(route: NotebookRoute) -> dict[str, Any]:
        return {
            "topic_id": route.topic_id,
            "to_node_id": route.to_node_id,
            "weight": round(route.weight, 3),
            "hits": route.hits,
            "confirmed_hits": route.confirmed_hits,
            "status": route.status,
        }


__all__ = ["NOTEBOOK_TOOL_NAMES", "NOTEBOOK_TOOL_SPECS", "NotebookPage", "NotebookTools"]
