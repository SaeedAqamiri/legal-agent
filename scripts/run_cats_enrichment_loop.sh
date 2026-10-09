#!/usr/bin/env bash
# Final loop of the cats enrichment plan — run AFTER the long background jobs
# (llm_extract_cats_relations.py, fetch_cats_links.py) have finished.
#
#   1. sweep rerun        — picks up fetched_missing docs + any 429-failed docs
#   2. resolve            — links stored LLM outputs to the grown corpus
#   3. fetch missing laws — new gap report -> ekhtebar search -> ingest
#   4. fill provisions    — LLM reads whole laws for missing ماده/تبصره
#   5. resolve again      — links everything created in 3-4
#   6. hierarchy + stats  — out/legal_hierarchy.json, out/cats_enrichment_stats.json
#
# Usage:
#   LEGAL_AGENT_POSTGRES_DSN=... LEGAL_AGENT_LLM_API_KEY=... \
#       scripts/run_cats_enrichment_loop.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python

echo "== [1/6] sweep rerun (skips done slugs) =="
$PY scripts/llm_extract_cats_relations.py --workers 6

echo "== [2/6] resolve relations =="
$PY scripts/resolve_cats_relations.py

echo "== [3/6] fetch missing laws =="
$PY scripts/fetch_missing_laws.py --delay 1

echo "== [4/6] fill missing provisions =="
$PY scripts/fill_missing_provisions.py

echo "== [5/6] resolve again =="
$PY scripts/resolve_cats_relations.py

echo "== [6/6] hierarchy + stats =="
$PY scripts/build_legal_hierarchy.py
$PY scripts/cats_enrichment_stats.py

echo "== loop complete =="
