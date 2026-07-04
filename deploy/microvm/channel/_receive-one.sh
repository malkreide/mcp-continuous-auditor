#!/usr/bin/env bash
#
# _receive-one.sh — per-connection Broker handler, exec'd by broker-listener.sh
# for every vsock delivery (socat ... EXEC:_receive-one.sh). Also invoked directly
# by tests/test_broker_pipeline.py, so it ships as a committed file (the test
# exercises this exact code — it cannot drift from what ships).
#
# It reads a one-line header + a tar of RAW EVIDENCE from stdin, extracts ONLY the
# two known evidence files by EXACT name, and re-derives the verdict with the
# Broker's OWN classifier. Everything on stdin is UNTRUSTED — see broker-listener.sh
# for the full threat model (Analysis S2).
#
# HARDENING (Analysis S-D): the stream is bounded in SIZE (head -c) and TIME
# (read -t on the header + timeout on the extraction) so a compromised Worker can
# neither fill the Broker disk nor pin a handler forever; SYMLINK members are
# dropped (so the classifier is never turned into an arbitrary-file read); and the
# untrusted header is stripped of control chars + truncated before it touches a
# file or the operator's terminal. target/sha sanitising happens at the sink
# (nightly_audit_report.py), where the promptfoo examples are cleaned too.
#
# Env: DROPBOX (per-run dirs), REPORT_PY (nightly_audit_report.py). Tunables:
#   HEADER_TIMEOUT (s, default 60), STREAM_LIMIT (bytes, default 10 MiB),
#   STREAM_TIMEOUT (s, default 120).
set -uo pipefail
dropbox="${DROPBOX:?}"
report_py="${REPORT_PY:?}"
HEADER_TIMEOUT="${HEADER_TIMEOUT:-60}"
STREAM_LIMIT="${STREAM_LIMIT:-10485760}"   # 10 MiB — DoS guard on the Broker disk
STREAM_TIMEOUT="${STREAM_TIMEOUT:-120}"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="${dropbox}/${ts}-$$"
mkdir -p "${run_dir}"

# First line = header; the rest of the stream = the tar bundle. Time-box the
# header so a silent open connection cannot pin this handler indefinitely (S-D).
if ! IFS= read -r -t "${HEADER_TIMEOUT}" header; then
  header=""
  echo "[broker] no header within ${HEADER_TIMEOUT}s — treating as no evidence" >&2
fi
# The header is UNTRUSTED: strip control chars (incl. terminal-escape sequences)
# and truncate before it lands in a file or the log line (S-D).
header="$(printf '%s' "${header}" | tr -d '\000-\037\177' | cut -c1-200)"
printf '%s\n' "${header}" > "${run_dir}/header.txt"

# Length-prefixed frame (Analysis T-G): if the header declares len=<bytes>, read
# EXACTLY that many (bounded by STREAM_LIMIT) so the stream cannot desync; a legacy
# sender without len= falls back to the size cap. Either way head -c bounds the
# Broker disk and timeout bounds the time (S-D).
declared="$(printf '%s' "${header}" | sed -n 's/.*len=\([0-9][0-9]*\).*/\1/p')"
read_bytes="${STREAM_LIMIT}"
if [ -n "${declared}" ] && [ "${declared}" -le "${STREAM_LIMIT}" ] 2>/dev/null; then
  read_bytes="${declared}"
fi
# Extract ONLY the two evidence files by EXACT name — the explicit member list is
# the path-traversal guard (any other/absolute/`..` member simply does not match).
timeout "${STREAM_TIMEOUT}" sh -c \
  'head -c "$1" | tar -C "$2" -xf - nightly-evidence.json promptfoo.json 2>/dev/null' \
  _ "${read_bytes}" "${run_dir}" || true

# Reject SYMLINK members. `tar -x <exact names>` blocks traversal, but a member
# that is itself a symlink would be extracted AS a symlink — turning the
# classifier into an arbitrary-file read primitive. Drop any symlink so the file
# reads as ABSENT -> hard-fail, never a stray read of a Broker file (S-D).
for f in nightly-evidence.json promptfoo.json; do
  [ -L "${run_dir}/${f}" ] && rm -f "${run_dir}/${f}"
done

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
