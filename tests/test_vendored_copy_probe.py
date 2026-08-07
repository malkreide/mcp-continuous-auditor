#!/usr/bin/env python3
"""Tests for scripts/vendored_copy_probe.py — do the identical files match?

The probe is a manifest parse plus a handful of file reads, so these tests
write real files into a temp dir and run the whole comparison over them.
Nothing that decides a finding is mocked.

The central scenario is the incident the probe was written for: two copies of
`sparql_client.py` that both declare `VENDORED COPY (v1.1.0)` while one of them
carries a retry policy the other does not. The marker is what makes it a
separate finding — the drift is ordinary, its invisibility is not.

Stdlib-only and offline.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import vendored_copy_probe as vcp  # noqa: E402

HEADER = '"""Shared client.\n\nVENDORED COPY ({version}). Kept byte-identical across the portfolio.\n"""\n'

REPAIRED = (
    HEADER
    + "\nimport random\n\n\ndef retry_delay(attempt):\n    return random.random()\n"
)
STALE = HEADER + "\n\ndef retry_delay(attempt):\n    return 2**attempt\n"


def _manifest(
    root: Path, *, marker: str | None = None, sites: list[tuple[str, str]]
) -> Path:
    lines = [
        "[[group]]",
        'name = "commons/sparql_client"',
        'says = "shared client"',
    ]
    if marker is not None:
        lines.append(f"marker = '{marker}'")
    for repo, file in sites:
        lines += ["", "[[group.site]]", f'repo = "{repo}"', f'file = "{file}"']
    path = root / "vendored.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _repo(
    root: Path, name: str, body: str, file: str = "src/pkg/sparql_client.py"
) -> Path:
    repo = root / name
    (repo / Path(file).parent).mkdir(parents=True, exist_ok=True)
    (repo / file).write_text(body, encoding="utf-8")
    return repo


class TestTheIncident(unittest.TestCase):
    """Both copies say v1.1.0 and only one has the retry policy."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, a: str, b: str):
        _repo(self.root, "swiss-environment-mcp", a)
        _repo(self.root, "fedlex-mcp", b)
        manifest = _manifest(
            self.root,
            sites=[
                ("malkreide/swiss-environment-mcp", "src/pkg/sparql_client.py"),
                ("malkreide/fedlex-mcp", "src/pkg/sparql_client.py"),
            ],
        )
        return vcp.run(manifest, roots=[self.root])

    def test_drift_under_one_marker_is_reported_twice(self):
        report = self._run(
            STALE.format(version="v1.1.0"), REPAIRED.format(version="v1.1.0")
        )
        codes = [f.code for f in report.findings]
        self.assertIn("COPY_DRIFT", codes)
        self.assertIn("MARKER_STALE", codes)
        self.assertEqual(report.exit_code(), vcp.EXIT_FINDINGS)

    def test_the_marker_finding_says_why_the_drift_survived(self):
        report = self._run(
            STALE.format(version="v1.1.0"), REPAIRED.format(version="v1.1.0")
        )
        stale = next(f for f in report.findings if f.code == "MARKER_STALE")
        self.assertEqual(stale.severity, "high")
        self.assertIn("v1.1.0", stale.detail)
        self.assertIn("only one half is visible", stale.detail)

    def test_the_drift_finding_prints_both_digests(self):
        report = self._run(
            STALE.format(version="v1.1.0"), REPAIRED.format(version="v1.1.0")
        )
        drift = next(f for f in report.findings if f.code == "COPY_DRIFT")
        self.assertEqual(drift.severity, "high")
        self.assertIn("swiss-environment-mcp", drift.detail)
        self.assertIn("fedlex-mcp", drift.detail)
        self.assertIn("sha256:", drift.detail)

    def test_identical_copies_are_green(self):
        same = REPAIRED.format(version="v1.1.0")
        report = self._run(same, same)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), vcp.EXIT_GREEN)
        self.assertEqual(report.groups_compared, ["commons/sparql_client"])

    def test_drift_with_different_markers_is_the_milder_finding(self):
        """Drift that announces itself is not the same defect."""
        report = self._run(
            STALE.format(version="v1.1.0"), REPAIRED.format(version="v1.2.0")
        )
        codes = [f.code for f in report.findings]
        self.assertIn("MARKER_SPLIT", codes)
        self.assertNotIn("MARKER_STALE", codes)
        split = next(f for f in report.findings if f.code == "MARKER_SPLIT")
        self.assertEqual(split.severity, "low")

    def test_a_whitespace_only_difference_is_still_drift_but_milder(self):
        """Byte identity is the contract; a stray newline still breaks it.

        The severity drops because a trailing newline is a different job from
        a missing retry policy, and a probe that rates them alike gets ignored.
        """
        body = REPAIRED.format(version="v1.1.0")
        report = self._run(body, body.replace("\n\n\ndef", "\n\n\ndef") + "\n\n")
        drift = next(f for f in report.findings if f.code == "COPY_DRIFT")
        self.assertEqual(drift.severity, "medium")
        self.assertIn("trailing whitespace", drift.detail)

    def test_a_missing_marker_is_its_own_finding(self):
        no_marker = '"""Shared client."""\n\n\ndef retry_delay(a):\n    return 1\n'
        report = self._run(no_marker, REPAIRED.format(version="v1.1.0"))
        codes = [f.code for f in report.findings]
        self.assertIn("MARKER_MISSING", codes)
        # …and it does not also claim the markers are stale: there is nothing
        # to have gone stale.
        self.assertNotIn("MARKER_STALE", codes)


class TestNothingIsSilentlyClean(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_missing_checkout_is_unverified_not_green(self):
        _repo(self.root, "fedlex-mcp", REPAIRED.format(version="v1.1.0"))
        manifest = _manifest(
            self.root,
            sites=[
                ("malkreide/swiss-environment-mcp", "src/pkg/sparql_client.py"),
                ("malkreide/fedlex-mcp", "src/pkg/sparql_client.py"),
            ],
        )
        report = vcp.run(manifest, roots=[self.root])
        codes = [u.code for u in report.unverified]
        self.assertIn("SITE_MISSING", codes)
        self.assertIn("GROUP_UNMEASURED", codes)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), vcp.EXIT_NOT_MEASURED)

    def test_a_moved_file_is_a_loud_finding_about_the_mapping(self):
        _repo(self.root, "a-mcp", REPAIRED.format(version="v1.1.0"))
        _repo(
            self.root,
            "b-mcp",
            REPAIRED.format(version="v1.1.0"),
            file="src/pkg/moved.py",
        )
        manifest = _manifest(
            self.root,
            sites=[
                ("malkreide/a-mcp", "src/pkg/sparql_client.py"),
                ("malkreide/b-mcp", "src/pkg/sparql_client.py"),
            ],
        )
        report = vcp.run(manifest, roots=[self.root])
        unread = next(u for u in report.unverified if u.code == "SITE_UNREADABLE")
        self.assertIn("mapping itself may be stale", unread.detail)
        self.assertEqual(report.exit_code(), vcp.EXIT_NOT_MEASURED)

    def test_an_explicit_repo_path_is_honoured(self):
        elsewhere = self.root / "elsewhere"
        _repo(elsewhere, "one", REPAIRED.format(version="v1.1.0"))
        _repo(elsewhere, "two", REPAIRED.format(version="v1.1.0"))
        manifest = _manifest(
            self.root,
            sites=[
                ("malkreide/one", "src/pkg/sparql_client.py"),
                ("malkreide/two", "src/pkg/sparql_client.py"),
            ],
        )
        report = vcp.run(
            manifest,
            explicit={
                "malkreide/one": elsewhere / "one",
                "malkreide/two": elsewhere / "two",
            },
        )
        self.assertEqual(report.exit_code(), vcp.EXIT_GREEN)


class TestTheManifestIsLoud(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_missing_manifest_cannot_run(self):
        with self.assertRaises(vcp.ManifestError):
            vcp.run(self.root / "nope.toml")

    def test_a_manifest_with_no_groups_cannot_run(self):
        path = self.root / "vendored.toml"
        path.write_text("# nothing here\n", encoding="utf-8")
        with self.assertRaises(vcp.ManifestError):
            vcp.run(path)

    def test_a_group_with_one_site_is_rejected(self):
        """A group with one site is always green and measures nothing."""
        manifest = _manifest(self.root, sites=[("malkreide/only", "src/pkg/x.py")])
        with self.assertRaisesRegex(vcp.ManifestError, "at least 2"):
            vcp.run(manifest)

    def test_a_site_without_a_file_is_rejected(self):
        path = self.root / "vendored.toml"
        path.write_text(
            '[[group]]\nname = "g"\n\n[[group.site]]\nrepo = "a/b"\n\n'
            '[[group.site]]\nrepo = "c/d"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(vcp.ManifestError, "missing `file`"):
            vcp.run(path)

    def test_a_bad_marker_pattern_is_reported_not_swallowed(self):
        _repo(self.root, "a-mcp", REPAIRED.format(version="v1.1.0"))
        _repo(self.root, "b-mcp", REPAIRED.format(version="v1.1.0"))
        manifest = _manifest(
            self.root,
            marker="VENDORED COPY ([unclosed",
            sites=[
                ("malkreide/a-mcp", "src/pkg/sparql_client.py"),
                ("malkreide/b-mcp", "src/pkg/sparql_client.py"),
            ],
        )
        report = vcp.run(manifest, roots=[self.root])
        self.assertTrue(any(u.code == "SITE_UNREADABLE" for u in report.unverified))
        self.assertEqual(report.exit_code(), vcp.EXIT_NOT_MEASURED)


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_bad_repo_path_argument_cannot_run(self):
        with self.assertRaises(vcp.ManifestError):
            vcp._explicit(["no-equals-sign"])

    def test_the_shipped_example_manifest_parses(self):
        """The committed example is the format's only reference — it must load."""
        example = Path(__file__).resolve().parents[1] / "vendored.example.toml"
        groups = vcp.load_manifest(example)
        self.assertTrue(groups)
        self.assertGreaterEqual(len(groups[0].sites), vcp.MIN_SITES)

    def test_main_returns_findings_exit_code(self):
        _repo(self.root, "a-mcp", STALE.format(version="v1.1.0"))
        _repo(self.root, "b-mcp", REPAIRED.format(version="v1.1.0"))
        manifest = _manifest(
            self.root,
            sites=[
                ("malkreide/a-mcp", "src/pkg/sparql_client.py"),
                ("malkreide/b-mcp", "src/pkg/sparql_client.py"),
            ],
        )
        code = vcp.main(
            [
                "--manifest",
                str(manifest),
                "--repos-root",
                str(self.root),
                "--format",
                "json",
            ]
        )
        self.assertEqual(code, vcp.EXIT_FINDINGS)


if __name__ == "__main__":
    unittest.main()


class TestTheProbeIsFindable(unittest.TestCase):
    """A probe nobody can find is a probe nobody runs.

    `docs/probes/README.md` is the index a reader actually opens; the page it
    links to is where the case history lives. Neither is enforced by anything
    else in this repository, and an index that silently loses a row is the same
    class of defect the probe itself is about.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_the_page_exists(self):
        self.assertTrue((self.ROOT / "docs/probes/vendored-copy.md").is_file())

    def test_the_index_links_to_it(self):
        index = (self.ROOT / "docs/probes/README.md").read_text(encoding="utf-8")
        self.assertIn("[vendored-copy.md](vendored-copy.md)", index)

    def test_the_page_names_the_script(self):
        page = (self.ROOT / "docs/probes/vendored-copy.md").read_text(encoding="utf-8")
        self.assertIn("scripts/vendored_copy_probe.py", page)
