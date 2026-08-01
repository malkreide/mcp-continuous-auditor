#!/usr/bin/env python3
"""Tests for the release-gap layer.

The failure guarded against here is the one no other check in this repo can
see: a repository that is green, audited and fixed, while the artifact users
install is none of those things. ``meteoswiss-mcp`` shipped an import error to
every fresh install for three days with ``main`` already corrected.

Two properties matter more than the individual detections and are tested
hardest, because getting them wrong turns the script into noise or into a lie:

* **an unreachable PyPI must never read as "in sync"** — a check that degrades
  into a plausible success is the exact failure it exists to catch;
* **a shallow clone must not read as "never released"** — no tags is unknown,
  not zero.

A third property joined them once the probe learned to read both of PyPI's
index APIs:

* **two disagreeing indexes must produce neither a finding nor a clean bill** —
  the divergence is a propagation window, and an auditor that fires on it gets
  muted.

Stdlib-only (``python3 -m unittest``), matching the rest of the repo's tooling.
Git-backed cases build a real repository in a temp dir: mocking ``git log``
would only assert that the mock matches the assumption. Index responses ARE
recorded rather than live — see ``tests/fixtures/pypi/README.md`` for what each
payload is and which parts of it are captured versus reconstructed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_gap as rg  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pypi"
DIST = "zurich-opendata-mcp"


PYPROJECT = """\
[project]
name = "demo-mcp"
version = "{version}"
"""


def payload(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def simple_payload(versions, yanked=(), dist="demo-mcp"):
    """A minimal PEP 691/700 Simple-API response, in the shape PyPI serves."""
    stem = dist.replace("-", "_")
    return {
        "meta": {"api-version": "1.4"},
        "name": dist,
        "versions": list(versions),
        "files": [
            {"filename": f"{stem}-{v}{ext}", "yanked": (v in yanked)}
            for v in versions
            for ext in ("-py3-none-any.whl", ".tar.gz")
        ],
    }


def json_payload(versions, latest=None, yanked=(), dist="demo-mcp"):
    """A minimal JSON-API response. `latest` defaults to the last version given."""
    stem = dist.replace("-", "_")
    return {
        "info": {"name": dist, "version": latest or list(versions)[-1]},
        "releases": {
            v: [
                {"filename": f"{stem}-{v}.tar.gz", "yanked": (v in yanked), "yanked_reason": None}
            ]
            for v in versions
        },
    }


class stub_index:
    """Serve both index APIs from recorded payloads, for the duration of a block.

    Patched at ``_get`` — the single point where either fetcher touches the
    network — so the parsing, the yank attribution and the reconciliation all
    still run for real. Patching the fetchers themselves would leave the code
    under test unexercised and assert only that the stub matches the assumption.
    """

    def __init__(self, simple: dict | None = None, json_api: dict | None = None):
        self._simple, self._json = simple, json_api

    def _get(self, url: str, timeout: float, accept: str | None = None):
        served = self._simple if "/simple/" in url else self._json
        if served is None:
            return None, "unreachable", "PyPI unreachable: simulated"
        return served, "ok", ""

    def __enter__(self):
        self._orig = rg._get
        rg._get = self._get  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        rg._get = self._orig  # type: ignore[assignment]
        return False


def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def make_repo(tmp: Path, version: str = "0.2.0") -> Path:
    """A real git repo — see module docstring on why this is not mocked."""
    (tmp / "pyproject.toml").write_text(PYPROJECT.format(version=version), encoding="utf-8")
    run(tmp, "git", "init", "-q", "-b", "main")
    run(tmp, "git", "config", "user.email", "t@example.invalid")
    run(tmp, "git", "config", "user.name", "T")
    run(tmp, "git", "add", "-A")
    run(tmp, "git", "commit", "-q", "-m", "chore: init")
    return tmp


def commit(repo: Path, subject: str, days_ago: float = 0.0) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    (repo / f"f{abs(hash(subject)) % 10_000}.txt").write_text(subject, encoding="utf-8")
    run(repo, "git", "add", "-A")
    subprocess.run(
        ["git", "commit", "-q", "-m", subject, "--date", stamp],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**__import__("os").environ, "GIT_COMMITTER_DATE": stamp},
    )


class ReleaseKeyTest(unittest.TestCase):
    """Ordering is narrow on purpose; it must not guess when it cannot parse."""

    def test_plain_release_versions(self):
        self.assertEqual(rg.release_key("1.2.3"), (1, 2, 3))
        self.assertEqual(rg.release_key("v0.6.0"), (0, 6, 0))

    def test_orders_correctly(self):
        self.assertGreater(rg.release_key("0.6.0"), rg.release_key("0.5.0"))
        self.assertGreater(rg.release_key("1.0"), rg.release_key("0.99.99"))

    def test_unparseable_is_none_not_a_guess(self):
        self.assertIsNone(rg.release_key("not-a-version"))
        self.assertIsNone(rg.release_key(""))


class ChangelogTest(unittest.TestCase):
    def test_counts_only_the_unreleased_section(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CHANGELOG.md").write_text(
                "# Changelog\n\n"
                "## [Unreleased]\n\n"
                "### Behoben\n\n"
                "- eine offene Sache\n"
                "- noch eine\n\n"
                "## [0.1.0] - 2026-01-01\n\n"
                "- alt, zählt nicht\n",
                encoding="utf-8",
            )
            self.assertEqual(rg.count_changelog_unreleased(root), 2)

    def test_empty_unreleased_block(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n\n- alt\n",
                encoding="utf-8",
            )
            self.assertEqual(rg.count_changelog_unreleased(root), 0)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rg.count_changelog_unreleased(Path(d)), 0)


class UnreachablePypiTest(unittest.TestCase):
    """The property that matters most: silence is not a pass."""

    def test_unreachable_index_is_not_ok(self):
        with tempfile.TemporaryDirectory() as d, stub_index():
            repo = make_repo(Path(d))
            report = rg.probe(repo, max_age_days=7, offline=False, timeout=1)
            self.assertEqual(report.pypi_status, "unreachable")
            self.assertFalse(report.ok, "an unreachable index must not report success")

    def test_report_says_the_comparison_did_not_happen(self):
        with tempfile.TemporaryDirectory() as d, stub_index():
            repo = make_repo(Path(d))
            text = rg.render(rg.probe(repo, max_age_days=7, offline=False, timeout=1))
            self.assertIn("UNKNOWN", text)
            self.assertNotIn("release OK", text)

    def test_yank_status_is_unavailable_not_healthy(self):
        """No index means no yank answer — never "nothing is yanked"."""
        with tempfile.TemporaryDirectory() as d, stub_index():
            repo = make_repo(Path(d))
            report = rg.probe(repo, max_age_days=7, offline=False, timeout=1)
            self.assertEqual(report.yank_source, "unavailable")


class OfflineTest(unittest.TestCase):
    def test_offline_is_declared_and_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            report = rg.probe(repo, max_age_days=7, offline=True, timeout=1)
            self.assertEqual(report.pypi_status, "skipped")
            self.assertTrue(report.ok)
            self.assertIn("--offline", rg.render(report))


class PublishGapTest(unittest.TestCase):
    """A tag PyPI does not have — the maintainer already thinks it shipped."""

    def _probe_with_pypi(self, repo: Path, pypi_version: str):
        versions = sorted({"0.1.0", pypi_version}, key=lambda v: rg.release_key(v) or ())
        with stub_index(simple_payload(versions), json_payload(versions, pypi_version)):
            return rg.probe(repo, max_age_days=7, offline=False, timeout=1)

    def test_tag_ahead_of_pypi_is_high_severity(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.6.0")
            run(repo, "git", "tag", "v0.6.0")
            report = self._probe_with_pypi(repo, "0.5.0")
            codes = {f.code: f for f in report.findings}
            self.assertIn("PUBLISH_GAP", codes)
            self.assertEqual(codes["PUBLISH_GAP"].severity, "high")

    def test_tag_matching_pypi_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.6.0")
            run(repo, "git", "tag", "v0.6.0")
            report = self._probe_with_pypi(repo, "0.6.0")
            self.assertNotIn("PUBLISH_GAP", {f.code for f in report.findings})


class UnreleasedCommitsTest(unittest.TestCase):
    def _probe(self, repo: Path, max_age_days: float = 7.0):
        with stub_index(simple_payload(["0.1.0"]), json_payload(["0.1.0"])):
            return rg.probe(repo, max_age_days=max_age_days, offline=False, timeout=1)

    def test_recent_work_is_not_a_finding(self):
        """Every repo is ahead of PyPI right after a merge. That is not news."""
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.1.0")
            run(repo, "git", "tag", "v0.1.0")
            commit(repo, "fix: something", days_ago=0.5)
            report = self._probe(repo)
            self.assertNotIn("UNRELEASED", {f.code for f in report.findings})

    def test_aged_user_facing_work_is_high(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.1.0")
            run(repo, "git", "tag", "v0.1.0")
            commit(repo, "fix: a 404 on every station", days_ago=30)
            report = self._probe(repo)
            codes = {f.code: f for f in report.findings}
            self.assertIn("UNRELEASED", codes)
            self.assertEqual(codes["UNRELEASED"].severity, "high")
            self.assertIn("user-facing", codes["UNRELEASED"].detail)

    def test_aged_housekeeping_only_is_low(self):
        """`docs:` sitting unreleased is a different fact from `fix:`."""
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.1.0")
            run(repo, "git", "tag", "v0.1.0")
            commit(repo, "docs: tidy the readme", days_ago=30)
            report = self._probe(repo)
            codes = {f.code: f for f in report.findings}
            self.assertIn("UNRELEASED", codes)
            self.assertEqual(codes["UNRELEASED"].severity, "low")


class NoTagsTest(unittest.TestCase):
    """A --depth 1 clone fetches no tags. That is unknown, not zero."""

    def test_missing_tags_are_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            orig = rg.release_tags
            rg.release_tags = lambda root: None  # type: ignore[assignment]
            try:
                report = rg.probe(repo, max_age_days=7, offline=True, timeout=1)
            finally:
                rg.release_tags = orig  # type: ignore[assignment]
            self.assertIsNone(report.tags)
            self.assertIn("cannot be concluded", rg.render(report))


class ConvergedIndexTest(unittest.TestCase):
    """The recorded ground truth: both APIs agreeing, on a real distribution.

    Everything below asserts behaviour under divergence, which only means
    something if the agreeing case is quiet. This is that control.
    """

    def _probe(self, repo_version="0.7.0", tag="v0.7.0"):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version=repo_version)
            if tag:
                run(repo, "git", "tag", tag)
            with stub_index(
                payload("zurich_simple_converged"), payload("zurich_json_converged")
            ):
                return rg.probe(repo, max_age_days=7, offline=False, timeout=1)

    def test_agreement_is_ok_and_quiet(self):
        report = self._probe()
        self.assertEqual(report.pypi_status, "ok")
        self.assertEqual(report.yank_source, "simple")
        self.assertEqual(report.pypi_version, "0.7.0")
        self.assertEqual([f.code for f in report.findings], [])

    def test_historic_yanks_are_reported_without_being_a_finding(self):
        """0.2.0–0.5.1 are withdrawn. That is history, not a defect in 0.7.0."""
        report = self._probe()
        self.assertEqual(
            sorted(report.yanked), ["0.2.0", "0.3.0", "0.3.3", "0.4.0", "0.5.0", "0.5.1"]
        )
        self.assertNotIn("RELEASE_YANKED", {f.code for f in report.findings})
        self.assertIn("yanked release(s) on PyPI", rg.render(report))

    def test_a_yanked_current_release_is_a_high_finding(self):
        """The gap in words: "published and healthy" vs "published and pulled".

        Same converged pair of payloads, with the current release withdrawn on
        both. Before this change the probe had no field to say it in and
        reported `release OK`.
        """
        simple = payload("zurich_simple_converged")
        json_api = payload("zurich_json_converged")
        for entry in simple["files"]:
            if "0.7.0" in entry["filename"]:
                entry["yanked"] = "built from the wrong tag"
        for entry in json_api["releases"]["0.7.0"]:
            entry["yanked"] = True
            entry["yanked_reason"] = "built from the wrong tag"

        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.7.0")
            run(repo, "git", "tag", "v0.7.0")
            with stub_index(simple, json_api):
                report = rg.probe(repo, max_age_days=7, offline=False, timeout=1)

        codes = {f.code: f for f in report.findings}
        self.assertIn("RELEASE_YANKED", codes)
        self.assertEqual(codes["RELEASE_YANKED"].severity, "high")
        self.assertIn("built from the wrong tag", codes["RELEASE_YANKED"].detail)
        # The consequence, not just the flag: installs land on 0.6.0.
        self.assertIn("0.6.0", codes["RELEASE_YANKED"].detail)
        self.assertFalse(report.ok)


class YankLagRegressionTest(unittest.TestCase):
    """Measured 2026-07-31, case 1.

    Six releases of `zurich-opendata-mcp` were yanked. The Simple API had all
    six as yanked; the JSON API still answered `yanked: false` for every one of
    them. The probe read the JSON API and had no yank field at all, so the
    withdrawal was invisible either way.
    """

    def _probe(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.7.0")
            run(repo, "git", "tag", "v0.7.0")
            with stub_index(
                payload("zurich_simple_converged"), payload("zurich_json_yank_lag")
            ):
                return rg.probe(repo, max_age_days=7, offline=False, timeout=1)

    def test_the_yanks_are_visible_at_all(self):
        """The core of the finding: yank status must reach the report."""
        report = self._probe()
        self.assertEqual(
            sorted(report.yanked), ["0.2.0", "0.3.0", "0.3.3", "0.4.0", "0.5.0", "0.5.1"]
        )
        self.assertIn("0.2.0", rg.render(report))

    def test_the_simple_api_is_the_one_believed(self):
        """Six versions the JSON API calls healthy, reported as yanked."""
        report = self._probe()
        self.assertEqual(report.json_view.yanked, {})
        for version in ("0.2.0", "0.5.1"):
            self.assertIn(version, report.yanked)

    def test_the_disagreement_is_declared_not_smoothed_over(self):
        report = self._probe()
        self.assertEqual(report.yank_source, "unconfirmed")
        text = rg.render(report)
        self.assertIn("UNCONFIRMED", text)
        self.assertIn("0.2.0", report.yank_detail)

    def test_a_lagging_yank_flag_does_not_turn_the_run_red(self):
        """No finding is raised from a value that is still propagating."""
        report = self._probe()
        self.assertEqual([f.code for f in report.findings], [])
        self.assertTrue(report.ok)


class PublishLagRegressionTest(unittest.TestCase):
    """Measured 2026-07-31, case 2 — the false alarm this must never raise again.

    ~90 s after `0.7.0` was published, the Simple API served it and the JSON API
    still said `0.6.0`. The probe read the JSON API, saw tag `v0.7.0` against
    "PyPI latest 0.6.0", and reported PUBLISH_GAP at high severity: a release
    that had just succeeded, called a failure.
    """

    def _probe(self, tag="v0.7.0", version="0.7.0"):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version=version)
            run(repo, "git", "tag", tag)
            with stub_index(
                payload("zurich_simple_converged"), payload("zurich_json_publish_lag")
            ):
                return rg.probe(repo, max_age_days=7, offline=False, timeout=1)

    def test_no_publish_gap_is_claimed_from_a_stale_json_api(self):
        report = self._probe()
        self.assertNotIn(
            "PUBLISH_GAP",
            {f.code for f in report.findings},
            "a release that landed 90 s ago was reported as never published",
        )
        self.assertTrue(report.ok)

    def test_the_run_is_not_silently_green_either(self):
        """Suppressing the false alarm must not become suppressing the fact."""
        report = self._probe()
        self.assertEqual(report.pypi_status, "unconfirmed")
        text = rg.render(report)
        self.assertIn("UNCONFIRMED", text)
        self.assertIn("0.7.0", text)
        self.assertIn("0.6.0", text)
        self.assertNotIn("release OK", text)

    def test_a_tag_ahead_of_both_apis_is_still_a_publish_gap(self):
        """The suppression is narrow: divergence is not a blanket amnesty."""
        report = self._probe(tag="v0.9.0", version="0.9.0")
        codes = {f.code: f for f in report.findings}
        self.assertIn("PUBLISH_GAP", codes)
        self.assertEqual(codes["PUBLISH_GAP"].severity, "high")
        self.assertIn("0.7.0", codes["PUBLISH_GAP"].detail)
        self.assertFalse(report.ok)


class IndexPrecedenceTest(unittest.TestCase):
    """Simple first, JSON only as a fallback, and the report says which."""

    def _probe(self, simple, json_api):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.7.0")
            run(repo, "git", "tag", "v0.7.0")
            with stub_index(simple, json_api):
                return rg.probe(repo, max_age_days=7, offline=False, timeout=1)

    def test_json_only_is_used_and_flagged_as_the_weaker_source(self):
        report = self._probe(None, payload("zurich_json_converged"))
        self.assertEqual(report.pypi_status, "ok")
        self.assertEqual(report.yank_source, "json-fallback")
        self.assertEqual(report.pypi_version, "0.7.0")
        self.assertIn("not the one pip reads", rg.render(report))

    def test_the_fallback_names_the_version_installs_land_on(self):
        """The survivor is read from the view the yank flags came from.

        An unreadable `IndexView` is still a truthy object, so reading it off
        `report.simple` unconditionally answers "no newer release to fall back
        to" on every fallback run — a wrong statement, not a missing one.
        """
        json_api = payload("zurich_json_converged")
        for entry in json_api["releases"]["0.7.0"]:
            entry["yanked"] = True
        report = self._probe(None, json_api)
        codes = {f.code: f for f in report.findings}
        self.assertIn("RELEASE_YANKED", codes)
        self.assertIn("0.6.0", codes["RELEASE_YANKED"].detail)
        self.assertIn("JSON API fallback", codes["RELEASE_YANKED"].detail)

    def test_simple_alone_is_enough_and_is_not_apologised_for(self):
        report = self._probe(payload("zurich_simple_converged"), None)
        self.assertEqual(report.pypi_status, "ok")
        self.assertEqual(report.yank_source, "simple")
        self.assertEqual(report.pypi_version, "0.7.0")

    def test_prereleases_do_not_become_the_latest_release(self):
        """Simple lists pre-releases; `info.version` does not. Measured on
        `pydantic`, which served `2.14.0a1` there against `2.13.4` here. Taking
        the last entry would report an alpha as what users install — and would
        then read as a disagreement with the JSON API on every such package."""
        versions = ["0.6.0", "0.7.0", "0.8.0a1"]
        report = self._probe(simple_payload(versions), json_payload(versions, "0.7.0"))
        self.assertEqual(report.pypi_version, "0.7.0")
        self.assertEqual(report.pypi_status, "ok")

    def test_a_partly_yanked_version_is_still_installable(self):
        """PEP 592 yanks files. One live wheel left means the version stands."""
        simple = simple_payload(["0.6.0", "0.7.0"])
        for entry in simple["files"]:
            if entry["filename"].endswith("0.7.0.tar.gz"):
                entry["yanked"] = True
        report = self._probe(simple, json_payload(["0.6.0", "0.7.0"]))
        self.assertNotIn("0.7.0", report.yanked)
        self.assertNotIn("RELEASE_YANKED", {f.code for f in report.findings})


class VersionParsingTest(unittest.TestCase):
    def test_prerelease_detection(self):
        for version in ("2.14.0a1", "1.0.0rc2", "1.0.dev1", "0.9.0-beta", "1.0b3"):
            self.assertTrue(rg.is_prerelease(version), version)
        for version in ("1.2.3", "0.7.0", "1.0.post1", "26.2"):
            self.assertFalse(rg.is_prerelease(version), version)

    def test_version_from_filename(self):
        cases = {
            "zurich_opendata_mcp-0.7.0.tar.gz": "0.7.0",
            "zurich_opendata_mcp-0.7.0-py3-none-any.whl": "0.7.0",
            "zurich_opendata_mcp-1.0.0rc1-py3-none-any.whl": "1.0.0rc1",
        }
        for filename, expected in cases.items():
            self.assertEqual(rg.version_from_filename(filename, DIST), expected)

    def test_unrecognised_filenames_are_none_not_a_guess(self):
        self.assertIsNone(rg.version_from_filename("README.md", DIST))
        self.assertIsNone(rg.version_from_filename("noversion.tar.gz", DIST))


@unittest.skipUnless(
    os.environ.get("RELEASE_GAP_LIVE") == "1",
    "live PyPI re-measurement is opt-in (RELEASE_GAP_LIVE=1)",
)
class LiveDivergenceTest(unittest.TestCase):
    """The reproduction itself, kept runnable and kept out of the default suite.

    This is how the fixtures above were measured, and how to check whether the
    two APIs are diverging right now. It asserts nothing about which answer is
    correct — the point is to observe, and observing needs the network.
    """

    def test_report_what_the_two_apis_currently_say(self):
        simple = rg.fetch_simple(DIST, timeout=30)
        json_view = rg.fetch_json(DIST, timeout=30)
        self.assertTrue(simple.readable, simple.detail)
        self.assertTrue(json_view.readable, json_view.detail)
        print(
            f"\nsimple: latest={simple.latest} installable={simple.latest_installable} "
            f"yanked={sorted(simple.yanked)}"
            f"\njson:   latest={json_view.latest} yanked={sorted(json_view.yanked)}"
        )
        if json_view.latest not in simple.candidates() or set(simple.yanked) != set(
            json_view.yanked
        ):
            print("DIVERGENT — the probe reports this as UNCONFIRMED.")


class JsonOutputTest(unittest.TestCase):
    def test_json_is_serialisable_and_complete(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            out = rg.to_json(rg.probe(repo, max_age_days=7, offline=True, timeout=1))
            round_tripped = json.loads(json.dumps(out))
            for key in (
                "dist",
                "version",
                "pypi_status",
                "findings",
                "ok",
                "tags_available",
                "yanked",
                "yank_source",
            ):
                self.assertIn(key, round_tripped)

    def test_yank_state_survives_the_json_round_trip(self):
        """A consumer reading only this dict must be able to tell the difference."""
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.7.0")
            run(repo, "git", "tag", "v0.7.0")
            with stub_index(
                payload("zurich_simple_converged"), payload("zurich_json_converged")
            ):
                out = json.loads(json.dumps(rg.to_json(rg.probe(repo, 7, False, 1))))
        self.assertEqual(out["yank_source"], "simple")
        self.assertIn("0.5.1", out["yanked"])
        self.assertEqual(out["index_views"]["simple"]["latest_installable"], "0.7.0")


class NonPythonTargetTest(unittest.TestCase):
    def test_missing_pyproject_exits_two(self):
        with tempfile.TemporaryDirectory() as d:
            argv = sys.argv
            sys.argv = ["release_gap", "--target", d]
            try:
                self.assertEqual(rg.main(), 2)
            finally:
                sys.argv = argv


if __name__ == "__main__":
    unittest.main()
