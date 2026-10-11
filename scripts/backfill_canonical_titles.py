#!/usr/bin/env python3
"""Normalize canonical.legal_instruments.canonical_title in place.

`canonical_title` is the normalized identity of an instrument and must equal
`sources.instruments.canonical_key` for the cross-schema joins (source file,
effects, mirror url). The older laws pipeline stored the *raw* title here
(ingest_cats_data stored the normalized one), so the two corpora disagreed and
instruments ingested by one pipeline were invisible to joins written for the
other. This backfill makes every row normalized; the ingestion pipeline now
normalizes on write, so re-running is a no-op.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/backfill_canonical_titles.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.normalization import normalize_legal_text

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()

    conn = psycopg.connect(args.dsn, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, canonical_title FROM canonical.legal_instruments")
        rows = cur.fetchall()

    updates = []
    for instrument_id, current in rows:
        normalized = normalize_legal_text(current or "")
        if normalized != (current or ""):
            updates.append((normalized, instrument_id))

    print(f"instruments: {len(rows)}, need normalization: {len(updates)}")
    if args.apply and updates:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE canonical.legal_instruments SET canonical_title = %s WHERE instrument_id = %s",
                updates,
            )
        print(f"updated {len(updates)} rows")
    elif updates:
        print("dry-run: pass --apply to write")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
