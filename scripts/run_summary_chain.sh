#!/usr/bin/env bash
# Sequential summary regeneration: instruments -> judgments/advisories -> provisions
# Resume-safe: each script skips already-summarized rows (matched by prompt_version).
set -u
cd "$(dirname "$0")/.."
echo "=== chain start $(date +%H:%M:%S) ==="
.venv/bin/python scripts/summarize_instruments.py --workers 10
echo "=== instruments done $(date +%H:%M:%S) ==="
.venv/bin/python scripts/summarize_source_docs.py --workers 10
echo "=== source docs done $(date +%H:%M:%S) ==="
.venv/bin/python scripts/summarize_provisions.py --workers 10
echo "=== provisions done $(date +%H:%M:%S) ==="
