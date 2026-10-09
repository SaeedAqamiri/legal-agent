"""Hierarchical document outline for query-time navigation.

PageIndex-style: a compact, char-budgeted textual rendering of one
instrument's provision tree (بخش > فصل > ماده > تبصره/بند) so the planner can
orient inside a long document — see the section map, then jump to page ranges
with the page-scoped tools. The tree uses the stored ``parent_provision_id``
hierarchy (parser or backfill), never text heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Serialized-character budget per outline part (planner-visible).
OUTLINE_CHAR_BUDGET = 40_000

#: Container types that render with a bracketed role marker.
_CONTAINERS = frozenset({"chapter", "part"})

_SUMMARY_CHARS = 220


@dataclass(frozen=True, slots=True)
class StructureEntry:
    """One provision of an instrument with its resolved page span."""

    provision_id: str
    label: str
    title: str | None
    provision_type: str
    ordinal: int
    parent_provision_id: str | None
    page: int | None
    end_page: int | None
    summary: str | None = None


def build_outline(entries: list[StructureEntry]) -> tuple[dict[str, Any], int]:
    """Nest entries by the stored parent links; returns (tree, max_depth).

    Entries must be document-ordered (parents precede children), which the
    ordinal sort guarantees.
    """
    nodes: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    depths: dict[str, int] = {}
    for entry in entries:
        depths[entry.provision_id] = 0
        nodes[entry.provision_id] = {
            "provision_id": entry.provision_id,
            "label": entry.label,
            "title": entry.title,
            "type": entry.provision_type,
            "ordinal": entry.ordinal,
            "page": entry.page,
            "end_page": entry.end_page,
            "summary": entry.summary,
            "children": [],
        }
    max_depth = 0
    for entry in entries:
        node = nodes[entry.provision_id]
        parent = nodes.get(entry.parent_provision_id) if entry.parent_provision_id else None
        if parent is None or parent is node:
            roots.append(node)
        else:
            depth = depths[entry.parent_provision_id] + 1
            depths[entry.provision_id] = depth
            max_depth = max(max_depth, depth)
            parent["children"].append(node)
    counter = 0

    def assign(node: dict[str, Any]) -> None:
        nonlocal counter
        counter += 1
        node["node_id"] = f"{counter:04d}"
        for child in node["children"]:
            assign(child)

    for root in roots:
        assign(root)
    tree = {
        "roots": roots,
        "node_count": len(entries),
        "max_depth": max_depth if roots else 0,
    }
    return tree, max_depth


def _page_label(node: dict[str, Any]) -> str:
    page = node["page"]
    end = node["end_page"]
    if page is None:
        return ""
    if end is not None and end != page:
        return f" — صص {page}–{end}"
    return f" — ص {page}"


def _line(node: dict[str, Any], depth: int) -> str:
    indent = "  " * depth
    marker = f"[{node['label']}]" if node["type"] in _CONTAINERS else node["label"]
    title = f" {node['title']}" if node.get("title") else ""
    summary = ""
    if depth > 0 or node["type"] in _CONTAINERS:
        text = (node.get("summary") or "").strip()
        if text:
            summary = " | " + text[:_SUMMARY_CHARS]
    return f"{indent}{node['node_id']} {marker}{title}{_page_label(node)}{summary}"


def render_outline(tree: dict[str, Any], char_budget: int = OUTLINE_CHAR_BUDGET) -> list[str]:
    """Depth-first outline text, split into parts of at most ``char_budget``.

    Every part stands alone (starts with a header line); a single oversized
    line gets its own part so pagination never drops content.
    """
    lines: list[str] = []

    def walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for node in nodes:
            lines.append(_line(node, depth))
            walk(node["children"], depth + 1)

    walk(tree["roots"], 0)
    if not lines:
        return []
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line_size = len(line) + 1
        if current and size + line_size > char_budget:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        parts.append("\n".join(current))
    return parts
