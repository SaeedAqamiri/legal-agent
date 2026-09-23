#!/usr/bin/env bash
# Point-in-time backup of the legal-agent persistence layer (PostgreSQL data +
# FalkorDB RDB). Source the environment first: source .env.
set -euo pipefail

DST="${1:-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
# Backup uses a BYPASSRLS role so forced tenant RLS does not hide audit rows.
PG_DSN="${LEGAL_AGENT_BACKUP_DSN:-postgresql://legal_agent_backup:legal_agent_backup@localhost:5432/legal_agent}"
FALKORDB_HOST="${FALKORDB_HOST:-localhost}"
FALKORDB_PORT="${FALKORDB_PORT:-7000}"

mkdir -p "${DST}"
echo "==> Backing up PostgreSQL (${PG_DSN}) → ${DST}/pg_${STAMP}.dump"
pg_dump --no-owner --no-privileges --format=custom "${PG_DSN}" > "${DST}/pg_${STAMP}.dump"

echo "==> Backing up FalkorDB RDB (${FALKORDB_HOST}:${FALKORDB_PORT})"
redis-cli -h "${FALKORDB_HOST}" -p "${FALKORDB_PORT}" BGSAVE >/dev/null 2>&1 || true
echo "    FalkorDB RDB lives in its configured data dir; snapshot '/data/dump.rdb' separately."
sha256sum "${DST}/pg_${STAMP}.dump" > "${DST}/pg_${STAMP}.dump.sha256"
echo "==> Done: backups/pg_${STAMP}.dump  (verify with 'pg_restore --list -f - backups/pg_*.dump')"
