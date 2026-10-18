#!/usr/bin/env python3
"""Merge/refresh notebook page summaries and detect staleness.

Docs/qavanin/06_law_notebook.md §9. Two independent concerns:

  --check    recompute a checksum of each page's underlying canonical summaries
             and mark pages whose sources changed as ``needs_revalidation``
             (freshness badge). No rewrite.
  --rebuild  recompute the digest deterministically from canonical summaries
             (idempotent) and clear the revalidation flag.
  --llm      (optional) let the configured LLM rewrite the digest into prose;
             skipped silently when no API key is set.

    .venv/bin/python scripts/merge_notebook_pages.py --check
    .venv/bin/python scripts/merge_notebook_pages.py --rebuild
"""

from __future__ import annotations

import argparse
import hashlib
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


def _leaves(node: dict) -> list[tuple[str, str | None]]:
    if node["kind"] == "instrument":
        return [(node.get("title") or node["label"], node.get("summary"))]
    out: list[tuple[str, str | None]] = []
    for child in node.get("children", []):
        out.extend(_leaves(child))
    return out


def _digest(items: list[tuple[str, str | None]]) -> str:
    lines = []
    for title, summary in items[:_MAX_CHILD_SUMMARIES]:
        text = (summary or title).strip().replace("\n", " ")
        lines.append(f"- {title[:60]}: {text[:_SUMMARY_CHARS]}")
    return "\n".join(lines)


def _checksum(items: list[tuple[str, str | None]]) -> str:
    raw = "|".join(f"{t}:{s or ''}" for t, s in sorted(items))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--tree", default="out/display_tree.json", type=Path)
    parser.add_argument("--organization", default=DEFAULT_ORG)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    if not (args.check or args.rebuild):
        args.check = True

    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    db = psycopg.connect(args.dsn, autocommit=True)
    repo = PostgresNotebookRepository(db)

    pages: dict[str, dict] = {}

    def walk(node: dict) -> None:
        if node["kind"] in {"branch", "domain"}:
            pages[node["id"]] = {
                "kind": node["kind"],
                "items": _leaves(node),
            }
        for child in node.get("children", []):
            walk(child)

    walk(tree)

    stale = rebuilt = 0
    for node_id, meta in pages.items():
        items = meta["items"]
        if not items:
            continue
        checksum = _checksum(items)
        page = repo.get_page(args.organization, node_id)
        stored = (page.based_on or {}).get("checksum") if page else None
        if args.rebuild:
            repo.upsert_page(
                NotebookPage(
                    organization_id=args.organization,
                    node_id=node_id,
                    kind=meta["kind"],
                    path=page.path if page else None,
                    summary=_digest(items),
                    summary_version=(page.summary_version + 1) if page else 1,
                    based_on={"instruments": len(items), "checksum": checksum},
                    needs_revalidation=False,
                )
            )
            rebuilt += 1
        elif stored != checksum:
            if page is not None and not page.needs_revalidation:
                repo.upsert_page(
                    NotebookPage(
                        organization_id=args.organization,
                        node_id=node_id,
                        kind=page.kind,
                        path=page.path,
                        summary=page.summary,
                        summary_version=page.summary_version,
                        based_on={**(page.based_on or {}), "checksum": stored},
                        needs_revalidation=True,
                    )
                )
            stale += 1

    if args.rebuild:
        print(f"rebuilt {rebuilt} pages")
    else:
        print(f"checked {len(pages)} pages; {stale} need revalidation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
