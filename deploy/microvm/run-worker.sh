#!/usr/bin/env bash
#
# run-worker.sh — boot ONE throwaway Worker microVM, run the audit, discard it.
#
# Per run we create a fresh qcow2 overlay on top of the read-only base image, so a
# compromised audit never survives the run (forkd-isolation.md: "Worker pro Lauf
# wegwerfen"). Networking is restricted (user-mode, the host firewall/nftables is
# where you pin the egress allowlist — GitHub-anon + Zürich only). The only path
# to the Broker is the vsock device; there is no shared filesystem and no SSH.
#
# Run on the host VM AFTER build-worker-image.sh and with the Broker listener up.
#
# Env:
#   IMG_DIR      images dir (default: deploy/.images)
#   WORKER_CID   guest vsock CID (default: 3; host is always 2)
#   MEM_MB       guest RAM (default: 2048)   VCPUS (default: 2)
#   BOOT_TIMEOUT seconds before we force-kill a hung run (default: 1800)
#   EGRESS_ALLOWLIST enforce the host egress allowlist before booting (default: on;
#                set 0/off ONLY on an already-isolated dev host — the Worker then
#                boots with UNRESTRICTED outbound egress, with a loud warning)
#   WORKER_RUN_AS  unprivileged user to run qemu as, so the nftables allowlist in
#                deploy/microvm/egress-allowlist.nft (scoped by UID) actually
#                constrains this run's egress (default: mcpworker — created by
#                deploy/microvm/apply-egress-allowlist.sh)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
IMG_DIR="${IMG_DIR:-${REPO_ROOT}/deploy/.images}"
WORKER_CID="${WORKER_CID:-3}"
MEM_MB="${MEM_MB:-2048}"
VCPUS="${VCPUS:-2}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-1800}"

arch="$(uname -m)"
case "${arch}" in
  x86_64)  base="${IMG_DIR}/debian-12-genericcloud-amd64.qcow2"; qemu="qemu-system-x86_64" ;;
  aarch64|arm64) base="${IMG_DIR}/debian-12-genericcloud-arm64.qcow2"; qemu="qemu-system-aarch64" ;;
  *) echo "FATAL: unsupported arch ${arch}" >&2; exit 1 ;;
esac
seed="${IMG_DIR}/worker-seed.iso"
[ -f "${base}" ] || { echo "FATAL: base image missing — run build-worker-image.sh first"; exit 1; }
[ -f "${seed}" ] || { echo "FATAL: seed ISO missing — run build-worker-image.sh first"; exit 1; }
[ -e /dev/kvm ] || { echo "FATAL: /dev/kvm missing — see deploy/00-preflight.sh"; exit 1; }

# --- egress interlock (Analysis S3) -------------------------------------------
# The Worker runs untrusted read-only code next to the credential-holding Broker.
# Refuse to boot it with open egress: require the host allowlist to be loaded AND
# run qemu as the UID that ruleset filters, so exfil / C2 / host-LAN access is
# actually blocked — not merely documented. The interlock is factored into a
# sourceable helper so it can be unit-tested without /dev/kvm or a base image; it
# sets the RUN_AS array (privilege-drop prefix) and fails closed.
# shellcheck source=deploy/microvm/_egress-interlock.sh
source "${HERE}/_egress-interlock.sh"
resolve_worker_run_as || exit 1

run_id="$(cat /proc/sys/kernel/random/uuid 2>/dev/null | cut -c1-8 || echo run)"
overlay="${IMG_DIR}/worker-run-${run_id}.qcow2"
echo "==> creating throwaway overlay ${overlay}"
qemu-img create -f qcow2 -F qcow2 -b "${base}" "${overlay}" >/dev/null \
  || { echo "FATAL: overlay create failed"; exit 1; }
# Always discard the overlay — the VM is throwaway whether it passed or hung.
# (trap runs as the invoking user, so the rm works regardless of the run-as uid.)
trap 'rm -f "${overlay}"; echo "==> discarded ${overlay}"' EXIT

# When dropping privileges, the run-as user must read the base+seed and own the
# overlay. Images hold no secrets by design, so widening read is safe.
if [ "${#RUN_AS[@]}" -gt 0 ]; then
  chown "${WORKER_RUN_AS}" "${overlay}" 2>/dev/null || true
  chmod a+rx "${IMG_DIR}" 2>/dev/null || true
  chmod a+r  "${base}" "${seed}" 2>/dev/null || true
fi

# Common args. user-mode (SLIRP) networking: NAT to the outside, no inbound, and
# no bridge to the host LAN (SLIRP isolates the guest from 192.168.x.x by design).
# restrict=off is REQUIRED — the guest must reach GitHub/uv/npm/Zürich to audit.
# SLIRP alone does NOT limit WHICH internet hosts the guest reaches: that is
# enforced on the host by the egress interlock above (nft, scoped to the run-as UID).
common=(
  -accel kvm -cpu host -smp "${VCPUS}" -m "${MEM_MB}"
  -drive "file=${overlay},if=virtio,format=qcow2"
  -drive "file=${seed},if=virtio,format=raw,readonly=on"
  -netdev user,id=n0,restrict=off -device virtio-net-pci,netdev=n0
  -device vhost-vsock-pci,guest-cid="${WORKER_CID}"
  -nographic -no-reboot
)
# aarch64 needs an explicit machine + UEFI; x86_64 boots the cloud image directly.
if [ "${arch}" = "x86_64" ]; then
  machine=(-machine q35)
else
  machine=(-machine virt -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd)
fi

echo "==> booting Worker microVM (cid=${WORKER_CID}, ${VCPUS} vCPU, ${MEM_MB} MB) — it powers off when the audit completes"
# The guest runs the audit and `poweroff`s itself (cloud-init). We bound the wall
# clock so a hung run can't pin the VM forever; the breaker on the Broker side
# then counts the missing result as a failure on the next cycle.
timeout "${BOOT_TIMEOUT}" "${RUN_AS[@]}" "${qemu}" "${machine[@]}" "${common[@]}"
rc=$?
if [ "${rc}" -eq 124 ]; then
  echo "==> WORKER TIMED OUT after ${BOOT_TIMEOUT}s — killed. No result shipped."
  exit 1
fi
echo "==> worker VM exited (qemu rc=${rc}). Result (if any) arrived on the Broker listener."
