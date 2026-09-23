#!/usr/bin/env bash
# Tenant administration and retention.
#   usage: tenant_admin.sh list
#          tenant_admin.sh retention <days>    (delete research_history older than N days)
#          tenant_admin.sh count <org>
set -euo pipefail

CMD="${1:-list}"
PG_DSN="${LEGAL_AGENT_POSTGRES_DSN:-postgresql://legal_agent:legal_agent@localhost:5432/legal_agent}"

case "${CMD}" in
  list)
    echo "==> Tenants (organizations) observed in research_history:"
    psql -d "${PG_DSN}" -X -A -q -c \
      "SELECT DISTINCT organization_id, count(*) FROM research_history.episodes GROUP BY organization_id ORDER BY organization_id;"
    ;;
  count)
    ORG="${2:?usage: retention count <org>}"
    psql -d "${PG_DSN}" -X -A -q -c \
      "SELECT set_config('legal_agent.organization_id', '${ORG}', true); SELECT count(*) AS episodes FROM research_history.episodes;"
    ;;
  retention)
    DAYS="${2:?usage: retention <days>}"
    echo "==> Deleting research history older than ${DAYS} days (terminal audit requested separately)."
    psql -d "${PG_DSN}" -X -A -q -c \
      "DELETE FROM research_history.episodes WHERE created_at < now() - interval '${DAYS} days';"
    ;;
  *)
    echo "unknown command: ${CMD}"; exit 2
    ;;
esac
