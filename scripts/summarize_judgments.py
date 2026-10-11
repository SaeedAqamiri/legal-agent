#!/usr/bin/env python3
"""One-line Persian summary per judicial document (رأی / نظریه مشورتی).

Reads each staged cats document's markdown, asks the LLM for a one-line
summary, and stores it in sources.source_documents.metadata->>'summary'.
The FalkorDB mirror exposes it as the `summary` property of (:Judgment) /
(:AdvisoryOpinion). Resumable (skips docs that already have a summary).

Usage:
    .venv/bin/python scripts/summarize_judgments.py [--workers 8] [--limit N] [--force]
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
PROMPT_VERSION = "judgment-summary-v1"
MAX_CHARS = 6000
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

SYSTEM_PROMPT = (
    "تو کارشناس حقوق ایران هستی. برای یک رأی/نظریه حقوقی، فقط و فقط یک خلاصه‌ی "
    "یک‌خطی (حداکثر ~۲۵ کلمه) بنویس که موضوع و نتیجه/حکم اصلی آن را بگوید. "
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
        "max_tokens": 800,
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
            parsed = extract_json(data["choices"][0]["message"]["content"] or "")
            summary = str(parsed.get("summary", "")).strip()
            if summary:
                return summary[:400]
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
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    key = api_key()
    db = psycopg.connect(args.dsn, autocommit=True)
    condition = "" if args.force else "AND metadata->>'summary' IS NULL"
    rows = db.execute(
        f"""SELECT document_uid, title, text_path
              FROM sources.source_documents
             WHERE source_id='cats' AND doc_kind IN ('judgment','unknown')
               AND text_path IS NOT NULL {condition}
             ORDER BY document_uid"""
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    log(f"{len(rows)} judicial docs to summarize (model={MODEL}, workers={args.workers})")

    def work(row):
        document_uid, title, text_path = row
        path = Path(text_path)
        if not path.exists():
            raise FileNotFoundError(text_path)
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS]
        prompt = f"عنوان: {title}\n\nمتن:\n{text}"
        return document_uid, llm_summary(key, prompt)

    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for future in as_completed(futures):
            try:
                document_uid, summary = future.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed % 20 == 0:
                    log(f"failures so far: {failed} (last: {str(exc)[:80]})")
                continue
            db.execute(
                """UPDATE sources.source_documents
                      SET metadata = metadata || jsonb_build_object(
                          'summary', %s::text,
                          'summary_model', %s::text,
                          'summary_prompt', %s::text)
                    WHERE document_uid = %s""",
                (summary, MODEL, PROMPT_VERSION, document_uid),
            )
            done += 1
            if done % 100 == 0:
                log(f"{done}/{len(rows)} done")
    log(f"finished: {done} summarized, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
