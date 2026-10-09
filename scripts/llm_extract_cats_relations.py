#!/usr/bin/env python3
"""LLM sweep of the cats corpus: citations, legal effects, statutory basis.

Phase 2 of the cats enrichment plan. For every staged ekhtebar cats document
(sources.source_documents WHERE metadata ? 'cat'):

  1. the WHOLE markdown text goes to the LLM in one call (no chunking) — the
     model returns strict JSON: citations (ماده X قانون Y), effects (منسوخ/
     حذف/اصلاح/الحاق/ابطال/مغایر/تفسیر/لازم‌الاتباع), statutory basis
     (به استناد ...) and footnote provision texts
  2. every returned quote is verified verbatim against the source text
     (schema golden rule) — entries without a witness quote are dropped
  3. targets are resolved against sources.instruments (exact/normalized match,
     difflib fallback); resolved citation/interpret/conflict/basis targets get
     canonical CITES/INTERPRETS/CONFLICTS_WITH/IMPLEMENTS edges whose source
     is the provision version actually containing the quote; REPEALS/AMENDS/
     ANNULS-type changes go through sources.legal_effects review instead
  4. everything (including unresolved targets) lands in
     sources.extraction_jobs (stage='reference_resolution') for the phase-3
     gap-filling loop; effects also land in sources.legal_effects as
     review_status='candidate'

Usage:
    LEGAL_AGENT_LLM_API_KEY=... LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/llm_extract_cats_relations.py \
        [--cats "آرا وحدت رویه,..."] [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.canonical import CanonicalEdge, CanonicalEdgeType, CreationMethod, Provenance
from legal_agent_core.ingestion.normalization import normalize_legal_text

BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"
PROMPT_VERSION = "cats-rel-v1"
DONE_LOG = Path("out/cats_relations_done.txt")

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)
#: psycopg connections are not thread-safe; LLM calls run in parallel but all
#: database reads/writes are serialized behind this lock
DB_LOCK = threading.Lock()

SYSTEM_PROMPT = """تو یک استخراج‌کننده دقیق روابط حقوقی هستی. متن کامل یک سند حقوقی ایرانی (رأی، نظریه، آیین‌نامه، بخشنامه، مصوبه، لایحه یا مقاله) به تو داده می‌شود.
فقط روابطی را استخراج کن که در متن صراحتاً آمده‌اند. هیچ چیز را حدس نزن. خروجی فقط و فقط JSON باشد.

ساختار خروجی:
{
 "document": {"title": "...", "number": "...", "date": "...", "authority": "..."},
 "citations": [{"target_title": "عنوان دقیق قانون/آیین‌نامه/مصوبه مقصد", "article": "ماده ۱۲۳ یا تبصره ۲ ماده ۱۲۳ یا null", "quote": "جمله عینی از متن سند که ارجاع در آن است"}],
 "effects": [{"type": "repeal|amend|append|supplement|repeal|replace|suspend|restore|annul|interpret|conflict", "target_title": "...", "provision_label": "ماده/تبصره هدف یا null", "quote": "...", "scope": "total|partial"}],
 "basis": [{"target_title": "...", "article": "...", "quote": "..."}],
 "footnotes": [{"target_title": "...", "article": "ماده ۱۲۳", "full_text": "متن کامل ماده از زیرنویس"}]
}

قواعد مهم:
- references به ماده‌های خودِ سند (ارجاع داخلی) citation نیست؛ فقط ارجاع به قوانین/مصوبات/آیین‌نامه‌های دیگر.
- «به استناد ماده X قانون Y» هم basis است هم اگر Y سند دیگر است citation.
- «لازم‌الاتباع بودن» یا «تفسیر ماده X» درباره رأی وحدت رویه/نظریه → effects با type="interpret".
- «منسوخ شدن» → type="repeal"، «ابطال» → "annul"، «مغایر قانون» → "conflict"، «حذف ماده» → "repeal" با scope="partial"، «اصلاح» → "amend"، «الحاق» → "append".
- quote باید عیناً از متن باشد (حداقل ۲۰ کاراکتر، جمله کامل).
- target_title را دقیقاً همان‌طور که در متن آمده بنویس (با سال مصوب اگر آمده).
- حداکثر ۶۰ citation؛ اگر سند صدها ارجاع داخلی دارد فقط ارجاع‌های بین‌سندی.
- اگر چیزی نیست، آرایه خالی بده."""

EFFECT_TYPES = {
    "repeal", "amend", "append", "supplement", "replace",
    "suspend", "restore", "annul", "interpret", "conflict",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def extract_json(text: str):
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = min((i for i in (candidate.find("{"), candidate.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON")
    return json.loads(candidate[start:])


def llm_json(key: str, prompt: str, retries: int = 8, max_tokens: int = 16000) -> dict:
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(request, timeout=600) as resp:
                data = json.loads(resp.read())
            return extract_json(data["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            time.sleep(40 if exc.code == 429 else 5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError(last_exc)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def quote_verified(quote: str, raw_squashed: str) -> bool:
    if not quote or len(quote.strip()) < 20:
        return False
    return squash(quote) in raw_squashed


def norm_num(article: str | None) -> str | None:
    """First digit run of an article mention, as ASCII digits."""
    if not article:
        return None
    m = re.search(r"[۰-۹0-9]+", article)
    return m.group().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")) if m else None


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class TargetResolver:
    """Resolve Persian instrument titles to (staging uuid, canonical id)."""

    def __init__(self, db: psycopg.Connection) -> None:
        rows = db.execute(
            """SELECT s.instrument_uid, s.canonical_key, s.canonical_title, i.instrument_id
               FROM sources.instruments s
               LEFT JOIN canonical.legal_instruments i ON i.canonical_title = s.canonical_key"""
        ).fetchall()
        self.by_key: dict[str, tuple[str, str | None]] = {}
        self.titles: list[str] = []
        for uid, key, title, canonical_id in rows:
            self.by_key[key] = (uid, canonical_id)
            self.titles.append(key)
        self._cache: dict[str, tuple[str, str | None] | None] = {}

    def resolve(self, title: str | None) -> tuple[str, str | None] | None:
        if not title or len(title.strip()) < 4:
            return None
        if title in self._cache:
            return self._cache[title]
        key = normalize_legal_text(title)
        result = self.by_key.get(key)
        if result is None:
            exact = [t for t in self.titles if key and (key in t or t in key)]
            if len(exact) == 1:
                result = self.by_key[exact[0]]
            else:
                close = difflib.get_close_matches(key, self.titles, n=1, cutoff=0.87)
                result = self.by_key[close[0]] if close else None
        self._cache[title] = result
        return result


def load_instrument_context(db: psycopg.Connection, canonical_ids: list[str]):
    """Map canonical instrument -> [(provision_version_id, squashed text)]."""
    if not canonical_ids:
        return {}
    rows = db.execute(
        """SELECT p.instrument_id, pv.provision_version_id, pv.normalized_text
           FROM canonical.provisions p
           JOIN canonical.provision_versions pv ON pv.provision_id = p.provision_id
           WHERE p.instrument_id = ANY(%s)""",
        (canonical_ids,),
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for instrument_id, pvid, text in rows:
        out.setdefault(instrument_id, []).append((pvid, squash(text or "")))
    return out


def find_citing_version(texts: list[tuple[str, str]], quote: str) -> str | None:
    q = squash(quote)
    for pvid, text in texts:
        if q in text:
            return pvid
    return None


def edge_id(source: str, target: str, edge_type: str, quote: str) -> str:
    return f"edge:cats:{sha1('|'.join((source, target, edge_type, quote)))[:24]}"


def canonical_ids_for(db, document_uid: str, metadata: dict | None) -> list[str]:
    """Canonical instrument ids of a staged doc (parse-mode: 1; rulings: many)."""
    meta = metadata or {}
    if meta.get("canonical_instrument_id"):
        return [meta["canonical_instrument_id"]]
    if meta.get("ruling_uids"):
        rows = db.execute(
            """SELECT i.instrument_id FROM canonical.legal_instruments i
               JOIN sources.instruments s ON s.canonical_key = i.canonical_title
               WHERE s.instrument_uid = ANY(%s)""",
            (meta["ruling_uids"],),
        ).fetchall()
        return [r[0] for r in rows]
    row = db.execute(
        """SELECT i.instrument_id FROM canonical.legal_instruments i
           JOIN sources.instruments s ON s.canonical_key = i.canonical_title
           JOIN sources.source_documents d ON d.instrument_uid = s.instrument_uid
           WHERE d.document_uid = %s LIMIT 1""",
        (document_uid,),
    ).fetchone()
    return [row[0]] if row else []


def process(
    db,
    repo,
    key: str,
    row,
    resolver: TargetResolver,
    texts_cache: dict,
    force: bool = False,
) -> dict:
    document_uid, external_id, title, text_path, checksum, metadata = row
    with DB_LOCK:
        if not force:
            exists = db.execute(
                """SELECT 1 FROM sources.extraction_jobs
                   WHERE document_uid=%s AND stage='reference_resolution' AND prompt_version=%s""",
                (document_uid, PROMPT_VERSION),
            ).fetchone()
            if exists:
                return {"status": "skipped"}

    raw = Path(text_path).read_text(encoding="utf-8", errors="replace")
    cut = re.search(r"^#{1,6}\s*تازه‌های قوانین.*$", raw, re.MULTILINE)
    if cut:
        raw = raw[: cut.start()]
    raw_squashed = squash(raw)
    if len(raw_squashed) < 60:
        return {"status": "too_short"}

    result = llm_json(key, f"عنوان سند: {title}\n\nمتن سند:\n{raw}")
    citations = result.get("citations") or []
    effects = result.get("effects") or []
    basis = result.get("basis") or []
    footnotes = result.get("footnotes") or []

    with DB_LOCK:
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO sources.extraction_jobs
                       (document_uid, stage, status, model, prompt_version, input_checksum, output)
                   VALUES (%s, 'reference_resolution', 'succeeded', %s, %s, %s, %s)
                   ON CONFLICT (document_uid, stage, input_checksum, prompt_version) DO UPDATE
                       SET output = EXCLUDED.output, status='succeeded', finished_at=now()""",
                (document_uid, MODEL, PROMPT_VERSION, checksum,
                 json.dumps(result, ensure_ascii=False)),
            )

    stats = {"cites": 0, "edges": 0, "effects": 0, "resolved": 0, "unresolved": 0, "footnotes": 0}

    with DB_LOCK:
        canonical_ids = canonical_ids_for(db, document_uid, metadata)
        cache_key = tuple(canonical_ids)
        if cache_key not in texts_cache:
            texts_cache[cache_key] = load_instrument_context(db, list(cache_key))
        pv_texts = texts_cache[cache_key]

        def target_instrument(entry_title: str | None):
            return resolver.resolve(entry_title)

        def citing_version_for(quote: str) -> str | None:
            for texts in pv_texts.values():
                pvid = find_citing_version(texts, quote)
                if pvid:
                    return pvid
            return None

        def add_edge(source_node: str, target_node: str, edge_type: CanonicalEdgeType, quote: str, confidence: float = 0.75) -> bool:
            if not target_node or source_node == target_node:
                return False
            try:
                repo.add_edge(
                    CanonicalEdge(
                        edge_id=edge_id(source_node, target_node, edge_type.value, quote),
                        source_node_id=source_node,
                        target_node_id=target_node,
                        edge_type=edge_type,
                        provenance=Provenance(
                            created_by="cats_llm_sweep",
                            creation_method=CreationMethod.LLM_EXTRACTOR,
                            source_id=external_id,
                            model_id=MODEL,
                        ),
                        confidence=confidence,
                    )
                )
                return True
            except Exception:  # noqa: BLE001 — duplicates/missing nodes are expected
                return False

        # --- citations -> CITES (+ provision-level target when article resolves)
        for entry in citations:
            quote = entry.get("quote") or ""
            if not quote_verified(quote, raw_squashed):
                continue
            target = target_instrument(entry.get("target_title"))
            if not target:
                stats["unresolved"] += 1
                continue
            stats["resolved"] += 1
            stage_uid, target_canonical = target
            source_node = citing_version_for(quote) or (canonical_ids[0] if canonical_ids else None)
            if not source_node or not target_canonical:
                continue
            stats["cites"] += 1
            target_node = target_canonical
            article = entry.get("article")
            if article:
                number = norm_num(article)
                if number:
                    prov = db.execute(
                        """SELECT provision_id FROM canonical.provisions
                           WHERE instrument_id=%s AND number=%s AND provision_type='article' LIMIT 1""",
                        (target_canonical, number),
                    ).fetchone()
                    if prov:
                        target_node = prov[0]
            if add_edge(source_node, target_node, CanonicalEdgeType.CITES, quote):
                stats["edges"] += 1

        # --- basis (به استناد) -> IMPLEMENTS --------------------------------
        for entry in basis:
            quote = entry.get("quote") or ""
            if not quote_verified(quote, raw_squashed):
                continue
            target = target_instrument(entry.get("target_title"))
            if not target or not target[1]:
                stats["unresolved"] += 1
                continue
            stats["resolved"] += 1
            source_node = citing_version_for(quote) or (canonical_ids[0] if canonical_ids else None)
            if source_node and add_edge(source_node, target[1], CanonicalEdgeType.IMPLEMENTS, quote):
                stats["edges"] += 1

        # --- effects ----------------------------------------------------------
        for entry in effects:
            quote = entry.get("quote") or ""
            etype = (entry.get("type") or "").strip().lower()
            if etype not in EFFECT_TYPES or not quote_verified(quote, raw_squashed):
                continue
            target = target_instrument(entry.get("target_title"))
            stats["effects"] += 1
            if not target:
                stats["unresolved"] += 1
                continue
            stage_uid, target_canonical = target
            if etype in ("interpret", "conflict") and target_canonical:
                source_node = citing_version_for(quote) or (canonical_ids[0] if canonical_ids else None)
                edge_type = CanonicalEdgeType.INTERPRETS if etype == "interpret" else CanonicalEdgeType.CONFLICTS_WITH
                if source_node and add_edge(source_node, target_canonical, edge_type, quote):
                    stats["edges"] += 1
            if etype in ("repeal", "amend", "append", "supplement", "replace", "suspend", "restore", "annul"):
                with db.cursor() as cur:
                    cur.execute(
                        """INSERT INTO sources.legal_effects
                               (affecting_document_uid, affected_instrument_uid, affected_provision_label,
                                effect_type, mode, scope, witness_quote, witness_checksum, confidence,
                                detected_by, review_status, created_by)
                           VALUES (%s,%s,%s,%s,'explicit',%s,%s,%s,0.75,'llm','candidate','cats_llm_sweep')
                           ON CONFLICT DO NOTHING""",
                        (
                            document_uid, stage_uid, entry.get("provision_label"), etype,
                            entry.get("scope") or "partial",
                            quote.strip(), sha1(quote.strip()),
                        ),
                    )

    stats["footnotes"] = len(footnotes)
    with DB_LOCK:
        repo.connection.commit()
    return {"status": "ok", **stats}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--cats", default="", help="comma-separated category subset")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY") or (
        Path("/tmp/opencode/llm_key").read_text().strip() if Path("/tmp/opencode/llm_key").exists() else ""
    )
    if not key:
        parser.error("LEGAL_AGENT_LLM_API_KEY missing")

    from legal_agent_core.adapters.postgres import PostgresCanonicalRepository

    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    db = psycopg.connect(args.dsn, autocommit=True)
    wanted = {c.strip() for c in args.cats.split(",") if c.strip()} or None

    rows = db.execute(
        """SELECT document_uid, external_id, title, text_path, original_checksum, metadata
           FROM sources.source_documents
           WHERE source_id='ekhtebar' AND metadata ? 'cat' AND text_path IS NOT NULL
           ORDER BY document_uid"""
    ).fetchall()
    if wanted:
        rows = [r for r in rows if (r[5] or {}).get("cat") in wanted]
    if args.limit:
        rows = rows[: args.limit]
    log(f"documents: {len(rows)}")

    resolver = TargetResolver(db)
    texts_cache: dict = {}
    done_file = DONE_LOG
    done = set(done_file.read_text().splitlines()) if done_file.exists() else set()

    counters = {"ok": 0, "skipped": 0, "too_short": 0, "error": 0}
    edges_total = effects_total = unresolved_total = 0
    started = time.time()

    def work(row):
        if row[1] in done and not args.force:
            return {"status": "skipped"}
        return process(db, repo, key, row, resolver, texts_cache, force=args.force)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for n, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            try:
                res = future.result()
            except Exception as exc:  # noqa: BLE001
                counters["error"] += 1
                log(f"  !! {row[1]}: {type(exc).__name__}: {str(exc)[:160]}")
                continue
            counters[res["status"]] = counters.get(res["status"], 0) + 1
            edges_total += res.get("edges", 0)
            effects_total += res.get("effects", 0)
            unresolved_total += res.get("unresolved", 0)
            if res["status"] == "ok":
                with open(done_file, "a", encoding="utf-8") as fh:
                    fh.write(row[1] + "\n")
            if n % 25 == 0:
                rate = n / max(time.time() - started, 1) * 60
                log(
                    f"  {n}/{len(rows)} done | edges={edges_total} effects={effects_total} "
                    f"unresolved={unresolved_total} | {rate:.0f} docs/min | errors={counters['error']}"
                )

    log(json.dumps(counters, ensure_ascii=False))
    log(f"TOTAL edges={edges_total} effects={effects_total} unresolved={unresolved_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
