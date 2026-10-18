"""Display tree: a single navigable projection over the legal corpus.

Docs/qavanin/06_law_notebook.md §4. This is a *presentation* tree for the
agent, NOT a legal claim: every parent link is labelled with its basis:

  * ``derivation`` — the document implements a specific law (edge_type
    ``implements`` with ``claim='statutory_basis'``); the child sits under the
    law it executes.
  * ``topic`` — no specific statutory parent, so the document sits under its
    ``subject_domain`` folder.

The graph stays the source of truth; the tree only orders things so the agent
can descend like a filesystem (root -> branch -> domain -> law -> article).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: L1 branch -> the 33 subject_domain values it groups (docs 06 §4).
BRANCHES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("civil_family", "حقوق مدنی و خانواده", ("مدنی", "خانواده", "ثبت و اسناد")),
    (
        "criminal_procedure",
        "حقوق کیفری و آیین دادرسی",
        ("کیفری", "آیین دادرسی"),
    ),
    (
        "commerce_finance",
        "حقوق تجارت، مالی و بانکی",
        (
            "تجارت و شرکت",
            "بانکی و پولی",
            "مالیاتی",
            "بیمه",
            "گمرک و تجارت خارجی",
            "اقتصاد و سرمایه‌گذاری",
        ),
    ),
    (
        "labor_welfare",
        "حقوق کار، رفاه و تأمین اجتماعی",
        ("کار و تأمین اجتماعی", "رفاه و حمایت اجتماعی"),
    ),
    (
        "admin_budget",
        "حقوق اداری، استخدامی و بودجه",
        ("اداری و استخدامی", "بودجه و برنامه", "شهرداری و شهرسازی"),
    ),
    (
        "public_sovereign",
        "حقوق عمومی و حاکمیتی",
        (
            "انتخابات",
            "دفاعی و امنیتی",
            "انرژی",
            "نفت و گاز",
            "محیط زیست",
            "فرهنگ و رسانه",
            "ورزش",
            "آموزش و پژوهش",
            "بهداشت و درمان",
        ),
    ),
    (
        "economy_infra",
        "حقوق اقتصادی و زیربنایی",
        (
            "صنعت و معدن",
            "کشاورزی و منابع طبیعی",
            "حمل‌ونقل",
            "مسکن و زمین",
            "ارتباطات و فناوری اطلاعات",
            "دانش‌بنیان و فناوری",
        ),
    ),
    ("judicial_procedure", "حقوق قضایی و رویه", ("قضایی و دادگستری", "سایر")),
)

_DOMAIN_TO_BRANCH: dict[str, str] = {
    domain: branch_id for branch_id, _name, domains in BRANCHES for domain in domains
}
_BRANCH_NAME: dict[str, str] = {
    branch_id: name for branch_id, name, _domains in BRANCHES
}

UNCLASSIFIED_BRANCH = "unclassified"
UNCLASSIFIED_NAME = "طبقه‌بندی‌نشده"
ROOT_ID = "root"
ROOT_LABEL = "کتابخانهٔ حقوق ایران"


@dataclass(slots=True)
class DisplayInstrument:
    instrument_id: str
    title: str
    kind: str | None = None
    tier: int | None = None
    domain: str | None = None
    summary: str | None = None


def build_display_tree(
    instruments: list[DisplayInstrument],
    implements_edges: list[tuple[str, str, float | None]],
    constitution_id: str | None,
) -> dict[str, Any]:
    """Build the display forest.

    ``implements_edges`` are ``(child_id, parent_id, confidence)`` tuples from
    ``implements``/``statutory_basis`` edges. Edges pointing at the
    constitution are ignored as structural parents (they are generic
    authority, not a subject-matter parent). Ties break on higher confidence,
    then on parent id for determinism.
    """
    by_id = {item.instrument_id: item for item in instruments}

    # choose ONE derivation parent per child (never the constitution)
    best_parent: dict[str, tuple[str, float | None]] = {}
    for child, parent, confidence in implements_edges:
        if child == parent or child not in by_id or parent not in by_id:
            continue
        if parent == constitution_id:
            continue
        current = best_parent.get(child)
        if current is None or (confidence or 0.0) > (current[1] or 0.0):
            best_parent[child] = (parent, confidence)

    # break cycles: keep only edges that do not create a loop
    parent_of: dict[str, str] = {}
    for child in sorted(best_parent):
        parent = best_parent[child][0]
        seen = {child}
        node = parent
        while node in best_parent:
            if node in seen:
                parent = None  # type: ignore[assignment]
                break
            seen.add(node)
            node = best_parent[node][0]
        if parent is not None:
            parent_of[child] = parent

    children: dict[str, list[str]] = {}
    placed_by_derivation: set[str] = set()
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)
        placed_by_derivation.add(child)

    def instrument_node(instrument_id: str, basis: str, confidence: float | None) -> dict[str, Any]:
        item = by_id[instrument_id]
        node: dict[str, Any] = {
            "id": instrument_id,
            "kind": "instrument",
            "label": item.title,
            "title": item.title,
            "tier": item.tier,
            "instrument_kind": item.kind,
            "domain": item.domain,
            "basis": basis,
            "children": [],
        }
        if item.summary:
            node["summary"] = item.summary
        if confidence is not None:
            node["confidence"] = confidence
        for child_id in sorted(children.get(instrument_id, []), key=lambda i: by_id[i].title):
            node["children"].append(
                instrument_node(child_id, "derivation", best_parent[child_id][1])
            )
        return node

    # domain folders -> branch folders
    domains: dict[str, list[str]] = {}
    for item in instruments:
        if item.instrument_id in placed_by_derivation:
            continue
        if item.instrument_id == constitution_id:
            continue
        domains.setdefault(item.domain or "", []).append(item.instrument_id)

    branches: dict[str, list[dict[str, Any]]] = {}
    for domain, ids in domains.items():
        branch_id = _DOMAIN_TO_BRANCH.get(domain, UNCLASSIFIED_BRANCH)
        domain_node: dict[str, Any] = {
            "id": f"domain:{domain or 'unclassified'}",
            "kind": "domain",
            "label": domain or UNCLASSIFIED_NAME,
            "children": [
                instrument_node(i, "topic", None)
                for i in sorted(ids, key=lambda i: by_id[i].title)
            ],
        }
        branches.setdefault(branch_id, []).append(domain_node)

    branch_nodes: list[dict[str, Any]] = []
    for branch_id, name, _domains in BRANCHES:
        domain_nodes = sorted(branches.pop(branch_id, []), key=lambda n: n["label"])
        branch_nodes.append(
            {
                "id": f"branch:{branch_id}",
                "kind": "branch",
                "label": name,
                "children": domain_nodes,
            }
        )
    if branches.get(UNCLASSIFIED_BRANCH):
        branch_nodes.append(
            {
                "id": f"branch:{UNCLASSIFIED_BRANCH}",
                "kind": "branch",
                "label": UNCLASSIFIED_NAME,
                "children": sorted(branches[UNCLASSIFIED_BRANCH], key=lambda n: n["label"]),
            }
        )

    root_children: list[dict[str, Any]] = []
    if constitution_id and constitution_id in by_id:
        constitution = by_id[constitution_id]
        constitution_node = instrument_node(constitution_id, "root", None)
        constitution_node["label"] = constitution.title
        root_children.append(constitution_node)
    root_children.extend(branch_nodes)

    return {
        "id": ROOT_ID,
        "kind": "root",
        "label": ROOT_LABEL,
        "stats": {
            "instruments": len(instruments),
            "placed_by_derivation": len(placed_by_derivation),
            "placed_by_topic": sum(len(v) for v in domains.values()),
            "branches": len(branch_nodes),
        },
        "children": root_children,
    }
