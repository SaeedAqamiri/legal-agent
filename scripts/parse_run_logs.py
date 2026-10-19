#!/usr/bin/env python3
"""Build a detailed, exact step-by-step navigation trace for every question.

Columns: step | thought | exact tool call (name + args) | result | next decision
(the next step's thought, or the terminal outcome). Writes out/navigation_trace.md.
"""
from __future__ import annotations

import re
from pathlib import Path

SOURCES = [
    ("out/q_SS-01.log", {"SS-01"}),
    ("out/q_SS-02.log", {"SS-02"}),
    ("out/q_SS-03.log", {"SS-03"}),
    ("out/q_rest17.log", {
        "SS-04", "SS-05", "SS-06", "SS-07", "SS-08", "SS-09", "SS-10",
        "SP-01", "SP-02", "SP-03", "SP-04", "SP-05", "SP-06", "SP-07", "SP-08", "SP-09", "SP-10",
    }),
]

QID = re.compile(r"^### ([A-Z]{2}-\d+): (.*)$")
STEP = re.compile(r"^\[گام (\d+)\] فکر: (.*)$")
TOOL = re.compile(
    r"^\s+→ (\w+)\((.*?)\) \| status=(\S+) \| reason=(\S+) \| نتایج=(\S+?)/(\S+?) \| evidence=(\S+)$"
)
TOOL_NOEV = re.compile(r"^\s+→ (\w+)\((.*?)\) \| status=(\S+) \| reason=(\S+) \| نتایج=(\S+?)/(\S+)$")
ERR = re.compile(r"^\s+!! (tool_error|contract_error) (.*)$")
VER = re.compile(r"^\[راستی‌آزمایی\] (پذیرفته شد|رد شد)(.*)$")
SUM = re.compile(r"^== خلاصه: iterations=(\d+) actions=(\d+) visited_provisions=(\d+)")
CLAIMS = re.compile(r"^claims=(\d+)")
NOANS = re.compile(r"^پاسخ نهایی: ندارد")
ORIENT = re.compile(r"^\[جهت‌گیری اولیه\] (.*)$")


def parse(path: str) -> dict[str, dict]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"^### ([A-Z]{2}-\d+): ", text, flags=re.MULTILINE)
    out: dict[str, dict] = {}
    for i in range(1, len(blocks), 2):
        qid, body = blocks[i], blocks[i + 1]
        question = body.splitlines()[0].strip() if body.splitlines() else ""
        steps: list[dict] = []
        thought = None
        orientation = None
        verified = None
        iterations = actions = None
        claims = 0
        for line in body.splitlines():
            m = ORIENT.match(line)
            if m:
                orientation = m.group(1).strip()
                continue
            if orientation is not None and line.startswith("- "):
                orientation += " | " + line[2:].strip()
                continue
            m = STEP.match(line)
            if m:
                thought = m.group(2).strip()
                continue
            m = TOOL.match(line)
            if not m:
                m2 = TOOL_NOEV.match(line)
                if m2:
                    m = m2
                    evidence = "-"
                else:
                    evidence = None
            if m:
                if evidence is None:
                    evidence = m.group(7) if m.re.groups >= 7 else "-"
                steps.append({
                    "thought": thought or "",
                    "tool": m.group(1),
                    "args": m.group(2),
                    "status": m.group(3),
                    "reason": m.group(4),
                    "returned": m.group(5),
                    "total": m.group(6),
                    "evidence": evidence,
                })
                thought = None
                continue
            m = ERR.match(line)
            if m:
                steps.append({"thought": thought or "", "tool": "ERROR", "args": m.group(2).strip(),
                              "status": "-", "reason": "-", "returned": "-", "total": "-", "evidence": "-"})
                thought = None
                continue
            m = VER.match(line)
            if m:
                verified = (m.group(1) + m.group(2)).strip()
            m = SUM.match(line)
            if m:
                iterations, actions = m.group(1), m.group(2)
            m = CLAIMS.match(line)
            if m:
                claims = int(m.group(1))
        out[qid] = {"question": question, "steps": steps, "orientation": orientation,
                    "verified": verified, "iterations": iterations, "actions": actions,
                    "claims": claims}
    return out


def main() -> int:
    data: dict[str, dict] = {}
    for path, ids in SOURCES:
        if not Path(path).exists():
            continue
        parsed = parse(path)
        for qid in ids:
            if qid in parsed:
                data[qid] = parsed[qid]

    lines: list[str] = ["# دقیق‌ترین رد پای ناوبری (SS-01 .. SS-10، SP-01 .. SP-10)", ""]
    for qid in sorted(data, key=lambda q: (q[:2] == "SP", q)):
        d = data[qid]
        verdict = d["verified"] or "ناتمام"
        lines.append(f"## {qid} — {verdict}")
        lines.append(f"**پرسش:** {d['question']}")
        lines.append(f"**جهت‌گیری اولیه (walk_tree):** {d['orientation'] or '—'}")
        lines.append("")
        lines.append("| گام | فکر | ابزار و آرگومان | نتیجه | تصمیم گام بعد |")
        lines.append("|---|---|---|---|---|")
        for idx, s in enumerate(d["steps"]):
            nxt = d["steps"][idx + 1]["thought"] if idx + 1 < len(d["steps"]) else (
                "پاسخ نهایی" if d["verified"] else "پایان (بدون پاسخ)")
            result = f"status={s['status']}؛ نتایج={s['returned']}/{s['total']}؛ evidence={s['evidence']}"
            lines.append(
                f"| {idx+1} | {s['thought'] or '—'} | `{s['tool']}({s['args']})` | {result} | {nxt} |"
            )
        lines.append("")
        lines.append(f"**جمع:** iterations={d['iterations']} actions={d['actions']} claims={d['claims']}")
        lines.append("")

    out = Path("out/navigation_trace.md")
    out.write_text("\n".join(lines), encoding="utf-8")

    # process metrics
    from collections import Counter
    tool_counts: Counter = Counter()
    struct_q = tree_q = nb_q = cite_q = 0
    reread = 0
    verdict = Counter()
    for qid in sorted(data, key=lambda q: (q[:2] == "SP", q)):
        d = data[qid]
        tools = [s["tool"] for s in d["steps"]]
        for t in tools:
            tool_counts[t] += 1
        reads = tools.count("read_span")
        reread += max(0, reads - 1)
        if any(t == "get_document_structure" for t in tools):
            struct_q += 1
        if any(t in {"walk_tree", "list_tree"} for t in tools):
            tree_q += 1
        if any(t in {"notebook_index", "notebook_route", "open_notebook"} for t in tools):
            nb_q += 1
        if any(t in {"get_related", "get_references", "get_statute_family", "search_in_subtree", "list_children"} for t in tools):
            cite_q += 1
        verdict["verified" if d["verified"] else "incomplete"] += 1

    print(f"\nwrote {out}")
    print("tools:", dict(tool_counts.most_common()))
    print(f"questions using: structure={struct_q} tree={tree_q} notebook={nb_q} subtree/refs={cite_q} / {len(data)}")
    print(f"read_span calls={tool_counts.get('read_span',0)} (re-reads≈{reread})")
    print("verdict:", dict(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
