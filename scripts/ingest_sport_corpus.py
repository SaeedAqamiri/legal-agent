#!/usr/bin/env python3
"""Ingest the sport-ministry corpus (``data/sport_ministry_corpus``) into canonical storage.

Like ``ingest_markdown_samples.py`` but driven by the corpus ``manifest.json``
and tolerant of prose documents (court rulings) that yield no provisions.

Usage:
    python scripts/ingest_sport_corpus.py [corpus_dir] [dsn]
Where no DSN is given it reads LEGAL_AGENT_POSTGRES_DSN; otherwise it ingests
into an in-memory repository and prints totals.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from legal_agent_core.errors import DomainError, NotFoundError
from legal_agent_core.ingestion import CanonicalIngestionPipeline
from legal_agent_core.ingestion.markdown_parser import (
    MarkdownDocumentParser,
)
from legal_agent_core.ingestion.pipeline import stable_id


def _resolve_repository(dsn: str | None):
    if dsn:
        import psycopg

        from legal_agent_core.adapters import PostgresCanonicalRepository
        from legal_agent_core.db import apply_migrations

        connection = psycopg.connect(dsn)
        apply_migrations(connection)
        return PostgresCanonicalRepository(connection), connection
    from legal_agent_core.in_memory import InMemoryCanonicalRepository

    return InMemoryCanonicalRepository(), None


def main() -> int:
    corpus = Path(sys.argv[1] if len(sys.argv) > 1 else "data/sport_ministry_corpus")
    dsn = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("LEGAL_AGENT_POSTGRES_DSN")
    manifest_path = corpus / "manifest.json"
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}")
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repository, connection = _resolve_repository(dsn)
    parser = MarkdownDocumentParser()
    pipeline = CanonicalIngestionPipeline(repository)
    totals = {"documents": 0, "provisions": 0, "spans": 0, "skipped": 0}

    for doc_id, meta in sorted(manifest["documents"].items()):
        if not meta.get("file"):
            print(f"[skip] {doc_id}: no file ({meta.get('quality', 'metadata-only')})")
            totals["skipped"] += 1
            continue
        path = corpus / "documents_md" / meta["file"]
        content = path.read_text(encoding="utf-8")
        try:
            parsed = parser.from_text(content, meta["file"])
        except DomainError as exc:
            print(f"[skip] {doc_id}: {exc}")
            totals["skipped"] += 1
            continue

        source_document_id = parsed.source.source_document_id or stable_id(
            "src", parsed.source.checksum
        )
        try:
            repository.get_source_document(source_document_id)
        except NotFoundError:
            pass
        else:
            print(f"[skip] {doc_id}: already ingested")
            continue

        result = pipeline.ingest(parsed)
        totals["documents"] += 1
        totals["provisions"] += len(result.provision_ids)
        totals["spans"] += len(result.source_span_ids)
        print(
            f"[ok] {doc_id} {meta['title'][:48]}: provisions={len(result.provision_ids)} "
            f"spans={len(result.source_span_ids)}"
        )

    if connection is not None:
        connection.commit()
        connection.close()

    print(
        f"\nIngested {totals['documents']} documents, {totals['provisions']} provisions, "
        f"{totals['spans']} spans; skipped {totals['skipped']} "
        f"(store={'postgres' if dsn else 'in-memory'})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
