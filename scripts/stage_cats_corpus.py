#!/usr/bin/env python3
"""Stage the cats judicial corpus (khanas: vahdat-e ravieh, divan, mashverati...)
into sources.source_documents with metadata {'cat': <category>} so
llm_extract_cats_relations.py can sweep them.

Reads data_dir/index-cats.json ({category: [{title, url, ...}]}) and the
markdown files under data_dir/cats/<category>/. Idempotent: dedupes on
(source_id='cats', external_id) where external_id is the md file stem.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/stage_cats_corpus.py \
        [--cats "آرا وحدت رویه,نظریه‌های مشورتی"] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
from urllib.parse import unquote
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg


DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
DEFAULT_DATA_DIR = "/home/arman/laws-mcp/data"
DOC_KIND = {
    "آرا وحدت رویه": "judgment",
    "آرای دیوان عدالت اداری": "judgment",
    "نظریه‌های مشورتی": "unknown",
    "نشست‌های قضایی": "judgment",
    "رویه قضایی": "judgment",
    "سیاست‌های کلی": "special_statute",
    "مصوبات": "cabinet_approval",
    "آیین‌نامه‌ها": "regulation",
    "بخشنامه‌ها": "circular",
    "طرح و لایحه": "bill",
    "نظریه‌های رئیس مجلس": "unknown",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument(
        "--data-dir", type=Path,
        default=Path(os.environ.get("LAWS_DATA_DIR", DEFAULT_DATA_DIR)),
    )
    parser.add_argument("--cats", default="", help="comma-separated category filter")
    parser.add_argument("--limit", type=int, default=None, help="max docs per category")
    args = parser.parse_args()

    index = json.loads((args.data_dir / "index-cats.json").read_text(encoding="utf-8"))
    wanted = {c.strip() for c in args.cats.split(",") if c.strip()} or set(index)

    conn = psycopg.connect(args.dsn, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sources.sources (source_id, name, kind, base_url)
               VALUES ('cats', 'قرارگیر کورپوس قضایی اختبار', 'portal',
                       'https://www.ekhtebar.ir')
               ON CONFLICT (source_id) DO NOTHING"""
        )
        cur.execute("SELECT count(*) FROM sources.source_documents WHERE source_id='cats'")
        existing = cur.fetchone()[0]
        print(f"cats docs already staged: {existing}")

        staged = skipped_missing = 0
        for category, entries in index.items():
            if category not in wanted:
                continue
            cat_dir = args.data_dir / "cats" / category
            if not cat_dir.is_dir():
                print(f"  no dir for category: {category}")
                continue
            files = {p.stem: p for p in cat_dir.glob("*.md")}
            count = 0
            for entry in entries:
                if args.limit and count >= args.limit:
                    break
                slug = unquote((entry.get("url") or "")).rstrip("/").split("/")[-1][:150]
                if not slug:
                    continue
                path = files.get(slug)
                if path is None:
                    skipped_missing += 1
                    continue
                title = (entry.get("title") or path.stem).strip()
                external_id = path.stem
                with conn.cursor() as check:
                    check.execute(
                        """SELECT 1 FROM sources.source_documents
                            WHERE source_id='cats' AND external_id=%s""",
                        (external_id,),
                    )
                    if check.fetchone():
                        continue
                text = path.read_text(encoding="utf-8", errors="replace")
                checksum = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
                cur.execute(
                    """INSERT INTO sources.source_documents
                           (source_id, external_id, doc_kind, title,
                            metadata, text_path, text_checksum, text_extract_method,
                            source_uri, fetch_state, created_by)
                       VALUES ('cats', %s, %s, %s,
                               jsonb_build_object('cat', %s::text, 'index_title', %s::text),
                               %s, %s, 'markdown',
                               %s, 'text_extracted', 'stage_cats_corpus')
                       ON CONFLICT (source_id, external_id) DO NOTHING""",
                    (
                        external_id,
                        DOC_KIND.get(category, "unknown"),
                        title[:400],
                        category,
                        entry.get("title", "")[:200],
                        str(path),
                        checksum,
                        entry.get("url", ""),
                    ),
                )
                staged += 1
                count += 1
            print(f"  {category}: staged {count}")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sources.source_documents WHERE source_id='cats'")
        print(f"cats docs staged total: {cur.fetchone()[0]} (skipped-no-file: {skipped_missing})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
