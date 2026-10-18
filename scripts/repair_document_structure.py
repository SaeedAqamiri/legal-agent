#!/usr/bin/env python3
"""Repair the structural containers created before instrument-scoped ids.

The original extractor built container ids from ``kind|num|rank|title`` without
the instrument, so identical chapters in different laws collided: the second
law's container insert was skipped and its articles were reparented to the
first law's container (1321 cross-instrument parents). This repair:

  1. nulls every child of a struct container and deletes all struct containers,
  2. re-plans each instrument from its markdown with NEW instrument-scoped ids,
  3. inserts containers (ordinal placed in the existing gap before their first
     article — articles are NOT renumbered again),
  4. reparents articles and recomputes depth.

Idempotent and safe to re-run. Dry-run unless --apply.

    .venv/bin/python scripts/repair_document_structure.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from extract_document_structure import (
    Container,
    norm_label,
    parse_tokens,
)

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@127.0.0.1:5432/legal_agent"


def plan_containers(provisions, md_text: str, instrument_id: str):
    """Return (containers, parent_updates, md_tokens) using current ordinals."""
    by_label: dict[str, list[tuple[str, int]]] = {}
    for pid, label, _ptype, ordinal, _parent, _hk in provisions:
        by_label.setdefault(norm_label(label), []).append((pid, ordinal))
    stack: list[Container] = []
    containers: list[Container] = []
    parent_updates: dict[str, str] = {}
    md_tokens = 0
    for kind, payload in parse_tokens(md_text, instrument_id):
        if kind == "container":
            while stack and stack[-1].rank >= payload.rank:
                stack.pop()
            payload.parent_id = stack[-1].provision_id if stack else None
            containers.append(payload)
            stack.append(payload)
            continue
        md_tokens += 1
        candidates = by_label.get(payload)
        if not candidates:
            continue
        pid, ordinal = candidates.pop(0)
        for container in stack:
            if container.first_article_ordinal is None:
                container.first_article_ordinal = ordinal
        if stack:
            parent_updates[pid] = stack[-1].provision_id
    # ordinals: innermost container nearest its first article, in the gap
    by_id = {c.provision_id: c for c in containers}
    trailing = 0
    for container in containers:
        fa = container.first_article_ordinal
        if fa is None:
            trailing += 1
            container.ordinal = 10**9 + trailing
            continue
        slot = 1
        parent = by_id.get(container.parent_id) if container.parent_id else None
        while parent is not None and parent.first_article_ordinal == fa:
            slot += 1
            parent = by_id.get(parent.parent_id) if parent.parent_id else None
        container.ordinal = fa - slot
    return containers, parent_updates, md_tokens


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LAWS_DATA_DIR", "/home/saeed/laws-mcp/data")))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = psycopg.connect(args.dsn)
    with db.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        # 1) detach + delete all existing struct containers
        cur.execute(
            """UPDATE canonical.provisions SET parent_provision_id = NULL, depth = 0
                WHERE parent_provision_id IN
                      (SELECT provision_id FROM canonical.provisions WHERE human_key LIKE 'struct:%')"""
        )
        detached = cur.rowcount
        cur.execute("DELETE FROM canonical.provisions WHERE human_key LIKE 'struct:%'")
        deleted = cur.rowcount
        cur.execute("SET session_replication_role = DEFAULT")
    db.commit()
    print(f"detached {detached} children, deleted {deleted} old containers")

    with db.cursor() as cur:
        cur.execute(
            """SELECT i.instrument_id, d.text_path FROM canonical.legal_instruments i
                 JOIN sources.instruments si ON si.canonical_title = i.canonical_title
                 JOIN sources.source_documents d ON d.instrument_uid = si.instrument_uid
                WHERE d.text_path IS NOT NULL ORDER BY i.instrument_id"""
        )
        instruments = cur.fetchall()

    inserted = repaired = 0
    for instrument_id, text_path in instruments:
        path = Path(text_path)
        if not path.exists():
            path = args.data_dir / path.name
        if not path.exists():
            continue
        md_text = path.read_text(encoding="utf-8", errors="replace")
        with db.cursor() as cur:
            cur.execute(
                """SELECT provision_id, label, provision_type, ordinal,
                          parent_provision_id, human_key
                     FROM canonical.provisions WHERE instrument_id = %s
                    ORDER BY ordinal, provision_id""",
                (instrument_id,),
            )
            provisions = cur.fetchall()
        # Only repair instruments that were already structurally processed
        # (their ordinals were spread out). Untouched instruments stay flat —
        # renumbering them here caused unique-constraint churn for no gain.
        if not provisions or min(row[3] for row in provisions) < 100:
            continue
        containers, parent_updates, md_tokens = plan_containers(provisions, md_text, instrument_id)
        if not containers or not parent_updates:
            continue
        # skip instruments whose markdown barely matches (avoid junk structure)
        if md_tokens < 5 or len(parent_updates) / max(md_tokens, 1) < 0.9:
            continue
        # dedup container ordinals against both other containers and the
        # existing provisions still in place under their current parent
        seen: set[tuple[str | None, int]] = {(row[4], row[3]) for row in provisions}
        for container in containers:
            key = (container.parent_id, container.ordinal)
            while key in seen:
                container.ordinal = container.ordinal + 1 if container.ordinal <= 0 else container.ordinal - 1
                key = (container.parent_id, container.ordinal)
            seen.add(key)
        if not args.apply:
            inserted += len(containers)
            repaired += len(parent_updates)
            continue
        try:
            with db.cursor() as cur:
                cur.execute("SET session_replication_role = replica")
                for container in containers:
                    cur.execute(
                        """INSERT INTO canonical.provisions
                               (provision_id, instrument_id, provision_type, number, label,
                                title, parent_provision_id, ordinal, depth, human_key, schema_version)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, 1)
                           ON CONFLICT (provision_id) DO NOTHING""",
                        (
                            container.provision_id,
                            instrument_id,
                            "part" if container.rank <= 2 else "chapter",
                            container.num,
                            f"{container.kind} {container.num}",
                            container.title or None,
                            container.parent_id,
                            container.ordinal,
                            "struct:" + container.provision_id,
                        ),
                    )
                for pid, parent_id in parent_updates.items():
                    cur.execute(
                        "UPDATE canonical.provisions SET parent_provision_id = %s WHERE provision_id = %s",
                        (parent_id, pid),
                    )
                cur.execute(
                    """WITH RECURSIVE tree AS (
                           SELECT provision_id, 0 AS depth FROM canonical.provisions
                            WHERE instrument_id = %s AND parent_provision_id IS NULL
                           UNION ALL
                           SELECT p.provision_id, t.depth + 1 FROM canonical.provisions p
                             JOIN tree t ON p.parent_provision_id = t.provision_id
                            WHERE p.instrument_id = %s)
                       UPDATE canonical.provisions p SET depth = t.depth
                         FROM tree t WHERE p.provision_id = t.provision_id""",
                    (instrument_id, instrument_id),
                )
                cur.execute("SET session_replication_role = DEFAULT")
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one bad doc must not abort the batch
            db.rollback()
            print(f"  skip {instrument_id}: {str(exc)[:90]}")
            continue
        inserted += len(containers)
        repaired += len(parent_updates)

    verb = "inserted" if args.apply else "would insert"
    print(f"{verb} {inserted} containers; {'reparented' if args.apply else 'would reparent'} {repaired} articles")
    with db.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM canonical.provisions c JOIN canonical.provisions p
                ON c.parent_provision_id = p.provision_id
               WHERE c.instrument_id <> p.instrument_id"""
        )
        print("cross-instrument parents now:", cur.fetchone()[0])
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
