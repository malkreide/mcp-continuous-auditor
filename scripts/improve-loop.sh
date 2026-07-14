#!/usr/bin/env bash
#
# improve-loop.sh — deterministic orchestrator of the Phase-6c improve loop
# (docs/plans/2026-07-13-phase-6-improve-loop.md). The autoresearch pattern,
# applied to the audit suite: per iteration ONE writer proposal, judged by the
# committed acceptance rule (improve_acceptance.py, D1/D2/D3); keeps are
# committed onto improve/<datum>; the run ends in a report and (optionally) a
# DRAFT PR on the target repo. The loop never merges — the human is the gate.
#
# Invariants enforced HERE, not just documented (plan: Invarianten 1–6):
#   * only files under promptfoo/ are ever committed (path check before every
#     commit, in addition to the judge's out-of-scope discard);
#   * infrastructure failures abort the run (exit 1) and are never verdicts;
#   * the budget guard runs with its OWN state file, so an expensive improve
#     run can never trip the nightly audit's breaker (and vice versa);
#   * per-iteration budget: every iteration is recorded individually, with the
#     writer's measured token spend.
#
# Exit code (the contract with the cron agent):
#   0  loop completed (with or without keeps — see improve-summary.json)
#   1  HARD failure: infrastructure broke, or the budget breaker skipped the run
#
# Usage:
#   WRITER_CMD="python3 scripts/improve_writer.py" scripts/improve-loop.sh
#
# Env:
#   TARGET_REPO          owner/name of the target (default: malkreide/zurich-opendata-mcp)
#   TARGET_REF           base ref the improve branch is cut from (default: main)
#   TARGET_GIT_URL       clone URL (default: https://github.com/$TARGET_REPO.git;
#                        overridable for tests / local mirrors)
#   AUDIT_DIR            work dir, gitignored (default: <repo>/.audit)
#   WRITER_CMD           (required) proposal command: `$WRITER_CMD <target-dir>
#                        <out-patch>`; exit 0 = patch written (optional
#                        <out-patch>.tokens with the call's token total),
#                        10 = no proposal (graceful stop), else hard-fail.
#   IMPROVE_RUNNER       suite runner for the judge (default:
#                        scripts/run-determ-eval.sh — pinned promptfoo)
#   IMPROVE_CONFIG       determ config in the target (default:
#                        promptfoo/promptfooconfig.determ.yaml)
#   IMPROVE_MAX_ITER     iterations ceiling (default: 10)
#   IMPROVE_MAX_KEEPS    keeps ceiling per run — suite-bloat guard (default: 5)
#   IMPROVE_COVERAGE_MODE  D3 mode: schema-path (default) | mutation | off
#   IMPROVE_MUTANTS_DIR  mutant pool (required for mutation mode)
#   IMPROVE_BRANCH       branch name (default: improve/<YYYY-MM-DD>)
#   IMPROVE_PUBLISH      1 = push branch + open draft PR (needs GITHUB_TOKEN);
#                        default 0 — report only.
#   BUDGET_GUARD         default on; BUDGET_STATE defaults to the improve-own
#                        .audit/improve-budget-state.json.
#   IMPROVE_ITER_TOKEN_CEILING  per-iteration token ceiling (maps onto
#                        BUDGET_TOKENS_PER_RUN for the per-iteration records).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"

TARGET_REPO="${TARGET_REPO:-malkreide/zurich-opendata-mcp}"
TARGET_REF="${TARGET_REF:-main}"
TARGET_GIT_URL="${TARGET_GIT_URL:-https://github.com/${TARGET_REPO}.git}"
AUDIT_DIR="${AUDIT_DIR:-${REPO_ROOT}/.audit}"
IMPROVE_RUNNER="${IMPROVE_RUNNER:-${HERE}/run-determ-eval.sh}"
IMPROVE_CONFIG="${IMPROVE_CONFIG:-promptfoo/promptfooconfig.determ.yaml}"
IMPROVE_MAX_ITER="${IMPROVE_MAX_ITER:-10}"
IMPROVE_MAX_KEEPS="${IMPROVE_MAX_KEEPS:-5}"
IMPROVE_COVERAGE_MODE="${IMPROVE_COVERAGE_MODE:-schema-path}"
IMPROVE_BRANCH="${IMPROVE_BRANCH:-improve/$(date +%Y-%m-%d)}"
IMPROVE_PUBLISH="${IMPROVE_PUBLISH:-0}"

repo_name="${TARGET_REPO##*/}"
work_dir="${AUDIT_DIR}/improve"
src_dir="${work_dir}/${repo_name}"
journal="${IMPROVE_JOURNAL:-${AUDIT_DIR}/experiments.jsonl}"
report_path="${AUDIT_DIR}/improve-report.md"
summary_path="${AUDIT_DIR}/improve-summary.json"
mkdir -p "${work_dir}"

# Own budget state — an improve run must never trip the nightly breaker.
BUDGET_GUARD="${BUDGET_GUARD:-1}"
export BUDGET_STATE="${BUDGET_STATE:-${AUDIT_DIR}/improve-budget-state.json}"
# Per-iteration ceiling: each iteration is one budget_guard "run".
[ -n "${IMPROVE_ITER_TOKEN_CEILING:-}" ] && export BUDGET_TOKENS_PER_RUN="${IMPROVE_ITER_TOKEN_CEILING}"

hard_fail() {
  local reason="$1"
  echo "FATAL: ${reason}" >&2
  python3 - "$reason" "$TARGET_REPO" "$IMPROVE_BRANCH" "$summary_path" "$report_path" <<'PY' || true
import json, sys
from datetime import datetime, timezone
reason, target, branch, summary_path, report_path = sys.argv[1:6]
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
summary = {"schema": 1, "generated_at": now, "target": target, "branch": branch,
           "outcome": "hard-fail", "iterations": 0, "keeps": [], "discards": {},
           "hard_fail_reasons": [reason]}
open(summary_path, "w", encoding="utf-8").write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
open(report_path, "w", encoding="utf-8").write(
    f"# ⛔ Improve loop — `{target}`\n\n- Outcome: **hard-fail**\n- Run (UTC): {now}\n\n"
    f"**HARD FAILURE — the loop did NOT complete. An aborted run is not a result.**\n\n- {reason}\n")
PY
  exit 1
}

budget_record() {  # budget_record <exit-code> <tokens>
  [ "${BUDGET_GUARD}" = "0" ] || [ "${BUDGET_GUARD}" = "off" ] && return 0
  python3 "${HERE}/budget_guard.py" record --exit-code "$1" --tokens "$2" || true
}

command -v git >/dev/null || hard_fail "git not found"
command -v python3 >/dev/null || hard_fail "python3 not found"
[ -n "${WRITER_CMD:-}" ] || hard_fail "WRITER_CMD is not set — the loop needs a proposal command"
if [ "${IMPROVE_COVERAGE_MODE}" = "mutation" ] && [ -z "${IMPROVE_MUTANTS_DIR:-}" ]; then
  hard_fail "IMPROVE_COVERAGE_MODE=mutation requires IMPROVE_MUTANTS_DIR"
fi

# --- 0) budget guard preflight (breaker on the improve-own state) --------------
if [ "${BUDGET_GUARD}" != "0" ] && [ "${BUDGET_GUARD}" != "off" ]; then
  if ! python3 "${HERE}/budget_guard.py" preflight --target "${TARGET_REPO}"; then
    pf_rc=$?
    [ "${pf_rc}" -eq 75 ] && hard_fail "budget guard: circuit OPEN — improve run skipped"
    hard_fail "budget guard preflight errored (exit ${pf_rc})"
  fi
fi

# --- 1) provision the target + cut the improve branch --------------------------
if [ -d "${src_dir}/.git" ]; then
  echo "==> updating ${TARGET_REPO} in ${src_dir}"
  git -C "${src_dir}" fetch --quiet origin || hard_fail "git fetch failed"
else
  echo "==> cloning ${TARGET_GIT_URL} into ${src_dir}"
  git clone --quiet "${TARGET_GIT_URL}" "${src_dir}" || hard_fail "git clone failed"
fi
if git -C "${src_dir}" rev-parse --verify --quiet "origin/${TARGET_REF}" >/dev/null; then
  base_ref="origin/${TARGET_REF}"
else
  git -C "${src_dir}" rev-parse --verify --quiet "${TARGET_REF}" >/dev/null \
    || hard_fail "TARGET_REF '${TARGET_REF}' not found in the target"
  base_ref="${TARGET_REF}"
fi
git -C "${src_dir}" checkout --quiet -B "${IMPROVE_BRANCH}" "${base_ref}" \
  || hard_fail "could not cut branch ${IMPROVE_BRANCH} from ${base_ref}"
git -C "${src_dir}" config user.email >/dev/null 2>&1 \
  || git -C "${src_dir}" config user.email "improve-loop@localhost"
git -C "${src_dir}" config user.name >/dev/null 2>&1 \
  || git -C "${src_dir}" config user.name "improve-loop"
sha="$(git -C "${src_dir}" rev-parse --short HEAD)"
echo "==> ${TARGET_REPO} @ ${base_ref} (${sha}) -> ${IMPROVE_BRANCH}"

# --- 2) baseline (clean suite + mutation kill map, cached per SHA) -------------
judge_args=(
  --target-dir "${src_dir}" --config "${IMPROVE_CONFIG}"
  --runner "${IMPROVE_RUNNER}"
  --coverage-mode "${IMPROVE_COVERAGE_MODE}"
)
[ -n "${IMPROVE_MUTANTS_DIR:-}" ] && judge_args+=(--mutants-dir "${IMPROVE_MUTANTS_DIR}")

python3 "${HERE}/improve_acceptance.py" --journal "${journal}" baseline "${judge_args[@]}" \
  || hard_fail "baseline not reproducible / not computable — fix the existing suite first"

# --- 3) the loop: propose -> judge -> keep/discard ------------------------------
journal_offset=0
[ -f "${journal}" ] && journal_offset="$(wc -l < "${journal}")"
keeps=0

for i in $(seq 1 "${IMPROVE_MAX_ITER}"); do
  patch="${work_dir}/candidate-${i}.patch"
  rm -f "${patch}" "${patch}.tokens"
  echo "==> iteration ${i}/${IMPROVE_MAX_ITER}: writer proposal"
  ${WRITER_CMD} "${src_dir}" "${patch}"
  wrc=$?
  if [ "${wrc}" -eq 10 ]; then
    echo "==> writer: no further proposals — ending loop"
    break
  elif [ "${wrc}" -ne 0 ]; then
    budget_record 1 0
    hard_fail "writer failed (exit ${wrc}) on iteration ${i}"
  fi
  tokens=0
  [ -f "${patch}.tokens" ] && tokens="$(tr -dc '0-9' < "${patch}.tokens")"
  tokens="${tokens:-0}"

  python3 "${HERE}/improve_acceptance.py" --journal "${journal}" judge \
    "${judge_args[@]}" --candidate "${patch}"
  jrc=$?

  case "${jrc}" in
    0)
      # Belt + braces: the judge already discards out-of-scope candidates, but
      # the plan demands the path check be enforced before EVERY commit.
      outside="$(git -C "${src_dir}" apply --numstat "${patch}" | cut -f3 | grep -v '^promptfoo/' || true)"
      [ -z "${outside}" ] || { budget_record 1 "${tokens}"; hard_fail "keep touches files outside promptfoo/: ${outside}"; }
      git -C "${src_dir}" apply "${patch}" \
        || { budget_record 1 "${tokens}"; hard_fail "kept candidate no longer applies"; }
      git -C "${src_dir}" add -A -- promptfoo/ \
        && git -C "${src_dir}" commit --quiet -m "improve: candidate-${i} (kept by D1-D3)" \
        || { budget_record 1 "${tokens}"; hard_fail "commit of kept candidate failed"; }
      keeps=$((keeps + 1))
      budget_record 0 "${tokens}"
      echo "==> KEEP (${keeps}/${IMPROVE_MAX_KEEPS})"
      if [ "${keeps}" -ge "${IMPROVE_MAX_KEEPS}" ]; then
        echo "==> keeps ceiling reached — ending loop (suite-bloat guard)"
        break
      fi
      ;;
    2)
      budget_record 2 "${tokens}"
      ;;
    *)
      budget_record 1 "${tokens}"
      hard_fail "judge hard-failed on iteration ${i} — aborting the run"
      ;;
  esac
done

# --- 4) report ------------------------------------------------------------------
python3 "${HERE}/improve_loop_support.py" report \
  --journal "${journal}" --skip-lines "${journal_offset}" \
  --target "${TARGET_REPO}" --sha "${sha}" --branch "${IMPROVE_BRANCH}" \
  --out-report "${report_path}" --out-summary "${summary_path}" \
  || hard_fail "report generation failed"

# --- 5) publish (opt-in): push branch + draft PR --------------------------------
if [ "${keeps}" -gt 0 ] && [ "${IMPROVE_PUBLISH}" = "1" ]; then
  [ -n "${GITHUB_TOKEN:-}" ] || hard_fail "IMPROVE_PUBLISH=1 needs GITHUB_TOKEN"
  echo "==> pushing ${IMPROVE_BRANCH} to ${TARGET_REPO}"
  # Token via credential helper from the environment — never inlined into the
  # URL or a process argument (openclaw/workspace/TOOLS.md).
  git -C "${src_dir}" \
    -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
    push --quiet -u origin "${IMPROVE_BRANCH}" \
    || hard_fail "git push of ${IMPROVE_BRANCH} failed"
  python3 "${HERE}/improve_loop_support.py" publish \
    --repo "${TARGET_REPO}" --branch "${IMPROVE_BRANCH}" --base "${TARGET_REF}" \
    --title "improve: ${IMPROVE_BRANCH} (${keeps} kept candidate(s))" \
    --body-file "${report_path}" \
    || hard_fail "draft-PR creation failed"
fi

echo
echo "===== IMPROVE LOOP  ${TARGET_REPO}@${sha} ====="
echo "  branch : ${IMPROVE_BRANCH}  (keeps: ${keeps})"
echo "  report : ${report_path}"
echo "  summary: ${summary_path}"
exit 0
