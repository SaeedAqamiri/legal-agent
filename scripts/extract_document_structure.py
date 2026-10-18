#!/usr/bin/env python3
"""Extract in-document structure (جلد/کتاب/باب/فصل/مبحث) into canonical.

Docs/qavanin/06_law_notebook.md §5. The markdown corpus carries container
headings («### کتاب اول- …», «#### باب اول- …») but the parser only recognized
بخش/فصل/ماده/تبصره, so big codes (e.g. قانون مدنی, 1533 articles) were stored
flat. This script re-derives the containers deterministically from the source
markdown and reparents the existing articles — WITHOUT changing any article id
or any existing edge.

Ordinals are renumbered per instrument with gaps (old*10+5) so container rows
get their own ordinal slot just before their first descendant article; this
keeps parents-before-children in document order. Idempotent: an instrument that
already has extracted containers (human_key LIKE 'struct:%') is skipped.

Dry-run by default; pass --apply to write. Usage:
    .venv/bin/python scripts/extract_document_structure.py --instrument ID --apply
    .venv/bin/python scripts/extract_document_structure.py --limit 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@127.0.0.1:5432/legal_agent"
CREATED_FROM = "structure-extractor"

_ORD = r"(?:اول|دوم|سوم|چهارم|پنجم|ششم|هفتم|هشتم|نهم|دهم|یازدهم|دوازدهم|سیزدهم|چهاردهم|پانزدهم|شانزدهم|هفدهم|هجدهم|نوزدهم|بیستم|سی‌ام)"
CONTAINER = re.compile(
    r"^#{1,6}\s*(?P<kind>جلد|کتاب|باب|فصل|مبحث|قسمت|گفتار)\s+"
    r"(?P<num>[0-9۰-۹]+|" + _ORD + r")\s*[-–—:.]?\s*(?P<title>.*)$"
)
ARTICLE = re.compile(
    r"^\s*(?P<label>ماده\s+(?:واحده|واحدی|[0-9۰-۹]+|" + _ORD + r"))"
)

# nesting rank: جلد > کتاب > باب/قسمت > فصل > مبحث > گفتار (distinct from
# provision_type which only has part/chapter). Mirrors Iranian code structure.
_KIND_RANK = {
    "جلد": 1,
    "کتاب": 2,
    "باب": 3,
    "قسمت": 3,
    "فصل": 4,
    "مبحث": 5,
    "گفتار": 6,
}
_PART_KINDS = {"جلد", "کتاب"}
_RANK_PART = 1
_RANK_CHAPTER = 2
_RANK_ARTICLE = 3

_ZWNJ = re.compile(r"[\u200c\u200e\u200f]+")


def norm_label(text: str) -> str:
    cleaned = _ZWNJ.sub(" ", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip("-–—.: ").strip()


@dataclass
class Container:
    kind: str
    num: str
    title: str
    rank: int
    ordinal: int = -1
    provision_id: str = ""
    parent_id: str | None = None
    first_article_ordinal: int | None = None


@dataclass
class Plan:
    containers: list[Container] = field(default_factory=list)
    # provision_id -> new parent id (articles/containers)
    parent_updates: dict[str, str | None] = field(default_factory=dict)
    # provision_id -> new ordinal
    ordinal_updates: dict[str, int] = field(default_factory=dict)
    matched_articles: int = 0
    total_articles: int = 0
    md_article_tokens: int = 0


def parse_tokens(md_text: str, instrument_id: str):
    """Yield ('container', Container) / ('article', normalized_label) in order."""
    for line in md_text.splitlines():
        container = CONTAINER.match(line)
        if container:
            kind = container.group("kind")
            num = container.group("num")
            title = container.group("title").strip()[:200]
            rank = _KIND_RANK.get(kind, _RANK_CHAPTER)
            # id MUST include the instrument: identical chapter names in
            # different laws must never collide (docs 06 §5).
            raw = f"{instrument_id}|{kind}|{num}|{rank}|{title}"
            digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
            yield (
                "container",
                Container(
                    kind=kind,
                    num=num,
                    title=title,
                    rank=rank,
                    provision_id=f"prov_struct_{digest}",
                ),
            )
            continue
        article = ARTICLE.match(line)
        if article:
            yield "article", norm_label(article.group("label"))


def plan_structure(provisions, md_text, instrument_id: str) -> Plan:
    """provisions: list of (provision_id, label, provision_type, ordinal, parent_id)."""
    plan = Plan()
    by_label: dict[str, list[tuple[str, int]]] = {}
    article_rows = []
    # keys already occupied at container-INSERT time: existing provisions still
    # sit under their OLD parent (reparenting happens after the inserts), so
    # container ordinals must avoid these too.
    occupied: set[tuple[str | None, int]] = set()
    for pid, label, ptype, ordinal, parent_id in provisions:
        by_label.setdefault(norm_label(label), []).append((pid, ordinal))
        occupied.add((parent_id, (ordinal + 1) * 100 + 50))
        if ptype == "article":
            article_rows.append((pid, norm_label(label), ordinal))
    plan.total_articles = len(article_rows)

    # walk tokens, tracking open containers and their first article
    stack: list[Container] = []
    for kind, payload in parse_tokens(md_text, instrument_id):
        if kind == "container":
            container: Container = payload
            while stack and stack[-1].rank >= container.rank:
                stack.pop()
            container.parent_id = stack[-1].provision_id if stack else None
            plan.containers.append(container)
            stack.append(container)
            continue
        # article
        plan.md_article_tokens += 1
        candidates = by_label.get(payload)
        if not candidates:
            continue
        pid, old_ordinal = candidates.pop(0)
        plan.matched_articles += 1
        # first article for every still-unassigned open container
        for container in stack:
            if container.first_article_ordinal is None:
                container.first_article_ordinal = old_ordinal
        if stack:
            plan.parent_updates[pid] = stack[-1].provision_id

    # assign container ordinals (innermost nearest its article)
    by_id = {c.provision_id: c for c in plan.containers}
    trailing = 0
    for container in plan.containers:
        fa = container.first_article_ordinal
        if fa is None:
            # trailing empty container -> park past the end, unique per row
            trailing += 1
            container.ordinal = 10**9 + trailing
            continue
        # depth of container chain sharing this fa (count ancestors with same fa)
        slot = 1
        parent = by_id.get(container.parent_id) if container.parent_id else None
        while parent is not None and parent.first_article_ordinal == fa:
            slot += 1
            parent = by_id.get(parent.parent_id) if parent.parent_id else None
        container.ordinal = (fa + 1) * 100 - slot
    # de-duplicate ordinals within the same parent (unique constraint) against
    # both other containers and the still-in-place existing provisions
    seen: set[tuple[str | None, int]] = set(occupied)
    for container in plan.containers:
        key = (container.parent_id, container.ordinal)
        while key in seen:
            container.ordinal = container.ordinal + 1 if container.ordinal <= 0 else container.ordinal - 1
            key = (container.parent_id, container.ordinal)
        seen.add(key)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LAWS_DATA_DIR", "/home/saeed/laws-mcp/data")))
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-coverage", type=float, default=0.9)
    parser.add_argument("--min-articles", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("out/document_structure_report.json"))
    args = parser.parse_args()

    db = psycopg.connect(args.dsn)
    with db.cursor() as cur:
        where = "AND i.instrument_id = %s" if args.instrument else ""
        params = (args.instrument,) if args.instrument else ()
        cur.execute(
            f"""SELECT i.instrument_id, i.title, d.text_path
                  FROM canonical.legal_instruments i
                  JOIN sources.instruments si ON si.canonical_key = i.canonical_title
                  JOIN sources.source_documents d ON d.instrument_uid = si.instrument_uid
                 WHERE d.text_path IS NOT NULL {where}
                 ORDER BY i.instrument_id""",
            params,
        )
        instruments = cur.fetchall()
        if args.limit:
            instruments = instruments[: args.limit]

    report = []
    applied = 0
    for instrument_id, title, text_path in instruments:
        path = Path(text_path)
        if not path.exists():
            path = args.data_dir / path.name
        if not path.exists():
            continue
        with db.cursor() as cur:
            cur.execute(
                """SELECT provision_id, label, provision_type, ordinal, parent_provision_id
                     FROM canonical.provisions WHERE instrument_id = %s
                    ORDER BY ordinal, provision_id""",
                (instrument_id,),
            )
            provisions = cur.fetchall()
            cur.execute(
                "SELECT 1 FROM canonical.provisions WHERE instrument_id = %s AND human_key LIKE 'struct:%%' LIMIT 1",
                (instrument_id,),
            )
            already = cur.fetchone() is not None
        if already:
            continue
        md_text = path.read_text(encoding="utf-8", errors="replace")
        plan = plan_structure(provisions, md_text, instrument_id)
        coverage = (
            plan.matched_articles / plan.md_article_tokens
            if plan.md_article_tokens
            else 0.0
        )
        entry = {
            "instrument_id": instrument_id,
            "title": title,
            "articles": plan.total_articles,
            "md_tokens": plan.md_article_tokens,
            "matched": plan.matched_articles,
            "containers": len(plan.containers),
            "coverage": round(coverage, 3),
            "applied": False,
        }
        if (
            not plan.containers
            or coverage < args.min_coverage
            or plan.matched_articles < args.min_articles
        ):
            entry["skipped"] = "no containers or low coverage/coverage too small"
            report.append(entry)
            continue
        if args.apply:
            try:
                _apply(db, instrument_id, provisions, plan)
                db.commit()
                entry["applied"] = True
                applied += 1
            except Exception as exc:  # noqa: BLE001 - one bad doc must not stop the batch
                db.rollback()
                entry["error"] = str(exc)[:200]
        report.append(entry)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"instruments scanned: {len(report)}, applied: {applied}, report -> {args.report}")
    for entry in report[:10]:
        print(
            f"  {entry['title'][:45]:47s} articles={entry['articles']} "
            f"matched={entry['matched']} containers={entry['containers']} "
            f"cov={entry['coverage']} {'APPLIED' if entry['applied'] else entry.get('skipped','dry')}"
        )
    return 0


def _apply(db, instrument_id, provisions, plan: Plan) -> None:
    """Write containers + reparent + renumber + recompute depth.

    The ``validate_provision_hierarchy`` trigger enforces depth == parent+1 on
    every write; reparenting articles would violate it mid-batch (their notes
    still carry old depths), so the trigger is disabled for the batch
    (``session_replication_role=replica``, superuser) and depths are recomputed
    from the final parent links with a recursive walk afterwards.
    """
    with db.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        # renumber every existing provision with gaps: (old+1)*100 + 50
        cur.execute(
            "UPDATE canonical.provisions SET ordinal = (ordinal + 1) * 100 + 50 WHERE instrument_id = %s",
            (instrument_id,),
        )
        # insert containers (outer before inner => correct depth)
        for container in plan.containers:
            label = f"{container.kind} {container.num}"
            cur.execute(
                """INSERT INTO canonical.provisions
                       (provision_id, instrument_id, provision_type, number, label,
                        title, parent_provision_id, ordinal, depth, human_key, schema_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, 1)
                   ON CONFLICT (provision_id) DO NOTHING""",
                (
                    container.provision_id,
                    instrument_id,
                    "part" if container.rank <= 2 else "chapter",
                    container.num,
                    label,
                    container.title or None,
                    container.parent_id,
                    container.ordinal,
                    "struct:" + container.provision_id,
                ),
            )
        # reparent articles
        for pid, parent_id in plan.parent_updates.items():
            cur.execute(
                "UPDATE canonical.provisions SET parent_provision_id = %s WHERE provision_id = %s",
                (parent_id, pid),
            )
        # recompute depth for the whole instrument from final parent links
        cur.execute(
            """WITH RECURSIVE tree AS (
                   SELECT provision_id, 0 AS depth
                     FROM canonical.provisions
                    WHERE instrument_id = %s AND parent_provision_id IS NULL
                   UNION ALL
                   SELECT p.provision_id, t.depth + 1
                     FROM canonical.provisions p
                     JOIN tree t ON p.parent_provision_id = t.provision_id
                    WHERE p.instrument_id = %s
               )
               UPDATE canonical.provisions p
                  SET depth = t.depth
                 FROM tree t
                WHERE p.provision_id = t.provision_id""",
            (instrument_id, instrument_id),
        )
        cur.execute("SET session_replication_role = DEFAULT")


if __name__ == "__main__":
    raise SystemExit(main())
