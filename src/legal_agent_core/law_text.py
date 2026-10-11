"""On-demand full-text lookup for a single provision from the canonical store.

The FalkorDB laws mirror deliberately carries only provision labels and
one-line summaries; the verbatim article text lives in
``canonical.provision_versions.text``. Both the graph-guide chat agent and the
explore UI read it through this helper so the query lives in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Longest article text returned in one payload (agent/UI both cap here).
MAX_PROVISION_TEXT = 6000

#: Longest judgment/advisory-opinion text returned in one payload.
MAX_JUDGMENT_TEXT = 8000

_PROVISION_TEXT_SQL = """
SELECT p.provision_id,
       p.instrument_id,
       p.label,
       p.number,
       p.provision_type,
       i.title,
       pv.text,
       pv.effective_from,
       pv.status
  FROM canonical.provisions p
  JOIN canonical.legal_instruments i ON i.instrument_id = p.instrument_id
  LEFT JOIN LATERAL (
       SELECT text, effective_from, status
         FROM canonical.provision_versions v
        WHERE v.provision_id = p.provision_id
        ORDER BY v.effective_from DESC NULLS LAST, v.provision_version_id DESC
        LIMIT 1
  ) pv ON true
 WHERE p.provision_id = %s
"""


_INSTRUMENT_TEXT_SQL = """
SELECT i.title, p.label, p.number, pv.text
  FROM canonical.legal_instruments i
  LEFT JOIN canonical.provisions p ON p.instrument_id = i.instrument_id
  LEFT JOIN LATERAL (
       SELECT text
         FROM canonical.provision_versions v
        WHERE v.provision_id = p.provision_id
        ORDER BY v.effective_from DESC NULLS LAST, v.provision_version_id DESC
        LIMIT 1
  ) pv ON true
 WHERE i.instrument_id = %s
 ORDER BY p.ordinal NULLS LAST, p.provision_id
"""


def fetch_instrument_text(connection: Any, instrument_id: str) -> dict[str, Any] | None:
    """Return every provision's text for one instrument, or ``None`` when absent.

    Used as the fallback reader for instruments that have no original source
    file on the sources side (e.g. the ``cats_ingest`` corpus); the verbatim
    article text still lives in ``canonical.provision_versions``.
    """
    with connection.cursor() as cur:
        cur.execute(_INSTRUMENT_TEXT_SQL, (instrument_id,))
        rows = cur.fetchall()
    if not rows:
        return None
    provisions = [
        {"label": row[1], "number": row[2], "text": row[3] or ""}
        for row in rows
        if row[1] is not None or row[3] is not None
    ]
    return {"instrument_id": instrument_id, "title": rows[0][0], "provisions": provisions}


_JUDGMENT_TEXT_SQL = """
SELECT d.title, d.doc_kind, d.issue_date, d.source_uri,
       d.metadata->>'cat' AS category,
       d.metadata->>'summary' AS summary,
       d.text_path,
       i.issuer
  FROM sources.source_documents d
  LEFT JOIN sources.instruments i ON i.instrument_uid = d.instrument_uid
 WHERE d.document_uid::text = %s
 LIMIT 1
"""


def fetch_judgment_text(connection: Any, document_uid: str) -> dict[str, Any] | None:
    """Return the text + metadata of a judgment/advisory opinion, or ``None``.

    Judgments live on the sources side (``sources.source_documents``) rather
    than in canonical, so the graph mirror carries only labels/summaries and
    the verbatim text is read from ``text_path`` on demand.
    """
    with connection.cursor() as cur:
        cur.execute(_JUDGMENT_TEXT_SQL, (document_uid,))
        row = cur.fetchone()
    if row is None:
        return None
    title, doc_kind, issue_date, source_uri, category, summary, text_path, issuer = row
    text = ""
    if text_path and Path(text_path).exists():
        try:
            text = Path(text_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    kind = (
        "opinion"
        if doc_kind != "judgment" or "نظریه" in (title or "")
        else "judgment"
    )
    return {
        "document_uid": document_uid,
        "title": title,
        "kind": kind,
        "category": category,
        "issuer": issuer,
        "date": issue_date.isoformat() if issue_date is not None else None,
        "summary": summary,
        "text": text[:MAX_JUDGMENT_TEXT],
        "truncated": len(text) > MAX_JUDGMENT_TEXT,
        "source_url": source_uri,
    }


def fetch_provision_text(connection: Any, provision_id: str) -> dict[str, Any] | None:
    """Return the current text of one provision, or ``None`` when absent.

    ``connection`` is a psycopg connection on the canonical schema.
    """
    with connection.cursor() as cur:
        cur.execute(_PROVISION_TEXT_SQL, (provision_id,))
        row = cur.fetchone()
    if row is None:
        return None
    text = row[6] or ""
    return {
        "provision_id": row[0],
        "instrument_id": row[1],
        "label": row[2],
        "number": row[3],
        "type": row[4],
        "instrument_title": row[5],
        "text": text[:MAX_PROVISION_TEXT],
        "truncated": len(text) > MAX_PROVISION_TEXT,
        "effective_from": row[7].isoformat() if row[7] is not None else None,
        "status": row[8],
    }
