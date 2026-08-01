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

``--format json`` IS TRANSLATED; THE TEXT IS NOT
------------------------------------------------
A JSON consumer is a *program*, and a renamed key breaks it silently — it reads
``None`` where it used to read a version and carries on. So ``--format json``
emits exactly the old key set, rebuilt from the merged report by
``to_old_schema`` below. Only five keys actually moved; the other thirteen
survived the merge unchanged, which is why this is a rename table and not a
second serialiser.

The **output text** is not translated. Reproducing the old rendering would mean
keeping a second formatter alive, which is the duplication the merge removed —
and a human reading a report notices a changed layout, where a program reading a
renamed key does not. The finding CODES are unchanged (``PUBLISH_GAP``,
``RELEASE_YANKED``, ``UNRELEASED``, ``UNTAGGED_VERSION``,
``CHANGELOG_UNRELEASED``), so a caller grepping for those still works; one
matching the old prose does not.

Usage — identical to the old script:
  python scripts/release_gap.py --target ../some-mcp
  python scripts/release_gap.py --target . --max-age-days 14 --format json
  python scripts/release_gap.py --target . --offline
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
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


def to_old_schema(new: dict) -> dict:
    """The merged probe's report payload, in the keys this script used to emit.

    Thirteen of the eighteen keys survived the merge untouched and are copied
    straight through. Five moved, and each is a rename rather than a
    recomputation — nothing here derives a value the merged report does not
    already state:

        version      <- versions.repo      (the repository's own version)
        pypi_version <- index_version      }
        pypi_status  <- index_status       } `pypi_*` predates --index-url
        pypi_detail  <- index_detail       }
        ok           <- exit_code == 0

    The merged report's own additions (``schema``, ``depth``, ``publication``,
    ``tool_call`` …) are dropped rather than passed through. A consumer written
    against the old contract expects that key set; handing it a third, wider
    shape would be its own kind of surprise.
    """
    versions = new.get("versions") or {}
    repo_version = versions.get("repo") or ""
    return {
        "dist": new.get("dist"),
        # The old script defaulted a missing `[project] version` to the literal
        # "(dynamic)"; the merged one stores an empty string. Restored, because a
        # caller may well be testing for that exact word. A version that is
        # present but genuinely empty is indistinguishable here — it was already
        # a pathological case in the old script and stays one.
        "version": repo_version or "(dynamic)",
        "index_url": new.get("index_url"),
        "pypi_version": new.get("index_version"),
        "pypi_status": new.get("index_status"),
        "pypi_detail": new.get("index_detail"),
        "yanked": new.get("yanked", {}),
        "yank_source": new.get("yank_source"),
        "yank_detail": new.get("yank_detail"),
        "index_views": new.get("index_views", {}),
        "latest_tag": new.get("latest_tag"),
        "tags_available": new.get("tags_available"),
        "unreleased_commits": new.get("unreleased_commits"),
        "oldest_unreleased_age_days": new.get("oldest_unreleased_age_days"),
        "changelog_unreleased_entries": new.get("changelog_unreleased_entries"),
        "findings": new.get("findings", []),
        # `ok` was a property, not a stored field. The merged report exposes the
        # same judgement as its exit code, so it is read back off that rather
        # than re-derived from the findings list — one source, not two.
        "ok": new.get("exit_code") == sp.EXIT_GREEN,
    }


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
          "Its exit codes and its --format json keys are translated back to this "
          "script's old contract; the report TEXT is the merged probe's.",
          file=sys.stderr)
    if args.timeout is not None:
        print("release_gap.py: --timeout is ignored by the merged probe.",
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

    if args.format != "json":
        return translate(sp.main(forwarded))

    # Forward as usual, then rewrite the payload. Capturing the merged probe's
    # own JSON rather than calling `probe()` directly keeps this file a
    # forwarder: it never decides what goes in the report, only what the keys
    # are called.
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = sp.main(forwarded)
    raw = captured.getvalue()
    try:
        payload = json.loads(raw)
    except ValueError:
        # It did not print JSON — an argparse or harness path. Pass it through
        # untouched rather than swallowing it into a translation that cannot be
        # made; the exit code below still carries the verdict.
        sys.stdout.write(raw)
        return translate(rc)
    print(json.dumps(to_old_schema(payload), indent=2, ensure_ascii=False))
    return translate(rc)


if __name__ == "__main__":
    raise SystemExit(main())
