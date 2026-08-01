#!/usr/bin/env python3
"""release_gap — compatibility shim over ``shipped_probe.py --metadata-only``.

WHAT THIS IS, AND WHY IT IS NOT THE OLD SCRIPT
----------------------------------------------
``release_gap.py`` was merged into ``shipped_probe.py``, where its question is
now the *metadata depth*: index + git, two HTTP requests, no venv and no
install. The merge deleted this file, which broke every caller outside this
repository — the name vanished, and the exit codes it had promised changed
underneath anyone who moved to the new one.

This restores the OLD CONTRACT so those callers work again unmodified. It is a
migration aid, not a second implementation: there is no probe logic here, only
argument forwarding and an exit-code translation. Delete it once nothing calls
``release_gap.py`` any more.

THE EXIT-CODE TRANSLATION IS THE POINT
--------------------------------------
The two vocabularies do not map one-to-one, and the gap is exactly where a
silent wrong answer would live::

    old (this file)                      new (shipped_probe.py)
    0  no findings                       0    green
    1  findings, OR the index            2    FINDINGS
       comparison could not be made      127  the harness could not run
    2  not a Python MCP repo                  (unreachable index *or* no
                                              distribution name)

``127`` covers two old codes at once. Mapping it blindly to either one would be
wrong half the time — as ``2`` it would tell a caller "this is not a Python
repo" when the truth was an unreachable index. So the one case that ``2`` meant
is decided HERE, before forwarding, exactly as the old script decided it: a
target without ``pyproject.toml`` never reached the network then either.
Everything else that prevents a verdict collapses into ``1``, which is what the
old script did with an unreachable index.

WHAT IS DELIBERATELY NOT REPRODUCED
-----------------------------------
The **output text** is the merged probe's, not the old one's. Reproducing the
old rendering would mean keeping a second formatter alive, which is the
duplication the merge removed. The finding CODES are unchanged
(``PUBLISH_GAP``, ``RELEASE_YANKED``, ``UNRELEASED``, ``UNTAGGED_VERSION``,
``CHANGELOG_UNRELEASED``), so a caller grepping for those still works; a caller
matching the old prose, or parsing ``--format json`` by its old key names, does
not. ``--format json`` therefore warns on stderr rather than pretending.

Usage — identical to the old script:
  python scripts/release_gap.py --target ../some-mcp
  python scripts/release_gap.py --target . --max-age-days 14 --format json
  python scripts/release_gap.py --target . --offline
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shipped_probe as sp

# old code -> what it meant. See the module docstring for why 127 cannot be
# translated by table alone.
EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_NOT_A_PYTHON_REPO = 2

REPLACEMENT = "python scripts/shipped_probe.py --target <path> --metadata-only"


def translate(rc: int) -> int:
    """Merged-probe exit code -> the code this file's callers were promised."""
    if rc == sp.EXIT_GREEN:
        return EXIT_CLEAN
    # Everything else — findings (2), an unreachable index (127), an unexpected
    # code — is `1` here. The old script made the same collapse: it exited 1
    # both for findings and for "the comparison could not be made", on the
    # grounds that neither is a pass.
    return EXIT_FINDINGS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="release_gap",
        description="Deprecated shim. Use: " + REPLACEMENT,
    )
    p.add_argument("--target", default=".", help="path to the MCP server repo")
    p.add_argument("--max-age-days", type=float, default=7.0)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--index-url", default=sp.DEFAULT_INDEX)
    p.add_argument("--format", choices=("text", "json"), default="text")
    # Accepted and ignored: the merged probe fixes the index request timeout,
    # and its install/run timeouts belong to a phase this depth never reaches.
    # Rejecting the flag would break the callers this file exists to unbreak;
    # silently swallowing it would let someone believe they had set something.
    p.add_argument("--timeout", type=float, default=None, help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    print(f"release_gap.py is deprecated — this is a shim over `{REPLACEMENT}`. "
          "Its exit codes are translated back to this script's old contract; "
          "the report text is the merged probe's.", file=sys.stderr)
    if args.timeout is not None:
        print("release_gap.py: --timeout is ignored by the merged probe.",
              file=sys.stderr)
    if args.format == "json":
        print("release_gap.py: --format json emits the MERGED probe's schema, not "
              "this script's old keys (`pypi_version` is now `index_version`, and "
              "the report carries `depth`, `index_url` and `index_views`).",
              file=sys.stderr)

    target = Path(args.target).resolve()
    # Decided here, not translated from 127 — see the module docstring. This is
    # also where the old script decided it, before touching the network.
    if not (target / "pyproject.toml").exists():
        print(f"{target}: no pyproject.toml — not a Python MCP server repo",
              file=sys.stderr)
        return EXIT_NOT_A_PYTHON_REPO

    forwarded = [
        "--target", str(target),
        "--metadata-only",
        "--max-age-days", str(args.max_age_days),
        "--index-url", args.index_url,
        "--format", args.format,
    ]
    if args.offline:
        forwarded.append("--offline")
    return translate(sp.main(forwarded))


if __name__ == "__main__":
    raise SystemExit(main())
