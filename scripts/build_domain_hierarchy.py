#!/usr/bin/env python3
"""Build a domain-aware hierarchy chain from every law up to the constitution.

For each instrument (tier 2..5) the parent is the nearest higher-tier
instrument in the SAME subject_domain (tier-1 constitution is the root and
fallback). Emits canonical.graph_edges rows with edge_type='subordinate_to',
created_by='domain-hierarchy', extraction_method='derived' — a structural
approximation (NOT an evidence-based legal basis), so it is clearly labelled
and low-confidence; evidence-based IMPLEMENTS/AMENDS edges stay separate.

Result: every law has a path  law -> ... -> constitution, colour-coded by tier
in the UI. Idempotent: replaces the previous 'domain-hierarchy' batch.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/build_domain_hierarchy.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
CREATED_BY = "domain-hierarchy"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    args = parser.parse_args()

    db = psycopg.connect(args.dsn, autocommit=True)
    with db.cursor() as cur:
        cur.execute(
            """SELECT instrument_id, title, COALESCE(authority_level,'3'), subject_domain,
                      (SELECT count(*) FROM canonical.provisions p WHERE p.instrument_id = i.instrument_id)
                 FROM canonical.legal_instruments i"""
        )
        rows = cur.fetchall()

    constitution = next((r[0] for r in rows if r[2] == "1"), None)
    if constitution is None:
        log("no constitution instrument found — abort")
        return 1

    # domain -> tier -> list of (instrument_id, provisions) sorted for determinism
    by_domain: dict[str, dict[int, list[tuple[str, int]]]] = {}
    for instrument_id, title, tier, domain, provisions in rows:
        t = int(tier)
        if t == 1:
            continue
        by_domain.setdefault(domain or "سایر", {}).setdefault(t, []).append(
            (instrument_id, provisions or 0)
        )

    edges: list[tuple[str, str]] = []
    to_constitution = 0
    for domain, tiers in by_domain.items():
        for tier, members in tiers.items():
            parents: list[tuple[str, int]] = []
            for higher in range(tier - 1, 1, -1):  # nearest higher tier in domain
                if tiers.get(higher):
                    parents = sorted(tiers[higher], key=lambda item: (-item[1], item[0]))
                    break
            for instrument_id, _ in members:
                if parents:
                    parent = parents[0][0]
                else:
                    parent = constitution
                    to_constitution += 1
                if parent != instrument_id:
                    edges.append((instrument_id, parent))

    with db.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        cur.execute("DELETE FROM canonical.graph_edges WHERE created_by = %s", (CREATED_BY,))
        removed = cur.rowcount
        cur.execute("SET session_replication_role = DEFAULT")
        for child, parent in edges:
            edge_id = "edge:domhier:" + hashlib.sha1(f"{child}|{parent}".encode()).hexdigest()[:24]
            cur.execute(
                """INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_by, source_id
                   ) VALUES (%s, 'legal_instrument', %s, 'legal_instrument', %s,
                             'subordinate_to', NULL, 'derived', 0.5, %s, 'domain-tier')
                   ON CONFLICT (edge_id) DO NOTHING""",
                (edge_id, child, parent, CREATED_BY),
            )
        cur.execute(
            "SELECT count(*) FROM canonical.graph_edges WHERE edge_type='subordinate_to'"
        )
        total = cur.fetchone()[0]
    log(f"removed previous: {removed}")
    log(f"domain-hierarchy edges: {len(edges)} (direct-to-constitution: {to_constitution})")
    log(f"subordinate_to total now: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
