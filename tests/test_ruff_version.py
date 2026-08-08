"""Tests for the Ruff version guard.

The pin guard next door compares two TEXTS. That the Ruff which then runs the
gates carries that version was never measured — and "both places agree" was
reported anyway. `scripts/check_ruff_version.py` closes that; this file proves
it closes it.

The pure side is what gets tested: `compare()` receives the pin, the raw
`ruff --version` output and its exit code as values. No PATH, no subprocess,
no mocks — a mock would only restate this file's own assumption about the
output shape.

The **ANCHOR** cases weigh more than the individual ones: when an anchor goes,
the check has nothing left to compare, and the obvious implementation reports
"passed" for it.

Stdlib `unittest` only, like the rest of this suite — the runner is
`unittest.defaultTestLoader.discover("tests")` and pytest is not installed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_ruff_pin as crp  # noqa: E402
import check_ruff_version as crv  # noqa: E402


class RuffVersionTest(unittest.TestCase):
    def test_matching_version_is_green(self):
        ok, message = crv.compare("0.16.1", "ruff 0.16.1\n", 0)
        self.assertTrue(ok, message)
        self.assertIn("0.16.1", message)

    def test_wrong_version_is_a_finding(self):
        """The measured incident: an older Ruff sits earlier on PATH."""
        ok, message = crv.compare("0.16.1", "ruff 0.15.8\n", 0)
        self.assertFalse(ok)
        self.assertIn("0.15.8", message)
        self.assertIn("0.16.1", message)
        # The finding must say why the pin sync does NOT catch this, or the
        # next reader looks for the fault in the wrong file.
        self.assertIn("two texts", message)

    def test_failing_invocation_is_a_finding(self):
        ok, message = crv.compare("0.16.1", "boom", 3)
        self.assertFalse(ok)
        self.assertIn("exited with 3", message)

    def test_parse_version(self):
        for raw, expected in [
            ("ruff 0.16.1", "0.16.1"),
            ("ruff 0.16.1+deadbeef", "0.16.1+deadbeef"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(crv.parse_version(raw), expected)


class AnchorTest(unittest.TestCase):
    """A missing anchor must be a finding, never a silent pass."""

    def test_missing_pin_is_a_finding(self):
        ok, message = crv.compare(None, "ruff 0.16.1\n", 0)
        self.assertFalse(ok)
        self.assertIn("anchor gone", message)

    def test_unreadable_output_shape_is_a_finding(self):
        """If upstream changes the output, the check must not do nothing."""
        for raw in ["Ruff, version 0.16.1", "", "0.16.1", "ruff\n"]:
            with self.subTest(raw=raw):
                ok, message = crv.compare("0.16.1", raw, 0)
                self.assertFalse(ok)
                self.assertIn("does not answer in the form", message)

    def test_the_real_pin_is_readable(self):
        """The guard must not be green because it cannot find the real pin."""
        text = (REPO_ROOT / crp.LINT_WORKFLOW).read_text(encoding="utf-8")
        self.assertTrue(
            crp.workflow_pins(text),
            "lint.yml names no `ruff==<version>` — then the version guard "
            "checks nothing when it matters.",
        )


if __name__ == "__main__":
    unittest.main()
