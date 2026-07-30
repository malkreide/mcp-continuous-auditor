#!/usr/bin/env python3
"""Release gap — is the fix on ``main`` the fix users actually install?

``identity_probe.py`` asks whether the version a server reports is *correct*.
This asks whether it is *current*: a repository can be green, audited, and
entirely fixed while every `pip install` still hands out the broken release.
Nothing in CI contradicts that, because CI tests the branch, not the artifact.

THE INCIDENT
------------
``meteoswiss-mcp`` (2026-07-30). The migration to the ``mcp`` 2.x SDK was
merged to ``main`` on the 29th. PyPI kept serving ``0.4.0``, which imports
``mcp.server.fastmcp`` — a module ``mcp`` 2.0.0 had removed the day before.
Every fresh ``uvx meteoswiss-mcp`` died on import for three days, until an
outside user filed the bug. The repository was, the whole time, fixed.

It happened a second time in the same afternoon: ``0.5.0`` was published, three
further fixes landed on ``main``, and until the next release PyPI served a
server whose ``meteo_current``, ``meteo_forecast`` and ``meteo_school_check``
all returned nothing.

Both windows are invisible to every other check in this repo. The live probe
hits the upstream API, not our package. The recall canary drives the server
from source. ``ruff``/``pytest`` see the branch. Only comparing the *published*
artifact against the *repository* closes it.

WHAT THIS REPORTS, AND WHY IN THAT ORDER
----------------------------------------
1. ``PUBLISH_GAP`` — a release tag exists that PyPI does not have. Somebody cut
   a release and it did not land: the workflow failed, or an OIDC/environment
   approval is still pending. This is the sharpest finding here, because the
   maintainer already believes the release happened.

2. ``UNRELEASED`` — commits on ``main`` beyond the last release. Reported with
   the age of the *oldest* one and a breakdown by Conventional-Commit type,
   because ``fix:`` sitting unreleased is a different fact from ``docs:``. In
   the incident above, every unreleased day was a user hitting a 404.

3. ``UNTAGGED_VERSION`` — ``pyproject.toml`` was bumped but no tag matches it.
   The usual, benign state of a prepared release; a finding only once it ages.

4. ``CHANGELOG_UNRELEASED`` — a ``[Unreleased]`` section with entries in it.
   Weakest signal, and deliberately last: it is prose, and prose lags.

THREE DELIBERATE DECISIONS
--------------------------
1. **Age is the finding, not the gap.** Every repository is ahead of PyPI for
   the minutes after a merge. A check that fires on that gets muted, and a
   muted check catches nothing — the same reasoning that keeps recall floors at
   half the observed count. ``--max-age-days`` (default 7) is the line.

2. **An unreachable PyPI is reported, never assumed away.** If the index cannot
   be reached, the comparison that matters did not happen, and this exits
   non-zero saying so rather than printing "in sync" from git alone. That is
   the lesson of the incident this script is named after: a failure that
   degrades into a plausible-looking success is worse than a loud one.

3. **A shallow clone has no tags, and that is not "never released".**
   ``git clone --depth 1`` fetches none, so an absent tag set is reported as
   unknown. Concluding "no releases" from it would invert the finding.

Version comparison is deliberately narrow: release segments only
(``1.2.3`` → ``(1, 2, 3)``), pre-release and local segments ignored for
ordering. Full PEP 440 would mean vendoring ``packaging`` into a stdlib-only
tool; the portfolio publishes plain release versions, and anything unparseable
is reported as "differs" instead of being silently ordered wrong.

Exit codes:
  0  no findings
  1  findings, or the PyPI comparison could not be made
  2  the target is not shaped as expected (no pyproject.toml)

Usage:
  python scripts/release_gap.py --target ../meteoswiss-mcp
  python scripts/release_gap.py --target . --max-age-days 14 --format json
  python scripts/release_gap.py --target . --offline      # git-only, honest about it
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomllib landed in 3.11
    tomllib = None  # type: ignore[assignment]

PYPI_JSON = "https://pypi.org/pypi/{dist}/json"
TAG = re.compile(r"^v?(\d+(?:\.\d+)*.*)$")
CHANGELOG_UNRELEASED = re.compile(r"^##\s*\[?Unreleased\]?", re.IGNORECASE)
CHANGELOG_HEADING = re.compile(r"^##\s")
# `fix:`, `feat(scope)!:`, `chore(deps):` — the prefix, not the scope.
CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?:")

# Commit types whose delay is felt by users. Everything else is housekeeping.
USER_FACING = frozenset({"fix", "feat", "perf", "revert"})


@dataclass
class Finding:
    code: str
    detail: str
    severity: str  # high | medium | low


@dataclass
class Report:
    dist: str
    version: str
    pypi_version: str | None = None
    pypi_status: str = "ok"  # ok | unreachable | not_published | skipped
    pypi_detail: str = ""
    tags: list[str] | None = None  # None = could not be determined (shallow clone)
    unreleased_commits: list[dict[str, str]] = field(default_factory=list)
    oldest_unreleased_age_days: float | None = None
    changelog_unreleased_entries: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings and self.pypi_status in ("ok", "not_published", "skipped")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def read_project(root: Path) -> dict[str, Any]:
    """The ``[project]`` table. Minimal parser when tomllib is unavailable.

    Mirrors ``identity_probe.read_project`` on purpose — the two scripts are
    siblings and should fail the same way on the same repository.
    """
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text).get("project", {})
    section = re.search(r"^\[project\]\s*$(.*?)(?=^\[)", text, re.MULTILINE | re.DOTALL)
    body = section.group(1) if section else text
    out: dict[str, Any] = {}
    for key in ("name", "version"):
        m = re.search(rf'^{key}\s*=\s*"([^"]+)"', body, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def git(root: Path, *args: str) -> str | None:
    """Run git, returning None when it fails rather than raising.

    Every caller here has a meaningful "could not determine" branch; turning a
    missing tag set into a traceback would lose that distinction.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def release_tags(root: Path) -> list[str] | None:
    """Tags that look like releases, newest first. None when undeterminable."""
    out = git(root, "tag", "--list", "--sort=-v:refname")
    if out is None:
        return None
    tags = [t for t in out.splitlines() if TAG.match(t.strip())]
    return tags


def fetch_pypi_version(dist: str, timeout: float) -> tuple[str | None, str, str]:
    """(version, status, detail). Never raises — the caller reports the status."""
    url = PYPI_JSON.format(dist=dist)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "not_published", f"{dist} is not on PyPI (HTTP 404)"
        return None, "unreachable", f"PyPI returned HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "unreachable", f"PyPI unreachable: {exc}"
    except (ValueError, KeyError) as exc:
        return None, "unreachable", f"PyPI response unparseable: {exc}"
    return payload.get("info", {}).get("version"), "ok", ""


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def release_key(version: str) -> tuple[int, ...] | None:
    """Release segment as a tuple, or None when it does not parse.

    See the module docstring: ordering is intentionally limited to plain
    release versions. Unparseable input is surfaced, not guessed at.
    """
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", version or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def normalise_tag(tag: str) -> str:
    m = TAG.match(tag.strip())
    return m.group(1) if m else tag.strip()


def commits_since(root: Path, ref: str | None) -> list[dict[str, str]]:
    """Commits on HEAD beyond ``ref`` (all of HEAD when ref is None)."""
    rng = f"{ref}..HEAD" if ref else "HEAD"
    out = git(root, "log", rng, "--no-merges", "--format=%H%x1f%cI%x1f%s")
    if not out:
        return []
    commits: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, when, subject = parts
        m = CONVENTIONAL.match(subject)
        commits.append(
            {
                "sha": sha[:9],
                "date": when,
                "subject": subject,
                "type": m.group("type") if m else "other",
            }
        )
    return commits


def age_days(iso: str, now: datetime | None = None) -> float:
    stamp = datetime.fromisoformat(iso)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (reference - stamp).total_seconds() / 86400.0


def count_changelog_unreleased(root: Path) -> int:
    """Non-empty, non-heading lines inside the ``[Unreleased]`` section."""
    path = root / "CHANGELOG.md"
    if not path.exists():
        return 0
    inside = False
    entries = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if CHANGELOG_UNRELEASED.match(line):
            inside = True
            continue
        if inside and CHANGELOG_HEADING.match(line):
            break
        if inside and line.strip() and not line.lstrip().startswith("###"):
            entries += 1
    return entries


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


def probe(
    target: Path,
    max_age_days: float,
    offline: bool,
    timeout: float,
    now: datetime | None = None,
) -> Report:
    project = read_project(target)
    dist = project.get("name", target.name)
    version = project.get("version", "(dynamic)")
    report = Report(dist=dist, version=version)

    if offline:
        report.pypi_status = "skipped"
        report.pypi_detail = "--offline: the published artifact was not consulted"
    else:
        report.pypi_version, report.pypi_status, report.pypi_detail = fetch_pypi_version(
            dist, timeout
        )

    report.tags = release_tags(target)
    latest_tag = report.tags[0] if report.tags else None

    # 1. PUBLISH_GAP — a cut release that never landed on the index.
    if report.pypi_status == "ok" and latest_tag:
        tag_key = release_key(normalise_tag(latest_tag))
        pypi_key = release_key(report.pypi_version or "")
        if tag_key and pypi_key and tag_key > pypi_key:
            report.findings.append(
                Finding(
                    code="PUBLISH_GAP",
                    severity="high",
                    detail=(
                        f"tag {latest_tag} exists, PyPI latest is {report.pypi_version} — "
                        "the release was cut but never landed. Check the publish workflow "
                        "run for that tag; a pending environment approval looks identical "
                        "to a failure from here."
                    ),
                )
            )

    # 2. UNRELEASED — work on main beyond the last release.
    report.unreleased_commits = commits_since(target, latest_tag)
    if report.unreleased_commits:
        oldest = min(report.unreleased_commits, key=lambda c: c["date"])
        report.oldest_unreleased_age_days = age_days(oldest["date"], now)
        user_facing = [c for c in report.unreleased_commits if c["type"] in USER_FACING]
        if report.oldest_unreleased_age_days > max_age_days:
            kinds: dict[str, int] = {}
            for c in report.unreleased_commits:
                kinds[c["type"]] = kinds.get(c["type"], 0) + 1
            breakdown = ", ".join(f"{n}× {t}" for t, n in sorted(kinds.items()))
            report.findings.append(
                Finding(
                    code="UNRELEASED",
                    severity="high" if user_facing else "low",
                    detail=(
                        f"{len(report.unreleased_commits)} commit(s) beyond "
                        f"{latest_tag or 'the start of history'}, oldest "
                        f"{report.oldest_unreleased_age_days:.1f} days old "
                        f"({breakdown})."
                        + (
                            f" {len(user_facing)} of them user-facing — every day of "
                            "delay is a day users run the old behaviour."
                            if user_facing
                            else " None user-facing; housekeeping only."
                        )
                    ),
                )
            )

    # 3. UNTAGGED_VERSION — pyproject bumped without a matching tag.
    if report.tags is not None and version not in ("(dynamic)", ""):
        tagged = {normalise_tag(t) for t in report.tags}
        if version not in tagged and report.oldest_unreleased_age_days is not None:
            if report.oldest_unreleased_age_days > max_age_days:
                report.findings.append(
                    Finding(
                        code="UNTAGGED_VERSION",
                        severity="medium",
                        detail=(
                            f"pyproject.toml says {version}, no tag matches it. A prepared "
                            "release that was never cut looks exactly like this."
                        ),
                    )
                )

    # 4. CHANGELOG_UNRELEASED — weakest signal, reported last.
    report.changelog_unreleased_entries = count_changelog_unreleased(target)
    if (
        report.changelog_unreleased_entries
        and report.oldest_unreleased_age_days is not None
        and report.oldest_unreleased_age_days > max_age_days
    ):
        report.findings.append(
            Finding(
                code="CHANGELOG_UNRELEASED",
                severity="low",
                detail=(
                    f"[Unreleased] carries {report.changelog_unreleased_entries} line(s) of "
                    "entries. Written up, not shipped."
                ),
            )
        )

    return report


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def render(report: Report) -> str:
    out: list[str] = []

    if report.pypi_status == "unreachable":
        out.append(
            f"UNKNOWN    {report.pypi_detail} — the published artifact was NOT compared. "
            "Findings below, if any, come from git alone."
        )
    elif report.pypi_status == "not_published":
        out.append(f"NOTE       {report.pypi_detail}; git-only comparison.")
    elif report.pypi_status == "skipped":
        out.append(f"NOTE       {report.pypi_detail}.")

    if report.tags is None:
        out.append(
            "NOTE       tags could not be listed (a --depth 1 clone fetches none) — "
            "'no releases' cannot be concluded from this."
        )

    for f in sorted(report.findings, key=lambda x: {"high": 0, "medium": 1, "low": 2}[x.severity]):
        out.append(f"{f.code:<20} [{f.severity}] {f.detail}")

    if not report.findings and report.pypi_status == "ok":
        latest = report.tags[0] if report.tags else "—"
        out.append(
            f"release OK ({report.dist}: pyproject {report.version}, "
            f"PyPI {report.pypi_version}, latest tag {latest}; "
            f"{len(report.unreleased_commits)} unreleased commit(s))"
        )
    return "\n".join(out)


def to_json(report: Report) -> dict[str, Any]:
    return {
        "dist": report.dist,
        "version": report.version,
        "pypi_version": report.pypi_version,
        "pypi_status": report.pypi_status,
        "pypi_detail": report.pypi_detail,
        "latest_tag": report.tags[0] if report.tags else None,
        "tags_available": report.tags is not None,
        "unreleased_commits": len(report.unreleased_commits),
        "oldest_unreleased_age_days": report.oldest_unreleased_age_days,
        "changelog_unreleased_entries": report.changelog_unreleased_entries,
        "findings": [
            {"code": f.code, "severity": f.severity, "detail": f.detail} for f in report.findings
        ],
        "ok": report.ok,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="release_gap")
    ap.add_argument("--target", default=".", help="path to the MCP server repo")
    ap.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="how long unreleased work may sit before it is a finding (default: 7)",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip the PyPI query; git-only, and the report says so",
    )
    ap.add_argument("--timeout", type=float, default=15.0, help="PyPI request timeout in seconds")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not (target / "pyproject.toml").exists():
        print(f"{target}: no pyproject.toml — not a Python MCP server repo", file=sys.stderr)
        return 2

    report = probe(target, args.max_age_days, args.offline, args.timeout)

    if args.format == "json":
        print(json.dumps(to_json(report), indent=2, ensure_ascii=False))
    else:
        print(render(report))

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
