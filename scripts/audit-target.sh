#!/usr/bin/env bash
#
# audit-target.sh — provision the target MCP server into the auditor sandbox
# and run the read-only toolchain (ruff + mypy + pytest).
#
# This is the harness that unblocks the python-auditor skill: it makes the
# target source available locally, then runs the three commands and records
# each exit code + log. It does NOT interpret the output — the agent reads the
# logs under .audit/logs/ and writes the report (docs/audits/<date>.md),
# quoting every non-zero finding as `path:line — message` from stderr.
#
# Read-only contract (Plan Phase 1 / AGENTS.md):
#   - clones/updates the target, never writes to it, never pushes.
#   - no deploy, no `main` mutation.
#
# Policy (openclaw/workspace/TOOLS.md):
#   - no `curl | sh`; the target is fetched with git over HTTPS only.
#   - the GitHub token is never echoed, written to a file, or put on a command
#     line. For a PUBLIC target no token is needed. For a private target,
#     configure a git credential helper out-of-band — do NOT inline the token
#     into the clone URL (it would persist in .git/config and logs).
#
# Egress: must run on a host whose allowlist permits the target repo origin
# (see openclaw/workspace/TOOLS.md and docs/deployment/raspberry-pi.md). This
# is why it cannot run from the auditor's restricted control-plane session.
#
# Usage:
#   TARGET_REPO=malkreide/zurich-opendata-mcp scripts/audit-target.sh [REF]
#
# Env:
#   TARGET_REPO  owner/name of the target (default: malkreide/zurich-opendata-mcp)
#   TARGET_REF   git ref to pin for reproducibility (default: main)
#   AUDIT_DIR    work dir, gitignored (default: .audit)
#
set -euo pipefail

TARGET_REPO="${TARGET_REPO:-malkreide/zurich-opendata-mcp}"
TARGET_REF="${TARGET_REF:-${1:-main}}"
AUDIT_DIR="${AUDIT_DIR:-.audit}"

repo_name="${TARGET_REPO##*/}"
src_dir="${AUDIT_DIR}/${repo_name}"
log_dir="${AUDIT_DIR}/logs"
mkdir -p "${log_dir}"

command -v git >/dev/null || { echo "FATAL: git not found" >&2; exit 127; }
command -v uv  >/dev/null || { echo "FATAL: uv not found (required by python-auditor)" >&2; exit 127; }

# --- provision (read-only against the target) ---------------------------------
if [ -d "${src_dir}/.git" ]; then
  echo "==> updating ${TARGET_REPO} in ${src_dir}"
  git -C "${src_dir}" fetch --quiet origin
else
  echo "==> cloning ${TARGET_REPO} into ${src_dir}"
  git clone --quiet "https://github.com/${TARGET_REPO}.git" "${src_dir}"
fi
git -C "${src_dir}" checkout --quiet "${TARGET_REF}"
git -C "${src_dir}" reset --hard --quiet "origin/${TARGET_REF}" 2>/dev/null || true
sha="$(git -C "${src_dir}" rev-parse --short HEAD)"
echo "==> ${TARGET_REPO} @ ${TARGET_REF} (${sha})"

# --- sync deps ----------------------------------------------------------------
echo "==> uv sync"
( cd "${src_dir}" && uv sync --all-extras --dev ) >"${log_dir}/uv-sync.log" 2>&1 \
  || { echo "FATAL: uv sync failed — see ${log_dir}/uv-sync.log" >&2; exit 1; }

# --- run the three commands, capture each exit code ---------------------------
declare -A rc
run() {  # run <name> <cmd...>
  local name="$1"; shift
  echo "==> ${name}: $*"
  ( cd "${src_dir}" && "$@" ) >"${log_dir}/${name}.log" 2>&1 && rc[$name]=0 || rc[$name]=$?
  echo "    exit=${rc[$name]}  log=${log_dir}/${name}.log"
}

run ruff   uv run ruff check .
run mypy   uv run mypy .
run pytest uv run pytest -q

# --- summary (exit codes are the ground truth) --------------------------------
echo
echo "===== AUDIT SUMMARY  ${TARGET_REPO}@${sha} ====="
printf '  %-7s exit=%s\n' ruff "${rc[ruff]}" mypy "${rc[mypy]}" pytest "${rc[pytest]}"
echo "  logs: ${log_dir}/{ruff,mypy,pytest}.log"
echo "  next: agent reads logs, writes docs/audits/$(date +%F).md with file:line cites"

# Non-zero overall if any command failed — lets CI/cron gate on it.
[ "${rc[ruff]}" -eq 0 ] && [ "${rc[mypy]}" -eq 0 ] && [ "${rc[pytest]}" -eq 0 ]
