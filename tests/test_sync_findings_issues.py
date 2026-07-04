#!/usr/bin/env python3
"""Tests for scripts/sync_findings_issues.py — deterministic issue routing (U-C).

Covers the pure decision logic (which issues a summary implies, and open-vs-comment
dedup) plus main()'s no-network paths (no-findings no-op, invalid target, dry-run
plan). No GitHub calls are made. Stdlib-only, matching the rest of the suite.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_findings_issues as sfi  # noqa: E402


def _summary(**over: object) -> dict:
    base = {
        "outcome": "findings", "target": "o/r", "target_sha": "abc1234",
        "schema_drift": False, "redteam": False, "other_findings": False,
        "toolchain_fail": False,
    }
    base.update(over)
    return base


class FindingClassesTest(unittest.TestCase):
    def test_green_summary_routes_nothing(self) -> None:
        self.assertEqual(sfi.finding_classes(_summary(outcome="green")), [])

    def test_hard_fail_routes_nothing(self) -> None:
        self.assertEqual(sfi.finding_classes(_summary(outcome="hard-fail", schema_drift=True)), [])

    def test_schema_and_redteam_are_two_issues(self) -> None:
        cls = sfi.finding_classes(_summary(schema_drift=True, redteam=True))
        labels = [c["label"] for c in cls]
        self.assertEqual(labels, ["schema-drift", "redteam"])
        # each carries a distinct hidden marker for dedup
        self.assertEqual(cls[0]["marker"], "<!-- nightly-audit:schema-drift -->")

    def test_other_and_toolchain_collapse_to_one_label(self) -> None:
        cls = sfi.finding_classes(_summary(other_findings=True, toolchain_fail=True))
        self.assertEqual([c["label"] for c in cls], ["audit-finding"])


class DecideTest(unittest.TestCase):
    def test_create_when_no_open_issue_matches(self) -> None:
        action, number = sfi.decide([], "<!-- nightly-audit:redteam -->")
        self.assertEqual(action, "create")
        self.assertIsNone(number)

    def test_comment_when_marker_present(self) -> None:
        issues = [{"number": 7, "body": "prefix\n<!-- nightly-audit:redteam -->\nx"}]
        action, number = sfi.decide(issues, "<!-- nightly-audit:redteam -->")
        self.assertEqual((action, number), ("comment", 7))

    def test_ignores_open_issue_without_marker(self) -> None:
        issues = [{"number": 9, "body": "unrelated open issue"}]
        action, number = sfi.decide(issues, "<!-- nightly-audit:redteam -->")
        self.assertEqual(action, "create")


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, summary: dict, report: str = "# report") -> tuple[int, str]:
        sp = self.dir / "s.json"
        rp = self.dir / "r.md"
        sp.write_text(json.dumps(summary), encoding="utf-8")
        rp.write_text(report, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = sfi.main(["--summary", str(sp), "--report", str(rp), "--dry-run"])
        return rc, buf.getvalue()

    def test_no_findings_is_noop(self) -> None:
        rc, out = self._run(_summary(outcome="green"))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)

    def test_invalid_target_fails(self) -> None:
        rc, out = self._run(_summary(schema_drift=True, target="invalid"))
        self.assertEqual(rc, 1)
        self.assertIn("no valid target", out)

    def test_dry_run_plans_without_network(self) -> None:
        rc, out = self._run(_summary(schema_drift=True, redteam=True))
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run] create issue for label 'schema-drift'", out)
        self.assertIn("[dry-run] create issue for label 'redteam'", out)


if __name__ == "__main__":
    unittest.main()
