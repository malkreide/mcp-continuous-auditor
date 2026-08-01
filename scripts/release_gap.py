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

3. ``RELEASE_YANKED`` — the release this repository considers current is on the
   index and *withdrawn*. Everything else here answers "did it get published";
   this one answers "is what got published still being handed out". A yanked
   release looks identical to a healthy one from every other check: the version
   exists, the tag matches, the workflow is green — and ``pip install`` quietly
   serves something older.

4. ``UNTAGGED_VERSION`` — ``pyproject.toml`` was bumped but no tag matches it.
   The usual, benign state of a prepared release; a finding only once it ages.

5. ``CHANGELOG_UNRELEASED`` — a ``[Unreleased]`` section with entries in it.
   Weakest signal, and deliberately last: it is prose, and prose lags.

WHICH INDEX API IS BELIEVED, AND WHY
------------------------------------
PyPI exposes the same distribution through two APIs, and they are not one
source with two spellings — they are two caches:

* the **Simple API** (``/simple/{dist}/``, PEP 503/691/700). This is the one
  ``pip`` and ``uv`` actually read, so it is the one that decides what a user
  gets. It carries a per-file ``yanked`` flag (PEP 592); the JSON API's
  equivalent has been observed lagging behind it.
* the **JSON API** (``/pypi/{dist}/json``). Convenient — ``info.version`` hands
  you the latest release with no work — and it is the only reason this script
  ever used it.

Measured against ``zurich-opendata-mcp`` on 2026-07-31, minutes after the
operations in question, the two disagreed twice: six freshly yanked releases
still read ``yanked: false`` on the JSON API while the Simple API had them all
as yanked, and ~90 s after ``0.7.0`` was published the JSON API still said
``0.6.0`` while the Simple API already had ``0.7.0``. Re-measured on
2026-08-01 both had converged, so the divergence is a propagation window and
not a permanent property of either API. That is precisely why it is dangerous:
it is invisible except in the minutes right after a release or a yank — the
minutes in which somebody is most likely to be running this script.

So: **the Simple API is the primary source, the JSON API is a fallback**, and
where the two disagree the answer is reported as UNCONFIRMED rather than
picked. An auditor that raises an alarm because one of PyPI's caches is 90 s
behind the other gets muted, and a muted auditor catches nothing — the same
reasoning that keeps recall floors at half the observed count. Being loudly
unsure is a supported outcome here; guessing is not.

FOUR DELIBERATE DECISIONS
-------------------------
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

4. **Two disagreeing indexes are not a finding, and not a pass either.**
   ``UNCONFIRMED`` is its own outcome: loud in the report, and it does not turn
   the run red. Same shape as the boot gate's ``not-selected`` — the evidence
   supports neither statement, and inventing one is how a check earns its
   reputation for crying wolf. A real finding still outranks it: a tag ahead of
   *both* readings is a publish gap no matter which cache you believe, and it
   is reported as one.

Version comparison is deliberately narrow: release segments only
(``1.2.3`` → ``(1, 2, 3)``), pre-release and local segments ignored for
ordering. Full PEP 440 would mean vendoring ``packaging`` into a stdlib-only
tool; the portfolio publishes plain release versions, and anything unparseable
is reported as "differs" instead of being silently ordered wrong.

Exit codes:
  0  no findings (including UNCONFIRMED — read the report, not just the code)
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
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomllib landed in 3.11
    tomllib = None  # type: ignore[assignment]

PYPI_JSON = "https://pypi.org/pypi/{dist}/json"
PYPI_SIMPLE = "https://pypi.org/simple"
# PEP 691 first, HTML second, and the HTML is not a formality: the JSON flavour
# is OPTIONAL, and the only response format a PEP 503 index is required to serve
# is HTML. PyPI content-negotiates to JSON; a devpi, an Artifactory or a plain
# directory listing answers HTML, and refusing to read it would mean refusing to
# audit every private index. Both are parsed into the same shape below.
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json, text/html;q=0.5, */*;q=0.1"

TAG = re.compile(r"^v?(\d+(?:\.\d+)*.*)$")
CHANGELOG_UNRELEASED = re.compile(r"^##\s*\[?Unreleased\]?", re.IGNORECASE)
CHANGELOG_HEADING = re.compile(r"^##\s")
# `fix:`, `feat(scope)!:`, `chore(deps):` — the prefix, not the scope.
CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?:")

# Commit types whose delay is felt by users. Everything else is housekeeping.
USER_FACING = frozenset({"fix", "feat", "perf", "revert"})

# A release segment followed by a pre-release marker. The Simple API's `versions`
# list includes pre-releases (measured: `pydantic` served `2.14.0a1` there while
# the JSON API's `info.version` said `2.13.4`), so taking the last entry as "the
# latest release" would report an alpha as what users install. `release_key`
# cannot help — it drops everything after the release segment, so `2.14.0a1` and
# `2.14.0` order identically. `.postN` is deliberately NOT a pre-release.
PRERELEASE = re.compile(r"^\s*v?\d+(?:\.\d+)*[._-]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)", re.I)

ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".whl", ".egg")


@dataclass
class Finding:
    code: str
    detail: str
    severity: str  # high | medium | low


@dataclass
class IndexView:
    """One index API's answer about one distribution.

    Both APIs are reduced to this shape so the reconciliation below compares
    like with like instead of comparing a parsed JSON blob against a file list.
    """

    source: str  # simple | json
    status: str = "ok"  # ok | unreachable | not_published
    detail: str = ""
    # Newest non-pre-release version present at all, and the newest one that is
    # also not yanked. They differ exactly when the newest release was withdrawn.
    latest: str | None = None
    latest_installable: str | None = None
    versions: list[str] = field(default_factory=list)
    # version -> yank reason ("" when the index gave no reason). Absent = healthy.
    yanked: dict[str, str] = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return self.status == "ok"

    def candidates(self) -> set[str]:
        """The versions this view would accept as "the latest release".

        Two, not one, and the tolerance is deliberate: whether the JSON API's
        ``info.version`` skips a yanked newest release is not something this
        repository has measured. Accepting either reading keeps that unmeasured
        detail from manufacturing an UNCONFIRMED on every package whose newest
        release happens to be withdrawn.
        """
        return {v for v in (self.latest, self.latest_installable) if v}


@dataclass
class Report:
    dist: str
    version: str
    pypi_version: str | None = None
    # ok | unreachable | not_published | skipped | unconfirmed
    pypi_status: str = "ok"
    pypi_detail: str = ""
    # What each API said, kept apart so the report can name the disagreement
    # rather than average it away.
    simple: IndexView | None = None
    json_view: IndexView | None = None
    yanked: dict[str, str] = field(default_factory=dict)
    # simple | json-fallback | unconfirmed | unavailable | skipped
    yank_source: str = "unavailable"
    yank_detail: str = ""
    tags: list[str] | None = None  # None = could not be determined (shallow clone)
    unreleased_commits: list[dict[str, str]] = field(default_factory=list)
    oldest_unreleased_age_days: float | None = None
    changelog_unreleased_entries: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """`unconfirmed` is in this list on purpose — see decision 4 above.

        It is not a pass in the sense of "checked and fine"; it is a refusal to
        turn one PyPI cache being seconds behind another into a red run. The
        report says so in words, loudly, and exit 0 is not allowed to be the
        only thing anyone reads.
        """
        return not self.findings and self.pypi_status in (
            "ok",
            "not_published",
            "skipped",
            "unconfirmed",
        )


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


class _SimpleHTMLParser(HTMLParser):
    """PEP 503 project page — the anchor list, with PEP 592's yank attribute.

    Only ``<a>`` is of interest: its text is the filename, and ``data-yanked``
    marks a withdrawn file. The attribute's PRESENCE is the yank; its value is
    an optional reason, so an empty ``data-yanked=""`` is still yanked. Reading
    it as a truthy value would call every reasonless yank healthy — the same
    class of mistake as trusting the JSON API's lagging flag.
    """

    def __init__(self) -> None:
        super().__init__()
        self.files: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        yanked: Any = False
        if "data-yanked" in attributes:
            yanked = attributes.get("data-yanked") or True
        self._current = {"filename": "", "url": attributes.get("href") or "", "yanked": yanked}

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["filename"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current is None:
            return
        entry = self._current
        self._current = None
        entry["filename"] = entry["filename"].strip()
        if not entry["filename"]:
            # Some indexes leave the anchor empty and carry the name in href.
            entry["filename"] = urlsplit(str(entry["url"])).path.rsplit("/", 1)[-1]
        if entry["filename"]:
            self.files.append(entry)


def _parse_simple_html(body: str) -> dict[str, Any]:
    """A PEP 503 page, in the same shape PEP 691 would have handed us.

    Deliberately no ``versions`` key: PEP 700 added that to the JSON flavour and
    HTML has no equivalent. Leaving it out makes ``fetch_simple`` derive the
    version list from the filenames instead of trusting an empty one, which
    would read as "this project has no releases".
    """
    parser = _SimpleHTMLParser()
    parser.feed(body)
    parser.close()
    return {"files": parser.files}


def _get(url: str, timeout: float, accept: str | None = None) -> tuple[Any, str, str]:
    """(payload, status, detail). Never raises — the caller reports the status.

    Answers in the PEP 691 shape whichever flavour the index served, so nothing
    downstream has to care which one it was. HTML is only ever produced by a
    Simple endpoint; the JSON API has no HTML representation to be confused with.
    """
    request = urllib.request.Request(url)
    if accept:
        request.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
            content_type = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "not_published", "not on PyPI (HTTP 404)"
        return None, "unreachable", f"PyPI returned HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "unreachable", f"PyPI unreachable: {exc}"

    if "html" in content_type:
        return _parse_simple_html(raw), "ok", ""
    try:
        return json.loads(raw), "ok", ""
    except ValueError as exc:
        # An index that mislabels its content type is common enough to be worth
        # one more attempt before giving up — but only when the body actually
        # looks like a project page, never as a way to turn an error page into
        # an empty-but-successful answer.
        if "<a" in raw.lower():
            return _parse_simple_html(raw), "ok", ""
        return None, "unreachable", f"PyPI response unparseable: {exc}"


def is_prerelease(version: str) -> bool:
    return bool(PRERELEASE.match(version or ""))


def version_from_filename(filename: str, dist: str) -> str | None:
    """The version a distribution filename encodes, or None.

    Only needed as a fallback: PEP 700 added a ``versions`` key to the Simple
    API response and PyPI serves it, but the yank flag is per *file*, so files
    still have to be attributed to a version somehow.
    """
    stem = filename
    for suffix in ARCHIVE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        return None

    # PEP 503 normalisation collapses runs of `-_.`; substituting one character
    # for one character keeps the offsets usable for the slice below. Any name
    # where the two disagree in length falls through to the positional split.
    def flatten(text: str) -> str:
        return re.sub(r"[-_.]", "_", text).lower()

    prefix = flatten(dist)
    if len(prefix) == len(dist) and flatten(stem).startswith(prefix + "_"):
        rest = stem[len(dist) + 1 :]
    else:
        parts = stem.split("-")
        if len(parts) < 2:
            return None
        rest = "-".join(parts[1:])
    # A wheel carries build/python/abi/platform tags after the version; an sdist
    # carries nothing.
    return rest.split("-")[0] or None


def _yank_reason(raw: Any) -> str | None:
    """PEP 592: ``yanked`` is false, true, or a string carrying the reason."""
    if raw is False or raw is None:
        return None
    if raw is True:
        return ""
    text = str(raw)
    return text if text else ""


def _summarise(view: IndexView) -> IndexView:
    """Fill ``latest`` / ``latest_installable`` from ``versions`` + ``yanked``.

    Sorting is ours because PEP 700 does not promise ``versions`` is ordered,
    and anything ``release_key`` cannot order is left out of the ranking rather
    than sorted wrongly — the same refusal to guess as everywhere else here.
    """
    ranked = sorted(
        (v for v in view.versions if not is_prerelease(v) and release_key(v)),
        key=lambda v: release_key(v) or (),
    )
    view.latest = ranked[-1] if ranked else None
    installable = [v for v in ranked if v not in view.yanked]
    view.latest_installable = installable[-1] if installable else None
    return view


def simple_url(dist: str, index_url: str = PYPI_SIMPLE) -> str:
    """The project page on a PEP 503 index.

    The name is normalised (PEP 503 §normalized-names) rather than passed
    through: an index is only required to serve the normalised spelling, and
    ``Foo.Bar_Baz`` would 404 on one that does — which this probe would then
    report as "never published".
    """
    normalised = re.sub(r"[-_.]+", "-", dist).lower()
    return f"{index_url.rstrip('/')}/{quote(normalised)}/"


def fetch_simple(dist: str, timeout: float, index_url: str = PYPI_SIMPLE) -> IndexView:
    """The Simple API — the surface ``pip`` installs from, and the primary here.

    ``index_url`` is the same value ``pip --index-url`` takes, so a target that
    publishes to a private index can be audited against the index it actually
    publishes to.

    The URL carries a cache-buster. The divergence this function exists for is a
    caching artefact, and asking through the same cache that is lagging would
    reproduce it faithfully rather than see past it.
    """
    view = IndexView(source="simple")
    url = simple_url(dist, index_url) + f"?_cb={int(time.time())}"
    payload, view.status, view.detail = _get(url, timeout, SIMPLE_ACCEPT)
    if payload is None:
        return view
    if not isinstance(payload, dict):
        view.status, view.detail = "unreachable", "Simple API answered a non-object"
        return view

    files = payload.get("files") or []
    # Per file, because PEP 592 yanks files. A version counts as yanked when it
    # has files and every one of them is yanked: a version with one live wheel
    # left is still installable, and calling it withdrawn would be a false
    # finding of exactly the kind this script must not produce.
    per_version: dict[str, list[str | None]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        version = version_from_filename(str(entry.get("filename", "")), dist)
        if not version:
            continue
        per_version.setdefault(version, []).append(_yank_reason(entry.get("yanked", False)))

    for version, reasons in per_version.items():
        if reasons and all(r is not None for r in reasons):
            view.yanked[version] = next((r for r in reasons if r), "")

    declared = payload.get("versions")
    view.versions = sorted(
        {str(v) for v in declared} if isinstance(declared, list) else set(per_version)
    )
    return _summarise(view)


def fetch_json(dist: str, timeout: float) -> IndexView:
    """The JSON API — kept only as the fallback and as a second opinion."""
    view = IndexView(source="json")
    payload, view.status, view.detail = _get(PYPI_JSON.format(dist=dist), timeout)
    if payload is None:
        return view
    if not isinstance(payload, dict):
        view.status, view.detail = "unreachable", "JSON API answered a non-object"
        return view

    releases = payload.get("releases") or {}
    if isinstance(releases, dict):
        for version, files in releases.items():
            if not isinstance(files, list) or not files:
                continue
            reasons = [
                _yank_reason(f.get("yanked", False)) for f in files if isinstance(f, dict)
            ]
            if reasons and all(r is not None for r in reasons):
                view.yanked[str(version)] = next((r for r in reasons if r), "")
        view.versions = sorted(str(v) for v in releases)

    _summarise(view)
    # `info.version` is what this script used to trust wholesale. It stays the
    # JSON view's answer for the latest release, because deriving one from
    # `releases` would answer a different question than the API does.
    declared = (payload.get("info") or {}).get("version")
    if declared:
        view.latest = str(declared)
    return view


def fetch_pypi_version(dist: str, timeout: float) -> tuple[str | None, str, str]:
    """(version, status, detail) from the JSON API.

    Retained as a named seam: it is the shape the rest of the portfolio and the
    test suite reach for, and it is still exactly what the JSON API answers.
    """
    view = fetch_json(dist, timeout)
    detail = f"{dist} is {view.detail}" if view.status == "not_published" else view.detail
    return view.latest, view.status, detail


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def reconcile(report: Report, simple: IndexView, json_view: IndexView) -> None:
    """Fold both index views into the report's ``pypi_*`` and ``yank_*`` fields.

    The precedence is fixed and one-directional: Simple first, JSON only when
    Simple could not be read, and neither when they are both readable and
    disagree. Nothing here averages, prefers the newer answer, or retries until
    they match — all three would turn "PyPI is mid-propagation" into a
    confident statement about the target, which is the failure being fixed.
    """
    report.simple, report.json_view = simple, json_view

    # ---- what the index serves as "latest" ---------------------------------
    if simple.readable and json_view.readable:
        agreed = json_view.latest in simple.candidates() or json_view.latest is None
        report.pypi_version = simple.latest_installable or simple.latest
        if agreed:
            report.pypi_status = "ok"
        else:
            report.pypi_status = "unconfirmed"
            report.pypi_detail = (
                f"the Simple API serves {simple.latest} and the JSON API says "
                f"{json_view.latest} — PyPI's two APIs disagree about this "
                "distribution's latest release. That is what a release or a yank "
                "looks like from the outside while it propagates; it is not a "
                "statement about the target, and no gap is claimed from it"
            )
    elif simple.readable:
        report.pypi_version = simple.latest_installable or simple.latest
        report.pypi_status = simple.status
        if not json_view.readable:
            report.pypi_detail = (
                f"the JSON API was not readable ({json_view.detail}); the Simple "
                "API answered and is the source that decides what pip installs"
            )
    elif json_view.readable:
        report.pypi_version = json_view.latest
        report.pypi_status = json_view.status
        report.pypi_detail = (
            f"the Simple API was not readable ({simple.detail}); falling back to "
            "the JSON API, which is the second-best source and not the one pip reads"
        )
    else:
        # Both failed. "Not on PyPI" is a real answer and outranks a transport
        # failure — one of the two knowing the package is absent is enough.
        if "not_published" in (simple.status, json_view.status):
            report.pypi_status = "not_published"
            report.pypi_detail = f"{report.dist} is not on PyPI (HTTP 404)"
        else:
            report.pypi_status = "unreachable"
            report.pypi_detail = simple.detail or json_view.detail

    # ---- which releases are withdrawn --------------------------------------
    if simple.readable and json_view.readable:
        # Compared over the union: a version one API has yanked and the other
        # does not know about yet is the same disagreement in a different shape.
        disputed = sorted(
            v
            for v in set(simple.versions) | set(json_view.versions)
            if (v in simple.yanked) != (v in json_view.yanked)
        )
        report.yanked = dict(simple.yanked)
        if disputed:
            report.yank_source = "unconfirmed"
            report.yank_detail = (
                "the Simple API and the JSON API disagree about the yank status of "
                + ", ".join(disputed[:6])
                + (f" (+{len(disputed) - 6} more)" if len(disputed) > 6 else "")
                + ". The Simple API's answer is shown below because it is the one "
                "pip reads, but the two are still propagating and no finding is "
                "raised from a value that is in flight"
            )
        else:
            report.yank_source = "simple"
    elif simple.readable:
        report.yanked, report.yank_source = dict(simple.yanked), "simple"
    elif json_view.readable:
        report.yanked, report.yank_source = dict(json_view.yanked), "json-fallback"
        report.yank_detail = (
            "yank status comes from the JSON API because the Simple API could not "
            "be read. This is the weaker source: its yank flag has been measured "
            "lagging the Simple API's by minutes"
        )
    else:
        report.yank_source = "unavailable"


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
        report.yank_source = "skipped"
    else:
        reconcile(report, fetch_simple(dist, timeout), fetch_json(dist, timeout))

    report.tags = release_tags(target)
    latest_tag = report.tags[0] if report.tags else None

    # 1. PUBLISH_GAP — a cut release that never landed on the index.
    #
    # Runs under `unconfirmed` as well as under `ok`, but only against the
    # HIGHEST version either API reported. A tag ahead of both readings is a
    # publish gap whichever cache you believe; a tag ahead of only the staler
    # one is the false alarm this gate is not allowed to raise.
    if report.pypi_status in ("ok", "unconfirmed") and latest_tag:
        tag_key = release_key(normalise_tag(latest_tag))
        seen = [
            v
            for view in (report.simple, report.json_view)
            if view is not None and view.readable
            for v in view.candidates()
        ]
        ranked = sorted((v for v in seen if release_key(v)), key=lambda v: release_key(v) or ())
        highest = ranked[-1] if ranked else report.pypi_version
        pypi_key = release_key(highest or "")
        if tag_key and pypi_key and tag_key > pypi_key:
            report.findings.append(
                Finding(
                    code="PUBLISH_GAP",
                    severity="high",
                    detail=(
                        f"tag {latest_tag} exists, the newest version any PyPI API "
                        f"reports is {highest} — the release was cut but never landed. "
                        "Check the publish workflow run for that tag; a pending "
                        "environment approval looks identical to a failure from here."
                    ),
                )
            )

    # 1b. RELEASE_YANKED — published, and withdrawn again.
    #
    # Deliberately not raised while the two APIs are still disagreeing about the
    # yank status: the report shows the Simple API's answer either way, so the
    # fact stays visible, but a value that is mid-propagation does not turn the
    # run red. Visible and unsure beats invisible; both beat confidently wrong.
    current = normalise_tag(latest_tag) if latest_tag else version
    if report.yank_source in ("simple", "json-fallback") and current in report.yanked:
        reason = report.yanked[current]
        # The survivor must come from the SAME view the yank flags came from —
        # an unreadable view is still a truthy object, and reading the version
        # off it would answer "nothing to fall back to" for every fallback run.
        origin = report.simple if report.yank_source == "simple" else report.json_view
        survivor = origin.latest_installable if origin else None
        source_note = (
            ""
            if report.yank_source == "simple"
            else " (read from the JSON API fallback; the Simple API was unreadable)"
        )
        report.findings.append(
            Finding(
                code="RELEASE_YANKED",
                severity="high",
                detail=(
                    f"{dist} {current} is on PyPI and YANKED"
                    + (f" — {reason}" if reason else " with no reason given")
                    + f"{source_note}. The release exists, the tag matches and CI is "
                    "green, so every other check here reads healthy; installs "
                    + (
                        f"silently resolve to {survivor} instead."
                        if survivor and survivor != current
                        else "have no newer release to fall back to."
                    )
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
    elif report.pypi_status == "unconfirmed":
        out.append(f"UNCONFIRMED {report.pypi_detail}.")
    elif report.pypi_detail:
        out.append(f"NOTE       {report.pypi_detail}.")

    if report.yank_source == "unconfirmed":
        out.append(f"UNCONFIRMED {report.yank_detail}.")
    elif report.yank_source == "json-fallback":
        out.append(f"NOTE       {report.yank_detail}.")

    if report.yanked:
        shown = sorted(report.yanked, key=lambda v: release_key(v) or ())
        out.append(
            f"NOTE       {len(shown)} yanked release(s) on PyPI: {', '.join(shown)}. "
            "Older withdrawn releases are history, not a finding — only the release "
            "this repository treats as current is one."
        )

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
        # The yank block is its own top level: a consumer that only reads
        # `pypi_version` cannot tell "published and healthy" from "published and
        # withdrawn", which is the gap this field closes.
        "yanked": dict(sorted(report.yanked.items())),
        "yank_source": report.yank_source,
        "yank_detail": report.yank_detail,
        "index_views": {
            view.source: {
                "status": view.status,
                "latest": view.latest,
                "latest_installable": view.latest_installable,
                "yanked": sorted(view.yanked),
            }
            for view in (report.simple, report.json_view)
            if view is not None
        },
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
