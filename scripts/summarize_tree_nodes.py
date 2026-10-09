#!/usr/bin/env python3
"""LLM summaries for container nodes of the document trees (PageIndex-style).

For every instrument whose provisions form a non-trivial hierarchy, one
whole-instrument call asks the model for a one-line Persian summary of each
بخش/فصل (and any other node that has children). Results persist in
canonical.provision_summaries (migration V0007) keyed by provision_id so
reruns skip finished instruments; scripts/build_document_tree_index.py merges
them into out/document_trees/*.json and the get_document_structure tool reads
them through the composition loader at query time.

Usage:
    LEGAL_AGENT_LLM_API_KEY=... LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/summarize_tree_nodes.py [--limit N] [--instrument ID] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

BASE_URL = "https://api.z.ai/api/coding/paas/v4"
MODEL = "glm-5.3-flash"
STAGE = "structure"
PROMPT_VERSION = "tree-v1"
MAX_TOKENS = 8000
#: containers longer than this many children are split across calls
MAX_NODES_PER_CALL = 60

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

SYSTEM_PROMPT = (
    "You are a Persian legal-document analyst. Given the heading structure of "
    "one law, write for every requested section a single-line summary "
    "(max ~20 words) of what that section regulates. Use only the given "
    "labels/titles/excerpts. Reply with strict JSON only: "
    '{"summaries": {"<provision_id>": "<summary>"}}. '
    "Every requested provision_id must appear exactly once."
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def extract_json(text: str):
    fenced = JSON_BLOCK_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("no JSON")
    return json.loads(candidate[start:])


def llm_json(key: str, prompt: str, retries: int = 6) -> dict:
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(request, timeout=600) as resp:
                data = json.loads(resp.read())
            return extract_json(data["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            time.sleep(40 if exc.code == 429 else 5 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError(last_exc)


def container_nodes(db: psycopg.Connection, instrument_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT p.provision_id, p.provision_type, COALESCE(p.label, ''),
                  COALESCE(p.title, ''),
                  (SELECT COALESCE(pv.normalized_text, pv.text, '')
                   FROM canonical.provision_versions pv
                   WHERE pv.provision_id = p.provision_id
                   ORDER BY pv.provision_version_id LIMIT 1),
                  (SELECT count(*) FROM canonical.provisions c
                   WHERE c.parent_provision_id = p.provision_id),
                  (SELECT string_agg(DISTINCT c.label, '، ')
                   FROM canonical.provisions c
                   WHERE c.parent_provision_id = p.provision_id)
           FROM canonical.provisions p
           WHERE p.instrument_id = %s
             AND EXISTS (SELECT 1 FROM canonical.provisions c
                         WHERE c.parent_provision_id = p.provision_id)
           ORDER BY p.ordinal, p.provision_id""",
        (instrument_id,),
    ).fetchall()
    return [
        {
            "provision_id": row[0],
            "type": row[1],
            "label": row[2],
            "title": row[3],
            "excerpt": (row[4] or "")[:600],
            "child_count": row[5],
            "child_labels": (row[6] or "")[:300],
        }
        for row in rows
    ]


def build_prompt(instrument_title: str, nodes: list[dict]) -> str:
    listing = []
    for node in nodes:
        head = f"{node['label']}".strip()
        if node["title"]:
            head += f" — {node['title']}"
        listing.append(
            f"- id={node['provision_id']} | {head} | محتوا: {node['excerpt'] or '—'}"
        )
    return (
        f"عنوان سند: {instrument_title}\n"
        "بخش‌هایی که باید خلاصه شوند:\n" + "\n".join(listing)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "LEGAL_AGENT_POSTGRES_DSN",
            "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent",
        ),
    )
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-instruments", type=int, default=0, help="0 = all")
    parser.add_argument(
        "--force", action="store_true", help="resummarize instruments that already have summaries"
    )
    args = parser.parse_args()
    key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    if not key:
        print("LEGAL_AGENT_LLM_API_KEY is not set", file=sys.stderr)
        return 1

    with psycopg.connect(args.dsn, autocommit=True) as db:
        condition = "AND i.instrument_id = %s" if args.instrument else ""
        params = (args.instrument,) if args.instrument else ()
        instruments = db.execute(
            f"""SELECT i.instrument_id, i.title
                FROM canonical.legal_instruments i
                WHERE EXISTS (
                    SELECT 1 FROM canonical.provisions p
                    WHERE p.instrument_id = i.instrument_id
                      AND EXISTS (SELECT 1 FROM canonical.provisions c
                                  WHERE c.parent_provision_id = p.provision_id))
                {condition}
                ORDER BY i.instrument_id""",
            params,
        ).fetchall()
        done = set()
        if not args.force:
            done = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT instrument_id FROM canonical.provision_summaries"
                ).fetchall()
            }
        todo = [(iid, title) for iid, title in instruments if iid not in done]
        if args.max_instruments:
            todo = todo[: args.max_instruments]
        log(f"{len(todo)} instruments to summarize ({len(done)} already done)")

        for index, (instrument_id, title) in enumerate(todo, 1):
            nodes = container_nodes(db, instrument_id)
            if not nodes:
                continue
            summaries: dict[str, str] = {}
            try:
                for start in range(0, len(nodes), MAX_NODES_PER_CALL):
                    batch = nodes[start : start + MAX_NODES_PER_CALL]
                    payload = llm_json(key, build_prompt(title, batch))
                    for node in batch:
                        text = (payload.get("summaries") or {}).get(node["provision_id"])
                        if isinstance(text, str) and text.strip():
                            summaries[node["provision_id"]] = text.strip()[:400]
            except Exception as exc:  # noqa: BLE001
                log(f"{instrument_id} FAILED: {exc}")
                continue
            if not summaries:
                continue
            for provision_id, text in summaries.items():
                db.execute(
                    """INSERT INTO canonical.provision_summaries
                           (provision_id, instrument_id, summary, model, prompt_version)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (provision_id) DO UPDATE
                       SET summary = EXCLUDED.summary,
                           model = EXCLUDED.model,
                           prompt_version = EXCLUDED.prompt_version,
                           created_at = now()""",
                    (provision_id, instrument_id, text, MODEL, PROMPT_VERSION),
                )
            if index % 25 == 0:
                log(f"{index}/{len(todo)} done (last: {title[:50]})")
        log("finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
