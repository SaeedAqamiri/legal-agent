#!/usr/bin/env python3
"""Ingest the hayula laws corpus (index.json + laws/*.md) into legal-agent.

Deterministic pipeline (S1-S3 + relations):
  1. migrate Postgres (canonical + sources schemas, V0001..V0005)
  2. register the ekhtebar source + one instrument/source_document per index entry
  3. parse laws/*.md with MarkdownDocumentParser (provisions, intra-doc references)
     into the canonical repository (Postgres), injecting enacted dates found in
     the "### مصوب ..." heading
  4. link «مستندات مرتبط» entries that actually resolve to other corpus laws
     (CITES edges); generic stubs are counted and skipped
  5. write a JSON report

Usage:
    LAWS_DATA_DIR=~/hayula/laws-mcp/data LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/ingest_laws_data.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.canonical import (
    CanonicalEdge,
    CanonicalEdgeType,
    CreationMethod,
    InstrumentType,
    Provenance,
)
from legal_agent_core.ingestion.markdown_parser import MarkdownDocumentParser
from legal_agent_core.ingestion.models import ParsedDocument
from legal_agent_core.ingestion.normalization import normalize_legal_text
from legal_agent_core.ingestion.pipeline import CanonicalIngestionPipeline
from legal_agent_core.jalali import parse_jalali_date

DEFAULT_DATA_DIR = Path("~/hayula/laws-mcp/data").expanduser()
DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:55432/legal_agent"
SOURCE_ID = "ekhtebar"
ORG = "hayula"

JALALI_MONTHS = {
    "فروردین": 1,
    "ارديبهشت": 2,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "آبان": 8,
    "آذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
}
ENACTED_WORDS_RE = re.compile(r"مصوب\s+([۰-۹0-9]{1,2})\s+([^\s\d]+)\s+([۰-۹0-9]{4})")
ENACTED_SLASH_RE = re.compile(r"مصوب\s+([۰-۹0-9]{4})[/\\]([۰-۹0-9]{1,2})[/\\]([۰-۹0-9]{1,2})")
GENERIC_RELATED = {
    "آیین‌نامه",
    "آیین نامه",
    "قوانین مرتبط",
    "رای وحدت رویه",
    "رأی وحدت رویه",
}
RELATED_LINK_RE = re.compile(r"[-*]\s*\[([^\]]+)\]\(([^)]+)\)")


def classify(title: str) -> tuple[str, int | None]:
    t = title.strip()
    if t.startswith(("قانون اساسی", "قانون اساسى")):
        return "constitution", 1
    if t.startswith(("آیین‌نامه", "آيین‌نامه", "آیین نامه", "آيين‌نامه")):
        return "regulation", 4
    if t.startswith(("تصویب‌نامه", "تصويب‌نامه")):
        return "cabinet_approval", 4
    if t.startswith("بخشنامه"):
        return "circular", 5
    if t.startswith("دستورالعمل"):
        return "directive", 5
    if t.startswith(("فهرست", "مجموعه")):
        return "unknown", None
    if t.startswith(("قانون", "لایحه", "لايحه")):
        return "statute", 3
    return "unknown", None


def slug_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return urllib.parse.unquote(tail)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def to_english_digits(text: str) -> str:
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    return text.translate(table)


def parse_enacted(text: str) -> date | None:
    slash = ENACTED_SLASH_RE.search(text)
    if slash:
        year, month, day = (int(to_english_digits(part)) for part in slash.groups())
        if 1200 <= year <= 1500 and 1 <= month <= 12 and 1 <= day <= 31:
            return parse_jalali_date(f"{year}/{month:02d}/{day:02d}")
        return None
    match = ENACTED_WORDS_RE.search(text)
    if not match:
        return None
    day = int(to_english_digits(match.group(1)))
    month = JALALI_MONTHS.get(match.group(2).strip())
    year = int(to_english_digits(match.group(3)))
    if not month:
        return None
    return parse_jalali_date(f"{year}/{month:02d}/{day:02d}")


def related_links(md_text: str) -> list[tuple[str, str]]:
    """Real (title, url) pairs under «مستندات مرتبط»; generic stubs excluded."""
    section = md_text.split("مستندات مرتبط", 1)
    if len(section) != 2:
        return []
    links = RELATED_LINK_RE.findall(section[1])
    return [
        (title.strip(), url.strip())
        for title, url in links
        if title.strip() not in GENERIC_RELATED
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=Path(os.environ.get("LAWS_DATA_DIR", DEFAULT_DATA_DIR)),
        type=Path,
    )
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument(
        "--report", default=Path("out/laws_ingest_report.json"), type=Path
    )
    args = parser.parse_args()

    index_path = args.data_dir / "index.json"
    entries = json.loads(index_path.read_text(encoding="utf-8"))
    print(f"index entries: {len(entries)}")

    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    pipeline = CanonicalIngestionPipeline(repo)
    db = psycopg.connect(args.dsn, autocommit=True)

    slug_counts: dict[str, int] = {}
    registered, no_file, parse_fail = 0, 0, 0
    failures: list[dict] = []
    instrument_uid_by_key: dict[str, str] = {}
    canonical_id_by_slug: dict[str, str] = {}
    stats = {"with_md": 0, "with_pdf": 0, "provisions": 0, "intra_refs": 0}

    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO sources.sources (source_id, name, kind, base_url, connector_version)
               VALUES (%s, %s, 'portal', 'https://www.ekhtebar.ir', '1')
               ON CONFLICT (source_id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()""",
            (SOURCE_ID, "قوانین hayula (اختبار ekhtebar.ir)"),
        )

    for entry in entries:
        title = str(entry.get("title", "")).strip() or "بدون عنوان"
        url = str(entry.get("url", "")).strip()
        slug = slug_of(url)
        if not slug:
            slug = f"entry-{registered + 1}"
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
        external_id = slug if slug_counts[slug] == 1 else f"{slug}#{slug_counts[slug]}"
        doc_kind, tier = classify(title)
        md_path = args.data_dir / "laws" / f"{slug}.md"
        pdf_path = args.data_dir / "pdf" / f"{slug}.pdf"
        canonical_key = normalize_legal_text(title)

        enacted = (
            parse_enacted(md_path.read_text(encoding="utf-8", errors="replace"))
            if md_path.exists()
            else None
        )
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO sources.instruments (canonical_key, canonical_title, instrument_type, tier, issuer, external_ids)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (canonical_key) DO UPDATE SET external_ids = sources.instruments.external_ids || EXCLUDED.external_ids
                   RETURNING instrument_uid""",
                (
                    canonical_key,
                    title,
                    doc_kind,
                    tier,
                    None,
                    json.dumps({"ekhtebar": url}, ensure_ascii=False),
                ),
            )
            instrument_uid = str(cur.fetchone()[0])
            instrument_uid_by_key[canonical_key] = instrument_uid
            md_checksum = sha256_file(md_path) if md_path.exists() else None
            pdf_checksum = sha256_file(pdf_path) if pdf_path.exists() else None
            duplicate = None
            for checksum in (md_checksum, pdf_checksum):
                if not checksum:
                    continue
                cur.execute(
                    """SELECT external_id FROM sources.source_documents
                       WHERE source_id=%s AND original_checksum=%s""",
                    (SOURCE_ID, checksum),
                )
                duplicate = cur.fetchone()
                if duplicate:
                    break
            if duplicate:
                # identical content under another slug: keep one row, record the alias
                target_checksum = md_checksum or pdf_checksum
                cur.execute(
                    """UPDATE sources.source_documents
                       SET metadata = metadata || %s, last_seen_at = now()
                       WHERE source_id=%s AND original_checksum=%s""",
                    (
                        json.dumps({"aliases": {external_id: url}}, ensure_ascii=False),
                        SOURCE_ID,
                        target_checksum,
                    ),
                )
                registered += 1
                continue
            cur.execute(
                """INSERT INTO sources.source_documents
                       (source_id, external_id, instrument_uid, match_status, doc_kind, tier, title,
                        issue_date, original_path, original_checksum, text_path, text_checksum,
                        text_extract_method, fetch_state, source_uri, metadata)
                   VALUES (%s,%s,%s,'expert',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source_id, external_id) DO UPDATE SET
                       instrument_uid = EXCLUDED.instrument_uid,
                       original_path = COALESCE(EXCLUDED.original_path, sources.source_documents.original_path),
                       text_path = COALESCE(EXCLUDED.text_path, sources.source_documents.text_path),
                       last_seen_at = now()""",
                (
                    SOURCE_ID,
                    external_id,
                    instrument_uid,
                    doc_kind,
                    tier,
                    title,
                    enacted,
                    str(pdf_path) if pdf_path.exists() else None,
                    pdf_checksum,
                    str(md_path) if md_path.exists() else None,
                    md_checksum,
                    "markdown" if md_path.exists() else None,
                    "text_extracted" if md_path.exists() else "fetched",
                    url,
                    json.dumps(
                        {
                            "year": entry.get("year") or None,
                            "pdf_url": entry.get("pdf"),
                            "pdf_text_path": str(
                                args.data_dir / "pdf-text" / f"{slug}.txt"
                            )
                            if (args.data_dir / "pdf-text" / f"{slug}.txt").exists()
                            else None,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        registered += 1
        no_file += 0 if md_path.exists() else 1

    print(f"registered source_documents: {registered} (without md: {no_file})")

    for md_path in sorted((args.data_dir / "laws").glob("*.md")):
        slug = md_path.stem
        try:
            raw = md_path.read_text(encoding="utf-8", errors="replace")
            parsed: ParsedDocument = MarkdownDocumentParser.from_text(raw, md_path.name)
            enacted = parse_enacted(raw)
            kind, tier = classify(parsed.instrument.title)
            instrument = replace(
                parsed.instrument,
                instrument_type=InstrumentType.REGULATION
                if kind == "unknown"
                else InstrumentType(kind),
                authority_level=str(tier) if tier else None,
                external_identifier=slug,
            )
            version = parsed.version
            if enacted:
                version = replace(version, enacted_at=enacted, effective_from=enacted)
            parsed = replace(parsed, instrument=instrument, version=version)
            result = pipeline.ingest(parsed)
            canonical_id_by_slug[slug] = result.instrument_id
            stats["with_md"] += 1
            stats["provisions"] += len(result.provision_ids)
            stats["intra_refs"] += len(result.explicit_references)
            if stats["with_md"] % 25 == 0:
                repo.connection.commit()  # the adapter leaves transaction ownership to the caller
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the corpus
            repo.connection.rollback()
            parse_fail += 1
            failures.append({"file": md_path.name, "error": str(exc)[:200]})
            print(f"  !! parse failed {md_path.name}: {exc}", flush=True)
    repo.connection.commit()
    print(
        f"parsed md files: {stats['with_md']} (provisions: {stats['provisions']}, intra-doc refs: {stats['intra_refs']}, failures: {parse_fail})"
    )

    edges_added, edges_unresolved, stub_sections = 0, 0, 0
    with db.cursor() as cur:
        cur.execute(
            "UPDATE sources.source_documents SET match_status='auto' WHERE source_id=%s AND instrument_uid IS NOT NULL",
            (SOURCE_ID,),
        )
    for md_path in sorted((args.data_dir / "laws").glob("*.md")):
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        links = related_links(raw)
        if not links:
            if "مستندات مرتبط" in raw:
                stub_sections += 1
            continue
        source_instrument = canonical_id_by_slug.get(md_path.stem)
        if not source_instrument:
            continue
        for link_title, link_url in links:
            target_slug = slug_of(link_url)
            target = canonical_id_by_slug.get(target_slug)
            if not target or target == source_instrument:
                edges_unresolved += 1
                continue
            edge = CanonicalEdge(
                edge_id=f"edge:rel:{hashlib.sha1((source_instrument + target + link_url).encode()).hexdigest()[:24]}",
                source_node_id=source_instrument,
                target_node_id=target,
                edge_type=CanonicalEdgeType.CITES,
                provenance=Provenance(
                    created_by="laws_ingest",
                    creation_method=CreationMethod.DETERMINISTIC_EXTRACTOR,
                    source_id=md_path.name,
                ),
                confidence=0.6,
            )
            try:
                repo.add_edge(edge)
                edges_added += 1
            except Exception as exc:  # noqa: BLE001 — duplicate/missing-node edges are counted elsewhere
                print(f"  edge skipped ({type(exc).__name__})")
                continue
    print(
        f"related-doc edges: {edges_added} added, {edges_unresolved} unresolved, {stub_sections} stub-only sections"
    )

    # deterministic legal_status: the corpus marks repealed laws with «[منسوخ]» in the title
    with db.cursor() as cur:
        cur.execute(
            "UPDATE sources.source_documents SET legal_status='repealed' WHERE source_id=%s AND title LIKE '%%[منسوخ]%%'",
            (SOURCE_ID,),
        )
        cur.execute(
            """UPDATE canonical.document_versions SET status='repealed'
               WHERE instrument_id IN (SELECT instrument_id FROM canonical.legal_instruments WHERE title LIKE '%[منسوخ]%')"""
        )
    db.commit()
    repo.connection.commit()

    report = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_dir": str(args.data_dir),
        "index_entries": len(entries),
        "source_documents_registered": registered,
        "instruments": len(instrument_uid_by_key),
        "parsed_md": stats["with_md"],
        "provisions": stats["provisions"],
        "intra_document_references": stats["intra_refs"],
        "related_edges_added": edges_added,
        "related_edges_unresolved": edges_unresolved,
        "stub_related_sections": stub_sections,
        "parse_failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
