#!/usr/bin/env python3
"""Seed the law notebook from the display tree + canonical summaries.

Docs/qavanin/06_law_notebook.md §6. Creates stable topic rows (branches +
domains) and base pages whose summary is a compact digest of the child
documents' canonical one-line summaries. Idempotent (upserts).

    .venv/bin/python scripts/build_notebook.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres_notebook import PostgresNotebookRepository
from legal_agent_core.notebook import NotebookPage

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
DEFAULT_ORG = os.environ.get("LEGAL_AGENT_ORGANIZATION", "org-a")
_SUMMARY_CHARS = 90
_MAX_CHILD_SUMMARIES = 8


def _digest(items: list[tuple[str, str | None]], limit: int = _MAX_CHILD_SUMMARIES) -> str:
    lines = []
    for title, summary in items[:limit]:
        text = (summary or title).strip().replace("\n", " ")
        lines.append(f"- {title[:60]}: {text[:_SUMMARY_CHARS]}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--tree", default="out/display_tree.json", type=Path)
    parser.add_argument("--organization", default=DEFAULT_ORG)
    args = parser.parse_args()

    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    db = psycopg.connect(args.dsn, autocommit=True)
    repo = PostgresNotebookRepository(db)

    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO notebook.topics (topic_id, name) VALUES ('root', 'کتابخانهٔ حقوق ایران')
               ON CONFLICT (topic_id) DO NOTHING"""
        )

    def seed_topic(topic_id: str, name: str, parent: str | None) -> None:
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO notebook.topics (topic_id, name, parent_topic_id)
                   VALUES (%s, %s, %s) ON CONFLICT (topic_id) DO UPDATE
                       SET name = EXCLUDED.name, parent_topic_id = EXCLUDED.parent_topic_id""",
                (topic_id, name, parent),
            )

    page_count = 0

    def instrument_items(node: dict) -> list[tuple[str, str | None]]:
        return [(node.get("title") or node["label"], node.get("summary"))]

    def walk(node: dict, path: list[str]) -> list[tuple[str, str | None]]:
        """Return (title, summary) leaves under this node; seed pages along the way."""
        nonlocal page_count
        kind = node["kind"]
        children = node.get("children", [])
        if kind == "instrument":
            summary = (node.get("summary") or node.get("title") or node["label"]).strip()[:400]
            if summary:
                repo.upsert_page(
                    NotebookPage(
                        organization_id=args.organization,
                        node_id=node["id"],
                        kind="instrument",
                        path=" / ".join(path + [node["label"]]),
                        summary=summary,
                        summary_version=1,
                        based_on={"instruments": 1},
                    )
                )
                page_count += 1
            return instrument_items(node)
        leaves: list[tuple[str, str | None]] = []
        for child in children:
            leaves.extend(walk(child, path + [node.get("label", "")]))
        if kind in {"branch", "domain"}:
            seed_topic(node["id"], node["label"], None if kind == "branch" else "root")
            summary = _digest(leaves)
            if summary:
                repo.upsert_page(
                    NotebookPage(
                        organization_id=args.organization,
                        node_id=node["id"],
                        kind=kind,
                        path=" / ".join(path + [node["label"]]),
                        summary=summary,
                        summary_version=1,
                        based_on={"instruments": len(leaves)},
                    )
                )
                page_count += 1
        return leaves

    # root page
    walk(tree, [])
    print(f"topics + {page_count} pages seeded for org={args.organization}")
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM notebook.topics")
        topics = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM notebook.pages WHERE organization_id = %s", (args.organization,))
        pages = cur.fetchone()[0]
    print(f"notebook.topics={topics} notebook.pages={pages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
