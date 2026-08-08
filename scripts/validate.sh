#!/usr/bin/env bash
# Every offline gate of this repository in one command.
#
# The checks themselves live under `scripts/checks/` — one function per gate,
# each runnable against a passed-in tree and therefore testable. This file is
# only the entry point.
set -euo pipefail

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

cd "$(dirname "$0")/.."
exec "$PY" -m scripts.checks "$@"
