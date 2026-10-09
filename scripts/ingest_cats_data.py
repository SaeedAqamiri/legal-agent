#!/usr/bin/env python3
"""Ingest the ekhtebar cats corpus (index-cats.json + cats/<category>/*.md).

Deterministic pipeline (phase 1 of the cats enrichment plan):
  1. migrate Postgres (canonical + sources schemas, V0001..V0006)
  2. register one sources.source_document per file (checksum deduped against
     the laws corpus under the same ekhtebar source)
  3. regulatory categories (آیین‌نامه‌ها/مصوبات/بخشنامه‌ها/سیاست‌های کلی/
     طرح و لایحه) go through MarkdownDocumentParser -> provisions
  4. judicial categories become one canonical instrument per ruling:
     «آرا وحدت رویه» yearly collections are split into their numbered rulings,
     «آرای دیوان عدالت اداری» files are split when they carry several rulings;
     the ruling text is stored as a single provision so later phases can hang
     INTERPRETS/ANNULS edges off provision versions
  5. write a JSON report

The «تازه‌های قوانین» tail is site-chrome noise and is stripped before parsing.

Usage:
    LAWS_DATA_DIR=~/laws-mcp/data LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/ingest_cats_data.py --cats "آرا وحدت رویه"
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
    DocumentStatus,
    InstrumentType,
    ProvisionType,
)
from legal_agent_core.errors import DomainError
from legal_agent_core.ingestion.markdown_parser import MarkdownDocumentParser
from legal_agent_core.ingestion.models import (
    DocumentVersionInput,
    InstrumentInput,
    ParsedDocument,
    ProvisionInput,
    SourceInput,
)
from legal_agent_core.ingestion.normalization import normalize_legal_text
from legal_agent_core.ingestion.pipeline import CanonicalIngestionPipeline
from legal_agent_core.jalali import parse_jalali_date

DEFAULT_DATA_DIR = Path("~/laws-mcp/data").expanduser()
DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
SOURCE_ID = "ekhtebar"

#: category folder -> (sources doc_kind, tier, canonical InstrumentType, mode)
CAT_CONFIG = {
    "آرا وحدت رویه": ("judgment", 2, InstrumentType.JUDGMENT, "rulings_yearly"),
    "آرای دیوان عدالت اداری": ("judgment", 2, InstrumentType.JUDGMENT, "rulings_files"),
    "رویه قضایی": ("judgment", None, InstrumentType.JUDGMENT, "single"),
    "نشست‌های قضایی": ("guideline", None, InstrumentType.GUIDELINE, "single"),
    "نظریه‌های مشورتی": ("advisory_opinion", None, InstrumentType.ADVISORY_OPINION, "single"),
    "نظریه‌های رئیس مجلس": ("advisory_opinion", None, InstrumentType.ADVISORY_OPINION, "single"),
    "سیاست‌های کلی": ("policy", 1, InstrumentType.POLICY, "parse"),
    "آیین‌نامه‌ها": ("regulation", 4, InstrumentType.REGULATION, "parse"),
    "مصوبات": ("cabinet_approval", 4, InstrumentType.RESOLUTION, "parse"),
    "بخشنامه‌ها": ("circular", 5, InstrumentType.CIRCULAR, "parse"),
    "طرح و لایحه": ("bill", 3, InstrumentType.OTHER, "parse"),
}

CAT_ISSUER = {
    "آرا وحدت رویه": "هیأت عمومی دیوان عالی کشور",
    "آرای دیوان عدالت اداری": "هیأت عمومی دیوان عدالت اداری",
    "نظریه‌های مشورتی": "اداره کل حقوقی قوه قضاییه",
    "نظریه‌های رئیس مجلس": "رئیس مجلس شورای اسلامی",
    "سیاست‌های کلی": "مقام معظم رهبری",
    "مصوبات": "هیأت وزیران",
    "نشست‌های قضایی": "دیوان عالی کشور",
}

JALALI_SLASH_RE = re.compile(r"([۰-۹0-9]{4})[/\\]([۰-۹0-9]{1,2})[/\\]([۰-۹0-9]{1,2})")
ENACTED_WORDS_RE = re.compile(r"مصوب\s+([۰-۹0-9]{1,2})\s+([^\s\d]+)\s+([۰-۹0-9]{4})")
JALALI_MONTHS = {
    "فروردین": 1, "ارديبهشت": 2, "اردیبهشت": 2, "خرداد": 3, "تیر": 4,
    "مرداد": 5, "شهریور": 6, "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10,
    "بهمن": 11, "اسفند": 12,
}

UNITY_HEADING_RE = re.compile(r"^(#{2,5})\s*(رأی\s*وحدت[^\n]*)$", re.MULTILINE)
COURT_HEADING_RE = re.compile(r"^(#{1,3})\s*(رأی\s*شماره[^\n]*)$", re.MULTILINE)
NOISE_TAIL_RE = re.compile(r"^#{1,6}\s*تازه‌های قوانین.*$", re.MULTILINE)

TITLE_KIND_RULES = (
    ("دستورالعمل", "directive", 5),
    ("تصویب‌نامه", "cabinet_approval", 4),
    ("بخشنامه", "circular", 5),
    ("آیین‌نامه", "regulation", 4),
    ("آيين‌نامه", "regulation", 4),
)


def to_english_digits(text: str) -> str:
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


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


def parse_jalali_in_text(text: str) -> date | None:
    """First plausible yyyy/mm/dd (Jalali) date in the text, normalized digits."""
    match = JALALI_SLASH_RE.search(text)
    if not match:
        words = ENACTED_WORDS_RE.search(text)
        if not words:
            return None
        day, month, year = words.groups()
        month_no = JALALI_MONTHS.get(month.strip())
        if not month_no:
            return None
        day, year = to_english_digits(day), to_english_digits(year)
    else:
        year, month_no, day = (to_english_digits(part) for part in match.groups())
    year_i, month_i, day_i = int(year), int(month_no), int(day)
    if not (1200 <= year_i <= 1500 and 1 <= month_i <= 12 and 1 <= day_i <= 31):
        return None
    return parse_jalali_date(f"{year_i}/{month_i:02d}/{day_i:02d}")


def strip_noise(md_text: str) -> str:
    """Drop the «تازه‌های قوانین» site-chrome tail."""
    match = NOISE_TAIL_RE.search(md_text)
    return md_text[: match.start()] if match else md_text


def clean_heading(title: str) -> str:
    collapsed = re.sub(r"\s+", " ", title).strip(" –—-\u200c ")
    return collapsed


def refine_kind(doc_kind: str, tier: int | None, title: str) -> tuple[str, int | None]:
    if doc_kind not in ("cabinet_approval", "regulation", "circular", "directive", "bill"):
        return doc_kind, tier
    for prefix, kind, kind_tier in TITLE_KIND_RULES:
        if title.strip().startswith(prefix):
            return kind, kind_tier
    return doc_kind, tier


def split_unity_rulings(md_text: str) -> list[tuple[str, str]]:
    """(ruling_title, chunk_text) pairs from a yearly وحدت رویه collection."""
    matches = list(UNITY_HEADING_RE.finditer(md_text))
    if not matches:
        return []
    chunks = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
        chunks.append((clean_heading(match.group(2)), md_text[match.start():end]))
    return chunks


def split_court_rulings(md_text: str) -> list[tuple[str, str]]:
    matches = list(COURT_HEADING_RE.finditer(md_text))
    if len(matches) < 2:
        return []
    chunks = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
        chunks.append((clean_heading(match.group(2)), md_text[match.start():end]))
    # a page-title H1 yields a title-only chunk; keep substantive bodies only
    chunks = [(title, chunk) for title, chunk in chunks if len(chunk.strip()) >= 300]
    return chunks if len(chunks) >= 2 else []


def build_ruling_document(
    *,
    title: str,
    chunk: str,
    checksum: str,
    file_size: int,
    source_uri: str,
    instrument_type: InstrumentType,
    issuer: str | None,
    tier: int | None,
) -> ParsedDocument:
    issued = parse_jalali_in_text(chunk[:600])
    version = DocumentVersionInput(
        status=DocumentStatus.PUBLISHED,
        version_label=None,
        publication_date=issued,
        enacted_at=issued,
        effective_from=issued,
    )
    return ParsedDocument(
        source=SourceInput(
            filename=title,
            media_type="text/markdown",
            checksum=checksum,
            file_size=file_size,
            ingested_at=datetime.now(UTC),
            ingested_by="cats_ingest",
            source_uri=source_uri,
        ),
        instrument=InstrumentInput(
            title=title,
            canonical_title=normalize_legal_text(title),
            instrument_type=instrument_type,
            jurisdiction="IR",
            issuer=issuer,
            authority_level=str(tier) if tier else None,
            external_identifier=None,  # identity by normalized title -> cross-file dedupe
        ),
        version=version,
        provisions=(
            ProvisionInput(
                provision_type=ProvisionType.PARAGRAPH,
                label="متن",
                text=chunk.strip(),
                page_number=1,
                title=title,
            ),
        ),
    )


def record_alias(cur, checksum: str | None, external_id: str, url: str) -> None:
    if not checksum:
        return
    cur.execute(
        """UPDATE sources.source_documents
           SET metadata = metadata || %s, last_seen_at = now()
           WHERE source_id=%s AND original_checksum=%s""",
        (json.dumps({"aliases": {external_id: url}}, ensure_ascii=False), SOURCE_ID, checksum),
    )


def upsert_stage_instrument(
    cur,
    *,
    canonical_key: str,
    title: str,
    staging_type: str,
    tier: int | None,
    issuer: str | None,
    url: str,
) -> str:
    """Register stable legal identity in sources.instruments; return its uuid."""
    cur.execute(
        """INSERT INTO sources.instruments
               (canonical_key, canonical_title, instrument_type, tier, issuer, external_ids)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (canonical_key) DO UPDATE SET
               external_ids = sources.instruments.external_ids || EXCLUDED.external_ids
           RETURNING instrument_uid""",
        (
            canonical_key,
            title,
            staging_type,
            tier,
            issuer,
            json.dumps({"ekhtebar": url}, ensure_ascii=False),
        ),
    )
    return str(cur.fetchone()[0])


def register_source_document(
    cur,
    *,
    external_id: str,
    instrument_uid: str,
    doc_kind: str,
    tier: int | None,
    title: str,
    issue_date: date | None,
    md_path: Path,
    checksum: str,
    url: str,
    metadata: dict,
) -> None:
    cur.execute(
        """INSERT INTO sources.source_documents
               (source_id, external_id, instrument_uid, match_status, doc_kind, tier, title,
                issue_date, original_path, original_checksum, text_path, text_checksum,
                text_extract_method, fetch_state, source_uri, metadata)
           VALUES (%s,%s,%s,'expert',%s,%s,%s,%s,%s,%s,%s,%s,'markdown','text_extracted',%s,%s)
           ON CONFLICT (source_id, external_id) DO UPDATE SET
               instrument_uid = EXCLUDED.instrument_uid,
               last_seen_at = now()""",
        (
            SOURCE_ID, external_id, instrument_uid, doc_kind, tier,
            title, issue_date, str(md_path), checksum, str(md_path), checksum,
            url, json.dumps(metadata, ensure_ascii=False),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", default=Path(os.environ.get("LAWS_DATA_DIR", DEFAULT_DATA_DIR)), type=Path
    )
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--report", default=Path("out/cats_ingest_report.json"), type=Path)
    parser.add_argument(
        "--cats",
        default="",
        help="comma-separated subset of category folders to ingest (default: all)",
    )
    parser.add_argument("--limit", type=int, default=0, help="max files per category (0 = all)")
    args = parser.parse_args()

    wanted = {c.strip() for c in args.cats.split(",") if c.strip()} if args.cats else None
    unknown = wanted - set(CAT_CONFIG) if wanted else set()
    if unknown:
        parser.error(f"unknown categories: {sorted(unknown)}; known: {sorted(CAT_CONFIG)}")

    index_path = args.data_dir / "index-cats.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for cat, cat_entries in index.items():
        if wanted and cat not in wanted:
            continue
        entries.extend(dict(item, cat=cat) for item in cat_entries)
    print(f"index entries: {len(entries)} across {len({e['cat'] for e in entries})} categories")

    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    pipeline = CanonicalIngestionPipeline(repo)
    db = psycopg.connect(args.dsn, autocommit=True)

    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO sources.sources (source_id, name, kind, base_url, connector_version)
               VALUES (%s, %s, 'portal', 'https://www.ekhtebar.ir', '1')
               ON CONFLICT (source_id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()""",
            (SOURCE_ID, "قوانین و آرای hayula (اختبار ekhtebar.ir)"),
        )

    stats: dict[str, dict[str, int]] = {}
    failures: list[dict] = []
    slug_counts: dict[str, int] = {}
    per_cat_processed: dict[str, int] = {}
    registered = duplicates = parse_fail = 0

    def bump(cat: str, key: str) -> None:
        stats.setdefault(cat, {}).setdefault(key, 0)
        stats[cat][key] += 1

    for entry in entries:
        cat = str(entry.get("cat", "")).strip()
        doc_kind, tier, instrument_type, mode = CAT_CONFIG.get(cat, ("unknown", None, InstrumentType.OTHER, "parse"))
        title = str(entry.get("title", "")).strip() or "بدون عنوان"
        url = str(entry.get("url", "")).strip()
        slug = slug_of(url) or f"entry-{len(slug_counts) + 1}"
        slug_counts[slug] = slug_counts.get(slug, 0) + 1
        external_id = slug if slug_counts[slug] == 1 else f"{slug}#{slug_counts[slug]}"

        md_path = args.data_dir / "cats" / cat / f"{slug}.md"
        if not md_path.exists():
            bump(cat, "md_missing")
            continue
        per_cat_processed[cat] = per_cat_processed.get(cat, 0) + 1
        if args.limit and per_cat_processed[cat] > args.limit:
            continue

        raw = strip_noise(md_path.read_text(encoding="utf-8", errors="replace"))
        checksum = sha256_file(md_path)
        issue_date = parse_jalali_in_text(raw[:2000])
        doc_kind_r, tier_r = refine_kind(doc_kind, tier, title)

        try:
            with db.cursor() as cur:
                cur.execute(
                    """SELECT document_uid FROM sources.source_documents
                       WHERE source_id=%s AND original_checksum=%s""",
                    (SOURCE_ID, checksum),
                )
                existing = cur.fetchone()
            if existing:  # identical content already registered (laws corpus or prior run)
                with db.cursor() as cur:
                    record_alias(cur, checksum, external_id, url)
                duplicates += 1
                bump(cat, "duplicate")
                continue

            if mode == "parse":
                try:
                    parsed = MarkdownDocumentParser.from_text(raw, md_path.name)
                except DomainError as exc:
                    if "no provisions" not in str(exc):
                        raise
                    # news/analysis pieces (e.g. bill status reports) carry no
                    # ماده structure: keep them as single-provision documents
                    bump(cat, "no_structure")
                    parsed = build_ruling_document(
                        title=title,
                        chunk=raw,
                        checksum=checksum,
                        file_size=md_path.stat().st_size,
                        source_uri=url,
                        instrument_type=instrument_type,
                        issuer=CAT_ISSUER.get(cat),
                        tier=tier,
                    )
                enacted = issue_date
                _, tier_c = refine_kind(doc_kind, tier, parsed.instrument.title)
                instrument = replace(
                    parsed.instrument,
                    instrument_type=instrument_type,
                    authority_level=str(tier_c) if tier_c else None,
                    issuer=CAT_ISSUER.get(cat),
                    external_identifier=slug,
                )
                version = parsed.version
                if enacted:
                    version = replace(version, enacted_at=enacted, effective_from=enacted)
                parsed = replace(parsed, instrument=instrument, version=version)
            else:
                if mode == "rulings_yearly":
                    parts = split_unity_rulings(raw)
                elif mode == "rulings_files":
                    parts = split_court_rulings(raw)
                else:
                    parts = []
                if not parts:
                    parts = [(title, raw)]
                ruling_uids: list[str] = []
                with db.cursor() as cur:
                    for ruling_no, (ruling_title, chunk) in enumerate(parts, start=1):
                        ruling_doc = build_ruling_document(
                            title=ruling_title,
                            chunk=chunk,
                            checksum=checksum,
                            file_size=md_path.stat().st_size,
                            source_uri=url,
                            instrument_type=instrument_type,
                            issuer=CAT_ISSUER.get(cat),
                            tier=tier,
                        )
                        result = pipeline.ingest(ruling_doc)
                        ruling_uids.append(
                            upsert_stage_instrument(
                                cur,
                                canonical_key=normalize_legal_text(ruling_title),
                                title=ruling_title,
                                staging_type=doc_kind,
                                tier=tier,
                                issuer=CAT_ISSUER.get(cat),
                                url=url,
                            )
                        )
                        bump(cat, "rulings")
                with db.cursor() as cur:
                    # one staging row per FILE; split rulings share it via metadata
                    register_source_document(
                        cur,
                        external_id=external_id,
                        instrument_uid=ruling_uids[0],
                        doc_kind=doc_kind,
                        tier=tier,
                        title=title,
                        issue_date=issue_date,
                        md_path=md_path,
                        checksum=checksum,
                        url=url,
                        metadata={"cat": cat, "rulings": len(parts), "ruling_uids": ruling_uids},
                    )
                registered += 1
                bump(cat, "registered")
                if registered % 25 == 0:
                    repo.connection.commit()
                continue

            result = pipeline.ingest(parsed)
            with db.cursor() as cur:
                stage_uid = upsert_stage_instrument(
                    cur,
                    canonical_key=normalize_legal_text(title),
                    title=title,
                    staging_type=doc_kind_r,
                    tier=tier_r,
                    issuer=CAT_ISSUER.get(cat),
                    url=url,
                )
                register_source_document(
                    cur,
                    external_id=external_id,
                    instrument_uid=stage_uid,
                    doc_kind=doc_kind_r,
                    tier=tier_r,
                    title=title,
                    issue_date=issue_date,
                    md_path=md_path,
                    checksum=checksum,
                    url=url,
                    metadata={"cat": cat, "canonical_instrument_id": result.instrument_id},
                )
            registered += 1
            bump(cat, "registered")
            bump(cat, "parsed")
            if registered % 25 == 0:
                repo.connection.commit()
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the corpus
            repo.connection.rollback()
            parse_fail += 1
            bump(cat, "failed")
            failures.append({"file": f"{cat}/{slug}.md", "error": str(exc)[:200]})
            print(f"  !! failed {cat}/{slug}: {exc}", flush=True)

    repo.connection.commit()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE sources.source_documents SET match_status='auto' WHERE source_id=%s AND instrument_uid IS NOT NULL",
            (SOURCE_ID,),
        )
    db.commit()

    report = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_dir": str(args.data_dir),
        "index_entries": len(entries),
        "files_processed": sum(per_cat_processed.values()),
        "source_documents_registered": registered,
        "duplicates": duplicates,
        "failures": failures,
        "per_category": stats,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "failures"}, ensure_ascii=False, indent=1))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
