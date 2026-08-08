"""Hygiene of the tree: everything that should parse, parses."""

from __future__ import annotations

import compileall
import contextlib
import io
from pathlib import Path

from ._core import CheckFailed, pycache_to_temp, register

# The directories `tests.yml` compiled before this became a check.
COMPILED = ("scripts", "tests", "schemas", "promptfoo/providers")


@register(7, "every shipped Python file parses")
def every_script_parses(root: Path) -> str:
    """`compileall` over the directories that carry Python.

    Weaker than it sounds — parsing is not importing, and an import error
    hides from it. It is still the cheapest net for the class that hurts: a
    file that cannot even be read by the interpreter, shipped.

    A missing directory is a FINDING, not a skip. Compiling nothing and
    reporting success is how a moved directory becomes invisible.
    """
    missing = [d for d in COMPILED if not (root / d).is_dir()]
    if missing:
        raise CheckFailed(
            f"these directories do not exist: {missing} — anchor gone. Were "
            "they moved or renamed? Compiling nothing must not read as "
            "'everything parses'."
        )

    buffer = io.StringIO()
    with pycache_to_temp(), contextlib.redirect_stdout(buffer):
        ok = all(
            compileall.compile_dir(str(root / d), quiet=1, force=True) for d in COMPILED
        )
    if not ok:
        raise CheckFailed(buffer.getvalue().strip() or "compileall reported a failure")
    return f"{len(COMPILED)} directories parse: {', '.join(COMPILED)}"
