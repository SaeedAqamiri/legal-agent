#!/usr/bin/env python3
"""Ingest the markdown sample corpus (``D0X.md``) into the canonical store.

OCR-free: each markdown file (pdftotext -layout) is parsed by
``MarkdownDocumentParser`` into a ``ParsedDocument`` and pushed through the
deterministic ``CanonicalIngestionPipeline``. Idempotent — re-running yields the
same identities and never overwrites canonical data.

Usage:
    python scripts/ingest_markdown_samples.py [corpus_dir] [dsn]
Where no DSN is given it reads LEGAL_AGENT_POSTGRES_DSN (Postgres, recommended);
otherwise it ingests into an in-memory repository and prints totals.
"""

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

KNOWN_DOCS = {
    "D01.md": "دستورالعمل اجرایی هیئت‌های تشخیص مطالبات",
    "D02.md": "دستورالعمل جامع سوءپیشینه / سوءپیشینه صدور بیمه",
    "D03.md": "شرایط و نحوه برخورداری مشمولین قانون کار از مقرری بیمه بیکاری",
    "D04.md": "شرایط و نحوه برخورداری بازماندگان بیمه‌شدگان از خدمات و تعهدات قانونی",
    "D05.md": "بیمه اجتماعی رانندگان حمل‌ونقل بار و مسافر بین‌شهری و درون‌شهری",
    "D06.md": "بخشنامه تنقیح و تلخیص ضوابط بیمه‌ای مقاطعه‌کاران",
    "D07.md": "بخشنامه تنقیح و تلخیص اجراییات",
    "D08.md": "بخشنامه تجمیع و تلخیص مستمر از کارافتادگی",
    "D09.md": "مجموعه قوانین و مقررات بازنشستگی کارکنان سازمان",
}


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
    corpus = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/tamin_longdocs_agentic_benchmark_v4/documents_md")
    dsn = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("LEGAL_AGENT_POSTGRES_DSN")

    md_dir = Path(corpus)
    if not md_dir.is_dir():
        print(f"corpus directory not found: {corpus}")
        return 2

    repository, connection = _resolve_repository(dsn)
    parser = MarkdownDocumentParser()
    pipeline = CanonicalIngestionPipeline(repository)
    totals = {"documents": 0, "provisions": 0, "spans": 0}

    for path in sorted(md_dir.glob("*.md")):
        if path.name not in KNOWN_DOCS:
            continue
        content = path.read_text(encoding="utf-8")
        try:
            parsed = parser.from_text(content, path.name)
        except DomainError as exc:
            print(f"[skip] {path.name}: {exc}")
            continue

        # Prefer the curated title from the manifest when the heading is noisy.
        if KNOWN_DOCS.get(path.name):
            parsed = _retitled(parsed, KNOWN_DOCS[path.name])

        # Idempotent: skip when the source identity is already present. The
        # remaining ingest is then a no-op (deterministic content-hash IDs).
        source_document_id = parsed.source.source_document_id or stable_id(
            "src", parsed.source.checksum
        )
        try:
            repository.get_source_document(source_document_id)
        except NotFoundError:
            pass
        else:
            print(f"[skip] {path.name}: already ingested")
            continue

        result = pipeline.ingest(parsed)
        totals["documents"] += 1
        totals["provisions"] += len(result.provision_ids)
        totals["spans"] += len(result.source_span_ids)
        print(
            f"[ok] {path.name}: src={result.source_document_id} "
            f"inst={result.instrument_id} provisions={len(result.provision_ids)} "
            f"spans={len(result.source_span_ids)}"
        )

    if connection is not None:
        connection.commit()
        connection.close()

    print(f"\nIngested {totals['documents']} documents, "
          f"{totals['provisions']} provisions, {totals['spans']} source spans "
          f"(store={'postgres' if dsn else 'in-memory'}).")
    return 0


def _retitled(parsed, title):
    from dataclasses import replace

    return replace(
        parsed,
        instrument=replace(parsed.instrument, title=title, canonical_title=title),
    )


if __name__ == "__main__":
    raise SystemExit(main())
