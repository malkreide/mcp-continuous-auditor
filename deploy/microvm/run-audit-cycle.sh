#!/usr/bin/env bash
#
# run-audit-cycle.sh — the Broker-side orchestrator that owns the budget breaker
# in the microVM topology (Analysis T-B).
#
# The throwaway Worker runs with BUDGET_GUARD=0 (a VM discarded each run keeps no
# history), so the circuit breaker + token ceilings + missing-result detection
# HAVE to live on the Broker side. Nothing wired them there — this script does:
#
#   1. budget_guard preflight — if the breaker is OPEN, SKIP this cycle (don't
#      spend on a wedged environment). A skip is a hard-fail-shaped outcome, exit 1.
#   2. launch the Worker (run-worker.sh) — the Broker listener classifies whatever
#      evidence arrives into <dropbox>/<ts>-<pid>/nightly-summary.json.
#   3. budget_guard record — feed the breaker the REAL outcome. A cycle that
#      shipped NO fresh evidence within the settle window is recorded as a HARD
#      failure (--exit-code 1), so the missing-result detection actually bites.
#
# ONE recorder (this script) — the untrusted listener handler stays side-effect
# minimal and the breaker can never double-count or race a forked handler. Run
# this as the per-cycle entrypoint instead of calling run-worker.sh directly
# (see docs/deployment/phase5-rollout.md).
#
# Env:
#   DROPBOX          (required) the Broker listener's dropbox dir (per-run subdirs)
#   BUDGET_STATE     budget state file (default: <repo>/.audit/budget-state.json)
#   BUDGET_GUARD     0/off disables the breaker (default: on)
#   RUN_WORKER_CMD   command to launch ONE Worker run
#                    (default: bash <repo>/deploy/microvm/run-worker.sh)
#   SETTLE_SECONDS   how long to wait for the listener to write the fresh run dir
#                    after the Worker returns (default: 10)
#   TARGET_REPO      passed through to the preflight skip report
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
GUARD="${REPO_ROOT}/scripts/budget_guard.py"

: "${DROPBOX:?set DROPBOX to the Broker listener dropbox dir}"
export BUDGET_STATE="${BUDGET_STATE:-${REPO_ROOT}/.audit/budget-state.json}"
BUDGET_GUARD="${BUDGET_GUARD:-1}"
RUN_WORKER_CMD="${RUN_WORKER_CMD:-bash ${REPO_ROOT}/deploy/microvm/run-worker.sh}"
SETTLE_SECONDS="${SETTLE_SECONDS:-10}"

command -v python3 >/dev/null || { echo "FATAL: python3 required" >&2; exit 1; }
[ -f "${GUARD}" ] || { echo "FATAL: budget_guard not found at ${GUARD}" >&2; exit 1; }
mkdir -p "${DROPBOX}"

guard_on() { [ "${BUDGET_GUARD}" != "0" ] && [ "${BUDGET_GUARD}" != "off" ]; }

# --- 1) preflight: gate the cycle on the breaker ------------------------------
if guard_on; then
  if python3 "${GUARD}" preflight --target "${TARGET_REPO:-unknown}"; then
    :   # breaker closed / half-open trial — proceed.
  else
    pf_rc=$?
    if [ "${pf_rc}" -eq 75 ]; then
      echo "==> budget breaker OPEN — Worker run skipped this cycle" >&2
      exit 1   # a skipped cycle is never a pass.
    fi
    echo "FATAL: budget preflight errored (exit ${pf_rc})" >&2
    exit 1
  fi
fi

# --- 2) snapshot the dropbox, then launch the Worker --------------------------
before="$(mktemp)"; after="$(mktemp)"
trap 'rm -f "${before}" "${after}"' EXIT
( cd "${DROPBOX}" && ls -1 2>/dev/null || true ) | sort > "${before}"

echo "==> launching Worker: ${RUN_WORKER_CMD}"
# shellcheck disable=SC2086
${RUN_WORKER_CMD}; worker_rc=$?
echo "==> Worker launcher exited rc=${worker_rc}"

# --- 3) find the fresh run dir the listener wrote, record the outcome ---------
# The vsock delivery may land just as the Worker powers off, so poll briefly for
# the listener to finish writing the new run dir before concluding "no evidence".
new_dir=""
i=0
attempts=$(( SETTLE_SECONDS + 1 ))
while [ "${i}" -lt "${attempts}" ]; do
  ( cd "${DROPBOX}" && ls -1 2>/dev/null || true ) | sort > "${after}"
  cand="$(comm -13 "${before}" "${after}" | tail -n1)"
  if [ -n "${cand}" ] && [ -s "${DROPBOX}/${cand}/nightly-summary.json" ]; then
    new_dir="${cand}"; break
  fi
  i=$(( i + 1 ))
  [ "${i}" -lt "${attempts}" ] && sleep 1
done

outcome_exit=1   # default: NO fresh evidence -> missing result -> hard-fail
if [ -n "${new_dir}" ]; then
  # Single-line python (no heredoc): read the classifier's exit_code, or 1 on any error.
  outcome_exit="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["exit_code"]))' \
    "${DROPBOX}/${new_dir}/nightly-summary.json" 2>/dev/null || echo 1)"
  echo "==> fresh evidence: ${new_dir} (exit_code=${outcome_exit})"
else
  echo "==> NO fresh evidence in ${DROPBOX} — recording missing result as hard-fail"
fi

if guard_on; then
  python3 "${GUARD}" record --exit-code "${outcome_exit}" || true
fi

exit "${outcome_exit}"
