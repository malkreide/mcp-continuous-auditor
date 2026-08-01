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
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import shipped_probe as rg  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pypi"
DIST = "zurich-opendata-mcp"


def probe_metadata(target, max_age_days=7.0, offline=False, timeout=1.0,
                   now=None, index_url=None):
    """The merged probe at its metadata depth, in the shape these tests use.

    They predate the merge, when this was `release_gap.probe(target, ...)` with
    no distribution name — it came from the target's pyproject. That is still
    how the CLI behaves, so the shim is the CLI's own default, not a fiction
    invented for the tests.
    """
    try:
        dist = rg.read_project(target).get("name", target.name)
    except OSError:
        dist = target.name
    return rg.probe(dist, target, metadata_only=True, offline=offline,
                    max_age_days=max_age_days, now=now,
                    index_url=index_url or rg.DEFAULT_INDEX)


def as_dict(report):
    return report.as_dict()


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
        # Which APIs were actually reached. An API that must not be consulted
        # cannot be tested for by its answer — only by its absence from here.
        self.seen: list[str] = []

    def _get(self, url: str, timeout: float, accept: str | None = None):
        which = "json" if "/pypi/" in url and url.endswith("/json") else "simple"
        self.seen.append(which)
        served = self._simple if which == "simple" else self._json
        if served is None:
            return None, "unreachable", "index unreachable: simulated"
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
            report = probe_metadata(repo, max_age_days=7, offline=False, timeout=1)
            self.assertEqual(report.index_status, "unreachable")
            self.assertFalse(report.ok, "an unreachable index must not report success")

    def test_report_says_the_comparison_did_not_happen(self):
        """The merge changed the wording, not the property.

        `release_gap` printed `UNKNOWN`; the merged report says the same thing in
        the shipped probe's own voice and routes it through `harness_error`, so
        it exits 127 rather than 0. What must not change is that the run never
        reads as a clean comparison.
        """
        with tempfile.TemporaryDirectory() as d, stub_index():
            repo = make_repo(Path(d))
            report = probe_metadata(repo, max_age_days=7, offline=False, timeout=1)
            text = rg.render(report)
            self.assertIn("NOT compared", text)
            self.assertNotIn("consistent", text)
            self.assertEqual(report.exit_code(), 127)

    def test_yank_status_is_unavailable_not_healthy(self):
        """No index means no yank answer — never "nothing is yanked"."""
        with tempfile.TemporaryDirectory() as d, stub_index():
            repo = make_repo(Path(d))
            report = probe_metadata(repo, max_age_days=7, offline=False, timeout=1)
            self.assertEqual(report.yank_source, "unavailable")


class OfflineTest(unittest.TestCase):
    def test_offline_is_declared_and_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            report = probe_metadata(repo, max_age_days=7, offline=True, timeout=1)
            self.assertEqual(report.index_status, "skipped")
            self.assertTrue(report.ok)
            self.assertIn("--offline", rg.render(report))


class PublishGapTest(unittest.TestCase):
    """A tag PyPI does not have — the maintainer already thinks it shipped."""

    def _probe_with_pypi(self, repo: Path, pypi_version: str):
        versions = sorted({"0.1.0", pypi_version}, key=lambda v: rg.release_key(v) or ())
        with stub_index(simple_payload(versions), json_payload(versions, pypi_version)):
            return probe_metadata(repo, max_age_days=7, offline=False, timeout=1)

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
            return probe_metadata(repo, max_age_days=max_age_days, offline=False, timeout=1)

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
                report = probe_metadata(repo, max_age_days=7, offline=True, timeout=1)
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
                return probe_metadata(repo, max_age_days=7, offline=False, timeout=1)

    def test_agreement_is_ok_and_quiet(self):
        report = self._probe()
        self.assertEqual(report.index_status, "ok")
        self.assertEqual(report.yank_source, "simple")
        self.assertEqual(report.index_version, "0.7.0")
        self.assertEqual([f.code for f in report.findings], [])

    def test_historic_yanks_are_reported_without_being_a_finding(self):
        """0.2.0–0.5.1 are withdrawn. That is history, not a defect in 0.7.0."""
        report = self._probe()
        self.assertEqual(
            sorted(report.yanked), ["0.2.0", "0.3.0", "0.3.3", "0.4.0", "0.5.0", "0.5.1"]
        )
        self.assertNotIn("RELEASE_YANKED", {f.code for f in report.findings})
        self.assertIn("yanked release(s) on the index", rg.render(report))

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
                report = probe_metadata(repo, max_age_days=7, offline=False, timeout=1)

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
                return probe_metadata(repo, max_age_days=7, offline=False, timeout=1)

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
                return probe_metadata(repo, max_age_days=7, offline=False, timeout=1)

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
        self.assertEqual(report.index_status, "unconfirmed")
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
                return probe_metadata(repo, max_age_days=7, offline=False, timeout=1)

    def test_json_only_is_used_and_flagged_as_the_weaker_source(self):
        report = self._probe(None, payload("zurich_json_converged"))
        self.assertEqual(report.index_status, "ok")
        self.assertEqual(report.yank_source, "json-fallback")
        self.assertEqual(report.index_version, "0.7.0")
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
        self.assertEqual(report.index_status, "ok")
        self.assertEqual(report.yank_source, "simple")
        self.assertEqual(report.index_version, "0.7.0")

    def test_prereleases_do_not_become_the_latest_release(self):
        """Simple lists pre-releases; `info.version` does not. Measured on
        `pydantic`, which served `2.14.0a1` there against `2.13.4` here. Taking
        the last entry would report an alpha as what users install — and would
        then read as a disagreement with the JSON API on every such package."""
        versions = ["0.6.0", "0.7.0", "0.8.0a1"]
        report = self._probe(simple_payload(versions), json_payload(versions, "0.7.0"))
        self.assertEqual(report.index_version, "0.7.0")
        self.assertEqual(report.index_status, "ok")

    def test_a_partly_yanked_version_is_still_installable(self):
        """PEP 592 yanks files. One live wheel left means the version stands."""
        simple = simple_payload(["0.6.0", "0.7.0"])
        for entry in simple["files"]:
            if entry["filename"].endswith("0.7.0.tar.gz"):
                entry["yanked"] = True
        report = self._probe(simple, json_payload(["0.6.0", "0.7.0"]))
        self.assertNotIn("0.7.0", report.yanked)
        self.assertNotIn("RELEASE_YANKED", {f.code for f in report.findings})


SIMPLE_HTML = """\
<!DOCTYPE html>
<html><head><title>Links for demo-mcp</title></head><body>
<h1>Links for demo-mcp</h1>
<a href="https://files.example.com/demo_mcp-0.5.0.tar.gz#sha256=aa"
   data-requires-python="&gt;=3.11">demo_mcp-0.5.0.tar.gz</a><br/>
<a href="https://files.example.com/demo_mcp-0.6.0-py3-none-any.whl#sha256=bb"
   data-yanked="built from the wrong tag">demo_mcp-0.6.0-py3-none-any.whl</a><br/>
<a href="https://files.example.com/demo_mcp-0.6.0.tar.gz#sha256=cc"
   data-yanked="">demo_mcp-0.6.0.tar.gz</a><br/>
</body></html>
"""


class SimpleHtmlTest(unittest.TestCase):
    """PEP 503 HTML — the only format an arbitrary index must serve.

    PEP 691's JSON is optional. A devpi, an Artifactory or a plain directory
    listing answers HTML, so refusing to read it would mean refusing to audit
    every private index — which is what honouring `--index-url` requires.
    """

    def _view(self, body: str, content_type: str = "text/html; charset=utf-8"):
        orig = urllib.request.urlopen

        class FakeResponse:
            headers = {"Content-Type": content_type}

            def read(self):
                return body.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        urllib.request.urlopen = lambda *a, **k: FakeResponse()  # type: ignore[assignment]
        try:
            return rg.fetch_simple("demo-mcp", timeout=1)
        finally:
            urllib.request.urlopen = orig  # type: ignore[assignment]

    def test_versions_are_derived_when_html_offers_no_versions_key(self):
        """PEP 700's `versions` is JSON-only. An empty list must not read as
        "this project has no releases"."""
        view = self._view(SIMPLE_HTML)
        self.assertTrue(view.readable, view.detail)
        self.assertEqual(view.versions, ["0.5.0", "0.6.0"])

    def test_data_yanked_is_a_yank_even_with_no_reason(self):
        """PEP 592: the ATTRIBUTE is the yank, its value is an optional reason.

        Reading it as a truthy value would call every reasonless yank healthy —
        the same class of mistake as trusting the JSON API's lagging flag.
        """
        view = self._view(SIMPLE_HTML)
        self.assertIn("0.6.0", view.yanked)
        self.assertEqual(view.yanked["0.6.0"], "built from the wrong tag")
        self.assertNotIn("0.5.0", view.yanked)

    def test_the_latest_installable_skips_the_yanked_release(self):
        view = self._view(SIMPLE_HTML)
        self.assertEqual(view.latest, "0.6.0")
        self.assertEqual(view.latest_installable, "0.5.0")

    def test_a_mislabelled_content_type_is_still_read(self):
        """Indexes that serve HTML as text/plain are common enough to survive."""
        view = self._view(SIMPLE_HTML, content_type="text/plain")
        self.assertTrue(view.readable, view.detail)
        self.assertEqual(view.versions, ["0.5.0", "0.6.0"])

    def test_a_body_that_is_neither_is_unreachable_not_empty(self):
        """An error page must never become an empty-but-successful answer."""
        view = self._view("upstream connect error", content_type="text/plain")
        self.assertFalse(view.readable)
        self.assertIn("unparseable", view.detail)


class CustomIndexTest(unittest.TestCase):
    """`--index-url`, and the cross-check that must NOT run against it.

    Querying pypi.org about a distribution that lives on a private index is not
    a weaker second opinion — it is a different package that happens to share a
    name. Agreement and disagreement are both noise, and the second is worse:
    it would raise UNCONFIRMED, or a PUBLISH_GAP, from an unrelated project.
    """

    PRIVATE = "https://pypi.example.com/simple"

    # `None` is a meaningful value for a served payload — it is how the stub
    # spells "unreachable" — so the default cannot also be None.
    DEFAULT = object()

    def _probe(self, index_url, simple=DEFAULT, json_api=None, tag="v0.7.0"):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.7.0")
            if tag:
                run(repo, "git", "tag", tag)
            served = payload("zurich_simple_converged") if simple is self.DEFAULT else simple
            with stub_index(served, json_api) as stub:
                report = probe_metadata(repo, 7, False, 1, index_url=index_url)
        return report, stub

    def test_pypi_org_is_never_asked_about_a_private_index_package(self):
        report, stub = self._probe(self.PRIVATE, json_api=payload("zurich_json_converged"))
        self.assertEqual(stub.seen, ["simple"], "the JSON API must not be consulted")
        self.assertEqual(report.json_view.status, rg.NOT_APPLICABLE)

    def test_the_missing_cross_check_is_stated_not_silently_skipped(self):
        """A run with one opinion must not look like a run with two that agreed."""
        report, _ = self._probe(self.PRIVATE)
        self.assertEqual(report.index_status, "ok")
        text = rg.render(report)
        self.assertIn("not PyPI", text)
        self.assertIn("did not run", text)
        self.assertIn(self.PRIVATE, text)

    def test_the_simple_answer_still_stands_on_its_own(self):
        report, _ = self._probe(self.PRIVATE)
        self.assertEqual(report.index_version, "0.7.0")
        self.assertEqual(report.yank_source, "simple")
        self.assertIn("0.5.1", report.yanked)
        self.assertEqual([f.code for f in report.findings], [])

    def test_unconfirmed_cannot_be_reached_without_a_second_opinion(self):
        """The status that exists to describe a disagreement needs two parties."""
        report, _ = self._probe(self.PRIVATE)
        self.assertNotEqual(report.index_status, "unconfirmed")
        self.assertNotEqual(report.yank_source, "unconfirmed")

    def test_an_unreachable_private_index_says_there_was_no_fallback(self):
        report, _ = self._probe(self.PRIVATE, simple=None)
        self.assertEqual(report.index_status, "unreachable")
        self.assertFalse(report.ok)
        self.assertIn("no JSON API to fall back to", report.index_detail)

    def test_the_default_index_still_cross_checks(self):
        """The narrowing is conditional on the index, not a general retreat."""
        report, stub = self._probe(
            rg.DEFAULT_INDEX, json_api=payload("zurich_json_publish_lag"))
        self.assertEqual(stub.seen, ["simple", "json"])
        self.assertEqual(report.index_status, "unconfirmed")

    def test_the_index_url_reaches_the_json_output(self):
        report, _ = self._probe(self.PRIVATE)
        self.assertEqual(json.loads(json.dumps(as_dict(report)))["index_url"], self.PRIVATE)

    def test_a_private_html_index_all_the_way_to_a_finding(self):
        """The whole chain on the shape a private index actually has: PEP 503
        HTML, no PEP 700 `versions`, a yank with a reason and one without, and
        no JSON API anywhere. The individual pieces are tested above; this is
        the one case that proves they compose."""
        html = (
            '<!DOCTYPE html><html><body>'
            '<a href="/f/demo_mcp-0.5.0.tar.gz">demo_mcp-0.5.0.tar.gz</a>'
            '<a href="/f/demo_mcp-0.6.0-py3-none-any.whl" data-yanked="bad build">'
            'demo_mcp-0.6.0-py3-none-any.whl</a>'
            '<a href="/f/demo_mcp-0.6.0.tar.gz" data-yanked="">demo_mcp-0.6.0.tar.gz</a>'
            "</body></html>"
        )
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d), version="0.6.0")
            run(repo, "git", "tag", "v0.6.0")
            orig = rg._get
            rg._get = lambda url, timeout, accept=None: (  # type: ignore[assignment]
                rg._parse_simple_html(html), "ok", ""
            )
            try:
                report = probe_metadata(repo, 7, False, 1, index_url="http://localhost:8099")
            finally:
                rg._get = orig  # type: ignore[assignment]

        codes = {f.code: f for f in report.findings}
        self.assertIn("RELEASE_YANKED", codes)
        self.assertIn("bad build", codes["RELEASE_YANKED"].detail)
        self.assertIn("localhost:8099", codes["RELEASE_YANKED"].detail)
        # The whole point of the yank: installs land on the previous release.
        self.assertIn("0.5.0", codes["RELEASE_YANKED"].detail)
        self.assertNotIn("PyPI and YANKED", rg.render(report))


class IsPypiTest(unittest.TestCase):
    def test_matches_on_host_not_on_prefix(self):
        for url in ("https://pypi.org/simple", "https://pypi.org/simple/"):
            self.assertTrue(rg.is_pypi(url), url)
        for url in ("https://pypi.example.com/simple",
                    "https://mirror.local/pypi.org/simple",
                    "http://localhost:8080/simple"):
            self.assertFalse(rg.is_pypi(url), url)


class SimpleUrlTest(unittest.TestCase):
    def test_the_name_is_normalised(self):
        """PEP 503: an index need only serve the normalised spelling, so passing
        the raw name through would 404 — reported as "never published"."""
        self.assertEqual(
            rg.simple_url("Foo.Bar_Baz"), "https://pypi.org/simple/foo-bar-baz/"
        )

    def test_a_custom_index_is_honoured_with_or_without_a_slash(self):
        for base in ("https://pypi.example.com/simple", "https://pypi.example.com/simple/"):
            self.assertEqual(
                rg.simple_url("demo-mcp", base), "https://pypi.example.com/simple/demo-mcp/"
            )


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

    def test_both_simple_flavours_agree_on_the_live_index(self):
        """The HTML parser against the same page PyPI serves as JSON.

        The fixtures prove the parser handles a page; only the live index proves
        it handles PyPI's actual markup, which is what a private-index user's
        `--index-url` run depends on being right.
        """
        as_json = rg.fetch_simple(DIST, timeout=30)
        original = rg.SIMPLE_ACCEPT
        rg.SIMPLE_ACCEPT = "text/html"
        try:
            as_html = rg.fetch_simple(DIST, timeout=30)
        finally:
            rg.SIMPLE_ACCEPT = original
        self.assertTrue(as_html.readable, as_html.detail)
        self.assertEqual(as_html.versions, as_json.versions)
        self.assertEqual(as_html.yanked, as_json.yanked)
        self.assertEqual(as_html.latest_installable, as_json.latest_installable)


class JsonOutputTest(unittest.TestCase):
    def test_json_is_serialisable_and_complete(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            out = as_dict(probe_metadata(repo, max_age_days=7, offline=True, timeout=1))
            round_tripped = json.loads(json.dumps(out))
            for key in (
                "dist",
                "versions",
                "index_status",
                "index_url",
                "findings",
                "exit_code",
                "tags_available",
                "yanked",
                "yank_source",
                "depth",
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
                out = json.loads(json.dumps(as_dict(probe_metadata(repo, 7, False, 1))))
        self.assertEqual(out["yank_source"], "simple")
        self.assertIn("0.5.1", out["yanked"])
        self.assertEqual(out["index_views"]["simple"]["latest_installable"], "0.7.0")


class NonPythonTargetTest(unittest.TestCase):
    def test_missing_pyproject_cannot_run(self):
        """127, not 2.

        Before the merge this was exit 2, which meant "not a Python MCP repo".
        In the merged exit-code vocabulary 2 means FINDINGS, so keeping it would
        have reported a directory with no pyproject.toml as a defect in a target
        rather than as a probe that could not be pointed at one.
        """
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rg.main(["--target", d]), 127)


if __name__ == "__main__":
    unittest.main()
