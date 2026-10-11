#!/usr/bin/env python3
"""Classify every legal instrument into ONE subject domain (controlled vocab).

Writes canonical.legal_instruments.subject_domain. The domain vocabulary is a
fixed list so the value is usable as a graph hub (:LegalDomain) and as a
filter in the UI/agentic tools. LLM batches of titles, parallel workers,
resumable (skips instruments that already have a domain unless --force).

Default provider: arman / deepseek-v4.1-flash (override with env
LEGAL_AGENT_LLM_BASE_URL / LEGAL_AGENT_LLM_MODEL / LEGAL_AGENT_LLM_API_KEY).

Usage:
    .venv/bin/python scripts/classify_subject_domains.py [--workers 10] [--force]
"""

from __future__ import annotations

import argparse
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

BASE_URL = os.environ.get("LEGAL_AGENT_LLM_BASE_URL", "https://llm.armantechhub.ir/v1")
MODEL = os.environ.get("LEGAL_AGENT_LLM_MODEL", "deepseek-v4.1-flash")
API_TXT = Path("~/api.txt").expanduser()
PROMPT_VERSION = "domain-v1"
BATCH = 12
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

DOMAINS = [
    "مدنی", "کیفری", "آیین دادرسی", "خانواده", "تجارت و شرکت", "ثبت و اسناد",
    "کار و تأمین اجتماعی", "مالیاتی", "گمرک و تجارت خارجی", "بانکی و پولی",
    "بیمه", "بودجه و برنامه", "اداری و استخدامی", "انتخابات", "شهرداری و شهرسازی",
    "قضایی و دادگستری", "محیط زیست", "انرژی", "نفت و گاز", "کشاورزی و منابع طبیعی",
    "بهداشت و درمان", "آموزش و پژوهش", "دانش‌بنیان و فناوری", "فرهنگ و رسانه",
    "ورزش", "حمل‌ونقل", "مسکن و زمین", "ارتباطات و فناوری اطلاعات",
    "دفاعی و امنیتی", "اقتصاد و سرمایه‌گذاری", "صنعت و معدن", "رفاه و حمایت اجتماعی",
    "سایر",
]

SYSTEM_PROMPT = (
    "You classify Iranian legal instruments into ONE subject domain. "
    "Reply with strict JSON only: {\"items\":[{\"n\":1,\"domain\":\"...\"}]}. "
    "Pick the single closest domain from the allowed list; if none fits, use «سایر»."
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api_key() -> str:
    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if key:
        return key.strip()
    text = API_TXT.read_text(encoding="utf-8", errors="replace")
    block = re.search(r'"arman"\s*:\s*\{(.*?)\n\s*\}', text, re.DOTALL)
    scoped = block.group(1) if block else text
    match = re.search(r'"apiKey"\s*:\s*"([^"]+)"', scoped)
    if not match:
        raise SystemExit("no LEGAL_AGENT_LLM_API_KEY and no arman apiKey in ~/api.txt")
    return match.group(1).strip()


def extract_json(text: str):
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("no JSON")
    return json.loads(candidate[start:])


def llm_json(key: str, prompt: str, retries: int = 5) -> dict:
    payload = {
        "model": MODEL,
        "temperature": 0.0,
        "max_tokens": 2000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    if "z.ai" in BASE_URL:
        payload["thinking"] = {"type": "disabled"}
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read())
            return extract_json(data["choices"][0]["message"]["content"] or "")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"LLM call failed: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get(
        "LEGAL_AGENT_POSTGRES_DSN", "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    key = api_key()
    db = psycopg.connect(args.dsn, autocommit=True)
    condition = "" if args.force else "WHERE subject_domain IS NULL"
    rows = db.execute(
        f"SELECT instrument_id, title FROM canonical.legal_instruments {condition} ORDER BY instrument_id"
    ).fetchall()
    log(f"{len(rows)} instruments to classify (workers={args.workers}, model={MODEL})")

    batches = [rows[i : i + BATCH] for i in range(0, len(rows), BATCH)]

    def classify(batch):
        listing = "\n".join(f"{i+1}. {title}" for i, (_, title) in enumerate(batch))
        prompt = (
            "دامنه‌های مجاز: " + "، ".join(DOMAINS) + "\n\n"
            "برای هر قانون یک دامنه انتخاب کن:\n" + listing
        )
        payload = llm_json(key, prompt)
        out = {}
        for item in payload.get("items", []) if isinstance(payload, dict) else []:
            try:
                idx = int(item.get("n", 0)) - 1
                domain = str(item.get("domain", "")).strip()
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(batch) and domain in DOMAINS:
                out[batch[idx][0]] = domain
        return out

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(classify, b): b for b in batches}
        for future in as_completed(futures):
            try:
                mapping = future.result()
            except Exception as exc:  # noqa: BLE001
                log(f"batch FAILED: {str(exc)[:100]}")
                continue
            for instrument_id, domain in mapping.items():
                db.execute(
                    "UPDATE canonical.legal_instruments SET subject_domain=%s WHERE instrument_id=%s",
                    (domain, instrument_id),
                )
            done += len(mapping)
            log(f"{done}/{len(rows)} classified")
    db.execute("SELECT subject_domain, count(*) FROM canonical.legal_instruments GROUP BY 1 ORDER BY 2 DESC")
    log("domains: " + str(db.execute(
        "SELECT subject_domain, count(*) FROM canonical.legal_instruments GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
