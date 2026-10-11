#!/usr/bin/env python3
"""Paragraph summaries (3-4 sentences) for judgments and advisory opinions.

Rewrites sources.source_documents.metadata->>'summary' for the cats corpus
(doc_kind judgment/unknown) from the one-line summaries into a Persian
paragraph: what the ruling/opinion decided, on which legal question, and the
key holding. Resumable via metadata->>'summary_prompt_version'. The FalkorDB
mirror reads metadata->>'summary' on the next sync.

Usage:
    LEGAL_AGENT_LLM_API_KEY=... LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/summarize_source_docs.py [--force] [--workers 10]
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
PROMPT_VERSION = "source-doc-paragraph-v1"
MAX_TOKENS = 1200
TEXT_CAP = 4000
API_TXT = Path("~/api.txt").expanduser()
KEY_FILE = Path("/tmp/opencode/llm_key")
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

SYSTEM_PROMPT = (
    "تو کارشناس حقوق ایران هستی. برای یک رأی قضایی یا نظریه مشورتی یک خلاصه‌ی "
    "پاراگرافی ۳ تا ۴ جمله‌ای به فارسی روان بنویس که توضیح دهد: (۱) موضوع و "
    "پرسش حقوقی پرونده یا استعلام چه بوده، (۲) رأی/نظریه چه حکمی داده، (۳) "
    "نکته یا استدلال حقوقی کلیدی چیست. از کپی‌کردن عین متن خودداری کن. "
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
        "max_tokens": MAX_TOKENS,
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
                return summary[:1200]
            raise ValueError("empty summary")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"LLM failed: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "LEGAL_AGENT_POSTGRES_DSN",
            "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent",
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="re-summarize docs that already have the paragraph summary"
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    key = api_key()

    with psycopg.connect(args.dsn, autocommit=True) as db:
        condition = "" if args.force else (
            "AND COALESCE(metadata->>'summary_prompt_version','') <> %s"
        )
        params = () if args.force else (PROMPT_VERSION,)
        rows = db.execute(
            f"""SELECT document_uid, title, text_path, metadata->>'summary'
                  FROM sources.source_documents
                 WHERE source_id = 'cats' AND doc_kind IN ('judgment','unknown')
                   AND text_path IS NOT NULL {condition}
                 ORDER BY document_uid""",
            params,
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        log(f"{len(rows)} documents to summarize (workers={args.workers})")

        def work(row):
            uid, title, text_path, _old = row
            try:
                text = Path(text_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return uid, None
            prompt = (
                f"عنوان: {title or '—'}\n"
                f"متن: {text[:TEXT_CAP]}"
            )
            try:
                return uid, llm_summary(key, prompt)
            except Exception as exc:  # noqa: BLE001
                log(f"{uid} FAILED: {str(exc)[:100]}")
                return uid, None

        done = failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(work, row) for row in rows]
            for future in as_completed(futures):
                uid, summary = future.result()
                if not summary:
                    failed += 1
                    continue
                db.execute(
                    """UPDATE sources.source_documents
                          SET metadata = COALESCE(metadata,'{}'::jsonb)
                              || jsonb_build_object('summary', %s::text,
                                                    'summary_prompt_version', %s::text)
                        WHERE document_uid = %s::uuid""",
                    (summary, PROMPT_VERSION, uid),
                )
                done += 1
                if done % 50 == 0:
                    log(f"{done}/{len(rows)} done")
        log(f"finished: {done} summarized, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
