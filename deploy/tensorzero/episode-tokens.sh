#!/usr/bin/env bash
#
# episode-tokens.sh <episode-id> — total tokens for one nightly-audit run.
#
# TensorZero tags every model call (writer + grader) of a run with the same
# episode id and records tokens/cost in ClickHouse. This script sums that run's
# tokens so nightly-audit.sh can feed the REAL per-run total to budget_guard
# (the per-run cost-cap), instead of the promptfoo-only count.
#
# Contract: on success prints a single integer to stdout and exits 0. On ANY
# failure it prints nothing and exits non-zero — the caller then falls back to
# the promptfoo token count, so a ClickHouse hiccup never zeroes out the budget.
#
# Env:
#   CLICKHOUSE_HTTP      default http://127.0.0.1:8123
#   CLICKHOUSE_USER      default tensorzero
#   CLICKHOUSE_PASSWORD  required (from .env)
#   CLICKHOUSE_DB        default tensorzero
set -uo pipefail

episode="${1:-}"
[ -n "${episode}" ] || { echo "usage: episode-tokens.sh <episode-id>" >&2; exit 2; }

CH_URL="${CLICKHOUSE_HTTP:-http://127.0.0.1:8123}"
CH_USER="${CLICKHOUSE_USER:-tensorzero}"
CH_DB="${CLICKHOUSE_DB:-tensorzero}"
: "${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD not set}" 2>/dev/null || exit 1

# NOTE: column/table names vary by TensorZero version — verify against your
# schema (docs/observability/tensorzero.md has the reference query). We read the
# episode id as a PARAMETER (not string-interpolated) so an untrusted id can
# never be injected into SQL.
sql="SELECT toUInt64(sum(input_tokens + output_tokens))
     FROM ${CH_DB}.ModelInference
     WHERE episode_id = {ep:String}
     FORMAT TabSeparated"

out="$(curl -fsS "${CH_URL}/" \
        --user "${CH_USER}:${CLICKHOUSE_PASSWORD}" \
        --data-urlencode "param_ep=${episode}" \
        --data-urlencode "query=${sql}" 2>/dev/null)" || exit 1

# Must be a bare non-negative integer; anything else is a failure (fall back).
case "${out}" in
  ''|*[!0-9]*) exit 1 ;;
  *) echo "${out}" ;;
esac
