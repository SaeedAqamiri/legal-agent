"""On-demand full-text lookup for a single provision from the canonical store.

The FalkorDB laws mirror deliberately carries only provision labels and
one-line summaries; the verbatim article text lives in
``canonical.provision_versions.text``. Both the graph-guide chat agent and the
explore UI read it through this helper so the query lives in one place.
"""

from __future__ import annotations

from typing import Any

#: Longest article text returned in one payload (agent/UI both cap here).
MAX_PROVISION_TEXT = 6000

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
