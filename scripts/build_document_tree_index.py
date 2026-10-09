#!/usr/bin/env python3
"""Build per-instrument document tree indexes (PageIndex-style).

Walks canonical.provisions of every instrument, derives the legal forest with
the shared hierarchy rules, computes page ranges from source spans, and emits
one JSON tree per instrument under out/document_trees/:

  {instrument_id, title, type, node_count, depth, structure: [
      {node_id, provision_id, label, title, type, page, start_page, end_page,
       summary?, children: [...]}]}

The structure tool at query time derives the same tree from the repository
directly; these files exist for offline inspection, the UI and the LLM
summarizer (scripts/summarize_tree_nodes.py) which patches «summary» fields.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/build_document_tree_index.py [--instrument ID] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.hierarchy import (
    assign_parents,
    depths_from_parents,
    hierarchy_rank,
)

TREE_DIR = Path("out/document_trees")
STRUCTURE_STAGE = "structure"
STRUCTURE_PROMPT_VERSION = "tree-v1"


def node_key(provision_id: str) -> str:
    return provision_id


def build_tree(rows, pages_by_version) -> dict:
    """rows: (provision_id, type, label, title, ordinal, pv_id); pages: pv_id -> [p, p]."""
    ranks = [hierarchy_rank(row[1], row[2]) for row in rows]
    parents = assign_parents(ranks)
    depths = depths_from_parents(parents)
    nodes: dict[str, dict] = {}
    roots: list[dict] = []
    for index, (row, parent_index, depth) in enumerate(zip(rows, parents, depths)):
        provision_id, ptype, label, title, ordinal, pv_id = row
        pages = pages_by_version.get(pv_id, [])
        node = {
            "node_id": f"{index + 1:04d}",
            "provision_id": provision_id,
            "label": label,
            "type": ptype,
            "title": title,
            "ordinal": ordinal,
            "depth": depth,
            "page": pages[0] if pages else None,
            "start_page": min(pages) if pages else None,
            "end_page": max(pages) if pages else None,
            "children": [],
        }
        nodes[provision_id] = node
        if parent_index is None:
            roots.append(node)
        else:
            nodes[rows[parent_index][0]]["children"].append(node)
    return {"structure": roots}


def propagate_page_ranges(nodes: list[dict]) -> None:
    for node in nodes:
        propagate_page_ranges(node["children"])
        if node["children"]:
            starts = [c["start_page"] for c in node["children"] if c["start_page"]]
            ends = [c["end_page"] for c in node["children"] if c["end_page"]]
            if node["start_page"] is None and starts:
                node["start_page"] = min(starts)
            if node["end_page"] is None and ends:
                node["end_page"] = max(ends)


def load_summaries(db) -> dict[str, dict[str, str]]:
    """instrument_id -> {provision_id: summary}, from canonical.provision_summaries."""
    try:
        rows = db.execute(
            "SELECT instrument_id, provision_id, summary FROM canonical.provision_summaries"
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        return {}
    merged: dict[str, dict[str, str]] = {}
    for instrument_id, provision_id, summary in rows:
        merged.setdefault(instrument_id, {})[provision_id] = summary
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "LEGAL_AGENT_POSTGRES_DSN",
            "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent",
        ),
    )
    parser.add_argument("--out", default=TREE_DIR, type=Path)
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--limit", type=int, default=0, help="only N instruments")
    args = parser.parse_args()

    params = (args.instrument,) if args.instrument else ()

    args.out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(args.dsn, autocommit=True) as db:
        condition = "AND i.instrument_id = %s" if args.instrument else ""
        instruments = db.execute(
            f"""SELECT i.instrument_id, i.title, i.instrument_type
                FROM canonical.legal_instruments i
                WHERE EXISTS (SELECT 1 FROM canonical.provisions p WHERE p.instrument_id = i.instrument_id)
                {condition}
                ORDER BY i.instrument_id""",
            params,
        ).fetchall()
        if args.limit:
            instruments = instruments[: args.limit]

        pages_by_version: dict[str, list[int]] = {}
        for pv_id, page in db.execute(
            "SELECT provision_version_id, page_number FROM canonical.source_spans WHERE page_number IS NOT NULL"
        ).fetchall():
            pages_by_version.setdefault(pv_id, []).append(page)

        summaries_by_instrument = load_summaries(db)

        written = 0
        for instrument_id, title, itype in instruments:
            rows = db.execute(
                """SELECT p.provision_id, p.provision_type, COALESCE(p.label, ''),
                           p.title, p.ordinal,
                           (SELECT pv.provision_version_id
                            FROM canonical.provision_versions pv
                            WHERE pv.provision_id = p.provision_id
                            ORDER BY pv.provision_version_id LIMIT 1)
                    FROM canonical.provisions p
                    WHERE p.instrument_id = %s
                    ORDER BY p.ordinal, p.provision_id""",
                (instrument_id,),
            ).fetchall()
            if not rows:
                continue
            tree = build_tree(rows, pages_by_version)
            propagate_page_ranges(tree["structure"])
            summaries = summaries_by_instrument.get(instrument_id, {})
            if summaries:
                stack = list(tree["structure"])
                while stack:
                    node = stack.pop()
                    stack.extend(node["children"])
                    text = summaries.get(node["provision_id"])
                    if text:
                        node["summary"] = text
            payload = {
                "instrument_id": instrument_id,
                "title": title,
                "type": itype,
                "node_count": len(rows),
                "structure": tree["structure"],
            }
            path = args.out / f"{instrument_id}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            written += 1
            if written % 250 == 0:
                print(f"{written}/{len(instruments)} instruments...")

        print(f"wrote {written} tree files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
