"""FalkorDB mirror of the canonical laws graph (docs/qavanin/01_graph_schema.md).

Projects canonical instruments, provisions and graph_edges into the shared
LAWS_GRAPH_ID graph so Cypher can be used for legal queries:

  (:Authority {name})-[:ISSUES]->(:LegalInstrument {id, title, kind, tier,
      issuer, provisions, summary})
  (:LegalInstrument)-[:CONTAINS]->(:Provision {id, instrument_id, label,
      type, number, ordinal, summary})
  (:Provision)-[:PART_OF]->(:Provision)
  plus every canonical.graph_edges row as an UPPER_SNAKE rel type
  (EXPLICITLY_REFERENCES, AMENDS, REPEALS, IMPLEMENTS with authority_basis...).

(:Judgment) / (:AdvisoryOpinion) labels are reserved for phase 3 (arâ-ye
vahdat-e ravie / divan-e edalat / mashverati) — nothing creates them yet.
Full rebuild each run (DETACH DELETE + bulk UNWIND); the mirror lags Postgres
until sync_laws_mirror() runs — composition starts it in a background thread
at boot and POST /v1/organizations/{org}/graph/sync triggers it manually.
"""

from __future__ import annotations

import time
from collections import defaultdict

from .adapters.falkordb import LAWS_GRAPH_ID  # re-exported for convenience

__all__ = ["LAWS_GRAPH_ID", "sync_laws_mirror"]

_LABEL_FOR_NODE_TYPE = {
    "legal_instrument": "LegalInstrument",
    "provision": "Provision",
}


def _log(message: str) -> None:
    print(f"[laws-mirror {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _sync_judgments(connection, falkor_graph, batch_size: int, log) -> None:
    """Create (:Judgment)/(:AdvisoryOpinion) nodes from the staged cats corpus
    and wire CITES edges to laws cited in their LLM extraction outputs.

    Citation targets are matched to canonical titles deterministically
    (normalized substring / difflib >= 0.75) at mirror time — no LLM here.
    """
    import difflib
    import json

    from .ingestion.normalization import normalize_legal_text

    with connection.cursor() as cur:
        cur.execute(
            """SELECT document_uid, external_id, title, doc_kind, metadata->>'cat',
                      metadata->>'summary'
                 FROM sources.source_documents
                WHERE source_id='cats' AND doc_kind IN ('judgment','unknown')"""
        )
        docs = cur.fetchall()
        cur.execute("SELECT instrument_id, title FROM canonical.legal_instruments")
        catalog = cur.fetchall()
        cur.execute(
            """SELECT j.document_uid, j.output
                 FROM sources.extraction_jobs j
                WHERE j.stage='reference_resolution' AND j.status='succeeded'"""
        )
        jobs = cur.fetchall()

    norm_titles = {iid: normalize_legal_text(t) for iid, t in catalog}
    title_tokens = {iid: set(norm.split()) for iid, norm in norm_titles.items()}
    docs_by_uid = {str(row[0]): row for row in docs}
    target_cache: dict[str, str | None] = {}
    exact_titles = {norm: iid for iid, norm in norm_titles.items()}

    def resolve_target(title: str) -> str | None:
        """Cached: ~48k citations reference ~13k distinct titles.

        Shortlist by token Jaccard (cheap set ops) then gate with difflib;
        a full 608-title difflib scan per title cost ~9 minutes per sync.
        """
        key = normalize_legal_text(title or "")
        if key in target_cache:
            return target_cache[key]
        result: str | None = None
        if len(key) >= 6:
            result = exact_titles.get(key)
            if result is None:
                for iid, t_norm in norm_titles.items():
                    if (key in t_norm or t_norm in key) and abs(len(key) - len(t_norm)) < 60:
                        result = iid
                        break
            if result is None:
                key_tokens = set(key.split())
                scored = sorted(
                    (
                        (len(key_tokens & tokens) / max(1, len(key_tokens | tokens)), iid)
                        for iid, tokens in title_tokens.items()
                    ),
                    reverse=True,
                )
                shortlist = [iid for ratio, iid in scored[:30] if ratio > 0]
                if shortlist:
                    best = max(
                        ((iid, difflib.SequenceMatcher(None, key, norm_titles[iid]).ratio())
                         for iid in shortlist),
                        key=lambda item: item[1],
                    )
                    result = best[0] if best[1] >= 0.75 else None
        target_cache[key] = result
        return result

    log("judgments: resolving citation targets...")
    judgments: dict[str, list] = {}
    cite_pairs: dict[tuple[str, str], int] = {}
    for document_uid, output in jobs:
        doc = docs_by_uid.get(str(document_uid))
        if doc is None:
            continue
        try:
            data = json.loads(output) if isinstance(output, str) else output
        except (TypeError, ValueError):
            continue
        _, external_id, title, doc_kind, category, summary = doc
        label = "AdvisoryOpinion" if (doc_kind != "judgment" or "نظریه" in (title or "")) else "Judgment"
        judgments[str(document_uid)] = [str(document_uid), (title or "")[:300], label, (category or "")[:60], summary]
        for citation in data.get("citations", []) + data.get("basis", []):
            target = resolve_target(str(citation.get("target_title", "")))
            if target:
                key = (str(document_uid), target)
                cite_pairs[key] = cite_pairs.get(key, 0) + 1

    if not judgments:
        log("judgments: no staged judicial docs with extraction output yet")
        return

    def run_unwind(template: str, rows: list) -> None:
        for start in range(0, len(rows), batch_size):
            falkor_graph.query(template, {"rows": rows[start : start + batch_size]})

    run_unwind(
        """UNWIND $rows AS row
           MERGE (j:Judgment {id: row[0]})
           SET j.title = row[1], j.category = row[3], j.summary = row[4]""",
        [row for row in judgments.values() if row[2] == "Judgment"],
    )
    run_unwind(
        """UNWIND $rows AS row
           MERGE (j:AdvisoryOpinion {id: row[0]})
           SET j.title = row[1], j.category = row[3], j.summary = row[4]""",
        [row for row in judgments.values() if row[2] == "AdvisoryOpinion"],
    )
    run_unwind(
        """UNWIND $rows AS row
           MATCH (j {id: row[0]}), (t:LegalInstrument {id: row[1]})
           MERGE (j)-[c:CITES]->(t)
           SET c.hits = row[2]""",
        [[k[0], k[1], v] for k, v in cite_pairs.items()],
    )
    log(
        f"judgments: {len(judgments)} nodes, {len(cite_pairs)} CITES pairs wired"
    )


def sync_laws_mirror(connection, falkor_graph, batch_size: int = 2000, log=_log) -> dict:
    """Rebuild the laws projection. `connection` is a psycopg connection on
    the canonical schema; `falkor_graph` the FalkorDB graph handle."""
    started = time.time()
    with connection.cursor() as cur:
        cur.execute(
            """SELECT i.instrument_id, i.title, i.instrument_type, i.authority_level,
                      i.issuer,
                      (SELECT count(*) FROM canonical.provisions p
                        WHERE p.instrument_id = i.instrument_id),
                      (SELECT min(d.source_uri) FROM sources.source_documents d
                        JOIN sources.instruments si ON si.instrument_uid = d.instrument_uid
                        WHERE si.canonical_key = i.canonical_title
                          AND d.source_uri IS NOT NULL),
                      i.subject_domain,
                      (SELECT s.summary FROM canonical.instrument_summaries s
                        WHERE s.instrument_id = i.instrument_id)
                 FROM canonical.legal_instruments i ORDER BY instrument_id"""
        )
        instruments = cur.fetchall()
        cur.execute(
            """SELECT p.provision_id, p.instrument_id, p.label, p.provision_type,
                      p.number, p.ordinal, p.parent_provision_id,
                      s.summary
                 FROM canonical.provisions p
                 LEFT JOIN canonical.provision_summaries s
                   ON s.provision_id = p.provision_id
                ORDER BY p.instrument_id, p.ordinal"""
        )
        provisions = cur.fetchall()
        cur.execute(
            """SELECT source_node_type, source_node_id, target_node_type,
                      target_node_id, edge_type, confidence, source_id
                 FROM canonical.graph_edges"""
        )
        edges = cur.fetchall()
    log(
        f"postgres: {len(instruments)} instruments, {len(provisions)} provisions, "
        f"{len(edges)} graph_edges"
    )

    falkor_graph.query("MATCH (n) DETACH DELETE n")
    for statement in (
        "CREATE INDEX FOR (i:LegalInstrument) ON (i.id)",
        "CREATE INDEX FOR (i:LegalInstrument) ON (i.tier)",
        "CREATE INDEX FOR (p:Provision) ON (p.id)",
        "CREATE INDEX FOR (a:Authority) ON (a.name)",
        "CREATE INDEX FOR (d:LegalDomain) ON (d.name)",
    ):
        try:
            falkor_graph.query(statement)
        except Exception as exc:  # noqa: BLE001 — indexes persist across rebuilds
            if "already indexed" in str(exc).lower():
                continue
            raise

    def run_unwind(template: str, rows: list, desc: str) -> None:
        for start in range(0, len(rows), batch_size):
            falkor_graph.query(template, {"rows": rows[start : start + batch_size]})
        log(f"  {desc}: {len(rows)} written")

    run_unwind(
        """UNWIND $rows AS row
           CREATE (:LegalInstrument {id: row[0], title: row[1], kind: row[2],
                                     tier: row[3], issuer: row[4],
                                     provisions: row[5], url: row[6],
                                     domain: row[7], summary: row[8]})""",
        [list(row) for row in instruments],
        "instruments",
    )
    run_unwind(
        """UNWIND $rows AS row
           MERGE (d:LegalDomain {name: row[1]})
           MERGE (i:LegalInstrument {id: row[0]})
           MERGE (i)-[:BELONGS_TO_DOMAIN]->(d)""",
        [[row[0], row[7]] for row in instruments if row[7]],
        "BELONGS_TO_DOMAIN",
    )
    run_unwind(
        """UNWIND $rows AS row
           CREATE (:Provision {id: row[0], instrument_id: row[1], label: row[2],
                               type: row[3], number: row[4], ordinal: row[5],
                               summary: row[7]})""",
        [list(row) for row in provisions],
        "provisions",
    )
    run_unwind(
        """UNWIND $rows AS row
           MATCH (i:LegalInstrument {id: row[0]}), (p:Provision {id: row[1]})
           CREATE (i)-[:CONTAINS]->(p)""",
        [[row[1], row[0]] for row in provisions],
        "CONTAINS",
    )
    run_unwind(
        """UNWIND $rows AS row
           MATCH (c:Provision {id: row[0]}), (p:Provision {id: row[1]})
           CREATE (c)-[:PART_OF]->(p)""",
        [[row[0], row[6]] for row in provisions if row[6]],
        "PART_OF",
    )
    run_unwind(
        """UNWIND $rows AS row
           MERGE (a:Authority {name: row[1]})
           MERGE (i:LegalInstrument {id: row[0]})
           MERGE (a)-[:ISSUES]->(i)""",
        [[row[0], row[4]] for row in instruments if row[4]],
        "ISSUES",
    )

    by_signature: dict[tuple[str, str, str], list] = defaultdict(list)
    unmapped = 0
    for source_type, source_id, target_type, target_id, edge_type, confidence, provenance in edges:
        source_label = _LABEL_FOR_NODE_TYPE.get(source_type)
        target_label = _LABEL_FOR_NODE_TYPE.get(target_type)
        if not source_label or not target_label:
            unmapped += 1
            continue
        basis = (
            provenance.split(":", 1)[1]
            if edge_type == "implements" and provenance and provenance.startswith("basis:")
            else None
        )
        by_signature[(edge_type, source_label, target_label)].append(
            [source_id, target_id, confidence, basis]
        )
    if unmapped:
        log(f"skipped {unmapped} edges with unknown node types")
    for (edge_type, source_label, target_label), rows in sorted(by_signature.items()):
        run_unwind(
            f"""UNWIND $rows AS row
                MATCH (s:{source_label} {{id: row[0]}}), (t:{target_label} {{id: row[1]}})
                CREATE (s)-[:{edge_type.upper()} {{confidence: row[2], authority_basis: row[3]}}]->(t)""",
            rows,
            edge_type.upper(),
        )

    _sync_judgments(connection, falkor_graph, batch_size, log)

    node_counts = dict(
        falkor_graph.query(
            """MATCH (n) RETURN labels(n)[0], count(*) ORDER BY count(*) DESC"""
        ).result_set
    )
    rel_counts = dict(
        falkor_graph.query(
            """MATCH ()-[r]->() RETURN type(r), count(*) ORDER BY count(*) DESC"""
        ).result_set
    )
    result = {
        "nodes": {str(k): v for k, v in node_counts.items()},
        "rels": {str(k): v for k, v in rel_counts.items()},
        "duration_s": round(time.time() - started, 1),
    }
    log(f"sync done in {result['duration_s']}s: {result['nodes']} / {result['rels']}")
    return result
