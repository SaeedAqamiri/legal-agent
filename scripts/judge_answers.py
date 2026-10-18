#!/usr/bin/env python3
"""Judge the agent's answers against the benchmark gold answers (the oracle).

OpenCode's agent knows it is "correct" because tests/lint pass — an objective
oracle. The legal agent's oracle is the benchmark ``gold_answer`` per question.
This script reads the stored research episodes, matches them to benchmark
questions, and asks an LLM judge whether the agent's answer agrees with gold.

    .venv/bin/python scripts/judge_answers.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

BENCHMARKS = ["social_security_core_v3.jsonl", "sports_core_v3.jsonl"]
DSN = "postgresql://legal_agent:legal_agent@127.0.0.1:5432/legal_agent"
JSON_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)
SYSTEM = "تو داور حقوقی هستی. فقط JSON معتبر برگردان؛ حدس نزن."


def _key() -> tuple[str, str, str]:
    s = json.loads(Path("out/llm_settings.json").read_text(encoding="utf-8"))
    return s["base_url"], s["model"], s["api_key"]


def _judge(base: str, model: str, key: str, question: str, gold: str, agent: str) -> dict:
    prompt = (
        "پرسش:\n" + question + "\n\nپاسخ مرجع (درست):\n" + gold + "\n\nپاسخ عامل:\n" + (agent or "(خالی)")
        + '\n\nآیا پاسخ عامل از نظر محتوای حقوقی با پاسخ مرجع هم‌خوان است؟ '
        'خروجی فقط: {"verdict":"correct|partial|wrong","score":0.0,"reason":"..."}'
    )
    body = json.dumps({
        "model": model, "temperature": 0.0, "max_tokens": 400,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"] or ""
            fenced = JSON_RE.search(content)
            candidate = fenced.group(1) if fenced else content
            start = candidate.find("{")
            return json.loads(candidate[start:])
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return {"verdict": "error", "score": 0.0, "reason": "judge failed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DSN)
    parser.add_argument("--out", type=Path, default=Path("out/answer_quality.json"))
    args = parser.parse_args()

    gold: dict[str, dict] = {}
    for path in BENCHMARKS:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            gold[item["question"].strip()] = item

    db = psycopg.connect(args.dsn, autocommit=True)
    episodes = db.execute(
        """SELECT question, completed, payload FROM research_history.episodes
            WHERE organization_id='org-a' ORDER BY created_at DESC"""
    ).fetchall()

    base, model, key = _key()
    results: list[dict] = []
    seen: set[str] = set()
    for question, completed, payload in episodes:
        q = (question or "").strip()
        if q in seen or q not in gold:
            continue
        seen.add(q)
        answer = (payload or {}).get("answer") or {}
        text = answer.get("text") or answer.get("rendered_text") or ""
        if not completed or not text.strip():
            results.append({"id": gold[q]["id"], "verdict": "no_answer", "score": 0.0,
                            "reason": "منتشر نشد", "question": q})
            continue
        verdict = _judge(base, model, key, q, gold[q]["gold_answer"], text)
        verdict.update({"id": gold[q]["id"], "question": q})
        results.append(verdict)
        print(f"  {verdict.get('id')} [{verdict.get('verdict')}] {verdict.get('reason','')[:70]}", flush=True)

    n = len(results)
    ok = sum(1 for r in results if r.get("verdict") == "correct")
    partial = sum(1 for r in results if r.get("verdict") == "partial")
    wrong = sum(1 for r in results if r.get("verdict") == "wrong")
    noans = sum(1 for r in results if r.get("verdict") == "no_answer")
    print(f"\nmatched={n} correct={ok} partial={partial} wrong={wrong} no_answer={noans}")
    args.out.write_text(json.dumps({"summary": {"matched": n, "correct": ok, "partial": partial,
                                                "wrong": wrong, "no_answer": noans},
                                    "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("report ->", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
