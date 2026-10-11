#!/usr/bin/env python3
"""Paragraph summaries (3-4 sentences) for ALL provisions, per instrument.

One whole-instrument LLM call per batch of provisions (label + text excerpt),
producing a Persian paragraph per provision: what the provision regulates,
who it binds / what rights it grants, and any key exceptions. Results go to
canonical.provision_summaries (same table the container summarizer uses, so
the FalkorDB mirror and the /graph API pick them up unchanged). Resumable:
--force re-summarizes instruments that already have provision summaries.

Usage:
    LEGAL_AGENT_LLM_API_KEY=... LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/summarize_provisions.py [--force] [--workers 10]
"""

from __future__ import annotations

import argparse
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

BASE_URL = os.environ.get("LEGAL_AGENT_LLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
MODEL = os.environ.get("LEGAL_AGENT_LLM_MODEL", "glm-5.3-flash")
#: fallback provider (avalai) used while the primary keeps returning 429
FALLBACK_BASE_URL = os.environ.get("LEGAL_AGENT_FALLBACK_BASE_URL", "https://api.avalai.ir/v1")
FALLBACK_MODEL = os.environ.get("LEGAL_AGENT_FALLBACK_MODEL", "deepseek-v4.1-flash")
FALLBACK_COOLDOWN = int(os.environ.get("LEGAL_AGENT_FALLBACK_COOLDOWN", "300"))
PROMPT_VERSION = "provision-paragraph-v1"
MAX_TOKENS = 16000
#: provisions per LLM call — keeps output under the token budget
BATCH_SIZE = 25
API_TXT = Path("~/api.txt").expanduser()
KEY_FILE = Path("/tmp/opencode/llm_key")
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

#: primary provider is used by default; on sustained 429s we switch to the
#: fallback for FALLBACK_COOLDOWN seconds, then probe the primary again.
_fallback_until = 0.0
_provider_lock = threading.Lock()

SYSTEM_PROMPT = (
    "تو کارشناس حقوق ایران هستی. برای هر ماده/تبصره/بندی که داده می‌شود یک "
    "خلاصه‌ی پاراگرافی ۳ تا ۴ جمله‌ای به فارسی روان بنویس که توضیح دهد: "
    "(۱) این حکم چه چیزی را مقرر می‌کند، (۲) چه اشخاص یا نهادهایی را مشمول "
    "می‌گیرد یا به آنها حق/تکلیف می‌دهد، (۳) استثنا یا جزء مهم آن اگر دارد. "
    "از کپی‌کردن عین متن خودداری کن. خروجی فقط JSON: "
    '{"summaries": {"<id>": "<خلاصه>"}} — هر id دقیقاً یک‌بار.'
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


def fallback_api_key() -> str:
    if os.environ.get("LEGAL_AGENT_FALLBACK_API_KEY"):
        return os.environ["LEGAL_AGENT_FALLBACK_API_KEY"].strip()
    text = API_TXT.read_text(encoding="utf-8", errors="replace")
    # the avalai block is the first provider block in api.txt
    m = re.search(r"apiKey\"\s*[=:]\s*\"([^\"]+)\"", text)
    if not m:
        raise SystemExit("no avalai api key found")
    return m.group(1).strip()


def extract_json(text: str) -> dict:
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("no JSON")
    body = candidate[start:]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # truncated response (reasoning models can hit max_tokens mid-string):
        # salvage every COMPLETE "prov_id": "summary" pair
        block = re.search(r'"summaries"\s*:\s*\{', body)
        region = body[block.end():] if block else body
        pairs = re.findall(r'"(prov_[0-9a-f]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', region)
        if not pairs:
            raise
        salvaged = {k: json.loads(f'"{v}"') for k, v in pairs}
        return {"summaries": salvaged}


def _call(base_url: str, model: str, key: str, prompt: str, timeout: int) -> dict:
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    if "z.ai" in base_url:
        payload["thinking"] = {"type": "disabled"}
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        data = json.loads(resp.read())
    message = data["choices"][0]["message"]
    # reasoning models (deepseek-v4.1) may return content alongside reasoning_content
    return extract_json(message.get("content") or "")


def _with_retries(base_url, model, key, prompt, retries, timeout, *, rate_limit_raises):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return _call(base_url, model, key, prompt, timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429 and rate_limit_raises:
                raise
            time.sleep(20 if exc.code == 429 else 5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"LLM failed: {last}")


def llm_json(key: str, prompt: str, fallback_key: str, retries: int = 4) -> dict:
    """Call the primary provider; on sustained 429s switch to avalai for a while."""
    global _fallback_until
    with _provider_lock:
        on_fallback = time.time() < _fallback_until
    if on_fallback:
        try:
            return _with_retries(
                FALLBACK_BASE_URL, FALLBACK_MODEL, fallback_key, prompt,
                retries, 300, rate_limit_raises=False,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"fallback provider failed ({str(exc)[:80]}); probing primary")
    try:
        return _with_retries(
            BASE_URL, MODEL, key, prompt, 1, 300, rate_limit_raises=True,
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            # non-rate-limit primary error: retry a few times before giving up
            return _with_retries(BASE_URL, MODEL, key, prompt, retries, 300, rate_limit_raises=False)
    except Exception:
        pass
    with _provider_lock:
        _fallback_until = time.time() + FALLBACK_COOLDOWN
    log(f"primary 429 -> switching to {FALLBACK_MODEL} for {FALLBACK_COOLDOWN}s")
    return _with_retries(
        FALLBACK_BASE_URL, FALLBACK_MODEL, fallback_key, prompt,
        retries, 300, rate_limit_raises=False,
    )


def provisions_of(db: psycopg.Connection, instrument_id: str, only_missing: bool) -> list[dict]:
    missing = (
        "AND NOT EXISTS (SELECT 1 FROM canonical.provision_summaries s"
        " WHERE s.provision_id = p.provision_id AND s.prompt_version = %s)"
        if only_missing
        else ""
    )
    params: tuple = (instrument_id, PROMPT_VERSION) if only_missing else (instrument_id,)
    rows = db.execute(
        f"""SELECT p.provision_id, COALESCE(p.label, ''), COALESCE(p.title, ''),
                  (SELECT COALESCE(pv.normalized_text, pv.text, '')
                     FROM canonical.provision_versions pv
                    WHERE pv.provision_id = p.provision_id
                    ORDER BY pv.provision_version_id LIMIT 1)
             FROM canonical.provisions p
            WHERE p.instrument_id = %s {missing}
            ORDER BY p.ordinal, p.provision_id""",
        params,
    ).fetchall()
    return [
        {
            "provision_id": row[0],
            "label": row[1],
            "title": row[2],
            "text": (row[3] or "")[:900],
        }
        for row in rows
    ]


def build_prompt(instrument_title: str, nodes: list[dict]) -> str:
    listing = []
    for node in nodes:
        head = node["label"].strip() or node["provision_id"]
        if node["title"]:
            head += f" — {node['title']}"
        body = node["text"].replace("\n", " ").strip() or "—"
        listing.append(f"- id={node['provision_id']} | {head} | متن: {body}")
    return (
        f"عنوان سند: {instrument_title}\n"
        "مصوباتی که باید خلاصه شوند:\n" + "\n".join(listing)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "LEGAL_AGENT_POSTGRES_DSN",
            "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent",
        ),
    )
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--limit", type=int, default=0, help="max instruments (0 = all)")
    parser.add_argument(
        "--force", action="store_true", help="re-summarize instruments that already have summaries"
    )
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    key = api_key()
    fallback_key = fallback_api_key()

    with psycopg.connect(args.dsn, autocommit=True) as db:
        condition = "AND i.instrument_id = %s" if args.instrument else ""
        params = (args.instrument,) if args.instrument else ()
        # resume at provision granularity: pick instruments that still have
        # at least one un-summarized provision
        missing_cond = "" if args.force else (
            "AND EXISTS (SELECT 1 FROM canonical.provisions p"
            "             WHERE p.instrument_id = i.instrument_id"
            "               AND NOT EXISTS (SELECT 1 FROM canonical.provision_summaries s"
            "                                WHERE s.provision_id = p.provision_id"
            "                                  AND s.prompt_version = %s))"
        )
        params = params + ((PROMPT_VERSION,) if missing_cond else ())
        instruments = db.execute(
            f"""SELECT i.instrument_id, i.title
                  FROM canonical.legal_instruments i
                 WHERE EXISTS (SELECT 1 FROM canonical.provisions p
                                WHERE p.instrument_id = i.instrument_id)
                   {condition} {missing_cond}
                 ORDER BY i.instrument_id""",
            params,
        ).fetchall()
        todo = list(instruments)
        if args.limit:
            todo = todo[: args.limit]
        log(f"{len(todo)} instruments with un-summarized provisions (force={args.force})")

        tasks = []
        for instrument_id, title in todo:
            nodes = provisions_of(db, instrument_id, only_missing=not args.force)
            batches = [
                nodes[start : start + BATCH_SIZE]
                for start in range(0, len(nodes), BATCH_SIZE)
            ]
            tasks.append((instrument_id, title, batches))
        total_calls = sum(len(t[2]) for t in tasks)
        log(f"tasks: {len(tasks)} instruments, {total_calls} LLM calls, workers={args.workers}")

        def run_task(task):
            instrument_id, title, batches = task
            summaries: dict[str, str] = {}
            for batch in batches:
                payload = llm_json(key, build_prompt(title, batch), fallback_key)
                for node in batch:
                    text = (payload.get("summaries") or {}).get(node["provision_id"])
                    if isinstance(text, str) and text.strip():
                        summaries[node["provision_id"]] = text.strip()[:1500]
            return instrument_id, title, summaries

        done_calls = 0
        done_instruments = failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_task, task): task for task in tasks}
            for future in as_completed(futures):
                instrument_id, title = futures[future][0], futures[future][1]
                try:
                    instrument_id, title, summaries = future.result()
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    log(f"{instrument_id} FAILED: {str(exc)[:120]}")
                    continue
                if summaries:
                    for provision_id, text in summaries.items():
                        db.execute(
                            """INSERT INTO canonical.provision_summaries
                                   (provision_id, instrument_id, summary, model, prompt_version)
                               VALUES (%s, %s, %s, %s, %s)
                               ON CONFLICT (provision_id) DO UPDATE
                               SET summary = EXCLUDED.summary,
                                   model = EXCLUDED.model,
                                   prompt_version = EXCLUDED.prompt_version,
                                   created_at = now()""",
                            (provision_id, instrument_id, text, MODEL, PROMPT_VERSION),
                        )
                done_instruments += 1
                done_calls += len(futures[future][2])
                if done_instruments % 25 == 0:
                    log(f"{done_instruments}/{len(tasks)} instruments, ~{done_calls}/{total_calls} calls (last: {title[:50]})")
        log(f"finished: {done_instruments} instruments, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
