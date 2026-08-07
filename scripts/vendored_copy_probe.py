#!/usr/bin/env python3
"""Vendored-copy probe — do the files that claim to be identical still match?

WHY THIS IS NOT ``reference_drift_probe.py``
--------------------------------------------
That probe compares a template against its adopters, and it compares
PROPERTIES rather than text — deliberately, and for a good reason: an adopting
repository renames the constants, rewraps the lines and renames the function,
and every one of those is a correct adoption. A text diff there would report
forty things nobody will fix.

This is the other arrangement, and it needs the opposite comparison. A vendored
copy is not adopted, it is *duplicated*, and the file says so about itself::

    VENDORED COPY (v1.1.0). Dieses Modul wird **byte-identisch** in mehreren
    `*-mcp`-Servern des Portfolios vorgehalten […] Änderungen hier und in den
    Schwesterkopien **synchron** halten.

When byte-identity is the declared contract, comparing bytes is not crude — it
is the contract. Nothing here is a property predicate, because nothing here is
allowed to be renamed.

THE INCIDENT
------------
``sparql_client.py`` is held in ``swiss-environment-mcp`` and ``fedlex-mcp``
and carries the header above in both. On 2026-08-07 they were 250 and 140
lines: the retry policy had been repaired in one copy and never reached the
other, so one server honoured ``Retry-After`` and jittered its backoff while
its twin did neither.

**And the version marker read ``v1.1.0`` on both sides.** That is the part
worth mechanising. The drift itself is ordinary — someone edits one copy under
time pressure. What made it survive is that the copies each *declared* they
were the same version, so there was no artefact anywhere that disagreed with
anything. In each repository only one half is visible, and both halves said
``v1.1.0``.

WHAT IS COMPARED
----------------
The bytes, and the marker.

* ``COPY_DRIFT`` — two sites that declare byte-identity do not match. The
  detail says whether they would match with trailing whitespace and blank lines
  normalised away, because "the copies differ" is a sentence somebody has to
  act on and a stray newline is a different job from a missing retry policy.
* ``MARKER_STALE`` — the copies differ AND every marker read is the same
  string. This is the incident, and it is a distinct finding rather than a
  footnote on the first: it is the reason nobody noticed. Bumping the marker on
  the edited side would not have fixed the drift, but it would have made it
  visible to the next reader of either repository.
* ``MARKER_SPLIT`` — the copies differ and so do the markers. Lower severity on
  purpose: this is drift that announces itself. Somebody comparing the two
  headers learns the truth in one second.
* ``MARKER_MISSING`` — a site declares a copy group but carries no marker at
  all. Not the same as stale; there is nothing to go stale.

WHAT THIS PROBE REFUSES TO DO
-----------------------------
It does not guess the groups. The mapping — which files in which repositories
claim to be the same file — comes from a manifest and nowhere else. A copy
detected by name similarity would produce findings nobody can retrace, and the
sister probe's note applies here word for word: a finding nobody can retrace is
how a gate gets switched off.

It does not fetch. A checkout that is not on disk is reported as
``SITE_MISSING`` and the group is UNVERIFIED. A group compared against one
readable file is not "clean" — it is unmeasured, and it says so.

It does not pick a winner. Which copy is right is a judgement about the code,
not about the bytes; the probe says the two disagree and prints both digests.

EXIT CODES
----------
0 green · 2 findings · 3 nothing measured · 127 cannot run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

# A group compared against fewer than this many readable files has not been
# compared at all. Two is the real floor here and not a convention: byte
# identity is a statement about a pair.
MIN_SITES = 2

DEFAULT_MARKER = r"VENDORED COPY \(([^)]+)\)"


class ManifestError(Exception):
    """The manifest cannot be read, or says something the probe cannot use."""


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


@dataclass
class Site:
    repo: str
    file: str
    path: Path | None = None
    digest: str = ""
    normalised: str = ""
    marker: str | None = None
    unreadable: str = ""

    @property
    def name(self) -> str:
        return f"{self.repo}:{self.file}"


@dataclass
class Group:
    name: str
    says: str
    marker_pattern: str
    sites: list[Site]


def _require(raw: dict, key: str, where: str) -> Any:
    if key not in raw:
        raise ManifestError(f"{where}: missing `{key}`")
    return raw[key]


def load_manifest(path: Path) -> list[Group]:
    """Parse the manifest, or raise ``ManifestError``.

    Every failure here is loud. A manifest the probe half-understands is worse
    than none: it silently narrows what gets compared, and the run still prints
    a green line.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"no manifest at {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc

    groups_raw = raw.get("group")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ManifestError(f"{path}: no `[[group]]` table — nothing to compare")

    groups: list[Group] = []
    for index, entry in enumerate(groups_raw):
        where = f"{path}: group #{index + 1}"
        if not isinstance(entry, dict):
            raise ManifestError(f"{where}: not a table")
        name = str(_require(entry, "name", where))
        sites_raw = _require(entry, "site", f"{where} ({name})")
        if not isinstance(sites_raw, list) or len(sites_raw) < MIN_SITES:
            raise ManifestError(
                f"{where} ({name}): needs at least {MIN_SITES} `[[group.site]]` "
                "entries — byte identity is a statement about a pair, and a "
                "group with one site would always be green"
            )
        sites: list[Site] = []
        for site_index, site_raw in enumerate(sites_raw):
            site_where = f"{where} ({name}): site #{site_index + 1}"
            if not isinstance(site_raw, dict):
                raise ManifestError(f"{site_where}: not a table")
            sites.append(
                Site(
                    repo=str(_require(site_raw, "repo", site_where)),
                    file=str(_require(site_raw, "file", site_where)),
                )
            )
        groups.append(
            Group(
                name=name,
                says=str(entry.get("says", "")),
                marker_pattern=str(entry.get("marker", DEFAULT_MARKER)),
                sites=sites,
            )
        )
    return groups


# --------------------------------------------------------------------------
# Reading a site
# --------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Strip trailing whitespace and trailing blank lines.

    Used only to CLASSIFY a difference, never to excuse one. A copy that
    declares byte identity and differs by a newline is still drift; the
    normalised digest is what lets the report say which kind of job it is.
    """
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n")


def read_site(site: Site, root: Path, marker_pattern: str) -> None:
    """Fill in digests and marker, or record why not."""
    site.path = root / site.file
    try:
        raw = site.path.read_bytes()
    except OSError as exc:
        site.unreadable = f"cannot read {site.path}: {exc}"
        return
    site.digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        site.unreadable = f"{site.path} is not UTF-8: {exc}"
        return
    site.normalised = hashlib.sha256(_normalise(text).encode("utf-8")).hexdigest()
    try:
        found = re.search(marker_pattern, text)
    except re.error as exc:
        site.unreadable = f"bad marker pattern {marker_pattern!r}: {exc}"
        return
    site.marker = found.group(1) if found and found.groups() else None


def resolve_repo(
    repo: str, roots: list[Path], explicit: dict[str, Path]
) -> Path | None:
    """Where ``owner/name`` is checked out, or ``None``.

    Nothing is fetched, for the same reason the sister probe gives: a probe
    that clones is neither reproducible nor read-only, and it turns a network
    error into something indistinguishable from a repository nobody checked
    out.
    """
    if repo in explicit:
        path = explicit[repo]
        return path if path.is_dir() else None
    name = repo.split("/")[-1]
    for root in roots:
        for candidate in (root / repo, root / name):
            if candidate.is_dir():
                return candidate
    return None


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    severity: str
    group: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "group": self.group,
            "detail": self.detail,
        }


@dataclass
class Unverified:
    code: str
    subject: str
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "subject": self.subject, "detail": self.detail}


@dataclass
class Report:
    manifest: str
    groups_compared: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    unverified: list[Unverified] = field(default_factory=list)
    sites: list[dict] = field(default_factory=list)
    provenance: probe_provenance.Provenance | None = None

    def exit_code(self) -> int:
        # A checkout that moved under the probe outranks everything below it:
        # the digests were read from a tree that no longer exists, so neither a
        # finding nor a green line is entitled to speak.
        if self.provenance is not None and self.provenance.blocking:
            return probe_provenance.EXIT_MOVED
        if self.findings:
            return EXIT_FINDINGS
        if not self.groups_compared:
            return EXIT_NOT_MEASURED
        return EXIT_GREEN

    def as_dict(self) -> dict:
        return {
            "probe": "vendored_copy",
            "manifest": self.manifest,
            "groups_compared": self.groups_compared,
            "findings": [f.as_dict() for f in self.findings],
            "unverified": [u.as_dict() for u in self.unverified],
            "sites": self.sites,
            "provenance": self.provenance.as_dict() if self.provenance else None,
        }


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _compare(report: Report, group: Group, readable: list[Site]) -> None:
    digests = {s.digest for s in readable}
    if len(digests) == 1:
        return

    normalised = {s.normalised for s in readable if s.normalised}
    only_whitespace = len(normalised) == 1
    kind = (
        "the difference is trailing whitespace or blank lines only"
        if only_whitespace
        else "the contents differ in substance"
    )
    listing = ", ".join(f"{s.name} sha256:{s.digest[:12]}" for s in readable)
    report.findings.append(
        Finding(
            code="COPY_DRIFT",
            severity="medium" if only_whitespace else "high",
            group=group.name,
            detail=(
                f"{len(digests)} distinct contents across {len(readable)} copies "
                f"that declare byte identity — {kind}. {listing}. Which copy is "
                "right is a judgement about the code; this probe only says they "
                "disagree"
            ),
        )
    )

    markers = [s.marker for s in readable]
    missing = [s.name for s in readable if s.marker is None]
    if missing:
        report.findings.append(
            Finding(
                code="MARKER_MISSING",
                severity="medium",
                group=group.name,
                detail=(
                    f"no version marker found in {', '.join(missing)}. A copy "
                    "with no marker cannot go stale, but it also cannot tell a "
                    "reader which version it is"
                ),
            )
        )
        return

    if len(set(markers)) == 1:
        report.findings.append(
            Finding(
                code="MARKER_STALE",
                severity="high",
                group=group.name,
                detail=(
                    f"the copies differ and every one of them declares "
                    f"`{markers[0]}`. This is why the drift survives: in each "
                    "repository only one half is visible, and both halves claim "
                    "the same version, so no artefact anywhere disagrees with "
                    "anything. Bumping the marker on the edited side does not "
                    "fix the drift — it makes it visible"
                ),
            )
        )
    else:
        report.findings.append(
            Finding(
                code="MARKER_SPLIT",
                severity="low",
                group=group.name,
                detail=(
                    "the copies differ and so do their markers ("
                    + ", ".join(f"{s.name}={s.marker}" for s in readable)
                    + "). Drift that announces itself: a reader comparing the "
                    "two headers learns the truth immediately"
                ),
            )
        )


def run(
    manifest: Path,
    roots: list[Path] | None = None,
    explicit: dict[str, Path] | None = None,
) -> Report:
    report = Report(manifest=str(manifest))
    groups = load_manifest(manifest)
    roots = roots or []
    explicit = explicit or {}

    for group in groups:
        readable: list[Site] = []
        for site in group.sites:
            root = resolve_repo(site.repo, roots, explicit)
            if root is None:
                report.unverified.append(
                    Unverified(
                        code="SITE_MISSING",
                        subject=site.name,
                        detail=(
                            f"`{site.repo}` is not checked out under any --repos-root "
                            "and has no --repo-path. Nothing is fetched by design; "
                            "this copy was not compared and must not be read as "
                            "agreeing with its siblings"
                        ),
                    )
                )
                continue
            read_site(site, root, group.marker_pattern)
            if site.unreadable:
                report.unverified.append(
                    Unverified(
                        code="SITE_UNREADABLE",
                        subject=site.name,
                        detail=(
                            f"{site.unreadable}. The manifest maps this copy "
                            "explicitly, so the mapping itself may be stale — the "
                            "file may have moved or been removed"
                        ),
                    )
                )
                continue
            readable.append(site)
            report.sites.append(
                {
                    "group": group.name,
                    "repo": site.repo,
                    "file": site.file,
                    "sha256": site.digest,
                    "marker": site.marker,
                }
            )

        if len(readable) < MIN_SITES:
            report.unverified.append(
                Unverified(
                    code="GROUP_UNMEASURED",
                    subject=group.name,
                    detail=(
                        f"only {len(readable)} of {len(group.sites)} copies could be "
                        f"read; {MIN_SITES} are needed to compare. A group compared "
                        "against one file is not clean, it is unmeasured"
                    ),
                )
            )
            continue

        report.groups_compared.append(group.name)
        _compare(report, group, readable)

    return report


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def render(report: Report) -> str:
    lines = [f"vendored-copy probe — manifest {report.manifest}"]
    for site in report.sites:
        lines.append(
            f"  {site['group']}: {site['repo']}:{site['file']} "
            f"sha256:{site['sha256'][:12]} marker={site['marker']}"
        )
    for unverified in report.unverified:
        lines.append(f"  {unverified.code} {unverified.subject}: {unverified.detail}")
    if report.groups_compared and not report.findings:
        lines.append(
            f"  {len(report.groups_compared)} group(s) compared, every copy identical: "
            + ", ".join(report.groups_compared)
        )
    if not report.groups_compared:
        lines.append("  nothing was compared — see the unverified entries above")
    for finding in report.findings:
        lines.append(
            f"  {finding.code} [{finding.severity}] {finding.group}: {finding.detail}"
        )
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest",
        default="vendored.toml",
        help="TOML declaring the copy groups (default: vendored.toml)",
    )
    parser.add_argument(
        "--repos-root",
        action="append",
        default=[],
        help="directory holding checkouts; repeatable",
    )
    parser.add_argument(
        "--repo-path",
        action="append",
        default=[],
        metavar="owner/name=PATH",
        help="explicit checkout for one repository; repeatable",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", default="", help="also write the JSON report here")
    return parser


def _explicit(pairs: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pair in pairs:
        repo, _, path = pair.partition("=")
        if not repo or not path:
            raise ManifestError(f"--repo-path expects owner/name=PATH, got {pair!r}")
        out[repo] = Path(path).resolve()
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = Path(args.manifest).resolve()

    try:
        explicit = _explicit(args.repo_path)
        report = run(
            manifest,
            roots=[Path(r).resolve() for r in args.repos_root],
            explicit=explicit,
        )
    except ManifestError as exc:
        print(f"vendored-copy probe: cannot run — {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    report.provenance = probe_provenance.capture_auditor().recheck()

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
