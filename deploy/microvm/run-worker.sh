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

run_id="$(cat /proc/sys/kernel/random/uuid 2>/dev/null | cut -c1-8 || echo run)"
overlay="${IMG_DIR}/worker-run-${run_id}.qcow2"
echo "==> creating throwaway overlay ${overlay}"
qemu-img create -f qcow2 -F qcow2 -b "${base}" "${overlay}" >/dev/null \
  || { echo "FATAL: overlay create failed"; exit 1; }
# Always discard the overlay — the VM is throwaway whether it passed or hung.
trap 'rm -f "${overlay}"; echo "==> discarded ${overlay}"' EXIT

# Common args. user-mode net with restrict=on: outbound DNS/HTTP only, no inbound,
# no host LAN access — the real egress allowlist belongs on the host firewall.
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
timeout "${BOOT_TIMEOUT}" "${qemu}" "${machine[@]}" "${common[@]}"
rc=$?
if [ "${rc}" -eq 124 ]; then
  echo "==> WORKER TIMED OUT after ${BOOT_TIMEOUT}s — killed. No result shipped."
  exit 1
fi
echo "==> worker VM exited (qemu rc=${rc}). Result (if any) arrived on the Broker listener."
