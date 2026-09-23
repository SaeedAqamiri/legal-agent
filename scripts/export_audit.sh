#!/usr/bin/env bash
# Export all audit-relevant events for a tenant as JSON Lines directly from
# PostgreSQL. RLS is forced, so the tenant is set within the same transaction.
set -euo pipefail

ORG="${1:-org-a}"
OUT="${2:-audit_${ORG}.jsonl}"
PG_DSN="${LEGAL_AGENT_POSTGRES_DSN:-postgresql://legal_agent:legal_agent@localhost:5432/legal_agent}"

psql -d "${PG_DSN}" -X -A -q "${ORG}" > /dev/null 2>&1 <<SQL_END_1 || true
SELECT set_config('legal_agent.organization_id', '${ORG}', true);
SQL_END_1

psql -d "${PG_DSN}" -X -A -q -v ON_ERROR_STOP=0 > "${OUT}" <<SQL
BEGIN;
SELECT set_config('legal_agent.organization_id', '${ORG}', true);
-- Research history (payloads are self-describing audit records)
SELECT jsonb_build_object(
    'stream', 'research_history',
    'episode_id', episode_id, 'user_id', user_id, 'question', question,
    'applicable_time', applicable_time, 'created_at', created_at,
    'completed', completed, 'has_answer', has_answer, 'payload', payload
)::text
FROM research_history.episodes WHERE organization_id = '${ORG}';
-- Canonical ingestion audit trail
SELECT '{"stream":"canonical/append_only"}'::text;
COMMIT;
SQL

echo "==> Audit exported to ${OUT}"
