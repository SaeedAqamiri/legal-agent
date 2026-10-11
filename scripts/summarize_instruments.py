#!/usr/bin/env python3
"""One-line Persian summary per legal instrument (PageIndex-style).

For every instrument, one LLM call summarizes what the law regulates, using
its title + subject domain + the labels/summaries of its first sections.
Stored in canonical.instrument_summaries; the FalkorDB mirror exposes it as
the `summary` property of (:LegalInstrument). Resumable (skips done rows).

Usage:
    .venv/bin/python scripts/summarize_instruments.py [--workers 8] [--force]
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

BASE_URL = os.environ.get("LEGAL_AGENT_LLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
MODEL = os.environ.get("LEGAL_AGENT_LLM_MODEL", "glm-5.3-flash")
API_TXT = Path("~/api.txt").expanduser()
KEY_FILE = Path("/tmp/opencode/llm_key")
PROMPT_VERSION = "instrument-summary-v2-paragraph"
#: instruments shorter than this many words keep their own text as the summary
MIN_WORDS = 250
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

SYSTEM_PROMPT = (
    "تو کارشناس حقوق ایران هستی. برای یک قانون یا مقرره یک خلاصه‌ی پاراگرافی "
    "۳ تا ۴ جمله‌ای بنویس که توضیح دهد: (۱) این قانون چه چیزی را مقرر می‌کند و "
    "هدف آن چیست، (۲) چه اشخاص یا نهادهایی را مشمول می‌گیرد، (۳) سازوکار یا "
    "تکلیف کلیدی آن چیست. زبان فارسی روان و مستقیم، بدون کپی‌کردن جمله‌ی عنوان. "
    'خروجی فقط JSON: {"summary":"..."}'
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api_key() -> str:
    if os.environ.get("LEGAL_AGENT_LLM_API_KEY"):
        return os.environ["LEGAL_AGENT_LLM_API_KEY"].strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    text = API_TXT.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'"apiKey"\s*:\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("no LLM api key found")
    return m.group(1).strip()


def extract_json(text: str) -> dict:
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("no JSON")
    return json.loads(candidate[start:])


def llm_summary(key: str, prompt: str, retries: int = 5) -> str:
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    if "z.ai" in BASE_URL:
        # reasoning on medium effort (user spec: never disabled, never max)
        payload["thinking"] = {"type": "enabled", "effort": "medium"}
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
            parsed = extract_json(data["choices"][0]["message"]["content"] or "")
            summary = str(parsed.get("summary", "")).strip()
            if summary:
                return summary[:1200]
            raise ValueError("empty summary")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"LLM failed: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get(
        "LEGAL_AGENT_POSTGRES_DSN", "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--instrument", default=None, help="summarize only this instrument_id")
    args = parser.parse_args()

    key = api_key()
    db = psycopg.connect(args.dsn, autocommit=True)
    if args.instrument:
        # a targeted re-run always regenerates the listed instruments' summaries
        ids = [x.strip() for x in args.instrument.split(",") if x.strip()]
        condition = "AND i.instrument_id = ANY(%s)"
        params: tuple = (ids,)
    else:
        condition = "" if args.force else (
            "AND NOT EXISTS (SELECT 1 FROM canonical.instrument_summaries s "
            "WHERE s.instrument_id = i.instrument_id)"
        )
        params = ()
    rows = db.execute(
        f"""SELECT i.instrument_id, i.title, COALESCE(i.subject_domain,''),
                   COALESCE(i.authority_level,''),
                   (SELECT string_agg(ps.summary, ' | ')
                      FROM canonical.provision_summaries ps
                     WHERE ps.instrument_id = i.instrument_id),
                   (SELECT string_agg(COALESCE(pv.normalized_text, pv.text, ''), ' ')
                      FROM canonical.provisions p
                      LEFT JOIN LATERAL (
                           SELECT normalized_text, text
                             FROM canonical.provision_versions v
                            WHERE v.provision_id = p.provision_id
                            ORDER BY v.provision_version_id LIMIT 1
                      ) pv ON true
                     WHERE p.instrument_id = i.instrument_id)
              FROM canonical.legal_instruments i
             WHERE TRUE {condition}
             ORDER BY i.instrument_id""",
        params,
    ).fetchall()
    log(f"{len(rows)} instruments to summarize (model={MODEL}, workers={args.workers})")

    def build_prompt(title, domain, tier, prov_summaries):
        lines = [f"عنوان قانون: {title}"]
        if domain:
            lines.append(f"دامنه موضوعی: {domain}")
        if tier:
            lines.append(f"لایه: {tier}")
        if prov_summaries:
            lines.append("خلاصه‌ی بخش‌ها: " + prov_summaries[:3000])
        return "\n".join(lines)

    def work(row):
        instrument_id, title, domain, tier, prov_summaries, full_text = row
        full_text = (full_text or "").strip()
        # short instruments: their own text IS the summary (no LLM call)
        if full_text and len(full_text.split()) < MIN_WORDS:
            return instrument_id, full_text[:4000]
        return instrument_id, llm_summary(key, build_prompt(title, domain, tier, prov_summaries))

    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for future in as_completed(futures):
            try:
                instrument_id, summary = future.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log(f"FAILED: {str(exc)[:100]}")
                continue
            db.execute(
                """INSERT INTO canonical.instrument_summaries
                       (instrument_id, summary, model, prompt_version)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (instrument_id) DO UPDATE
                   SET summary = EXCLUDED.summary, model = EXCLUDED.model,
                       prompt_version = EXCLUDED.prompt_version, created_at = now()""",
                (instrument_id, summary, MODEL, PROMPT_VERSION),
            )
            done += 1
            if done % 25 == 0:
                log(f"{done}/{len(rows)} done")
    log(f"finished: {done} summarized, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
