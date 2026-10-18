#!/usr/bin/env python3
"""Build the display tree projection (docs/qavanin/06_law_notebook.md §4).

Reads canonical instruments + statutory-basis implements edges from Postgres,
places every document by the derivation-first / topic-fallback rule, and writes
``out/display_tree.json``. Read-only: nothing is written to the database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.display_tree import DisplayInstrument, build_display_tree

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--out", default="out/display_tree.json", type=Path)
    args = parser.parse_args()

    db = psycopg.connect(args.dsn, autocommit=True)
    with db.cursor() as cur:
        cur.execute(
            """SELECT i.instrument_id, i.title, i.instrument_type,
                      i.authority_level, i.subject_domain, s.summary
                 FROM canonical.legal_instruments i
                 LEFT JOIN canonical.instrument_summaries s
                   ON s.instrument_id = i.instrument_id
                ORDER BY i.instrument_id"""
        )
        instruments = [
            DisplayInstrument(
                instrument_id=row[0],
                title=row[1],
                kind=row[2],
                tier=int(row[3]) if row[3] else None,
                domain=row[4],
                summary=row[5],
            )
            for row in cur.fetchall()
        ]
        cur.execute(
            """SELECT source_node_id, target_node_id, confidence
                 FROM canonical.graph_edges
                WHERE edge_type = 'implements' AND claim = 'statutory_basis'"""
        )
        edges = [(row[0], row[1], row[2]) for row in cur.fetchall()]
        constitution_id = next(
            (i.instrument_id for i in instruments if i.tier == 1), None
        )

    tree = build_display_tree(instruments, edges, constitution_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    stats = tree["stats"]
    print(
        f"display tree -> {args.out}\n"
        f"  instruments={stats['instruments']} "
        f"derivation={stats['placed_by_derivation']} "
        f"topic={stats['placed_by_topic']} branches={stats['branches']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
