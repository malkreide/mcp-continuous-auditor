#!/usr/bin/env python3
"""Tests for scripts/yank_probe.py — is a known-broken release still installable?

The probe is three network seams (the project page, a release's core metadata,
a dependency's version list) with every decision outside them, so these tests
own the decisions and inject the seams.

What is injected is not invented. ``tests/fixtures/pypi/`` carries the real
bytes: the captured Simple API file list for ``zurich-opendata-mcp``, the
captured ``Requires-Dist`` header block of all eight releases, and the captured
version lists of their dependencies. The single derived scenario — the six
predecessors before they were yanked — flips exactly the six yank flags and
changes nothing else, which is the same discipline ``fixtures/pypi/README.md``
sets out for the lag fixtures.

Stdlib-only and offline: no index is contacted.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import shipped_probe as sp  # noqa: E402
import yank_probe as yp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pypi"
DIST = "zurich-opendata-mcp"

SIMPLE = json.loads((FIXTURES / "zurich_simple_files.json").read_text(encoding="utf-8"))
HEADERS = json.loads((FIXTURES / "zurich_core_metadata_headers.json").read_text(encoding="utf-8"))
DEP_VERSIONS = json.loads((FIXTURES / "dependency_versions.json").read_text(encoding="utf-8"))

# The six releases that carried an uncapped `mcp` range. Named here rather than
# derived, because "a probe that only checked latest-1 would have found one of
# these" is the property under test and deriving the list from the probe's own
# logic would make the assertion circular.
BROKEN_SIX = ("0.2.0", "0.3.0", "0.3.3", "0.4.0", "0.5.0", "0.5.1")


def codes(findings: list[yp.Finding]) -> list[str]:
    return [f.code for f in findings]


def by_code(findings: list[yp.Finding], code: str) -> yp.Finding:
    return next(f for f in findings if f.code == code)


def simple_payload(
    yanked: dict[str, Any] | None = None,
    only: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """The captured project page, with the yank flags set per scenario.

    ``yanked`` maps a version to its PEP 592 flag (``True``, or a string reason);
    anything unlisted is healthy. ``only`` restricts the catalogue to a subset of
    versions, which is how the state at a past date is reconstructed.
    """
    yanked = yanked or {}
    files = []
    versions = []
    for entry in SIMPLE["files"]:
        version = sp.version_from_filename(entry["filename"], DIST)
        if only is not None and version not in only:
            continue
        files.append({**entry, "yanked": yanked.get(version, False)})
    for version in SIMPLE["versions"]:
        if only is None or version in only:
            versions.append(version)
    return {"name": SIMPLE["name"], "versions": versions, "files": files}


class Harness:
    """Installs fakes over the three network seams for the duration of a run."""

    def __init__(
        self,
        payload: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
        dependencies: dict[str, list[str]] | None = None,
        status: str = "ok",
        detail: str = "",
    ) -> None:
        self.payload = payload
        self.headers = HEADERS if headers is None else headers
        self.dependencies = DEP_VERSIONS if dependencies is None else dependencies
        self.status = status
        self.detail = detail
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> Harness:
        self._saved = {
            name: getattr(yp, name)
            for name in ("fetch_project", "fetch_core_metadata",
                         "fetch_dependency_versions", "fetch_pypi_requires")
        }
        yp.fetch_project = lambda dist, index_url, timeout: (
            self.payload, self.status, self.detail)
        # The JSON fallback is a FOURTH seam and stubbing it is not optional:
        # the index under test is pypi.org, so a release whose core metadata is
        # unreadable falls through to it. Left unstubbed, the two "unreadable
        # metadata" tests below quietly reach the real network and pass for the
        # wrong reason — they were doing exactly that until this line existed.
        yp.fetch_pypi_requires = lambda dist, version, timeout: (
            None, "the JSON API fallback is not stubbed in this test")

        def core_metadata(entry: dict[str, Any], timeout: float) -> tuple[str | None, str]:
            version = sp.version_from_filename(str(entry.get("filename", "")), DIST)
            body = self.headers.get(version or "")
            return (body, "") if body is not None else (None, "no core metadata recorded")

        yp.fetch_core_metadata = core_metadata
        yp.fetch_dependency_versions = (
            lambda name, index_url, timeout: self.dependencies.get(name))
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, original in self._saved.items():
            setattr(yp, name, original)


def run(**kwargs: Any) -> yp.Report:
    with Harness(**kwargs):
        return yp.run(DIST, index_url="https://pypi.org/simple", timeout=1.0)


# ---------------------------------------------------------------------------
# The incident — all six, not latest-1
# ---------------------------------------------------------------------------

class TheIncidentTest(unittest.TestCase):
    """The state of the index on 2026-07-31: 0.6.0 shipped, nothing yanked yet."""

    def setUp(self) -> None:
        self.report = run(payload=simple_payload(
            only=("0.2.0", "0.3.0", "0.3.3", "0.4.0", "0.5.0", "0.5.1", "0.6.0")))

    def test_the_broken_releases_are_found(self) -> None:
        self.assertIn("UNYANKED_BROKEN_RELEASE", codes(self.report.findings))
        self.assertEqual(self.report.exit_code(), yp.EXIT_FINDINGS)

    def test_all_six_predecessors_are_named_not_just_the_previous_one(self) -> None:
        # The property the whole probe exists for. A check that looked at
        # latest-1 would have reported 0.5.1 and called the catalogue clean,
        # leaving five installable broken releases behind.
        finding = by_code(self.report.findings, "UNYANKED_BROKEN_RELEASE")
        self.assertEqual(finding.versions, BROKEN_SIX)
        for version in BROKEN_SIX:
            self.assertIn(version, finding.detail)

    def test_the_healthy_successor_is_the_one_that_fixed_it(self) -> None:
        self.assertEqual(self.report.reference, "0.6.0")
        self.assertNotIn("0.6.0", by_code(
            self.report.findings, "UNYANKED_BROKEN_RELEASE").versions)

    def test_the_dependency_and_the_boundary_are_named(self) -> None:
        detail = by_code(self.report.findings, "UNYANKED_BROKEN_RELEASE").detail
        self.assertIn("mcp", detail)
        self.assertIn("1.x -> 2.x", detail)
        # The corroborating evidence — the successor's own pin — has to be in
        # the text, because it is what licenses the word "broken".
        self.assertIn("mcp[cli]<3,>=2.0.0", detail)

    def test_it_recommends_a_yank_and_explicitly_not_a_deletion(self) -> None:
        detail = by_code(self.report.findings, "UNYANKED_BROKEN_RELEASE").detail
        self.assertIn("yank", detail.lower())
        self.assertIn("not a deletion", detail.lower())
        self.assertIn("do not delete", detail.lower())

    def test_it_says_superseding_is_not_enough(self) -> None:
        detail = by_code(self.report.findings, "UNYANKED_BROKEN_RELEASE").detail
        self.assertIn("lockfile", detail.lower())

    def test_only_mcp_is_reported_not_every_uncapped_dependency(self) -> None:
        # httpx, pydantic, sqlparse, uvicorn and defusedxml are all declared
        # uncapped in the same releases. None of them crossed a major boundary
        # their successor excluded, so none of them is a finding — the gate has
        # to stay quiet about them or it gets muted.
        self.assertEqual(
            [f.code for f in self.report.findings].count("UNYANKED_BROKEN_RELEASE"), 1)


# ---------------------------------------------------------------------------
# After the yank — the fix worked, and the reason did not come with it
# ---------------------------------------------------------------------------

class AfterTheYankTest(unittest.TestCase):
    """The captured state: the six are yanked, 0.6.0 and 0.7.0 are healthy."""

    def setUp(self) -> None:
        self.report = run(payload=simple_payload(
            yanked={v: True for v in BROKEN_SIX}))

    def test_the_broken_release_finding_is_gone(self) -> None:
        self.assertNotIn("UNYANKED_BROKEN_RELEASE", codes(self.report.findings))

    def test_a_yank_with_no_reason_is_its_own_lower_finding(self) -> None:
        finding = by_code(self.report.findings, "YANK_REASON_MISSING")
        self.assertEqual(finding.severity, "low")
        self.assertEqual(finding.versions, BROKEN_SIX)

    def test_the_reason_finding_names_what_pip_actually_prints(self) -> None:
        # This is the only string the affected audience ever sees.
        self.assertIn("<none given>", by_code(
            self.report.findings, "YANK_REASON_MISSING").detail)

    def test_a_yank_carrying_a_reason_is_silent(self) -> None:
        report = run(payload=simple_payload(
            yanked={v: "broken with mcp 2.x; fixed in 0.6.0" for v in BROKEN_SIX}))
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), yp.EXIT_GREEN)

    def test_a_partial_yank_leaves_the_version_installable(self) -> None:
        # PEP 592 yanks FILES. A version with one live file left still installs,
        # so it must still be reported — the inverse mistake would hide it.
        payload = simple_payload(yanked={v: True for v in BROKEN_SIX})
        for entry in payload["files"]:
            if entry["filename"].endswith(".whl") and "0.5.1" in entry["filename"]:
                entry["yanked"] = False
        report = run(payload=payload)
        finding = by_code(report.findings, "UNYANKED_BROKEN_RELEASE")
        self.assertEqual(finding.versions, ("0.5.1",))


# ---------------------------------------------------------------------------
# The four conditions — each one, removed, silences the finding
# ---------------------------------------------------------------------------

class ConservatismTest(unittest.TestCase):
    def test_an_upper_bound_is_enough_to_stay_silent(self) -> None:
        headers = dict(HEADERS)
        for version in BROKEN_SIX:
            headers[version] = headers[version].replace(
                "Requires-Dist: mcp[cli]>=1", "Requires-Dist: mcp[cli]<2,>=1")
        report = run(payload=simple_payload(), headers=headers)
        self.assertNotIn("UNYANKED_BROKEN_RELEASE", codes(report.findings))

    def test_a_successor_that_still_admits_the_old_floor_is_not_corroboration(self) -> None:
        # Without the successor excluding the floor, an uncapped range is a RISK
        # and not a finding — nobody has established the crossing breaks anything.
        headers = dict(HEADERS)
        for version in ("0.6.0", "0.7.0"):
            headers[version] = headers[version].replace(
                "Requires-Dist: mcp[cli]<3,>=2.0.0", "Requires-Dist: mcp[cli]>=1.0.0")
        report = run(payload=simple_payload(), headers=headers)
        self.assertNotIn("UNYANKED_BROKEN_RELEASE", codes(report.findings))

    def test_a_dependency_that_never_shipped_past_the_boundary_is_silent(self) -> None:
        # If mcp 2.x does not exist, nothing resolves across the boundary and
        # there is nothing to yank. The break has to be reachable to be real.
        deps = dict(DEP_VERSIONS)
        deps["mcp"] = [v for v in deps["mcp"] if not v.startswith("2.")]
        report = run(payload=simple_payload(), dependencies=deps)
        self.assertNotIn("UNYANKED_BROKEN_RELEASE", codes(report.findings))

    def test_only_a_prerelease_past_the_boundary_is_silent(self) -> None:
        # pip does not select a pre-release unless asked, so 2.0.0rc1 alone is
        # not something a plain install resolves to.
        deps = dict(DEP_VERSIONS)
        deps["mcp"] = [v for v in deps["mcp"] if not v.startswith("2.") or "0a" in v
                       or "0b" in v or "rc" in v]
        report = run(payload=simple_payload(), dependencies=deps)
        self.assertNotIn("UNYANKED_BROKEN_RELEASE", codes(report.findings))

    def test_an_already_yanked_release_is_not_reported_again(self) -> None:
        report = run(payload=simple_payload(yanked={v: "x" for v in BROKEN_SIX}))
        self.assertEqual(report.findings, [])

    def test_dev_extras_are_never_a_finding(self) -> None:
        # `ruff>=0.15.12; extra == 'dev'` is uncapped in every release and is not
        # installed by a plain `pip install`. A broken dev dependency breaks
        # nobody's server.
        for release in run(payload=simple_payload()).releases:
            for name in release.declared():
                self.assertNotIn(name, {"ruff", "mypy", "pytest", "pytest-cov",
                                        "pytest-asyncio", "respx"})


# ---------------------------------------------------------------------------
# A comparison that did not happen is never a pass
# ---------------------------------------------------------------------------

class RefusalsTest(unittest.TestCase):
    def test_an_unreachable_index_is_a_harness_error_not_a_clean_run(self) -> None:
        report = run(payload=None, status="unreachable", detail="index unreachable: boom")
        self.assertEqual(report.exit_code(), yp.EXIT_CANNOT_RUN)
        self.assertIn("boom", report.harness_error)

    def test_a_never_published_distribution_makes_no_claim(self) -> None:
        # shipped_probe already reports NOT_ON_INDEX for this; saying it twice
        # would make one problem look like two.
        report = run(payload=None, status="not_published", detail="404")
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), yp.EXIT_GREEN)
        self.assertIn("nothing to audit", report.index_detail)

    def test_an_unreadable_successor_stops_the_audit(self) -> None:
        # Every comparison is against the successor. Without its metadata the
        # probe knows nothing, and "knows nothing" must not read as "clean".
        headers = {k: v for k, v in HEADERS.items() if k != "0.7.0"}
        report = run(payload=simple_payload(), headers=headers)
        self.assertEqual(report.exit_code(), yp.EXIT_CANNOT_RUN)
        self.assertIn("0.7.0", report.harness_error)

    def test_an_unreadable_predecessor_narrows_the_audit_and_says_so(self) -> None:
        headers = {k: v for k, v in HEADERS.items() if k != "0.3.3"}
        report = run(payload=simple_payload(), headers=headers)
        self.assertIn("0.3.3", report.unreadable)
        self.assertIn("not audited, not clean", yp.render(report))

    def test_a_catalogue_with_no_healthy_release_makes_no_claim(self) -> None:
        report = run(payload=simple_payload(
            yanked={v: True for v in SIMPLE["versions"]}))
        self.assertIsNone(report.reference)
        self.assertNotIn("UNYANKED_BROKEN_RELEASE", codes(report.findings))
        self.assertIn("no successor", report.index_detail)

    def test_truncation_is_reported_rather_than_read_as_clean(self) -> None:
        with Harness(payload=simple_payload()):
            report = yp.run(DIST, timeout=1.0, max_versions=2)
        self.assertEqual(report.truncated, 6)
        self.assertIn("NOT AUDITED", yp.render(report))


# ---------------------------------------------------------------------------
# The metadata parser — the folded-License trap
# ---------------------------------------------------------------------------

class MetadataParserTest(unittest.TestCase):
    def test_a_folded_license_does_not_end_the_header_block(self) -> None:
        # Regression, measured against the real bytes: PyPI inlines the whole
        # MIT licence as a folded `License:` header, and the blank lines inside
        # it arrive as whitespace-only continuations. Treating one of those as
        # the end of the headers reads six dependencies as zero — which this
        # probe would then report as a clean catalogue.
        parsed = yp.parse_metadata(HEADERS["0.6.0"])
        self.assertIn("License", HEADERS["0.6.0"])
        self.assertIn("\n        \n", HEADERS["0.6.0"])
        names = {r.key for r in parsed if not r.conditional}
        self.assertEqual(
            names, {"defusedxml", "httpx", "mcp", "pydantic", "sqlparse", "uvicorn"})

    def test_the_description_below_the_headers_is_not_parsed(self) -> None:
        text = "Name: x\nRequires-Dist: a>=1\n\nRequires-Dist: not-a-header\n"
        self.assertEqual([r.name for r in yp.parse_metadata(text)], ["a"])

    def test_a_continuation_of_a_foreign_header_is_not_a_requirement(self) -> None:
        text = "License: MIT\n    Requires-Dist: fake>=1\nRequires-Dist: real>=1\n"
        self.assertEqual([r.name for r in yp.parse_metadata(text)], ["real"])

    def test_extras_and_markers_survive_the_round_trip(self) -> None:
        req = yp.parse_requirement("mcp[cli]>=1.28.1 ; python_version < \"3.13\"")
        self.assertEqual((req.key, req.extras), ("mcp", "cli"))
        self.assertTrue(req.conditional)

    def test_a_url_requirement_states_no_range_and_is_dropped(self) -> None:
        self.assertIsNone(yp.parse_requirement("thing @ https://example.com/x.whl"))


# ---------------------------------------------------------------------------
# PEP 440, the subset that decides something
# ---------------------------------------------------------------------------

class SpecifierTest(unittest.TestCase):
    def admits(self, spec: str, version: str) -> bool | None:
        req = yp.parse_requirement(f"x{spec}")
        assert req is not None
        return req.admits(version)

    def test_zero_padding_makes_1_28_and_1_28_0_the_same_version(self) -> None:
        # Raw tuple comparison orders (1,28) before (1,28,0) and would put a
        # candidate exactly ON its floor below it. That is a boundary error
        # precisely where every decision here is made.
        self.assertTrue(self.admits(">=1.28", "1.28.0"))
        self.assertTrue(self.admits(">=1.28.0", "1.28"))
        self.assertTrue(self.admits("==1.28", "1.28.0"))

    def test_compatible_release_is_an_upper_bound(self) -> None:
        req = yp.parse_requirement("x~=2.1")
        assert req is not None
        self.assertTrue(req.bounded_above())
        self.assertTrue(req.admits("2.9"))
        self.assertFalse(req.admits("3.0"))
        self.assertFalse(req.admits("2.0"))

    def test_a_wildcard_equality_is_an_upper_bound(self) -> None:
        req = yp.parse_requirement("x==2.*")
        assert req is not None
        self.assertTrue(req.bounded_above())
        self.assertTrue(req.admits("2.7.1"))
        self.assertFalse(req.admits("3.0"))

    def test_an_exclusion_is_not_an_upper_bound(self) -> None:
        req = yp.parse_requirement("x>=1.0,!=1.5")
        assert req is not None
        self.assertFalse(req.bounded_above())
        self.assertFalse(req.admits("1.5"))
        self.assertTrue(req.admits("9.0"))

    def test_the_floor_is_the_highest_lower_bound_stated(self) -> None:
        req = yp.parse_requirement("x>=1.0,>1.28.1")
        assert req is not None
        self.assertEqual(req.floor(), "1.28.1")

    def test_an_unparseable_version_is_undecidable_not_false(self) -> None:
        # Answering False would silently exclude it from the comparison and let
        # a finding be built on an evaluation that never happened.
        self.assertIsNone(self.admits(">=nonsense", "1.0"))
        self.assertIsNone(self.admits(">=1.0", "nonsense"))

    def test_no_clauses_admits_everything(self) -> None:
        req = yp.parse_requirement("x")
        assert req is not None
        self.assertFalse(req.bounded_above())
        self.assertIsNone(req.floor())
        self.assertTrue(req.admits("9.9.9"))

    def test_newest_release_skips_prereleases(self) -> None:
        self.assertEqual(
            yp.newest_release(["1.29.0", "2.0.0a1", "2.0.0rc1", "2.0.0"]), "2.0.0")
        self.assertEqual(yp.newest_release(["1.29.0", "2.0.0rc1"]), "1.29.0")
        self.assertIsNone(yp.newest_release([]))


# ---------------------------------------------------------------------------
# The probe proposes; it does not act
# ---------------------------------------------------------------------------

class ReadOnlyTest(unittest.TestCase):
    """The probe recommends a yank. It must never be able to perform one.

    Yanking needs a PyPI token with upload scope, it changes what every
    resolver on the internet sees, and whether a release was really unusable is
    the maintainer's judgement. An auditor holding that credential is a much
    larger blast radius than one that writes a sentence — so the boundary is
    pinned by a test rather than left to review.
    """

    SOURCE = (Path(__file__).resolve().parents[1] / "scripts" / "yank_probe.py"
              ).read_text(encoding="utf-8")

    def test_no_command_line_option_performs_a_yank(self) -> None:
        options = {
            option
            for action in yp.build_parser()._actions
            for option in action.option_strings
        }
        self.assertEqual(
            options & {"--yank", "--apply", "--fix", "--unyank", "--delete"}, set())

    def test_the_probe_only_ever_issues_get_requests(self) -> None:
        # urllib sends a GET unless it is given a body or an explicit method.
        # Neither appears here, and no credential is read from anywhere.
        self.assertNotIn("method=", self.SOURCE)
        self.assertNotIn("data=", self.SOURCE)
        self.assertNotIn("Authorization", self.SOURCE)
        self.assertNotIn("getenv", self.SOURCE)

    def test_the_recommendation_hands_the_action_to_a_human(self) -> None:
        report = run(payload=simple_payload(
            only=("0.5.1", "0.6.0")))
        detail = by_code(report.findings, "UNYANKED_BROKEN_RELEASE").detail
        self.assertIn("RECOMMENDED", detail)
        self.assertIn("does not and will not perform the yank", detail)


if __name__ == "__main__":
    unittest.main()
