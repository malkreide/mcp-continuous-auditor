#!/usr/bin/env python3
"""Holds the two Ruff pins to each other.

Ruff is pinned in two places, and both must name the same version:

  * ``.github/workflows/lint.yml`` — ``pip install ruff==X.Y.Z``
  * ``.pre-commit-config.yaml``    — ``rev: vX.Y.Z`` on the ruff-pre-commit repo

The pre-commit hook exists to enforce locally exactly the formatting the lint
job checks. That only holds while both name the same version. Let the pins
drift and the hook formats to one while CI checks against the other: **the
hook reports green and CI goes red** — the very failure the hook was
introduced to prevent, one level up.

Without this guard the only thing holding them together is a comment in both
files asking whoever bumps one to bump the other. Asking is not a check; that
is the rule behind ``OPS-005`` (pipeline honesty), and this repo is where that
check came from (#29: a test suite no workflow ever ran).

TWO DECISIONS
-------------
1. **A missing pin is a finding, not a silent pass.** If either place is gone,
   the comparison did not happen. Then ``NO PIN`` and exit 1, rather than
   printing "they agree" from half the evidence.

2. **The comparison is a pure function.** ``compare()`` takes both file
   contents as strings and is testable without touching the filesystem. Only
   ``main()`` reads from disk.

The ``v`` prefix on the pre-commit ``rev`` belongs to the git tag, not to the
version, and is stripped before comparing.

Stdlib-only, matching the rest of the repo's tooling — hence regex rather than
PyYAML: two fields do not justify a dependency, and the check runs in a job
that installs nothing.

Formatting: this file is meant to be copied between the portfolio repos, where
``line-length`` 88, 100, 110 and 120 sit side by side. ``ruff format`` joins an
expression as soon as it fits the width in force, so a line between 89 and 120
characters would be correctly formatted in one half of the repos and not in the
other, and ``ruff format --check`` would fall over on the copy. Two rules keep
it identical at every width:

  * no line over 88 characters — long expressions get a local variable rather
    than a wrap
  * no implicit string concatenation across lines, except in calls carrying a
    magic trailing comma

Exit codes:
  0  both pins name the same version
  1  they disagree, or one of them is missing
  2  usage error (one of the two files is unreadable)

Usage:
    python scripts/check_ruff_pin.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINT_WORKFLOW = Path(".github") / "workflows" / "lint.yml"
PRECOMMIT_CONFIG = Path(".pre-commit-config.yaml")

# `pip install ruff==0.15.8` — tolerates spaces around `==` and other packages
# on the same line.
PIP_PIN = re.compile(r"\bruff\s*==\s*([0-9][^\s'\"]*)")

# The ruff-pre-commit repo entry, up to the next `- repo:` or end of file.
# `rev:` is searched only inside that slice so another repo's rev is not read
# by mistake.
RUFF_REPO_BLOCK = re.compile(
    r"^\s*-\s*repo:\s*\S*ruff-pre-commit\s*$(.*?)(?=^\s*-\s*repo:|\Z)",
    re.MULTILINE | re.DOTALL,
)
REV = re.compile(r"^\s*rev:\s*['\"]?(\S+?)['\"]?\s*$", re.MULTILINE)


def workflow_pins(text: str) -> list[str]:
    """Every Ruff version pinned in the workflow."""
    return PIP_PIN.findall(text)


def precommit_pin(text: str) -> str | None:
    """The ruff-pre-commit `rev`, without its `v` prefix.

    ``None`` when the repo entry is missing or carries no ``rev`` — both mean
    there is nothing to compare, which ``compare()`` treats as a finding.
    """
    block = RUFF_REPO_BLOCK.search(text)
    if block is None:
        return None
    rev = REV.search(block.group(1))
    if rev is None:
        return None
    return rev.group(1).removeprefix("v")


def compare(workflow_text: str, precommit_text: str) -> tuple[bool, str]:
    """Pure comparison: ``(they_agree, message)``.

    No file or network access, so the test exercises the real behaviour rather
    than a mock of our own assumption about the file format.
    """
    pins = workflow_pins(workflow_text)
    hook = precommit_pin(precommit_text)

    workflow = LINT_WORKFLOW.as_posix()
    config = PRECOMMIT_CONFIG.as_posix()

    if not pins:
        return False, f"NO PIN: {workflow} carries no `ruff==<version>`."
    if hook is None:
        missing = "has no ruff-pre-commit repo, or no `rev:` on it."
        return False, f"NO PIN: {config} {missing}"

    divergent = sorted({p for p in pins if p != hook})
    if divergent:
        others = ", ".join(repr(p) for p in divergent)
        head = f"DRIFT: {config} pins Ruff to {hook!r},"
        return False, f"{head} {workflow} to {others}."

    return True, f"Ruff pin OK ({hook}; both places agree)."


def main(argv: list[str] | None = None) -> int:
    workflow = REPO_ROOT / LINT_WORKFLOW
    precommit = REPO_ROOT / PRECOMMIT_CONFIG

    for path in (workflow, precommit):
        if not path.is_file():
            print(f"Unreadable: {path}", file=sys.stderr)
            return 2

    ok, message = compare(
        workflow.read_text(encoding="utf-8"),
        precommit.read_text(encoding="utf-8"),
    )
    if ok:
        print(message)
        return 0

    print(message, file=sys.stderr)
    print(
        "\nBump both in the same commit: `rev:` in "
        f"{PRECOMMIT_CONFIG.as_posix()} and `pip install ruff==…` in "
        f"{LINT_WORKFLOW.as_posix()}. Otherwise the hook formats to one "
        "version and CI checks against the other.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
