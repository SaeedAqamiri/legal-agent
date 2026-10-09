#!/usr/bin/env python3
"""Re-fetch ekhtebar pages for the cats corpus and harvest «مستندات مرتبط».

Phase 4 of the cats enrichment plan. The md dump lost the hyperlinks of the
tabbed «مستندات مرتبط» widget; the live page still has (title, href) pairs
grouped by tab (آیین‌نامه / قوانین مرتبط / رای وحدت رویه / نظریه مشورتی) plus
machine-readable articleSection in the Yoast JSON-LD.

For every staged cats document:
  1. fetch the source page (cached as HTML under out/fetched_html/)
  2. extract {tab -> [(title, href)]} and the site articleSection
  3. store both in sources.source_documents.metadata
  4. resolve hrefs to staged external ids; when both ends are known add a
     canonical CITES edge (deterministic, confidence 0.9); unresolved hrefs
     land in out/cats_unfetched_links.json as a phase-3 ingest queue

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/fetch_cats_links.py \
        [--cats "آرا وحدت رویه,..."] [--limit N] [--delay 0.8]
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.canonical import CanonicalEdge, CanonicalEdgeType, CreationMethod, Provenance

CACHE_DIR = Path("out/fetched_html")
USER_AGENT = "Mozilla/5.0 (legal-agent cats enrichment; contact: local)"
TOGGLE_RE = re.compile(r"<h3[^>]*>\s*مستندات مرتبط", re.DOTALL)
TAB_LABEL_RE = re.compile(r'<a\s+href="#tab-content-(\d+)"[^>]*>\s*([^<]+?)\s*</a>', re.DOTALL)
TAB_CONTENT_RE = re.compile(r'<div class="tab-content" id="tab-content-(\d+)">', re.DOTALL)
LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
JSONLD_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*class="yoast-schema-graph"[^>]*>(.*?)</script>', re.DOTALL
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url: str, cache_path: Path, delay: float) -> str | None:
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        return cache_path.read_text(encoding="utf-8", errors="replace")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(body, encoding="utf-8")
        time.sleep(delay)
        return body
    except Exception as exc:  # noqa: BLE001
        log(f"  fetch failed {url}: {type(exc).__name__}")
        time.sleep(delay)
        return None


def slug_of(href: str) -> str | None:
    href = href.strip()
    match = re.match(r"^https?://(?:www\.)?ekhtebar\.(?:ir|com)/([^/?#]+)/?$", href)
    if match:
        return urllib.parse.unquote(match.group(1))
    match = re.match(r"^https?://(?:www\.)?ekhtebar\.(?:ir|com)/\?p=(\d+)$", href)
    if match:
        return f"?{match.group(1)}"
    return None


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return html_mod.unescape(re.sub(r"\s+", " ", text)).strip()


def parse_related(page: str) -> dict[str, list[dict]]:
    """{tab_label: [{title, href}]} from the tabbed widget."""
    toggle = TOGGLE_RE.search(page)
    if not toggle:
        return {}
    tail = page[toggle.start():]
    labels = {num: strip_tags(label) for num, label in TAB_LABEL_RE.findall(tail[:4000])}
    blocks = list(TAB_CONTENT_RE.finditer(tail))
    out: dict[str, list[dict]] = {}
    for idx, block in enumerate(blocks):
        end = blocks[idx + 1].start() if idx + 1 < len(blocks) else len(tail)
        section = tail[block.end():end]
        # cut the section at the matching closing </div> of tab-content
        depth = 1
        cut = len(section)
        for close in re.finditer(r"</div>", section):
            depth -= 1
            if depth == 0:
                cut = close.start()
                break
        label = labels.get(block.group(1), block.group(1))
        links = []
        for href, title in LINK_RE.findall(section[:cut]):
            href = html_mod.unescape(href)
            if "ekhtebar." in href:
                links.append({"title": strip_tags(title), "href": href})
        if links:
            out.setdefault(label, []).extend(links)
    return out


def parse_site_category(page: str) -> str | None:
    match = JSONLD_RE.search(page)
    if not match:
        return None
    try:
        graph = json.loads(match.group(1)).get("@graph", [])
    except json.JSONDecodeError:
        return None
    for node in graph:
        if node.get("@type") == "Article":
            sections = node.get("articleSection")
            if isinstance(sections, list) and sections:
                return sections[0]
            if isinstance(sections, str):
                return sections
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--cats", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.8)
    args = parser.parse_args()

    wanted = {c.strip() for c in args.cats.split(",") if c.strip()} or None
    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    db = psycopg.connect(args.dsn, autocommit=True)

    rows = db.execute(
        """SELECT document_uid, external_id, source_uri, metadata
           FROM sources.source_documents
           WHERE source_id='ekhtebar' AND metadata ? 'cat' AND source_uri IS NOT NULL
           ORDER BY document_uid"""
    ).fetchall()
    if wanted:
        rows = [r for r in rows if (r[3] or {}).get("cat") in wanted]
    if args.limit:
        rows = rows[: args.limit]
    log(f"pages to fetch: {len(rows)}")

    # external_id -> canonical instrument id (for link resolution)
    canonical_by_external: dict[str, str | None] = {
        r[0]: r[1]
        for r in db.execute(
            """SELECT d.external_id, i.instrument_id
               FROM sources.source_documents d
               LEFT JOIN canonical.legal_instruments i
                 ON i.canonical_title = (SELECT canonical_key FROM sources.instruments
                                          WHERE instrument_uid = d.instrument_uid)
               WHERE d.source_id='ekhtebar'"""
        ).fetchall()
    }

    unresolved_links: dict[str, list[dict]] = {}
    counters = {"fetched": 0, "cached": 0, "with_widget": 0, "edges": 0, "missing": 0}
    started = time.time()

    for n, (document_uid, external_id, source_uri, metadata) in enumerate(rows, start=1):
        cache_path = CACHE_DIR / f"{external_id}.html"
        existed = cache_path.exists()
        body = fetch(source_uri, cache_path, args.delay)
        if not body:
            counters["missing"] += 1
            continue
        counters["cached" if existed else "fetched"] += 1

        related = parse_related(body)
        site_cat = parse_site_category(body)
        if related:
            counters["with_widget"] += 1

        canonical_source = canonical_by_external.get(external_id)
        edge_targets: list[str] = []
        for links in related.values():
            for link in links:
                target_slug = slug_of(link["href"])
                if not target_slug:
                    continue
                target = canonical_by_external.get(target_slug, "__missing__")
                if target == "__missing__":
                    unresolved_links.setdefault(external_id, []).append(link)
                elif target and target != canonical_source:
                    edge_targets.append(target)

        if canonical_source and edge_targets:
            for target in dict.fromkeys(edge_targets):
                edge = CanonicalEdge(
                    edge_id=f"edge:mostanadat:{hashlib.sha1((canonical_source + target).encode()).hexdigest()[:24]}",
                    source_node_id=canonical_source,
                    target_node_id=target,
                    edge_type=CanonicalEdgeType.CITES,
                    provenance=Provenance(
                        created_by="cats_fetch_links",
                        creation_method=CreationMethod.DETERMINISTIC_EXTRACTOR,
                        source_id=external_id,
                    ),
                    confidence=0.9,
                )
                try:
                    repo.add_edge(edge)
                    counters["edges"] += 1
                except Exception:  # noqa: BLE001 — duplicates expected
                    continue

        meta = dict(metadata or {})
        if related:
            meta["mostanadat"] = related
        if site_cat:
            meta["site_cat"] = site_cat
        db.execute(
            "UPDATE sources.source_documents SET metadata=%s, last_seen_at=now() WHERE document_uid=%s",
            (json.dumps(meta, ensure_ascii=False), document_uid),
        )

        if n % 25 == 0:
            repo.connection.commit()
        if n % 50 == 0:
            rate = n / max(time.time() - started, 1) * 60
            log(f"  {n}/{len(rows)} | widget={counters['with_widget']} edges={counters['edges']} "
                f"missing={counters['missing']} | {rate:.0f} pages/min")

    repo.connection.commit()
    Path("out/cats_unfetched_links.json").write_text(
        json.dumps(
            {
                "unresolved_link_count": sum(len(v) for v in unresolved_links.values()),
                "unresolved": unresolved_links,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    log(json.dumps(counters, ensure_ascii=False))
    log(f"unresolved links queued: {sum(len(v) for v in unresolved_links.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
