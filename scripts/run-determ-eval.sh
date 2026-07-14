#!/usr/bin/env bash
#
# run-determ-eval.sh — the default suite runner of the improve loop (Phase 6c).
#
# Contract with scripts/improve_acceptance.py (--runner): invoked as
#   run-determ-eval.sh <config> <output.json>
# with the TARGET checkout as cwd; runs the key-less determ suite once and
# writes promptfoo's machine-readable results to <output.json>. Exit codes
# 0/1/100 mean "the suite ran" (the harness reads the JSON); anything else is
# an infrastructure failure the harness hard-fails on.
#
# The truth-engine is PINNED, exactly like nightly-audit.sh: prefer the
# lockfile-pinned local install via install-promptfoo.sh, fall back to a
# version-pinned npx. Never @latest.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG="${1:?usage: run-determ-eval.sh <config> <output.json>}"
OUT="${2:?usage: run-determ-eval.sh <config> <output.json>}"
PROMPTFOO_VERSION="${PROMPTFOO_VERSION:-0.121.17}"

pf_cmd=(npx -y "promptfoo@${PROMPTFOO_VERSION}")
if pf_bin="$("${HERE}/install-promptfoo.sh" "$(dirname "${CONFIG}")" 2>/dev/null)"; then
  pf_cmd=("${pf_bin}")
fi

exec "${pf_cmd[@]}" eval -c "${CONFIG}" --output "${OUT}" --no-progress-bar
