#!/usr/bin/env python3
"""LLM extraction of amendment/repeal effects from statute markdown texts.

LLM-first design (per project decision: prefer recall over regex pre-filters
when budget allows): every statute's FULL text is chunked (~7k chars, small
overlap) and every chunk is sent to the model — no keyword gating that could
drop information. Events without a verbatim witness quote in the source text
are dropped (schema golden rule). Targets are resolved deterministically
against sources.instruments (normalized match, difflib >= 0.87); unresolved
targets stay in the JSON report. Rows land in sources.legal_effects as
review_status='candidate', detected_by='llm', mode='explicit'.

Usage:
    LEGAL_AGENT_LLM_API_KEY=... LAWS_DATA_DIR=~/laws-mcp/data \
        LEGAL_AGENT_POSTGRES_DSN=... .venv/bin/python scripts/llm_extract_effects.py
"""

from __future__ import annotations

import difflib
import json
import os
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from publish_relations import DEFAULT_DSN, llm_json, log

from legal_agent_core.ingestion.normalization import normalize_legal_text

DATA_DIR = Path(os.environ.get("LAWS_DATA_DIR", str(Path.home() / "laws-mcp/data")))
WORKERS = 6
EFFECT_TYPES = (
    "amend",
    "append",
    "supplement",
    "repeal",
    "replace",
    "suspend",
    "restore",
    "annul",
)
SYSTEM = "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان."
PROMPT_TMPL = (
    "عنوان قانون فعلی: «{title}» (تکه {index} از {total})\n"
    "از متن تکه زیر همه اعلام‌های تغییر حقوقی نسبت به قانون/مقرره دیگری را استخراج کن "
    "(اصلاح، الحاق، تتمیم، متمم، نسخ، لغو، ابطال، توقف اجرا، جایگزینی، ابقاء و هر عبارت معادل دیگر).\n"
    "قواعد:\n"
    "- فقط تغییرات صریح و مکتوب در همین تکه؛ حدس نزن. اگر چیزی نیست، events خالی بده.\n"
    "- «اصلاح قانون X» یعنی همین سند، X را اصلاح می‌کند (سندِ اصلاح‌کننده است، خود X نیست).\n"
    "- effect_type یکی از: amend, append, supplement, repeal, replace, suspend, restore, annul.\n"
    "- scope: total (نسخ/اصلاح کلی) یا partial (برخی مواد) یا null.\n"
    "- quote: عین همان عبارت از تکه زیر، کوتاه (حداکثر یک جمله) — باید حرف‌به‌حرف در متن باشد.\n"
    '- خروجی: {{"events":[{{"effect_type":"...","target_title":"عنوان دقیق قانون هدف","provisions":["۵","۷"] یا null,"scope":"...","quote":"..."}}]}}\n\n'
    "متن تکه:\n{body}"
)


def slug_of(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return unquote(tail)


def split_for_fallback(text: str, parts: int = 6) -> list[str]:
    size = max(1, -(-len(text) // parts))
    return [text[i : i + size] for i in range(0, len(text), size)]


def quote_verbatim(quote: str, text: str) -> bool:
    needle = " ".join(quote.split())
    if len(needle) < 12:
        return False
    return needle in " ".join(text.split())


def main() -> int:
    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if not key:
        print("LEGAL_AGENT_LLM_API_KEY is required")
        return 2
    dsn = os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)

    entries = json.loads((DATA_DIR / "index.json").read_text(encoding="utf-8"))
    from ingest_laws_data import classify

    db = psycopg.connect(dsn, autocommit=True)
    catalog: dict[str, str] = {}
    doc_uids: dict[str, tuple[str, str]] = {}
    with db.cursor() as cur:
        cur.execute("SELECT canonical_key, instrument_uid FROM sources.instruments")
        catalog = dict(cur.fetchall())
        cur.execute(
            """SELECT external_id, document_uid, instrument_uid
               FROM sources.source_documents WHERE source_id='ekhtebar'"""
        )
        for external_id, document_uid, instrument_uid in cur.fetchall():
            doc_uids[external_id] = (str(document_uid), str(instrument_uid))

    jobs: list[dict] = []
    done_path = Path("out/effects_done_slugs.txt")
    done_slugs: set[str] = set()
    if done_path.exists():
        done_slugs = set(done_path.read_text(encoding="utf-8").split())
    done_file = done_path.open("a", encoding="utf-8")
    for entry in entries:
        kind, _ = classify(entry.get("title", ""))
        if kind != "statute":
            continue
        slug = slug_of(entry.get("url", ""))
        if slug in done_slugs:
            continue
        md_path = DATA_DIR / "laws" / f"{slug}.md"
        if not md_path.exists() or slug not in doc_uids:
            continue
        text = md_path.read_text(encoding="utf-8", errors="replace")
        document_uid, instrument_uid = doc_uids[slug]
        jobs.append(
            {
                "slug": slug,
                "title": entry.get("title", ""),
                "text": text,
                "document_uid": document_uid,
                "instrument_uid": instrument_uid,
            }
        )
    log(f"statutes: {len(jobs)} (one LLM call per law, fallback splits only on context errors)")

    def resolve(title: str, self_uid: str) -> str | None:
        norm = normalize_legal_text(title)
        if not norm:
            return None
        if catalog.get(norm) and catalog[norm] != self_uid:
            return catalog[norm]
        scored = sorted(
            catalog.items(),
            key=lambda item: difflib.SequenceMatcher(None, norm, item[0]).ratio(),
            reverse=True,
        )
        if scored and scored[0][1] != self_uid:
            ratio = difflib.SequenceMatcher(None, norm, scored[0][0]).ratio()
            if ratio >= 0.87:
                return scored[0][1]
        return None

    stats = {"calls": 0, "events": 0, "inserted": 0, "bad_quote": 0, "unresolved": 0}
    unresolved_report: list[dict] = []

    def process(job: dict) -> None:
        def ask(body: str, index: int, total: int):
            return llm_json(
                key,
                PROMPT_TMPL.format(title=job["title"], index=index, total=total, body=body),
                SYSTEM,
                max_tokens=8000,
            )

        try:
            payload = ask(job["text"], 1, 1)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                log(f"  !! {job['slug']}: HTTP {exc.code}")
                return
            # context overflow (giant law) — retry the same law in a few
            # coarse parts so nothing is silently dropped
            events = []
            for i, part in enumerate(split_for_fallback(job["text"]), 1):
                try:
                    partial = ask(part, i, 6)
                except Exception as part_exc:  # noqa: BLE001
                    log(f"  !! {job['slug']} part{i}: {type(part_exc).__name__}")
                    continue
                if isinstance(partial, dict):
                    events.extend(partial.get("events", []))
            payload = {"events": events}
        except Exception as exc:  # noqa: BLE001
            log(f"  !! {job['slug']}: {type(exc).__name__}: {str(exc)[:80]}")
            return
        stats["calls"] += 1
        for event in payload.get("events", []) if isinstance(payload, dict) else []:
            try:
                effect_type = str(event.get("effect_type", "")).strip()
                quote = str(event.get("quote", "")).strip()
                target_title = str(event.get("target_title", "")).strip()
            except Exception:  # noqa: BLE001, S112 — malformed LLM row
                continue
            if effect_type not in EFFECT_TYPES:
                continue
            if not quote or not target_title:
                continue
            if not quote_verbatim(quote, job["text"]):
                stats["bad_quote"] += 1
                continue
            scope = str(event.get("scope") or "").strip().lower()
            scope = scope if scope in {"total", "partial"} else None
            provisions = event.get("provisions") or []
            labels = [str(p).strip() for p in provisions if str(p).strip()][:8] or [None]
            target_uid = resolve(target_title, job["instrument_uid"])
            if target_uid is None:
                stats["unresolved"] += 1
                unresolved_report.append(
                    {"slug": job["slug"], "target_title": target_title, "quote": quote}
                )
            confidence = 0.8
            with db.cursor() as cur:
                for label in labels:
                    cur.execute(
                        """SELECT 1 FROM sources.legal_effects
                           WHERE affecting_document_uid=%s AND effect_type=%s
                             AND affected_instrument_uid IS NOT DISTINCT FROM %s
                             AND affected_provision_label IS NOT DISTINCT FROM %s""",
                        (job["document_uid"], effect_type, target_uid, label),
                    )
                    if cur.fetchone():
                        continue
                    cur.execute(
                        """INSERT INTO sources.legal_effects (
                               affecting_document_uid, affected_instrument_uid,
                               affected_provision_label, effect_type, mode, scope,
                               witness_quote, confidence, detected_by, review_status, created_by
                           ) VALUES (%s,%s,%s,%s,'explicit',%s,%s,%s,'llm','candidate','llm-effects-v2')""",
                        (
                            job["document_uid"],
                            target_uid,
                            label,
                            effect_type,
                            scope,
                            quote[:600],
                            confidence,
                        ),
                    )
                    stats["inserted"] += 1
            stats["events"] += 1

    total_jobs = len(jobs)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(process, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            future.result()
            done_file.write(job["slug"] + "\n")
            done_file.flush()
            if done % 20 == 0 or done == total_jobs:
                log(f"  {done}/{total_jobs} laws done (inserted={stats['inserted']})")
    done_file.close()

    log(
        f"llm calls: {stats['calls']}, events kept: {stats['events']}, "
        f"rows inserted: {stats['inserted']}, bad quotes: {stats['bad_quote']}, "
        f"unresolved targets: {stats['unresolved']}"
    )
    report_path = Path("out/llm_effects_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            stats | {"unresolved_targets": unresolved_report}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
