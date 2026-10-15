#!/usr/bin/env python3
"""Driver for the enrichment quality cycle (docs/qavanin/05_quality_cycle.md).

Subcommands:
  discover       — unified gap inventory (4 sources) -> out/quality/cycle-*.json/.md
  sample-audit   — LLM-as-oracle diff audit over sampled laws (--famous --random N)
  fetch-missing  — absent referenced laws -> fetch_missing_laws -> relation chain -> sync
  full           — discover -> fetch-missing -> sample-audit -> report

The heavy lifting stays in the deterministic scripts; this driver only
orchestrates them and records the metrics of each cycle.

Usage:
    LEGAL_AGENT_POSTGRES_DSN=... [LEGAL_AGENT_LLM_API_KEY=...] \
        .venv/bin/python scripts/quality_cycle.py discover
    .venv/bin/python scripts/quality_cycle.py sample-audit --famous --random 5
    .venv/bin/python scripts/quality_cycle.py fetch-missing --top 12
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg

from legal_agent_core.enrichment.quality import (
    FAMOUS_LAW_IDS,
    collect_gaps,
    resolve_llm_key,
    sample_audit,
    write_report,
)

SCRIPTS = Path(__file__).resolve().parent
QUALITY_DIR = Path(__file__).resolve().parents[1] / "out" / "quality"
FETCH_PREFIXES = (
    "قانون", "آیین‌نامه", "آيين‌نامه", "آیین نامه", "مصوبه", "تصویب‌نامه",
    "بخشنامه", "دستورالعمل", "اساسنامه", "منشور", "لایحه",
)


def _log(message: str) -> None:
    print(f"[quality {datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


def _connect(args) -> psycopg.Connection:
    dsn = args.dsn or os.environ.get("LEGAL_AGENT_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("no DSN (--dsn or LEGAL_AGENT_POSTGRES_DSN)")
    return psycopg.connect(dsn, autocommit=True)


def cmd_discover(args) -> int:
    conn = _connect(args)
    report = collect_gaps(conn, progress=_log)
    path = write_report(QUALITY_DIR, gap_report=report)
    counts = report.curated.get("counts", {})
    _log(f"curated bullets: {counts.get('bullets', 0)} | edged: {counts.get('edged', 0)} | "
         f"matched-no-edge: {counts.get('matched_no_edge', 0)} | absent: {counts.get('absent', 0)}")
    _log(f"rulings: {report.rulings}")
    _log(f"refs by status: {report.refs.get('by_status')}")
    _log(f"report: {path}")
    return 0


def _pick_instruments(conn, args) -> list[str]:
    ids = list(FAMOUS_LAW_IDS) if args.famous else []
    if args.random:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT i.instrument_id FROM canonical.legal_instruments i
                   WHERE EXISTS (SELECT 1 FROM canonical.provisions p WHERE p.instrument_id = i.instrument_id)
                   ORDER BY random() LIMIT %s""",
                (args.random,),
            )
            ids += [r[0] for r in cur.fetchall()]
    seen: set[str] = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


def cmd_sample_audit(args) -> int:
    key = resolve_llm_key()
    if not key:
        raise SystemExit("no LLM key (LEGAL_AGENT_LLM_API_KEY or ~/api.txt)")
    conn = _connect(args)
    instruments = _pick_instruments(conn, args)
    _log(f"sample-audit over {len(instruments)} laws, {args.per_law} provisions each")
    audit = sample_audit(conn, key, instruments, per_law=args.per_law,
                         progress=lambda msg, _s=None: _log(str(msg)))
    path = write_report(QUALITY_DIR, audit=audit)
    findings = audit.get("findings", [])
    top_laws = __import__("collections").Counter(
        f["law"] for f in findings if f["kind"] == "missed_by_pipeline"
    ).most_common(15)
    _log(f"stats: {audit['stats']}")
    _log("top LLM-seen laws the pipeline has no mention for:")
    for law, n in top_laws:
        _log(f"  {n:3}x {law[:80]}")
    _log(f"report: {path}")
    return 0


def _absent_titles(conn, top: int) -> list[str]:
    """Referenced-but-absent law titles, best-effort cleaned for ekhtebar search."""
    candidates: list[str] = []
    gap = collect_gaps(conn, progress=_log)
    for row in gap.absent_laws:
        candidates.append(row["title_norm"])
    with conn.cursor() as cur:
        cur.execute(
            """SELECT target_text, count(*) FROM canonical.explicit_references
               WHERE resolution_status='invalid' AND target_text ~ 'قانون'
               GROUP BY 1 ORDER BY 2 DESC LIMIT %s""",
            (top,),
        )
        for text, _n in cur.fetchall():
            candidates.append(text)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        title = raw.strip()
        # cut the mention at the first article/aside marker so the search gets
        # the document name, not the citing sentence
        for marker in (" به استثنا", " موضوع ", " مصوب ", " است", " می", " و اصلاح"):
            idx = title.find(marker)
            if idx > 15:
                title = title[:idx]
        title = title.strip(" .،؛-")
        if not title.startswith(FETCH_PREFIXES):
            continue
        key = title[:45]
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(title)
        if len(cleaned) >= top:
            break
    return cleaned


def cmd_fetch_missing(args) -> int:
    conn = _connect(args)
    titles = _absent_titles(conn, args.top)
    _log(f"fetch-worthy absent titles: {len(titles)}")
    for t in titles:
        _log(f"  - {t[:80]}")
    mini = {"missing_laws": {t: {"titles": [t], "articles": []} for t in titles}}
    mini_path = QUALITY_DIR / "missing-mini.json"
    mini_path.parent.mkdir(parents=True, exist_ok=True)
    mini_path.write_text(json.dumps(mini, ensure_ascii=False, indent=1), encoding="utf-8")

    env = os.environ.copy()
    env.setdefault("LEGAL_AGENT_POSTGRES_DSN", args.dsn or "")
    steps: list[list[str]] = [
        [sys.executable, str(SCRIPTS / "fetch_missing_laws.py"),
         "--gap-report", str(mini_path), "--delay", str(args.delay)],
        [sys.executable, str(SCRIPTS / "publish_relations.py"),
         "--stages", "repair,bare,crosslaw", *(["--skip-llm"] if args.skip_llm else [])],
        [sys.executable, str(SCRIPTS / "ingest_source_related.py"),
         *(["--skip-llm"] if args.skip_llm else [])],
        [sys.executable, str(SCRIPTS / "sync_laws_graph_falkordb.py")],
    ]
    for cmd in steps:
        _log("running: " + " ".join(cmd[1:3]))
        result = subprocess.run(cmd, env=env, check=False)
        if result.returncode != 0:
            _log(f"step failed rc={result.returncode}: {cmd[1]}")
            return result.returncode
    _log("fetch-missing cycle done — re-run discover to measure")
    return 0


def cmd_full(args) -> int:
    rc = cmd_discover(args)
    if rc == 0 and not args.skip_fetch:
        rc = cmd_fetch_missing(args)
    if rc == 0 and not args.skip_llm:
        rc = cmd_sample_audit(args)
    _log("full cycle finished")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="gap inventory report")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("sample-audit", help="LLM-as-oracle diff audit")
    p.add_argument("--famous", action="store_true", help="include the famous-law set")
    p.add_argument("--random", type=int, default=0, help="N random laws")
    p.add_argument("--per-law", type=int, default=30, help="provisions per law")
    p.set_defaults(func=cmd_sample_audit)

    p = sub.add_parser("fetch-missing", help="fetch absent referenced laws and re-link")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--skip-llm", action="store_true")
    p.set_defaults(func=cmd_fetch_missing)

    p = sub.add_parser("full", help="discover + fetch-missing + sample-audit")
    p.add_argument("--famous", action="store_true")
    p.add_argument("--random", type=int, default=0)
    p.add_argument("--per-law", type=int, default=30)
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--skip-llm", action="store_true")
    p.add_argument("--skip-fetch", action="store_true")
    p.set_defaults(func=cmd_full)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
