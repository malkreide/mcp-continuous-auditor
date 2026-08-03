#!/usr/bin/env python3
"""Every gate the auditor ships has been answered for the auditor itself.

The auditor ships CI templates (`.github/workflows/*.yml.template`) that impose
gates on the repos it audits. Nothing made anyone answer whether those gates
also hold here — and the answer, for one of them, was no: `uv run ruff check`
went out to every target from the start and had never run against the auditor's
own 57 Python files until #52. Not a decision that was taken and got it wrong;
a decision nobody was ever asked to take.

That is `OPS-005` (pipeline honesty) turned on its author. This guard closes it
the only way `OPS-005` accepts: not by documenting the rule, but by making
something go red when it is unmet.

WHAT IT PROVES, AND WHAT IT DOES NOT
------------------------------------
It proves that **every executing step of every shipped template carries a
classification** in `.github/dogfood.yml`, that every `mirrored` claim names an
own step which actually exists, and that no classification outlives the step it
describes.

It does **not** prove the mirror is semantically equivalent — that `unittest`
discovery covers what `pytest` would, say. That judgement stays with the note
in the table and with the reader. A guard that claimed more than it checks
would be the failure mode it exists to prevent.

WHY `gap` WARNS RATHER THAN FAILS
---------------------------------
A `gap` is an applicable gate the auditor does not run. Failing on it would
make the guard unadoptable on the day it lands (`mypy` is one today), and the
predictable response would be to delete the entry rather than the gap. Warning
keeps it in every CI run's annotations and in this file, where removing it is a
visible edit. The classification is enforced; the remedy is scheduled.

Structural rules — all five are hard failures:

  1. a template step with no entry                     → UNCLASSIFIED
  2. an entry for a step that no longer exists         → STALE
  3. `mirrored` whose `by` names no existing own step  → BROKEN MIRROR
  4. `target-only` or `gap` without a `note`           → UNJUSTIFIED
  5. two steps in one job sharing an identity          → AMBIGUOUS

Rule 3 is the one that ages well: it catches the day someone deletes the own
lint step and leaves the claim that it exists standing. Rule 5 closes the way
around rule 1 — a second step reusing a name would otherwise inherit the first
one's classification.

Exit codes:
  0  every template step classified, every claim intact (gaps may be warned)
  1  a structural rule above is violated
  2  usage error (a file is unreadable or not valid YAML)

Usage:
    python scripts/dogfood_gate.py
    python scripts/dogfood_gate.py --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = Path(".github") / "workflows"
TABLE = Path(".github") / "dogfood.yml"

VALID_STATUS = ("mirrored", "target-only", "setup", "gap")
NEEDS_NOTE = ("target-only", "gap")


def workflow_steps(workflow_text: str, label: str) -> list[str]:
    """`<label>::<job>::<identity>` for **every** step of the workflow.

    Identity is the step's `name`, falling back to its `uses:` spec, falling
    back to `(unnamed)`.

    An earlier version collected only steps carrying a `run:`, on the reasoning
    that `uses:` steps are actions rather than gates. That reasoning held for
    `actions/checkout` and stopped there: `live-probe.yml.template` opens a
    drift issue and uploads reports through actions, and
    `redteam-regen.yml.template` opens a PR through one. Those are shipped
    behaviour, and the guard reported «fully classified» while never having
    seen them. A future action-based gate — a CodeQL analyse step, say — would
    have slipped past `UNCLASSIFIED` in exactly the same way.

    So every step is classified; the plumbing ones simply carry `setup`. That
    the list is complete matters more than that it is short — a guard whose
    completeness claim has a hole is the failure mode it exists to prevent.
    """
    doc = yaml.safe_load(workflow_text) or {}
    keys: list[str] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            identity = step.get("name") or step.get("uses") or "(unnamed)"
            keys.append(f"{label}::{job_name}::{identity}")
    return keys


def compare(
    template_steps: list[str],
    own_steps: set[str],
    table: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Pure comparison: `(errors, warnings)`.

    Takes the three inputs as data, so the test drives the real logic without a
    filesystem — the same shape as tools/check_repo_description.py's compare().
    """
    entries = (table or {}).get("steps") or {}
    errors: list[str] = []
    warnings: list[str] = []

    # Two steps in one job sharing a name collapse to one key, and then a
    # single entry classifies both. A second step named `Lint` running
    # `bandit` would inherit the mirror claim of the first and pass unseen.
    # Identity has to be unique before anything is read from the table.
    seen: set[str] = set()
    for key in template_steps:
        if key in seen:
            errors.append(
                f"AMBIGUOUS: {key}\n"
                f"    Two steps in this job share an identity, so one entry "
                f"would classify both. Give them distinct names."
            )
        seen.add(key)

    for key in dict.fromkeys(template_steps):
        entry = entries.get(key)
        if entry is None:
            errors.append(
                f"UNCLASSIFIED: {key}\n"
                f"    A shipped template imposes this step and "
                f"{TABLE.as_posix()} does not say whether it holds here."
            )
            continue

        status = entry.get("status")
        if status not in VALID_STATUS:
            errors.append(
                f"BAD STATUS: {key}\n"
                f"    status={status!r}; expected one of {', '.join(VALID_STATUS)}."
            )
            continue

        if status in NEEDS_NOTE and not str(entry.get("note", "")).strip():
            errors.append(
                f"UNJUSTIFIED: {key}\n    status={status!r} needs a `note` saying why."
            )
            continue

        if status == "mirrored":
            by = entry.get("by")
            if not by:
                errors.append(f"BROKEN MIRROR: {key}\n    status=mirrored needs `by`.")
            elif by not in own_steps:
                errors.append(
                    f"BROKEN MIRROR: {key}\n"
                    f"    claims to be mirrored by {by!r}, which no own workflow has."
                )

        if status == "gap":
            warnings.append(key)

    known = set(template_steps)
    for key in sorted(set(entries) - known):
        errors.append(
            f"STALE: {key}\n"
            f"    {TABLE.as_posix()} classifies a step no shipped template has."
        )

    return errors, warnings


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def collect(root: Path) -> tuple[list[str], set[str], dict[str, Any]]:
    """Read the templates, the auditor's own workflows and the table."""
    template_steps: list[str] = []
    for path in sorted((root / WORKFLOWS).glob("*.yml.template")):
        template_steps += workflow_steps(path.read_text(encoding="utf-8"), path.name)

    own_steps: set[str] = set()
    for path in sorted((root / WORKFLOWS).glob("*.yml")):
        own_steps |= set(workflow_steps(path.read_text(encoding="utf-8"), path.name))

    return template_steps, own_steps, _load(root / TABLE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dogfood_gate")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        template_steps, own_steps, table = collect(REPO_ROOT)
    except (OSError, yaml.YAMLError) as exc:
        print(f"Unreadable: {exc}", file=sys.stderr)
        return 2

    errors, warnings = compare(template_steps, own_steps, table)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "template_steps": len(template_steps),
                    "errors": errors,
                    "gaps": warnings,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1 if errors else 0

    for key in warnings:
        print(f"::warning title=Dogfooding gap::{key} — applicable here, not run.")

    if errors:
        print(
            "The auditor does not answer for itself what it demands:", file=sys.stderr
        )
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(
            f"\nEvery executing step of a shipped template needs an entry in "
            f"{TABLE.as_posix()}. Add one, or remove the step from the template.",
            file=sys.stderr,
        )
        return 1

    gaps = f", {len(warnings)} declared gap(s)" if warnings else ""
    print(f"Dogfooding OK ({len(template_steps)} template steps classified{gaps}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
