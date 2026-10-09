#!/usr/bin/env python3
"""Final enrichment stats + QA sample for the cats graph work.

Writes out/cats_enrichment_stats.json:
  - node/edge counts by type (before/after baselines hardcoded)
  - legal_effects by type & review status
  - edge provenance breakdown (cats_llm_sweep vs laws_ingest vs ...)
  - a random sample of new edges with their documents for human spot-checks

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/cats_enrichment_stats.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

BASELINE = {"instruments": 651, "provisions": 46898, "graph_edges_total": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--out", default=Path("out/cats_enrichment_stats.json"), type=Path)
    parser.add_argument("--sample", type=int, default=12)
    args = parser.parse_args()

    with psycopg.connect(args.dsn, autocommit=True) as db:
        stats = {
            "generated_at": db.execute("SELECT now()").fetchone()[0].isoformat(),
            "instruments_by_type": dict(db.execute(
                "SELECT instrument_type, count(*) FROM canonical.legal_instruments GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()),
            "instruments_total": db.execute("SELECT count(*) FROM canonical.legal_instruments").fetchone()[0],
            "provisions_total": db.execute("SELECT count(*) FROM canonical.provisions").fetchone()[0],
            "articles_with_number": db.execute(
                "SELECT count(*) FROM canonical.provisions WHERE provision_type='article' AND number IS NOT NULL"
            ).fetchone()[0],
            "edges_by_type": dict(db.execute(
                "SELECT edge_type, count(*) FROM canonical.graph_edges GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()),
            "edges_by_provenance": dict(db.execute(
                """SELECT created_by AS who, count(*)
                   FROM canonical.graph_edges GROUP BY 1 ORDER BY 2 DESC"""
            ).fetchall()),
            "legal_effects_by_type": dict(db.execute(
                "SELECT effect_type, count(*) FROM sources.legal_effects GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()),
            "legal_effects_by_review": dict(db.execute(
                "SELECT review_status, count(*) FROM sources.legal_effects GROUP BY 1"
            ).fetchall()),
            "extraction_jobs": db.execute(
                "SELECT count(*) FROM sources.extraction_jobs WHERE prompt_version='cats-rel-v1'"
            ).fetchone()[0],
            "staged_docs_by_cat": dict(db.execute(
                """SELECT metadata->>'cat' AS cat, count(*) FROM sources.source_documents
                   WHERE source_id='ekhtebar' AND metadata ? 'cat' GROUP BY 1 ORDER BY 2 DESC"""
            ).fetchall()),
            "missing_provision_link": {},
        }

        rows = db.execute(
            """SELECT e.edge_type, e.confidence, si.title, di.title
               FROM canonical.graph_edges e
               LEFT JOIN canonical.legal_instruments si
                 ON si.instrument_id = CASE WHEN e.source_node_id LIKE 'inst_%%' THEN e.source_node_id END
               LEFT JOIN canonical.legal_instruments di
                 ON di.instrument_id = CASE WHEN e.target_node_id LIKE 'inst_%%' THEN e.target_node_id END
               WHERE e.created_by IN ('cats_llm_sweep','cats_fetch_links')
               ORDER BY random() LIMIT %s""",
            (args.sample,),
        ).fetchall()
        stats["qa_sample"] = [
            {"edge_type": t, "confidence": c, "source": s, "target": d}
            for t, c, s, d in rows
        ]

    stats["delta_instruments"] = stats["instruments_total"] - BASELINE["instruments"]
    stats["delta_provisions"] = stats["provisions_total"] - BASELINE["provisions"]

    args.out.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1)[:2600])
    print(f"...\nstats: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
