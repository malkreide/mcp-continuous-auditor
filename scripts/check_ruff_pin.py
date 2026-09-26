#!/usr/bin/env python3
"""Holds the Ruff pin to ONE source, and everything else to that source.

The version lives in exactly one file, and every other place reads from it:

  * ``requirements-lint.txt``    — ``ruff==X.Y.Z``, the single source
  * ``.pre-commit-config.yaml``  — ``rev: vX.Y.Z`` on the ruff-pre-commit repo,
    which must name the same version (pre-commit builds its own environment
    and cannot read a requirements file)
  * ``.github/workflows/lint.yml`` and ``tests.yml`` — install with
    ``pip install -r requirements-lint.txt`` and carry NO ``ruff==`` of their
    own

The workflows used to carry the number themselves: three copies held together
by this script. A number that exists three times is two chances to drift, and
a template that shipped one (``github-repo-skill``'s ``ci.yml`` at 0.16.1 while
this repo was on 0.16.3) became a drift source of its own. Reading from one
file removes the copies instead of policing them.

The pre-commit hook exists to enforce locally exactly the formatting the lint
job checks. That only holds while it names the same version. Let them drift
and the hook formats to one while CI checks against the other: **the hook
reports green and CI goes red** — the very failure the hook was introduced to
prevent, one level up.

THREE DECISIONS
---------------
1. **A missing pin is a finding, not a silent pass.** If the source or the hook
   rev is gone, the comparison did not happen. Then ``NO PIN`` and exit 1,
   rather than printing "they agree" from half the evidence.

2. **A workflow that pins by itself is a finding** (``SECOND SOURCE``), even
   when its number happens to match today — so is one that does not install
   from the file at all (``NOT WIRED``). Either way the single source stopped
   being single, and nothing else would say so.

3. **The comparison is a pure function.** ``compare()`` takes the file contents
   as strings and is testable without touching the filesystem. Every finding is
   named, not just the first — otherwise each fix costs a round.

The ``v`` prefix on the pre-commit ``rev`` belongs to the git tag, not to the
version, and is stripped before comparing.

Stdlib-only, matching the rest of the repo's tooling — hence regex rather than
PyYAML: a few fields do not justify a dependency, and the check runs in a job
that installs nothing it does not need.

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
  0  source, hook and workflows agree
  1  a finding: drift, a missing pin, a second source, or an unwired workflow
  2  usage error (one of the files is unreadable)

Usage:
    python scripts/check_ruff_pin.py
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = Path("requirements-lint.txt")
LINT_WORKFLOW = Path(".github") / "workflows" / "lint.yml"
# `tests.yml` installs Ruff too, because tests/test_format_gate.py runs the two
# gates against fixtures and this repo does not tolerate a test that skips for a
# missing dependency. It reads the same file as lint.yml.
TESTS_WORKFLOW = Path(".github") / "workflows" / "tests.yml"
PINNED_WORKFLOWS = (LINT_WORKFLOW, TESTS_WORKFLOW)
PRECOMMIT_CONFIG = Path(".pre-commit-config.yaml")

# `ruff==0.16.3` — tolerates spaces around `==` and other packages on the line.
PIP_PIN = re.compile(r"\bruff\s*==\s*([0-9][^\s'\"]*)")
# `pip install -r requirements-lint.txt`, also `--requirement`.
INSTALLS_FROM = re.compile(r"(?:-r|--requirement)[\s=]+\S*requirements-lint\.txt")

# The ruff-pre-commit repo entry, up to the next `- repo:` or end of file.
# `rev:` is searched only inside that slice so another repo's rev is not read
# by mistake.
RUFF_REPO_BLOCK = re.compile(
    r"^\s*-\s*repo:\s*\S*ruff-pre-commit\s*$(.*?)(?=^\s*-\s*repo:|\Z)",
    re.MULTILINE | re.DOTALL,
)
REV = re.compile(r"^\s*rev:\s*['\"]?(\S+?)['\"]?\s*$", re.MULTILINE)


def workflow_pins(text: str) -> list[str]:
    """Every Ruff version pinned literally in a text."""
    return PIP_PIN.findall(text)


def source_pins(text: str) -> list[str]:
    """Every distinct Ruff version in requirements-lint.txt, comments ignored."""
    lines = [ln.split("#", 1)[0] for ln in text.splitlines()]
    return sorted(set(workflow_pins("\n".join(lines))))


def requirements_pin(text: str) -> str | None:
    """The one Ruff version in requirements-lint.txt, or ``None``.

    More than one distinct version is not a pin but a contradiction, and also
    comes back as ``None``; ``compare()`` tells the two cases apart.
    """
    pins = source_pins(text)
    return pins[0] if len(pins) == 1 else None


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


def compare(
    requirements_text: str,
    precommit_text: str,
    workflows: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Pure comparison: ``(everything_agrees, message)``.

    ``workflows`` maps a name to a workflow's text. No file or network access,
    so the test exercises the real behaviour rather than a mock of our own
    assumption about the file format.
    """
    source = REQUIREMENTS.as_posix()
    config = PRECOMMIT_CONFIG.as_posix()
    pin = requirements_pin(requirements_text)
    hook = precommit_pin(precommit_text)
    findings = []

    if pin is None:
        found = source_pins(requirements_text)
        if len(found) > 1:
            listed = ", ".join(repr(p) for p in found)
            findings.append(f"DRIFT: {source} pins Ruff more than once: {listed}.")
        else:
            findings.append(f"NO PIN: {source} carries no `ruff==<version>`.")
    if hook is None:
        missing = "has no ruff-pre-commit repo, or no `rev:` on it."
        findings.append(f"NO PIN: {config} {missing}")
    if pin is not None and hook is not None and hook != pin:
        head = f"DRIFT: {source} pins Ruff to {pin!r},"
        findings.append(f"{head} {config} to {hook!r}.")

    for name, text in (workflows or {}).items():
        literals = sorted(set(workflow_pins(text)))
        if literals:
            listed = ", ".join(repr(p) for p in literals)
            tail = f"install from {source} instead."
            findings.append(f"SECOND SOURCE: {name} pins Ruff {listed} — {tail}")
        if not INSTALLS_FROM.search(text):
            tail = f"does not install from {source}."
            findings.append(f"NOT WIRED: {name} {tail}")

    if findings:
        return False, "\n".join(findings)
    return True, f"Ruff pin OK ({pin}; {source}, hook and workflows agree)."


def main(argv: list[str] | None = None) -> int:
    requirements = REPO_ROOT / REQUIREMENTS
    precommit = REPO_ROOT / PRECOMMIT_CONFIG
    workflows = {p.as_posix(): REPO_ROOT / p for p in PINNED_WORKFLOWS}

    for path in (requirements, precommit, *workflows.values()):
        if not path.is_file():
            print(f"Unreadable: {path}", file=sys.stderr)
            return 2

    texts = {name: p.read_text(encoding="utf-8") for name, p in workflows.items()}
    ok, message = compare(
        requirements.read_text(encoding="utf-8"),
        precommit.read_text(encoding="utf-8"),
        texts,
    )
    if ok:
        print(message)
        return 0

    print(message, file=sys.stderr)
    print(
        f"\nThe version lives in {REQUIREMENTS.as_posix()} only. Bump it there "
        f"and `rev:` in {PRECOMMIT_CONFIG.as_posix()} in the same commit; the "
        "workflows install with `pip install -r` and name no version.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
