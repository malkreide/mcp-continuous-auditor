#!/usr/bin/env bash
#
# install-promptfoo.sh <promptfoo-dir> — install the PINNED promptfoo truth-engine
# from the committed lockfile (Analysis T-G) and print the resolved binary path on
# stdout. Reproducible by construction: `npm ci` installs the EXACT transitive tree
# the committed package-lock.json pins (integrity-checked), instead of
# `npx -y promptfoo@x` re-resolving dependency ranges against a moving registry on
# every run.
#
# Usage:
#   bin="$(scripts/install-promptfoo.sh promptfoo)" && "$bin" eval -c <config>
#
# Prints nothing and exits non-zero when the dir has no committed lockfile (or npm
# is missing), so the caller can fall back to a version-pinned npx.
set -uo pipefail

dir="${1:?usage: install-promptfoo.sh <dir-with-package-lock.json>}"
[ -f "${dir}/package-lock.json" ] || { echo "no committed lockfile in ${dir}" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm not found" >&2; exit 1; }

bin="${dir}/node_modules/.bin/promptfoo"
if [ ! -x "${bin}" ]; then
  # All npm chatter goes to stderr so stdout stays exactly the binary path.
  ( cd "${dir}" && npm ci --no-audit --no-fund ) >&2 \
    || { echo "npm ci failed in ${dir}" >&2; exit 1; }
fi
[ -x "${bin}" ] || { echo "promptfoo binary missing after npm ci in ${dir}" >&2; exit 1; }
printf '%s\n' "${bin}"
