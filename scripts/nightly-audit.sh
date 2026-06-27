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
#
set -uo pipefail   # NOT -e: we must observe every gate's exit code, not abort on the first red one.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"

TARGET_REPO="${TARGET_REPO:-malkreide/zurich-opendata-mcp}"
TARGET_REF="${TARGET_REF:-${1:-main}}"
AUDIT_DIR="${AUDIT_DIR:-${REPO_ROOT}/.audit}"
MCP_SERVER_IMPORT="${MCP_SERVER_IMPORT:-zurich_opendata_mcp.server:mcp}"
PROMPTFOO_CONFIG="${PROMPTFOO_CONFIG:-promptfoo/promptfooconfig.yaml}"

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
  ( cd "${src_dir}" && npx -y promptfoo@latest eval \
      -c "${PROMPTFOO_CONFIG}" \
      --output "${pf_json}" \
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

# --- 5) classify + report -----------------------------------------------------
echo "==> aggregating into ${report_path}"
python3 "${HERE}/nightly_audit_report.py" \
  --ruff "${rc_ruff}" --mypy "${rc_mypy}" --pytest "${rc_pytest}" \
  --schema-drift "${rc_schema}" \
  --promptfoo-rc "${rc_pf}" --promptfoo-json "${pf_json}" \
  --target "${TARGET_REPO}" --sha "${sha}" \
  --out-report "${report_path}" --out-summary "${summary_path}"
outcome_rc=$?

echo
echo "===== NIGHTLY AUDIT  ${TARGET_REPO}@${sha} ====="
echo "  report : ${report_path}"
echo "  summary: ${summary_path}"
echo "  logs   : ${log_dir}/"
echo "  exit   : ${outcome_rc}  (0 green / 2 findings / 1 hard-fail)"
exit "${outcome_rc}"
