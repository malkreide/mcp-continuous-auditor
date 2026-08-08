"""The Ruff pin, and the Ruff that actually runs.

THE PURE LOGIC IS NOT HERE. It stays in `scripts/check_ruff_pin.py` and
`scripts/check_ruff_version.py`, and this module only hangs it into the
registry. The reason is a hard constraint, not taste:

* The pre-commit hook calls `python3 scripts/check_ruff_pin.py` DIRECTLY
  (`language: system`). That file must keep its entry point, or the hook
  breaks — and the hook is what makes good on "what passes locally passes in
  CI".
* `check_ruff_pin.py` is copied portfolio-wide; check 5 guards its formatting
  at four widths for exactly that reason.

Pulling the functions here and leaving shims there would mean two files and
the question "which one counts?" — the very second place these checks exist
to prevent. One implementation, two entry points.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ._core import CheckFailed, register

REPO_ROOT = Path(__file__).resolve().parents[2]


# Relativ zum Elternpaket `scripts`, nicht nackt: Beim Lauf als
# `python -m scripts.checks` liegt das Wurzelverzeichnis auf sys.path, nicht
# `scripts/`. Ein nackter Import fand die Module nicht — gemessen beim ersten
# Lauf, drei Pruefungen stuerzten ab.
def _pin_module():
    from .. import check_ruff_pin

    return check_ruff_pin


def _version_module():
    from .. import check_ruff_version

    return check_ruff_version


@register(1, "the ruff pin agrees across the workflows and the pre-commit hook")
def ruff_pin_sync(root: Path) -> str:
    """Compares TEXTS — the workflows and `.pre-commit-config.yaml`.

    Let them drift and the hook formats to one version while CI checks against
    the other: the hook reports green and CI turns red. A missing pin is a
    finding too; then no comparison happened.

    What this does NOT do is the reason check 2 sits next to it: whether the
    Ruff that then runs the gates carries that version, it never says.
    """
    crp = _pin_module()
    texts = []
    for rel in (*crp.PINNED_WORKFLOWS, crp.PRECOMMIT_CONFIG):
        path = root / rel
        if not path.is_file():
            raise CheckFailed(f"not readable: {rel.as_posix()}")
        texts.append(path.read_text(encoding="utf-8"))
    *workflows, precommit = texts

    ok, message = crp.compare("\n".join(workflows), precommit)
    if not ok:
        raise CheckFailed(
            f"{message}\n  Bump them in the same commit: `rev:` in "
            f"{crp.PRECOMMIT_CONFIG.as_posix()} and `pip install ruff==…` in "
            f"{' and '.join(p.as_posix() for p in crp.PINNED_WORKFLOWS)}."
        )
    return message


@register(2, "the ruff on PATH is the pinned one")
def ruff_version_matches_pin(root: Path) -> str:
    """Holds the text against the running program.

    Check 1 proves the workflows and the hook name the same number — not that
    the Ruff about to run the gates carries it. A different one earlier on
    PATH and the gates run on a version nobody pinned.

    Not hypothetical: up to 0.15.8 `ruff format --check .` left Markdown
    alone, since 0.16.1 it does not. Measured in `mcp-data-source-probe-skill`
    (check 18 there) and repeatedly in development environments where a 0.15.8
    masked the installed 0.16.1.
    """
    crp, crv = _pin_module(), _version_module()
    workflow = root / crp.LINT_WORKFLOW
    if not workflow.is_file():
        raise CheckFailed(f"not readable: {crp.LINT_WORKFLOW.as_posix()}")
    pins = crp.workflow_pins(workflow.read_text(encoding="utf-8"))
    pinned = pins[0] if pins else None

    # FAIL rather than skip: a skipped check reports "passed" where "did not
    # run" would be correct.
    executable = shutil.which("ruff")
    if executable is None:
        raise CheckFailed(
            "Ruff is not on PATH — the running version cannot be determined."
        )
    done = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=False
    )
    ok, message = crv.compare(pinned, done.stdout + done.stderr, done.returncode)
    if not ok:
        raise CheckFailed(f"{message}\n  Ruff that ran: {executable}")
    return message
