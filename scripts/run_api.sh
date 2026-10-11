#!/usr/bin/env bash
# Run the API natively from the repo venv (code changes apply without rebuild).
# Infra (postgres + falkordb) still comes from docker compose.
set -euo pipefail
cd "$(dirname "$0")/.."

export LEGAL_AGENT_POSTGRES_DSN="${LEGAL_AGENT_POSTGRES_DSN:-postgresql://legal_agent:legal_agent@localhost:5432/legal_agent}"
export FALKORDB_URL="${FALKORDB_URL:-falkor://localhost:6380}"
export LAWS_DATA_DIR="${LAWS_DATA_DIR:-/home/arman/laws-mcp/data}"
export LEGAL_AGENT_SESSION_SECRET="${LEGAL_AGENT_SESSION_SECRET:-dev-secret-change-me}"

exec .venv/bin/python -m uvicorn legal_agent_core.composition:app \
    --host 0.0.0.0 --port 8000 --reload
