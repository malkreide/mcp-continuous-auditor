#!/usr/bin/env bash
#
# _receive-one.sh — per-connection Broker handler, exec'd by broker-listener.sh
# for every vsock delivery (socat ... EXEC:_receive-one.sh). It is also invoked
# directly by tests/test_broker_pipeline.py, which is why it ships as a committed
# file instead of a heredoc generated at listener startup: the test exercises the
# REAL handler, so a change here cannot silently drift from what is tested.
#
# It reads a one-line header + a tar of RAW EVIDENCE from stdin, extracts ONLY the
# two known evidence files by their EXACT names (the path-traversal guard: any
# other/absolute/`..` member is never requested, so never extracted), and
# re-derives the verdict with the Broker's OWN classifier. Everything on stdin is
# UNTRUSTED — see broker-listener.sh for the full threat model (Analysis S2).
#
# Env: DROPBOX (per-run dirs are created here), REPORT_PY (nightly_audit_report.py).
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
