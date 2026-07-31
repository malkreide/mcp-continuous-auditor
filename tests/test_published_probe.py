#!/usr/bin/env python3
"""Tests for scripts/published_probe.py.

Stdlib-only. The probe's slow half installs a distribution from an index; that
is not tested here, because a test that needs PyPI is a test that goes red when
PyPI has a bad afternoon. What is tested is the half that decides what a
measurement *means* — the pattern recognition and the classification — since
that is where every bug in this probe's history actually sat.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import published_probe as pp  # noqa: E402


DIST = "demo-mcp"


def classify(findings: list[pp.Finding], installed: str, mentions_ua: bool) -> str:
    """The status rules of probe(), applied to an already-taken measurement."""
    result = pp.Result(dist=DIST, installed=installed, findings=findings, mentions_ua=mentions_ua)
    for f in result.findings:
        f.own = pp._is_own(f.value, DIST)
        f._ok = f.own and f.sent_version == installed
    foreign = [f for f in result.findings if not f.own]
    graded = [f for f in result.findings if f.own and f.sent_version is not None]
    if graded and not all(f._ok for f in graded):
        return "drift"
    if foreign:
        return "foreign_user_agent"
    if graded:
        return "ok"
    return "unverified" if mentions_ua else "no_user_agent"


def finding(value: str, evidence: str = "runtime") -> pp.Finding:
    return pp.Finding(
        value=value, sent_version=pp._sent_version(value), evidence=evidence, where="test"
    )


class VersionExtractionTest(unittest.TestCase):
    def test_reads_the_version_after_the_slash(self) -> None:
        self.assertEqual(pp._sent_version("demo-mcp/1.2.3"), "1.2.3")

    def test_stops_at_the_comment_that_follows(self) -> None:
        self.assertEqual(
            pp._sent_version("demo-mcp/0.3.5 (+https://example.invalid)"), "0.3.5"
        )

    def test_a_user_agent_without_a_version_yields_none(self) -> None:
        """sbb-opendata-mcp sends a bare product token.

        There is nothing to compare, so it must not count as a match — it has to
        fall through to `unverified` rather than quietly pass.
        """
        self.assertIsNone(pp._sent_version("sbb-opendata-mcp"))


class PatternTest(unittest.TestCase):
    def test_fstring_pattern_finds_the_inline_form(self) -> None:
        """The shape a literal scan cannot see: no digit after the slash."""
        found = pp.FSTRING.findall('headers={"User-Agent": f"news-monitor-mcp/{__version__}"}')
        self.assertEqual(found, [("news-monitor-mcp", "__version__")])

    def test_fstring_pattern_accepts_any_variable_name(self) -> None:
        """lobbywatch-mcp calls it PACKAGE_VERSION; matching `__version__` missed it."""
        found = pp.FSTRING.findall('UA = f"lobbywatch-mcp/{PACKAGE_VERSION} (+url)"')
        self.assertEqual(found, [("lobbywatch-mcp", "PACKAGE_VERSION")])

    def test_literal_pattern_finds_the_hand_maintained_form(self) -> None:
        self.assertIn(
            "swiss-transport-mcp/1.0",
            pp.LITERAL.findall('headers={"User-Agent": "swiss-transport-mcp/1.0"}'),
        )


class CommentStrippingTest(unittest.TestCase):
    """`bakom-mcp` 2.0.4 sends the right version and was reported as DRIFT.

    Its `__init__.py` documents the old incident in a comment, and the literal
    scan read the comment as evidence. Going red on a comment that records the
    very bug the probe exists to catch teaches people to delete the record.
    """

    def test_a_comment_recording_old_drift_is_not_evidence(self) -> None:
        src = (
            "from importlib.metadata import version\n"
            '# in server.py carried "bakom-mcp/1.0" to the BAKOM endpoints all the while.\n'
            '__version__ = version("bakom-mcp")\n'
        )
        self.assertNotIn("bakom-mcp/1.0", pp.LITERAL.findall(pp.code_only(src)))

    def test_a_real_literal_still_counts(self) -> None:
        src = 'HEADERS = {"User-Agent": "bakom-mcp/1.0"}  # legacy\n'
        self.assertIn("bakom-mcp/1.0", pp.LITERAL.findall(pp.code_only(src)))

    def test_a_hash_inside_a_string_does_not_truncate_the_line(self) -> None:
        """Why tokenize and not split("#")."""
        src = 'UA = "demo-mcp/1.2.3"  # note\nURL = "https://x.invalid/#frag"\n'
        out = pp.code_only(src)
        self.assertIn("demo-mcp/1.2.3", out)
        self.assertIn("https://x.invalid/#frag", out)
        self.assertNotIn("# note", out)

    def test_unparseable_source_is_checked_whole_not_skipped(self) -> None:
        src = 'def broken(:\n    UA = "demo-mcp/1.2.3"\n'
        self.assertIn("demo-mcp/1.2.3", pp.code_only(src))


class ClassificationTest(unittest.TestCase):
    def test_matching_version_is_ok(self) -> None:
        self.assertEqual(classify([finding("demo-mcp/1.2.3")], "1.2.3", True), "ok")

    def test_stale_version_is_drift(self) -> None:
        self.assertEqual(classify([finding("demo-mcp/1.0.0")], "1.2.3", True), "drift")

    def test_nothing_found_and_nothing_mentioned_is_no_user_agent(self) -> None:
        self.assertEqual(classify([], "1.2.3", False), "no_user_agent")

    def test_nothing_found_but_mentioned_is_unverified(self) -> None:
        """The load-bearing rule.

        Reporting this as clean is how a first pass called 24 packages
        unremarkable, 16 of which were drifting.
        """
        self.assertEqual(classify([], "1.2.3", True), "unverified")

    def test_versionless_user_agent_does_not_pass_as_ok(self) -> None:
        """sbb-opendata-mcp sends a bare product token — nothing to compare.

        It must fall through to `unverified` rather than counting as a match.
        """
        self.assertEqual(classify([finding(DIST)], "0.3.3", True), "unverified")

    def test_one_stale_value_among_correct_ones_still_drifts(self) -> None:
        """swiss-electricity-mcp shipped a stale second constant after a merged fix."""
        self.assertEqual(
            classify([finding("demo-mcp/1.2.3"), finding("demo-mcp/0.2.0")], "1.2.3", True),
            "drift",
        )

    def test_a_spoofed_browser_is_not_read_as_a_version(self) -> None:
        """swiss-efv-mcp sends `Mozilla/5.0 … Chrome/124.0`.

        Parsed as product-token/version it yields "5.0" and reports as drift
        against 0.3.0 — wrong twice: the package is not announcing a stale
        version of itself, and impersonating a browser goes unnamed.
        """
        chrome = finding(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0"
        )
        self.assertEqual(classify([chrome], "0.3.0", True), "foreign_user_agent")

    def test_a_foreign_token_does_not_mask_real_drift(self) -> None:
        """Drift outranks it: a stale own identity stays the headline."""
        self.assertEqual(
            classify([finding("Mozilla/5.0 (X11)"), finding("demo-mcp/0.1.0")], "1.2.3", True),
            "drift",
        )

    def test_own_token_is_matched_case_and_separator_insensitively(self) -> None:
        """swisstopo-mcp sends `SwisstopoMCP/…`; the token need not be the dist name."""
        self.assertTrue(pp._is_own("DemoMCP/1.2.3", "demo-mcp"))
        self.assertFalse(pp._is_own("Mozilla/5.0", "demo-mcp"))


class ExitStatusTest(unittest.TestCase):
    def test_only_ok_and_no_user_agent_count_as_clean(self) -> None:
        for status, expected in (
            ("ok", True),
            ("no_user_agent", True),
            ("drift", False),
            ("unverified", False),
            ("foreign_user_agent", False),
            ("install_failed", False),
        ):
            with self.subTest(status=status):
                self.assertEqual(pp.Result(dist="x", status=status).ok, expected)


if __name__ == "__main__":
    unittest.main()
