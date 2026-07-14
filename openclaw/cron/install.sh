#!/usr/bin/env bash
#
# install.sh — register the daily 03:00 nightly-audit cron job in OpenClaw
# (Plan Phase 4). OpenClaw manages schedules through the CLI (`openclaw cron
# add|edit|remove`), not a config file, so this script is how the committed spec
# in openclaw/cron/nightly-audit.json becomes a live job. The JSON is the single
# source of truth for the prompt; this script reads it and fills the ${...}
# placeholders from the environment.
#
# HARD-FAIL ON UNRESOLVABLE MODEL (the headline Phase-4 requirement):
#   * OPENCLAW_AUDIT_MODEL is REQUIRED — there is no default, so the job is never
#     registered against an implicit/silent model;
#   * the job is created with `--fallbacks ""` (strict, no fallback chain), so
#     OpenClaw fails the run with a validation error if the model cannot be
#     resolved at runtime instead of quietly switching models.
#
# Run from the repo root on the gateway host:
#   OPENCLAW_AUDIT_MODEL="anthropic/claude-opus-4-6" \
#   TELEGRAM_ANNOUNCE_TO="123456789" \
#     openclaw/cron/install.sh
#
# Env:
#   JOB                    which committed spec to install: nightly-audit
#                          (default) or improve-loop (Plan Phase 6c — the
#                          weekly Sunday-04:30 improve run).
#   OPENCLAW_AUDIT_MODEL   (required) explicit, resolvable model ref for the
#                          auditor agent. Keep it a DIFFERENT family than the
#                          promptfoo grader (writer != checker — see README).
#   TELEGRAM_ANNOUNCE_TO   (required) chat/user id the report is announced to
#                          (e.g. 123456789, or -1001234567890:topic:42 for a
#                          forum topic).
#   OPENCLAW_CRON_REPLACE  set to 1 to remove an existing job of the same name
#                          before creating (otherwise install refuses to clobber).
#   DRY_RUN                set to 1 to print the command without executing it.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_NAME="${JOB:-nightly-audit}"
case "${JOB_NAME}" in
  nightly-audit|improve-loop) ;;
  *) echo "FATAL: unknown JOB='${JOB_NAME}' — use nightly-audit or improve-loop" >&2; exit 1 ;;
esac
SPEC="${HERE}/${JOB_NAME}.json"

: "${OPENCLAW_AUDIT_MODEL:?set OPENCLAW_AUDIT_MODEL to an explicit, resolvable model ref — there is no default, so the auditor never runs on a silent/implicit model}"
: "${TELEGRAM_ANNOUNCE_TO:?set TELEGRAM_ANNOUNCE_TO to the Telegram chat/user id for the --announce report}"

command -v openclaw >/dev/null || { echo "FATAL: openclaw CLI not found on PATH" >&2; exit 127; }
command -v python3  >/dev/null || { echo "FATAL: python3 not found (needed to read the spec)" >&2; exit 127; }
[ -f "${SPEC}" ] || { echo "FATAL: spec not found: ${SPEC}" >&2; exit 1; }

# Pull the schedule, tz and prompt straight out of the committed spec so the CLI
# call can never drift from the reviewed JSON. python reads the file directly —
# the prompt is never interpolated through a shell (TOOLS.md: untrusted/no-eval).
read_spec() { python3 - "$SPEC" "$1" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1], encoding="utf-8"))
key = sys.argv[2]
node = spec
for part in key.split("."):
    node = node[part]
sys.stdout.write(node if isinstance(node, str) else json.dumps(node))
PY
}

CRON_EXPR="$(read_spec schedule.value)"
CRON_TZ="$(read_spec schedule.tz)"
MESSAGE="$(read_spec payload.message)"
THINKING="$(read_spec payload.thinking)"
TIMEOUT="$(read_spec payload.timeoutSeconds)"

# Idempotency: refuse to create a duplicate unless OPENCLAW_CRON_REPLACE=1.
existing_id="$(openclaw cron list --json 2>/dev/null \
  | python3 -c 'import json,sys
try: jobs=json.load(sys.stdin)
except Exception: jobs=[]
jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
print(next((j.get("id","") for j in jobs if j.get("name")=="'"${JOB_NAME}"'"), ""))' \
  || true)"

if [ -n "${existing_id}" ]; then
  if [ "${OPENCLAW_CRON_REPLACE:-0}" = "1" ]; then
    echo "==> removing existing '${JOB_NAME}' job (${existing_id}) [OPENCLAW_CRON_REPLACE=1]"
    [ "${DRY_RUN:-0}" = "1" ] || openclaw cron remove "${existing_id}"
  else
    echo "A '${JOB_NAME}' cron job already exists (id ${existing_id})." >&2
    echo "Re-run with OPENCLAW_CRON_REPLACE=1 to replace it, or edit it with:" >&2
    echo "  openclaw cron edit ${existing_id} --model \"\$OPENCLAW_AUDIT_MODEL\" --fallbacks \"\"" >&2
    exit 1
  fi
fi

# Build the create command. --fallbacks "" => strict, no silent model fallback.
set -- \
  cron create "${CRON_EXPR}" "${MESSAGE}" \
  --name "${JOB_NAME}" \
  --tz "${CRON_TZ}" \
  --session isolated \
  --model "${OPENCLAW_AUDIT_MODEL}" \
  --fallbacks "" \
  --thinking "${THINKING}" \
  --timeout-seconds "${TIMEOUT}" \
  --announce \
  --channel telegram \
  --to "${TELEGRAM_ANNOUNCE_TO}"

echo "==> openclaw cron create '${JOB_NAME}'  (${CRON_EXPR} ${CRON_TZ}, model=${OPENCLAW_AUDIT_MODEL}, fallbacks=strict)"
if [ "${DRY_RUN:-0}" = "1" ]; then
  printf '   DRY_RUN — would run: openclaw'; printf ' %q' "$@"; printf '\n'
  exit 0
fi
openclaw "$@"
echo "==> done. Inspect with: openclaw cron list   ·   trigger once with: openclaw cron run ${JOB_NAME}"
