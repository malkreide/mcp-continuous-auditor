#!/usr/bin/env python3
"""Yank probe — is a known-broken release still installable?

WHY THIS IS NOT ``shipped_probe.py``
------------------------------------
``shipped_probe.py`` already reads PEP 592 yank flags, and it asks a sharp
question about them: *is the version users install right now withdrawn?*
(``RELEASE_YANKED``). That is a question about ONE version — the current one —
and it is answered by the two HTTP requests its ``--metadata-only`` depth
promises. ``nightly-audit.sh`` leans on that promise: the metadata pre-run
exists so the release verdict survives the full gate hanging, and it only
survives because it is cheap.

This asks the inverse question, and it is a different one in kind:

    Does a known-unusable, NOT-yanked release still exist, with a healthy
    successor beside it?

Inverting it changes the shape of the work. It is no longer about the current
version, it is about EVERY version; and it cannot be answered from the project
page alone, because the evidence lives in each release's ``Requires-Dist``. That
is O(versions + dependencies) requests, not two. Folding it into the metadata
depth would break the exact property the pre-run was added for, and folding it
into the full depth would hide a catalogue question behind a venv build. So it
is its own probe, importing ``shipped_probe``'s index primitives rather than
copying them — the same relationship ``shipped_probe`` has with
``transport_boot_probe``.

THE INCIDENT
------------
``zurich-opendata-mcp`` 0.5.1 declared ``mcp[cli]>=1.28.1`` with no upper bound.
``mcp`` 2.0.0 removed ``mcp.server.fastmcp``, so every fresh install of 0.5.1
died on import. 0.6.0 fixed it by moving to ``mcp[cli]>=2.0.0,<3``.

Superseding was not enough. The broken release stayed selectable for anyone a
resolver had constrained away from 0.6.0 — an old lockfile, a colliding pin, a
``==0.5.1`` in somebody's Dockerfile. It had to be yanked, and it was.

TWO DETAILS A NAIVE PROBE MISSES
--------------------------------
1. ALL SIX predecessors were affected — 0.2.0, 0.3.0, 0.3.3, 0.4.0, 0.5.0 and
   0.5.1 each carried an uncapped ``mcp`` range. A probe that checked only
   ``latest-1`` would have found one of six and reported the catalogue clean.
   So this walks every version, and groups the answer by the dependency
   boundary rather than by release, because that is the shape of the fix.
2. A yank is not a deletion. After the yank, ``pip install
   'zurich-opendata-mcp==0.5.1'`` still resolves, with a warning — that is PEP
   592 working as designed, so existing lockfiles do not break. The finding
   below therefore never says "delete"; it says "yank", and it says what a yank
   does and does not do.

WHAT THE FINDING IS ALLOWED TO CLAIM
------------------------------------
"Known-unusable" is a strong word and metadata alone rarely earns it. Four
conditions must ALL hold before ``UNYANKED_BROKEN_RELEASE`` is raised for a
version V and a dependency D:

  1. V is not yanked and is not a pre-release — a withdrawn or unreleased
     version is not the problem being reported.
  2. V's requirement on D has NO upper bound, so a resolver may take D
     arbitrarily far past whatever V was built against.
  3. The healthy successor R — the newest non-yanked release — declares D too,
     and R's requirement EXCLUDES V's own lower bound. This is the corroborating
     step, and it is the one that turns a risk into a finding: the maintainer's
     own later release has already declared that the series V floors on is not
     supported. Without it, every uncapped dependency in the world is a finding
     and the gate gets muted.
  4. The newest non-pre-release of D that V actually admits is in a HIGHER major
     series than V's lower bound. Without this the break is theoretical — if D
     never shipped past V's series, V still resolves to something it was built
     against, and there is nothing to yank.

Failing any one of them, this stays quiet. That is deliberate: a yank is a
public, irreversible-in-practice statement about someone's release, and a probe
that cries wolf about it will be turned off.

Both the operations here are read-only HTTP GETs against a PEP 503 index. The
Simple API is primary — it is the surface pip resolves against, and PEP 658's
``core-metadata`` means each release's ``Requires-Dist`` can be read WITHOUT
downloading the wheel. PyPI's per-version JSON API is consulted only as a
fallback and only when the index IS PyPI, which is the same precedence
``shipped_probe.reconcile`` documents at length.

THIS PROBE DOES NOT YANK ANYTHING
---------------------------------
Deliberately, and not as an oversight. Yanking needs a PyPI API token with
upload scope for the project, it changes what every resolver on the internet
sees, and "was this release actually unusable" is a judgement the maintainer
owns. An auditor that holds that credential is a much larger blast radius than
one that writes a sentence. So the output is a recommendation with the evidence
attached, and the ``pip``/``twine`` side of it is left to a human. There is no
flag to make it act, and adding one would be a change of category, not a
feature.

EXIT CODES
  0    no unyanked known-broken release, and every yank carries a reason
  2    FINDING
  127  the HARNESS could not run (index unreachable, or the healthy successor's
       own metadata could not be read — a comparison that did not happen is
       never a pass)

Usage:
  python scripts/yank_probe.py --target ../zurich-opendata-mcp
  python scripts/yank_probe.py --dist zurich-opendata-mcp --format json
  python scripts/yank_probe.py --dist foo-mcp --index-url https://pypi.example.com/simple
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402
import shipped_probe as sp

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_CANNOT_RUN = 127

DEFAULT_TIMEOUT = 30.0
# Newest-first, so a project with hundreds of releases still audits the part
# anybody could plausibly be pinned to. Truncation is REPORTED, never silent —
# a capped run that reads as "catalogue clean" is the failure this whole file
# is about.
DEFAULT_MAX_VERSIONS = 60

PYPI_VERSION_JSON = "https://pypi.org/pypi/{dist}/{version}/json"

# PEP 440 comparison clauses. Ordered longest-first so `<=` never parses as `<`.
_CLAUSE = re.compile(r"(===|==|!=|<=|>=|~=|<|>)\s*([^,\s]+)")

# PEP 508, reduced to the parts that decide anything here: the name, the extras
# that come with it, the specifier set, and whether a marker is present at all.
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?:\[(?P<extras>[^\]]*)\])?\s*"
    r"(?:\((?P<paren>[^)]*)\)|(?P<bare>[^;]*))"
    r"(?:;(?P<marker>.*))?$"
)

# An upper bound is anything that stops a resolver walking forwards. `~=2.1` and
# `==2.*` are bounds even though neither spells `<`; missing them would call a
# correctly-pinned release uncapped.
_BOUNDS_ABOVE = frozenset({"<", "<=", "==", "===", "~="})
# What a lower bound can be spelled as. `>` and `>=` are the usual ones; `==`
# and `~=` pin a floor as a side effect of pinning everything else.
_BOUNDS_BELOW = frozenset({">", ">=", "==", "===", "~="})


# --------------------------------------------------------------------------
# PEP 440, the subset that decides something here
# --------------------------------------------------------------------------


def _release(version: str) -> tuple[int, ...] | None:
    """The release segment as a tuple, or None when it does not parse.

    ``shipped_probe.release_key`` by another name, and deliberately the same
    limitation: epochs, local versions and the ordering of ``.postN`` against
    ``.devN`` are not modelled. Nothing below needs them, and a comparison this
    file cannot make is reported as undecidable rather than guessed at.
    """
    return sp.release_key(version)


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare two release tuples with PEP 440's zero-padding.

    Padding is the whole point: ``1.28`` and ``1.28.0`` are the SAME version
    under PEP 440, and raw tuple comparison would order the shorter one first.
    That is a boundary error exactly where the boundaries matter — a floor of
    ``>=1.28`` against a candidate ``1.28``.
    """
    width = max(len(a), len(b))
    left = a + (0,) * (width - len(a))
    right = b + (0,) * (width - len(b))
    return (left > right) - (left < right)


def _matches(op: str, candidate: tuple[int, ...], spec: str) -> bool | None:
    """Does ``candidate`` satisfy one clause? None when undecidable."""
    wildcard = spec.endswith(".*")
    base = spec[:-2] if wildcard else spec
    target = _release(base)
    if target is None:
        return None

    if op in ("==", "==="):
        if wildcard:
            return candidate[: len(target)] == target[: len(candidate)] and (
                len(candidate) >= len(target)
            )
        return _cmp(candidate, target) == 0
    if op == "!=":
        eq = _matches("==", candidate, spec)
        return None if eq is None else not eq
    if op == "~=":
        # `~=X.Y.Z` is `>=X.Y.Z, ==X.Y.*`: compatible within the series one
        # component up. `~=X` alone is not valid PEP 440 and is left undecidable
        # rather than read as `>=X`.
        if len(target) < 2:
            return None
        series = target[:-1]
        return _cmp(candidate, target) >= 0 and candidate[: len(series)] == series
    if op == "<":
        return _cmp(candidate, target) < 0
    if op == "<=":
        return _cmp(candidate, target) <= 0
    if op == ">":
        return _cmp(candidate, target) > 0
    if op == ">=":
        return _cmp(candidate, target) >= 0
    return None


@dataclass(frozen=True)
class Requirement:
    """One ``Requires-Dist`` line, reduced to what this probe reads."""

    name: str
    extras: str = ""
    clauses: tuple[tuple[str, str], ...] = ()
    marker: str = ""
    raw: str = ""

    @property
    def key(self) -> str:
        """PEP 503 normalised name — the only spelling an index is required to
        serve, and therefore the only safe dictionary key."""
        return re.sub(r"[-_.]+", "-", self.name).lower()

    @property
    def conditional(self) -> bool:
        """Does an environment marker gate this requirement?

        Anything gated is skipped wholesale, extras included. ``extra == 'dev'``
        is not installed by a plain ``pip install`` and a broken dev dependency
        breaks nobody's server; ``python_version < "3.12"`` is a requirement
        that holds for some installs and not others, and deciding which without
        an environment to evaluate against would be a guess.
        """
        return bool(self.marker.strip())

    def bounded_above(self) -> bool:
        return any(op in _BOUNDS_ABOVE for op, _ in self.clauses)

    def floor(self) -> str | None:
        """The highest lower bound this requirement states, or None."""
        candidates = [v for op, v in self.clauses if op in _BOUNDS_BELOW]
        ranked = [(v, _release(v.rstrip(".*"))) for v in candidates]
        usable = [(v, key) for v, key in ranked if key is not None]
        if not usable:
            return None
        return max(usable, key=lambda pair: pair[1])[0]

    def admits(self, version: str) -> bool | None:
        """Does this requirement accept ``version``? None when undecidable."""
        candidate = _release(version)
        if candidate is None:
            return None
        if not self.clauses:
            return True
        for op, spec in self.clauses:
            verdict = _matches(op, candidate, spec)
            if verdict is None:
                return None
            if not verdict:
                return False
        return True


def parse_requirement(line: str) -> Requirement | None:
    """One ``Requires-Dist`` value, or None when it is not one we can read."""
    text = (line or "").strip()
    if not text:
        return None
    match = _REQUIREMENT.match(text)
    if not match:
        return None
    body = match.group("paren")
    if body is None:
        body = match.group("bare") or ""
    # A URL requirement (`name @ https://…`) states no version range at all, so
    # there is no bound to reason about and no finding to make.
    if "@" in body:
        return None
    clauses = tuple((op, ver.strip()) for op, ver in _CLAUSE.findall(body))
    return Requirement(
        name=match.group("name"),
        extras=(match.group("extras") or "").strip(),
        clauses=clauses,
        marker=(match.group("marker") or "").strip(),
        raw=text,
    )


def parse_metadata(text: str) -> list[Requirement]:
    """``Requires-Dist`` lines out of a core-metadata document.

    RFC 822 folding has to be handled, and the order of the two tests below is
    the whole reason this function exists rather than a ``startswith`` loop.
    PyPI inlines the full licence text as a FOLDED ``License:`` header, and the
    blank lines inside an MIT licence arrive as continuation lines that are
    whitespace-only. Testing "is this line blank" before "is this line a
    continuation" ends the header block on the second line of the licence — and
    every ``Requires-Dist`` sits below it. Measured against
    ``zurich-opendata-mcp`` 0.6.0: that ordering reads six dependencies as zero,
    which this probe would then report as a clean catalogue.

    So: a line starting with space or tab continues the header above it, and
    ONLY a genuinely empty line ends the headers and starts the description.
    """
    out: list[Requirement] = []
    pending: str | None = None  # a Requires-Dist value still being folded
    inside_header = False  # any header, so foreign continuations are skipped

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        parsed = parse_requirement(pending)
        if parsed is not None:
            out.append(parsed)
        pending = None

    for line in text.splitlines():
        if line[:1] in (" ", "\t") and inside_header:
            # A continuation. It belongs to Requires-Dist only when that is the
            # header we are inside; a licence body line that happens to contain
            # a colon must not be read as a new header.
            if pending is not None:
                pending += " " + line.strip()
            continue
        if not line:
            break  # the empty line ends the headers; the rest is the description
        flush()
        inside_header = True
        name, sep, value = line.partition(":")
        if not sep:
            continue
        if name.strip().lower() == "requires-dist":
            pending = value.strip()
    flush()
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    detail: str
    severity: str = "high"  # high | medium | low
    versions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "severity": self.severity,
            "versions": list(self.versions),
        }


@dataclass
class ReleaseView:
    """One release of the audited distribution, as the index describes it."""

    version: str
    yanked: bool = False
    yank_reason: str = ""
    # None = the metadata was not readable. Distinct from [] (readable, and the
    # release genuinely declares no dependencies) — one is a gap in the audit,
    # the other is an answer.
    requires: list[Requirement] | None = None
    metadata_detail: str = ""

    def declared(self) -> dict[str, Requirement]:
        """Unconditional requirements, by normalised name."""
        return {r.key: r for r in (self.requires or []) if not r.conditional}


@dataclass
class Report:
    dist: str
    index_url: str = sp.DEFAULT_INDEX
    index_status: str = "ok"  # ok | unreachable | not_published | skipped
    index_detail: str = ""
    releases: list[ReleaseView] = field(default_factory=list)
    reference: str | None = None  # the healthy successor everything is held against
    # Newest non-pre-release of each dependency, as the index serves it. None
    # when the dependency's own project page could not be read.
    dependency_latest: dict[str, str | None] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    truncated: int = 0
    findings: list[Finding] = field(default_factory=list)
    harness_error: str = ""
    # The checkout the distribution name was read from. Not decisive — every
    # finding below is about the index, and withdrawing one because somebody
    # committed locally would be superstition. It is recorded so the report
    # still names the state it was launched from.
    provenance: probe_provenance.Provenance | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 2,  # +provenance
            "probe": "yank",
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "dist": self.dist,
            "index_url": self.index_url,
            "index_status": self.index_status,
            "index_detail": self.index_detail,
            "reference": self.reference,
            "releases": [
                {
                    "version": r.version,
                    "yanked": r.yanked,
                    "yank_reason": r.yank_reason,
                    "metadata": "ok" if r.requires is not None else "unreadable",
                    "metadata_detail": r.metadata_detail,
                }
                for r in self.releases
            ],
            "dependency_latest": dict(sorted(self.dependency_latest.items())),
            "metadata_unreadable": sorted(self.unreadable),
            "versions_not_audited": self.truncated,
            "harness_error": self.harness_error,
            "findings": [f.as_dict() for f in self.findings],
            "exit_code": self.exit_code(),
        }

    def exit_code(self) -> int:
        if self.harness_error:
            return EXIT_CANNOT_RUN
        return EXIT_FINDINGS if self.findings else EXIT_GREEN


# --------------------------------------------------------------------------
# Network seams — everything that decides anything lives outside these
# --------------------------------------------------------------------------


def _fetch_text(
    url: str, timeout: float, accept: str | None = None
) -> tuple[str | None, str]:
    """(body, detail). Never raises; an empty detail means it worked."""
    request = urllib.request.Request(url)
    if accept:
        request.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace"), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable: {exc}"


def fetch_project(
    dist: str, index_url: str, timeout: float
) -> tuple[dict[str, Any] | None, str, str]:
    """The project page, in PEP 691 shape. (payload, status, detail).

    ``shipped_probe._get`` does the content negotiation and the HTML fallback
    already, and reimplementing it here would be a second place for a private
    index's quirks to be handled differently.
    """
    url = sp.simple_url(dist, index_url)
    payload, status, detail = sp._get(url, timeout, sp.SIMPLE_ACCEPT)
    if payload is not None and not isinstance(payload, dict):
        return None, "unreachable", "the index answered a non-object"
    return payload, status, detail


def fetch_core_metadata(
    entry: dict[str, Any], timeout: float
) -> tuple[str | None, str]:
    """PEP 658: the ``.metadata`` sidecar next to a distribution file.

    This is what makes walking every release affordable — the whole point of
    PEP 658 is that a resolver can read ``Requires-Dist`` without fetching the
    wheel. A 30 KB wheel per version would be a download; a 2 KB header block is
    a request. The index must advertise it; an index that does not is handled by
    the caller's fallback rather than by guessing the URL and hoping.
    """
    advertised = entry.get("core-metadata")
    if advertised in (None, False):
        # PEP 714 renamed it; the old spelling is still served for old clients.
        advertised = entry.get("data-dist-info-metadata")
    if advertised in (None, False):
        return None, "the index does not advertise PEP 658 core metadata for this file"
    url = str(entry.get("url") or "")
    if not url:
        return None, "the index gave no URL for this file"
    return _fetch_text(url + ".metadata", timeout)


def fetch_pypi_requires(
    dist: str, version: str, timeout: float
) -> tuple[list[Requirement] | None, str]:
    """PyPI's per-version JSON, used ONLY as the fallback and ONLY on PyPI.

    Same precedence as ``shipped_probe.reconcile``: the Simple API is what pip
    resolves against and therefore what a claim about pip should be made from.
    This exists so a release uploaded before PyPI backfilled PEP 658 metadata is
    still audited instead of silently dropped.
    """
    body, detail = _fetch_text(
        PYPI_VERSION_JSON.format(dist=quote(dist), version=quote(version)), timeout
    )
    if body is None:
        return None, f"the JSON API fallback also failed ({detail})"
    try:
        payload = json.loads(body)
    except ValueError as exc:
        return None, f"the JSON API answered unparseable content ({exc})"
    raw = (payload.get("info") or {}).get("requires_dist")
    if raw is None:
        # A real answer, not a failure: the release declares no dependencies.
        return [], ""
    if not isinstance(raw, list):
        return None, "the JSON API's requires_dist was not a list"
    parsed = [parse_requirement(str(line)) for line in raw]
    return [r for r in parsed if r is not None], ""


def fetch_dependency_versions(
    name: str, index_url: str, timeout: float
) -> list[str] | None:
    """Every version of a dependency the index serves, or None if unreadable."""
    payload, status, _ = fetch_project(name, index_url, timeout)
    if payload is None or status != "ok":
        return None
    declared = payload.get("versions")
    if isinstance(declared, list):
        return [str(v) for v in declared]
    versions: set[str] = set()
    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        found = sp.version_from_filename(str(entry.get("filename", "")), name)
        if found:
            versions.add(found)
    return sorted(versions)


# --------------------------------------------------------------------------
# Assembling the view
# --------------------------------------------------------------------------


def collect_releases(payload: dict[str, Any], dist: str) -> list[ReleaseView]:
    """Group the project page's files into releases, with yank state.

    Yank is per FILE in PEP 592, so a version counts as yanked only when it has
    files and every one of them is yanked — the same rule ``shipped_probe``
    applies, for the same reason: a version with one live wheel left is still
    installable, and calling it withdrawn would invert this probe's finding.
    """
    per_version: dict[str, list[str | None]] = {}
    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        version = sp.version_from_filename(str(entry.get("filename", "")), dist)
        if not version:
            continue
        per_version.setdefault(version, []).append(
            sp._yank_reason(entry.get("yanked", False))
        )

    declared = payload.get("versions")
    names = (
        {str(v) for v in declared} if isinstance(declared, list) else set(per_version)
    )

    out: list[ReleaseView] = []
    for version in names:
        reasons = per_version.get(version) or []
        yanked = bool(reasons) and all(r is not None for r in reasons)
        out.append(
            ReleaseView(
                version=version,
                yanked=yanked,
                yank_reason=next((r for r in reasons if r), "") if yanked else "",
            )
        )
    return sorted(out, key=lambda r: _release(r.version) or ())


def pick_metadata_file(
    payload: dict[str, Any], dist: str, version: str
) -> dict[str, Any] | None:
    """The file to read ``Requires-Dist`` from — a wheel where there is one.

    Wheels carry built metadata; an sdist's is generated at build time and PyPI
    does not always have a PEP 658 sidecar for one. Preferring the wheel turns a
    fallback into a first-choice hit for essentially every release here.
    """
    wheel: dict[str, Any] | None = None
    other: dict[str, Any] | None = None
    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename", ""))
        if sp.version_from_filename(filename, dist) != version:
            continue
        if filename.endswith(".whl") and wheel is None:
            wheel = entry
        elif other is None:
            other = entry
    return wheel or other


def load_requirements(
    payload: dict[str, Any],
    report: Report,
    release: ReleaseView,
    timeout: float,
) -> None:
    """Fill ``release.requires``, PEP 658 first and the JSON API second."""
    entry = pick_metadata_file(payload, report.dist, release.version)
    if entry is not None:
        body, detail = fetch_core_metadata(entry, timeout)
        if body is not None:
            release.requires = parse_metadata(body)
            return
        release.metadata_detail = detail
    else:
        release.metadata_detail = "the index lists no files for this version"

    if sp.is_pypi(report.index_url):
        parsed, detail = fetch_pypi_requires(report.dist, release.version, timeout)
        if parsed is not None:
            release.requires = parsed
            release.metadata_detail = ""
            return
        release.metadata_detail = f"{release.metadata_detail}; {detail}".strip("; ")
    else:
        release.metadata_detail += (
            f" — and {report.index_url} is not PyPI, so there was no per-version "
            "JSON API to fall back to"
        )


def choose_reference(releases: list[ReleaseView]) -> str | None:
    """The healthy successor: the newest release that is neither yanked nor a
    pre-release. Everything below is held against exactly this one."""
    healthy = [
        r
        for r in releases
        if not r.yanked and not sp.is_prerelease(r.version) and _release(r.version)
    ]
    if not healthy:
        return None
    return max(healthy, key=lambda r: _release(r.version) or ()).version


# --------------------------------------------------------------------------
# The verdict — pure, from an already-populated report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Boundary:
    """One dependency's major-series crossing, and who is exposed to it."""

    dependency: str
    from_major: int
    to_major: int
    newest: str
    reference_spec: str
    affected: tuple[tuple[str, str], ...] = ()  # (version, that version's raw spec)


def find_boundaries(report: Report) -> list[Boundary]:
    """Every (dependency, major crossing) an unyanked release is exposed to.

    Grouped by BOUNDARY rather than by release on purpose. Six versions sharing
    one uncapped range is one mistake with six instances, and one operational
    decision — the maintainer yanks a list. Reporting it as six findings would
    bury the thing that matters, which is that the list has six entries and not
    one.
    """
    if report.reference is None:
        return []
    reference = next(
        (r for r in report.releases if r.version == report.reference), None
    )
    if reference is None or reference.requires is None:
        return []
    reference_declared = reference.declared()
    reference_key = _release(report.reference)

    grouped: dict[tuple[str, int, int], list[tuple[str, str]]] = {}
    context: dict[tuple[str, int, int], tuple[str, str]] = {}

    for release in report.releases:
        key = _release(release.version)
        # (1) not yanked, not a pre-release, strictly older than the successor.
        if (
            release.yanked
            or sp.is_prerelease(release.version)
            or release.requires is None
        ):
            continue
        if key is None or reference_key is None or _cmp(key, reference_key) >= 0:
            continue

        for name, requirement in release.declared().items():
            # (2) no upper bound — the resolver may walk forwards without limit.
            if requirement.bounded_above():
                continue
            floor = requirement.floor()
            if floor is None:
                continue
            floor_key = _release(floor.rstrip(".*"))
            if floor_key is None:
                continue

            # (3) the successor's own requirement EXCLUDES that floor. This is
            # the maintainer corroborating the boundary; without it there is a
            # risk here but not a finding.
            reference_requirement = reference_declared.get(name)
            if reference_requirement is None:
                continue
            if reference_requirement.admits(floor) is not False:
                continue

            # (4) something past the boundary is actually published AND this
            # release's own requirement admits it, so a resolver would really
            # select it. A break nobody can resolve into is not one.
            available = report.dependency_latest.get(name)
            if not available or requirement.admits(available) is not True:
                continue
            newest_key = _release(available)
            if newest_key is None or newest_key[0] <= floor_key[0]:
                continue

            bucket = (name, floor_key[0], newest_key[0])
            grouped.setdefault(bucket, []).append((release.version, requirement.raw))
            context.setdefault(bucket, (available, reference_requirement.raw))

    out: list[Boundary] = []
    for (name, from_major, to_major), affected in grouped.items():
        newest, reference_spec = context[(name, from_major, to_major)]
        out.append(
            Boundary(
                dependency=name,
                from_major=from_major,
                to_major=to_major,
                newest=newest,
                reference_spec=reference_spec,
                affected=tuple(
                    sorted(affected, key=lambda pair: _release(pair[0]) or ())
                ),
            )
        )
    return sorted(out, key=lambda b: (b.dependency, b.from_major))


def newest_release(versions: list[str]) -> str | None:
    """The newest version a plain ``pip install`` would consider.

    Pre-releases are excluded because pip excludes them unless asked, and this
    probe's entire claim is about what an unadorned install does. Measured: the
    Simple API served ``mcp`` 2.0.0a1 through 2.0.0rc1 alongside 2.0.0, and
    taking the list's last entry would have named a release candidate as the
    thing users resolve to.
    """
    usable = [
        (v, _release(v))
        for v in versions
        if not sp.is_prerelease(v) and _release(v) is not None
    ]
    if not usable:
        return None
    return max(usable, key=lambda pair: pair[1] or ())[0]


def build_findings(report: Report) -> list[Finding]:
    """The whole verdict. Pure — every network answer is already in ``report``."""
    out: list[Finding] = []

    # 1. UNYANKED_BROKEN_RELEASE — the reason this probe exists.
    for boundary in find_boundaries(report):
        versions = tuple(v for v, _ in boundary.affected)
        ranges = ", ".join(f"{v} ({spec})" for v, spec in boundary.affected)
        out.append(
            Finding(
                "UNYANKED_BROKEN_RELEASE",
                f"{len(versions)} release(s) of {report.dist} declare {boundary.dependency} "
                f"with no upper bound and are NOT yanked: {ranges}. The newest "
                f"{boundary.dependency} on {report.index_url} is {boundary.newest}, so a "
                f"fresh install of any of them resolves across the "
                f"{boundary.from_major}.x -> {boundary.to_major}.x boundary. That boundary "
                f"is not a guess: {report.dist} {report.reference} — the newest healthy "
                f"release — declares {boundary.reference_spec}, which excludes the series "
                "every one of those releases floors on, so the maintainer has already "
                "established the crossing was breaking. Superseding them is not enough: "
                "they stay selectable for any resolver constrained away from "
                f"{report.reference} by an old lockfile or a colliding pin. RECOMMENDED: "
                f"yank {', '.join(versions)} with a reason. A yank is not a deletion — "
                "PEP 592 keeps them resolvable for an explicit pin, with a warning, so "
                "existing lockfiles do not break. Do NOT delete them. This probe does "
                "not and will not perform the yank: it needs an upload-scoped token and "
                "it is a maintainer's call.",
                severity="high",
                versions=versions,
            )
        )

    # 2. YANK_REASON_MISSING — lower, and separate on purpose.
    #
    # The reason travels through the Simple API (PEP 592) and pip prints it
    # verbatim. It is the ONLY thing seen by the one person this still reaches:
    # somebody an old lockfile has dropped onto the withdrawn version, who gets
    # "Reason for being yanked: <none given>" and no way to tell a security
    # withdrawal from a bad build. Cheap to fix, and not in the same class as a
    # broken release still being installable — hence low, and reported second.
    reasonless = tuple(
        r.version for r in report.releases if r.yanked and not r.yank_reason.strip()
    )
    if reasonless:
        out.append(
            Finding(
                "YANK_REASON_MISSING",
                f"{len(reasonless)} yanked release(s) carry no reason: "
                f'{", ".join(reasonless)}. pip shows this as "Reason for being yanked: '
                '<none given>" to anyone an old lockfile drops onto them, which is the '
                "only audience a yanked release still has. Re-yank with a one-line "
                "reason naming the defect and the release that fixes it.",
                severity="low",
                versions=reasonless,
            )
        )

    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def run(
    dist: str,
    index_url: str = sp.DEFAULT_INDEX,
    timeout: float = DEFAULT_TIMEOUT,
    max_versions: int = DEFAULT_MAX_VERSIONS,
) -> Report:
    report = Report(dist=dist, index_url=index_url)

    payload, status, detail = fetch_project(dist, index_url, timeout)
    report.index_status, report.index_detail = status, detail
    if payload is None:
        if status == "not_published":
            # Not a defect this probe can name: there is no catalogue to audit.
            # `shipped_probe` already reports NOT_ON_INDEX for it, and saying it
            # twice would make one problem look like two.
            report.index_detail = f"{dist} is not on {index_url} — nothing to audit"
            return report
        report.harness_error = detail or f"{index_url} could not be read"
        return report

    releases = collect_releases(payload, dist)
    if len(releases) > max_versions:
        # Newest-first truncation, said out loud in the report AND on stderr.
        report.truncated = len(releases) - max_versions
        releases = releases[-max_versions:]
    report.releases = releases

    report.reference = choose_reference(releases)
    if report.reference is None:
        report.index_detail = (
            f"{dist} has no healthy release — every version on {index_url} is "
            "yanked or a pre-release. There is no successor to hold the older "
            "releases against, so no claim is made about them"
        )
        return report

    for release in releases:
        load_requirements(payload, report, release, timeout)
        if release.requires is None:
            report.unreadable.append(release.version)

    reference = next(r for r in releases if r.version == report.reference)
    if reference.requires is None:
        # The one unreadable release that stops the audit rather than narrowing
        # it: every comparison below is against this one.
        report.harness_error = (
            f"the healthy successor {report.reference} has no readable metadata "
            f"({reference.metadata_detail}) — every comparison this probe makes is "
            "against it, so nothing can be concluded. Not reported as clean"
        )
        return report

    # Dependency version lists, once per distinct dependency rather than once
    # per (release, dependency) — the same dependency appears in every release.
    # Only dependencies that could possibly produce a finding are fetched: an
    # uncapped range in some unyanked release, on a name the healthy successor
    # also declares. Everything else is a request that cannot change the answer.
    #
    # What is recorded is the index's newest non-pre-release, held free of any
    # one release's specifier. Each release's own requirement is then applied to
    # it in `find_boundaries`, so a version whose floor sits ABOVE that newest
    # release is not credited with resolving to it.
    wanted: set[str] = set()
    for release in releases:
        if release.yanked or release.requires is None:
            continue
        for name, requirement in release.declared().items():
            if not requirement.bounded_above() and name in reference.declared():
                wanted.add(name)
    for name in sorted(wanted):
        versions = fetch_dependency_versions(name, index_url, timeout)
        report.dependency_latest[name] = newest_release(versions or [])

    report.findings = build_findings(report)
    return report


def render(report: Report) -> str:
    lines = [f"yank probe — {report.dist} on {report.index_url}"]
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
    if report.harness_error:
        lines.append(f"  HARNESS: {report.harness_error}")
        return "\n".join(lines)
    if report.index_detail:
        lines.append(f"  note: {report.index_detail}")
    lines.append(
        f"  {len(report.releases)} release(s), "
        f"{sum(1 for r in report.releases if r.yanked)} yanked, "
        f"healthy successor: {report.reference or 'none'}"
    )
    for name, newest in sorted(report.dependency_latest.items()):
        lines.append(f"  dependency {name}: index serves {newest or 'unknown'}")
    if report.truncated:
        lines.append(
            f"  NOT AUDITED: the {report.truncated} oldest release(s) — raise "
            "--max-versions to include them. They are not being reported as clean"
        )
    if report.unreadable:
        lines.append(
            "  metadata unreadable (not audited, not clean): "
            + ", ".join(sorted(report.unreadable))
        )
    if not report.findings:
        lines.append("  no unyanked known-broken release; every yank carries a reason")
    for finding in report.findings:
        lines.append(f"  {finding.code} [{finding.severity}] {finding.detail}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, as its own function so a test can assert on it.

    What the test asserts is that no option here performs a yank. That is a
    property worth pinning rather than trusting to review: the difference
    between this probe and a credential-holding one is exactly one flag, and
    nothing else in the file would fail if somebody added it.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dist",
        default="",
        help="distribution name (default: [project] name in --target)",
    )
    parser.add_argument("--target", default=".", help="path to the target checkout")
    parser.add_argument(
        "--index-url",
        default=sp.DEFAULT_INDEX,
        help="PEP 503 index, as pip --index-url takes it",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--max-versions",
        type=int,
        default=DEFAULT_MAX_VERSIONS,
        help="audit at most this many releases, newest first",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", default="", help="also write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    dist = args.dist
    if not dist:
        try:
            dist = str(sp.read_project(Path(args.target)).get("name") or "")
        except OSError as exc:
            print(
                f"could not read {args.target}/pyproject.toml: {exc}", file=sys.stderr
            )
            return EXIT_CANNOT_RUN
    if not dist:
        print("no distribution name — pass --dist", file=sys.stderr)
        return EXIT_CANNOT_RUN

    prov = probe_provenance.capture(Path(args.target), decisive=False)
    report = run(dist, args.index_url, args.timeout, args.max_versions)
    report.provenance = prov.recheck()

    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8"
        )
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
