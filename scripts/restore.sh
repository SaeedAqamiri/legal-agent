#!/usr/bin/env bash
# Restore a PostgreSQL dump produced by scripts/backup.sh. The target database
# must exist; data is restored on top (drop/truncate controlled by the caller).
set -euo pipefail

DUMP="${1:?usage: restore.sh <backup.dump> [dsn]}"
PG_DSN="${2:-${LEGAL_AGENT_POSTGRES_DSN:-postgresql://legal_agent:legal_agent@localhost:5432/legal_agent}}"

[ -f "${DUMP}" ] || { echo "dump not found: ${DUMP}"; exit 1; }
test -f "${DUMP}.sha256" && (cd "$(dirname "${DUMP}")" && sha256sum -c "${DUMP}.sha256")

echo "==> Restoring ${DUMP} into ${PG_DSN}"
# custom-format dump with --exit-on-error; migrations/dev tables recreated by app.
pg_restore --no-owner --no-privileges --exit-on-error --dbname "${PG_DSN}" "${DUMP}"

echo "==> Rollback note: the app runs versioned migrations. To roll back AFTER a
    migration, restore the pre-upgrade dump (this file) and restart the app,
    which will not re-apply already-recorded migrations."
echo "==> Restore complete."
