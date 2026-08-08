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
    from .. import check_workflow_templates as cwt

    ok, message = cwt.compare(cwt.read_templates(root))
    if not ok:
        raise CheckFailed(message)
    return message
