#!/usr/bin/env python3
"""Lockfile probe — is the declared bound in force where the install happens?

WHY THIS IS NOT ``yank_probe.py``
---------------------------------
``yank_probe`` reads ``Requires-Dist`` off the index and asks whether a
published release states an upper bound. That is a question about *metadata*:
what a resolver is told when it installs the package fresh.

This asks the neighbouring question, and it is the one that bites in the
repository rather than on PyPI:

    ``pyproject.toml`` states the bound. Does the lockfile — the file the
    deployment actually installs from — state it too?

A ``pyproject.toml`` that says ``mcp[cli]>=2.0.0,<3`` next to a ``uv.lock``
resolved before that line existed is two different dependency declarations in
one repository. Every review reads the first one; every ``uv sync``, every
container build and every CI job uses the second.

THE INCIDENT
------------
The upper bounds from a portfolio PR were merged, reviewed and green. They were
in ``pyproject.toml``. ``uv.lock`` was not regenerated, so its recorded
``requires-dist`` still carried the uncapped range and its pinned versions were
whatever the pre-bound resolution had produced. On ``main``, the fix was
present in the file everybody reads and absent from the file that installs. The
probe that would have caught it in one second did not exist. This is it.

WHAT IS CHECKED, AND WHAT EACH FINDING IS ENTITLED TO CLAIM
-----------------------------------------------------------
``LOCK_DRIFT`` — the specifier ``pyproject.toml`` states for a dependency is
not the specifier the lock recorded for it. Both are printed side by side,
because "the lock is out of date" is a sentence somebody has to act on and the
diverging pair is the whole of the action. ``uv.lock`` makes this directly
readable: it echoes the project's own ``requires-dist`` under
``[package.metadata]``, so the comparison needs no resolver and no network.
Clauses are compared as parsed sets, never as strings — ``>=2.0.0,<3`` and
``<3,>=2.0.0`` are one requirement, and ``<3`` and ``<3.0`` are one bound. A
probe that reported those as drift would be retired within a week.

``LOCK_UNSATISFIED`` — the version pinned in the lock is not admitted by the
specifier in ``pyproject.toml``. This is the sharpest of the four: it does not
argue about metadata hygiene, it says that what gets installed violates what
the project declared. It is the shape the incident above takes once the
dependency has actually released past the boundary.

``LOCK_STALE`` — ``uv lock --check`` or ``poetry check --lock`` says the lock
is out of date with respect to ``pyproject.toml``. The tools own the full
answer, including the parts of it this file deliberately does not model (marker
evaluation, extras closure, the resolver's own hashes). Where a tool is
installed it is asked; where it is not, its absence is REPORTED rather than
counted as agreement — see the note in the report. A check that did not run is
never a pass, which is the same rule ``shipped_probe.NO_TAGS`` follows.

``LOCK_MISSING_DEP`` — ``pyproject.toml`` declares a dependency that has no
package entry in the lock at all. An install from that lock does not get it.

WHAT IS DELIBERATELY NOT CLAIMED
--------------------------------
1. **Conditional requirements are skipped**, marker and extras alike — the same
   rule ``yank_probe`` documents. ``extra == 'dev'`` is not installed by a plain
   sync, and ``python_version < "3.12"`` holds for some installs and not
   others; deciding which without an environment to evaluate against would be a
   guess wearing a finding's clothes.
2. **A missing lockfile is not a finding.** A library that ships no lock has
   made a defensible choice, and turning that into a red gate would teach
   people to commit a lock they do not use. It exits ``3`` — NOT MEASURED, the
   same category the boot gate uses — so a report can never read this run as
   evidence of anything.
3. **Whether the deployment installs from the lock at all** is not checked
   here. A lock nobody syncs from is decoration, and finding that out means
   reading Dockerfiles and workflow files across a portfolio. It is a real gap
   and it belongs in its own probe rather than smuggled into this one's exit
   code.

READ-ONLY. ``uv lock --check`` and ``poetry check --lock`` are the only
subprocesses, both are read-only by contract, and neither is given a chance to
write: ``uv lock`` without ``--check`` would REGENERATE the file this probe
exists to compare against. The flag is not optional here and there is no switch
to drop it.

EXIT CODES
  0    the lock states what pyproject states
  2    FINDING — drift, an unsatisfied pin, a stale lock, or a missing entry
  3    NOT MEASURED — no lockfile in the target
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run (no pyproject.toml, unreadable TOML)

Usage:
  python scripts/lockfile_probe.py --target ../swiss-electricity-mcp
  python scripts/lockfile_probe.py --target . --format json
  python scripts/lockfile_probe.py --target . --no-tools    # parsing only
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

# PEP 508 parsing and PEP 440 bound semantics already exist in the yank gate and
# are exactly the semantics needed here — `~=2.1` and `==2.*` are upper bounds
# though neither spells `<`. A second implementation would be a second place for
# that to be subtly wrong.
import yank_probe as yp  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10; the project requires 3.11+
    tomllib = None  # type: ignore[assignment]

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

DEFAULT_TOOL_TIMEOUT = 60.0

UV_LOCK = "uv.lock"
POETRY_LOCK = "poetry.lock"


# --------------------------------------------------------------------------
# Comparing two specifier sets without comparing their spelling
# --------------------------------------------------------------------------


def _clause_key(op: str, version: str) -> tuple[str, Any]:
    """A clause reduced to its meaning.

    ``<3`` and ``<3.0`` are the same bound; ``>=2.0.0,<3`` and ``<3,>=2.0.0``
    are the same requirement. Trailing zeros are trimmed off the release tuple
    so the two spellings collapse. A version that does not parse keeps its
    string — undecidable, so compared literally rather than guessed at.
    """
    release = yp._release(version.rstrip(".*"))
    if release is None:
        return (op, version)
    trimmed = list(release)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
    star = ".*" if version.endswith(".*") else ""
    return (op, tuple(trimmed) if not star else (tuple(trimmed), star))


def specifier_set(requirement: yp.Requirement | None) -> frozenset[tuple[str, Any]]:
    if requirement is None:
        return frozenset()
    return frozenset(_clause_key(op, ver) for op, ver in requirement.clauses)


def render_clauses(requirement: yp.Requirement | None) -> str:
    """The specifier as a human reads it, or the word for its absence."""
    if requirement is None or not requirement.clauses:
        return "(no bound)"
    return ",".join(f"{op}{ver}" for op, ver in requirement.clauses)


def extras_of(requirement: yp.Requirement | None) -> frozenset[str]:
    if requirement is None or not requirement.extras:
        return frozenset()
    return frozenset(e.strip() for e in requirement.extras.split(",") if e.strip())


# --------------------------------------------------------------------------
# Reading the two files
# --------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover - the project requires 3.11+
        raise RuntimeError("tomllib is unavailable — this probe needs Python 3.11+")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def declared_requirements(pyproject: dict[str, Any]) -> dict[str, yp.Requirement]:
    """The unconditional runtime dependencies, by normalised name.

    PEP 621 ``[project].dependencies`` only. ``[project.optional-dependencies]``
    is skipped for the reason markers are: an extra nobody installs cannot break
    an install, and reporting one would train the reader to skim the findings.
    """
    out: dict[str, yp.Requirement] = {}
    for line in pyproject.get("project", {}).get("dependencies", []) or []:
        requirement = yp.parse_requirement(str(line))
        if requirement is None or requirement.conditional:
            continue
        out[requirement.key] = requirement
    return out


@dataclass
class LockView:
    """What one lockfile says, reduced to the two things worth comparing."""

    name: str  # uv.lock | poetry.lock
    kind: str  # uv | poetry
    # The project's own requires-dist as the lock recorded it. Empty for poetry:
    # poetry.lock does not echo it, so that half of the comparison is not
    # available there and the report says so rather than reporting agreement.
    recorded: dict[str, yp.Requirement] = field(default_factory=dict)
    records_requirements: bool = False
    pinned: dict[str, str] = field(default_factory=dict)  # name -> version
    detail: str = ""


def read_uv_lock(path: Path, dist: str) -> LockView:
    """``uv.lock``: pinned versions, plus the project's recorded requires-dist.

    ``[package.metadata].requires-dist`` on the root package is the load-bearing
    field: it is what ``pyproject.toml`` said at the moment the lock was
    resolved. Comparing against it is what turns "somebody forgot to re-lock"
    from an inference into a measurement.
    """
    data = _load_toml(path)
    view = LockView(name=path.name, kind="uv")
    root_key = yp.Requirement(name=dist).key
    for package in data.get("package", []) or []:
        key = yp.Requirement(name=str(package.get("name", ""))).key
        if not key:
            continue
        if key == root_key:
            metadata = package.get("metadata", {}) or {}
            entries = metadata.get("requires-dist")
            if entries is not None:
                view.records_requirements = True
                for entry in entries:
                    requirement = _uv_entry(entry)
                    if requirement is not None and not requirement.conditional:
                        view.recorded[requirement.key] = requirement
            continue
        version = str(package.get("version", "") or "")
        if version:
            view.pinned[key] = version
    if not view.records_requirements:
        view.detail = (
            f"{path.name} has no [package.metadata] requires-dist for {dist} — the "
            "specifier comparison could not be made (only the pinned versions were)"
        )
    return view


def _uv_entry(entry: Any) -> yp.Requirement | None:
    """One ``requires-dist`` entry of ``uv.lock``, in either spelling.

    uv writes an inline table; older lock versions wrote a PEP 508 string. Both
    are read, because a probe that only understands the current writer's output
    silently stops checking the moment a repository lags a release behind.
    """
    if isinstance(entry, str):
        return yp.parse_requirement(entry)
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name", "") or "")
    if not name:
        return None
    extras = entry.get("extras") or []
    extras_text = f"[{','.join(str(e) for e in extras)}]" if extras else ""
    specifier = str(entry.get("specifier", "") or "")
    marker = str(entry.get("marker", "") or "")
    line = f"{name}{extras_text}{specifier}"
    if marker:
        line += f"; {marker}"
    return yp.parse_requirement(line)


def read_poetry_lock(path: Path) -> LockView:
    """``poetry.lock``: pinned versions only.

    poetry does not echo the project's own requirement set into the lock, so
    ``LOCK_DRIFT`` cannot be measured from the file. ``LOCK_UNSATISFIED`` can —
    and it is the stronger statement anyway — and ``poetry check --lock``
    supplies the freshness half. The report names the gap instead of letting a
    poetry repository read as more thoroughly checked than it was.
    """
    data = _load_toml(path)
    view = LockView(name=path.name, kind="poetry")
    for package in data.get("package", []) or []:
        key = yp.Requirement(name=str(package.get("name", ""))).key
        version = str(package.get("version", "") or "")
        if key and version:
            view.pinned[key] = version
    view.detail = (
        "poetry.lock does not record the project's own requires-dist, so the "
        "specifier comparison is not available for it — the pinned versions and "
        "`poetry check --lock` are"
    )
    return view


# --------------------------------------------------------------------------
# The tools, asked read-only
# --------------------------------------------------------------------------


@dataclass
class ToolCheck:
    tool: str
    status: str = "not_run"  # ok | stale | not_run | unavailable | error
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"tool": self.tool, "status": self.status, "detail": self.detail}


def run_tool_check(kind: str, target: Path, timeout: float) -> ToolCheck:
    """``uv lock --check`` / ``poetry check --lock``. Never writes.

    Both commands are read-only by contract. ``uv lock`` WITHOUT ``--check``
    regenerates the very file this probe compares against, which is why the flag
    is hard-coded and there is no switch to drop it.
    """
    if kind == "uv":
        binary, argv = "uv", ["uv", "lock", "--check"]
    else:
        binary, argv = "poetry", ["poetry", "check", "--lock"]
    check = ToolCheck(tool=" ".join(argv))
    if not shutil.which(binary):
        check.status = "unavailable"
        check.detail = (
            f"{binary} is not on PATH — its freshness check did not run. That is "
            "reported, not counted as agreement: a check that did not happen is "
            "never a pass"
        )
        return check
    try:
        proc = subprocess.run(
            argv, cwd=str(target), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        check.status = "error"
        check.detail = f"{type(exc).__name__}: {exc}"
        return check
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        check.status = "ok"
        check.detail = "the tool considers the lock up to date with pyproject.toml"
        return check
    if _failed_rather_than_stale(output):
        # A resolver that could not reach an index has not disagreed with the
        # lock — it has not read it. Filing that as LOCK_STALE would put an
        # infrastructure failure on the repository's account, which is the
        # distinction the boot gate's 127 exists to keep.
        check.status = "error"
        check.detail = (
            f"`{check.tool}` failed for a reason that is not staleness, so the "
            f"freshness question is unanswered: {output[:300]}"
        )
        return check
    check.status = "stale"
    check.detail = output[:400] or f"exit {proc.returncode}"
    return check


# Phrases that mean "the tool could not do its job", as opposed to "the lock is
# out of date". Deliberately a short, unmistakable list: anything not on it is
# read as staleness, so a new wording produces a false finding rather than a
# silent pass.
_TOOL_FAILURE = (
    "failed to fetch",
    "network",
    "offline",
    "connection",
    "timed out",
    "timeout",
    "no solution found",
    "proxy",
    "certificate",
    "permission denied",
    "does not exist",
    "not found: ",
)


def _failed_rather_than_stale(output: str) -> bool:
    low = output.lower()
    return any(phrase in low for phrase in _TOOL_FAILURE)


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    severity: str
    dependency: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "dependency": self.dependency,
            "detail": self.detail,
        }


@dataclass
class Report:
    dist: str
    target: str
    status: str = "ok"  # ok | no_lockfile | harness_error
    locks: list[LockView] = field(default_factory=list)
    declared: dict[str, yp.Requirement] = field(default_factory=dict)
    tool_checks: list[ToolCheck] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    harness_error: str = ""
    provenance: probe_provenance.Provenance | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "probe": "lockfile",
            "dist": self.dist,
            "target": self.target,
            "status": self.status,
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "lockfiles": [
                {
                    "name": lock.name,
                    "kind": lock.kind,
                    "records_requirements": lock.records_requirements,
                    "packages": len(lock.pinned),
                    "detail": lock.detail,
                }
                for lock in self.locks
            ],
            "declared": {
                name: render_clauses(req) for name, req in sorted(self.declared.items())
            },
            "tool_checks": [c.as_dict() for c in self.tool_checks],
            "notes": list(self.notes),
            "findings": [f.as_dict() for f in self.findings],
            "harness_error": self.harness_error,
            "exit_code": self.exit_code(),
        }

    def exit_code(self) -> int:
        if self.provenance is not None and self.provenance.blocking:
            return probe_provenance.EXIT_MOVED
        if self.harness_error:
            return EXIT_CANNOT_RUN
        if self.findings:
            return EXIT_FINDINGS
        if self.status == "no_lockfile":
            return EXIT_NOT_MEASURED
        return EXIT_GREEN


def compare(declared: dict[str, yp.Requirement], lock: LockView) -> list[Finding]:
    """Every finding this probe can make from the two files alone."""
    findings: list[Finding] = []
    for name, requirement in sorted(declared.items()):
        recorded = lock.recorded.get(name)

        if lock.records_requirements:
            if recorded is None:
                findings.append(
                    Finding(
                        code="LOCK_DRIFT",
                        severity="high",
                        dependency=name,
                        detail=(
                            f"pyproject.toml declares `{name}{render_clauses(requirement)}`; "
                            f"{lock.name} recorded no requirement for it at all. The lock "
                            "predates the declaration — re-lock before the next install"
                        ),
                    )
                )
            else:
                want, got = specifier_set(requirement), specifier_set(recorded)
                if want != got:
                    bound_note = ""
                    if requirement.bounded_above() and not recorded.bounded_above():
                        # The incident, named: the cap exists where it is read
                        # and not where it is installed from.
                        bound_note = (
                            " — the upper bound is in pyproject.toml and NOT in the "
                            "lock, so it is not in force where the install happens"
                        )
                    findings.append(
                        Finding(
                            code="LOCK_DRIFT",
                            severity="high",
                            dependency=name,
                            detail=(
                                f"pyproject.toml says `{render_clauses(requirement)}`, "
                                f"{lock.name} recorded `{render_clauses(recorded)}`"
                                f"{bound_note}"
                            ),
                        )
                    )
                elif extras_of(requirement) != extras_of(recorded):
                    findings.append(
                        Finding(
                            code="LOCK_DRIFT",
                            severity="medium",
                            dependency=name,
                            detail=(
                                f"same version range, different extras: pyproject.toml "
                                f"asks for `{name}[{','.join(sorted(extras_of(requirement))) or '-'}]`, "
                                f"{lock.name} recorded "
                                f"`{name}[{','.join(sorted(extras_of(recorded))) or '-'}]` — "
                                "a different set of packages gets installed"
                            ),
                        )
                    )

        pinned = lock.pinned.get(name)
        if pinned is None:
            findings.append(
                Finding(
                    code="LOCK_MISSING_DEP",
                    severity="high",
                    dependency=name,
                    detail=(
                        f"pyproject.toml declares `{name}` and {lock.name} locks no version "
                        "of it — an install from this lock does not get the dependency"
                    ),
                )
            )
            continue
        admits = requirement.admits(pinned)
        if admits is False:
            findings.append(
                Finding(
                    code="LOCK_UNSATISFIED",
                    severity="high",
                    dependency=name,
                    detail=(
                        f"{lock.name} pins `{name}=={pinned}`, which "
                        f"`{render_clauses(requirement)}` in pyproject.toml does not admit. "
                        "What gets installed violates what the project declares"
                    ),
                )
            )
        elif admits is None:
            findings.append(
                Finding(
                    code="LOCK_UNDECIDABLE",
                    severity="low",
                    dependency=name,
                    detail=(
                        f"{lock.name} pins `{name}=={pinned}` and this probe cannot decide "
                        f"whether `{render_clauses(requirement)}` admits it (an epoch, a "
                        "local version or a pre-release segment). Not reported as clean"
                    ),
                )
            )
    return findings


def run(
    target: Path, use_tools: bool = True, timeout: float = DEFAULT_TOOL_TIMEOUT
) -> Report:
    report = Report(dist="", target=str(target))
    pyproject_path = target / "pyproject.toml"
    if not pyproject_path.exists():
        report.status = "harness_error"
        report.harness_error = (
            f"{target}: no pyproject.toml — nothing to compare a lock against"
        )
        return report
    try:
        pyproject = _load_toml(pyproject_path)
    except Exception as exc:  # noqa: BLE001 - unreadable input is a harness failure
        report.status = "harness_error"
        report.harness_error = (
            f"pyproject.toml could not be read: {type(exc).__name__}: {exc}"
        )
        return report

    report.dist = str(pyproject.get("project", {}).get("name", "") or "")
    report.declared = declared_requirements(pyproject)

    candidates = [(target / UV_LOCK, "uv"), (target / POETRY_LOCK, "poetry")]
    present = [(path, kind) for path, kind in candidates if path.exists()]
    if not present:
        report.status = "no_lockfile"
        report.notes.append(
            f"no {UV_LOCK} and no {POETRY_LOCK} in {target} — nothing was measured. "
            "A library that ships no lock has made a defensible choice; this run is "
            "not evidence that its bounds are in force anywhere"
        )
        return report

    if not report.declared:
        report.notes.append(
            "pyproject.toml declares no unconditional runtime dependencies — every "
            "comparison below is vacuous"
        )

    for path, kind in present:
        try:
            lock = (
                read_uv_lock(path, report.dist)
                if kind == "uv"
                else read_poetry_lock(path)
            )
        except Exception as exc:  # noqa: BLE001
            report.status = "harness_error"
            report.harness_error = (
                f"{path.name} could not be read: {type(exc).__name__}: {exc} — a lock "
                "this probe cannot parse is not a lock it can call clean"
            )
            return report
        report.locks.append(lock)
        if lock.detail:
            report.notes.append(lock.detail)
        report.findings.extend(compare(report.declared, lock))

        if use_tools:
            check = run_tool_check(kind, target, timeout)
            report.tool_checks.append(check)
            if check.status == "stale":
                report.findings.append(
                    Finding(
                        code="LOCK_STALE",
                        severity="high",
                        dependency="(project)",
                        detail=(
                            f"`{check.tool}` reports the lock out of date with "
                            f"pyproject.toml: {check.detail}"
                        ),
                    )
                )
            elif check.status in ("unavailable", "error"):
                report.notes.append(f"{check.tool}: {check.detail}")
        else:
            report.notes.append(
                "--no-tools: the resolver's own freshness check did not run, so "
                "marker evaluation and the extras closure were not checked"
            )

    return report


def render(report: Report) -> str:
    lines = [
        f"lockfile probe — {report.dist or '(unnamed project)'} in {report.target}"
    ]
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
        if report.provenance.blocking:
            lines.append(f"  {report.provenance.moved_detail()}")
            return "\n".join(lines)
    if report.harness_error:
        lines.append(f"  HARNESS: {report.harness_error}")
        return "\n".join(lines)
    if report.status == "no_lockfile":
        lines.append(
            f"  NOT MEASURED: {report.notes[0] if report.notes else 'no lockfile'}"
        )
        return "\n".join(lines)
    for lock in report.locks:
        lines.append(
            f"  {lock.name} [{lock.kind}]: {len(lock.pinned)} package(s), "
            f"requires-dist recorded: {'yes' if lock.records_requirements else 'no'}"
        )
    for check in report.tool_checks:
        lines.append(f"  {check.tool}: {check.status}")
    for note in report.notes:
        lines.append(f"  note: {note}")
    if not report.findings:
        lines.append(
            f"  the lock states what pyproject.toml states "
            f"({len(report.declared)} dependency/-ies compared)"
        )
    for finding in report.findings:
        lines.append(
            f"  {finding.code} [{finding.severity}] {finding.dependency}: "
            f"{finding.detail}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=".", help="path to the target checkout")
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="skip `uv lock --check` / `poetry check --lock` and compare "
        "the files only (offline; the report says what was skipped)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TOOL_TIMEOUT)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", default="", help="also write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()

    prov = probe_provenance.capture(target)
    report = run(target, use_tools=not args.no_tools, timeout=args.timeout)
    report.provenance = prov.recheck()

    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
