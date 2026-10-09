#!/usr/bin/env python3
"""Find, fetch and ingest laws that the corpus is missing.

Phase 3, step 3. Input: out/cats_gap_report.json (missing_laws from
resolve_cats_relations.py). For every fetch-worthy missing title
(قانون/آیین‌نامه/مصوبه/بخشنامه/دستورالعمل/اساسنامه):

  1. search ekhtebar (?s=<title>) — cached under out/fetched_laws/search/
  2. score the candidates by normalized-title similarity, fetch the best page
  3. extract the entry-content block, sanitize to plain text, sanity-check
     (length + legal markers), save as markdown
  4. register it exactly like ingest_cats_data parse-mode: sources.instruments
     upsert + canonical pipeline (provisions) + sources.source_documents row
     (metadata.cat='fetched_missing') so the next resolve run links everything

Everything still unfindable lands in out/missing_laws_unfound.json — the user
decides what to do with those.

Usage:
    LEGAL_AGENT_LLM_API_KEY not required. LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/fetch_missing_laws.py [--limit N] [--delay 1]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.canonical import InstrumentType
from legal_agent_core.ingestion.markdown_parser import MarkdownDocumentParser
from legal_agent_core.ingestion.models import ParsedDocument
from legal_agent_core.ingestion.normalization import normalize_legal_text
from legal_agent_core.ingestion.pipeline import CanonicalIngestionPipeline
from legal_agent_core.jalali import parse_jalali_date

SOURCE_ID = "ekhtebar"
BASE = "https://www.ekhtebar.ir"
CACHE = Path("out/fetched_laws")
USER_AGENT = "Mozilla/5.0 (legal-agent gap filling; contact: local)"

FETCH_PREFIXES = (
    "قانون", "آیین‌نامه", "آيين‌نامه", "آیین نامه", "مصوبه", "تصویب‌نامه",
    "بخشنامه", "دستورالعمل", "اساسنامه", "منشور", "لایحه",
)
#: mentions like «ماده ۸۴ قانون آیین دادرسی...» are article references, not laws
BOGUS_TITLE_RE = re.compile(
    r"^(ماده|تبصره|بند|اصل|رأی|رای|دادنامه|نظریه|حکم|مقررات|قواعد|رویه|مصوبات|قوانین)\b|"
    r"^(ماده|تبصره)\s*[۰-۹0-9]"
)
INSTRUMENT_HINT_RE = re.compile(r"قانون|آیین|آيين|مصوب|تصویب|دستورالعمل|اساسنامه|بخشنامه|لایحه")
ENTRY_RE = re.compile(r'<div class="entry-content entry clearfix">', re.DOTALL)
RESULT_RE = re.compile(r'<h2 class="post-title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
ENACTED_SLASH_RE = re.compile(r"مصوب\s+([۰-۹0-9]{4})[/\\]([۰-۹0-۹]{1,2})[/\\]([۰-۹0-۹]{1,2})")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get(url: str, cache: Path, delay: float) -> str | None:
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(body, encoding="utf-8")
        time.sleep(delay)
        return body
    except Exception as exc:  # noqa: BLE001
        log(f"  fetch {type(exc).__name__}: {url[:90]}")
        return None


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return html_mod.unescape(text)


def entry_text(page: str) -> str | None:
    match = ENTRY_RE.search(page)
    if not match:
        return None
    depth, start = 1, match.end()
    pos = start
    for tag in re.finditer(r"<div\b|</div>", page[start:]):
        pos = start + tag.end()
        depth += 1 if tag.group() == "<div" else -1
        if depth == 0:
            break
    html_block = page[start:pos - 6]
    html_block = re.sub(r"<(script|style)\b.*?</\1>", "", html_block, flags=re.DOTALL)
    html_block = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n\n", html_block)
    html_block = re.sub(r"<br\s*/?>", "\n", html_block)
    text = strip_tags(html_block)
    text = re.sub(r"[ \t\u200c]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


REJECT_PREFIXES = ("مقاله", "دانلود", "اخبار", "شرح ", "پرسش", "پاسخ ", "نکته ", "کاربرد ")


def search(query: str, delay: float) -> list[tuple[str, str]]:
    cache = CACHE / "search" / f"{hashlib.sha1(query.encode()).hexdigest()[:16]}.html"
    body = get(f"{BASE}/?s={urllib.parse.quote(query)}", cache, delay)
    if not body:
        return []
    out = []
    for href, title in RESULT_RE.findall(body):
        title = strip_tags(title).strip()
        if "ekhtebar." in href:
            out.append((title, href))
    return out


def token_cover(want_key: str, cand_key: str) -> float:
    """Share of the wanted title's words present in the candidate title."""
    want_tokens = [t for t in want_key.split() if len(t) > 2]
    if not want_tokens:
        return 0.0
    hit = sum(1 for t in want_tokens if t in cand_key)
    return hit / len(want_tokens)


def looks_legal(text: str) -> bool:
    if len(text) < 800:
        return False
    return ("ماده" in text or "مصوب" in text or "بند" in text)


def staged_slug_exists(db, slug: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sources.source_documents WHERE source_id=%s AND external_id=%s",
        (SOURCE_ID, slug),
    ).fetchone())


def ingest_fetched(db, repo, pipeline, *, title: str, slug: str, url: str, text: str) -> str | None:
    canonical_key = normalize_legal_text(title)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO sources.instruments
                   (canonical_key, canonical_title, instrument_type, external_ids)
               VALUES (%s, %s, 'statute', %s)
               ON CONFLICT (canonical_key) DO UPDATE SET external_ids = sources.instruments.external_ids || EXCLUDED.external_ids
               RETURNING instrument_uid""",
            (canonical_key, title, json.dumps({"ekhtebar": url}, ensure_ascii=False)),
        )
        stage_uid = cur.fetchone()[0]

    md_path = CACHE / f"{slug}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
    checksum = f"sha256:{hashlib.sha256(md_path.read_bytes()).hexdigest()}"

    try:
        parsed: ParsedDocument = MarkdownDocumentParser.from_text(
            md_path.read_text(encoding="utf-8"), md_path.name
        )
        from dataclasses import replace

        match = ENACTED_SLASH_RE.search(text)
        enacted = None
        if match:
            y, m, d = (int(part.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))) for part in match.groups())
            try:
                enacted = parse_jalali_date(f"{y}/{m:02d}/{d:02d}")
            except Exception:  # noqa: BLE001
                enacted = None
        version = parsed.version
        if enacted:
            version = replace(version, enacted_at=enacted, effective_from=enacted)
        instrument = replace(parsed.instrument, external_identifier=slug)
        result = pipeline.ingest(replace(parsed, instrument=instrument, version=version))
    except Exception as exc:  # noqa: BLE001 — no ماده structure: single-provision fallback
        from legal_agent_core.canonical import DocumentStatus, ProvisionType
        from legal_agent_core.ingestion.models import (
            DocumentVersionInput, InstrumentInput, ProvisionInput, SourceInput,
        )
        parsed = ParsedDocument(
            source=SourceInput(
                filename=md_path.name, media_type="text/markdown", checksum=checksum,
                file_size=md_path.stat().st_size, ingested_at=datetime.now(UTC),
                ingested_by="gap_filler", source_uri=url,
            ),
            instrument=InstrumentInput(
                title=title, canonical_title=canonical_key, instrument_type=InstrumentType.STATUTE,
                jurisdiction="IR", external_identifier=slug,
            ),
            version=DocumentVersionInput(status=DocumentStatus.PUBLISHED),
            provisions=(ProvisionInput(
                provision_type=ProvisionType.PARAGRAPH, label="متن", text=text.strip(), page_number=1,
            ),),
        )
        result = pipeline.ingest(parsed)

    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO sources.source_documents
                   (source_id, external_id, instrument_uid, match_status, doc_kind, title,
                    original_path, original_checksum, text_path, text_checksum,
                    text_extract_method, fetch_state, source_uri, metadata)
               VALUES (%s,%s,%s,'expert','statute',%s,%s,%s,%s,%s,'html','fetched',%s,%s)
               ON CONFLICT (source_id, external_id) DO UPDATE SET last_seen_at=now()""",
            (SOURCE_ID, slug, stage_uid, title, str(md_path), checksum, str(md_path), checksum,
             url, json.dumps({"cat": "fetched_missing"}, ensure_ascii=False)),
        )
    return result.instrument_id


def core_title(title: str) -> str:
    """Strip مصوب-date/اصلاح tails so variant phrasings dedupe to one fetch."""
    key = normalize_legal_text(title)
    key = re.sub(r"(مصوب|ابلاغی|اصلاحی).*$", "", key)
    key = re.sub(r"(با اصلاحات|و الحاقات|اصلاحات بعدی).*$", "", key)
    key = re.sub(r"[۰-۹0-9/\\.\-]+$", "", key)
    return key.strip(" -،")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--gap-report", default=Path("out/cats_gap_report.json"), type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    gap = json.loads(args.gap_report.read_text(encoding="utf-8"))
    missing = gap.get("missing_laws") or {}
    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    pipeline = CanonicalIngestionPipeline(repo)
    db = psycopg.connect(args.dsn, autocommit=True)

    # known keys for quick skip
    known_keys = {r[0] for r in db.execute("SELECT canonical_key FROM sources.instruments").fetchall()}

    wanted: dict[str, tuple[str, list]] = {}
    for key, info in missing.items():
        title = (info["titles"] or [""])[0]
        if not title.startswith(FETCH_PREFIXES):
            continue
        if BOGUS_TITLE_RE.match(title.strip()) or not INSTRUMENT_HINT_RE.search(title):
            continue
        if len(title) > 130:  # prose, not an instrument title
            continue
        if key in known_keys:
            continue
        core = core_title(title)
        if core and core not in wanted:  # variant phrasings share one fetch
            wanted[core] = (title, info.get("articles") or [])
    queue = list(wanted.values())
    log(f"fetch-worthy missing laws: {len(queue)} (of {len(missing)} gap titles, core-deduped)")
    if args.limit:
        queue = queue[: args.limit]

    ingested, unfound = [], []
    for n, (title, articles) in enumerate(queue, start=1):
        candidates = search(title, args.delay)
        if not candidates:
            unfound.append({"title": title, "articles": articles, "reason": "no search results"})
            continue
        want_key = normalize_legal_text(title)
        scored = []
        for cand_title, href in candidates:
            if cand_title.startswith(REJECT_PREFIXES) or BOGUS_TITLE_RE.match(cand_title.strip()):
                continue
            if not cand_title.startswith(FETCH_PREFIXES):
                continue
            cand_key = normalize_legal_text(cand_title)
            contains = 1.0 if (want_key in cand_key or cand_key in want_key) else 0.0
            cover = token_cover(want_key, cand_key)
            scored.append((max(contains, cover * 0.95), cand_title, href))
        scored.sort(reverse=True)
        best_score, best_title, best_href = scored[0] if scored else (0.0, "", "")
        if best_score < 0.7:
            unfound.append({"title": title, "articles": articles,
                            "reason": f"weak match {best_score:.2f}: {best_title[:60]}"})
            continue
        slug = urllib.parse.unquote(urllib.parse.urlparse(best_href).path.rstrip("/").rsplit("/", 1)[-1])
        if staged_slug_exists(db, slug):
            ingested.append({"title": title, "slug": slug, "note": "already staged"})
            continue
        page = get(best_href, CACHE / "pages" / f"{slug}.html", args.delay)
        if not page:
            unfound.append({"title": title, "articles": articles, "reason": "page fetch failed"})
            continue
        text = entry_text(page)
        if not text or not looks_legal(text):
            unfound.append({"title": title, "articles": articles, "reason": "page not legal text"})
            continue
        canonical_id = ingest_fetched(db, repo, pipeline, title=best_title, slug=slug, url=best_href, text=text)
        repo.connection.commit()
        ingested.append({"title": best_title, "slug": slug, "canonical_id": canonical_id})
        log(f"  [{n}/{len(wanted)}] + {best_title[:70]}")

    Path("out/missing_laws_unfound.json").write_text(
        json.dumps({"count": len(unfound), "unfound": unfound}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log(f"ingested: {len(ingested)} | unfound: {len(unfound)} -> out/missing_laws_unfound.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
