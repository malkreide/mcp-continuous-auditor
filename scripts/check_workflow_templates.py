"""The `.yml.template` files under .github/workflows/ are well-formed YAML.

The templates are never executed here — they are what this repository hands to
other repos. Nothing else would catch a broken one; it would fail first in
whichever target repo copied it, far from the commit that broke it.

Parsing is not validation. A template that parses can still be wrong. But it
catches the class that hurts most: the one where a copied file is dead on
arrival in somebody else's CI.

**A run that finds no templates is not a pass.** Exiting 0 because the glob
came up empty would make a rename or a move a silent all-clear — the same
failure class the catalogue calls `OPS-005`. No templates found is a finding,
with a message saying so.

Usage:

    python scripts/check_workflow_templates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = Path(".github") / "workflows"
TEMPLATE_GLOB = "*.yml.template"


def parse_failures(templates: dict[str, str]) -> list[str]:
    """Pure: names and texts in, one message per unparseable template out.

    No filesystem, so the interesting cases are reachable from a test without
    writing files that a later run would have to clean up.
    """
    bad = []
    for name in sorted(templates):
        try:
            yaml.safe_load(templates[name])
        except yaml.YAMLError as exc:
            bad.append(f"{name}: {exc}")
    return bad


def compare(templates: dict[str, str]) -> tuple[bool, str]:
    """Pure: `(all_well_formed, message)`.

    An empty mapping is a finding, not a pass — see the module docstring.
    """
    if not templates:
        return False, (
            f"no {TEMPLATE_GLOB} files found under {TEMPLATE_DIR.as_posix()} — "
            "did they move or get renamed? An empty glob must not read as "
            "'all templates are fine'."
        )
    bad = parse_failures(templates)
    if bad:
        return False, "\n".join(bad)
    return True, f"{len(templates)} template(s) parse: {', '.join(sorted(templates))}"


def read_templates(root: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((root / TEMPLATE_DIR).glob(TEMPLATE_GLOB))
    }


def main(argv: list[str] | None = None) -> int:
    ok, message = compare(read_templates(REPO_ROOT))
    if ok:
        print(message)
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
