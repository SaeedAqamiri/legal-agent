#!/usr/bin/env python3
"""Derive and store the legal hierarchy as SUBORDINATE_TO graph_edges.

Builds the 5-tier chain (tier 1 constitution -> 2 special statutes/policies
-> 3 ordinary statutes -> 4 regulations/approvals -> 5 circulars/directives)
that the graph UI and the FalkorDB projection expect:

  1. normalizes authority_level tiers where the ingest left them NULL
     (bakhshnameh/dastural -> 5, other regulations -> 4, statutes -> 3)
  2. links every tier >= 2 instrument to its parent:
       - tier 3 statutes and unmatched lower instruments -> the constitution
         (art. 4: all law derives from the constitution)
       - tier 4/5 instruments whose title names a statute (e.g. «... قانون کار»)
         -> that statute, matched on the normalized statute title
  3. writes canonical.graph_edges (edge_type='subordinate_to'), replacing the
     previous run's edges (created_by='hierarchy-batch') so the script is
     idempotent.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/build_hierarchy_edges.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.normalization import normalize_legal_text

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
CREATED_BY = "hierarchy-batch"
SOURCE_ID = "tier-hierarchy"
CIRCULAR_PREFIX = r"^بخشنامه|دستورالعمل|ابلاغیه|نظامنامه"


def log(msg: str) -> None:
    print(msg, flush=True)


def core_title(title: str) -> str:
    """Statute title without dates, [منسوخ] marks and parentheticals."""
    text = normalize_legal_text(title)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"مصوب\s*[\d۰-۹/].*$", " ", text)
    text = re.sub(r"\b1[0-9]{3}(/[0-9]{2,3})?\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    args = parser.parse_args()

    db = psycopg.connect(args.dsn, autocommit=True)
    with db.cursor() as cur:
        # -- 1. normalize missing tiers -----------------------------------
        cur.execute(
            """UPDATE canonical.legal_instruments
                   SET authority_level = '5'
                 WHERE instrument_type = 'regulation'
                   AND title ~ %s
                   AND (authority_level IS NULL OR authority_level = '')""",
            (CIRCULAR_PREFIX,),
        )
        circulars = cur.rowcount
        cur.execute(
            """UPDATE canonical.legal_instruments
                   SET authority_level = '4'
                 WHERE instrument_type = 'regulation'
                   AND (authority_level IS NULL OR authority_level = '')"""
        )
        regulations = cur.rowcount
        cur.execute(
            """UPDATE canonical.legal_instruments
                   SET authority_level = '3'
                 WHERE instrument_type = 'statute'
                   AND (authority_level IS NULL OR authority_level = '')"""
        )
        statutes_fixed = cur.rowcount
        log(f"tier fixes: +5:{circulars} +4:{regulations} +3:{statutes_fixed}")

        cur.execute(
            """SELECT instrument_id, title, instrument_type, authority_level
                 FROM canonical.legal_instruments ORDER BY instrument_id"""
        )
        instruments = cur.fetchall()

        cur.execute(
            """SELECT instrument_id, title FROM canonical.legal_instruments
                 WHERE instrument_type = 'statute' ORDER BY length(title) DESC"""
        )
        statute_titles = cur.fetchall()
        constitution_id = next(
            row[0] for row in instruments if row[2] == "constitution"
        )

        # longest core statute titles first so «قانون کار» style matches pick
        # the most specific parent
        cores = sorted(
            ((row[0], core_title(row[1])) for row in statute_titles),
            key=lambda item: len(item[1]),
            reverse=True,
        )

        def find_parent(instrument_id: str, title: str, tier: str | None) -> str:
            if instrument_id == constitution_id:
                return ""
            core = core_title(title)
            if tier in ("4", "5") and len(core) >= 8:
                for statute_id, statute_core in cores:
                    if len(statute_core) < 4:
                        continue
                    if statute_core in core:
                        return statute_id
            return constitution_id

        edges: dict[str, tuple[str, str]] = {}
        skipped_self = 0
        for instrument_id, title, kind, tier in instruments:
            parent = find_parent(instrument_id, title, tier)
            if not parent:
                continue
            if parent == instrument_id:
                skipped_self += 1
                continue
            edges[f"edge:hier:{hashlib.sha1(f'{instrument_id}|{parent}'.encode()).hexdigest()[:24]}"] = (
                instrument_id,
                parent,
            )
        log(
            f"hierarchy edges derived: {len(edges)} "
            f"(constitution root, self-loops skipped: {skipped_self})"
        )

        # -- 3. replace previous batch, insert fresh ------------------------
        cur.execute(
            "DELETE FROM canonical.graph_edges WHERE created_by = %s", (CREATED_BY,)
        )
        removed = cur.rowcount
        for edge_id, (source_id, target_id) in edges.items():
            cur.execute(
                """INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_by, source_id
                   ) VALUES (%s, 'legal_instrument', %s, 'legal_instrument', %s,
                             'subordinate_to', NULL, 'parser', 0.9, %s, %s)
                   ON CONFLICT (edge_id) DO NOTHING""",
                (edge_id, source_id, target_id, CREATED_BY, SOURCE_ID),
            )

        cur.execute(
            """SELECT count(*), count(DISTINCT target_node_id)
                 FROM canonical.graph_edges WHERE edge_type = 'subordinate_to'"""
        )
        total, parents = cur.fetchone()
        cur.execute(
            """SELECT authority_level, count(*) FROM canonical.legal_instruments
                 GROUP BY 1 ORDER BY 1 NULLS LAST"""
        )
        tier_counts = cur.fetchall()
    log(f"replaced previous edges: {removed}")
    log(f"subordinate_to edges now: {total} -> {parents} distinct parents")
    log(f"tier distribution: {tier_counts}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
