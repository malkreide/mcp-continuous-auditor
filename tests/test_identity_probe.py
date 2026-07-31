#!/usr/bin/env python3
"""Tests for scripts/identity_probe.py.

Stdlib-only (`python3 -m unittest`), like the rest of the suite. Each test
builds a throwaway repo shaped like a Python MCP server and runs the probe over
it — no network, no installs.

The two tests that matter are the ones for the false all-clear. A probe that
misses a finding costs one bug; a probe that reports "src/ clean" while missing
it costs the bug *and* the confidence that it was looked for.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import identity_probe as ip  # noqa: E402


def make_repo(root: Path, name: str, version: str, module: str, code: str) -> Path:
    (root / "src" / module).mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "src" / module / "client.py").write_text(code, encoding="utf-8")
    return root


class IdentityProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_product_token_need_not_equal_the_dist_name(self) -> None:
        """swisstopo-mcp sends `SwisstopoMCP/0.1`.

        Matching the dist name literally reported that as clean — the exact
        failure the probe exists to prevent, produced by the probe itself.
        """
        make_repo(
            self.root, "demo-mcp", "0.4.0", "demo_mcp", 'USER_AGENT = "DemoMCP/0.1"\n'
        )
        report = ip.probe(self.root, check_installed=False)
        self.assertFalse(report.ok)
        self.assertTrue(report.hardcoded)
        self.assertIn("DemoMCP/0.1", report.hardcoded[0]["code"])

    def test_unreadable_user_agent_is_not_reported_as_clean(self) -> None:
        """Mentioned but unresolvable must read as unverified, not as clean."""
        make_repo(
            self.root,
            "opaque-mcp",
            "1.2.0",
            "opaque_mcp",
            "from vendor import BRANDING\n\nHEADERS = {'User-Agent': BRANDING}\n",
        )
        report = ip.probe(self.root, check_installed=False)
        self.assertFalse(report.ok)
        self.assertIsNotNone(report.unresolved)
        self.assertIn("no value could be resolved", report.unresolved or "")

    def test_metadata_driven_user_agent_is_clean(self) -> None:
        """The correct shape must not be flagged, or the check gets switched off."""
        make_repo(
            self.root,
            "good-mcp",
            "2.1.0",
            "good_mcp",
            'from importlib.metadata import version\n\n'
            '__version__ = version("good-mcp")\n'
            'USER_AGENT = f"good-mcp/{__version__} (https://example.invalid)"\n'
            'HEADERS = {"User-Agent": USER_AGENT}\n',
        )
        report = ip.probe(self.root, check_installed=False)
        self.assertTrue(report.ok, ip.render(report))
        self.assertIsNone(report.unresolved)

    def test_repo_without_any_user_agent_is_not_flagged(self) -> None:
        """No User-Agent at all is a different matter, and not this check's."""
        make_repo(self.root, "quiet-mcp", "0.1.0", "quiet_mcp", "TIMEOUT = 30\n")
        report = ip.probe(self.root, check_installed=False)
        self.assertTrue(report.ok, ip.render(report))

    def test_comment_documenting_past_drift_is_not_a_finding(self) -> None:
        """Going red on the documentation teaches people to delete it."""
        make_repo(
            self.root,
            "demo-mcp",
            "0.4.0",
            "demo_mcp",
            "from importlib.metadata import version\n\n"
            "# Until 0.3.0 this sent a hand-maintained \"demo-mcp/1.0\".\n"
            '__version__ = version("demo-mcp")\n'
            'USER_AGENT = f"demo-mcp/{__version__}"\n',
        )
        report = ip.probe(self.root, check_installed=False)
        self.assertTrue(report.ok, ip.render(report))

    def test_fallback_with_local_segment_is_not_a_finding(self) -> None:
        """`0.0.0+source` is a marker; a bare `0.0.0` would be a finding."""
        make_repo(
            self.root,
            "demo-mcp",
            "0.4.0",
            "demo_mcp",
            '__version__ = "0.0.0+source"\nUSER_AGENT = f"demo-mcp/{__version__}"\n',
        )
        report = ip.probe(self.root, check_installed=False)
        self.assertTrue(report.ok, ip.render(report))

    def test_badge_drift_does_not_hide_the_source_scan(self) -> None:
        """Both categories are reported; one clean says nothing about the other."""
        make_repo(
            self.root, "demo-mcp", "0.4.0", "demo_mcp", 'USER_AGENT = "demo-mcp/0.1.0"\n'
        )
        (self.root / "README.md").write_text(
            "![v](https://img.shields.io/badge/Version-0.1.0-blue)\n", encoding="utf-8"
        )
        report = ip.probe(self.root, check_installed=False)
        self.assertTrue(report.drift, "badge drift missing")
        self.assertTrue(report.hardcoded, "source scan was not reached")

    def test_norm_collapses_case_and_separators(self) -> None:
        self.assertEqual(ip.norm("SwisstopoMCP"), ip.norm("swisstopo-mcp"))
        self.assertNotEqual(ip.norm("other-mcp"), ip.norm("demo-mcp"))


if __name__ == "__main__":
    unittest.main()
