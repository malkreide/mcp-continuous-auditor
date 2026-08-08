"""The Ruff gates, as checks rather than shell loops in `lint.yml`.

Three of them, and the third is the one nobody would guess from the name.

**Root plus every subproject.** `ci.yml.template` ships exactly this, so it
runs here too. The rationale is narrower than it looks, and worth stating
exactly, because the obvious reason is wrong: Ruff already resolves each
file's nearest `pyproject.toml` on a plain `.` run — measured on this repo, a
95-character line under `examples/worker-tdd-demo/` (line-length 100) passes
the root run and fails at the root's own width. A loop justified by differing
widths would catch nothing. It is about a root `exclude` that would make the
directory run skip a subproject **in silence**; an explicitly passed path is
checked regardless.

**Format stability across widths.** `scripts/check_ruff_pin.py` is copied
portfolio-wide, into repos that set their own `line-length`. It must format
identically at every width in use, or the first `ruff format` in the target
repo rewrites it and reads as a defect in the copied file.

THE VERDICT LOGIC IS SPLIT FROM THE SUBPROCESS (`verdict`). What can go wrong
hangs on an exit code and a text — both values, both testable without Ruff on
PATH and without a shell shim.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ._core import CheckFailed, register

MISSING = (
    "Ruff is not on PATH — this gate cannot run. FAIL rather than skip: "
    "reporting 'passed' where 'did not run' is correct is the one answer worse "
    "than none."
)

# Widths in use across the portfolio. A file copied between repos must format
# identically at each, or the first `ruff format` in the target rewrites it.
PORTFOLIO_WIDTHS = (88, 100, 110, 120)
PORTFOLIO_FILES = ("scripts/check_ruff_pin.py",)

SKIP = ("/.venv/", "/node_modules/", "/.git/")


def subprojects(root: Path) -> list[Path]:
    """Every directory below the root that carries its own pyproject.toml."""
    found = []
    for path in sorted(root.rglob("pyproject.toml")):
        rel = path.relative_to(root).as_posix()
        if "/" not in rel:  # the root's own
            continue
        if any(
            s.strip("/") in rel.split("/") for s in (".venv", "node_modules", ".git")
        ):
            continue
        found.append(path.parent)
    return found


def verdict(kind: str, results: list[tuple[str, int, str]]) -> tuple[bool, str]:
    """Pure: `(green, message)` from one `(label, returncode, output)` per run.

    Every failing target is named, not just the first — otherwise each fix
    costs a round.
    """
    bad = [
        f"{label}:\n{output.strip()}" for label, code, output in results if code != 0
    ]
    if not bad:
        targets = ", ".join(label for label, _, _ in results)
        return True, f"{kind} clean on {len(results)} target(s): {targets}"
    hint = (
        "\n  `ruff format .` fixes this; the pre-commit hook does it before "
        "every commit."
        if kind == "format"
        else ""
    )
    return False, "\n".join(bad) + hint


def _ruff(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("ruff")
    if executable is None:
        raise CheckFailed(MISSING)
    return subprocess.run(
        [executable, *args], cwd=root, capture_output=True, text=True, check=False
    )


def _over_tree(root: Path, kind: str, *args: str) -> str:
    targets = [".", *[str(p.relative_to(root)) for p in subprojects(root)]]
    results = []
    for target in targets:
        done = _ruff(root, *args, target)
        results.append((target, done.returncode, done.stdout + done.stderr))
    ok, message = verdict(kind, results)
    if not ok:
        raise CheckFailed(message)
    return message


@register(3, "ruff check passes on the root and every subproject")
def ruff_check(root: Path) -> str:
    return _over_tree(root, "check", "check")


@register(4, "ruff format leaves the root and every subproject unchanged")
def ruff_format(root: Path) -> str:
    return _over_tree(root, "format", "format", "--check")


@register(5, "the portfolio scripts format identically at every width in use")
def portfolio_format_stability(root: Path) -> str:
    results = []
    for name in PORTFOLIO_FILES:
        if not (root / name).is_file():
            raise CheckFailed(
                f"{name} does not exist — anchor gone. This check would "
                "otherwise pass by having nothing to look at."
            )
        for width in PORTFOLIO_WIDTHS:
            done = _ruff(root, "format", "--check", "--line-length", str(width), name)
            results.append(
                (f"{name} @ {width}", done.returncode, done.stdout + done.stderr)
            )
    ok, message = verdict("format", results)
    if not ok:
        raise CheckFailed(
            message + "\n  This file is copied portfolio-wide and must format "
            "identically at every width in use — rationale in its docstring."
        )
    return (
        f"{len(PORTFOLIO_FILES)} file(s) stable at widths "
        f"{', '.join(str(w) for w in PORTFOLIO_WIDTHS)}"
    )
