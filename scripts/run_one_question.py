#!/usr/bin/env python3
"""Run ONE research question and print the agent's step-by-step trajectory.

Streams /v1/research/stream and prints, for each hop: the thought, the tool +
args, status, typed reason and result counts — then the final verified answer.
Used to review navigation behaviour question by question.

    .venv/bin/python scripts/run_one_question.py "SS-01" "<question>"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API = os.environ.get(
    "LEGAL_AGENT_RESEARCH_API", "http://127.0.0.1:8000/v1/research/stream"
)
ORG = "org-a"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qid", nargs="?")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--all", action="store_true", help="run the curated 20 sequentially")
    parser.add_argument("--ids", default="", help="comma-separated ids to run (with --all)")
    parser.add_argument("--applicable", default="2024-06-01")
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args()

    if args.all:
        return _run_all(args)

    if not args.qid or not args.question:
        parser.error("provide qid and question, or --all")
    _run_one(args.qid, args.question, args.applicable, args.timeout)
    return 0


def _run_all(args) -> int:
    import sys as _sys
    from pathlib import Path

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from eval_navigation import QUESTIONS

    wanted = {q.strip() for q in args.ids.split(",") if q.strip()}
    out_dir = Path("out")
    out_dir.mkdir(exist_ok=True)
    for qid, question in QUESTIONS:
        if wanted and qid not in wanted:
            continue
        print(f"===== {qid} =====", flush=True)
        _run_one(qid, question, args.applicable, args.timeout, echo=True)
        print(f"===== {qid} done =====\n", flush=True)
    return 0


def _run_one(qid: str, question: str, applicable: str, timeout: float, echo: bool = False) -> None:
    import sys as _sys

    stream = _sys.stdout if echo else open(f"out/q_{qid}.log", "w", encoding="utf-8")  # noqa: SIM115
    try:
        _stream(qid, question, applicable, timeout, stream)
    finally:
        if not echo:
            stream.close()


def _stream(qid: str, question: str, applicable: str, timeout: float, out) -> None:
    print(f"### {qid}: {question}", file=out, flush=True)
    payload = json.dumps(
        {
            "organization_id": ORG,
            "question": question,
            "applicable_time": applicable,
            "strategy": "agentic",
        }
    ).encode()
    request = urllib.request.Request(
        API, data=payload, headers={"Content-Type": "application/json"}
    )
    result: dict | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            buffer = ""
            for raw in response:
                buffer += raw.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    for line in frame.splitlines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        _print_event(event, out)
                        if event.get("type") == "result":
                            result = event.get("data")
    except Exception as exc:  # noqa: BLE001
        print(f"!! stream error: {exc}", file=out, flush=True)
    _print_result(result, out)


def _print_event(event: dict, out=None) -> None:
    out = out or sys.stdout
    etype = event.get("type")
    if etype == "orientation":
        print(f"[جهت‌گیری اولیه] {event.get('text')}", file=out, flush=True)
    elif etype == "planner_call":
        tokens = (event.get("input_tokens") or 0) + (event.get("output_tokens") or 0)
        status = "ok" if event.get("ok") else "error"
        print(
            f"[تصمیم مدل {event.get('step')}.{event.get('attempt')}] "
            f"{status} | action={event.get('action')}"
            + (f":{event.get('tool')}" if event.get("tool") else "")
            + f" | model={event.get('model')} "
            f"| latency={event.get('latency_ms')}ms | tokens={tokens} "
            f"(in={event.get('input_tokens')}, out={event.get('output_tokens')})"
            + (f" | error={event.get('error')}" if event.get("error") else ""),
            file=out,
            flush=True,
        )
    elif etype == "tool_call":
        thought = (event.get("thought") or "").strip().replace("\n", " ")
        args = json.dumps(event.get("args"), ensure_ascii=False)
        print(f"[گام {event.get('step')}] فکر: {thought[:140]}", file=out, flush=True)
        print(
            f"        → {event.get('tool')}({args}) | status={event.get('status')} "
            f"| reason={event.get('reason')} | نتایج={event.get('returned_count')}/"
            f"{event.get('total_count')} | evidence={event.get('evidence_count')}",
            file=out,
            flush=True,
        )
    elif etype == "tool_error":
        args = json.dumps(event.get("args"), ensure_ascii=False) if event.get("args") else ""
        print(
            f"        !! tool_error {event.get('tool')}({args}): {event.get('error')}",
            file=out,
            flush=True,
        )
    elif etype == "contract_error":
        print(
            f"        !! contract_error: {event.get('message') or event.get('error')}",
            file=out,
            flush=True,
        )
    elif etype == "verification_rejected":
        print(f"[راستی‌آزمایی] رد شد — ادامه می‌دهد: {event.get('issues')}", file=out, flush=True)
    elif etype == "verification_accepted":
        print("[راستی‌آزمایی] پذیرفته شد ✓", file=out, flush=True)
    elif etype == "abstained":
        print(f"[امتناع] {event.get('reason')}", file=out, flush=True)


def _print_result(result: dict | None, out=None) -> None:
    out = out or sys.stdout
    if not result:
        print("!! بدون نتیجهٔ نهایی", file=out, flush=True)
        return
    trace = result.get("trace") or {}
    print(
        f"\n== خلاصه: iterations={result.get('iterations')} "
        f"actions={len(trace.get('actions') or [])} "
        f"visited_provisions={len(trace.get('visited_provision_ids') or [])}",
        file=out,
        flush=True,
    )
    answer = result.get("answer")
    if not answer:
        print("پاسخ نهایی: ندارد (منتشر نشد)", file=out, flush=True)
        return
    text = answer.get("text") if isinstance(answer, dict) else str(answer)
    print(f"\n== پاسخ ({'منتشرشده' if answer else '—'}):\n{text}", file=out, flush=True)
    claims = answer.get("claims") if isinstance(answer, dict) else None
    if claims:
        print(f"\nclaims={len(claims)}", file=out, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
