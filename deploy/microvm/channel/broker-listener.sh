#!/usr/bin/env bash
#
# broker-listener.sh <port> <dropbox-dir> — receive Worker audit RESULTS on vsock
# and CLASSIFY them on the trusted Broker side.
#
# Runs on the host (Broker side). The Worker connects over vsock and streams a
# one-line header + a tar of RAW EVIDENCE: {nightly-evidence.json, promptfoo.json}.
# For each delivery we unpack the evidence into a per-run dir, then re-run the
# auditor's own classifier (scripts/nightly_audit_report.py --from-evidence) HERE,
# producing the authoritative nightly-summary.json + nightly-report.md. The OpenClaw
# cron agent then reads those Broker-produced files and routes them (issue /
# Telegram / gated PR) — the SAME contract as before.
#
# SECURITY (AGENTS.md + Analysis S2): everything received is UNTRUSTED. We NEVER
# trust the Worker's verdict — the trusted Broker re-derives it from the raw
# evidence. Concretely we only:
#   - read the exit code from the header as an integer,
#   - extract a FIXED, known set of filenames by exact name (any other/absolute/..
#     member simply does not match and is never extracted),
#   - never exec anything from the payload,
#   - and classify with OUR copy of the classifier, where missing/garbled evidence
#     becomes HARD-FAIL, never green — so a compromised Worker cannot forge a pass.
#
# Residual risk: a fully-compromised Worker can still ship *self-consistent* green
# evidence (all-zero exit codes + a clean promptfoo JSON). Defeating that needs
# result attestation and is out of scope; the win here is that omission, garbling,
# or an exit-code/promptfoo mismatch can no longer read as green.
#
# This is a long-running listener (socat ... fork): start it before run-worker.sh
# and leave it up (e.g. a systemd unit). Ctrl-C to stop.
set -uo pipefail

port="${1:?usage: broker-listener.sh <port> <dropbox-dir>}"
dropbox="${2:?usage: broker-listener.sh <port> <dropbox-dir>}"
mkdir -p "${dropbox}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
REPORT_PY="${REPO_ROOT}/scripts/nightly_audit_report.py"
handler="${HERE}/_receive-one.sh"

[ -f "${REPORT_PY}" ] || { echo "FATAL: classifier not found at ${REPORT_PY}" >&2; exit 1; }
command -v python3 >/dev/null || { echo "FATAL: python3 required for Broker classification" >&2; exit 1; }

# The per-connection handler is a committed script (deploy/microvm/channel/
# _receive-one.sh) rather than a heredoc generated here at startup, so the exact
# code socat exec's is the same code tests/test_broker_pipeline.py exercises.
[ -f "${handler}" ] || { echo "FATAL: handler not found at ${handler}" >&2; exit 1; }
chmod +x "${handler}" 2>/dev/null || true

echo "== Broker vsock listener =="
echo "  port    : ${port}  (guests connect to host CID 2, port ${port})"
echo "  dropbox : ${dropbox}/<timestamp>-<pid>/"
echo "  classify: ${REPORT_PY}  (Broker re-derives the verdict; Worker is untrusted)"
echo "  stop    : Ctrl-C"
echo

# VSOCK-LISTEN binds the host side; fork = one handler per delivery.
exec env DROPBOX="${dropbox}" REPORT_PY="${REPORT_PY}" \
  socat "VSOCK-LISTEN:${port},reuseaddr,fork" "EXEC:${handler}"
