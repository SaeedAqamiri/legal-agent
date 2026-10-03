#!/usr/bin/env python3
"""LLM enrichment (S4) for the hayula laws corpus — glm-5.3-flash via z.ai.

Stages (all LLM outputs are stored as candidates with witnesses, never silently trusted):
  E1  title-similarity verdicts: near-duplicate instrument titles are judged
      SAME_LAW vs DIFFERENT by the LLM (difflib is only the candidate finder)
  E2  instrument enrichment: doc_kind / tier / issuer / keywords per law
  E3  amendment & repeal events: laws titled «اصلاح/الحاق/تتمیم» are scanned for
      what they amend, with a verbatim witness quote -> sources.legal_effects (candidate)
  E4  the 20 no-provision files (lists/tables): extract repealed-law entries and
      any provisions -> legal_effects + extraction_jobs

Usage:
    .venv/bin/python scripts/llm_enrich_laws.py [--stages E1,E2,E3,E4] [--limit N]
Env: LEGAL_AGENT_LLM_API_KEY (falls back to the zai apiKey inside ~/api.txt),
     LEGAL_AGENT_POSTGRES_DSN, LAWS_DATA_DIR.
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
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.normalization import normalize_legal_text

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:55432/legal_agent"
DEFAULT_DATA_DIR = Path("~/hayula/laws-mcp/data").expanduser()
API_TXT = Path("~/api.txt").expanduser()
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"
PROMPT_VERSION = "laws-enrich-v1"
KIND_TO_TYPE = {
    "constitution": "constitution",
    "statute": "statute",
    "special_statute": "statute",
    "regulation": "regulation",
    "cabinet_approval": "regulation",
    "bylaw": "bylaw",
    "circular": "circular",
    "directive": "directive",
    "unknown": "statute",
}

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


def api_key() -> str:
    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if key:
        return key.strip()
    text = API_TXT.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'"zai".*?"apiKey":\s*"([^"]+)"', text, re.DOTALL)
    if not match:
        raise SystemExit("no LEGAL_AGENT_LLM_API_KEY and no zai apiKey in ~/api.txt")
    return match.group(1).strip()


def extract_json(text: str):
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        start = candidate.find("[")
    if start < 0:
        raise ValueError("no JSON in model output")
    return json.loads(candidate[start:])


class Chat:
    def __init__(self, key: str, workers: int = 6) -> None:
        self.key = key
        self.workers = workers
        self.calls = 0
        self.failures = 0

    def _once(self, prompt: str, system: str) -> str:
        payload = {
            "model": MODEL,
            "temperature": 0.1,
            "max_tokens": 8000,
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        with urllib.request.urlopen(request, timeout=180) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"] or ""

    def json_call(self, prompt: str, system: str) -> dict | list:
        for attempt in range(3):
            try:
                self.calls += 1
                return extract_json(self._once(prompt, system))
            except Exception as exc:
                self.failures += 1 if attempt == 2 else 0
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
                log(
                    f"    retry {attempt + 1} after {type(exc).__name__}: {str(exc)[:80]}"
                )
        raise AssertionError

    def map_all(self, items: list, build_prompt, system: str, desc: str):
        results, errors = [], []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self.json_call, build_prompt(item), system): item
                for item in items
            }
            for done, future in enumerate(as_completed(futures), 1):
                item = futures[future]
                try:
                    results.append((item, future.result()))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"item": str(item)[:80], "error": str(exc)[:120]})
                if done % 10 == 0 or done == len(items):
                    log(
                        f"  {desc}: {done}/{len(items)} (calls={self.calls}, fail={self.failures})"
                    )
        return results, errors


SYSTEM_ENRICHER = (
    "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان، بدون هیچ متن اضافه. "
    "اگر چیزی را نمی‌دانی null بگذار؛ حدس نزن."
)


def similar_pairs(titles: list[str], threshold: float = 0.75) -> list[tuple[str, str]]:
    pairs = []
    normalized = [(t, normalize_legal_text(t)) for t in titles]
    for i in range(len(normalized)):
        for j in range(i + 1, len(normalized)):
            ratio = difflib.SequenceMatcher(
                None, normalized[i][1], normalized[j][1]
            ).ratio()
            if ratio >= threshold:
                pairs.append((normalized[i][0], normalized[j][0]))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("LAWS_DATA_DIR", DEFAULT_DATA_DIR)),
    )
    parser.add_argument("--stages", default="E1,E2,E3,E4")
    parser.add_argument(
        "--limit", type=int, default=None, help="cap work items per stage (smoke tests)"
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--report", type=Path, default=Path("out/laws_llm_enrich_report.json")
    )
    args = parser.parse_args()
    stages = {s.strip().upper() for s in args.stages.split(",") if s.strip()}

    chat = Chat(api_key(), workers=args.workers)
    db = psycopg.connect(args.dsn, autocommit=True)
    report: dict = {
        "model": MODEL,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "stages": {},
    }

    with db.cursor() as cur:
        cur.execute(
            "SELECT instrument_id, title FROM canonical.legal_instruments ORDER BY instrument_id"
        )
        canonical_instruments = cur.fetchall()
        cur.execute(
            "SELECT canonical_key, canonical_title, tier FROM sources.instruments ORDER BY canonical_key"
        )
        catalog = cur.fetchall()
        cur.execute(
            """SELECT document_uid, external_id, title FROM sources.source_documents
               WHERE source_id='ekhtebar' AND text_path IS NOT NULL ORDER BY external_id"""
        )
        docs = cur.fetchall()

    log(
        f"catalog: {len(canonical_instruments)} canonical / {len(catalog)} catalog keys / {len(docs)} docs"
    )

    # ---------------------------------------------------------------- E1
    if "E1" in stages:
        pairs = similar_pairs([t for _, t in canonical_instruments])
        if args.limit:
            pairs = pairs[: args.limit]
        log(f"E1 title-similarity pairs to judge: {len(pairs)}")

        def batch_prompt(chunk: list[tuple[str, str]]) -> str:
            lines = "\n".join(
                f"{i + 1}. «{a}»  ↔  «{b}»" for i, (a, b) in enumerate(chunk)
            )
            return (
                "برای هر جفت عنوان قانون زیر بگو یک قانون واحدند یا دو قانون متفاوت. "
                "دقت کن: «قانون اصلاح X» با «قانون X» دو سند متفاوت‌اند (یکی اصلاح‌کننده دیگری است). "
                "نسخه‌های سال متفاوت بودجه/برنامه هم متفاوت‌اند. فقط سال یا عبارت الحاقی متفاوتِ یک قانون واحد = same.\n"
                'خروجی: {"verdicts":[{"n":1,"verdict":"same|different","reason":"..."}]}\n\n'
                + lines
            )

        chunks = [pairs[i : i + 8] for i in range(0, len(pairs), 8)]
        results, errors = chat.map_all(
            chunks, batch_prompt, SYSTEM_ENRICHER, "E1 pairs"
        )
        same: list[tuple[str, str, str]] = []
        diff: list[tuple[str, str]] = []
        invalid = 0
        for chunk, payload in results:
            verdicts = payload.get("verdicts", []) if isinstance(payload, dict) else []
            for verdict in verdicts:
                try:
                    idx = int(verdict.get("n", 0)) - 1
                    a, b = chunk[idx]
                except (IndexError, ValueError, TypeError):
                    invalid += 1
                    continue
                if str(verdict.get("verdict", "")).lower() == "same":
                    same.append((a, b, str(verdict.get("reason", ""))[:120]))
                else:
                    diff.append((a, b))
        for a, b in diff:
            with db.cursor() as cur:
                cur.execute(
                    """UPDATE sources.instruments SET external_ids = external_ids || %s
                       WHERE canonical_title IN (%s, %s)""",
                    (json.dumps({"llm_title_check": "different"}), a, b),
                )
        for a, b, reason in same:
            with db.cursor() as cur:
                cur.execute(
                    """UPDATE sources.instruments SET external_ids = external_ids || %s
                       WHERE canonical_title IN (%s, %s)""",
                    (
                        json.dumps(
                            {"llm_title_check": "same", "same_reason": reason},
                            ensure_ascii=False,
                        ),
                        a,
                        b,
                    ),
                )
        report["stages"]["E1"] = {
            "pairs": len(pairs),
            "same": len(same),
            "different": len(diff),
            "invalid": invalid,
            "errors": errors[:10],
        }
        log(
            f"E1 done: {len(same)} same / {len(diff)} different / {invalid} invalid / {len(errors)} failed calls"
        )

    # ---------------------------------------------------------------- E2
    if "E2" in stages:
        items = list(canonical_instruments)
        if args.limit:
            items = items[: args.limit]
        log(f"E2 enrich instruments: {len(items)}")

        def enrich_prompt(chunk: list[tuple[str, str]]) -> str:
            lines = "\n".join(f"{i + 1}. {t}" for i, (_, t) in enumerate(chunk))
            return (
                "برای هر قانون زیر این فیلدها را تعیین کن و دقیقاً به ترتیب شماره‌ها به همان تعداد پاسخ بده:\n"
                "kind: یکی از constitution|statute|special_statute|regulation|cabinet_approval|circular|directive|unknown\n"
                "tier: عدد 1..5 (1=قانون اساسی، 2=قوانین خاص/سیاست‌ها، 3=قانون عادی مصوب مجلس، 4=آیین‌نامه/تصویب‌نامه، 5=بخشنامه)\n"
                "issuer: مرجع وضع — فقط یکی از این مقادیر: «مجلس شورای اسلامی»، «مجلس خبرگان»، «شورای نگهبان»، "
                "«مجمع تشخیص مصلحت نظام»، «مقام رهبری»، «هیئت وزیران»، «شورای عالی امنیت ملی»، "
                "«شورای عالی انقلاب فرهنگی»، «دستگاه اجرایی». هر قانون عادی مصوب مجلس = «مجلس شورای اسلامی».\n"
                "keywords: حداکثر ۵ کلیدواژه موضوعی\n"
                'خروجی: {"items":[{"n":1,"kind":"...","tier":3,"issuer":"...","keywords":["..."]}]}\n\n'
                + lines
            )

        chunks = [items[i : i + 12] for i in range(0, len(items), 12)]
        results, errors = chat.map_all(
            chunks, enrich_prompt, SYSTEM_ENRICHER, "E2 instruments"
        )
        updated = 0
        for chunk, payload in results:
            for item in payload.get("items", []) if isinstance(payload, dict) else []:
                try:
                    instrument_id, title = chunk[int(item.get("n", 0)) - 1]
                except (IndexError, ValueError, TypeError):
                    continue
                kind = str(item.get("kind") or "unknown")
                tier = item.get("tier")
                issuer = item.get("issuer") or None
                with db.cursor() as cur:
                    cur.execute(
                        """UPDATE canonical.legal_instruments
                           SET instrument_type=%s, authority_level=%s, issuer=%s
                           WHERE instrument_id=%s""",
                        (
                            KIND_TO_TYPE.get(kind, "statute"),
                            str(tier) if tier else None,
                            issuer,
                            instrument_id,
                        ),
                    )
                    cur.execute(
                        """UPDATE sources.instruments SET instrument_type=%s, tier=%s, issuer=%s
                           WHERE canonical_title=%s""",
                        (kind, tier, issuer, title),
                    )
                updated += 1
        report["stages"]["E2"] = {"updated": updated, "errors": errors[:10]}
        log(f"E2 done: {updated} instruments updated")

    # ---------------------------------------------------------------- E3
    if "E3" in stages:
        amend_docs = [
            (uid, ext, title)
            for uid, ext, title in docs
            if re.search(r"اصلاح|الحاق|تتمیم", title)
        ]
        if args.limit:
            amend_docs = amend_docs[: args.limit]
        catalog_titles = [t for _, t, _ in catalog]
        log(f"E3 amendment laws to scan: {len(amend_docs)}")

        def resolve(hint: str) -> tuple[str | None, float]:
            best = difflib.get_close_matches(hint, catalog_titles, n=3, cutoff=0.55)
            if not best:
                return None, 0.0
            ratio = difflib.SequenceMatcher(
                None, normalize_legal_text(hint), normalize_legal_text(best[0])
            ).ratio()
            return best[0], ratio

        def resolve_with_llm(hint: str, options: list[str]) -> str | None:
            payload = chat.json_call(
                "عنوان هدف: «" + hint + "»\n"
                "کدام گزینه دقیقاً همان قانونی است که این عنوان به آن اشاره دارد؟ اگر هیچ‌کدام نیست null بده.\n"
                "گزینه‌ها: "
                + json.dumps(options, ensure_ascii=False)
                + '\nخروجی: {"pick":"<عنوان انتخابی یا null>"}',
                SYSTEM_ENRICHER,
            )
            pick = payload.get("pick") if isinstance(payload, dict) else None
            return pick if pick in options else None

        def amend_prompt(doc_title: str, text: str) -> str:
            return (
                f"متن زیر «{doc_title}» است — قانونی که قوانین دیگری را اصلاح/الحاق/تتمیم می‌کند.\n"
                "هر قانون هدفی که این متن تغییر می‌دهد را استخراج کن. برای هر هدف:\n"
                "target_title: نام دقیق قانون هدف همان‌طور که در متن آمده\n"
                "effect_type: amend|append|supplement|repeal (اصلاح|الحاق|تتمیم|نسخ)\n"
                "scope: total|partial\n"
                "quote: یک جمله عینی از همین متن که نشان می‌دهد کدام قانون را تغییر می‌دهد (نقل مستقیم)\n"
                "اگر متن چیزی را اصلاح نمی‌کند آرایه خالی بده.\n"
                'خروجی: {"targets":[{"target_title":"...","effect_type":"amend","scope":"partial","quote":"..."}]}\n\n'
                f"متن:\n{text[:9000]}"
            )

        def process_amend(item: tuple):
            _uid, ext, title = item
            md_path = args.data_dir / "laws" / f"{ext.split('#')[0]}.md"
            if not md_path.exists():
                return item, None
            text = md_path.read_text(encoding="utf-8", errors="replace")
            payload = chat.json_call(amend_prompt(title, text), SYSTEM_ENRICHER)
            input_checksum = hashlib.sha1(text[:9000].encode()).hexdigest()
            return item, payload, input_checksum

        results, errors = [], []
        with ThreadPoolExecutor(max_workers=chat.workers) as pool:
            futures = [pool.submit(process_amend, item) for item in amend_docs]
            for done, future in enumerate(as_completed(futures), 1):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append({"item": str(future)[:60], "error": str(exc)[:120]})
                if done % 10 == 0 or done == len(amend_docs):
                    log(
                        f"  E3 scan: {done}/{len(amend_docs)} (calls={chat.calls}, fail={chat.failures})"
                    )

        effects = 0
        llm_resolves = 0
        for ((uid, ext, title), payload, input_checksum) in results:
            if not isinstance(payload, dict):
                continue
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO sources.extraction_jobs
                           (document_uid, stage, status, model, prompt_version, output, finished_at)
                       VALUES (%s,'amendment_detection','succeeded',%s,%s,%s, now())""",
                    (
                        uid,
                        MODEL,
                        PROMPT_VERSION,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            for target in payload.get("targets", []):
                hint = str(target.get("target_title", "")).strip()
                quote = str(target.get("quote", "")).strip()
                if not hint or not quote:
                    continue
                matched, ratio = resolve(hint)
                if not matched:
                    continue
                if ratio < 0.8:
                    options = difflib.get_close_matches(
                        hint, catalog_titles, n=3, cutoff=0.55
                    )
                    picked = resolve_with_llm(hint, options)
                    if not picked:
                        continue
                    matched = picked
                    llm_resolves += 1
                effect_type = str(target.get("effect_type", "amend"))
                witness = hashlib.sha1(f"{uid}:{quote}".encode()).hexdigest()
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT instrument_uid FROM sources.instruments WHERE canonical_title=%s",
                        (matched,),
                    )
                    row = cur.fetchone()
                    affected_uid = row[0] if row else None
                    cur.execute(
                        """INSERT INTO sources.legal_effects
                               (affecting_document_uid, affected_instrument_uid, affected_provision_label,
                                effect_type, mode, scope, witness_quote, witness_page, witness_checksum,
                                confidence, detected_by, review_status)
                           VALUES (%s,%s,NULL,%s,'explicit',%s,%s,NULL,%s,%s,'llm','candidate')
                           ON CONFLICT DO NOTHING""",
                        (
                            uid,
                            affected_uid,
                            effect_type
                            if effect_type
                            in ("amend", "append", "supplement", "repeal")
                            else "amend",
                            "partial"
                            if str(target.get("scope")) == "partial"
                            else "total",
                            quote,
                            f"sha256:{witness}",
                            min(0.9, 0.5 + ratio / 2),
                        ),
                    )
                effects += 1
        report["stages"]["E3"] = {
            "docs": len(amend_docs),
            "effects": effects,
            "llm_resolves": llm_resolves,
            "errors": errors[:10],
        }
        log(f"E3 done: {effects} candidate effects")

    # ---------------------------------------------------------------- E4
    if "E4" in stages:
        # exactly the files whose canonical parse produced no provisions (from the ingest report)
        ingest_report_path = Path("out/laws_ingest_report.json")
        failed_slugs: set[str] = set()
        if ingest_report_path.exists():
            failed_slugs = {
                Path(entry["file"]).stem
                for entry in json.loads(
                    ingest_report_path.read_text(encoding="utf-8")
                ).get("parse_failures", [])
            }
        slug_to_doc = {ext.split("#")[0]: (uid, ext, title) for uid, ext, title in docs}
        candidates = [
            slug_to_doc[slug] for slug in sorted(failed_slugs) if slug in slug_to_doc
        ]
        if args.limit:
            candidates = candidates[: args.limit]
        log(
            f"E4 unstructured files: {len(candidates)} (from {len(failed_slugs)} parse failures)"
        )

        def e4_prompt(doc_title: str, text: str) -> str:
            return (
                f"سند «{doc_title}» ساختار ماده/تبصره استاندارد ندارد (فهرست، جدول یا تفسیر است).\n"
                "اگر فهرست قوانین منسوخ/ناسخ است، هر ردیف را استخراج کن: title (نام قانون منسوخ‌شده)، "
                "repealed_by (قانون یا مرجعی که آن را نسخ/نامعتبر کرده، اگر آمده)، date (تاریخ اگر آمده).\n"
                "اگر متن قابل تجزیه به ماده است هم provisions بده (number, label, text).\n"
                'خروجی: {"repealed":[{"title":"...","repealed_by":"...","date":"..."}],'
                '"provisions":[{"number":"1","label":"ماده ۱","text":"..."}]}\n\n'
                f"متن:\n{text[:9000]}"
            )

        def process_e4(item: tuple):
            _uid, ext, title = item
            md_path = args.data_dir / "laws" / f"{ext.split('#')[0]}.md"
            if not md_path.exists():
                return item, None
            text = md_path.read_text(encoding="utf-8", errors="replace")
            payload = chat.json_call(e4_prompt(title, text), SYSTEM_ENRICHER)
            input_checksum = hashlib.sha1(text[:9000].encode()).hexdigest()
            return item, payload, input_checksum

        results, errors = [], []
        with ThreadPoolExecutor(max_workers=chat.workers) as pool:
            futures = [pool.submit(process_e4, item) for item in candidates]
            for done, future in enumerate(as_completed(futures), 1):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append({"item": str(future)[:60], "error": str(exc)[:120]})
                if done % 5 == 0 or done == len(candidates):
                    log(
                        f"  E4 files: {done}/{len(candidates)} (calls={chat.calls}, fail={chat.failures})"
                    )

        repeal_rows, provision_rows = 0, 0
        catalog_titles = [t for _, t, _ in catalog]
        for ((uid, ext, title), payload, input_checksum) in results:
            if not isinstance(payload, dict):
                continue
            provisions = payload.get("provisions") or []
            repealed = payload.get("repealed") or []
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO sources.extraction_jobs
                           (document_uid, stage, status, model, prompt_version, output, review_status, finished_at)
                       VALUES (%s,'structure','succeeded',%s,%s,%s,'candidate', now())
                       ON CONFLICT DO NOTHING""",
                    (
                        uid,
                        MODEL,
                        PROMPT_VERSION,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            for entry in repealed:
                hint = str(entry.get("title", "")).strip()
                if not hint:
                    continue
                matched = difflib.get_close_matches(
                    hint, catalog_titles, n=1, cutoff=0.55
                )
                affected_uid = None
                if matched:
                    with db.cursor() as cur:
                        cur.execute(
                            "SELECT instrument_uid FROM sources.instruments WHERE canonical_title=%s",
                            (matched[0],),
                        )
                        row = cur.fetchone()
                        affected_uid = row[0] if row else None
                witness = hashlib.sha1(f"{uid}:{hint}".encode()).hexdigest()
                with db.cursor() as cur:
                    cur.execute(
                        """INSERT INTO sources.legal_effects
                               (affecting_document_uid, affected_instrument_uid, effect_type, mode, scope,
                                witness_quote, witness_checksum, confidence, detected_by, review_status)
                           VALUES (%s,%s,'repeal','explicit','total',%s,%s,0.7,'llm','candidate')
                           ON CONFLICT DO NOTHING""",
                        (
                            uid,
                            affected_uid,
                            hint
                            + (
                                f" — توسط: {entry.get('repealed_by')}"
                                if entry.get("repealed_by")
                                else ""
                            ),
                            f"sha256:{witness}",
                        ),
                    )
                repeal_rows += 1
            provision_rows += len(provisions)
        report["stages"]["E4"] = {
            "files": len(candidates),
            "repeal_candidates": repeal_rows,
            "provisions_extracted": provision_rows,
            "errors": errors[:10],
        }
        log(f"E4 done: {repeal_rows} repeal candidates, {provision_rows} provisions")

    report["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
    report["total_calls"] = chat.calls
    report["total_failures"] = chat.failures
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE — report: {args.report} (calls={chat.calls}, failures={chat.failures})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
