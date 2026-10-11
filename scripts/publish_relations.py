#!/usr/bin/env python3
"""Publish relations into the canonical graph (batch, deterministic + LLM results).

Steps:
  1. effects   — batch-approve+publish candidate legal_effects whose confidence
                 clears --min-confidence and whose both sides map to canonical
                 instruments (same gate as the review endpoint).
  2. bare refs — «ماده N» references the deterministic extractor left unresolved:
                 resolved to the article of the SAME instrument (no LLM needed).
  2b. crosslaw — «ماده N قانون X» mentions whose law name the extractor dropped:
                 target_text rewritten from the provision text, then resolved
                 deterministically (catalog substring containment, no LLM).
  3. named refs— references that name another law («ماده ۵ قانون X») resolved by
                 the LLM (glm-5.3-flash) against the instrument catalog with
                 difflib top-3 candidates; verdict -> explicit_references.updated
                 + EXPLICITLY_REFERENCES edges.
Prints final graph statistics.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.normalization import normalize_legal_text

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
BARE_RE = re.compile(r"^ماده\s*([۰-۹0-9]+)$")
# Bare resolution also accepts the explicit local scope («ماده ۲ این قانون»).
BARE_SELF_RE = re.compile(
    r"^ماده\s*([۰-۹0-9]+)(?:\s+این\s+(?:قانون|آیین[\u200c\s-]*نامه|دستورالعمل|بخشنامه|مصوبه))?$"
)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

EFFECT_EDGE = {
    "amend": "amends",
    "append": "amends",
    "supplement": "amends",
    "repeal": "repeals",
    "annul": "annuls",
    "replace": "replaces",
    "suspend": "suspends",
    "restore": "restores",
    "interpret": "interprets",
    "conflict": "conflicts_with",
    "implement": "implements",
}

# «ماده (۱۲۲) قانون کار جمهوری اسلامی ایران» — capture the target-law name the
# legacy extractor dropped (it only kept «ماده ۱۲۲»).
CROSS_LAW_RE = re.compile(
    r"ماده[\s\u200c]*[\(]?\s*([۰-۹0-9]+)(?:\s*[\)])?[\s\u200c]+"
    r"((?:قانون|آیین[\u200c\s-]*نامه|دستورالعمل|مصوبه|مقررات|شیوه[\u200c\s]*نامه)[^\n،؛.!]{0,100})"
)
AMENDMENT_GUARD_RE = re.compile(r"اصلاح[yی‌\s]*$")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def extract_json(text: str):
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = min(
        (i for i in (candidate.find("{"), candidate.find("[")) if i >= 0), default=-1
    )
    if start < 0:
        raise ValueError("no JSON")
    return json.loads(candidate[start:])


def llm_json(key: str, prompt: str, system: str, retries: int = 5, max_tokens: int = 3000) -> dict | list:
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read())
            return extract_json(data["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as exc:
            if attempt == retries - 1:
                raise
            time.sleep(25 if exc.code == 429 else 3 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError


def publish_effects(db: psycopg.Connection, min_confidence: float) -> dict:
    stats = {"published": 0, "approved_no_target": 0, "kept_candidate": 0}
    with db.cursor() as cur:
        cur.execute(
            """SELECT e.effect_uid, e.effect_type, e.confidence,
                      ci.instrument_id, ci2.instrument_id
               FROM sources.legal_effects e
               JOIN sources.source_documents sd ON sd.document_uid = e.affecting_document_uid
               JOIN sources.instruments si ON si.instrument_uid = sd.instrument_uid
               LEFT JOIN sources.instruments di ON di.instrument_uid = e.affected_instrument_uid
               JOIN canonical.legal_instruments ci ON ci.canonical_title = si.canonical_key
               LEFT JOIN canonical.legal_instruments ci2 ON ci2.canonical_title = di.canonical_key
               WHERE e.review_status IN ('candidate', 'approved')"""
        )
        rows = cur.fetchall()
    for effect_uid, effect_type, confidence, source_id, target_id in rows:
        if (confidence or 0) < min_confidence:
            stats["kept_candidate"] += 1
            continue
        publishable = bool(source_id and target_id and source_id != target_id)
        with db.cursor() as cur:
            if publishable:
                cur.execute(
                    """INSERT INTO canonical.graph_edges (
                           edge_id, source_node_type, source_node_id, target_node_type,
                           target_node_id, edge_type, source_span_id, extraction_method,
                           confidence, created_by, source_id, model_id
                       ) VALUES (%s, 'legal_instrument', %s, 'legal_instrument', %s,
                                 %s, NULL, 'llm', %s, 'effects-batch', %s, %s)
                       ON CONFLICT (edge_id) DO NOTHING""",
                    (
                        f"edge:effect:{effect_uid}",
                        source_id,
                        target_id,
                        EFFECT_EDGE.get(str(effect_type), "amends"),
                        float(confidence),
                        f"effect:{effect_uid}",
                        MODEL,
                    ),
                )
                stats["published"] += cur.rowcount
            else:
                stats["approved_no_target"] += 1
            cur.execute(
                """UPDATE sources.legal_effects SET review_status='approved', reviewed_by='batch', reviewed_at=now()
                   WHERE effect_uid=%s""",
                (effect_uid,),
            )
    db.commit()
    return stats


def resolve_bare_refs(db: psycopg.Connection) -> dict:
    stats = {"resolved": 0, "edges": 0, "no_article": 0}
    with db.cursor() as cur:
        cur.execute(
            """SELECT r.reference_id, r.source_span_id, r.target_text,
                      pv.provision_id, pv.instrument_id
               FROM canonical.explicit_references r
               JOIN canonical.provision_versions pv ON pv.provision_version_id = r.source_provision_version_id
               WHERE r.resolution_status = 'unresolved'
                 AND r.target_text ~ '^ماده\\s*[۰-۹0-9]+(\\s+این\\s+[^\\s]+)?$'"""
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT p.instrument_id, p.number, min(p.provision_id)
               FROM canonical.provisions p
               WHERE p.provision_type = 'article' AND p.number IS NOT NULL
               GROUP BY p.instrument_id, p.number"""
        )
        article_map = {
            (instrument_id, number.strip().translate(DIGITS)): pid
            for instrument_id, number, pid in cur.fetchall()
        }
    for reference_id, span_id, target_text, provision_id, instrument_id in rows:
        match = BARE_SELF_RE.match(target_text.strip())
        if match is None:
            stats["no_article"] += 1
            continue
        number = match.group(1).translate(DIGITS)
        target = article_map.get((instrument_id, number))
        if not target or target == provision_id:
            stats["no_article"] += 1
            continue
        with db.cursor() as cur:
            cur.execute(
                """UPDATE canonical.explicit_references
                   SET resolution_status='resolved', resolved_target_provision_id=%s, confidence=0.95
                   WHERE reference_id=%s""",
                (target, reference_id),
            )
            cur.execute(
                """INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_by, source_id
                   ) VALUES (%s, 'provision', %s, 'provision', %s,
                             'explicitly_references', %s, 'parser', 0.95, 'refs-batch', %s)
                   ON CONFLICT (edge_id) DO NOTHING""",
                (
                    f"edge:ref:{hashlib.sha1(reference_id.encode()).hexdigest()[:24]}",
                    provision_id,
                    target,
                    span_id,
                    reference_id,
                ),
            )
            stats["edges"] += cur.rowcount
        stats["resolved"] += 1
    db.commit()
    return stats


def load_catalog(db: psycopg.Connection) -> tuple[dict[str, str], list[str]]:
    """normalized title -> instrument_id, plus normalized titles longest-first."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT instrument_id, canonical_title, title FROM canonical.legal_instruments"
        )
        rows = cur.fetchall()
    catalog_by_key: dict[str, str] = {}
    for instrument_id, canonical_title, title in rows:
        for name in (canonical_title, title):
            key = normalize_legal_text(name or "")
            if key:
                catalog_by_key.setdefault(key, instrument_id)
    return catalog_by_key, sorted(catalog_by_key, key=len, reverse=True)


def match_catalog(phrase: str, catalog_by_key: dict[str, str], sorted_titles: list[str]) -> str | None:
    """Deterministic: longest catalog title contained in the phrase.

    «...قانون اصلاح X» guard: the amending law is not X itself — keep scanning.
    """
    key = normalize_legal_text(phrase)
    for title in sorted_titles:
        position = key.find(title)
        if position < 0:
            continue
        if AMENDMENT_GUARD_RE.search(key[:position]):
            continue
        return catalog_by_key[title]
    return None


def resolve_cross_law_refs(db: psycopg.Connection) -> dict:
    """Backfill stage: the legacy extractor stored bare «ماده N» and dropped
    the target-law name. Re-scan the provision text, rewrite target_text to
    «ماده N <law name>», then resolve deterministically against the catalog
    (substring containment). Leftovers stay unresolved for the LLM stage.
    """
    stats = {"scanned_versions": 0, "rewritten": 0, "resolved": 0, "edges": 0}
    catalog_by_key, sorted_titles = load_catalog(db)
    with db.cursor() as cur:
        cur.execute(
            """SELECT pv.provision_version_id, pv.provision_id, pv.normalized_text,
                      array_agg(r.reference_id), array_agg(r.target_text)
                 FROM canonical.explicit_references r
                 JOIN canonical.provision_versions pv
                   ON pv.provision_version_id = r.source_provision_version_id
                WHERE r.resolution_status = 'unresolved'
                GROUP BY 1, 2, 3"""
        )
        versions = cur.fetchall()
    log(f"crosslaw: scanning {len(versions)} provision versions")

    updates: list[tuple[str, str]] = []  # (reference_id, new target_text)
    resolutions: dict[str, tuple[str, str, str]] = {}  # ref -> (provision_id, span_id, text)
    for _, provision_id, text, ref_ids, target_texts in versions:
        stats["scanned_versions"] += 1
        mentions: dict[str, set[str]] = {}
        for number, name in CROSS_LAW_RE.findall(text or ""):
            mentions.setdefault(number.translate(DIGITS), set()).add(name.strip())
        for reference_id, target_text in zip(ref_ids, target_texts):
            bare = BARE_RE.match((target_text or "").strip())
            if bare:
                names = mentions.get(bare.group(1).translate(DIGITS), set())
                if len(names) != 1:
                    continue
                new_text = f"ماده {bare.group(1).translate(DIGITS)} {names.pop().strip()}".strip()
                if new_text != target_text:
                    updates.append((reference_id, new_text))
                    resolutions[reference_id] = (provision_id, new_text)
            else:
                # Already-named leftover the previous LLM run could not match —
                # try the deterministic catalog containment on it now.
                if (target_text or "").strip():
                    resolutions[reference_id] = (provision_id, target_text)

    if updates:
        with db.cursor() as cur:
            for reference_id, new_text in updates:
                cur.execute(
                    """UPDATE canonical.explicit_references SET target_text=%s
                        WHERE reference_id=%s AND resolution_status='unresolved'""",
                    (new_text, reference_id),
                )
                stats["rewritten"] += cur.rowcount
        db.commit()
    log(f"crosslaw: {stats['rewritten']} target_texts rewritten")

    for reference_id, (provision_id, phrase) in resolutions.items():
        instrument_id = match_catalog(phrase, catalog_by_key, sorted_titles)
        if not instrument_id:
            continue
        with db.cursor() as cur:
            # Atomic status flip + edge insert (CTE): the schema requires a
            # source_span_id on explicitly_references edges, so resolve it from
            # the reference row rather than passing NULL.
            cur.execute(
                """WITH upd AS (
                       UPDATE canonical.explicit_references
                          SET resolution_status='ambiguous', confidence=0.9
                        WHERE reference_id=%s AND resolution_status='unresolved'
                        RETURNING reference_id, source_span_id
                   )
                   INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_by, source_id
                   )
                   SELECT %s, 'provision', %s, 'legal_instrument', %s,
                          'explicitly_references', u.source_span_id, 'parser', 0.9,
                          'refs-crosslaw', %s
                     FROM upd u
                   ON CONFLICT (edge_id) DO NOTHING""",
                (
                    reference_id,
                    f"edge:ref:{hashlib.sha1(reference_id.encode()).hexdigest()[:24]}",
                    provision_id,
                    instrument_id,
                    reference_id,
                ),
            )
            if cur.rowcount == 0:
                continue  # already resolved (or no span) — skip the edge
            stats["edges"] += cur.rowcount
        stats["resolved"] += 1
    db.commit()
    return stats


def resolve_named_refs(db: psycopg.Connection, key: str) -> dict:
    stats = {"resolved": 0, "edges": 0, "unmatched": 0, "calls": 0}
    with db.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT target_text FROM canonical.explicit_references
               WHERE resolution_status='unresolved'
                 AND target_text !~ '^ماده\\s*[۰-۹0-9]+(\\s+این\\s+[^\\s]+)?$'
                 AND target_text !~ 'قانون$'"""
        )
        phrases = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT canonical_title FROM canonical.legal_instruments")
        catalog = [row[0] for row in cur.fetchall()]
        catalog_by_key = {normalize_legal_text(title): title for title in catalog}
    log(f"named phrases to resolve: {len(phrases)}")

    def candidates_for(phrase: str) -> list[str]:
        key_norm = normalize_legal_text(phrase)
        # The «ماده N» prefix is not part of the law title — matching without it
        # keeps short titles («قانون کار») competitive in the difflib shortlist.
        match_part = re.sub(r"^ماده\s*[۰-۹0-9]+[\s\u200c]*", "", key_norm)[:80]
        # real_quick_ratio is a length-only upper bound of ratio(): cheap enough
        # for the whole catalog, then full ratio() for the top slice only.
        matchers = [
            (title, difflib.SequenceMatcher(None, match_part, key))
            for key, title in catalog_by_key.items()
        ]
        shortlist = sorted(matchers, key=lambda item: item[1].real_quick_ratio(), reverse=True)[:50]
        scored = sorted(shortlist, key=lambda item: item[1].ratio(), reverse=True)
        picks = [item[0] for item in scored[:3]]
        # difflib buries short titles («قانون کار») under long phrases — make
        # sure any title literally contained in the phrase is also offered.
        for tkey, title in catalog_by_key.items():
            if tkey in key_norm and title not in picks:
                picks.append(title)
        return picks[:5]

    def prompt(chunk: list[str]) -> str:
        lines = []
        for i, phrase in enumerate(chunk, 1):
            options = candidates_for(phrase)
            lines.append(
                f"{i}. «{phrase}» → گزینه‌ها: {json.dumps(options, ensure_ascii=False)}"
            )
        return (
            "هر ارجاع زیر به کدام قانون اشاره می‌کند؟ از بین گزینه‌ها یکی را انتخاب کن "
            "(اگر گزینه درستی نیست null بده). «قانون اصلاح X» یعنی سند اصلاح‌کننده، خودِ X نیست.\n"
            'خروجی: {"verdicts":[{"n":1,"pick":"<عنوان یا null>"}]}\n\n'
            + "\n".join(lines)
        )

    system = "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان."
    chunks = [phrases[i : i + 8] for i in range(0, len(phrases), 8)]
    verdicts: dict[str, str] = {}
    cache_path = Path(__file__).resolve().parents[1] / "out" / "named_ref_verdicts.json"
    try:
        verdicts.update(json.loads(cache_path.read_text(encoding="utf-8")))
        log(f"verdict cache: {len(verdicts)} entries loaded")
    except (OSError, ValueError):
        pass
    pending = [chunk for chunk in chunks if any(p not in verdicts for p in chunk)]
    log(f"named refs: {len(chunks)} chunks total, {len(pending)} to call")
    def call_chunk(chunk: list[str]) -> dict | list:
        # Prompt-building (candidates_for × 8 phrases) runs in the worker so
        # the main thread stays free to consume results and log progress.
        return llm_json(key, prompt(chunk), system)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(call_chunk, chunk): chunk for chunk in pending}
        for done, future in enumerate(as_completed(futures), 1):
            chunk = futures[future]
            try:
                payload = future.result()
                stats["calls"] += 1
                for verdict in (
                    payload.get("verdicts", []) if isinstance(payload, dict) else []
                ):
                    try:
                        idx = int(verdict.get("n", 0)) - 1
                        pick = verdict.get("pick")
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if pick and 0 <= idx < len(chunk):
                        verdicts[chunk[idx]] = str(pick)
            except Exception as exc:  # noqa: BLE001
                log(f"  chunk failed: {type(exc).__name__}: {str(exc)[:80]}")
            if done % 10 == 0 or done == len(pending):
                log(f"  named refs: {done}/{len(pending)} chunks")
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(verdicts, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError:
                    pass
    log(f"LLM resolved {len(verdicts)}/{len(phrases)} phrases")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(verdicts, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    title_to_id: dict[str, str] = {}
    with db.cursor() as cur:
        cur.execute(
            "SELECT canonical_title, instrument_id FROM canonical.legal_instruments"
        )
        title_to_id = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            """SELECT r.reference_id, r.source_span_id, r.target_text,
                      pv.provision_id, r.confidence
               FROM canonical.explicit_references r
               JOIN canonical.provision_versions pv ON pv.provision_version_id = r.source_provision_version_id
               WHERE r.resolution_status='unresolved'
                 AND r.source_span_id IS NOT NULL
                 AND r.target_text !~ '^ماده\\s*[۰-۹0-9]+(\\s+این\\s+[^\\s]+)?$'
                 AND r.target_text !~ 'قانون$'"""
        )
        rows = cur.fetchall()
    for reference_id, span_id, target_text, provision_id, old_confidence in rows:
        picked_title = verdicts.get(target_text)
        if not picked_title or picked_title not in title_to_id:
            stats["unmatched"] += 1
            continue
        target_instrument = title_to_id[picked_title]
        confidence = max(0.7, float(old_confidence or 0.7))
        with db.cursor() as cur:
            cur.execute(
                """UPDATE canonical.explicit_references
                   SET resolution_status='ambiguous', confidence=%s
                   WHERE reference_id=%s""",
                (confidence, reference_id),
            )
            cur.execute(
                """INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_by, source_id, model_id
                   ) VALUES (%s, 'provision', %s, 'legal_instrument', %s,
                             'explicitly_references', %s, 'llm', %s, 'refs-llm', %s, %s)
                   ON CONFLICT (edge_id) DO NOTHING""",
                (
                    f"edge:ref:{hashlib.sha1(reference_id.encode()).hexdigest()[:24]}",
                    provision_id,
                    target_instrument,
                    span_id,
                    confidence,
                    reference_id,
                    MODEL,
                ),
            )
            stats["edges"] += cur.rowcount
        stats["resolved"] += 1
    db.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--stages", default="effects,bare,named")
    parser.add_argument(
        "--skip-llm", action="store_true", help="skip the named-refs LLM stage"
    )
    args = parser.parse_args()

    key = None
    if not args.skip_llm:
        key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
        if not key:
            text = (
                Path("~/api.txt")
                .expanduser()
                .read_text(encoding="utf-8", errors="replace")
            )
            match = re.search(r'"zai".*?"apiKey":\s*"([^"]+)"', text, re.DOTALL)
            if not match:
                raise SystemExit("no LLM key")
            key = match.group(1).strip()

    db = psycopg.connect(args.dsn, autocommit=True)
    stages = {s.strip() for s in args.stages.split(",")}

    if "effects" in stages:
        stats = publish_effects(db, args.min_confidence)
        log(f"effects: {stats}")

    if "bare" in stages:
        stats = resolve_bare_refs(db)
        log(f"bare refs: {stats}")

    if "crosslaw" in stages:
        stats = resolve_cross_law_refs(db)
        log(f"crosslaw refs: {stats}")

    if "named" in stages and not args.skip_llm:
        stats = resolve_named_refs(db, key)
        log(f"named refs: {stats}")

    with db.cursor() as cur:
        cur.execute(
            """SELECT edge_type, count(*) FROM canonical.graph_edges GROUP BY 1 ORDER BY 2 DESC"""
        )
        log(
            "graph edges by type: "
            + json.dumps(dict(cur.fetchall()), ensure_ascii=False)
        )
        cur.execute("SELECT count(*) FROM canonical.graph_edges")
        log(f"total edges: {cur.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
