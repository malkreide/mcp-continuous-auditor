#!/usr/bin/env bash
#
# 00-preflight.sh — verify a local Linux VM can host the Phase-5 rollout.
#
# Run this FIRST, on the host VM, before anything else. It only *reads* the host
# (no installs, no writes outside the work dir) and fails loud with a remediation
# line for each missing prerequisite — the same "observe, never assume" contract
# as the rest of the auditor (SOUL.md).
#
# What it checks:
#   - nested KVM is available (/dev/kvm) — required for the Worker microVM;
#   - CPU virt extensions are exposed to this guest (vmx/svm on x86, KVM on ARM);
#   - qemu-system + a vsock channel tool (socat) are present;
#   - docker + compose are present (TensorZero gateway + ClickHouse);
#   - a few basics (git, curl, python3).
#
# Exit 0 = ready. Exit 1 = at least one blocker (printed). Nothing is changed.
set -uo pipefail

arch="$(uname -m)"
fail=0
note() { printf '  %s\n' "$*"; }
ok()   { printf 'OK   %s\n' "$*"; }
bad()  { printf 'MISS %s\n    -> %s\n' "$1" "$2"; fail=1; }

echo "== Phase-5 preflight (arch: ${arch}) =="

# --- nested virtualization ----------------------------------------------------
if [ -e /dev/kvm ] && { [ -r /dev/kvm ] && [ -w /dev/kvm ]; }; then
  ok "/dev/kvm present and accessible"
else
  bad "/dev/kvm not usable" \
    "enable NESTED virtualization on the outer hypervisor and expose KVM to this VM; \
add your user to the 'kvm' group (sudo usermod -aG kvm \"\$USER\"; re-login)"
fi

case "${arch}" in
  x86_64)
    if grep -Eqc '(vmx|svm)' /proc/cpuinfo; then ok "x86 virt extensions exposed (vmx/svm)"
    else bad "no vmx/svm in /proc/cpuinfo" \
      "the outer hypervisor is not passing virt extensions through — turn on nested virt for this VM"; fi ;;
  aarch64|arm64)
    if dmesg 2>/dev/null | grep -qi 'kvm' || [ -e /dev/kvm ]; then ok "ARM64 KVM path"
    else bad "ARM64 KVM not detected" "ensure the kernel has KVM and the host passes it through"; fi ;;
  *) bad "unsupported arch ${arch}" "Phase-5 microVMs are validated on x86_64 / aarch64 only" ;;
esac

# --- tooling ------------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 && ok "$1" || bad "$1 not found" "$2"; }

case "${arch}" in
  x86_64) need qemu-system-x86_64 "sudo apt install -y qemu-system-x86" ;;
  *)      need qemu-system-aarch64 "sudo apt install -y qemu-system-arm" ;;
esac
need qemu-img  "sudo apt install -y qemu-utils"
need socat     "sudo apt install -y socat        # vsock result channel"
need cloud-localds "sudo apt install -y cloud-image-utils   # builds the cloud-init seed"
need envsubst  "sudo apt install -y gettext-base   # renders the cloud-init template"
need nft       "sudo apt install -y nftables       # Worker egress allowlist (S3)"
need git       "sudo apt install -y git"
need curl      "sudo apt install -y curl"
need python3   "sudo apt install -y python3"

# docker + compose (TensorZero)
if command -v docker >/dev/null 2>&1; then
  ok "docker"
  if docker compose version >/dev/null 2>&1; then ok "docker compose (v2)"
  else bad "docker compose v2 not found" "install the docker compose plugin"; fi
  docker info >/dev/null 2>&1 || bad "docker daemon not reachable" \
    "start docker and/or add your user to the 'docker' group (re-login)"
else
  bad "docker not found" "install Docker Engine + the compose plugin (https://docs.docker.com/engine/install/)"
fi

# vhost-vsock (the worker<->broker channel rides this)
if [ -e /dev/vhost-vsock ]; then ok "/dev/vhost-vsock present"
else note "INFO /dev/vhost-vsock missing — run: sudo modprobe vhost_vsock (load at boot via /etc/modules-load.d/)"; fi

# Worker egress allowlist (S3): run-worker.sh refuses to boot without it. Not a
# hard blocker here (it is loaded later by apply-egress-allowlist.sh) — just flag it.
if command -v nft >/dev/null 2>&1 && nft list table inet mcp_worker_egress >/dev/null 2>&1; then
  ok "egress allowlist loaded (inet mcp_worker_egress)"
else
  note "INFO egress allowlist not loaded yet — run: sudo deploy/microvm/apply-egress-allowlist.sh"
fi

echo
if [ "${fail}" -eq 0 ]; then
  echo "PREFLIGHT: ready. Next: deploy/tensorzero/up.sh, then deploy/microvm/build-worker-image.sh"
else
  echo "PREFLIGHT: blocked — resolve the MISS lines above and re-run."
fi
exit "${fail}"
