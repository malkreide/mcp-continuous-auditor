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

# The per-connection handler. Written next to this script so socat can exec it.
cat > "${handler}" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
dropbox="${DROPBOX:?}"
report_py="${REPORT_PY:?}"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="${dropbox}/${ts}-$$"
mkdir -p "${run_dir}"
# First line = header; the rest of the stream = the tar bundle.
IFS= read -r header
echo "${header}" > "${run_dir}/header.txt"
# Extract ONLY the two evidence files by their EXACT names, into the isolated
# run_dir. The explicit member list IS the path-traversal guard: names have no `/`
# or `..`, and any other archive member (incl. absolute/`../` paths) simply does
# not match and is never extracted — portable across tar builds, no reliance on a
# non-portable --no-absolute-names flag.
tar -C "${run_dir}" -xf - nightly-evidence.json promptfoo.json 2>/dev/null || true
# TRUSTED classification: re-derive the verdict from the raw evidence with the
# Broker's own classifier. Missing/garbled evidence -> hard-fail, never green.
python3 "${report_py}" \
  --from-evidence "${run_dir}/nightly-evidence.json" \
  --promptfoo-json "${run_dir}/promptfoo.json" \
  --out-report "${run_dir}/nightly-report.md" \
  --out-summary "${run_dir}/nightly-summary.json" >/dev/null 2>&1
# Belt-and-suspenders: if the classifier itself could not run at all, synthesize a
# hard-fail summary so the cron agent never sees an absent/green verdict by default.
if [ ! -s "${run_dir}/nightly-summary.json" ]; then
  printf '{"outcome":"hard-fail","exit_code":1,"green":false,"hard_fail":true,"hard_fail_reasons":["broker classifier could not run"],"target":"unknown","target_sha":"unknown"}\n' \
    > "${run_dir}/nightly-summary.json"
  printf '# ⛔ Nightly audit — broker classification failed\n\nThe evidence bundle could not be classified. Do NOT treat as passed.\n' \
    > "${run_dir}/nightly-report.md"
fi
outcome="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("outcome","?"))' "${run_dir}/nightly-summary.json" 2>/dev/null || echo '?')"
echo "[$(date -u +%H:%M:%S)] received -> ${run_dir} (${header}; broker-classified: ${outcome})" >&2
EOF
chmod +x "${handler}"

echo "== Broker vsock listener =="
echo "  port    : ${port}  (guests connect to host CID 2, port ${port})"
echo "  dropbox : ${dropbox}/<timestamp>-<pid>/"
echo "  classify: ${REPORT_PY}  (Broker re-derives the verdict; Worker is untrusted)"
echo "  stop    : Ctrl-C"
echo

# VSOCK-LISTEN binds the host side; fork = one handler per delivery.
exec env DROPBOX="${dropbox}" REPORT_PY="${REPORT_PY}" \
  socat "VSOCK-LISTEN:${port},reuseaddr,fork" "EXEC:${handler}"
