#!/usr/bin/env python3
"""Re-resolve stored LLM relation outputs against the (grown) corpus.

Phase 3, step 1. The LLM sweep (llm_extract_cats_relations.py) stores its raw
JSON in sources.extraction_jobs. Target resolution improves every time new
laws enter the corpus, so this script re-runs resolution offline — no LLM
calls — over every stored job:

  1. re-resolve citation/basis/effect targets against sources.instruments
  2. add canonical CITES/IMPLEMENTS/INTERPRETS/CONFLICTS_WITH edges for newly
     resolved targets (same golden rule: the quote must exist in the document
     text stored with the job)
  3. insert sources.legal_effects rows for newly resolved change events
  4. write out/cats_gap_report.json:
       - missing_laws:    target titles with no corpus instrument -> fetch/OCR queue
       - missing_provisions: {instrument: [ماده numbers]} cited but not yet
         extracted from an existing instrument -> LLM provision filler queue

Idempotent: edges use content-derived ids, effects dedupe on the unique index.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/resolve_cats_relations.py
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.canonical import CanonicalEdge, CanonicalEdgeType, CreationMethod, Provenance
from legal_agent_core.ingestion.normalization import normalize_legal_text

EFFECT_TYPES = {
    "repeal", "amend", "append", "supplement", "replace",
    "suspend", "restore", "annul", "interpret", "conflict",
}


def log(msg: str) -> None:
    print(f"[{__import__('time').strftime('%H:%M:%S')}] {msg}", flush=True)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def norm_num(article: str | None) -> str | None:
    """First digit run of an article mention, as ASCII digits."""
    if not article:
        return None
    m = re.search(r"[۰-۹0-9]+", article)
    return m.group().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")) if m else None


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Resolver:
    """Title -> (staging uuid, canonical id) with a core-title index.

    Persian instrument titles circulate in many phrasings (enactment dates,
    amendment tails, year variants). Matching is layered:
      1. exact normalized key
      2. core-title index (enactment/amendment tails stripped from both sides)
      3. containment of the wanted core in a candidate key (unique hit only)
      4. difflib >= 0.87 fallback
      5. full token coverage with a length guard
    """

    TAIL_PATTERNS = (
        r"(مصوب|ابلاغی|اصلاحی).*$",
        r"(با اصلاحات|و الحاقات|اصلاحات بعدی).*$",
    )

    @staticmethod
    def core_of(key: str) -> str:
        for pattern in Resolver.TAIL_PATTERNS:
            key = re.sub(pattern, "", key)
        key = re.sub(r"[۰-۹0-9/\\.\-ـ]+$", "", key)
        return re.sub(r"\s+", " ", key).strip(" -،")

    def __init__(self, db: psycopg.Connection) -> None:
        rows = db.execute(
            """SELECT s.instrument_uid, s.canonical_key, i.instrument_id
               FROM sources.instruments s
               LEFT JOIN canonical.legal_instruments i ON i.canonical_title = s.canonical_key"""
        ).fetchall()
        self.by_key: dict[str, tuple[str, str | None]] = {}
        self.titles: list[str] = []
        self.core_index: dict[str, list[str]] = {}
        for uid, key, canonical_id in rows:
            self.by_key[key] = (uid, canonical_id)
            self.titles.append(key)
            core = self.core_of(key)
            if len(core) >= 8:
                self.core_index.setdefault(core, []).append(key)
        for core in list(self.core_index):
            keys = sorted(set(self.core_index[core]), key=len)
            if len(keys) > 1:
                canonical_ids = {self.by_key[k][1] for k in keys}
                keys = keys if len(canonical_ids) == 1 else keys[:1]
            self.core_index[core] = keys
        self._cache: dict[str, tuple[str, str | None] | None] = {}

    @staticmethod
    def _tokens(key: str) -> list[str]:
        return [t for t in key.split() if len(t) > 2]

    def resolve(self, title: str | None) -> tuple[str, str | None] | None:
        if not title or len(title.strip()) < 4:
            return None
        if title in self._cache:
            return self._cache[title]
        key = normalize_legal_text(title)
        result = self._resolve_key(key)
        self._cache[title] = result
        return result

    def _resolve_key(self, key: str) -> tuple[str, str | None] | None:
        if key in self.by_key:
            return self.by_key[key]
        core = self.core_of(key)
        if core in self.core_index:
            return self.by_key[self.core_index[core][0]]
        exact = [t for t in self.titles if core and (core in t or t in core) and abs(len(t) - len(core)) <= 60]
        if len(exact) == 1:
            return self.by_key[exact[0]]
        close = difflib.get_close_matches(key, self.titles, n=1, cutoff=0.87)
        if close:
            return self.by_key[close[0]]
        want_tokens = set(self._tokens(core or key))
        if want_tokens:
            hits = [
                t for t in self.titles
                if want_tokens <= set(self._tokens(t))
                and len(self._tokens(t)) - len(want_tokens) <= 4
            ]
            if len(hits) == 1:
                return self.by_key[hits[0]]
        return None


def provision_exists(db, canonical_id: str, article: str | None) -> str | None:
    number = norm_num(article)
    if not number:
        return None
    row = db.execute(
        """SELECT provision_id FROM canonical.provisions
           WHERE instrument_id=%s AND number=%s AND provision_type='article' LIMIT 1""",
        (canonical_id, number),
    ).fetchone()
    return row[0] if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--report", default=Path("out/cats_gap_report.json"), type=Path)
    args = parser.parse_args()

    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    db = psycopg.connect(args.dsn, autocommit=True)
    resolver = Resolver(db)

    jobs = db.execute(
        """SELECT j.job_id, j.document_uid, j.output, d.external_id
           FROM sources.extraction_jobs j
           JOIN sources.source_documents d ON d.document_uid = j.document_uid
           WHERE j.stage='reference_resolution' AND j.prompt_version='cats-rel-v1'"""
    ).fetchall()
    log(f"jobs to re-resolve: {len(jobs)}")

    missing_laws: dict[str, dict] = {}
    missing_provisions: dict[str, set] = defaultdict(set)
    counters = {"edges": 0, "effects": 0, "jobs": 0}

    for job_id, document_uid, output, external_id in jobs:
        if not output:
            continue
        counters["jobs"] += 1
        raw_squashed = None  # lazily loaded per doc (quotes must re-verify)

        canonical_ids = [
            r[0] for r in db.execute(
                """SELECT i.instrument_id FROM canonical.legal_instruments i
                   JOIN sources.instruments s ON s.canonical_key = i.canonical_title
                   JOIN sources.source_documents d ON d.instrument_uid = s.instrument_uid
                   WHERE d.document_uid=%s""",
                (document_uid,),
            ).fetchall()
        ]
        source_node = canonical_ids[0] if canonical_ids else None
        if not source_node:
            continue

        # provision versions of this document for quote-anchored edges
        pv_rows = db.execute(
            """SELECT pv.provision_version_id, pv.normalized_text
               FROM canonical.provisions p
               JOIN canonical.provision_versions pv ON pv.provision_id = p.provision_id
               WHERE p.instrument_id = ANY(%s)""",
            (canonical_ids,),
        ).fetchall()
        pv_texts = [(pvid, squash(text or "")) for pvid, text in pv_rows]

        def citing_for(quote: str) -> str | None:
            q = squash(quote)
            for pvid, text in pv_texts:
                if q in text:
                    return pvid
            return None

        def verified(quote: str) -> bool:
            nonlocal raw_squashed
            if not quote or len(quote.strip()) < 20:
                return False
            if raw_squashed is None:
                row = db.execute(
                    "SELECT text_path FROM sources.source_documents WHERE document_uid=%s",
                    (document_uid,),
                ).fetchone()
                raw = Path(row[0]).read_text(encoding="utf-8", errors="replace") if row else ""
                raw_squashed = squash(raw)
            return squash(quote) in raw_squashed

        def add_edge(target_node: str, edge_type: CanonicalEdgeType, quote: str, confidence: float = 0.75) -> None:
            node = citing_for(quote) or source_node
            try:
                repo.add_edge(
                    CanonicalEdge(
                        edge_id=f"edge:cats:{sha1('|'.join((node, target_node, edge_type.value, quote)))[:24]}",
                        source_node_id=node,
                        target_node_id=target_node,
                        edge_type=edge_type,
                        provenance=Provenance(
                            created_by="cats_llm_sweep",
                            creation_method=CreationMethod.LLM_EXTRACTOR,
                            source_id=external_id,
                            model_id="glm-5.3-flash",
                        ),
                        confidence=confidence,
                    )
                )
                counters["edges"] += 1
            except Exception:  # noqa: BLE001 — duplicate edge is fine
                pass

        def note_target(entry_title: str | None, article: str | None) -> tuple[str, str | None] | None:
            target = resolver.resolve(entry_title)
            if not target:
                if entry_title and len(entry_title.strip()) > 6:
                    key = normalize_legal_text(entry_title)
                    slot = missing_laws.setdefault(key, {"titles": set(), "articles": set()})
                    slot["titles"].add(entry_title.strip())
                    if article:
                        slot["articles"].add(article)
                return None
            stage_uid, canonical_id = target
            if canonical_id and article:
                number = norm_num(article)
                if number and not provision_exists(db, canonical_id, article):
                    missing_provisions[canonical_id].add(number)
            return target

        for entry in output.get("citations") or []:
            quote = entry.get("quote") or ""
            if not verified(quote):
                continue
            target = note_target(entry.get("target_title"), entry.get("article"))
            if not target or not target[1]:
                continue
            target_node = provision_exists(db, target[1], entry.get("article")) or target[1]
            add_edge(target_node, CanonicalEdgeType.CITES, quote)

        for entry in output.get("basis") or []:
            quote = entry.get("quote") or ""
            if not verified(quote):
                continue
            target = note_target(entry.get("target_title"), entry.get("article"))
            if not target or not target[1]:
                continue
            add_edge(target[1], CanonicalEdgeType.IMPLEMENTS, quote)

        for entry in output.get("effects") or []:
            quote = entry.get("quote") or ""
            etype = (entry.get("type") or "").strip().lower()
            if etype not in EFFECT_TYPES or not verified(quote):
                continue
            target = note_target(entry.get("target_title"), entry.get("provision_label"))
            if not target:
                continue
            stage_uid, canonical_id = target
            if etype in ("interpret", "conflict") and canonical_id:
                add_edge(canonical_id,
                         CanonicalEdgeType.INTERPRETS if etype == "interpret" else CanonicalEdgeType.CONFLICTS_WITH,
                         quote)
            if etype not in ("interpret", "conflict"):
                with db.cursor() as cur:
                    cur.execute(
                        """INSERT INTO sources.legal_effects
                               (affecting_document_uid, affected_instrument_uid, affected_provision_label,
                                effect_type, mode, scope, witness_quote, witness_checksum, confidence,
                                detected_by, review_status, created_by)
                           VALUES (%s,%s,%s,%s,'explicit',%s,%s,%s,0.75,'llm','candidate','cats_llm_sweep')
                           ON CONFLICT DO NOTHING""",
                        (document_uid, stage_uid, entry.get("provision_label"), etype,
                         entry.get("scope") or "partial", quote.strip(), sha1(quote.strip())),
                    )
                    counters["effects"] += cur.rowcount

        if counters["jobs"] % 100 == 0:
            repo.connection.commit()
            log(f"  {counters['jobs']} jobs | edges={counters['edges']} effects={counters['effects']} "
                f"missing_laws={len(missing_laws)}")

    repo.connection.commit()

    # merge the laws-corpus effects sweep's unresolved targets into the gap queue
    effects_report = Path("out/llm_effects_report.json")
    if effects_report.exists():
        try:
            data = json.loads(effects_report.read_text(encoding="utf-8"))
            for item in data.get("unresolved_targets") or []:
                title = (item.get("target_title") or "").strip()
                if len(title) > 6:
                    slot = missing_laws.setdefault(
                        normalize_legal_text(title), {"titles": set(), "articles": set()}
                    )
                    slot["titles"].add(title)
            log(f"merged unresolved targets from laws effects sweep -> {len(missing_laws)} gap titles")
        except Exception as exc:  # noqa: BLE001
            log(f"effects report merge failed: {exc}")

    report = {
        "jobs": counters["jobs"],
        "edges_added": counters["edges"],
        "effects_inserted": counters["effects"],
        "missing_law_titles": len(missing_laws),
        "missing_provision_instruments": {k: sorted(v) for k, v in missing_provisions.items()},
        "missing_laws": {k: {"titles": sorted(v["titles"]), "articles": sorted(v["articles"])}
                         for k, v in missing_laws.items()},
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"edges={counters['edges']} effects={counters['effects']} "
        f"missing_laws={len(missing_laws)} missing_provisions={sum(len(v) for v in missing_provisions.values())}")
    log(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
