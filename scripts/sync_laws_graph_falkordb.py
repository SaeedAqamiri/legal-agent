#!/usr/bin/env python3
"""CLI wrapper: project the canonical laws graph into FalkorDB.

The sync logic lives in legal_agent_core.laws_mirror (shared with the API's
startup auto-sync and POST /v1/organizations/{org}/graph/sync).

Full rebuild each run, so re-run after publishing new relations.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.falkordb import connect_laws_graph
from legal_agent_core.laws_mirror import LAWS_GRAPH_ID, sync_laws_mirror

DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
DEFAULT_FALKOR_URL = "falkor://localhost:6380"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN)
    )
    parser.add_argument(
        "--falkor-url",
        default=os.environ.get("FALKORDB_URL", DEFAULT_FALKOR_URL),
    )
    parser.add_argument("--graph", default=LAWS_GRAPH_ID)
    parser.add_argument("--batch-size", type=int, default=2000)
    args = parser.parse_args()

    db = psycopg.connect(args.dsn, autocommit=True)
    graph = connect_laws_graph(args.falkor_url)
    if args.graph != LAWS_GRAPH_ID:
        graph = graph.client.select_graph(args.graph) if hasattr(graph, "client") else graph
    result = sync_laws_mirror(db, graph, batch_size=args.batch_size)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
