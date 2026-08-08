"""The Ruff on PATH carries the version lint.yml pins.

`scripts/check_ruff_pin.py` compares TWO TEXTS: the workflows and
`.pre-commit-config.yaml`. It proves they name the same number — not that the
Ruff which then runs `ruff check` and `ruff format --check` carries it. If a
different Ruff sits earlier on PATH than the one just installed, the pin sync
still reports "both places agree", and the gates run beside it on a version
nobody pinned.

This is not hypothetical for this portfolio. Up to Ruff 0.15.8
`ruff format --check .` left Markdown alone; since 0.16.1 formatting of Python
blocks inside Markdown is stable and on by default — exactly the difference
the pin exists to prevent, and without this check the pin would only have
asserted it. Measured in `mcp-data-source-probe-skill` (check 18 there), and
again while writing this file: a Ruff 0.15.8 under `/root/.local/bin` masked a
freshly installed 0.16.1.

WORTH SAYING PLAINLY: `ci.yml.template` in this repo already runs
`uv run ruff --version` — but that only PRINTS the version, it compares
nothing. This repository shipped the idea to others without running it on
itself.

THREE ANCHORS, each failing with its own message rather than silently: the pin
in the workflow, the presence of Ruff on PATH, and the OUTPUT SHAPE
`ruff <version>`. If upstream changes that shape, this check must not quietly
stop comparing — it says it could not read the answer.

Usage:

    python scripts/check_ruff_version.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Same import shape as `tests/test_ruff_pin.py` uses: this directory is on
# sys.path both when the script is run directly and when a test puts it there.
from check_ruff_pin import LINT_WORKFLOW, workflow_pins  # noqa: E402

# The output shape "ruff 0.16.1" is itself an anchor.
VERSION_LINE = re.compile(r"^ruff\s+([0-9]\S*)", re.MULTILINE)


def parse_version(raw: str) -> str | None:
    """The version out of `ruff --version` output, or `None`."""
    match = VERSION_LINE.search(raw)
    return match.group(1) if match else None


def compare(pinned: str | None, raw: str, returncode: int) -> tuple[bool, str]:
    """Pure comparison: `(agrees, message)`.

    Everything that can go wrong is decidable here — no PATH, no subprocess.
    That is precisely why this check is testable and the inline step it
    complements was not.
    """
    if pinned is None:
        return False, (
            f"{LINT_WORKFLOW.as_posix()} names no `ruff==<version>` — anchor "
            "gone. Without it this check has nothing to hold the running Ruff "
            "against, and would have reported success for exactly that reason."
        )
    if returncode != 0:
        return False, f"`ruff --version` exited with {returncode}: {raw.strip()}"

    running = parse_version(raw)
    if running is None:
        return False, (
            "`ruff --version` does not answer in the form 'ruff <version>' — "
            f"read: {raw.strip()!r}. If upstream changed the output, update "
            "VERSION_LINE here; without that this check would compare nothing "
            "and not say so."
        )
    if running != pinned:
        return False, (
            f"The Ruff on PATH is {running}, the pin says {pinned}. The gates "
            "then run on a version other than the pinned one. Both directions "
            "cost: an older one lets through what turns red later; a newer one "
            "flags what the pin permits. The pin sync does not notice — it "
            "compares two texts with each other, not the text with the running "
            "program."
        )
    return True, f"Ruff version OK ({running} on PATH, as pinned)."


def main(argv: list[str] | None = None) -> int:
    workflow = REPO_ROOT / LINT_WORKFLOW
    if not workflow.is_file():
        print(f"File not readable: {workflow}", file=sys.stderr)
        return 2

    pins = workflow_pins(workflow.read_text(encoding="utf-8"))
    pinned = pins[0] if pins else None

    executable = shutil.which("ruff")
    if executable is None:
        # FAIL rather than skip: a skipped step reports "passed" where "did not
        # run" would be correct.
        print(
            "Ruff is not on PATH — the running version cannot be determined.",
            file=sys.stderr,
        )
        return 1

    done = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    ok, message = compare(pinned, done.stdout + done.stderr, done.returncode)
    if ok:
        print(message)
        return 0

    print(message, file=sys.stderr)
    print(f"\nRuff that ran: {executable}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
