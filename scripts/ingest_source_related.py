#!/usr/bin/env python3
"""Ingest the human-curated «مستندات مرتبط» block of the source documents.

The ekhtebar corpus prepends a curated navigation block to every law page:

    ### مستندات مرتبط
    - آیین‌نامه / قوانین مرتبط / رای وحدت رویه / نظریه مشورتی   (category links)
    - <bullet per related document>

A human editor maintains it, so it is a trustworthy source of law<->law
relations that never appear as «ماده N» mentions inside any provision (which
is why the reference extractor can not see them). This script turns those
bullets into:

  - RELATED_TO instrument->instrument edges for قانون/لایحه/آیین‌نامه/... items
    (created_by='source-curation'; skipped when the pair already has any edge)
  - CITES judgment->instrument edges for «رأی وحدت رویه شماره N» bullets that
    resolve to exactly one staged judgment (sources.source_documents, cats)

Matching is deterministic (longest catalog title contained in the bullet,
no amendment guard — the list intentionally names amending laws) with an
optional LLM fallback for unmatched law bullets (--skip-llm to disable).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from publish_relations import llm_resolve_phrases, load_catalog, load_title_catalog

from legal_agent_core.enrichment.related import (
    LAW_RE,
    REGULATION_RE,
    RULING_NUMBER_RE,
    RULING_RE,
    _fold,
    match_bullet,
    parse_related_block,
)
from legal_agent_core.ingestion.normalization import normalize_legal_text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN"))
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="max source docs (0 = all)")
    args = parser.parse_args()
    dsn = args.dsn or "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"

    key = None
    if not args.skip_llm:
        key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
        if not key:
            api_txt = Path("~/api.txt").expanduser()
            if api_txt.exists():
                match = re.search(r'"zai".*?"apiKey":\s*"([^"]+)"', api_txt.read_text(encoding="utf-8", errors="replace"), re.DOTALL)
                if match:
                    key = match.group(1).strip()
            if not key:
                raise SystemExit("no LLM key (use --skip-llm for deterministic only)")

    db = psycopg.connect(dsn, autocommit=True)
    catalog_by_key, sorted_titles = load_catalog(db)

    with db.cursor() as cur:
        # every edge between two instruments, either direction: a curated
        # RELATED_TO must not duplicate/shadow a stronger typed relation
        cur.execute(
            """SELECT source_node_id, target_node_id FROM canonical.graph_edges
               WHERE source_node_type='legal_instrument' AND target_node_type='legal_instrument'"""
        )
        instrument_pairs = set()
        for src, dst in cur.fetchall():
            instrument_pairs.add((src, dst))
            instrument_pairs.add((dst, src))
        cur.execute(
            """SELECT i.instrument_id, sd.document_uid, sd.text_path
               FROM canonical.legal_instruments i
               JOIN sources.instruments si ON si.canonical_key = i.canonical_title
               JOIN sources.source_documents sd ON sd.instrument_uid = si.instrument_uid
               WHERE sd.text_path IS NOT NULL
               ORDER BY i.instrument_id"""
        )
        docs = cur.fetchall()
        # staged judicial docs (cats) for رأی وحدت رویه matching; mirror uses
        # document_uid as the Judgment/AdvisoryOpinion node id (same label
        # rule as laws_mirror._sync_judgments)
        cur.execute(
            """SELECT document_uid, title FROM sources.source_documents
               WHERE source_id='cats' AND doc_kind IN ('judgment', 'unknown')"""
        )
        judicial = cur.fetchall()

    if args.limit:
        docs = docs[: args.limit]
    # دو دادگاه شماره‌گذاری وحدت رویه را جداگانه انجام می‌دهند: «دیوان عالی
    # کشور» و «دیوان عدالت اداری» هر دو «رأی وحدت رویه شماره ۸۳۸» دارند —
    # self.number -> [(uid, node_type, title)]
    rulings_by_number: dict[str, list[tuple[str, str, str]]] = {}
    for document_uid, title in judicial:
        match = RULING_NUMBER_RE.search(normalize_legal_text(title or ""))
        if match:
            node_type = "advisory_opinion" if "نظریه" in (title or "") else "judgment"
            rulings_by_number.setdefault(match.group(1), []).append(
                (str(document_uid), node_type, title or "")
            )

    def pick_ruling(bullet: str, cands: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        """Court filter: the bullet names its court («دیوان عالی کشور» /
        «دیوان عدالت اداری»); prefer matching-court candidates, keep the rest
        only when the preferred court yields nothing."""
        folded = _fold(bullet)
        court = "عدالت" if "عدالت" in folded else "عالی"
        preferred = [c for c in cands if court in _fold(c[2])]
        return preferred or cands

    stats = {"docs": 0, "with_block": 0, "bullets": 0, "laws": 0, "regulations": 0,
             "rulings": 0, "matched_catalog": 0, "matched_llm": 0, "edges": 0,
             "cites": 0, "unmatched": 0, "skipped_existing_pair": 0, "unresolved_rulings": 0}

    pending_law_bullets: list[tuple[str, str, str, str]] = []  # (source_iid, doc_uid, bullet, phrase)

    def add_related(source_iid: str, doc_uid: str, target_iid: str, bullet: str, confidence: float) -> None:
        if target_iid == source_iid:
            return
        if (source_iid, target_iid) in instrument_pairs:
            stats["skipped_existing_pair"] += 1
            return
        instrument_pairs.add((source_iid, target_iid))
        instrument_pairs.add((target_iid, source_iid))
        edge_id = "edge:related:" + hashlib.sha1(f"{source_iid}|{target_iid}".encode()).hexdigest()[:24]
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, extraction_method, confidence,
                       created_by, source_id
                   ) VALUES (%s, 'legal_instrument', %s, 'legal_instrument', %s,
                             'related_to', 'curation', %s, 'source-curation', %s)
                   ON CONFLICT (edge_id) DO NOTHING""",
                (edge_id, source_iid, target_iid, confidence, doc_uid),
            )
            stats["edges"] += cur.rowcount

    for instrument_id, document_uid, text_path in docs:
        stats["docs"] += 1
        try:
            text = Path(text_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        bullets = parse_related_block(text)
        if not bullets:
            continue
        stats["with_block"] += 1
        stats["bullets"] += len(bullets)
        for bullet in bullets:
            normalized = normalize_legal_text(bullet)
            # classify on the FOLDED text — raw bullets carry RLM/ZWNJ combos
            # (words with internal ZWNJ/RLM) the raw kind regexes don't cover
            folded = _fold(bullet)
            if RULING_RE.match(folded):
                stats["rulings"] += 1
                number_match = RULING_NUMBER_RE.search(normalized)
                if not number_match:
                    continue
                candidates = pick_ruling(bullet, rulings_by_number.get(number_match.group(1), []))
                if len(candidates) != 1:
                    stats["unresolved_rulings"] += 1
                    continue
                ruling_uid, ruling_type, _ruling_title = candidates[0]
                edge_id = "edge:cites:" + hashlib.sha1(f"{ruling_uid}|{instrument_id}".encode()).hexdigest()[:24]
                with db.cursor() as cur:
                    cur.execute(
                        """INSERT INTO canonical.graph_edges (
                               edge_id, source_node_type, source_node_id, target_node_type,
                               target_node_id, edge_type, extraction_method, confidence,
                               created_by, source_id
                           ) VALUES (%s, %s, %s, 'legal_instrument', %s,
                                     'cites', 'curation', 0.9, 'source-curation', %s)
                           ON CONFLICT (edge_id) DO NOTHING""",
                        (edge_id, ruling_type, ruling_uid, instrument_id, str(document_uid)),
                    )
                    stats["cites"] += cur.rowcount
            elif not folded.startswith("نظریه"):
                # everything else (قانون/لایحه/آیین‌نامه/دستورالعمل/مصوبه and
                # statutes whose official name starts with «اصلاح…» /
                # «مقررات…») is a relatable document
                if LAW_RE.match(folded):
                    stats["laws"] += 1
                elif REGULATION_RE.match(folded):
                    stats["regulations"] += 1
                target = match_bullet(bullet, catalog_by_key, sorted_titles)
                if target:
                    stats["matched_catalog"] += 1
                    add_related(instrument_id, str(document_uid), target, bullet, 0.9)
                else:
                    pending_law_bullets.append((instrument_id, str(document_uid), bullet, normalized))

    if pending_law_bullets and key:
        title_by_key, _ = load_title_catalog(db)
        verdicts = llm_resolve_phrases(
            key, title_by_key, [b[2] for b in pending_law_bullets], "related_verdicts.json"
        )
        for instrument_id, document_uid, bullet, _normalized in pending_law_bullets:
            pick = verdicts.get(bullet)
            target = catalog_by_key.get(normalize_legal_text(pick)) if pick else None
            if target:
                stats["matched_llm"] += 1
                add_related(instrument_id, document_uid, target, bullet, 0.75)
            else:
                stats["unmatched"] += 1
    elif pending_law_bullets:
        stats["unmatched"] += len(pending_law_bullets)

    with db.cursor() as cur:
        cur.execute(
            """SELECT edge_type, count(*) FROM canonical.graph_edges
               WHERE created_by='source-curation' GROUP BY 1"""
        )
        totals = dict(cur.fetchall())
    print(json.dumps({"run": stats, "total_source_curation_edges": totals}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
