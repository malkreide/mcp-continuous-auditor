#!/usr/bin/env python3
"""Tests for scripts/nightly_audit_report.py — the audit classifier.

Focus on the Broker-side classification path (Analysis S2): the trusted Broker
re-derives the verdict from a Worker's RAW evidence via ``--from-evidence``, so a
compromised Worker cannot forge a green outcome. Stdlib-only (`python3 -m
unittest`), matching the rest of the repo's tooling.
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

import nightly_audit_report as nar  # noqa: E402


class ClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, obj: object) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def _classify(self, evidence: Path, promptfoo: Path | str = "") -> dict:
        """Run main() through --from-evidence exactly as the Broker handler does."""
        report = self.dir / "report.md"
        summary = self.dir / "summary.json"
        argv = [
            "--from-evidence", str(evidence),
            "--promptfoo-json", str(promptfoo),
            "--out-report", str(report),
            "--out-summary", str(summary),
        ]
        old = sys.argv
        sys.argv = ["nightly_audit_report.py", *argv]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = nar.main()
        except SystemExit as e:  # pragma: no cover - argparse errors
            rc = int(e.code or 0)
        finally:
            sys.argv = old
        out = json.loads(summary.read_text(encoding="utf-8"))
        out["_exit"] = rc
        return out

    # --- happy path -----------------------------------------------------------

    def test_green_evidence_classifies_green(self) -> None:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "green")
        self.assertTrue(s["green"])
        self.assertEqual(s["_exit"], nar.EXIT_GREEN)
        # target + sha are taken from the evidence, not the (absent) --target flag.
        self.assertEqual(s["target"], "o/r")
        self.assertEqual(s["target_sha"], "abc1234")

    # --- the S2 safety properties --------------------------------------------

    def test_garbled_evidence_is_hard_fail_never_green(self) -> None:
        bad = self.dir / "bad.json"
        bad.write_text("this is not json {{{", encoding="utf-8")
        s = self._classify(bad, "/does/not/exist.json")
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])
        self.assertEqual(s["_exit"], nar.EXIT_HARD_FAIL)

    def test_absent_evidence_is_hard_fail_never_green(self) -> None:
        s = self._classify(self.dir / "missing.json", "")
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])

    def test_forged_green_exit_codes_are_caught_by_promptfoo_evidence(self) -> None:
        # A compromised Worker claims every gate exit code is 0, but the raw
        # promptfoo JSON it shipped still carries real failures. The Broker
        # classifies from the promptfoo evidence too -> findings, NOT green.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "deadbee",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": [
            {"success": False, "testCase": {"description": "schema"},
             "gradingResult": {"componentResults": [
                 {"pass": False, "assertion": {"type": "is-json"}}]}},
            {"success": False, "testCase": {"description": "pii", "metadata": {"pluginId": "pii"}},
             "gradingResult": {"componentResults": [
                 {"pass": False, "assertion": {"type": "llm-rubric"}}]}},
        ]}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "findings")
        self.assertFalse(s["green"])
        self.assertTrue(s["schema_drift"])
        self.assertTrue(s["redteam"])

    def test_promptfoo_provider_error_in_evidence_is_hard_fail(self) -> None:
        # An unresolvable/unauthorised grader model must HARD-fail, never pass.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "c0ffee",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 1},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 1}, "results": [
            {"error": "model provider unauthorised"},
        ]}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertTrue(s["hard_fail"])

    def test_promptfoo_success_without_output_is_hard_fail(self) -> None:
        # Analysis S-A: evidence claims promptfoo passed (rc 0) but ships NO
        # promptfoo JSON. The eval cannot be verified -> hard-fail, never green.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 0},
        })
        s = self._classify(ev, "")  # no --promptfoo-json
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])
        self.assertTrue(any("evidence incomplete" in r for r in s["hard_fail_reasons"]))

    def test_unclassified_failure_is_other_not_schema_drift(self) -> None:
        # Analysis T-F: a failure that is neither a contract/schema assertion nor a
        # red-team hit must classify as its own 'other' finding — NOT be folded into
        # schema_drift (which would falsely report a drift).
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": [
            {"success": False, "testCase": {"description": "injection negative-test"},
             "gradingResult": {"componentResults": [
                 {"pass": False, "assertion": {"type": "not-contains"}}]}},
        ]}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "findings")
        self.assertFalse(s["green"])
        self.assertTrue(s["other_findings"])
        self.assertFalse(s["schema_drift"])  # the key property: not mislabelled
        self.assertFalse(s["redteam"])

    def test_partial_evidence_missing_gate_defaults_to_hard_fail(self) -> None:
        # A gate omitted from the evidence must read as could-not-run (127),
        # never as an implicit pass.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "beef",
            "gates": {"ruff": 0, "mypy": 0},  # pytest / schema_drift / promptfoo_rc missing
        })
        s = self._classify(ev, "")
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])


if __name__ == "__main__":
    unittest.main()
