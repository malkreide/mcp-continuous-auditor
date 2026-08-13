#!/usr/bin/env python3
"""Is the `mcp-audit-skill` tag this repository cites still the current one?

WHAT THIS COMPLETES
-------------------
`tests/test_quality_chain_table.py` holds every content link in this
repository's documentation to one `PIN` constant. It asserts CONSISTENCY, and
it says so in its own docstring: whether that tag is still the LATEST release
of `mcp-audit-skill` needs the network, and a stdlib-only test has no business
reaching for it.

So the pin could rot in exactly one way the tests could not see: correct
everywhere, pointing at a release two versions old. That is a smaller failure
than a link to `main` — a stale pin is at least a real, readable snapshot —
but "smaller" is not "visible", and invisible is the property this portfolio
exists to remove.

This script is the other half. Same shape as `sdk-drift.yml` in
`mcp-audit-skill`, and for the same reason: the pin stays pinned so the tests
stay reproducible, and a weekly run makes the drift visible anyway. **A red
result here is not a reason to block a pull request** — it is the prompt to
raise the pin deliberately.

TWO QUESTIONS, NOT ONE
----------------------
1. **Does the pinned tag exist at all?** A typo, or a tag deleted upstream,
   leaves every link in the documentation pointing at nothing while the local
   tests stay green — they only compare the pin against itself.
2. **Is it the latest release?** The drift this exists for.

They fail differently and are reported differently. A pin that points at
nothing is broken now; a pin behind the latest release is a decision waiting
to be made.

THE DOCTRINE, AS EVERYWHERE ELSE IN THIS PORTFOLIO
--------------------------------------------------
* **An unreachable API is not a pass.** Without an answer the comparison did
  not happen; that is `UNKNOWN` and exit 1, never "in sync".
* **A missing anchor is an error.** If `PIN` cannot be read from the test
  file — renamed, moved, reformatted — this script says so instead of
  comparing against a default it invented.
* **This script does not write.** Raising the pin means editing `PIN`, the
  links beside it, and re-reading what changed in the skill between the two
  releases. That is a judgement, and it belongs to a person.

Exit codes:
  0  the pin exists and is the latest release
  1  drift, a pin pointing at nothing, or the API could not be reached
  2  usage error (the pin could not be read)

Usage:
    python scripts/audit_pin_drift.py
    python scripts/audit_pin_drift.py --format json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the pin lives. ONE place, by construction — see the test's docstring.
PIN_FILE = REPO_ROOT / "tests" / "test_quality_chain_table.py"
PIN_NAME = "PIN"

UPSTREAM = "malkreide/mcp-audit-skill"
LATEST_URL = "https://api.github.com/repos/{repo}/releases/latest"
TAG_URL = "https://api.github.com/repos/{repo}/git/ref/tags/{tag}"


def read_pin(path: Path | None = None, name: str = PIN_NAME) -> str:
    """The pinned tag, parsed out of the module that owns it.

    `path` defaults to `None` and resolves to `PIN_FILE` at CALL time, not at
    definition time. Written the obvious way — `path: Path = PIN_FILE` — the
    module attribute becomes a decoy: rebinding it changes nothing, because
    the default was captured when this function was defined. A test caught
    exactly that here, which is the reason the signature looks like this.

    Parsed rather than imported: importing pulls in `pytest` and the whole test
    module for one string. Parsed rather than grepped: a regex would also match
    the constant's name inside a docstring or an error message, and this file
    has both.

    A missing constant raises. Falling back to a default would make every
    rename of `PIN` a silent all-clear against a tag nobody chose.
    """
    path = PIN_FILE if path is None else path
    if not path.is_file():
        raise LookupError(f"{path} is missing — the pin has no home any more.")

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
                raise LookupError(
                    f"{path.name}: `{name}` is not a plain string literal — "
                    "this script cannot read a computed pin, and guessing one "
                    "would be worse than stopping."
                )

    raise LookupError(
        f'{path.name}: no module-level `{name} = "..."` found. The anchor is '
        "gone; without it there is nothing to compare against."
    )


def compare(pin: str, latest: str | None, tag_exists: bool | None) -> tuple[bool, str]:
    """(ok, message). Pure — no network, no filesystem.

    `None` means NOT MEASURED for either input, and is reported as such. It is
    deliberately not folded into "differs": one says the pin is wrong, the
    other says nobody looked, and treating them alike is how a gate starts
    lying.
    """
    if tag_exists is False:
        return False, (
            f"BROKEN: the pinned tag {pin} does not exist in {UPSTREAM}. Every "
            "content link in this repository's documentation points at nothing. "
            "The local tests cannot see this — they only hold the pin against "
            "itself."
        )

    unmeasured = []
    if tag_exists is None:
        unmeasured.append("whether the pinned tag exists")
    if latest is None:
        unmeasured.append("the latest release")
    if unmeasured:
        return False, (
            "UNKNOWN: could not determine " + " and ".join(unmeasured) + ". "
            "The comparison did not happen, so this is NOT a pass — reporting "
            "«in sync» here would be the one answer worse than none."
        )

    if pin == latest:
        return True, f"In sync: {pin} is the latest release of {UPSTREAM}."

    return False, (
        f"DRIFT: this repository cites {pin}, the latest release of {UPSTREAM} "
        f"is {latest}.\n"
        "  Not a reason to block a pull request. Raising the pin means editing "
        "PIN in tests/test_quality_chain_table.py, the links it holds, and "
        "reading what changed in the skill between the two releases — a "
        "judgement, not a bump."
    )


def _request(url: str, timeout: float) -> tuple[Any | None, str]:
    """(payload, status). Never raises — the caller reports the status.

    Deliberately the thinnest function here: everything that happens inside is
    out of reach for the tests, so as little as possible should happen inside.
    """
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), "ok"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable: {exc}"
    except ValueError as exc:
        return None, f"unreadable response: {exc}"


def fetch_latest(repo: str = UPSTREAM, timeout: float = 15.0) -> tuple[str | None, str]:
    payload, status = _request(LATEST_URL.format(repo=repo), timeout)
    if payload is None:
        return None, status
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag:
        return None, "response carries no tag_name"
    return tag, "ok"


def fetch_tag_exists(
    tag: str, repo: str = UPSTREAM, timeout: float = 15.0
) -> tuple[bool | None, str]:
    """True / False / None — and `None` is a third answer, not a fallback.

    A 404 is evidence of absence here: the endpoint answers for a tag that
    exists. Any other failure is absence of evidence, and the two must not
    collapse into one.
    """
    payload, status = _request(TAG_URL.format(repo=repo, tag=tag), timeout)
    if payload is not None:
        return True, "ok"
    if status == "HTTP 404":
        return False, "ok"
    return None, status


def _kurz(path: Path) -> str:
    """Repo-relative where possible, absolute otherwise.

    `Path.relative_to` RAISES for a path outside the tree rather than falling
    back, and a diagnostic line that crashes takes the diagnosis with it.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_pin_drift",
        description=(
            "Compare the mcp-audit-skill tag this repository cites against the "
            "latest release upstream."
        ),
    )
    parser.add_argument("--repo", default=UPSTREAM)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        pin = read_pin()
    except LookupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    exists, exists_status = fetch_tag_exists(pin, args.repo, args.timeout)
    latest, latest_status = fetch_latest(args.repo, args.timeout)
    ok, message = compare(pin, latest, exists)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "pin": pin,
                    "upstream": args.repo,
                    "latest": latest,
                    "tag_exists": exists,
                    "status": {"tag": exists_status, "latest": latest_status},
                    "ok": ok,
                    "message": message,
                },
                indent=2,
            )
        )
        return 0 if ok else 1

    print(message)
    if not ok:
        print(f"\n  pin file : {_kurz(PIN_FILE)}")
        print(f"  tag call : {exists_status}")
        print(f"  latest   : {latest_status}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
