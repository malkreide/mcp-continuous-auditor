#!/usr/bin/env python3
"""Shipped probe — install what users install, and make it prove it runs.

WHY THIS EXISTS
---------------
Green CI is not shipped software. The concrete case, from this portfolio:
``main`` stood at 0.6.0, the GitHub release was never cut, so the publish
workflow never fired, and PyPI served 0.5.0 for an entire release cycle — with
three tools that were demonstrably broken in it. Every nightly run was green.
The auditor was reading the source and never once the thing users install.

``published_probe.py`` installs the artifact and reads its User-Agent out of the
code, but never runs the installed server. This does:

  1. install the distribution from the index into a FRESH venv (never the
     checkout);
  2. hold the installed version against the repository's version AND the last
     git tag, reporting every divergence;
  3. start the installed entrypoint and speak a real ``initialize`` plus a real
     ``tools/call`` to it;
  4. keep "not on the index at all" apart from "on the index but stale". Both
     are findings. They are not the same finding and do not have the same fix.

TWO DEPTHS, ONE PROBE
---------------------
This absorbed ``release_gap.py``, which asked a narrower question — is the fix
on ``main`` the fix users install? — from the index metadata and git alone, with
no venv and no install. That question did not stop being worth asking cheaply,
so it is a DEPTH here rather than a deleted feature:

  ``--metadata-only``   phase 1 only: what the index serves, which releases are
                        yanked, how far ``main`` has drifted past its last
                        release. Two HTTP requests and some git.
  (default)             phase 1, then install and run.

Phase 1's findings are carried into phase 2, never replaced: a yanked release is
still yanked once the install has run, and the deeper run must never report LESS
than the cheap one. Where the two phases reach the same conclusion from
different evidence — ``PUBLISH_GAP`` from metadata and ``TAG_NOT_ON_INDEX``
after an install are one statement, not two — the metadata code wins, so what a
maintainer sees does not depend on which depth happened to run.

The merge also spread the release-gap cross-check to this gate: both index APIs
are read on PyPI, and a disagreement between them is ``UNCONFIRMED`` rather than
a finding. That costs one extra request and buys a shipped-artifact gate that
does not fire during the minutes after a publish.

EXIT CODES CHANGED FOR RELEASE-GAP CALLERS
------------------------------------------
``release_gap.py`` exited 1 for findings and 2 for "not a Python repo". This
probe's vocabulary wins, because it is the one the nightly gate reads: 0 green,
2 FINDINGS, 127 the harness could not run. A caller testing ``$? -eq 1`` will
now see 2, and a target with no ``pyproject.toml`` gives 127 rather than 2 —
which is also more correct, since 2 now means "the target has a defect" and a
directory that is not a Python project has not been shown to have one.

A ``release_gap.py`` shim carried the old name and the old codes for a while so
outside callers kept working. It has been removed: the old name no longer
resolves, and the codes above are the only contract.

WHY (4) IS ITS OWN DISTINCTION
------------------------------
"Never published" means the release process has never run for this package —
the fix is to publish it. "Published but behind" means the process exists and
did not fire this time — the fix is to look at the workflow run, which usually
failed on an approval or an OIDC trust that nobody was watching. Reporting both
as "PyPI is out of date" sends the maintainer to the wrong place.

Because that distinction carries an accusation, the check behind it reads the
SIMPLE API at ``--index-url`` — the exact surface pip will resolve against, and
therefore the one this probe's entire claim is about. It used to ask pypi.org's
JSON API and then install from wherever the target publishes: two caches of one
index in the best case, and two different hosts for anyone on a private index.
Reading an arbitrary index means reading PEP 503 HTML, since the JSON flavour is
optional and HTML is the only format required; both are parsed into one shape in
``_get``. ``reconcile`` below documents why the JSON API is consulted only for
PyPI and why a 404 there is corroborated rather than believed.

THE STDIN TRAP, AGAIN — AND WORSE HERE
--------------------------------------
``transport_boot_probe.py`` documents it: close stdin after writing and the
server shuts down before network-bound work finishes, and you record a failure
that does not exist. This probe makes a real TOOL CALL, which is the most
network-bound thing a server does, so the trap has more room to bite. stdin is
held open until every answer is in, and ``_close_stdin_early`` exists purely so
the test suite can demonstrate that closing it fabricates a failure.

WHAT A FAILING TOOL CALL DOES AND DOES NOT PROVE
------------------------------------------------
A tool that answers ``isError`` is reported as a finding — that is the shape the
incident took. But this probe runs inside a Worker with a default-deny egress
allowlist, and a tool whose upstream origin is not on that list fails in exactly
the same way. The finding says so in its own text rather than letting the reader
assume the artifact is broken: check the allowlist before filing a bug against
the target. (``deploy/microvm/forward-proxy/README.md``.)

READ-ONLY, like every other path here: the target checkout is only read. The
venv is built in a temp dir and removed.

EXIT CODES
  0    the published artifact matches the repository and ran
  2    FINDING — absent from the index, stale, version divergence, or the
       installed server did not answer
  127  the HARNESS could not run (no network to the index, venv creation failed).
       An unreachable index is NOT reported as "in sync": a comparison that did
       not happen is never a pass.

Usage:
  python scripts/shipped_probe.py --dist zurich-opendata-mcp --target ../zurich-opendata-mcp
  python scripts/shipped_probe.py --dist foo-mcp --target . --tool health --format json
  python scripts/shipped_probe.py --target . --metadata-only      # no venv, no install
  python scripts/shipped_probe.py --target . --offline            # git-only, says so
  python scripts/shipped_probe.py --target . --index-url https://pypi.example.com/simple
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import quote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomllib landed in 3.11
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transport_boot_probe as tbp

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_CANNOT_RUN = 127

DEFAULT_INDEX = "https://pypi.org/simple"
DEFAULT_INSTALL_TIMEOUT = 600
DEFAULT_RUN_TIMEOUT = 120
DEFAULT_MAX_AGE_DAYS = 7.0
# Kind beats age: user-facing work is reported the moment it is unreleased.
# Housekeeping keeps the seven-day clock. See `metadata_findings`.
DEFAULT_USER_FACING_AGE_DAYS = 0.0

PYPI_JSON = "https://pypi.org/pypi/{dist}/json"
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
CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:")
# Conventional Commits' two spellings of a breaking change. The `!` is captured
# above; this catches the footer spelling when it lands in the subject line.
BREAKING_TEXT = re.compile(r"\bBREAKING[ -]CHANGE\b")

# Commit types whose delay is felt by users. Everything else is housekeeping.
USER_FACING = frozenset({"fix", "feat", "perf", "revert"})

# A release segment followed by a pre-release marker. The Simple API's `versions`
# list includes pre-releases (measured: `pydantic` served `2.14.0a1` there while
# the JSON API's `info.version` said `2.13.4`), so taking the last entry as "the
# latest release" would report an alpha as what users install. `release_key`
# cannot help — it drops everything after the release segment, so `2.14.0a1` and
# `2.14.0` order identically. `.postN` is deliberately NOT a pre-release.
PRERELEASE = re.compile(
    r"^\s*v?\d+(?:\.\d+)*[._-]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)", re.I
)

ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".whl", ".egg")

# The JSON API exists only on PyPI. Against any other index it is not a source
# that failed — it is a source that does not exist, and the two must not be
# reported as the same thing: one is a degraded run, the other a correct one
# that simply has no second opinion available.
NOT_APPLICABLE = "not_applicable"

# Publication states — (4) in the docstring. Kept apart because they send the
# maintainer to different places.
NOT_PUBLISHED = "not-published"
PUBLISHED = "published"
INDEX_UNREACHABLE = "index-unreachable"
INSTALL_FAILED = "install-failed"


@dataclass
class Finding:
    code: str
    detail: str
    # The metadata findings have always carried one; the artifact findings never
    # did, because everything they report is serious by construction — a package
    # that will not install has no low-severity reading. `high` is therefore the
    # default rather than a claim made per finding.
    severity: str = "high"  # high | medium | low

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass
class Versions:
    installed: str = ""
    repo: str = ""
    tag: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"installed": self.installed, "repo": self.repo, "tag": self.tag}


@dataclass
class TreeDiff:
    """The published artifact's Python sources against the checkout's.

    THE GAP THIS CLOSES. Every other comparison in this file is between VERSION
    NUMBERS, and numbers agree in exactly the case that matters most: the
    artifact on the index and the tree in the repository both say 0.3.3 and are
    not the same code. That happens whenever something was edited after a
    release without a bump, or the release was built from a different tree than
    the one anybody is reading. Numbers cannot see it. Content can.
    """

    checked: bool = False
    compared: int = 0
    differs: list[str] = field(default_factory=list)
    missing_in_artifact: list[str] = field(default_factory=list)
    # In the wheel and not in the tree. Reported, but NOT on its own a finding:
    # a generated `_version.py` from setuptools-scm lives exactly here, and
    # calling that a stale artifact would be a false accusation on every target
    # that uses dynamic versioning.
    extra_in_artifact: list[str] = field(default_factory=list)
    truncated: bool = False
    detail: str = ""

    @property
    def diverged(self) -> bool:
        return bool(self.differs or self.missing_in_artifact)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "compared": self.compared,
            "differs": self.differs,
            "missing_in_artifact": self.missing_in_artifact,
            "extra_in_artifact": self.extra_in_artifact,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass
class ToolCall:
    ran: bool = False
    name: str = ""
    status: str = "skipped"  # ok | error | empty | no-answer | skipped
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Report:
    dist: str
    publication: str = PUBLISHED
    versions: Versions = field(default_factory=Versions)
    findings: list[Finding] = field(default_factory=list)
    entrypoint: str = ""
    tools: int | None = None
    tool_call: ToolCall = field(default_factory=ToolCall)
    tree: TreeDiff = field(default_factory=TreeDiff)
    harness_error: str = ""

    # ---- phase 1: the repository and the index ----------------------------
    # Cheap: two HTTP requests and some git. Everything above this line needs a
    # venv and an install, which is why `--metadata-only` stops here.
    depth: str = "full"  # full | metadata
    index_url: str = DEFAULT_INDEX
    index_version: str | None = None
    # ok | unreachable | not_published | skipped | unconfirmed
    index_status: str = "ok"
    index_detail: str = ""
    # What each index API said, kept apart so the report can name a
    # disagreement rather than average it away.
    simple: IndexView | None = None
    json_view: IndexView | None = None
    yanked: dict[str, str] = field(default_factory=dict)
    # simple | json-fallback | unconfirmed | unavailable | skipped
    yank_source: str = "unavailable"
    yank_detail: str = ""
    tags: list[str] | None = None  # None = undeterminable (a --depth 1 clone)
    unreleased_commits: list[dict[str, str]] = field(default_factory=list)
    oldest_unreleased_age_days: float | None = None
    changelog_unreleased_entries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            # Bumped from 1: the report gained the whole phase-1 block, and
            # findings gained `severity`. Bumped to 3 for `tree`, the artifact
            # content comparison. Additive both times, but a consumer pinning the
            # shape deserves to see the number move.
            "schema": 3,
            "dist": self.dist,
            "publication": self.publication,
            "depth": self.depth,
            "versions": self.versions.as_dict(),
            "entrypoint": self.entrypoint,
            "tools": self.tools,
            "tool_call": self.tool_call.as_dict(),
            "tree": self.tree.as_dict(),
            "harness_error": self.harness_error,
            "index_url": self.index_url,
            "index_version": self.index_version,
            "index_status": self.index_status,
            "index_detail": self.index_detail,
            "index_views": {
                view.source: {
                    "status": view.status,
                    "latest": view.latest,
                    "latest_installable": view.latest_installable,
                    "yanked": sorted(view.yanked),
                }
                for view in (self.simple, self.json_view)
                if view is not None
            },
            "yanked": dict(sorted(self.yanked.items())),
            "yank_source": self.yank_source,
            "yank_detail": self.yank_detail,
            "latest_tag": self.tags[0] if self.tags else None,
            "tags_available": self.tags is not None,
            "unreleased_commits": len(self.unreleased_commits),
            "oldest_unreleased_age_days": self.oldest_unreleased_age_days,
            "changelog_unreleased_entries": self.changelog_unreleased_entries,
            "findings": [f.as_dict() for f in self.findings],
            "exit_code": self.exit_code(),
        }

    def exit_code(self) -> int:
        if self.harness_error:
            return EXIT_CANNOT_RUN
        return EXIT_FINDINGS if self.findings else EXIT_GREEN

    @property
    def ok(self) -> bool:
        """Nothing found and nothing prevented the finding.

        `unconfirmed` is deliberately not disqualifying — see the boot gate's
        `not-selected`. It is a refusal to turn one index cache being seconds
        behind another into a red run, and the report says so in words. An
        unreachable index is a different matter and sets `harness_error`.
        """
        return not self.findings and not self.harness_error


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


def is_shallow(root: Path) -> bool:
    """Is this checkout a shallow clone?

    Load-bearing for ``NO_TAGS``. ``git clone --depth 1`` fetches no tags, and
    ``git tag --list`` then SUCCEEDS with empty output — indistinguishable from a
    repository that has never cut a release, unless this is asked. Reporting
    "this project has no tags" about a repository whose tags simply were not
    fetched is the same class of mistake as reporting "no User-Agent" for one the
    probe could not parse.
    """
    return (git(root, "rev-parse", "--is-shallow-repository") or "").strip() == "true"


def release_tags(root: Path) -> list[str] | None:
    """Tags that look like releases, newest first. None when undeterminable.

    An EMPTY list from a shallow clone is undeterminable, not empty — see
    ``is_shallow``. Only a full checkout that lists no tags is entitled to the
    claim that there are none.
    """
    out = git(root, "tag", "--list", "--sort=-v:refname")
    if out is None:
        return None
    tags = [t for t in out.splitlines() if TAG.match(t.strip())]
    if not tags and is_shallow(root):
        return None
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
        self._current = {
            "filename": "",
            "url": attributes.get("href") or "",
            "yanked": yanked,
        }

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
            return None, "not_published", "not on the index (HTTP 404)"
        return None, "unreachable", f"the index returned HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "unreachable", f"index unreachable: {exc}"

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
        return None, "unreachable", f"index response unparseable: {exc}"


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


def is_pypi(index_url: str) -> bool:
    """Is this index PyPI itself — the only index with a JSON API to compare to?

    Host-based, not a prefix match on the URL: ``https://pypi.org/simple`` and
    ``https://pypi.org/simple/`` are the same index, while a mirror at
    ``https://mirror.local/pypi.org/simple`` is emphatically not one.
    """
    host = urlsplit(index_url).hostname or ""
    return host == "pypi.org" or host.endswith(".pypi.org")


def simple_url(dist: str, index_url: str = DEFAULT_INDEX) -> str:
    """The project page on a PEP 503 index.

    The name is normalised (PEP 503 §normalized-names) rather than passed
    through: an index is only required to serve the normalised spelling, and
    ``Foo.Bar_Baz`` would 404 on one that does — which this probe would then
    report as "never published".
    """
    normalised = re.sub(r"[-_.]+", "-", dist).lower()
    return f"{index_url.rstrip('/')}/{quote(normalised)}/"


def fetch_simple(
    dist: str, timeout: float, index_url: str = DEFAULT_INDEX
) -> IndexView:
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
        per_version.setdefault(version, []).append(
            _yank_reason(entry.get("yanked", False))
        )

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
                _yank_reason(f.get("yanked", False))
                for f in files
                if isinstance(f, dict)
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
    detail = (
        f"{dist} is {view.detail}" if view.status == "not_published" else view.detail
    )
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
        report.index_version = simple.latest_installable or simple.latest
        if agreed:
            report.index_status = "ok"
        else:
            report.index_status = "unconfirmed"
            report.index_detail = (
                f"the Simple API serves {simple.latest} and the JSON API says "
                f"{json_view.latest} — PyPI's two APIs disagree about this "
                "distribution's latest release. That is what a release or a yank "
                "looks like from the outside while it propagates; it is not a "
                "statement about the target, and no gap is claimed from it"
            )
    elif simple.readable:
        report.index_version = simple.latest_installable or simple.latest
        report.index_status = simple.status
        if json_view.status == NOT_APPLICABLE:
            # Not a degraded run — a correctly narrower one. Said out loud all
            # the same: every UNCONFIRMED outcome in this script depends on
            # having two opinions, and here there is exactly one, so the
            # cross-check that would catch a mid-propagation index is not armed.
            report.index_detail = json_view.detail
        elif not json_view.readable:
            report.index_detail = (
                f"the JSON API was not readable ({json_view.detail}); the Simple "
                "API answered and is the source that decides what pip installs"
            )
    elif json_view.readable:
        report.index_version = json_view.latest
        report.index_status = json_view.status
        report.index_detail = (
            f"the Simple API was not readable ({simple.detail}); falling back to "
            "the JSON API, which is the second-best source and not the one pip reads"
        )
    else:
        # Neither answered. "Not on the index" is a real answer and outranks a
        # transport failure — one of the two knowing the package is absent is
        # enough. NOT_APPLICABLE is neither: it is the absence of a second
        # opinion, not a second opinion of absence.
        statuses = [simple.status, json_view.status]
        if "not_published" in statuses:
            report.index_status = "not_published"
            report.index_detail = (
                f"{report.dist} is not on {report.index_url} (HTTP 404)"
            )
        else:
            report.index_status = "unreachable"
            report.index_detail = simple.detail or json_view.detail
            if json_view.status == NOT_APPLICABLE:
                report.index_detail += (
                    f" — and {report.index_url} is not PyPI, so there was no JSON "
                    "API to fall back to"
                )

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
                "breaking": "yes"
                if ((m and m.group("bang")) or BREAKING_TEXT.search(subject))
                else "",
            }
        )
    return commits


def age_days(iso: str, now: datetime | None = None) -> float:
    stamp = datetime.fromisoformat(iso)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
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
# PURE LOGIC — no network, no subprocess. This is the part the tests own.
# --------------------------------------------------------------------------


def compare_versions(v: Versions) -> list[Finding]:
    """Every divergence between what is installed, what the repo says, and what
    the last tag says.

    Ordering uses `release_key`'s narrow release-segment comparison; anything it
    cannot order is reported as a plain difference rather than silently ranked
    the wrong way round. The direction matters: PyPI *behind* the repo is the
    incident, PyPI *ahead* of it means something was published from a tree this
    checkout does not have, which is a different and rarer problem.
    """
    out: list[Finding] = []
    inst_key = release_key(v.installed) if v.installed else None
    repo_key = release_key(v.repo) if v.repo else None

    if v.installed and v.repo and v.installed != v.repo:
        if inst_key and repo_key and inst_key < repo_key:
            out.append(
                Finding(
                    "STALE_ON_INDEX",
                    f"PyPI serves {v.installed}, the repository is at {v.repo} — users "
                    f"install {v.installed}. This is the shape of the incident: the "
                    "release was never cut, so the publish workflow never ran",
                )
            )
        elif inst_key and repo_key and inst_key > repo_key:
            out.append(
                Finding(
                    "INDEX_AHEAD",
                    f"PyPI serves {v.installed}, ahead of the repository's {v.repo} — "
                    "something was published from a tree this checkout does not have, "
                    "or the checkout is not the branch that releases",
                )
            )
        else:
            out.append(
                Finding(
                    "VERSION_DIFFERS",
                    f"PyPI serves {v.installed}, the repository says {v.repo}; neither "
                    "could be ordered against the other, so the direction is unknown",
                )
            )

    if v.installed and v.tag:
        tag_version = normalise_tag(v.tag)
        tag_key = release_key(tag_version) if tag_version else None
        if tag_version and tag_version != v.installed:
            if tag_key and inst_key and tag_key > inst_key:
                out.append(
                    Finding(
                        "TAG_NOT_ON_INDEX",
                        f"the last tag is {v.tag} but PyPI serves {v.installed} — a tag "
                        "exists that the index does not. Somebody cut the release and "
                        "it did not land, so the publish WORKFLOW RUN is where to look, "
                        "not the release process",
                    )
                )
            else:
                out.append(
                    Finding(
                        "TAG_DIFFERS",
                        f"the last tag is {v.tag} while PyPI serves {v.installed} — the "
                        "index is not behind the tag, so the two were cut from "
                        "different places",
                    )
                )
    return out


def find_repo_package(target: Path, top: str) -> Path | None:
    """Where a top-level import package lives in the checkout, or None.

    Only the two layouts this portfolio actually uses. A layout that is neither
    returns None and the comparison reports itself as not made, rather than
    walking the whole repository looking for something that looks close enough.
    """
    for candidate in (target / "src" / top, target / top):
        if candidate.is_dir():
            return candidate
    return None


def _source_bytes(path: Path) -> bytes | None:
    """File contents with line endings normalised, or None if unreadable.

    Normalised because a wheel built on one platform and a checkout on another
    legitimately differ in exactly that byte and in nothing else — a diff this
    probe reported for it would be true and useless.
    """
    try:
        return path.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return None


def compare_trees(artifact: Path, repo: Path, cap: int = 800) -> TreeDiff:
    """The installed package's ``*.py`` against the checkout's. Pure.

    ONLY ``*.py``: the code is what runs, and it is the part whose divergence is
    a defect rather than a build detail. Data files, compiled caches and
    packaging metadata differ between an sdist build and a working tree for
    reasons that say nothing about whether the published code is the published
    code.
    """
    diff = TreeDiff(checked=True)

    def sources(root: Path) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            out[path.relative_to(root).as_posix()] = path
        return out

    left, right = sources(artifact), sources(repo)
    for rel in sorted(set(left) | set(right)):
        if diff.compared >= cap:
            diff.truncated = True
            break
        if rel not in left:
            diff.missing_in_artifact.append(rel)
            continue
        if rel not in right:
            diff.extra_in_artifact.append(rel)
            continue
        diff.compared += 1
        a, b = _source_bytes(left[rel]), _source_bytes(right[rel])
        if a is None or b is None or a != b:
            diff.differs.append(rel)
    return diff


# Failures whose text says "the sandbox stopped me", not "the artifact is broken".
# This probe runs behind a default-deny egress allowlist, and a tool whose
# upstream origin is not on that list fails in the same place a genuinely broken
# tool does. Reporting those as findings would make the gate fire on every target
# whose upstream nobody has allowlisted yet — and a gate that cries wolf gets
# muted, which is the same reasoning that keeps recall floors at half the
# observed count. So they are recorded as UNATTRIBUTABLE and do not raise a
# finding. Note what is deliberately NOT in this list: an empty content list.
# That is the incident's own shape, it looks nothing like a blocked socket, and
# it stays a finding.
_EGRESS_MARKERS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "getaddrinfo",
    "dns",
    "proxy",
    "timed out",
    "timeout",
    "network is unreachable",
    "no route to host",
    "ssl",
    "certificate verify failed",
    "403 forbidden",
    "tunnel connection failed",
)


def looks_like_egress(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _EGRESS_MARKERS)


def classify_tool_result(payload: Any) -> ToolCall:
    """Turn a ``tools/call`` reply into a verdict.

    Deliberately more than two-way. "The transport answered", "the tool worked"
    and "the sandbox let it reach its upstream" are three different claims, and
    the incident was tools that answered perfectly well with nothing in them.
    """
    if not isinstance(payload, dict):
        return ToolCall(
            ran=True,
            status="no-answer",
            detail="the server returned no JSON-RPC object",
        )
    if "error" in payload:
        err = payload.get("error") or {}
        message = str(err.get("message"))
        if looks_like_egress(message):
            return ToolCall(
                ran=True,
                status="blocked",
                detail=f"unattributable — the error reads like this "
                f"Worker's egress allowlist, not the artifact: "
                f"{message[:160]}",
            )
        return ToolCall(
            ran=True,
            status="error",
            detail=f"JSON-RPC error {err.get('code')}: {message[:160]}",
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        return ToolCall(
            ran=True, status="no-answer", detail="the reply carried no result object"
        )
    if result.get("isError"):
        text = " ".join(
            str(c.get("text", ""))
            for c in (result.get("content") or [])
            if isinstance(c, dict)
        )
        if looks_like_egress(text):
            return ToolCall(
                ran=True,
                status="blocked",
                detail=f"unattributable — the tool's own error reads "
                f"like this Worker's egress allowlist rather "
                f"than a defect: {text[:160]}",
            )
        return ToolCall(
            ran=True,
            status="error",
            detail=f"the tool reported isError: {text[:160]}. If the target's "
            "upstream origin is not on this Worker's allowlist, add it "
            "before filing this against the target "
            "(deploy/microvm/forward-proxy/README.md)",
        )
    content = result.get("content")
    if isinstance(content, list) and not content:
        return ToolCall(
            ran=True,
            status="empty",
            detail="the tool returned an empty content list — it "
            "answered, and it answered with nothing",
        )
    return ToolCall(ran=True, status="ok", detail="returned content")


def pick_tool(tools: list[dict[str, Any]], preferred: str = "") -> tuple[str, str]:
    """Which tool to call, and why not, if none.

    A tool is only callable blind when it needs no arguments — inventing values
    for a required parameter would test our guess, not the artifact.
    """
    by_name = {str(t.get("name")): t for t in tools if isinstance(t, dict)}
    if preferred:
        if preferred in by_name:
            return preferred, ""
        return "", f"the requested tool {preferred!r} is not in tools/list"
    for name, spec in by_name.items():
        schema = (
            spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
        )
        if not (schema.get("required") or []):
            return name, ""
    if by_name:
        return "", (
            "every tool requires arguments; pass --tool/--tool-args to "
            "exercise one rather than have the probe guess values"
        )
    return "", "the server listed no tools"


# Phase 2 finding -> the phase 1 finding that already made the same claim from
# cheaper evidence. Merging the two probes merged two vocabularies that grew
# apart, and some of their codes are the same statement reached twice:
#
#   PUBLISH_GAP      "a tag exists that the index does not have" — from metadata
#   TAG_NOT_ON_INDEX "a tag exists that the INSTALLED version is behind" — same
#                    fact, established after paying for a venv
#
# Reporting both is not extra information, it is the same sentence twice with
# different provenance, and a reader counting findings would double-count it.
# The metadata one wins because it is the one that also fires in --metadata-only,
# so the code a maintainer sees does not depend on which depth happened to run.
_SUPERSEDED_BY = {"TAG_NOT_ON_INDEX": "PUBLISH_GAP"}


def dedupe(findings: list[Finding]) -> list[Finding]:
    """One statement, one finding — see ``_SUPERSEDED_BY``.

    Also drops exact code repeats, which the phase-1/phase-2 concatenation can
    produce for a target where both passes reach the same conclusion.
    """
    present = {f.code for f in findings}
    out: list[Finding] = []
    seen: set[str] = set()
    for finding in findings:
        if _SUPERSEDED_BY.get(finding.code) in present:
            continue
        if finding.code in seen:
            continue
        seen.add(finding.code)
        out.append(finding)
    return out


def read_index(report: Report, target: Path, timeout: float, offline: bool) -> None:
    """Phase 1a — what the index serves, and which releases are withdrawn."""
    if offline:
        report.index_status = "skipped"
        report.index_detail = "--offline: the published artifact was not consulted"
        report.yank_source = "skipped"
        return

    # Primary first, so the order of the requests matches the order of authority
    # and a reader tracing the network sees the precedence the docstring claims.
    simple_view = fetch_simple(report.dist, timeout, report.index_url)
    # The JSON API is asked ONLY when the index is PyPI. Querying pypi.org about
    # a distribution that lives on a private index is not a weaker second
    # opinion, it is a different package: same name, unrelated contents, and any
    # agreement or disagreement between them is noise.
    if is_pypi(report.index_url):
        json_view = fetch_json(report.dist, timeout)
    else:
        json_view = IndexView(
            source="json",
            status=NOT_APPLICABLE,
            detail=(
                f"{report.index_url} is not PyPI, which is the only index with a "
                "JSON API — so the Simple API's answer stands alone and the "
                "cross-check that would catch a mid-propagation index did not run"
            ),
        )
    reconcile(report, simple_view, json_view)


def metadata_findings(
    report: Report,
    target: Path,
    repo_version: str,
    max_age_days: float,
    now: datetime | None = None,
    user_facing_age: float = 0.0,
) -> list[Finding]:
    """Phase 1b — everything answerable from the index and git, no install.

    Ordered by sharpness, not by how the evidence was gathered: a cut release
    that never landed outranks commits that have not been released, which
    outranks a prepared version, which outranks prose in a CHANGELOG.
    """
    out: list[Finding] = []
    latest_tag = report.tags[0] if report.tags else None

    # 0. NO_TAGS — nothing to measure the rest against.
    #
    # `report.tags == []` is a full checkout that has never cut a release; a
    # shallow clone comes back as None instead (see `release_tags`), because
    # "the tags were not fetched" is a different statement. Reported as its own
    # finding rather than left implicit, because half the checks below quietly
    # do nothing without a tag and the run then reads as a clean bill of health
    # for comparisons that never happened.
    if report.tags == []:
        out.append(
            Finding(
                "NO_TAGS",
                "the repository has no release tags at all. PUBLISH_GAP, TAG_NOT_ON_INDEX "
                "and UNTAGGED_VERSION all measure against the last tag and therefore "
                "measured nothing here, and UNRELEASED counted every commit in history "
                "rather than the ones past a release. A green run on an untagged "
                "repository is a statement about how little was compared",
                severity="medium",
            )
        )

    # 1. PUBLISH_GAP — a cut release that never landed on the index.
    #
    # Runs under `unconfirmed` as well as under `ok`, but only against the
    # HIGHEST version either API reported. A tag ahead of both readings is a
    # publish gap whichever cache you believe; a tag ahead of only the staler
    # one is the false alarm this gate is not allowed to raise.
    if report.index_status in ("ok", "unconfirmed") and latest_tag:
        tag_key = release_key(normalise_tag(latest_tag))
        seen = [
            v
            for view in (report.simple, report.json_view)
            if view is not None and view.readable
            for v in view.candidates()
        ]
        ranked = sorted(
            (v for v in seen if release_key(v)), key=lambda v: release_key(v) or ()
        )
        highest = ranked[-1] if ranked else report.index_version
        index_key = release_key(highest or "")
        if tag_key and index_key and tag_key > index_key:
            out.append(
                Finding(
                    "PUBLISH_GAP",
                    f"tag {latest_tag} exists, the newest version any index API reports "
                    f"is {highest} — the release was cut but never landed. Check the "
                    "publish workflow run for that tag; a pending environment approval "
                    "looks identical to a failure from here.",
                )
            )

    # 2. RELEASE_YANKED — published, and withdrawn again.
    #
    # Deliberately not raised while the two APIs are still disagreeing about the
    # yank status: the report shows the Simple API's answer either way, so the
    # fact stays visible, but a value that is mid-propagation does not turn the
    # run red. Visible and unsure beats invisible; both beat confidently wrong.
    current = normalise_tag(latest_tag) if latest_tag else repo_version
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
        out.append(
            Finding(
                "RELEASE_YANKED",
                f"{report.dist} {current} is on {report.index_url} and YANKED"
                + (f" — {reason}" if reason else " with no reason given")
                + f"{source_note}. The release exists, the tag matches and CI is green, "
                "so every other check here reads healthy; installs "
                + (
                    f"silently resolve to {survivor} instead."
                    if survivor and survivor != current
                    else "have no newer release to fall back to."
                ),
            )
        )

    # 3. UNRELEASED — work on main beyond the last release.
    #
    # KIND BEATS AGE. This used to be a single age threshold, and a `fix:` that
    # had sat unreleased for six days was reported as nothing at all, exactly
    # like a `docs:` — while every one of those days is a day users run the
    # behaviour the fix removed. The two are not the same fact and they no
    # longer share a clock:
    #
    #   breaking (`feat!:`, `BREAKING CHANGE`)  reported at any age
    #   user-facing (fix/feat/perf/revert)      reported past user_facing_age,
    #                                           which defaults to 0 — immediately
    #   housekeeping                            still waits for max_age_days
    #
    # `--max-age-days-user-facing` exists for anyone who wants a grace period
    # after a merge rather than none; the default says the delay itself is the
    # thing worth seeing.
    if report.unreleased_commits:
        oldest = min(report.unreleased_commits, key=lambda c: c["date"])
        report.oldest_unreleased_age_days = age_days(oldest["date"], now)
        user_facing = [c for c in report.unreleased_commits if c["type"] in USER_FACING]
        breaking = [c for c in report.unreleased_commits if c.get("breaking")]
        age = report.oldest_unreleased_age_days
        due = (
            bool(breaking)
            or (bool(user_facing) and age > user_facing_age)
            or age > max_age_days
        )
        if due:
            kinds: dict[str, int] = {}
            for c in report.unreleased_commits:
                kinds[c["type"]] = kinds.get(c["type"], 0) + 1
            breakdown = ", ".join(f"{n}× {t}" for t, n in sorted(kinds.items()))
            if breaking:
                why = (
                    f" {len(breaking)} of them BREAKING "
                    f"({breaking[0]['subject'][:60]!r}) — a breaking change that is "
                    "not released is a change nobody can plan around, whatever its "
                    "age."
                )
            elif user_facing:
                why = (
                    f" {len(user_facing)} of them user-facing — every day of delay "
                    "is a day users run the old behaviour."
                )
            else:
                why = " None user-facing; housekeeping only."
            out.append(
                Finding(
                    "UNRELEASED",
                    f"{len(report.unreleased_commits)} commit(s) beyond "
                    f"{latest_tag or 'the start of history'}, oldest "
                    f"{age:.1f} days old ({breakdown})." + why,
                    severity="high" if (breaking or user_facing) else "low",
                )
            )

    # 4. UNTAGGED_VERSION — pyproject bumped without a matching tag.
    if report.tags is not None and repo_version not in ("(dynamic)", ""):
        tagged = {normalise_tag(t) for t in report.tags}
        if (
            repo_version not in tagged
            and report.oldest_unreleased_age_days is not None
            and report.oldest_unreleased_age_days > max_age_days
        ):
            out.append(
                Finding(
                    "UNTAGGED_VERSION",
                    f"pyproject.toml says {repo_version}, no tag matches it. A prepared "
                    "release that was never cut looks exactly like this.",
                    severity="medium",
                )
            )

    # 5. CHANGELOG_UNRELEASED — weakest signal, reported last.
    if (
        report.changelog_unreleased_entries
        and report.oldest_unreleased_age_days is not None
        and report.oldest_unreleased_age_days > max_age_days
    ):
        out.append(
            Finding(
                "CHANGELOG_UNRELEASED",
                f"[Unreleased] carries {report.changelog_unreleased_entries} line(s) of "
                "entries. Written up, not shipped.",
                severity="low",
            )
        )

    return out


def build_findings(report: Report) -> list[Finding]:
    """The whole verdict, from an already-populated report. Pure."""
    out: list[Finding] = []
    if report.publication == NOT_PUBLISHED:
        out.append(
            Finding(
                "NOT_ON_INDEX",
                f"{report.dist} does not exist on the index at all. Distinct from a "
                "stale release: there is no publish process to repair here, there is "
                "one to create — `pip install` has never worked for this package",
            )
        )
        return out
    if report.publication == INSTALL_FAILED:
        out.append(
            Finding(
                "INSTALL_FAILED",
                "the distribution exists on the index but could not be installed into "
                "a clean venv — which is what every user's first command does",
            )
        )
        return out

    out.extend(compare_versions(report.versions))

    # STALE_ARTIFACT — the one comparison the version numbers cannot make.
    # Only raised when the numbers AGREE, because that is the whole gap: two
    # things called 0.3.3 that are not the same code. Where the numbers already
    # disagree, STALE_ON_INDEX above says it more directly and a content diff
    # would just be the same news with more lines.
    if report.tree.checked and report.tree.diverged:
        shown = ", ".join((report.tree.differs + report.tree.missing_in_artifact)[:5])
        more = len(report.tree.differs) + len(report.tree.missing_in_artifact) - 5
        out.append(
            Finding(
                "STALE_ARTIFACT",
                f"the index serves {report.versions.installed} and the checkout says the "
                f"same, but {len(report.tree.differs)} installed source file(s) differ "
                f"from the checkout and {len(report.tree.missing_in_artifact)} are absent "
                f"from the artifact entirely ({shown}"
                + (f", +{more} more" if more > 0 else "")
                + "). One version number, two different bodies of code — a version "
                "comparison cannot see this, which is why it went unseen. Either the "
                "release was built from a tree nobody is reading, or the tree moved "
                "after the release without the version moving with it",
            )
        )

    if not report.entrypoint:
        out.append(
            Finding(
                "NO_ENTRYPOINT",
                "the installed distribution declares no console script — nothing to "
                "start, so nobody can run what was published",
            )
        )
        return out

    if report.tools is None:
        out.append(
            Finding(
                "DOES_NOT_RUN",
                "the installed entrypoint did not answer initialize + tools/list. The "
                "artifact on the index does not start, whatever the branch does",
            )
        )
        return out

    tc = report.tool_call
    if tc.status == "error":
        out.append(Finding("TOOL_ERROR", f"{tc.name}: {tc.detail}"))
    elif tc.status == "no-answer":
        out.append(Finding("TOOL_NO_ANSWER", f"{tc.name}: {tc.detail}"))
    elif tc.status == "empty":
        # The incident's own shape: it answered, and it answered with nothing.
        out.append(Finding("TOOL_EMPTY", f"{tc.name}: {tc.detail}"))
    # status == "blocked" raises nothing on purpose — see _EGRESS_MARKERS. It is
    # still in the report, so it is visible rather than swallowed.
    return out


# --------------------------------------------------------------------------
# IMPURE — the network and the subprocess. Injected, so the logic above can be
# tested without either.
# --------------------------------------------------------------------------


@dataclass
class Installed:
    ok: bool
    version: str = ""
    entrypoint: str = ""
    python: str = ""
    detail: str = ""
    site: str = ""
    tops: list[str] = field(default_factory=list)


def install_from_index(
    dist: str, workdir: Path, index_url: str, timeout: float, pin_version: str = ""
) -> Installed:
    """A fresh venv and one ``pip install`` from the index.

    ``--no-cache-dir`` is not optional: with a warm wheel cache this measures
    what pip kept on disk last time, which is precisely the stale artifact we
    are trying to catch — the check would then confirm the bug as healthy.
    ``--index-url`` is pinned so a pip.conf mirror cannot quietly answer for
    PyPI either.

    ``pin_version`` makes it ``dist==VERSION``. Unpinned is the DEFAULT and is
    the right question for a gate — what does a user's ``pip install`` actually
    resolve to today. It is the wrong question for a re-check immediately after
    a release: ``--no-cache-dir`` empties pip's cache and not the index's, and
    an unpinned install was measured serving the PREVIOUS artifact for minutes
    while the new version was already listed. A re-check that does not pin is a
    re-check of the release before it. Same propagation window `reconcile`
    refuses to guess through, on the install side.
    """
    env_dir = workdir / "venv"
    try:
        venv.create(env_dir, with_pip=True, clear=True)
    except Exception as exc:  # noqa: BLE001 - harness failure, reported as such
        return Installed(
            False, detail=f"venv creation failed: {type(exc).__name__}: {exc}"
        )
    py = (
        env_dir
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    try:
        proc = subprocess.run(
            [
                str(py),
                "-m",
                "pip",
                "install",
                "-q",
                "--no-cache-dir",
                "--index-url",
                index_url,
                f"{dist}=={pin_version}" if pin_version else dist,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Installed(False, detail=f"pip install exceeded {timeout:.0f}s")
    except OSError as exc:
        return Installed(
            False, detail=f"could not run pip: {type(exc).__name__}: {exc}"
        )
    if proc.returncode != 0:
        return Installed(
            False, detail=(proc.stderr or proc.stdout or "").strip()[-400:]
        )

    # `site` and `tops` are what the content comparison needs: where the wheel
    # unpacked to, and which top-level packages it owns there.
    probe_src = (
        "import json,sys,sysconfig\n"
        "from importlib import metadata as m\n"
        "d=sys.argv[1]\n"
        "try:\n"
        "    v=m.version(d)\n"
        "except Exception as e:\n"
        "    print(json.dumps({'error':str(e)})); raise SystemExit(0)\n"
        "eps=[]\n"
        "try:\n"
        "    for ep in m.distribution(d).entry_points:\n"
        "        if ep.group=='console_scripts': eps.append(ep.name)\n"
        "except Exception: pass\n"
        "tops=set()\n"
        "try:\n"
        "    for f in m.files(d) or []:\n"
        "        p=f.parts\n"
        "        if len(p)>1 and p[0] not in ('..',) and not p[0].endswith(('.dist-info','.data')):\n"
        "            tops.add(p[0])\n"
        "except Exception: pass\n"
        "print(json.dumps({'version':v,'scripts':eps,'tops':sorted(tops),"
        "'site':sysconfig.get_paths()['purelib']}))\n"
    )
    try:
        meta = subprocess.run(
            [str(py), "-c", probe_src, dist],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        info = json.loads((meta.stdout or "{}").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        return Installed(False, detail=f"installed metadata unreadable: {exc}")
    if info.get("error"):
        return Installed(
            False, detail=f"installed metadata unreadable: {info['error']}"
        )

    scripts = info.get("scripts") or []
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    entry = ""
    for name in scripts:
        candidate = bindir / name
        if candidate.exists():
            entry = str(candidate)
            break
    return Installed(
        True,
        version=str(info.get("version") or ""),
        entrypoint=entry,
        python=str(py),
        site=str(info.get("site") or ""),
        tops=[str(t) for t in (info.get("tops") or [])],
    )


def compare_content(report: Report, got: Installed, target: Path) -> None:
    """Fill ``report.tree`` — the artifact's sources against the checkout's.

    Runs only when the two version numbers AGREE, because that is the only case
    the comparison adds anything to: where they differ, `compare_versions` has
    already said so from cheaper evidence. Every reason the comparison could not
    be made is recorded in ``detail`` and leaves ``checked`` false, so a
    comparison that did not happen never reads as one that came back clean.
    """
    tree = report.tree
    if not (report.versions.installed and report.versions.repo):
        tree.detail = "no version on one side; nothing to compare content for"
        return
    if report.versions.installed != report.versions.repo:
        tree.detail = (
            f"the index serves {report.versions.installed} and the checkout says "
            f"{report.versions.repo} — the numbers already differ, so a content "
            "comparison would only restate it"
        )
        return
    if not got.site or not got.tops:
        tree.detail = "the installed distribution owns no importable top-level package"
        return

    merged = TreeDiff(checked=False)
    compared_any = False
    for top in got.tops:
        artifact_dir = Path(got.site) / top
        repo_dir = find_repo_package(target, top)
        if not artifact_dir.is_dir():
            continue
        if repo_dir is None:
            merged.detail += (
                f"{top}: no matching package in the checkout (looked in src/{top} "
                f"and {top}/); "
            )
            continue
        compared_any = True
        one = compare_trees(artifact_dir, repo_dir)
        merged.compared += one.compared
        merged.truncated = merged.truncated or one.truncated
        merged.differs += [f"{top}/{p}" for p in one.differs]
        merged.missing_in_artifact += [f"{top}/{p}" for p in one.missing_in_artifact]
        merged.extra_in_artifact += [f"{top}/{p}" for p in one.extra_in_artifact]
    merged.checked = compared_any
    if not compared_any and not merged.detail:
        merged.detail = "no top-level package could be paired with the checkout"
    report.tree = merged


def speak_mcp(
    argv: list[str],
    timeout: float,
    cwd: Path,
    tool: str = "",
    tool_args: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    _close_stdin_early: bool = False,
) -> dict[str, Any]:
    """initialize -> tools/list -> tools/call, over stdio, against the INSTALLED
    entrypoint.

    stdin stays open until the last answer is read. ``_close_stdin_early``
    exists only so the tests can show that closing it fabricates a failure — a
    tool call is the most network-bound thing a server does, so this is where
    the trap bites hardest.
    """
    run_env = dict(os.environ if env is None else env)
    run_env["PYTHONUNBUFFERED"] = "1"
    out: dict[str, Any] = {"tools": None, "listing": None, "call": None, "error": ""}
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=run_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        out["error"] = (
            f"could not start the installed entrypoint: {type(exc).__name__}: {exc}"
        )
        return out

    q: Queue[str | None] = Queue()
    tbp._reader_thread(proc.stdout, q)
    err_q: Queue[str | None] = Queue()
    tbp._reader_thread(proc.stderr, err_q)
    err_lines: list[str] = []
    deadline = time.monotonic() + timeout

    def send(msg: dict[str, Any]) -> bool:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def await_id(ident: int) -> dict[str, Any] | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = q.get(timeout=min(remaining, 0.5))
            except Empty:
                if proc.poll() is not None:
                    return None
                continue
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == ident:
                return msg

    try:
        if not send(tbp._rpc("initialize", 1, tbp._initialize_params())):
            out["error"] = (
                "the entrypoint closed stdin before initialize; stderr: "
                + tbp._tail(tbp._drain(err_q, err_lines))
            )
            return out
        # THE TRAP. Production callers never set this.
        if _close_stdin_early and proc.stdin is not None:
            proc.stdin.close()

        init = await_id(1)
        if init is None or "error" in init:
            out["error"] = "initialize did not succeed; stderr: " + tbp._tail(
                tbp._drain(err_q, err_lines)
            )
            return out

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        if not send(tbp._rpc("tools/list", 2)):
            out["error"] = "the entrypoint went away before tools/list"
            return out
        listing = await_id(2)
        if listing is None or "error" in listing:
            out["error"] = "tools/list did not succeed; stderr: " + tbp._tail(
                tbp._drain(err_q, err_lines)
            )
            return out
        tools = (
            ((listing.get("result") or {}).get("tools") or [])
            if isinstance(listing.get("result"), dict)
            else []
        )
        out["tools"] = tools
        out["listing"] = listing

        if tool:
            send(
                tbp._rpc("tools/call", 3, {"name": tool, "arguments": tool_args or {}})
            )
            out["call"] = await_id(3)
        return out
    finally:
        tbp._terminate(proc)
        tbp._close_streams(proc)


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------


def probe(
    dist: str,
    target: Path,
    *,
    tool: str = "",
    tool_args: dict[str, Any] | None = None,
    index_url: str = DEFAULT_INDEX,
    install_timeout: float = DEFAULT_INSTALL_TIMEOUT,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
    metadata_only: bool = False,
    offline: bool = False,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    user_facing_age: float = DEFAULT_USER_FACING_AGE_DAYS,
    pin_version: str = "",
    now: datetime | None = None,
    installer: Callable[..., Installed] | None = None,
    speaker: Callable[..., dict[str, Any]] | None = None,
) -> Report:
    """Everything, wired, in two phases.

    PHASE 1 always runs and costs two HTTP requests plus some git: what the
    index serves, which releases are withdrawn, and how far the repository has
    drifted past its last release. PHASE 2 builds a venv, installs the
    distribution and speaks MCP to it, and is skipped by ``--metadata-only``.

    The split is the whole point of keeping one entry point rather than two
    scripts: the same report, the same finding vocabulary and the same exit
    codes, at whichever cost the caller can afford. A nightly gate wants both.
    A pre-release check wants the first, and should not have to pay for a venv
    to ask whether a tag landed.

    The three injectable seams are the three impure parts: the index lookup,
    the install, and the subprocess.
    """
    report = Report(
        dist=dist, index_url=index_url, depth="metadata" if metadata_only else "full"
    )
    installer = installer or install_from_index
    speaker = speaker or speak_mcp

    # ---- PHASE 1 ----------------------------------------------------------
    # The repository side, read-only. `read_project` returns the [project] TABLE
    # (not the whole document) and raises when there is no pyproject at all — a
    # target we cannot read a version from still deserves the index comparison,
    # so an absent version is left empty rather than made fatal.
    try:
        project = read_project(target)
    except OSError:
        project = {}
    report.versions.repo = str(project.get("version") or "")
    # `release_tags` sorts NEWEST FIRST (`--sort=-v:refname`), so the latest tag
    # is [0]. Taking [-1] would compare the index against the OLDEST release the
    # repository ever cut, which is always behind and always "a finding".
    report.tags = release_tags(target)
    report.versions.tag = report.tags[0] if report.tags else ""
    report.unreleased_commits = commits_since(target, report.versions.tag or None)
    report.changelog_unreleased_entries = count_changelog_unreleased(target)

    read_index(report, target, 20.0, offline)
    report.findings = metadata_findings(
        report, target, report.versions.repo, max_age_days, now, user_facing_age
    )

    if report.index_status == "unreachable":
        # A comparison that did not happen is not a pass. 127, not 0 and not 2.
        report.harness_error = (
            f"the index could not be reached ({report.index_detail}) — the "
            "published artifact was NOT compared. This is not 'in sync'"
        )
        return report

    if metadata_only or offline:
        return report

    # ---- PHASE 2 ----------------------------------------------------------
    # "Does it exist on the index at all?" was already answered in phase 1 and is
    # NOT asked again. Two reads meant two chances to disagree with each other,
    # and one of them would then be describing a state the other never saw.
    index_version = report.index_version
    if report.index_status == "not_published":
        report.publication = NOT_PUBLISHED
        report.findings = dedupe(report.findings + build_findings(report))
        return report

    # Phase 1's findings are carried, never replaced: a yanked release is still
    # yanked once the install has run, and losing it here would make the deeper
    # run report LESS than the cheap one.
    phase1 = list(report.findings)

    with tempfile.TemporaryDirectory(prefix="shipped-probe-") as tmp:
        got = installer(dist, Path(tmp), index_url, install_timeout, pin_version)
        if not got.ok:
            report.publication = INSTALL_FAILED
            report.findings = dedupe(
                phase1
                + build_findings(report)
                + [Finding("INSTALL_DETAIL", got.detail)]
            )
            return report

        report.versions.installed = got.version or (index_version or "")
        report.entrypoint = got.entrypoint
        if pin_version and report.versions.installed != pin_version:
            # The pin is the whole point of a post-release re-check; a venv that
            # came back holding something else has verified a different release.
            report.harness_error = (
                f"pinned to =={pin_version} and the venv reports "
                f"{report.versions.installed or 'nothing'} — the artifact under test "
                "is not the one named, so no claim is made about either"
            )
            return report

        compare_content(report, got, target)

        if not got.entrypoint:
            report.findings = dedupe(phase1 + build_findings(report))
            return report

        spoke = speaker([got.entrypoint], run_timeout, Path(tmp), tool, tool_args)
        if spoke.get("error") or spoke.get("tools") is None:
            report.findings = dedupe(
                phase1
                + build_findings(report)
                + [
                    Finding(
                        "RUN_DETAIL", str(spoke.get("error") or "no tools/list answer")
                    )
                ]
            )
            return report

        tools = spoke["tools"] or []
        report.tools = len(tools)
        chosen, why = pick_tool(tools, tool)
        if not chosen:
            report.tool_call = ToolCall(ran=False, status="skipped", detail=why)
        elif spoke.get("call") is None and chosen != tool:
            # tools/list came back, but the caller did not name a tool, so the
            # call has to be made now that we know which one is argument-free.
            again = speaker([got.entrypoint], run_timeout, Path(tmp), chosen, tool_args)
            report.tool_call = classify_tool_result(again.get("call"))
            report.tool_call.name = chosen
        else:
            report.tool_call = classify_tool_result(spoke.get("call"))
            report.tool_call.name = chosen

        report.findings = dedupe(phase1 + build_findings(report))
    return report


_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def render(r: Report) -> str:
    lines = [f"# Shipped probe — `{r.dist}`", ""]
    if r.harness_error:
        lines += [f"⛔ {r.harness_error}", ""]
        return "\n".join(lines) + "\n"
    icon = "✅" if not r.findings else "🚨"
    lines += [f"{icon} publication: **{r.publication}**", ""]

    # Phase 1 — always present.
    lines += [
        f"- index:                    `{r.index_url}`",
        f"- index serves:             `{r.index_version or '—'}`",
        f"- repository version:       `{r.versions.repo or '—'}`",
        f"- last git tag:             `{r.versions.tag or '—'}`",
        f"- unreleased commits:       {len(r.unreleased_commits)}"
        + (
            f" (oldest {r.oldest_unreleased_age_days:.1f} d)"
            if r.oldest_unreleased_age_days is not None
            else ""
        ),
    ]
    if r.depth == "full":
        lines += [
            f"- installed from the index: `{r.versions.installed or '—'}`",
            f"- entrypoint:               "
            f"`{Path(r.entrypoint).name if r.entrypoint else '—'}`",
            f"- tools listed:             {r.tools if r.tools is not None else '—'}",
            f"- tool call:                {r.tool_call.name or '—'} → {r.tool_call.status}",
        ]
        if r.tool_call.detail:
            lines.append(f"  ({r.tool_call.detail})")

    # The states that are neither a pass nor a finding, and must not be read as
    # either. Same reasoning as the boot gate's `not-selected`.
    if r.index_status == "unconfirmed":
        lines += ["", f"⚠️ UNCONFIRMED — {r.index_detail}."]
    elif r.index_status == "skipped" or r.index_detail:
        lines += ["", f"ℹ️ {r.index_detail}."]
    if r.yank_source == "unconfirmed":
        lines += ["", f"⚠️ UNCONFIRMED — {r.yank_detail}."]
    elif r.yank_source == "json-fallback":
        lines += ["", f"ℹ️ {r.yank_detail}."]
    if r.tags is None:
        lines += [
            "",
            "ℹ️ tags could not be listed (a `--depth 1` clone fetches none) — "
            "'no releases' cannot be concluded from this.",
        ]
    if r.depth == "full":
        if r.tree.checked:
            extra = (
                f", {len(r.tree.extra_in_artifact)} file(s) only in the artifact "
                "(a generated version module lives here)"
                if r.tree.extra_in_artifact
                else ""
            )
            lines += [
                "",
                f"ℹ️ content: {r.tree.compared} source file(s) compared "
                f"against the checkout{extra}"
                + (" — TRUNCATED at the cap" if r.tree.truncated else "")
                + ".",
            ]
        elif r.tree.detail:
            lines += [
                "",
                f"ℹ️ content comparison not made — {r.tree.detail.rstrip('; ')}.",
            ]
    if r.yanked:
        shown = sorted(r.yanked, key=lambda v: release_key(v) or ())
        lines += [
            "",
            f"ℹ️ {len(shown)} yanked release(s) on the index: "
            f"{', '.join(shown)}. Older withdrawn releases are history, not "
            "a finding — only the release this repository treats as current.",
        ]

    if r.findings:
        lines += ["", "## 🚨 Findings"]
        lines += [
            f"- **{f.code}** [{f.severity}] — {f.detail}"
            for f in sorted(r.findings, key=lambda f: _SEVERITY_RANK.get(f.severity, 3))
        ]
    elif r.depth == "metadata":
        lines += [
            "",
            "The release metadata is consistent. The artifact itself was NOT "
            "installed or run — `--metadata-only` stops before that.",
        ]
    else:
        lines += ["", "The artifact users install matches the repository and runs."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--dist",
        default="",
        help="distribution name on the index (default: the name in pyproject.toml)",
    )
    p.add_argument("--target", default=".", help="the target checkout (read-only)")
    p.add_argument(
        "--tool",
        default="",
        help="tool to call (default: the first one needing no arguments)",
    )
    p.add_argument("--tool-args", default="{}", help="JSON arguments for --tool")
    p.add_argument(
        "--index-url",
        default=DEFAULT_INDEX,
        help="PEP 503 index to compare against, as pip takes it "
        f"(default: {DEFAULT_INDEX}). Against anything but PyPI the "
        "JSON API cross-check does not run — there is none to run",
    )
    p.add_argument(
        "--metadata-only",
        action="store_true",
        help="stop after the index and git comparison: no venv, no "
        "install, no tool call. Two HTTP requests instead of minutes",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="git-only, and the report says so. Implies --metadata-only",
    )
    p.add_argument(
        "--max-age-days",
        type=float,
        default=DEFAULT_MAX_AGE_DAYS,
        help="how long unreleased HOUSEKEEPING may sit before it is a "
        f"finding (default: {DEFAULT_MAX_AGE_DAYS:g})",
    )
    p.add_argument(
        "--max-age-days-user-facing",
        type=float,
        default=DEFAULT_USER_FACING_AGE_DAYS,
        help="the same clock for unreleased fix/feat/perf/revert work "
        f"(default: {DEFAULT_USER_FACING_AGE_DAYS:g} — reported "
        "immediately). A breaking change is reported at any age and "
        "ignores both",
    )
    p.add_argument(
        "--pin-version",
        default="",
        help="install `dist==VERSION` instead of whatever the index "
        "resolves to. Use it for every re-check after a release: an "
        "unpinned install was measured serving the previous artifact "
        "for minutes after the new one was listed, --no-cache-dir and "
        "all",
    )
    p.add_argument("--install-timeout", type=float, default=DEFAULT_INSTALL_TIMEOUT)
    p.add_argument("--run-timeout", type=float, default=DEFAULT_RUN_TIMEOUT)
    p.add_argument(
        "--report", default="", help="write the machine-readable report here"
    )
    p.add_argument("--format", choices=("text", "json"), default="text")
    args = p.parse_args(argv)

    try:
        tool_args = json.loads(args.tool_args or "{}")
        if not isinstance(tool_args, dict):
            raise ValueError("--tool-args must be a JSON object")
    except ValueError as exc:
        print(f"shipped: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    if not shutil.which("git"):
        print(
            "shipped: git not found — the tag comparison cannot be made",
            file=sys.stderr,
        )

    target = Path(args.target).resolve()
    dist = args.dist
    if not dist:
        # `release_gap.py` never needed --dist; it read the name out of the
        # target. Keeping that is what lets the metadata depth stay a one-flag
        # invocation rather than making every caller repeat what pyproject says.
        try:
            dist = str(read_project(target).get("name") or "")
        except OSError:
            dist = ""
        if not dist:
            print(
                f"shipped: no --dist and no [project] name in {target}/pyproject.toml",
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN

    try:
        report = probe(
            dist,
            target,
            tool=args.tool,
            tool_args=tool_args,
            index_url=args.index_url,
            metadata_only=args.metadata_only or args.offline,
            offline=args.offline,
            max_age_days=args.max_age_days,
            user_facing_age=args.max_age_days_user_facing,
            pin_version=args.pin_version,
            install_timeout=args.install_timeout,
            run_timeout=args.run_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - the harness itself failed
        print(
            f"shipped: harness could not run: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    print(
        json.dumps(report.as_dict(), indent=2, sort_keys=True)
        if args.format == "json"
        else render(report)
    )
    if args.report:
        try:
            Path(args.report).write_text(
                json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"shipped: could not write {args.report}: {exc}", file=sys.stderr)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
