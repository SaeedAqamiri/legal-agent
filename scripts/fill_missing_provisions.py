#!/usr/bin/env python3
"""Fill missing provisions (ماده/تبصره) by reading the whole law with the LLM.

Phase 3, step 2. Input: out/cats_gap_report.json (missing_provision_instruments
from resolve_cats_relations.py). For every instrument with missing article
numbers:

  1. locate its text (sources.source_documents.text_path)
  2. one LLM call with the WHOLE text asks for the requested ماده + their
     تبصره‌ها, verbatim
  3. every returned text is verified as a substring of the source text
  4. provisions + versions + spans are inserted under the instrument's latest
     document_version using the pipeline's stable-id convention

Idempotent: existing provisions are skipped (checked before insert).

Usage:
    LEGAL_AGENT_LLM_API_KEY=... LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/fill_missing_provisions.py [--report PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.canonical import (
    CreationMethod,
    DocumentStatus,
    Provision,
    ProvisionType,
    ProvisionVersion,
    SourceSpan,
)
from legal_agent_core.ingestion.normalization import normalize_legal_text, normalize_number
from legal_agent_core.ingestion.pipeline import stable_id

BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"

SYSTEM_PROMPT = """تو یک استخراج‌کننده دقیق متن قوانین هستی. متن کامل یک قانون/آیین‌نامه و فهرستی از شماره ماده‌های موردنیاز به تو داده می‌شود.
برای هر شماره درخواستی، متن کامل و عینی آن ماده را از قانون استخراج کن. تبصره آن ماده (اگر دارد) را هم به‌صورت entry جدا با kind="note" بده.
فقط JSON بده. اگر ماده‌ای در متن وجود ندارد، برای آن nothing برنگردان (ردش کن).

ساختار خروجی:
{"provisions": [{"number": "12", "kind": "article", "label": "ماده ۱۲", "text": "متن کامل ماده عیناً از قانون"},
                 {"number": "12", "kind": "note", "label": "تبصره ماده ۱۲", "text": "متن تبصره عیناً"}]}"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def extract_json(text: str):
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    return json.loads(candidate[start:])


def llm(key: str, prompt: str, retries: int = 6) -> dict:
    payload = {
        "model": MODEL, "temperature": 0.1, "max_tokens": 8000,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
    }
    import urllib.error
    import urllib.request

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
            if attempt == retries - 1:
                raise
            time.sleep(40 if exc.code == 429 else 5 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError


def latest_version(db, canonical_id: str):
    return db.execute(
        """SELECT dv.document_version_id, dv.source_document_id, sd.text_path
           FROM canonical.document_versions dv
           JOIN canonical.source_documents cs ON cs.source_document_id = dv.source_document_id
           LEFT JOIN sources.source_documents sd ON sd.text_checksum = cs.checksum
           WHERE dv.instrument_id=%s
           ORDER BY dv.effective_from DESC NULLS LAST LIMIT 1""",
        (canonical_id,),
    ).fetchone()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN",
                                                        "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--gap-report", default=Path("out/cats_gap_report.json"), type=Path)
    parser.add_argument("--out", default=Path("out/provisions_filled.json"), type=Path)
    args = parser.parse_args()

    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY") or (
        Path("/tmp/opencode/llm_key").read_text().strip() if Path("/tmp/opencode/llm_key").exists() else ""
    )
    if not key:
        parser.error("LEGAL_AGENT_LLM_API_KEY missing")

    gap = json.loads(args.gap_report.read_text(encoding="utf-8"))
    targets = gap.get("missing_provision_instruments") or {}
    log(f"instruments with missing provisions: {len(targets)}")

    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    db = psycopg.connect(args.dsn, autocommit=True)

    results = {"filled": 0, "not_found": [], "failed": [], "verified": 0}
    for canonical_id, numbers in targets.items():
        title_row = db.execute(
            "SELECT title FROM canonical.legal_instruments WHERE instrument_id=%s", (canonical_id,)
        ).fetchone()
        version = latest_version(db, canonical_id)
        if not title_row or not version or not version[2] or not Path(version[2]).exists():
            results["failed"].append({"instrument": canonical_id, "reason": "no text_path"})
            continue
        title, text_path = title_row[0], version[2]
        raw = Path(text_path).read_text(encoding="utf-8", errors="replace")
        raw_sq = squash(raw)

        still_missing = [
            num for num in numbers
            if not db.execute(
                """SELECT 1 FROM canonical.provisions
                   WHERE instrument_id=%s AND number=%s AND provision_type='article' LIMIT 1""",
                (canonical_id, num),
            ).fetchone()
        ]
        if not still_missing:
            continue
        log(f"{title[:60]} — missing: {still_missing}")

        try:
            out = llm(key, f"عنوان قانون: {title}\n\nماده‌های موردنیاز: {', '.join(still_missing)}\n\nمتن کامل:\n{raw}")
        except Exception as exc:  # noqa: BLE001
            results["failed"].append({"instrument": canonical_id, "error": str(exc)[:160]})
            continue

        document_version_id, source_document_id = version[0], version[1]
        for entry in out.get("provisions") or []:
            number = str(entry.get("number", "")).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
            kind = entry.get("kind", "article")
            text = entry.get("text") or ""
            if not number or not text.strip():
                continue
            if squash(text) not in raw_sq:
                continue  # golden rule: verbatim or dropped
            results["verified"] += 1
            ptype = ProvisionType.NOTE if kind == "note" else ProvisionType.ARTICLE
            segment = ("note" if kind == "note" else "article") + f":{number}"
            provision_id = stable_id("prov", canonical_id, segment)
            existing = db.execute(
                "SELECT 1 FROM canonical.provisions WHERE provision_id=%s", (provision_id,)
            ).fetchone()
            if existing:
                continue
            normalized = normalize_legal_text(text)
            provision_version_id = stable_id(
                "provv", provision_id, document_version_id, normalized, None, None
            )
            span_id = stable_id("span", provision_version_id, 1, None, None, text)
            try:
                repo.add_provision(Provision(
                    provision_id=provision_id, instrument_id=canonical_id, provision_type=ptype,
                    number=number if kind != "note" else None, label=entry.get("label") or f"ماده {number}",
                    title=None, parent_provision_id=None, ordinal=0, depth=0,
                ))
                repo.add_provision_version(ProvisionVersion(
                    provision_version_id=provision_version_id, provision_id=provision_id,
                    document_version_id=document_version_id, text=text, normalized_text=normalized,
                    effective_from=None, effective_to=None, status=DocumentStatus.EFFECTIVE,
                    created_from=f"llm:{MODEL}",
                ))
                repo.add_source_span(SourceSpan(
                    source_span_id=span_id, source_document_id=source_document_id,
                    document_version_id=document_version_id, provision_version_id=provision_version_id,
                    page_number=1, bbox=None, char_start=None, char_end=None, raw_text=text,
                ))
                results["filled"] += 1
            except Exception as exc:  # noqa: BLE001
                repo.connection.rollback()
                results["failed"].append({"instrument": canonical_id, "number": number, "error": str(exc)[:120]})
        results["not_found"].extend(
            {"instrument": canonical_id, "number": n}
            for n in still_missing
            if not any(str(e.get("number", "")).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")) == n
                       for e in (out.get("provisions") or []))
        )
        repo.connection.commit()

    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    log(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in results.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
