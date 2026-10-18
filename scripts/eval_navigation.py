#!/usr/bin/env python3
"""Focused 20-question evaluation of the *spirit* of lawyer-like navigation.

Not an accuracy benchmark — it measures whether the agent actually navigates
hierarchically (tree/notebook/structure/call-graph/precedent) instead of only
grepping, and whether the trace carries typed reasons.

For each question it POSTs to /v1/research/stream and records which tools and
typed reasons fired, plus whether a verified answer was produced.

    .venv/bin/python scripts/eval_navigation.py --workers 3
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = os.environ.get(
    "LEGAL_AGENT_RESEARCH_API", "http://127.0.0.1:8000/v1/research/stream"
)
ORG = "org-a"
APPLICABLE = "2024-06-01"

TREE_TOOLS = {"walk_tree", "list_tree"}
NOTEBOOK_TOOLS = {"notebook_index", "notebook_route", "open_notebook"}
STRUCTURE_TOOLS = {"get_document_structure", "list_document_contents"}
CITATION_TOOLS = {"get_related", "get_references", "node_neighbors", "find_paths", "get_statute_family"}
PRECEDENT_TOOLS = {"search_judgments", "judgments_for_law", "get_judgment_text"}

#: Hand-picked to exercise the whole capability: subject descent, internal
#: structure, cross-law call graph, temporal validity, precedent, procedure.
QUESTIONS: list[tuple[str, str]] = [
    ("SS-01", "کارفرما پس از ابلاغ بدهی، اگر به مبلغ یا اصل بدهی اعتراض داشته باشد تا چه زمانی فرصت اعتراض دارد؟"),
    ("SS-02", "برای اینکه شخص در بخشنامه «راننده حمل‌ونقل عمومی بار و مسافر بین‌شهری» محسوب شود، مهم‌ترین عناصر تعریف چیست؟"),
    ("SS-03", "شرکت پیمانکاری بدهی بیمه‌ای اعلام‌شده را قبول ندارد و برای آزادسازی مطالباتش به مفاصاحساب نیاز دارد؛ اگر اعتراض رد شود و بدهی قطعی پرداخت نشود، از مرحله اعتراض تا وصول اجباری چه وضعی ایجاد می‌شود؟"),
    ("SS-04", "راننده‌ای از سال ۱۳۸۳ با دفترچه کار معتبر بین‌شهری فعالیت داشته و در مرداد ۱۳۸۴ حق بیمه پرداخته است. شعبه می‌گوید چون بعداً کارت هوشمند جایگزین دفترچه شد، سابقه مرداد ۱۳۸۴ قابل پذیرش نیست. آیا این استدلال درست است؟"),
    ("SS-05", "بدهی حق بیمه یک شرکت با مسئولیت محدود قطعی و وارد اجرائیات شده و شرکت مال کافی ندارد. آیا سازمان می‌تواند صرفاً به خاطر مدیرعامل بودن، خانه شخصی مدیرعامل را توقیف کند؟"),
    ("SS-06", "«سابقه قطعی» و «سابقه غیرقطعی» در دستورالعمل سوابق پرداخت حق بیمه چه تفاوتی دارند؟"),
    ("SS-07", "اگر خود سازمان تأمین اجتماعی به رأی هیئت بدوی تشخیص مطالبات معترض باشد، مهلت اعتراض سازمان چند روز است؟"),
    ("SS-08", "در اجرای بدهی یک شرکت، مأمور اجرا با سه نوع مال روبه‌رو است: اثاث اداری که مدیر «مستثنیات دین» می‌داند، سرقفلی مغازه و ضمانت‌نامه بانکی پیمانکاری. آیا برخورد با هر سه یکسان است؟"),
    ("SS-09", "کارگری برای ۹ ماه سابقه جاافتاده رأی اشتغال دارد، پیش از تکمیل ثبت بیکار شده و مقرری بیمه بیکاری گرفته و شش ماه بعد فوت کرده است. آیا صرف رأی اشتغال برای منظور کردن آن ۹ ماه کافی است؟"),
    ("SS-10", "بدهی پیمانکار پس از رسیدگی تجدیدنظر قطعی و ابلاغ مجدد، در مهلت مقرر پرداخت نشده و پنج درصد و آخرین قسط نزد واگذارنده مانده است. سازمان از این مرحله به بعد چه اقداماتی می‌تواند انجام دهد؟"),
    ("SP-01", "برای صدور پروانه یک باشگاه بدنسازی، «باشگاه ورزشی» از نظر قانونی چه تعریفی دارد و فعالیت آن به چه مجوزی نیاز دارد؟"),
    ("SP-02", "برای انتخابات ریاست یک فدراسیون، داوطلب ریاست باید در چه بازه سنی‌ای باشد؟"),
    ("SP-03", "رئیس انجمن یک رشته غیرالمپیکی در مجمع عمومی فدراسیون چندرشته‌ای چه جایگاهی دارد؛ حق رأی دارد یا تابع ضوابط دیگری است؟"),
    ("SP-04", "شرایط تصدی دبیرکلی فدراسیون چیست و اگر دبیرکل بخواهد در انتخابات هیأت رئیسه کاندیدا شود تکلیفش چیست؟"),
    ("SP-05", "از انتشار فراخوان انتخابات فدراسیون تا بررسی صلاحیت داوطلبان چه مراحل و مهلت‌هایی وجود دارد؟"),
    ("SP-06", "مبنای قانونی تصویب‌نامه ترخیص کالاهای اهدایی فدراسیون فوتبال (۱۳۸۰) کدام ماده قانونی بود؟"),
    ("SP-07", "استان می‌خواهد مدیریت یک پیست اسکی قدیمی را بر مبنای اساسنامه سال ۱۳۵۶ مراکز جهانگردی ورزش‌های زمستانی انجام دهد. آیا این اساسنامه هنوز مبنای حقوقی معتبری است؟"),
    ("SP-08", "برای تنظیم دفاعیه به ترکیب ارکان فدراسیون فوتبال نیاز داریم اما متن کامل اساسنامه در دسترس نیست. چه بخش‌هایی را می‌توان با اطمینان تعیین کرد؟"),
    ("SP-09", "در یک رأی دیوان به قانون تأسیس سازمان تربیت بدنی ۱۳۵۰ استناد شده است. با توجه به وضعیت فعلی آن قانون، آیا این استناد هنوز قابل اتکاست؟"),
    ("SP-10", "برای احداث یک مجموعه ورزشی روستایی، از انتخاب زمین تا ساخت و نگهداری چه مراحلی باید طی شود و مسئول هر مرحله چه نهادی است؟"),
]


def run_one(qid: str, question: str, timeout: float, api: str = API) -> dict:
    started = time.monotonic()
    payload = json.dumps(
        {
            "organization_id": ORG,
            "question": question,
            "applicable_time": APPLICABLE,
            "strategy": "agentic",
        }
    ).encode()
    request = urllib.request.Request(
        api, data=payload, headers={"Content-Type": "application/json"}
    )
    tool_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    result: dict | None = None
    error: str | None = None
    planner_calls = 0
    input_tokens = 0
    output_tokens = 0
    tool_errors: dict[str, int] = {}
    abstention: str | None = None
    verification_rejections = 0
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
                        etype = event.get("type")
                        if etype == "tool_call":
                            tool = event.get("tool")
                            tool_counts[tool] = tool_counts.get(tool, 0) + 1
                            reason = event.get("reason")
                            if reason:
                                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                        elif etype == "planner_call":
                            planner_calls += 1
                            input_tokens += event.get("input_tokens") or 0
                            output_tokens += event.get("output_tokens") or 0
                        elif etype == "tool_error":
                            label = event.get("error") or "unknown"
                            tool_errors[label] = tool_errors.get(label, 0) + 1
                        elif etype == "abstained":
                            abstention = event.get("reason")
                        elif etype == "verification_rejected":
                            verification_rejections += 1
                        elif etype == "result":
                            result = event.get("data")
                        elif etype == "failed":
                            error = event.get("message")
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    answer = (result or {}).get("answer")
    used = set(tool_counts)
    return {
        "id": qid,
        "question": question,
        "verified": bool(answer),
        "error": error,
        "elapsed_s": round(time.monotonic() - started, 1),
        "tool_counts": tool_counts,
        "reason_counts": reason_counts,
        "used_tree": bool(used & TREE_TOOLS),
        "used_notebook": bool(used & NOTEBOOK_TOOLS),
        "used_structure": bool(used & STRUCTURE_TOOLS),
        "used_citation": bool(used & CITATION_TOOLS),
        "used_precedent": bool(used & PRECEDENT_TOOLS),
        "total_tools": sum(tool_counts.values()),
        "planner_calls": planner_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_errors": tool_errors,
        "abstention": abstention,
        "verification_rejections": verification_rejections,
        "iterations": (result or {}).get("iterations"),
        "identified_issues": ((result or {}).get("trace") or {}).get("identified_issues"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--ids", default="", help="comma-separated question ids")
    parser.add_argument("--api", default=API)
    parser.add_argument("--out", type=Path, default=Path("out/eval_navigation.json"))
    args = parser.parse_args()

    wanted = {part.strip() for part in args.ids.split(",") if part.strip()}
    questions = [
        (qid, question)
        for qid, question in QUESTIONS
        if not wanted or qid in wanted
    ]
    print(f"running {len(questions)} questions, workers={args.workers} …", flush=True)
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_one, qid, question, args.timeout, args.api): qid
            for qid, question in questions
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            flag = "✓" if row["verified"] else "·"
            print(
                f"  {flag} {row['id']}  tools={row['total_tools']:2d} "
                f"tree={int(row['used_tree'])} nb={int(row['used_notebook'])} "
                f"str={int(row['used_structure'])} cite={int(row['used_citation'])} "
                f"prec={int(row['used_precedent'])} "
                f"steps={row['iterations']} errors={sum(row['tool_errors'].values())} "
                f"{row['elapsed_s']}s",
                flush=True,
            )

    rows.sort(key=lambda r: r["id"])
    n = len(rows)
    agg = {
        "questions": n,
        "verified": sum(r["verified"] for r in rows),
        "used_tree": sum(r["used_tree"] for r in rows),
        "used_notebook": sum(r["used_notebook"] for r in rows),
        "used_structure": sum(r["used_structure"] for r in rows),
        "used_citation": sum(r["used_citation"] for r in rows),
        "used_precedent": sum(r["used_precedent"] for r in rows),
        "errors": sum(1 for r in rows if r["error"]),
    }
    reasons: dict[str, int] = {}
    for row in rows:
        for reason, count in row["reason_counts"].items():
            reasons[reason] = reasons.get(reason, 0) + count
    report = {"aggregate": agg, "reasons": reasons, "rows": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n== aggregate ==")
    for key, value in agg.items():
        print(f"  {key}: {value}/{n}")
    print("== typed reasons ==", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
    print(f"report -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
