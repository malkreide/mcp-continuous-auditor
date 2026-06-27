#!/usr/bin/env bash
#
# build-worker-image.sh — prepare the Worker microVM base image + cloud-init seed.
#
# Run once on the host VM (re-runnable). It downloads a stock Debian cloud image
# (the read-only base; per-run overlays are created by run-worker.sh) and renders
# the cloud-init seed ISO from worker-cloud-init.yaml.tmpl. No secrets are baked
# in — the Worker holds none by design.
#
# Env (all have defaults except where noted):
#   TARGET_REPO    owner/name of the MCP server to audit               (required)
#   AUDITOR_REPO   auditor git URL   (default: this repo's origin)
#   AUDITOR_REF    auditor ref       (default: main)
#   BROKER_PORT    vsock port the Broker listener uses (default: 9000)
#   IMG_DIR        where images live  (default: deploy/.images, gitignored)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"

: "${TARGET_REPO:?set TARGET_REPO=owner/name of the MCP server to audit}"
AUDITOR_REPO="${AUDITOR_REPO:-$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || echo https://github.com/malkreide/mcp-continuous-auditor.git)}"
AUDITOR_REF="${AUDITOR_REF:-main}"
BROKER_PORT="${BROKER_PORT:-9000}"
BROKER_CID="2"   # the host is always vsock CID 2 (from the guest's view)
IMG_DIR="${IMG_DIR:-${REPO_ROOT}/deploy/.images}"
mkdir -p "${IMG_DIR}"

arch="$(uname -m)"
case "${arch}" in
  x86_64)  img="debian-12-genericcloud-amd64.qcow2"; url="https://cloud.debian.org/images/cloud/bookworm/latest/${img}" ;;
  aarch64|arm64) img="debian-12-genericcloud-arm64.qcow2"; url="https://cloud.debian.org/images/cloud/bookworm/latest/${img}" ;;
  *) echo "FATAL: unsupported arch ${arch}" >&2; exit 1 ;;
esac
base="${IMG_DIR}/${img}"

if [ ! -f "${base}" ]; then
  echo "==> downloading base cloud image: ${url}"
  curl -fSL --retry 3 -o "${base}.part" "${url}" || { echo "FATAL: download failed"; exit 1; }
  mv "${base}.part" "${base}"
else
  echo "==> base image present: ${base}"
fi
echo "    sha256: $(sha256sum "${base}" | cut -d' ' -f1)   (verify against the published checksum)"

echo "==> rendering cloud-init seed from template"
export AUDITOR_REPO AUDITOR_REF TARGET_REPO BROKER_CID BROKER_PORT
seed_yaml="${IMG_DIR}/worker-cloud-init.yaml"
# envsubst only the placeholders we own (keep $PATH etc. literal in the template).
envsubst '${AUDITOR_REPO} ${AUDITOR_REF} ${TARGET_REPO} ${BROKER_CID} ${BROKER_PORT}' \
  < "${HERE}/worker-cloud-init.yaml.tmpl" > "${seed_yaml}"

seed_iso="${IMG_DIR}/worker-seed.iso"
echo "==> building seed ISO (${seed_iso})"
cloud-localds "${seed_iso}" "${seed_yaml}" || { echo "FATAL: cloud-localds failed (apt install cloud-image-utils)"; exit 1; }

cat <<EOF

== Worker base image ready ==
  base image : ${base}        (read-only base; never booted directly)
  seed ISO   : ${seed_iso}
  target     : ${TARGET_REPO}
  channel    : vsock host(CID 2):${BROKER_PORT}

Next:
  1) Start the Broker listener (on the host, before launching the worker):
       deploy/microvm/channel/broker-listener.sh ${BROKER_PORT} ./.audit/incoming
  2) Launch a throwaway worker run:
       deploy/microvm/run-worker.sh
EOF
