#!/usr/bin/env python3
"""Probe provenance — which commit does this report describe?

THE INCIDENT
------------
An identity-probe run on a portfolio repository reported ``VERSION = "0.4.0"``
as a hand-maintained literal. The finding was correct when it was measured and
false ten minutes later: ``main`` had moved, and the report named no commit, so
there was nothing in it that could be re-checked, contradicted or dated. It was
not wrong — it was *unanchored*, which is worse, because a wrong finding gets
argued with and an unanchored one gets filed.

Every probe in this repository makes claims about a checkout: "src/ is clean",
"the installed sources differ from the tree", "no upper bound in the lock".
None of those sentences mean anything without the commit they are about. A
report that omits the SHA asserts something about a state it cannot name.

WHAT THIS MODULE DOES
---------------------
Two calls, at the two ends of a probe run:

    prov = probe_provenance.capture(target)
    ... the probe runs ...
    prov.recheck()

``capture`` records ``HEAD``, the branch, whether the checkout is shallow, and
a digest of the *uncommitted* state. ``recheck`` reads them again. If anything
moved between the two, the report's status becomes ``MOVED_DURING_RUN`` and the
probe returns that instead of a verdict.

Refusing to answer is the point. A probe that reads a tree over thirty seconds
while a rebase lands underneath it has measured two different trees and has no
way to know which finding came from which. "I cannot tell you" is a true
statement about that run; a merged verdict is not.

WHY THE DIRTY DIGEST, AND NOT JUST THE SHA
------------------------------------------
``git checkout``, ``git stash pop`` and an editor writing a file all change what
a probe reads while ``HEAD`` stays exactly where it was. The SHA alone would
call that run pinned. So the digest covers ``git status --porcelain`` — the set
of paths that differ from ``HEAD`` and how — and a change in *that* is a move
too. It is deliberately not a content hash of the worktree: probes routinely
create venvs, caches and report files inside the target, and a hash that reacted
to those would report every full-depth run as having moved.

A checkout that is dirty at both ends with the same digest is ``PINNED_DIRTY``,
not ``MOVED_DURING_RUN``: it did not move, but the SHA does not name it either,
and a report that says ``PINNED`` about a modified tree makes a promise its
reader cannot verify by checking out that commit.

WHEN THERE IS NO SHA
--------------------
Not every target is a git checkout — a probe can be pointed at an unpacked
sdist, and CI can run without git on the PATH. That case is ``UNPINNED`` with a
reason, never a silently absent field. It does not fail the run: a probe whose
findings are real is still useful without provenance. It simply must not claim
the anchor it does not have, which is the same rule ``shipped_probe.NO_TAGS``
and ``published_probe``'s ``UNVERIFIED`` follow.

NOT EVERY PROBE IS TALKING ABOUT THE CHECKOUT
---------------------------------------------
``identity_probe`` reads the tree, so a tree that moves invalidates it.
``yank_probe`` and ``published_probe`` read a package index; the checkout only
tells them which distribution to ask about. Suppressing a catalogue finding
because somebody committed locally would be superstition, not rigour.

So ``capture(..., decisive=False)`` marks a run whose verdict does not come from
the tree. Such a run still records and prints the move — the report says which
commit it started from and that the tree changed — but it keeps its verdict, and
``blocking`` stays False. ``blocking``, not ``moved``, is what a probe branches
on. The distinction is written into the report so a reader is never left to
infer which of the two happened.

EXIT CODE
---------
``EXIT_MOVED = 4``, shared by every probe that adopts this module, and outside
the 0/2/3/127 vocabulary the gates already read. ``portfolio_scan`` maps an
unknown return code to an error cell rather than to green or to a finding, which
is the correct reading of a run that reached no verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXIT_MOVED = 4

PINNED = "PINNED"
PINNED_DIRTY = "PINNED_DIRTY"
MOVED_DURING_RUN = "MOVED_DURING_RUN"
UNPINNED = "UNPINNED"
OPEN = "OPEN"  # captured, not yet re-checked

_TIMEOUT = 30


def _git(root: Path, *args: str) -> str | None:
    """Run git in ``root``; None when it cannot answer.

    Mirrors ``shipped_probe.git`` deliberately rather than importing it: this
    module sits *below* the probes so that any of them can use it without
    pulling in an index client, and a four-line subprocess call is a cheaper
    dependency than that inversion.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Paths a probe creates by doing its job. A boot probe that starts a server in
# the checkout leaves `__pycache__/` behind; an install leaves an egg-info. If
# those counted as a move, every full-depth run would report MOVED_DURING_RUN
# about itself — a probe crying wolf at its own footprints is the fastest way
# to get the whole check switched off. The same list the boot probe and
# `portfolio_scan` already skip.
_NOISE = (
    "__pycache__/",
    ".venv/",
    "venv/",
    ".tox/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    ".eggs/",
    ".audit/",
    "node_modules/",
    "dist/",
    "build/",
    ".egg-info/",
)


def _is_noise(path: str) -> bool:
    probe_path = path if path.endswith("/") else path + "/"
    return path.endswith(".pyc") or any(seg in probe_path for seg in _NOISE)


def _porcelain(root: Path) -> list[str] | None:
    """``git status --porcelain`` minus the probe's own byproducts.

    ``--porcelain`` (v1) is stable across git versions by contract, which is why
    it is the input and ``git diff`` is not. Untracked files are included: a
    probe that imports a module reads it whether or not git tracks it.
    """
    raw = _git(root, "status", "--porcelain")
    if raw is None:
        return None
    kept = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not _is_noise(path):
            kept.append(line)
    return sorted(kept)


def _worktree_digest(root: Path) -> str | None:
    """A short digest of everything that differs from HEAD."""
    entries = _porcelain(root)
    if entries is None:
        return None
    return hashlib.sha256("\n".join(entries).encode("utf-8", "replace")).hexdigest()[
        :16
    ]


@dataclass
class Provenance:
    """The commit a report is about, measured at both ends of the run."""

    target: str
    head: str | None = None
    branch: str | None = None
    shallow: bool = False
    digest: str | None = None
    dirty: bool = False
    started: str = field(default_factory=_now)
    # False for probes whose verdict comes from an index rather than the tree.
    decisive: bool = True

    head_after: str | None = None
    digest_after: str | None = None
    finished: str | None = None

    unavailable: str = ""
    moves: list[str] = field(default_factory=list)

    # ---- state ----------------------------------------------------------

    @property
    def status(self) -> str:
        if self.unavailable:
            return UNPINNED
        if self.finished is None:
            return OPEN
        if self.moves:
            return MOVED_DURING_RUN
        return PINNED_DIRTY if self.dirty else PINNED

    @property
    def moved(self) -> bool:
        return self.status == MOVED_DURING_RUN

    @property
    def blocking(self) -> bool:
        """Did a move invalidate this probe's verdict? The branch probes take."""
        return self.moved and self.decisive

    @property
    def pinned(self) -> bool:
        """Is there a commit this report can be re-checked against?"""
        return self.status in (PINNED, PINNED_DIRTY)

    @property
    def short(self) -> str:
        return (self.head or "unknown")[:12]

    # ---- the second measurement -----------------------------------------

    def recheck(self) -> Provenance:
        """Read HEAD and the worktree again; record what moved. Returns self."""
        self.finished = _now()
        if self.unavailable:
            return self
        self.head_after = _git(Path(self.target), "rev-parse", "HEAD")
        self.digest_after = _worktree_digest(Path(self.target))
        if self.head_after is None:
            # The checkout was readable at the start and is not now — a probe
            # cannot conclude anything about a tree that disappeared under it.
            self.moves.append(
                f"HEAD was {self.short} at the start and could not be read at the end"
            )
            return self
        if self.head_after != self.head:
            self.moves.append(
                f"HEAD moved {self.short} → {self.head_after[:12]} during the run"
            )
        if self.digest_after != self.digest:
            self.moves.append(
                "the working tree changed during the run "
                f"(uncommitted-state digest {self.digest} → {self.digest_after})"
            )
        return self

    # ---- output ----------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decisive": self.decisive,
            "blocking": self.blocking,
            "target": self.target,
            "head": self.head,
            "head_after": self.head_after,
            "branch": self.branch,
            "shallow": self.shallow,
            "dirty": self.dirty,
            "worktree_digest": self.digest,
            "worktree_digest_after": self.digest_after,
            "started": self.started,
            "finished": self.finished,
            "unavailable": self.unavailable,
            "moves": list(self.moves),
        }

    def render(self) -> str:
        """One line, for the top of a text report."""
        if self.unavailable:
            return f"provenance: UNPINNED — {self.unavailable}"
        where = f"{self.short}"
        if self.branch:
            where += f" ({self.branch})"
        if self.shallow:
            where += " [shallow]"
        if self.status == MOVED_DURING_RUN:
            line = f"provenance: MOVED_DURING_RUN {where} — {'; '.join(self.moves)}"
            if not self.decisive:
                line += (
                    " — this probe's verdict is read from the index, not from "
                    "the tree, so it stands; the checkout fields do not"
                )
            return line
        if self.status == PINNED_DIRTY:
            return (
                f"provenance: PINNED_DIRTY {where} — the working tree carries "
                "uncommitted changes, so this commit does not reproduce what was read"
            )
        if self.status == OPEN:
            return f"provenance: {where} — run in progress"
        return f"provenance: PINNED {where}"

    def moved_detail(self) -> str:
        """The sentence a probe prints instead of a verdict."""
        return (
            "MOVED_DURING_RUN — " + "; ".join(self.moves) + ". No verdict is reported: "
            "the checks in this run did not all read the same tree, and a merged "
            "result would name a state that never existed. Re-run against a fixed "
            "commit (`git checkout " + self.short + "`) to get an answer."
        )


def capture(target: Path | str, decisive: bool = True) -> Provenance:
    """Record the state a probe is about to read. Never raises.

    ``decisive=False`` for a probe whose verdict comes from a package index: the
    move is still reported, but it does not withdraw the verdict.
    """
    root = Path(target)
    prov = Provenance(target=str(root), decisive=decisive)
    if not root.is_dir():
        prov.unavailable = f"{root} is not a directory"
        return prov
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        # Both "git is not installed" and "this is an unpacked sdist" land
        # here, and the distinction matters to whoever reads the report.
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
        prov.unavailable = (
            f"{root} is not a git checkout — no commit to pin this report to"
            if inside is None
            else f"{root}: git could not resolve HEAD (an empty repository?)"
        )
        return prov
    prov.head = head
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    prov.branch = None if branch in (None, "HEAD") else branch
    prov.shallow = (_git(root, "rev-parse", "--is-shallow-repository") or "") == "true"
    prov.digest = _worktree_digest(root)
    prov.dirty = bool(_porcelain(root))
    return prov


def capture_auditor(decisive: bool = False) -> Provenance:
    """Provenance of the checkout this probe's own code and manifests live in.

    For a probe with no ``--target`` — ``live_probe``, ``recall_canary``,
    ``published_probe`` — the tree that decides the run is *this* repository:
    the manifest that says which targets to visit, and the floors the answers
    are held to. "The canary was green" is a different claim depending on which
    revision of the manifest it walked, and until now no report said which.

    Not decisive by default: the verdict comes from a live server or an index,
    and the auditor moving does not withdraw it.
    """
    return capture(Path(__file__).resolve().parents[1], decisive=decisive)


def main(argv: list[str] | None = None) -> int:
    """Print the provenance of a checkout. Useful on its own for a shell gate."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=".", help="path to the checkout")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    prov = capture(Path(args.target)).recheck()
    if args.format == "json":
        print(json.dumps(prov.as_dict(), indent=2, sort_keys=True))
    else:
        print(prov.render())
    return EXIT_MOVED if prov.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
