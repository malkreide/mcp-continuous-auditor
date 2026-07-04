#!/usr/bin/env bash
#
# _egress-interlock.sh — the Worker egress interlock (Analysis S3), factored out
# of run-worker.sh so it is unit-testable (tests/test_egress_interlock.py) without
# needing /dev/kvm or a base image. Sourcing this file defines one function:
#
#   resolve_worker_run_as
#     Reads EGRESS_ALLOWLIST (default "on") + WORKER_RUN_AS (default "mcpworker").
#     On success it sets the global array RUN_AS (the privilege-drop prefix for
#     qemu, empty when egress is unrestricted) and returns 0. Fail-closed: if the
#     allowlist is required but not active (nft table missing, run-as user
#     missing, or no way to drop privileges) it prints a FATAL reason and returns
#     non-zero — the caller must refuse to boot.
#
# This is behaviour-identical to the inline block it replaced in run-worker.sh;
# only `exit 1` became `return 1` so the caller (`resolve_worker_run_as || exit 1`)
# decides, and the function is safe to source into a test.
resolve_worker_run_as() {
  EGRESS_ALLOWLIST="${EGRESS_ALLOWLIST:-on}"
  WORKER_RUN_AS="${WORKER_RUN_AS:-mcpworker}"
  RUN_AS=()
  if [ "${EGRESS_ALLOWLIST}" != "0" ] && [ "${EGRESS_ALLOWLIST}" != "off" ]; then
    if ! command -v nft >/dev/null 2>&1 || ! nft list table inet mcp_worker_egress >/dev/null 2>&1; then
      cat >&2 <<EOF
FATAL: Worker egress allowlist not active — refusing to boot with open egress.
  Load it first (as root):  sudo deploy/microvm/apply-egress-allowlist.sh
  Dev only, UNRESTRICTED egress on an already-isolated host:  EGRESS_ALLOWLIST=off run-worker.sh
EOF
      return 1
    fi
    if ! id "${WORKER_RUN_AS}" >/dev/null 2>&1; then
      echo "FATAL: WORKER_RUN_AS='${WORKER_RUN_AS}' does not exist — run apply-egress-allowlist.sh" >&2
      return 1
    fi
    if   command -v runuser >/dev/null 2>&1; then RUN_AS=(runuser -u "${WORKER_RUN_AS}" --)
    elif command -v sudo    >/dev/null 2>&1; then RUN_AS=(sudo -n -u "${WORKER_RUN_AS}" --)
    else echo "FATAL: need runuser or sudo to drop privileges to ${WORKER_RUN_AS}" >&2; return 1; fi
    echo "==> egress allowlist active — qemu runs as ${WORKER_RUN_AS} (constrained outbound)"
  else
    echo "!! WARNING: EGRESS_ALLOWLIST=off — Worker boots with UNRESTRICTED outbound egress." >&2
    echo "!! Only acceptable on a host that is itself network-isolated. Never in the rollout." >&2
  fi
  return 0
}
