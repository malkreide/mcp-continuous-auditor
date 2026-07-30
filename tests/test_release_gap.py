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

Stdlib-only (``python3 -m unittest``), matching the rest of the repo's tooling.
Git-backed cases build a real repository in a temp dir: mocking ``git log``
would only assert that the mock matches the assumption.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_gap as rg  # noqa: E402


PYPROJECT = """\
[project]
name = "demo-mcp"
version = "{version}"
"""


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

    def setUp(self):
        self._orig = rg.fetch_pypi_version
        rg.fetch_pypi_version = lambda dist, timeout: (  # type: ignore[assignment]
            None,
            "unreachable",
            "PyPI unreachable: simulated",
        )

    def tearDown(self):
        rg.fetch_pypi_version = self._orig  # type: ignore[assignment]

    def test_unreachable_index_is_not_ok(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            report = rg.probe(repo, max_age_days=7, offline=False, timeout=1)
            self.assertEqual(report.pypi_status, "unreachable")
            self.assertFalse(report.ok, "an unreachable index must not report success")

    def test_report_says_the_comparison_did_not_happen(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            text = rg.render(rg.probe(repo, max_age_days=7, offline=False, timeout=1))
            self.assertIn("UNKNOWN", text)
            self.assertNotIn("release OK", text)


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
        orig = rg.fetch_pypi_version
        rg.fetch_pypi_version = lambda dist, timeout: (pypi_version, "ok", "")  # type: ignore[assignment]
        try:
            return rg.probe(repo, max_age_days=7, offline=False, timeout=1)
        finally:
            rg.fetch_pypi_version = orig  # type: ignore[assignment]

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
        orig = rg.fetch_pypi_version
        rg.fetch_pypi_version = lambda dist, timeout: ("0.1.0", "ok", "")  # type: ignore[assignment]
        try:
            return rg.probe(repo, max_age_days=max_age_days, offline=False, timeout=1)
        finally:
            rg.fetch_pypi_version = orig  # type: ignore[assignment]

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


class JsonOutputTest(unittest.TestCase):
    def test_json_is_serialisable_and_complete(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            payload = rg.to_json(rg.probe(repo, max_age_days=7, offline=True, timeout=1))
            round_tripped = json.loads(json.dumps(payload))
            for key in ("dist", "version", "pypi_status", "findings", "ok", "tags_available"):
                self.assertIn(key, round_tripped)


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
