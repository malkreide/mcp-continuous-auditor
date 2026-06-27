#!/usr/bin/env bash
#
# up.sh — bring up the TensorZero gateway + ClickHouse on the host VM (Broker side)
# and verify it end-to-end. Run on the host VM after 00-preflight.sh passes.
#
# It is idempotent: re-running reconciles the compose stack and re-checks health.
# Secrets come from the environment / .env (NEVER inline) — mirrors the repo's
# .env discipline. The gateway is bound to 127.0.0.1 by the compose file, so it is
# reachable only from OpenClaw on the same host.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
TZ_DIR="${REPO_ROOT}/tensorzero"      # the version-controlled compose + config
GATEWAY_URL="${TENSORZERO_GATEWAY:-http://127.0.0.1:3000}"

die() { echo "FATAL: $*" >&2; exit 1; }

[ -f "${TZ_DIR}/docker-compose.yml" ] || die "missing ${TZ_DIR}/docker-compose.yml"
[ -f "${TZ_DIR}/tensorzero.toml" ]    || die "missing ${TZ_DIR}/tensorzero.toml"

# The stack reads ANTHROPIC_API_KEY + CLICKHOUSE_PASSWORD from the environment.
# Load the repo .env if present (same file OpenClaw uses); never print it.
if [ -f "${REPO_ROOT}/.env" ]; then
  set -a; . "${REPO_ROOT}/.env"; set +a
fi
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY (in ${REPO_ROOT}/.env or the env)}"
: "${CLICKHOUSE_PASSWORD:?set CLICKHOUSE_PASSWORD (in ${REPO_ROOT}/.env or the env)}"

echo "==> pinning image tags before pull (edit docker-compose.yml to pin :latest -> a tested tag)"
grep -nE 'image:.*:latest' "${TZ_DIR}/docker-compose.yml" \
  && echo "    WARNING: :latest tags above drift between releases — pin them for a real rollout." || true

echo "==> docker compose up -d"
( cd "${TZ_DIR}" && docker compose up -d ) || die "compose up failed"

echo "==> waiting for ClickHouse + gateway to become healthy"
for i in $(seq 1 30); do
  if curl -fsS "${GATEWAY_URL}/health" >/dev/null 2>&1 \
     || curl -fsS "${GATEWAY_URL}/openai/v1/models" >/dev/null 2>&1; then
    echo "    gateway up at ${GATEWAY_URL}"
    break
  fi
  [ "${i}" -eq 30 ] && die "gateway did not become healthy — check: (cd ${TZ_DIR} && docker compose logs)"
  sleep 2
done

echo "==> smoke test: a minimal inference through the gateway"
# Calls the 'nightly_audit' function defined in tensorzero.toml. A 2xx with a
# choices/output body means OpenClaw can route through here. (Endpoint shape is
# the OpenAI-compatible surface; adjust to the native /inference if you prefer.)
smoke_rc=0
curl -fsS "${GATEWAY_URL}/openai/v1/chat/completions" \
  -H 'content-type: application/json' \
  -d '{"model":"tensorzero::function_name::nightly_audit","messages":[{"role":"user","content":"reply with the single word: ready"}],"max_tokens":16}' \
  >/dev/null 2>&1 || smoke_rc=$?
if [ "${smoke_rc}" -eq 0 ]; then
  echo "    smoke OK — gateway reached the provider and returned a completion."
else
  echo "    smoke INCONCLUSIVE (rc=${smoke_rc}) — the stack is up but the test call"
  echo "    failed. Verify the function/model names in tensorzero.toml and your"
  echo "    ANTHROPIC_API_KEY. See: (cd ${TZ_DIR} && docker compose logs gateway)"
fi

cat <<EOF

== TensorZero is up ==
  gateway : ${GATEWAY_URL}   (OpenAI-compatible: ${GATEWAY_URL}/openai/v1)
  config  : ${TZ_DIR}/tensorzero.toml
  trail   : ClickHouse (per-inference tokens/cost) — query with deploy/tensorzero/episode-tokens.sh

Next:
  1) Point OpenClaw at the gateway (see docs/observability/tensorzero.md
     "OpenClaw auf das Gateway zeigen"): set the provider baseURL to
     ${GATEWAY_URL}/openai/v1 (or ANTHROPIC_BASE_URL=${GATEWAY_URL}).
  2) Export TENSORZERO_GATEWAY=${GATEWAY_URL} for nightly-audit.sh so each run is
     episode-tagged and its true token total feeds budget_guard (cost-cap).
EOF
