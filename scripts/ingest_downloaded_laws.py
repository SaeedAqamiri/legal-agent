#!/usr/bin/env python3
"""Ingest downloaded law texts (out/downloads/*.md) into the canonical store.

Uses the same MarkdownDocumentParser + CanonicalIngestionPipeline as the
composition seeding path, so parsing rules and content-addressed idempotency
match the rest of the corpus. Run after fetch scripts drop new files into
out/downloads/.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/ingest_downloaded_laws.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.db import apply_migrations
from legal_agent_core.ingestion.markdown_parser import MarkdownDocumentParser
from legal_agent_core.ingestion.pipeline import CanonicalIngestionPipeline

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
DOWNLOADS = Path(__file__).resolve().parents[1] / "out" / "downloads"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument("--dir", type=Path, default=DOWNLOADS)
    parser.add_argument(
        "--paths-file", type=Path, default=None,
        help="optional file with one markdown path per line (corpus backfill)",
    )
    args = parser.parse_args()

    connection = psycopg.connect(args.dsn)
    apply_migrations(connection)
    repository = PostgresCanonicalRepository(connection)
    pipeline = CanonicalIngestionPipeline(repository)
    docs = sorted(args.dir.glob("*.md"))
    if args.paths_file:
        docs.extend(
            Path(line.strip())
            for line in args.paths_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if not docs:
        print(f"no markdown files in {args.dir}")
        return 0
    for path in docs:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            result = pipeline.ingest(MarkdownDocumentParser().from_text(text, path.name))
        except Exception as exc:  # noqa: BLE001 — report per-file failures
            print(f"FAIL {path.name}: {type(exc).__name__}: {str(exc)[:140]}")
            continue
        print(
            f"OK   {path.name}: instrument={result.instrument_id} "
            f"provisions={len(result.provision_ids)} refs={len(result.explicit_references)}"
        )
    connection.commit()
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
