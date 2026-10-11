#!/usr/bin/env python3
"""Backfill canonical.provisions.number from the provision label.

The ingest stored article numbers only inside the label («ماده ۳۳۶»,
«ماده۵», «تبصره۲ـ») leaving number NULL for all but one row, which breaks
resolve_bare_refs() in publish_relations.py (it joins on provisions.number).
This script extracts the leading number for ماده/تبصره/بند labels, converts
Persian digits to ASCII, and fills number where NULL. Idempotent.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/backfill_provision_numbers.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    args = parser.parse_args()

    import re

    label_re = re.compile(r"^(ماده|تبصره|بند|فصل|باب)\s*([۰-۹0-9]+)")

    db = psycopg.connect(args.dsn, autocommit=True)
    with db.cursor() as cur:
        cur.execute(
            """SELECT provision_id, label FROM canonical.provisions
                 WHERE number IS NULL AND label IS NOT NULL"""
        )
        rows = cur.fetchall()
        updates = []
        for provision_id, label in rows:
            match = label_re.match(label.strip().translate(DIGITS))
            if match:
                updates.append((match.group(2), provision_id))
        for number, provision_id in updates:
            cur.execute(
                "UPDATE canonical.provisions SET number = %s WHERE provision_id = %s",
                (number, provision_id),
            )
        cur.execute(
            "SELECT count(*), count(number) FROM canonical.provisions"
        )
        total, with_number = cur.fetchone()
    print(f"labels scanned: {len(rows)}, numbers backfilled: {len(updates)}")
    print(f"provisions with number now: {with_number}/{total}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
