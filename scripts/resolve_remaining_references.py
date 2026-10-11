#!/usr/bin/env python3
"""Resolve the remaining unresolved canonical.explicit_references.

Classes found in the data (post bare-ref pass):
  «ماده N» / «ماده N این قانون»  -> same instrument, article N (deterministic;
      ~40% of these now resolve since provisions.number was backfilled)
  «ماده N قانون X» / «تبصره N قانون X» -> cross-instrument: title resolved via
      normalized/difflib match, ambiguous hints via LLM (glm-5.3-flash, cached
      per distinct hint); resolved title + existing article -> provision edge,
      no article -> provision->instrument edge
  «ماده N» pointing at articles that do not exist in the parsed corpus ->
      marked 'ambiguous' (no invented edges)

Writes explicit_references.resolution_status + graph_edges
('explicitly_references', source_span kept per CHECK). Idempotent: only
'unresolved' rows are considered, edge ids are deterministic.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/resolve_remaining_references.py
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.normalization import normalize_legal_text

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
API_TXT = Path("~/api.txt").expanduser()
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

BARE_SAME = re.compile(r"^ماده\s*([0-9]+)\s*(?:این\s*قانون)?$")
NAMED = re.compile(
    r"^(?:تبصره|بند\s+[\u0621-\u064aائ]+)?\s*"
    r"(?:ماده|ماده‌های|مواد|تبصره‌های|بندهای|اصل)\s*"
    r"([۰-۹0-9]+)(?:\s*(?:و|تا)\s*[۰-۹0-9]+)*\s*"
    r"(قانون|آیین[\u200c\s]*نامه|مصوبه|مقررات|نظامنامه|دستورالعمل)\s+(.{3,})$",
    re.DOTALL,
)
TITLE_ONLY = re.compile(
    r"^(قانون|آیین[\u200c\s]*نامه|مصوبه|مقررات|نظامنامه|دستورالعمل)\s+(.{3,})$"
)
STRIP_TAIL = re.compile(r"مصوب\s*[\d۰-۹][\d۰-۹/]*|\(منسوخ\)|\[منسوخ\]|\.$")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api_key() -> str:
    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if key:
        return key.strip()
    match = re.search(
        r'"zai".*?"apiKey":\s*"([^"]+)"',
        API_TXT.read_text(encoding="utf-8", errors="replace"),
        re.DOTALL,
    )
    if not match:
        raise SystemExit("no LEGAL_AGENT_LLM_API_KEY and no zai apiKey in ~/api.txt")
    return match.group(1).strip()


def llm_pick(key: str, hint: str, options: list[str], cache: dict) -> str | None:
    if hint in cache:
        return cache[hint]
    system = "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان؛ حدس نزن، اگر مطمئن نیستی null بده."
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 500,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "عبارت ارجاعی: «" + hint + "»\n"
                "به کدام گزینه اشاره می‌کند؟ اگر هیچ‌کدام نیست null بده.\n"
                "گزینه‌ها: " + json.dumps(options, ensure_ascii=False)
                + '\nخروجی: {"pick":"<عنوان یا null>"}',
            },
        ],
    }
    import urllib.request

    pick: str | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
            )
            with urllib.request.urlopen(request, timeout=120) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"] or ""
            fenced = JSON_BLOCK_RE.search(content)
            candidate = fenced.group(1) if fenced else content
            start = min(
                (i for i in (candidate.find("{"), candidate.find("[")) if i >= 0),
                default=-1,
            )
            parsed = json.loads(candidate[start:])
            pick_raw = parsed.get("pick") if isinstance(parsed, dict) else None
            pick = pick_raw if pick_raw in options else None
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                log(f"  llm failed for «{hint[:40]}»: {exc}")
            time.sleep(2 * (attempt + 1))
    cache[hint] = pick
    return pick


def edge_id_for(reference_id: str) -> str:
    return "edge:ref:" + hashlib.sha1(reference_id.encode()).hexdigest()[:24]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    db = psycopg.connect(args.dsn, autocommit=True)
    with db.cursor() as cur:
        cur.execute(
            """SELECT r.reference_id, r.source_span_id, r.target_text,
                      pv.provision_id, pv.instrument_id
                 FROM canonical.explicit_references r
                 JOIN canonical.provision_versions pv
                   ON pv.provision_version_id = r.source_provision_version_id
                WHERE r.resolution_status = 'unresolved'"""
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT instrument_id, number, min(provision_id)
                 FROM canonical.provisions
                WHERE provision_type IN ('article', 'note') AND number IS NOT NULL
                GROUP BY 1, 2"""
        )
        article_map = {
            (instrument_id, number.strip()): provision_id
            for instrument_id, number, provision_id in cur.fetchall()
        }
        cur.execute(
            """SELECT instrument_id, title, authority_level
                 FROM canonical.legal_instruments"""
        )
        instruments = cur.fetchall()
    norm_titles = {
        instrument_id: normalize_legal_text(title)
        for instrument_id, title, _ in instruments
    }
    title_by_id = {instrument_id: title for instrument_id, title, _ in instruments}
    log(f"unresolved refs: {len(rows)}, article keys: {len(article_map)}")

    stats = {"same_resolved": 0, "named_resolved": 0, "named_instrument": 0,
             "ambiguous": 0, "llm_calls": 0, "other": 0}
    edges: list[tuple[str, str, str, float, str, str | None]] = []
    llm_jobs: list[tuple[str, str | None, str, str, str, str | None]] = []
    cache: dict[str, str | None] = {}

    def top3(norm: str) -> list[str]:
        return [
            title_by_id[iid]
            for iid, _ in sorted(
                (
                    (iid, difflib.SequenceMatcher(None, norm, t).ratio())
                    for iid, t in norm_titles.items()
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
        ]

    for reference_id, span_id, target_text, provision_id, instrument_id in rows:
        text = STRIP_TAIL.sub(" ", target_text.strip().translate(DIGITS))
        text = re.sub(r"\s+", " ", text)
        if bare := BARE_SAME.match(text):
            target = article_map.get((instrument_id, bare.group(1)))
            if target and target != provision_id:
                edges.append((reference_id, provision_id, target, 0.95, "parser", span_id))
                stats["same_resolved"] += 1
            else:
                with db.cursor() as cur:
                    cur.execute(
                        """UPDATE canonical.explicit_references
                              SET resolution_status='ambiguous', confidence=0.3
                            WHERE reference_id=%s""",
                        (reference_id,),
                    )
                stats["ambiguous"] += 1
            continue
        if named := NAMED.match(text):
            number, kind, rest = named.group(1), named.group(2), named.group(3)
            hint = f"{kind} {rest.strip()}"
        elif title_only := TITLE_ONLY.match(text):
            number, hint = None, f"{title_only.group(1)} {title_only.group(2).strip()}"
        else:
            stats["other"] += 1
            continue
        norm = normalize_legal_text(hint)
        exact = next((iid for iid, t in norm_titles.items() if t == norm), None)
        if exact:
            picked, method, confidence = exact, "parser", 0.9
        else:
            best_id, best_ratio = max(
                (
                    (iid, difflib.SequenceMatcher(None, norm, t).ratio())
                    for iid, t in norm_titles.items()
                ),
                key=lambda item: item[1],
            )
            if best_ratio < 0.55 and not args.no_llm:
                llm_jobs.append(
                    (hint, number, reference_id, provision_id, instrument_id, span_id)
                )
                continue
            if best_ratio < 0.55:
                stats["ambiguous"] += 1
                continue
            picked, method, confidence = (
                best_id,
                "parser",
                min(0.85, 0.5 + best_ratio / 2),
            )
        resolve_named(
            db, stats, edges, article_map, reference_id, span_id,
            provision_id, instrument_id, picked, number, method, confidence,
        )

    if llm_jobs and not args.no_llm:
        key = api_key()
        log(f"LLM hints to resolve: {len(set(job[0] for job in llm_jobs))}")
        for hint, number, reference_id, provision_id, instrument_id, span_id in llm_jobs:
            norm = normalize_legal_text(hint)
            options = top3(norm)
            picked_title = llm_pick(key, hint, options, cache)
            stats["llm_calls"] += 1
            if not picked_title:
                stats["ambiguous"] += 1
                continue
            picked = next(
                (iid for iid, t in title_by_id.items() if t == picked_title), None
            )
            if not picked:
                stats["ambiguous"] += 1
                continue
            resolve_named(
                db, stats, edges, article_map, reference_id, span_id,
                provision_id, instrument_id, picked, number, "llm", 0.75,
            )

    with db.cursor() as cur:
        for reference_id, source_provision, target, confidence, method, span_id in edges:
            target_kind = "provision" if target.startswith("prov_") else "legal_instrument"
            if target_kind == "provision":
                # schema CHECK: 'resolved' requires a provision target; an
                # instrument-level match keeps 'ambiguous' (same convention as
                # publish_relations named stage) — the edge carries the link
                cur.execute(
                    """UPDATE canonical.explicit_references
                          SET resolution_status='resolved',
                              resolved_target_provision_id=%s,
                              confidence=%s
                        WHERE reference_id=%s""",
                    (target, confidence, reference_id),
                )
            else:
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
                       confidence, created_by, source_id
                   ) VALUES (%s, 'provision', %s, %s, %s,
                             'explicitly_references', %s, %s, %s, %s, %s)
                   ON CONFLICT (edge_id) DO NOTHING""",
                (
                    edge_id_for(reference_id), source_provision, target_kind, target,
                    span_id, method, confidence,
                    "refs-batch2" if method == "parser" else "refs-llm2",
                    reference_id,
                ),
            )
        cur.execute(
            "SELECT resolution_status, count(*) FROM canonical.explicit_references GROUP BY 1"
        )
        status = dict(cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM canonical.graph_edges WHERE edge_type='explicitly_references'"
        )
        total_refs = cur.fetchone()[0]
    log(f"stats: {stats}")
    log(f"resolution_status: {status}")
    log(f"explicitly_references edges: {total_refs}")
    db.close()
    return 0


def resolve_named(
    db, stats, edges, article_map, reference_id, span_id,
    provision_id, instrument_id, picked, number, method, confidence,
):
    if picked == instrument_id:
        # named self-reference («این قانون») — provision-level only
        target = article_map.get((picked, number)) if number else None
        if target and target != provision_id:
            edges.append((reference_id, provision_id, target, confidence, method, span_id))
            stats["same_resolved"] += 1
        else:
            stats["ambiguous"] += 1
        return
    if number:
        target = article_map.get((picked, number))
        if target:
            edges.append((reference_id, provision_id, target, confidence, method, span_id))
            stats["named_resolved"] += 1
            return
    edges.append((reference_id, provision_id, picked, confidence, method, span_id))
    stats["named_instrument"] += 1


if __name__ == "__main__":
    raise SystemExit(main())
