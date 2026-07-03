#!/usr/bin/env bash
#
# apply-egress-allowlist.sh — set up + load the Worker egress allowlist (S3).
#
# Idempotent. Run once on the host (as root) before launching Workers. It:
#   1. creates the dedicated unprivileged user the Worker qemu runs as
#      (WORKER_USER, default `mcpworker`) — no login, no home;
#   2. puts that user in the `kvm` group and grants the `kvm` group access to
#      /dev/vhost-vsock via a udev rule, so non-root qemu can use KVM + the vsock
#      result channel;
#   3. loads egress-allowlist.nft (an additive `inet mcp_worker_egress` table).
#
# After this, run-worker.sh's egress interlock is satisfied and the Worker's
# outbound traffic is constrained to DNS + web to the public internet only.
#
# Env: WORKER_USER (default mcpworker).  Undo: deploy/microvm/apply-egress-allowlist.sh --remove
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES="${HERE}/egress-allowlist.nft"
WORKER_USER="${WORKER_USER:-mcpworker}"
UDEV_RULE="/etc/udev/rules.d/99-mcp-worker-vsock.rules"

if [ "$(id -u)" -ne 0 ]; then
  echo "FATAL: must run as root (creates a user, loads nftables, writes a udev rule)." >&2
  echo "  try: sudo WORKER_USER=${WORKER_USER} ${BASH_SOURCE[0]} ${*}" >&2
  exit 1
fi
command -v nft >/dev/null || { echo "FATAL: nftables (nft) not installed — apt install nftables"; exit 1; }

# --- teardown -----------------------------------------------------------------
if [ "${1:-}" = "--remove" ]; then
  nft list table inet mcp_worker_egress >/dev/null 2>&1 && nft delete table inet mcp_worker_egress
  rm -f "${UDEV_RULE}" && udevadm control --reload-rules 2>/dev/null || true
  echo "==> removed the egress table + udev rule (user ${WORKER_USER} left intact)."
  exit 0
fi

[ -f "${RULES}" ] || { echo "FATAL: ${RULES} not found"; exit 1; }

# --- 1) dedicated Worker user -------------------------------------------------
if id "${WORKER_USER}" >/dev/null 2>&1; then
  echo "==> user ${WORKER_USER} exists"
else
  echo "==> creating system user ${WORKER_USER} (no login, no home)"
  useradd --system --no-create-home --shell /usr/sbin/nologin "${WORKER_USER}" \
    || { echo "FATAL: useradd ${WORKER_USER} failed"; exit 1; }
fi

# --- 2) KVM + vsock access for that user --------------------------------------
if getent group kvm >/dev/null 2>&1; then
  usermod -aG kvm "${WORKER_USER}" && echo "==> ${WORKER_USER} added to group kvm"
else
  echo "    note: no 'kvm' group on this host — ensure ${WORKER_USER} can read /dev/kvm"
fi
# /dev/vhost-vsock is root-only by default; let the kvm group use it so non-root
# qemu can open the result channel.
if [ ! -f "${UDEV_RULE}" ]; then
  echo "==> writing ${UDEV_RULE} (kvm group -> /dev/vhost-vsock)"
  printf 'KERNEL=="vhost-vsock", GROUP="kvm", MODE="0660"\n' > "${UDEV_RULE}"
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --name-match=vhost-vsock 2>/dev/null || true
fi

# --- 3) load the ruleset ------------------------------------------------------
echo "==> loading egress allowlist (${RULES})"
nft -f "${RULES}" || { echo "FATAL: nft -f failed — check ${RULES}"; exit 1; }

echo
echo "== Worker egress allowlist active =="
nft list table inet mcp_worker_egress
echo
echo "Worker qemu must run as '${WORKER_USER}' for this to bite — run-worker.sh does"
echo "that by default (WORKER_RUN_AS=${WORKER_USER}). Undo: ${BASH_SOURCE[0]} --remove"
