#!/usr/bin/env bash
# Waits for the fetch_missing_laws process, then runs the closing chain.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
FETCH_PID="${1:-}"

if [ -n "$FETCH_PID" ]; then
  while kill -0 "$FETCH_PID" 2>/dev/null; do sleep 60; done
fi
echo "== fetch done, $(date +%H:%M:%S) =="

echo "== resolve =="
$PY scripts/resolve_cats_relations.py

echo "== fill missing provisions =="
$PY scripts/fill_missing_provisions.py

echo "== resolve again =="
$PY scripts/resolve_cats_relations.py

echo "== hierarchy + stats =="
$PY scripts/build_legal_hierarchy.py
$PY scripts/cats_enrichment_stats.py

echo "== all done, $(date +%H:%M:%S) =="
