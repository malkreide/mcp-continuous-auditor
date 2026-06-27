#!/usr/bin/env bash
#
# broker-listener.sh <port> <dropbox-dir> — receive Worker audit results on vsock.
#
# Runs on the host (Broker side). The Worker connects over vsock and streams a
# one-line header + a tar of {nightly-summary.json, nightly-report.md}. We unpack
# each delivery into a per-run dir under the dropbox. The OpenClaw cron agent then
# reads summary.json from there and routes it (issue / Telegram / gated PR) — the
# SAME contract as before, except the untrusted read happened in a throwaway VM.
#
# SECURITY: everything received is UNTRUSTED (AGENTS.md). We only ever:
#   - read the exit code from the header as an integer,
#   - extract a FIXED, known set of filenames (never honor paths from the tar),
#   - never exec anything from the payload.
#
# This is a long-running listener (socat ... fork): start it before run-worker.sh
# and leave it up (e.g. a systemd unit). Ctrl-C to stop.
set -uo pipefail

port="${1:?usage: broker-listener.sh <port> <dropbox-dir>}"
dropbox="${2:?usage: broker-listener.sh <port> <dropbox-dir>}"
mkdir -p "${dropbox}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
handler="${HERE}/_receive-one.sh"

# The per-connection handler. Written next to this script so socat can exec it.
cat > "${handler}" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
dropbox="${DROPBOX:?}"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="${dropbox}/${ts}-$$"
mkdir -p "${run_dir}"
# First line = header; the rest of the stream = the tar bundle.
IFS= read -r header
echo "${header}" > "${run_dir}/header.txt"
# Extract ONLY the two known files; ignore anything else in the archive and
# refuse absolute/.. paths (tar --no-absolute-names + explicit member list).
tar -C "${run_dir}" --no-absolute-names -xf - \
  nightly-summary.json nightly-report.md 2>/dev/null || true
echo "[$(date -u +%H:%M:%S)] received -> ${run_dir} (${header})" >&2
EOF
chmod +x "${handler}"

echo "== Broker vsock listener =="
echo "  port    : ${port}  (guests connect to host CID 2, port ${port})"
echo "  dropbox : ${dropbox}/<timestamp>-<pid>/"
echo "  stop    : Ctrl-C"
echo

# VSOCK-LISTEN binds the host side; fork = one handler per delivery.
exec env DROPBOX="${dropbox}" \
  socat "VSOCK-LISTEN:${port},reuseaddr,fork" "EXEC:${handler}"
