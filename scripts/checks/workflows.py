"""The workflow templates this repository ships to others."""

from __future__ import annotations

from pathlib import Path

from ._core import CheckFailed, register


@register(6, "the shipped workflow templates are well-formed YAML")
def workflow_templates(root: Path) -> str:
    """The templates are never executed here.

    They are what this repository hands to other repos. Nothing else would
    catch a broken one; it would fail first in whichever target repo copied
    it, far from the commit that broke it.

    An empty glob is a FINDING, not a pass — a rename must not read as an
    all-clear.
    """
    try:
        from .. import check_workflow_templates as cwt
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the env
        # Naming the dependency beats a traceback: whoever runs `validate.sh`
        # in a fresh clone needs to know what to install, not where it broke.
        raise CheckFailed(
            f"this check needs {exc.name!r}, which is not installed — "
            "`pip install pyyaml`. FAIL rather than skip: a check that cannot "
            "run must not report 'passed'."
        ) from exc

    ok, message = cwt.compare(cwt.read_templates(root))
    if not ok:
        raise CheckFailed(message)
    return message
