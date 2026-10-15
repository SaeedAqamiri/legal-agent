"""Quality-cycle core: gap discovery, LLM-as-oracle sample audit, reporting.

Implements the 6-step cycle documented in docs/qavanin/05_quality_cycle.md.
Pure orchestration + DB reads; fixes themselves stay in the deterministic
stages (publish_relations, ingest_source_related) — the LLM here is only an
oracle whose disagreements with the pipeline reveal patterns.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from legal_agent_core.enrichment.related import (
    RULING_NUMBER_RE,
    RULING_RE,
    _fold,
    match_bullet,
    parse_related_block,
    pick_ruling,
)
from legal_agent_core.ingestion.normalization import normalize_legal_text

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"

# قوانین شاخص برای نمونه‌گیری ثابت (ممیزی دستی چرخه صفر)
FAMOUS_LAW_IDS = [
    "inst_703cef9e4fb1e416b9256a2a",  # قانون مجازات اسلامی ۱۳۹۲
    "inst_669041cc2ced283cf582da59",  # قانون کار
    "inst_205abd6eb49739dfbd9909ea",  # قانون مدنی
    "inst_66e907091e2dd2bf1edb5b13",  # قانون تجارت
    "inst_b2452b9eb1e2acb9fd38fa24",  # قانون آیین دادرسی کیفری ۱۳۹۲
    "inst_240f4d7ba84d17b610db12ca",  # قانون امور گمرکی
    "inst_d5ccdbb8d6e5ec47b25b31f9",  # قانون مالیات‌های مستقیم
    "inst_c2826fed98387f652635583c",  # قانون ثبت اسناد و املاک
    "inst_5a182271fbb9a60cb8f7d268",  # قانون مجازات اخلالگران
    "inst_0b08fcd6e16c2154c726df5f",  # قانون مبارزه با قاچاق کالا و ارز
    "inst_8d72cab9c319b0e693e6061e",  # قانون اصلاح قانون مبارزه با مواد مخدر
]


def resolve_llm_key() -> str | None:
    """Same key resolution as scripts/publish_relations.py."""
    import os

    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if key:
        return key
    api_txt = Path("~/api.txt").expanduser()
    if api_txt.exists():
        match = re.search(
            r'"zai".*?"apiKey":\s*"([^"]+)"',
            api_txt.read_text(encoding="utf-8", errors="replace"),
            re.DOTALL,
        )
        if match:
            return match.group(1).strip()
    return None


def llm_json(key: str, prompt: str, system: str) -> dict | list:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
    ).encode()
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"LLM call failed: {type(exc).__name__}: {str(exc)[:120]}") from exc
    content = payload["choices"][0]["message"]["content"]
    block = JSON_BLOCK_RE.search(content)
    return json.loads(block.group(1) if block else content)


# ---------------------------------------------------------------- gap discovery

@dataclass
class GapReport:
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    curated: dict = field(default_factory=dict)
    refs: dict = field(default_factory=dict)
    rulings: dict = field(default_factory=dict)
    absent_laws: list = field(default_factory=list)
    baselines: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "curated_block": self.curated,
            "explicit_references": self.refs,
            "rulings": self.rulings,
            "absent_laws": self.absent_laws,
            "baselines": self.baselines,
        }


def collect_gaps(conn, progress=None, progress_every: int = 250) -> GapReport:
    """Unified gap inventory across the four signal sources.
    `progress(label)` fires at least every `progress_every` docs so long runs
    never look hung."""
    report = GapReport()
    catalog_by_key, sorted_titles = _load_catalog(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, canonical_title FROM canonical.legal_instruments")
        id_title = dict(cur.fetchall())
        cur.execute(
            """SELECT source_node_id, target_node_id, edge_type FROM canonical.graph_edges
               WHERE source_node_type='legal_instrument' AND target_node_type='legal_instrument'"""
        )
        pair_edges: dict[tuple[str, str], set[str]] = {}
        for s, t, et in cur.fetchall():
            pair_edges.setdefault((s, t), set()).add(et)
            pair_edges.setdefault((t, s), set()).add(et)
        cur.execute(
            """SELECT i.instrument_id, i.canonical_title, sd.document_uid, sd.text_path
               FROM canonical.legal_instruments i
               JOIN sources.instruments si ON si.canonical_key = i.canonical_title
               JOIN sources.source_documents sd ON sd.instrument_uid = si.instrument_uid
               WHERE sd.text_path IS NOT NULL ORDER BY i.instrument_id"""
        )
        docs = cur.fetchall()
        cur.execute(
            """SELECT document_uid, title FROM sources.source_documents
               WHERE source_id='cats' AND doc_kind IN ('judgment', 'unknown')"""
        )
        idx: dict[str, list[tuple[str, str, str]]] = {}
        for uid, title in cur.fetchall():
            m = RULING_NUMBER_RE.search(normalize_legal_text(title or ""))
            if m:
                idx.setdefault(m.group(1), []).append((str(uid), "judgment", title or ""))

    curated_counter: Counter = Counter()
    absent_counter: Counter = Counter()
    curated_detail: list[dict] = []
    for doc_index, (iid, _title, document_uid, text_path) in enumerate(docs, 1):
        if progress and (doc_index == 1 or doc_index % progress_every == 0):
            progress(f"docs {doc_index}/{len(docs)} | bullets={curated_counter.get('bullets', 0)} "
                     f"edged={curated_counter.get('edged', 0)} absent={curated_counter.get('absent', 0)} "
                     f"rulings={curated_counter.get('rulings', 0)}")
        try:
            bullets = parse_related_block(Path(text_path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not bullets:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT target_node_id FROM canonical.graph_edges
                   WHERE (edge_type='related_to' AND source_node_id=%s)
                      OR (edge_type='explicitly_references'
                          AND source_node_type='provision' AND target_node_id=%s)""",
                (iid, iid),
            )
            related = {str(r[0]) for r in cur.fetchall()}
            cur.execute(
                "SELECT DISTINCT source_node_id FROM canonical.graph_edges WHERE edge_type='cites' AND target_node_id=%s",
                (iid,),
            )
            cited = {str(r[0]) for r in cur.fetchall()}
        for bullet in bullets:
            folded = _fold(bullet)
            if RULING_RE.match(folded):
                curated_counter["rulings"] += 1
                m = RULING_NUMBER_RE.search(normalize_legal_text(bullet))
                if not m:
                    curated_counter["rulings_no_number"] += 1
                    continue
                cands = pick_ruling(bullet, idx.get(m.group(1), []))
                if len(cands) == 1 and cands[0][0] in cited:
                    curated_counter["rulings_edged"] += 1
                elif len(cands) <= 1:
                    curated_counter["rulings_absent"] += 1
                else:
                    curated_counter["rulings_ambiguous"] += 1
                continue
            if folded.startswith("نظریه"):
                curated_counter["advisory_skipped"] += 1
                continue
            curated_counter["bullets"] += 1
            target = match_bullet(bullet, catalog_by_key, sorted_titles)
            if not target:
                curated_counter["absent"] += 1
                absent_counter[normalize_legal_text(bullet)[:60]] += 1
                curated_detail.append({"instrument": iid, "status": "absent", "bullet": bullet[:120]})
            elif target == iid:
                curated_counter["self_skipped"] += 1
            elif target in related or pair_edges.get((iid, target)):
                curated_counter["edged"] += 1
            else:
                curated_counter["matched_no_edge"] += 1
                curated_detail.append(
                    {"instrument": iid, "status": "matched_no_edge",
                     "bullet": bullet[:120], "target": id_title.get(target, target)}
                )
    report.curated = {"counts": dict(curated_counter), "detail": curated_detail[:200]}
    report.absent_laws = sorted(
        ({"title_norm": k, "count": v} for k, v in absent_counter.items()),
        key=lambda r: -r["count"],
    )[:50]

    with conn.cursor() as cur:
        cur.execute(
            """SELECT resolution_status, count(*) FROM canonical.explicit_references
               GROUP BY 1 ORDER BY 2 DESC"""
        )
        report.refs["by_status"] = dict(cur.fetchall())
        cur.execute(
            """SELECT count(*) FROM canonical.graph_edges WHERE created_by='refs-invalidated'"""
        )
        report.refs["invalidated_edges"] = cur.fetchone()[0]
        cur.execute(
            """SELECT target_text, count(*) FROM canonical.explicit_references
               WHERE resolution_status='unresolved' AND target_text !~ '^ماده\\s*[۰-۹0-9]+(\\s+این\\s+[^\\s]+)?$'
               GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""
        )
        report.refs["top_unresolved_named"] = [{"text": r[0][:90], "count": r[1]} for r in cur.fetchall()]

    report.rulings = {
        "total": curated_counter.get("rulings", 0),
        "edged": curated_counter.get("rulings_edged", 0),
        "absent_from_corpus": curated_counter.get("rulings_absent", 0),
        "ambiguous": curated_counter.get("rulings_ambiguous", 0),
    }
    return report


def _load_catalog(conn) -> tuple[dict[str, str], list[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, canonical_title, title FROM canonical.legal_instruments")
        rows = cur.fetchall()
    by_key: dict[str, str] = {}
    for instrument_id, canonical_title, title in rows:
        for name in (canonical_title, title):
            key = normalize_legal_text(name or "")
            if key:
                by_key.setdefault(key, instrument_id)
    return by_key, sorted(by_key, key=len, reverse=True)


# ------------------------------------------------------------- LLM sample audit

@dataclass
class AuditFinding:
    instrument_id: str
    provision_id: str
    kind: str  # 'missed_by_pipeline' | 'missed_by_llm' | 'target_disagreement'
    detail: dict


def sample_audit(conn, key: str, instrument_ids: list[str], per_law: int = 30,
                 progress=None) -> dict:
    """LLM-as-oracle audit: extract cross-law references from sampled provisions
    with the LLM and diff against what the deterministic pipeline recorded."""
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, canonical_title FROM canonical.legal_instruments")
        id_title = dict(cur.fetchall())

    findings: list[AuditFinding] = []
    stats = {"laws": 0, "provisions": 0, "llm_refs": 0, "pipeline_refs": 0,
             "missed_by_pipeline": 0, "missed_by_llm": 0}
    for iid in instrument_ids:
        title = id_title.get(iid, iid)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.provision_id, p.number, pv.normalized_text
                   FROM canonical.provisions p
                   JOIN canonical.provision_versions pv ON pv.provision_id = p.provision_id
                   WHERE p.instrument_id = %s
                     AND pv.provision_version_id IN (
                         SELECT max(provision_version_id) FROM canonical.provision_versions
                         GROUP BY provision_id)
                   ORDER BY p.ordinal LIMIT %s""",
                (iid, per_law),
            )
            provisions = cur.fetchall()
            known_by_provision: dict[str, list[str]] = {}
            cur.execute(
                """SELECT pv.provision_id, er.target_text FROM canonical.explicit_references er
                   JOIN canonical.provision_versions pv ON pv.provision_version_id = er.source_provision_version_id
                   WHERE pv.provision_id = ANY(%s)""",
                ([p[0] for p in provisions],),
            )
            for prov_id, text in cur.fetchall():
                known_by_provision.setdefault(prov_id, []).append(normalize_legal_text(text))

        stats["pipeline_refs"] += sum(len(v) for v in known_by_provision.values())
        # batch provisions for the oracle
        batch = [provisions[i:i + 6] for i in range(0, len(provisions), 6)]
        for chunk_index, chunk in enumerate(batch, 1):
            if progress and (chunk_index == 1 or chunk_index % 5 == 0):
                progress(f"  {title[:35]} | provisions {min(chunk_index * 6, len(provisions))}/{len(provisions)} "
                         f"| missed_by_pipeline={stats['missed_by_pipeline']}")
            pieces = [f"ماده {n or '?'}: {t}" for _pid, n, t in chunk]
            prompt = (
                f"متن زیر موادهایی از «{title}» است. همه ارجاع‌های صریح آن به "
                "قوانین/آیین‌نامه/مصوبه‌های دیگر را فهرست کن (ارجاع به همین قانون را نیاور). "
                "اگر شماره ماده مقصد معلوم است بنویس.\n"
                'خروجی: {"refs":[{"law":"نام قانون مقصد","article":"شماره یا null"}]}\n\n'
                + "\n\n".join(pieces)
            )
            try:
                payload = llm_json(key, prompt, "تو کارشناس حقوق ایران هستی. فقط JSON معتبر برگردان.")
            except RuntimeError:
                continue
            refs = payload.get("refs", []) if isinstance(payload, dict) else []
            stats["llm_refs"] += len(refs)
            for (_pid, number, _text) in chunk:
                stats["provisions"] += 1
                known = known_by_provision.get(_pid, [])
                for ref in refs:
                    law_name = str(ref.get("law") or "").strip()
                    if not law_name:
                        continue
                    law_norm = normalize_legal_text(law_name)
                    # does the pipeline have any mention naming this law for this provision?
                    hit = any(law_norm[:25] in k for k in known)
                    if not hit:
                        stats["missed_by_pipeline"] += 1
                        findings.append(AuditFinding(
                            iid, _pid, "missed_by_pipeline",
                            {"law": law_name, "article": ref.get("article"),
                             "provision": f"ماده {number}"},
                        ))
        stats["laws"] += 1
        if progress:
            progress(title, stats)
    return {"stats": stats, "findings": [
        {"instrument": f.instrument_id, "provision": f.provision_id,
         "kind": f.kind, **f.detail}
        for f in findings
    ]}


# ------------------------------------------------------------------- reporting

def write_report(directory: Path, gap_report: GapReport | None = None,
                 audit: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    path = directory / f"cycle-{stamp}.json"
    payload = {
        "gaps": gap_report.to_dict() if gap_report else None,
        "llm_audit": audit,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    md = directory / f"cycle-{stamp}.md"
    lines = [f"# چرخه کیفیت — {stamp}", ""]
    if gap_report:
        c = gap_report.curated.get("counts", {})
        lines += [
            "## بلوک کیوریت‌شده",
            (f"- بولت: {c.get('bullets', 0)} | یال‌دار: {c.get('edged', 0)} | "
             f"مچ-بی‌یال: {c.get('matched_no_edge', 0)} | خارج از کورپس: {c.get('absent', 0)}"),
            f"- رأی‌ها: {gap_report.rulings}",
            "## ارجاع‌های صریح",
            f"- وضعیت‌ها: {gap_report.refs.get('by_status')}",
            f"- یال‌های invalidated: {gap_report.refs.get('invalidated_edges')}",
            "",
        ]
    if audit:
        s = audit.get("stats", {})
        lines += [
            "## داوری LLM",
            (f"- قوانین: {s.get('laws')} | مواد: {s.get('provisions')} | "
             f"ارجاع LLM: {s.get('llm_refs')} | ارجاع pipeline: {s.get('pipeline_refs')}"),
            f"- از قلم pipeline: {s.get('missed_by_pipeline')} | موارد مشکوک pipeline: {s.get('missed_by_llm')}",
            "",
        ]
    md.write_text("\n".join(lines), encoding="utf-8")
    return path
