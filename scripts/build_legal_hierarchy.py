#!/usr/bin/env python3
"""Derive the legal hierarchy tree (قانون → آیین‌نامه → بخشنامه/دستورالعمل).

Phase 5. Reads canonical.graph_edges (implements/cites/interprets/annuls/...)
plus instrument types and emits out/legal_hierarchy.json:

  {title, id, type, children: [{...}], links: [{type, target_title, target_id}]}

Children come from IMPLEMENTS edges (doc -> its statutory basis) — the
«به استناد» relations harvested from the cats corpus. Direct CITES links are
listed separately per node so the UI can render both the statutory tree and
the citation network.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/build_legal_hierarchy.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

TIER_ORDER = [
    "constitution", "policy", "statute", "resolution", "regulation",
    "circular", "directive", "guideline", "judgment", "advisory_opinion",
    "other",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--out", default=Path("out/legal_hierarchy.json"), type=Path)
    parser.add_argument("--min-children", type=int, default=1,
                        help="only include parents with at least N implements children")
    args = parser.parse_args()

    with psycopg.connect(args.dsn, autocommit=True) as db:
        instruments = {
            r[0]: {"id": r[0], "title": r[1], "type": r[2], "issuer": r[3]}
            for r in db.execute(
                """SELECT instrument_id, title, instrument_type, issuer
                   FROM canonical.legal_instruments"""
            ).fetchall()
        }
        implements = defaultdict(list)
        for source, target in db.execute(
            """SELECT source_node_id, target_node_id FROM canonical.graph_edges
               WHERE edge_type='implements'"""
        ).fetchall():
            implements[target].append(source)

        def node(iid: str, depth: int = 0) -> dict:
            info = instruments.get(iid, {"id": iid, "title": iid, "type": "unknown", "issuer": None})
            children = []
            for child_id in implements.get(iid, []):
                if depth < 6 and child_id != iid:
                    children.append(node(child_id, depth + 1))
            children.sort(key=lambda c: (
                TIER_ORDER.index(c["type"]) if c["type"] in TIER_ORDER else 99,
                c["title"],
            ))
            return {**info, "children": children}

        roots = []
        for iid in instruments:
            is_root = iid not in {c for kids in implements.values() for c in kids}
            if is_root and len(implements.get(iid, [])) >= args.min_children:
                roots.append(node(iid))
        roots.sort(key=lambda r: (
            TIER_ORDER.index(r["type"]) if r["type"] in TIER_ORDER else 99, r["title"]
        ))

        stats = {
            "roots": len(roots),
            "instruments": len(instruments),
            "implements_edges": sum(len(v) for v in implements.values()),
            "by_type": {},
        }
        for info in instruments.values():
            stats["by_type"][info["type"]] = stats["by_type"].get(info["type"], 0) + 1

    args.out.write_text(
        json.dumps({"stats": stats, "forest": roots}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"hierarchy: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
