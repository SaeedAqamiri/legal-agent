#!/usr/bin/env python3
"""Backfill provision hierarchy (parent_provision_id / depth) in Postgres.

The canonical corpus was ingested before the parsers produced nested
provisions, so nearly every row sits flat at depth 0. This script replays the
shared hierarchy rules (ingestion/hierarchy.py) over each instrument's
provisions — ordered by (ordinal, provision_id) — and UPDATEs the rows whose
computed parent or depth differs.

Idempotent: a second run finds nothing to change. Rows keep their
provision_id (identity is content-addressed), so citations and graph edges
stay valid.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/backfill_provision_parents.py [--dry-run] [--instrument ID]
"""

from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "LEGAL_AGENT_POSTGRES_DSN",
            "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent",
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report without updating")
    parser.add_argument("--instrument", default=None, help="limit to one instrument_id")
    args = parser.parse_args()

    where = "WHERE instrument_id = %s" if args.instrument else ""
    params = (args.instrument,) if args.instrument else ()

    with psycopg.connect(args.dsn, autocommit=True) as db:
        instruments = [
            row[0]
            for row in db.execute(
                f"SELECT DISTINCT instrument_id FROM canonical.provisions {where}"
                " ORDER BY instrument_id",
                params,
            ).fetchall()
        ]
        total = changed = 0
        for instrument_id in instruments:
            rows = db.execute(
                """SELECT provision_id, provision_type, COALESCE(label, ''),
                          parent_provision_id, depth
                   FROM canonical.provisions
                   WHERE instrument_id = %s
                   ORDER BY ordinal, provision_id""",
                (instrument_id,),
            ).fetchall()
            if len(rows) < 2:
                continue
            ranks = [hierarchy_rank(row[1], row[2]) for row in rows]
            parents = assign_parents(ranks)
            depths = depths_from_parents(parents)
            for row, parent_index, depth in zip(rows, parents, depths):
                total += 1
                new_parent = rows[parent_index][0] if parent_index is not None else None
                new_depth = depth
                provision_id, _, _, old_parent, old_depth = row
                if new_parent == old_parent and new_depth == old_depth:
                    continue
                changed += 1
                if args.dry_run:
                    continue
                db.execute(
                    """UPDATE canonical.provisions
                       SET parent_provision_id = %s, depth = %s
                       WHERE provision_id = %s AND instrument_id = %s""",
                    (new_parent, new_depth, provision_id, instrument_id),
                )
            print(f"{instrument_id}: {len(rows)} provisions")

        mode = "would change" if args.dry_run else "changed"
        print(f"\n{mode} {changed} of {total} provisions across {len(instruments)} instruments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
