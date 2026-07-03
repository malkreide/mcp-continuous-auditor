#!/usr/bin/env bash
#
# nightly-audit.sh — deterministic core of the daily 03:00 OpenClaw cron audit
# (Plan Phase 4). It is the "truth" half of the job: the cron *agent* only
# interprets and routes; this script produces the ground-truth result.
#
# What it does, all read-only against the target MCP server:
#   1. provisions / pulls the target (git over HTTPS, no writes, no push — the
#      same read-only contract as scripts/audit-target.sh) and uv-syncs deps;
#   2. runs ruff + mypy + pytest;
#   3. runs the schema-drift gate (schemas/generate_schemas.py --check);
#   4. runs the promptfoo eval (tool-output contract + OWASP red-team), writing
#      machine-readable JSON output;
#   5. hands every exit code + the promptfoo JSON to
#      scripts/nightly_audit_report.py, which classifies the outcome and writes
#      a concise report + summary.json.
#
# Exit code (the contract with the cron agent — see nightly_audit_report.py):
#   0  all gates green
#   2  finding(s): schema drift and/or red-team hit and/or toolchain failure
#   1  HARD failure: a gate could not run, or a model/provider was unresolvable.
#      An unresolvable model HARD-fails here — it is never silently downgraded
#      to a pass (Plan Phase 4: "hart fehlschlagen, nicht still ausweichen").
#
# Policy (openclaw/workspace/{AGENTS,TOOLS}.md): no `curl | sh`, the token is
# never inlined, nothing outside the project workspace is written, and the
# target is only ever read. Outputs land in the gitignored .audit/ work dir.
#
# Usage:
#   TARGET_REPO=malkreide/zurich-opendata-mcp scripts/nightly-audit.sh [REF]
#
# Env:
#   TARGET_REPO         owner/name of the target (default: malkreide/zurich-opendata-mcp)
#   TARGET_REF          git ref to pin (default: main)
#   AUDIT_DIR           work dir, gitignored (default: <repo>/.audit)
#   MCP_SERVER_IMPORT   FastMCP server ref for the schema gate
#                       (default: zurich_opendata_mcp.server:mcp)
#   PROMPTFOO_CONFIG    path to promptfooconfig.yaml inside the target checkout
#                       (default: promptfoo/promptfooconfig.yaml)
#   PROMPTFOO_VERSION   pinned promptfoo version — the truth-engine is NOT run at
#                       @latest (that would make a "deterministic" gate reload a
#                       moving target every night). Default: 0.121.17.
#   GRADER_PROVIDER     llm-rubric grader — MUST be a different model family than
#                       the writer agent (writer != checker). Passed to promptfoo
#                       as --grader when set; otherwise the config default is used.
#   BUDGET_GUARD        Phase-5 budget guardrails (default: on; set 0/off to skip)
#                       — circuit breaker + token ceiling, see budget_guard.py /
#                       docs/budget/guardrails.md. Tunable via BUDGET_* env vars.
#
set -uo pipefail   # NOT -e: we must observe every gate's exit code, not abort on the first red one.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"

TARGET_REPO="${TARGET_REPO:-malkreide/zurich-opendata-mcp}"
TARGET_REF="${TARGET_REF:-${1:-main}}"
AUDIT_DIR="${AUDIT_DIR:-${REPO_ROOT}/.audit}"
MCP_SERVER_IMPORT="${MCP_SERVER_IMPORT:-zurich_opendata_mcp.server:mcp}"
PROMPTFOO_CONFIG="${PROMPTFOO_CONFIG:-promptfoo/promptfooconfig.yaml}"
# Pin the truth-engine: a deterministic gate must not silently reload a new
# promptfoo (plugin renames / behaviour changes) on every run. Bump deliberately.
PROMPTFOO_VERSION="${PROMPTFOO_VERSION:-0.121.17}"

repo_name="${TARGET_REPO##*/}"
src_dir="${AUDIT_DIR}/${repo_name}"
log_dir="${AUDIT_DIR}/logs"
report_path="${AUDIT_DIR}/nightly-report.md"
summary_path="${AUDIT_DIR}/nightly-summary.json"
mkdir -p "${log_dir}"

# Hard-fail helper: write a hard-fail report+summary and exit 1. Used when the
# environment cannot even run the gates — that is never reported as a pass.
hard_fail() {
  local reason="$1"
  echo "FATAL: ${reason}" >&2
  python3 "${HERE}/nightly_audit_report.py" \
    --ruff 127 --mypy 127 --pytest 127 --schema-drift 127 --promptfoo-rc 127 \
    --target "${TARGET_REPO}" --sha "unknown" \
    --out-report "${report_path}" --out-summary "${summary_path}" >/dev/null 2>&1 || true
  exit 1
}

command -v git >/dev/null || hard_fail "git not found"
command -v uv  >/dev/null || hard_fail "uv not found (required for ruff/mypy/pytest + schema gate)"

# --- 0) budget guard preflight (Phase 5) --------------------------------------
# The circuit breaker stops us from re-spending tokens on a wedged environment.
# If it is OPEN, budget_guard writes a hard-fail-shaped report+summary here and
# exits 75; we surface that to the cron agent as exit 1 (a skipped audit is never
# announced as a pass). Opt out with BUDGET_GUARD=0.
BUDGET_GUARD="${BUDGET_GUARD:-1}"
export BUDGET_STATE="${BUDGET_STATE:-${AUDIT_DIR}/budget-state.json}"

# When a TensorZero gateway is configured (Phase-5 rollout), tag every model call
# of THIS run with one episode id so its true token total can be summed for the
# per-run cost-cap. Harmless when the gateway is absent.
if [ -n "${TENSORZERO_GATEWAY:-}" ] && [ -z "${TENSORZERO_EPISODE_ID:-}" ]; then
  export TENSORZERO_EPISODE_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
    || python3 -c 'import uuid; print(uuid.uuid4())')"
  echo "==> TensorZero episode: ${TENSORZERO_EPISODE_ID}"
fi
if [ "${BUDGET_GUARD}" != "0" ] && [ "${BUDGET_GUARD}" != "off" ]; then
  if python3 "${HERE}/budget_guard.py" preflight \
       --target "${TARGET_REPO}" \
       --out-report "${report_path}" --out-summary "${summary_path}"; then
    : # breaker closed / half-open trial — proceed.
  else
    pf_rc=$?
    if [ "${pf_rc}" -eq 75 ]; then
      echo "==> budget guard: circuit OPEN — audit skipped (see ${report_path})"
      exit 1   # map the protective skip onto the cron agent's hard-fail contract.
    fi
    hard_fail "budget guard preflight errored (exit ${pf_rc})"
  fi
fi

# --- 1) provision (read-only against the target) ------------------------------
if [ -d "${src_dir}/.git" ]; then
  echo "==> updating ${TARGET_REPO} in ${src_dir}"
  git -C "${src_dir}" fetch --quiet origin || hard_fail "git fetch failed (egress allowlist?)"
else
  echo "==> cloning ${TARGET_REPO} into ${src_dir}"
  git clone --quiet "https://github.com/${TARGET_REPO}.git" "${src_dir}" \
    || hard_fail "git clone failed (egress allowlist? target private?)"
fi
git -C "${src_dir}" checkout --quiet "${TARGET_REF}" || hard_fail "checkout ${TARGET_REF} failed"
git -C "${src_dir}" reset --hard --quiet "origin/${TARGET_REF}" 2>/dev/null || true
sha="$(git -C "${src_dir}" rev-parse --short HEAD)"
echo "==> ${TARGET_REPO} @ ${TARGET_REF} (${sha})"

echo "==> uv sync"
( cd "${src_dir}" && uv sync --all-extras --dev ) >"${log_dir}/uv-sync.log" 2>&1 \
  || hard_fail "uv sync failed — see ${log_dir}/uv-sync.log"

# --- 2) ruff / mypy / pytest --------------------------------------------------
run_in_target() {  # run_in_target <name> <cmd...>  -> echoes the exit code
  local name="$1"; shift
  echo "==> ${name}: $*" >&2
  ( cd "${src_dir}" && "$@" ) >"${log_dir}/${name}.log" 2>&1
  echo $?
}
rc_ruff=$(run_in_target ruff   uv run ruff check .)
rc_mypy=$(run_in_target mypy   uv run mypy .)
rc_pytest=$(run_in_target pytest uv run pytest -q)

# --- 3) schema-drift gate -----------------------------------------------------
echo "==> schema-drift gate (generate_schemas.py --check)"
if [ -f "${src_dir}/schemas/generate_schemas.py" ]; then
  ( cd "${src_dir}" && MCP_SERVER_IMPORT="${MCP_SERVER_IMPORT}" \
      uv run python schemas/generate_schemas.py --check ) \
    >"${log_dir}/schema-drift.log" 2>&1
  rc_schema=$?
else
  echo "    no schemas/generate_schemas.py in target — gate not present" \
    | tee "${log_dir}/schema-drift.log"
  rc_schema=0   # absence is not drift; the promptfoo is-json asserts still guard the contract.
fi

# --- 4) promptfoo eval (contract + OWASP red-team) ----------------------------
echo "==> promptfoo eval (${PROMPTFOO_CONFIG})"
pf_json="${log_dir}/promptfoo.json"
rm -f "${pf_json}"
if [ -f "${src_dir}/${PROMPTFOO_CONFIG}" ]; then
  # Grader override: keep writer != checker. Only pass --grader when the operator
  # set GRADER_PROVIDER (else the config's own cross-family default is used).
  pf_grader=()
  [ -n "${GRADER_PROVIDER:-}" ] && pf_grader=(--grader "${GRADER_PROVIDER}")
  ( cd "${src_dir}" && npx -y "promptfoo@${PROMPTFOO_VERSION}" eval \
      -c "${PROMPTFOO_CONFIG}" \
      --output "${pf_json}" \
      "${pf_grader[@]}" \
      --no-progress-bar ) \
    >"${log_dir}/promptfoo.log" 2>&1
  rc_pf=$?
else
  echo "    no ${PROMPTFOO_CONFIG} in target — red-team/contract layer missing" \
    | tee "${log_dir}/promptfoo.log"
  # The deterministic red-team IS the auditor's job; a missing eval is an
  # infrastructure failure, never a silent "surface looks safe".
  rc_pf=127
fi

# --- 4b) emit raw evidence (for Broker-side classification, S2) ----------------
# In the microVM rollout the untrusted Worker ships THIS file (raw gate exit codes)
# plus the promptfoo JSON — never a self-declared verdict. The trusted Broker then
# re-runs the classifier over it (nightly_audit_report.py --from-evidence), so a
# compromised Worker cannot forge a green outcome. Values are integers / a hex sha
# / the operator-set target, so no untrusted string is interpolated here.
evidence_path="${AUDIT_DIR}/nightly-evidence.json"
cat > "${evidence_path}" <<EOF
{
  "schema": 1,
  "target": "${TARGET_REPO}",
  "target_sha": "${sha}",
  "gates": {
    "ruff": ${rc_ruff},
    "mypy": ${rc_mypy},
    "pytest": ${rc_pytest},
    "schema_drift": ${rc_schema},
    "promptfoo_rc": ${rc_pf}
  }
}
EOF

# --- 5) classify + report -----------------------------------------------------
echo "==> aggregating into ${report_path}"
python3 "${HERE}/nightly_audit_report.py" \
  --ruff "${rc_ruff}" --mypy "${rc_mypy}" --pytest "${rc_pytest}" \
  --schema-drift "${rc_schema}" \
  --promptfoo-rc "${rc_pf}" --promptfoo-json "${pf_json}" \
  --target "${TARGET_REPO}" --sha "${sha}" \
  --out-report "${report_path}" --out-summary "${summary_path}"
outcome_rc=$?

# --- 6) budget guard record (Phase 5) -----------------------------------------
# Feed the outcome + measured token usage back to the breaker for the next run.
# Recording reflects what already happened — it never changes this run's exit
# code (no --strict), so a budget breach trips the breaker for *tomorrow*, not a
# rewrite of today's green/findings verdict.
if [ "${BUDGET_GUARD}" != "0" ] && [ "${BUDGET_GUARD}" != "off" ]; then
  # Prefer TensorZero's full per-run token total (writer + grader) when the
  # gateway is in use; otherwise fall back to the promptfoo-only count.
  budget_tokens=()
  if [ -n "${TENSORZERO_GATEWAY:-}" ] && [ -n "${TENSORZERO_EPISODE_ID:-}" ]; then
    ep_tokens="$("${REPO_ROOT}/deploy/tensorzero/episode-tokens.sh" "${TENSORZERO_EPISODE_ID}" 2>/dev/null)" \
      && [ -n "${ep_tokens}" ] && budget_tokens=(--tokens "${ep_tokens}")
  fi
  if [ "${#budget_tokens[@]}" -eq 0 ]; then
    budget_tokens=(--promptfoo-json "${pf_json}")
  fi
  python3 "${HERE}/budget_guard.py" record \
    --exit-code "${outcome_rc}" "${budget_tokens[@]}" || true
fi

echo
echo "===== NIGHTLY AUDIT  ${TARGET_REPO}@${sha} ====="
echo "  report : ${report_path}"
echo "  summary: ${summary_path}"
echo "  logs   : ${log_dir}/"
echo "  exit   : ${outcome_rc}  (0 green / 2 findings / 1 hard-fail)"
exit "${outcome_rc}"
