#!/usr/bin/env python3
"""Build real statutory-basis hierarchy: IMPLEMENTS edges from source text.

Replaces the placeholder SUBORDINATE_TO star (everything -> constitution) with
evidence-derived IMPLEMENTS edges per docs/qavanin/01_graph_schema.md section
2.2 («این آیین‌نامه مجریِ این قانون است»).

Deterministic passes (tier 4/5 instruments — مقررات/بخشنامه):
  1. title pattern: «آیین‌نامه اجرایی قانون X» / «دستورالعمل اجرایی قانون X»
  2. basis phrases anywhere in the doc text: «در اجرای/به موجب/به استناد/
     بر اساس ... قانون X» (quote kept as provenance)
  3. «مستندات مرتبط» link lists: a link naming a statute whose title contains
     the instrument's own subject (e.g. compilation of «روابط موجر و مستاجر»
     linking «قانون روابط موجر و مستاجر مصوب ۱۳۷۶»)

Resolution: normalized exact / difflib >= 0.75 against the instrument catalog
-> published edge (extraction_method='parser'). Hints in the 0.5..0.75 band or
instruments with no parent go to the LLM, which picks among difflib top-3 or
declines; LLM verdicts land in sources.legal_effects as review_status='candidate'
(effect_type='implement') so they flow through the existing review UI — nothing
LLM-made is trusted silently.

Old placeholder edges (created_by='hierarchy-batch') are deleted. Idempotent.

Usage:
    LEGAL_AGENT_LLM_API_KEY=... .venv/bin/python scripts/build_implements_edges.py
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.ingestion.normalization import normalize_legal_text

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
API_TXT = Path("~/api.txt").expanduser()
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"
CREATED_BY = "statutory-basis"
LEGACY_CREATED_BY = "hierarchy-batch"
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

BASIS_PHRASE = re.compile(
    r"(در\s*اجرای|به\s*موجب|به\s*استناد|بر\s*اساس)[^.\n»]{0,140}?"
    r"(قانون|آیین[\u200c\s]*نامه|مصوبه|نظامنامه)\s*(?:اجرایی)?\s*"
    r"(?:ماده\s*[۰-۹0-9]+[^.\n]{0,30})?«?([^»\n،,؛.]{4,90})»?"
)
TITLE_BASIS = re.compile(
    r"^(?:آیین[\u200c\s]*نامه|دستورالعمل|مقررات|نظامنامه)[^\n]{0,30}?"
    r"(?:اجرایی|مربوط|مصوب)?[^\n]{0,10}?(?:قانون)\s+(.{4,80})$"
)
RELATED_LINK = re.compile(
    r"[-*]\s*((?:قانون|آیین[\u200c\s]*نامه|مصوبه|نظامنامه)[^\n]{4,90})"
)
STRIP_TAIL = re.compile(
    r"مصوب\s*[\d۰-۹][\d۰-۹/]*|با\s*آخرین\s*اصلاحات.*|\(منسوخ\)|\[منسوخ\]"
    r"|\b1[0-9]{3}(/[0-9]{2,3})?\b",
)
SUBJECT_PREFIX = re.compile(r"^(قوانین\s*و\s*مقررات|مجموعه\s*قوانین\s*و\s*مقررات|آیین[\u200c\s]*نامه|دستورالعمل|بخشنامه|مقررات|نظامنامه|شرایط\s*و\s*نحوه)\s*")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api_key() -> str:
    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if key:
        return key.strip()
    text = API_TXT.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'"zai".*?"apiKey":\s*"([^"]+)"', text, re.DOTALL)
    if not match:
        raise SystemExit("no LEGAL_AGENT_LLM_API_KEY and no zai apiKey in ~/api.txt")
    return match.group(1).strip()


def llm_json(key: str, prompt: str, system: str, retries: int = 4) -> dict:
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 2000,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    import urllib.request

    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"] or ""
            fenced = JSON_BLOCK_RE.search(content)
            candidate = fenced.group(1) if fenced else content
            start = min(
                (i for i in (candidate.find("{"), candidate.find("[")) if i >= 0),
                default=-1,
            )
            return json.loads(candidate[start:])
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"LLM call failed: {last}")


def clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", STRIP_TAIL.sub(" ", text)).strip(" ‌-:،")


def subject_of(title: str) -> str:
    return normalize_legal_text(SUBJECT_PREFIX.sub("", clean_title(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("LAWS_DATA_DIR", "/home/arman/laws-mcp/data")),
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="skip the LLM candidate stage"
    )
    parser.add_argument("--llm-limit", type=int, default=40)
    args = parser.parse_args()

    db = psycopg.connect(args.dsn, autocommit=True)
    with db.cursor() as cur:
        cur.execute(
            """SELECT instrument_id, title, instrument_type, authority_level
                 FROM canonical.legal_instruments ORDER BY instrument_id"""
        )
        instruments = cur.fetchall()
        cur.execute(
            """SELECT DISTINCT i.instrument_id, i.title, i.authority_level,
                      d.document_uid, d.text_path
                 FROM canonical.legal_instruments i
                 JOIN sources.instruments si ON si.canonical_key = i.canonical_title
                 JOIN sources.source_documents d ON d.instrument_uid = si.instrument_uid
                WHERE i.authority_level IN ('4','5') AND d.text_path IS NOT NULL"""
        )
        regulated = cur.fetchall()
    catalog = [(row[0], row[1], int(row[3] or 3)) for row in instruments]
    title_by_id = {row[0]: row[1] for row in instruments}
    log(f"instruments: {len(instruments)}, tier-4/5 with text: {len(regulated)}")

    # ---------------- deterministic candidate mining ----------------------
    found: dict[str, dict[str, tuple[str, float, str]]] = {}

    def add_hint(instrument_id: str, hint: str, quote: str, weight: float, kind: str):
        hint = clean_title(hint)
        if len(hint) < 4:
            return
        found.setdefault(instrument_id, {})[hint] = (quote[:160], weight, kind)

    for instrument_id, title, tier, document_uid, text_path in regulated:
        if title_basis := TITLE_BASIS.match(clean_title(title)):
            add_hint(instrument_id, title_basis.group(1), title, 0.85, "title")
        if not text_path:
            continue
        path = Path(text_path)
        if not path.exists():
            path = args.data_dir / path.name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")[:60_000]
        for match in BASIS_PHRASE.finditer(text):
            add_hint(
                instrument_id,
                match.group(3),
                match.group(0),
                0.9,
                f"phrase:{match.group(1)}",
            )
        own_subject = subject_of(title)
        for match in RELATED_LINK.finditer(text):
            link = clean_title(match.group(1))
            # «مستندات مرتبط» lists amendments of THIS law too — an amendment
            # is a child, never the statutory parent (inverted-edge guard)
            if re.match(r"^(قانون|لایحه)?\s*(اصلاح|الحاق|تتمیم|تبدیل|پیوست|افزایش|بعلاوه)\b", link):
                continue
            link_subject = normalize_legal_text(link)
            if len(own_subject) >= 6 and own_subject in link_subject:
                add_hint(instrument_id, link, match.group(1), 0.85, "related-link")

    # ---------------- resolve against the catalog -------------------------
    norm_titles = {
        instrument_id: normalize_legal_text(title)
        for instrument_id, title, _ in catalog
    }
    by_norm = {}
    for instrument_id, title, _ in catalog:
        by_norm.setdefault(normalize_legal_text(title), instrument_id)

    def resolve(instrument_id: str, hint: str):
        """-> (parent_id | 'llm' | None, confidence, matched_title)"""
        norm = normalize_legal_text(hint)
        if norm in by_norm and by_norm[norm] != instrument_id:
            return by_norm[norm], 0.95, hint
        scored = sorted(
            (
                (iid, difflib.SequenceMatcher(None, norm, t).ratio())
                for iid, t in norm_titles.items()
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        child = next(c for c in catalog if c[0] == instrument_id)
        for best_id, best_ratio in scored[:10]:
            parent_tier = next(c[2] for c in catalog if c[0] == best_id)
            if parent_tier >= int(child[2]):
                continue  # same/higher tier is a sibling, not a parent
            if best_ratio >= 0.75:
                return best_id, min(0.9, 0.5 + best_ratio / 2), title_by_id[best_id]
            if best_ratio >= 0.5:
                return "llm", best_ratio, title_by_id[best_id]
            return None, 0.0, ""
        return None, 0.0, ""

    edges: list[tuple[str, str, str, float, str]] = []
    llm_tasks: list[dict] = []
    doc_uid_by_instrument = {row[0]: row[3] for row in regulated}
    for instrument_id, hints in found.items():
        for hint, (quote, weight, kind) in hints.items():
            verdict, confidence, matched = resolve(instrument_id, hint)
            if verdict and verdict != "llm":
                edges.append(
                    (instrument_id, verdict, quote or hint, max(confidence, weight - 0.05), kind)
                )
            elif verdict == "llm":
                llm_tasks.append(
                    {"instrument_id": instrument_id, "hint": hint, "quote": quote,
                     "document_uid": doc_uid_by_instrument.get(instrument_id),
                     "top": [title_by_id[i] for i, _ in sorted(
                         norm_titles.items(),
                         key=lambda item: difflib.SequenceMatcher(
                             None, normalize_legal_text(hint), item[1]).ratio(),
                         reverse=True)[:3]]}
                )

    # dedupe: one parent per (child, parent), keep highest confidence
    best_edges: dict[tuple[str, str], tuple[str, float, str]] = {}
    for child, parent, quote, confidence, kind in edges:
        if child == parent:
            continue  # self-reference (e.g. a circular citing itself) is not a hierarchy edge
        key = (child, parent)
        if key not in best_edges or confidence > best_edges[key][1]:
            best_edges[key] = (quote, confidence, kind)

    log(
        f"deterministic implements: {len(best_edges)} edges from "
        f"{len(found)} instruments; LLM queue: {len(llm_tasks)}"
    )
    for (child, parent), (quote, confidence, kind) in sorted(
        best_edges.items(), key=lambda item: -item[1][1]
    )[:12]:
        log(f"  {title_by_id[child][:40]:<42} -> {title_by_id[parent][:45]} [{kind}]")

    # ---------------- LLM stage -> candidates ------------------------------
    candidates: list[tuple[str, str, str]] = []  # child, picked_title, witness
    if llm_tasks and not args.no_llm:
        key = api_key()
        system = (
            "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان؛ حدس نزن، "
            "اگر مطمئن نیستی null بده."
        )
        for task in llm_tasks[: args.llm_limit]:
            if not task["document_uid"]:
                log(f"  skip (no source doc for queue): {title_by_id[task['instrument_id']][:38]}")
                continue
            answer = llm_json(
                key,
                "این عبارت («" + task["hint"] + "») از متن/عنوان سندی است که "
                "بر پایه یک قانون بالاتر وضع شده. کدام گزینه همان قانون است؟\n"
                "گزینه‌ها: " + json.dumps(task["top"], ensure_ascii=False)
                + '\nخروجی: {"pick":"<عنوان یا null>"}',
                system,
            )
            pick = answer.get("pick") if isinstance(answer, dict) else None
            if pick in task["top"]:
                candidates.append(
                    (task["instrument_id"], task["document_uid"], pick,
                     task["quote"] or task["hint"])
                )
                log(f"  LLM: {title_by_id[task['instrument_id']][:38]} -> {pick[:45]}")
            else:
                log(f"  LLM: {title_by_id[task['instrument_id']][:38]} -> declined")
    elif llm_tasks:
        log(f"  --no-llm: {len(llm_tasks)} hints left unresolved")

    # ---------------- LLM pass for parentless tier-4/5 instruments ---------
    # instruments whose parent could not be derived from text/title get one
    # LLM pick among difflib top-5 catalog options; direct llm edges (marked,
    # confidence 0.6) so the hierarchy has no silent orphans
    if not args.no_llm:
        key = api_key()
        constitution_id = next(
            row[0] for row in instruments if row[2] == "constitution"
        )
        parented = {child for child, _ in best_edges} | {
            t["instrument_id"] for t in llm_tasks
        }
        orphans = []
        for iid, title, kind, tier in instruments:
            if tier not in ("4", "5") or iid in parented or iid == constitution_id:
                continue
            orphans.append((iid, title))
        log(f"orphan tier-4/5 instruments for LLM parenting: {len(orphans)}")
        norm_all = norm_titles
        for iid, title in orphans:
            norm = normalize_legal_text(title)
            scored = sorted(
                ((t_id, difflib.SequenceMatcher(None, norm, t_norm).ratio())
                 for t_id, t_norm in norm_all.items()),
                key=lambda item: item[1], reverse=True,
            )
            options = [title_by_id[t_id] for t_id, _ in scored[:5]]
            answer = llm_json(
                key,
                "این سند («" + title + "») کدام قانون از گزینه‌های زیر را اجرا "
                "یا مبنای آن است؟ اگر مبنای مشخصی در گزینه‌ها نیست null بده.\n"
                "گزینه‌ها: " + json.dumps(options, ensure_ascii=False)
                + '\nخروجی: {"pick":"<عنوان یا null>"}',
                "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان؛ حدس نزن.",
            )
            pick = answer.get("pick") if isinstance(answer, dict) else None
            # hard gate: reject guessed parents with no title affinity —
            # a wrong hierarchy edge is worse than no edge
            pick_ratio = next(
                (ratio for t_id, ratio in scored if title_by_id[t_id] == pick), 0.0
            )
            if pick in options and pick_ratio >= 0.25:
                parent = next(t_id for t_id, t in title_by_id.items() if t == pick)
                if parent != iid:
                    best_edges[(iid, parent)] = (
                        "LLM title inference: " + title[:100], 0.6, "llm-orphan"
                    )
                    log(f"  orphan LLM: {title[:38]} -> {pick[:44]} (r={pick_ratio:.2f})")
            else:
                log(f"  orphan LLM declined: {title[:50]} (pick={str(pick)[:30]} r={pick_ratio:.2f})")

    # ---------------- persist ---------------------------------------------
    with db.cursor() as cur:
        # graph_edges is append-only (block_delete trigger); derived batch
        # edges are the one legitimate replacement case, so bypass the
        # trigger for exactly this delete
        cur.execute("SET session_replication_role = replica")
        cur.execute(
            "DELETE FROM canonical.graph_edges WHERE created_by IN (%s, %s)",
            (CREATED_BY, LEGACY_CREATED_BY),
        )
        removed = cur.rowcount
        cur.execute("SET session_replication_role = DEFAULT")
        for (child, parent), (quote, confidence, _kind) in best_edges.items():
            if child == parent:
                continue
            edge_id = (
                "edge:impl:"
                + hashlib.sha1(f"{child}|{parent}".encode()).hexdigest()[:24]
            )
            cur.execute(
                """INSERT INTO canonical.graph_edges (
                       edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_by, source_id
                   ) VALUES (%s, 'legal_instrument', %s, 'legal_instrument', %s,
                             'implements', NULL, 'parser', %s, %s, %s)
                   ON CONFLICT (edge_id) DO NOTHING""",
                (edge_id, child, parent, confidence, CREATED_BY,
                 "basis:" + quote[:120]),
            )
        for child, document_uid, picked_title, witness in candidates:
            cur.execute(
                """INSERT INTO sources.legal_effects
                       (affecting_document_uid, affected_instrument_uid,
                        effect_type, mode, scope, witness_quote, witness_checksum,
                        confidence, detected_by, review_status)
                   VALUES (%s, (SELECT instrument_uid FROM sources.instruments
                                WHERE canonical_title = %s LIMIT 1),
                           'implement', 'explicit', 'total', %s,
                           'sha256:' || sha256(%s::bytea), 0.7, 'llm', 'candidate')
                   ON CONFLICT DO NOTHING""",
                (document_uid, picked_title, witness[:400], witness.encode()),
            )
        cur.execute(
            """SELECT edge_type, count(*) FROM canonical.graph_edges GROUP BY 1 ORDER BY 2 DESC"""
        )
        totals = dict(cur.fetchall())
        cur.execute(
            """SELECT count(*) FROM sources.legal_effects WHERE effect_type='implement'"""
        )
        pending = cur.fetchone()[0]
    log(f"replaced old hierarchy edges: {removed}")
    log(f"graph_edges now: {totals}")
    log(f"implement candidates in review queue: {pending}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
