#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# OpsPilot AI backend entrypoint for a hosted deployment (docs/deployment.md).
#
# Three things a container platform needs that `uvicorn app.main:app` alone
# does not give us:
#
#   1. the port is assigned by the platform, not by us ($PORT);
#   2. schema changes must be applied, and *when* they are applied has to be
#      a deliberate choice rather than a side effect of a restart;
#   3. TLS is terminated by the platform's proxy, so uvicorn has to be told
#      which peers may set X-Forwarded-*.
#
# Migrations are opt-in per deployment. The default is OFF: `/readyz` already
# refuses traffic when the applied revision is not the code's head (§13.7), so
# a container that boots without migrating fails its health check loudly
# instead of serving a stale schema quietly. Set OPSPILOT_MIGRATE_ON_START=true
# on platforms with no separate release phase (Railway), and leave it unset
# where migrations run as their own step.
# ---------------------------------------------------------------------------
set -eu

: "${PORT:=8000}"
: "${OPSPILOT_MIGRATE_ON_START:=false}"
: "${OPSPILOT_SEED_ON_START:=false}"
: "${FORWARDED_ALLOW_IPS:=127.0.0.1}"

if [ "${OPSPILOT_MIGRATE_ON_START}" = "true" ]; then
  echo "opspilot: applying database migrations (alembic upgrade head)" >&2
  alembic upgrade head
fi

if [ "${OPSPILOT_SEED_ON_START}" = "true" ]; then
  # Additive seed of the deterministic mock CRM: `reset=False`, so restarting
  # a demo deployment never truncates the rows an operator was looking at.
  echo "opspilot: seeding the mock CRM (additive)" >&2
  python -m app.integrations.mock.seed --no-reset
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
