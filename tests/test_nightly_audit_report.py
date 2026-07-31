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
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
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
            "target": "o/r", "target_sha": "deadbee", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
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
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 1},
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
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
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
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
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

    def test_determ_profile_flags_graded_layer_not_run(self) -> None:
        # Analysis T-C: a green determ-only run must be stamped so it is never read
        # as "red-team clear" — graded_layer_ran False + a loud report caveat.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "promptfoo_profile": "determ",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "green")
        self.assertEqual(s["promptfoo_profile"], "determ")
        self.assertFalse(s["graded_layer_ran"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("deterministic profile only", report)

    def test_graded_profile_marks_layer_ran(self) -> None:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "promptfoo_profile": "graded",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertTrue(s["graded_layer_ran"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("deterministic profile only", report)

    def test_invalid_target_metadata_is_hard_fail(self) -> None:
        # Analysis S-D: a tampered target (spaces / newlines / shell) fails
        # validation -> 'invalid' + hard-fail, never rendered raw.
        ev = self._write("ev.json", {
            "target": "o/r; rm -rf /\n## All green", "target_sha": "abc1234",
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertEqual(s["target"], "invalid")
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("rm -rf", report)

    def test_control_chars_stripped_from_report(self) -> None:
        # Analysis S-D: an untrusted promptfoo example with newlines / escapes must
        # not inject Markdown structure or terminal escapes into the report sink.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 1},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 1}, "results": [
            {"error": "boom\n## FAKE ALL GREEN\n\x1b[31mred"},
        ]}})
        s = self._classify(ev, pf)
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("\n## FAKE ALL GREEN", report)  # no injected heading line
        self.assertNotIn("\x1b", report)                 # no terminal escape

    # --- transport boot gate --------------------------------------------------

    def test_transport_boot_failure_is_a_finding_not_a_hard_fail(self) -> None:
        # The gate's whole point: a target that will not start is a statement about
        # the TARGET (exit 2), not about the infrastructure. Every other gate is
        # green here — that is exactly the situation the gate was added for.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 2, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "findings")
        self.assertEqual(s["_exit"], nar.EXIT_FINDINGS)
        self.assertTrue(s["transport_boot_fail"])
        self.assertFalse(s["hard_fail"])
        self.assertFalse(s["green"])
        # …and it must not be mislabelled as one of the neighbouring classes.
        self.assertFalse(s["schema_drift"])
        self.assertFalse(s["toolchain_fail"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Transport boot failure", report)

    def test_transport_boot_harness_failure_is_hard_fail(self) -> None:
        # 127 means the HARNESS could not run. That says nothing about whether the
        # target boots, so it must never be reported as a finding about the target.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 127, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertEqual(s["_exit"], nar.EXIT_HARD_FAIL)
        self.assertTrue(any("transport boot gate could not run" in r
                            for r in s["hard_fail_reasons"]))

    def test_evidence_without_the_boot_gate_is_hard_fail_never_green(self) -> None:
        # A Worker image still running the previous nightly-audit.sh ships evidence
        # with no transport_boot key. It genuinely did not run the gate, so the run
        # must NOT classify green — the Worker and Broker roll out together.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])
        self.assertEqual(s["gates"]["transport_boot_gate"], 127)

    def test_boot_gate_appears_in_the_rendered_gate_list(self) -> None:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        self._classify(ev, pf)
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("transport boot gate", report)

    # --- DNS-rebinding gate: the one gate with three outcomes -------------------

    def test_an_unconfigured_allowlist_is_neither_a_pass_nor_a_finding(self) -> None:
        # Exit 3. The run stays green — nothing is broken — but the report has to
        # say the control is absent, or a missing control reads like a passing one.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 3, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "green")
        self.assertEqual(s["_exit"], nar.EXIT_GREEN)
        self.assertFalse(s["host_allowlist_fail"])
        self.assertTrue(s["host_allowlist_unconfigured"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Control not configured", report)
        # …and the gate line must NOT wear a tick.
        self.assertIn("control not configured (exit 3)", report)
        self.assertNotIn("DNS-rebinding gate (inbound Host/Origin allow-list): ✅", report)
        # The headline cannot be left saying only "All gates green".
        self.assertIn("NOT configured", report)

    def test_a_rebinding_control_that_failed_is_a_finding(self) -> None:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 2, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "findings")
        self.assertEqual(s["_exit"], nar.EXIT_FINDINGS)
        self.assertTrue(s["host_allowlist_fail"])
        self.assertFalse(s["host_allowlist_unconfigured"])
        self.assertFalse(s["hard_fail"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("DNS-rebinding control failed", report)

    def test_a_rebinding_harness_failure_is_hard_fail_not_a_missing_control(self) -> None:
        # 127 is the harness. Reporting "the control is missing" on the strength of
        # a probe that never ran would be the same error as claiming a boot failure
        # we never observed.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 127, "shipped_artifact": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["host_allowlist_fail"])
        self.assertFalse(s["host_allowlist_unconfigured"])
        self.assertTrue(any("DNS-rebinding gate could not run" in r
                            for r in s["hard_fail_reasons"]))

    def test_evidence_without_the_rebinding_gate_is_hard_fail_never_green(self) -> None:
        # Same rollout rule as the boot gate: a Worker image predating this gate
        # genuinely did not run it, and must not classify green.
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])
        self.assertEqual(s["gates"]["host_allowlist_gate"], 127)

    # --- hung gates: a timeout is not a failure and not "could not run" ---------

    def _green_gates(self, **over: int) -> dict:
        gates = {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                 "transport_boot": 0, "host_allowlist": 0, "shipped_artifact": 0, "promptfoo_rc": 0}
        gates.update(over)
        return gates

    def _run(self, gates: dict, tests_collected: int = 7) -> dict:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234",
            "tests_collected": tests_collected, "gates": gates,
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        return self._classify(ev, pf)

    def test_a_hung_gate_is_hard_fail_and_is_named(self) -> None:
        # The name is the actionable part: "pytest hung" and "promptfoo hung" call
        # for entirely different next steps.
        s = self._run(self._green_gates(pytest=nar.GATE_TIMEOUT_RC))
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertEqual(s["_exit"], nar.EXIT_HARD_FAIL)
        self.assertTrue(s["hung"])
        self.assertEqual(s["hung_gates"], ["pytest"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Gate(s) HUNG", report)
        self.assertIn("pytest", report)
        # "Re-run it" is the wrong advice: a hang that disappears on the second
        # attempt has not been explained, it has been talked out of the record.
        self.assertIn("has not been explained", report)

    def test_a_sigkilled_gate_counts_as_hung_too(self) -> None:
        # `timeout --kill-after` returns 137 when the command ignored SIGTERM —
        # which is exactly the wedged-in-an-uninterruptible-read case this guard
        # exists for, so it must not fall through as an ordinary failure.
        s = self._run(self._green_gates(promptfoo_rc=nar.GATE_KILLED_RC))
        self.assertTrue(s["hung"])
        self.assertEqual(s["hung_gates"], ["promptfoo"])
        self.assertEqual(s["outcome"], "hard-fail")

    def test_a_hung_gate_is_not_also_counted_as_a_finding(self) -> None:
        # A timeout is not "ruff found problems". Folding it into toolchain_fail
        # would put a defect claim in the report that no gate ever made — and
        # would route it to a tracking issue asserting that class.
        s = self._run(self._green_gates(ruff=nar.GATE_TIMEOUT_RC,
                                        schema_drift=nar.GATE_TIMEOUT_RC,
                                        transport_boot=nar.GATE_TIMEOUT_RC,
                                        host_allowlist=nar.GATE_TIMEOUT_RC))
        self.assertTrue(s["hung"])
        self.assertFalse(s["toolchain_fail"])
        self.assertFalse(s["schema_drift"])
        self.assertFalse(s["transport_boot_fail"])
        self.assertFalse(s["host_allowlist_fail"])
        self.assertEqual(s["outcome"], "hard-fail")

    def test_every_gate_can_be_reported_as_hung_under_its_own_name(self) -> None:
        for key, label in (("ruff", "ruff"), ("mypy", "mypy"), ("pytest", "pytest"),
                           ("schema_drift", "schema-drift gate"),
                           ("transport_boot", "transport boot gate"),
                           ("host_allowlist", "DNS-rebinding gate"),
                           ("promptfoo_rc", "promptfoo")):
            with self.subTest(gate=key):
                s = self._run(self._green_gates(**{key: nar.GATE_TIMEOUT_RC}))
                self.assertEqual(s["hung_gates"], [label])

    def test_a_hung_gate_does_not_read_as_a_pass_in_the_gate_list(self) -> None:
        self._run(self._green_gates(mypy=nar.GATE_TIMEOUT_RC))
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("mypy: ⏱ HUNG", report)
        self.assertNotIn("mypy: ✅", report)

    # --- the silent zero: green, and nothing ran --------------------------------

    def test_zero_tests_with_a_green_gate_is_not_a_pass(self) -> None:
        s = self._run(self._green_gates(), tests_collected=0)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertEqual(s["_exit"], nar.EXIT_HARD_FAIL)
        self.assertTrue(s["no_tests_executed"])
        self.assertFalse(s["green"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("No tests executed", report)
        # The gate line must withdraw its tick: "✅ pass — 0 test(s)" is exactly
        # the sentence this class exists to prevent.
        self.assertIn("pytest: 🕳 0 tests executed (exit 0) — NOT a pass", report)
        self.assertNotIn("pytest: ✅", report)
        # And the closing advice must not be "re-run" without fixing anything.
        self.assertIn("Fix the suite's selection", report)

    def test_an_unknown_test_count_is_also_not_a_pass(self) -> None:
        # Same rule as promptfoo's rc-0-with-no-output: a green result whose suite
        # size cannot be established is indistinguishable from an empty one.
        s = self._run(self._green_gates(), tests_collected=-1)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertTrue(s["tests_unverified"])
        self.assertFalse(s["no_tests_executed"])

    def test_evidence_without_a_test_count_cannot_be_green(self) -> None:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "gates": self._green_gates(),
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertEqual(s["tests_collected"], nar.TESTS_UNKNOWN)

    def test_a_real_suite_passes_and_the_count_is_shown(self) -> None:
        s = self._run(self._green_gates(), tests_collected=217)
        self.assertEqual(s["outcome"], "green")
        self.assertFalse(s["no_tests_executed"])
        self.assertFalse(s["tests_unverified"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("217 test(s)", report)

    def test_a_red_pytest_gate_is_a_finding_not_a_silent_zero(self) -> None:
        # no_tests_executed only speaks about a gate that claimed success. A red
        # suite is an ordinary toolchain finding and must not be relabelled.
        s = self._run(self._green_gates(pytest=1), tests_collected=0)
        self.assertFalse(s["no_tests_executed"])
        self.assertTrue(s["toolchain_fail"])
        self.assertEqual(s["outcome"], "findings")

    def test_a_hung_pytest_gate_is_not_reported_as_an_empty_suite(self) -> None:
        # A killed suite leaves no summary line, so the count arrives as UNKNOWN.
        # The report must say it HUNG, not that it ran nothing.
        s = self._run(self._green_gates(pytest=nar.GATE_TIMEOUT_RC), tests_collected=-1)
        self.assertTrue(s["hung"])
        self.assertFalse(s["no_tests_executed"])
        self.assertFalse(s["tests_unverified"])

    # --- the shipped-artifact gate: what users actually install ----------------

    def test_a_stale_published_artifact_is_a_finding(self) -> None:
        s = self._run(self._green_gates(shipped_artifact=2))
        self.assertEqual(s["outcome"], "findings")
        self.assertTrue(s["shipped_artifact_fail"])
        self.assertFalse(s["hard_fail"])
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Shipped artifact diverges", report)
        self.assertIn("Green CI is not shipped software", report)

    def test_an_unreachable_index_is_hard_fail_not_in_sync(self) -> None:
        # The whole family's rule: a comparison that did not happen is never a
        # pass, and must not quietly become one.
        s = self._run(self._green_gates(shipped_artifact=127))
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["shipped_artifact_fail"])
        self.assertTrue(any("was NOT compared" in r for r in s["hard_fail_reasons"]))

    def test_a_hung_shipped_gate_is_hung_not_a_stale_release(self) -> None:
        s = self._run(self._green_gates(shipped_artifact=nar.GATE_TIMEOUT_RC))
        self.assertTrue(s["hung"])
        self.assertEqual(s["hung_gates"], ["shipped-artifact gate"])
        self.assertFalse(s["shipped_artifact_fail"])

    def test_evidence_without_the_shipped_gate_is_hard_fail_never_green(self) -> None:
        ev = self._write("ev.json", {
            "target": "o/r", "target_sha": "abc1234", "tests_collected": 7,
            "gates": {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0,
                      "transport_boot": 0, "host_allowlist": 0, "promptfoo_rc": 0},
        })
        pf = self._write("pf.json", {"results": {"stats": {"errors": 0}, "results": []}})
        s = self._classify(ev, pf)
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertEqual(s["gates"]["shipped_artifact_gate"], 127)

    def test_the_gate_appears_in_the_rendered_list(self) -> None:
        self._run(self._green_gates())
        report = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("shipped-artifact gate (install from PyPI + run it)", report)

    def test_every_evidence_gate_is_visible_in_the_summary(self) -> None:
        # The rollout runbook (docs/deployment/worker-broker-rollout.md) detects a
        # violated rollout order by diffing the evidence's gate names against the
        # summary's `gates` object: a name present in the evidence but absent from
        # the summary means the Broker ignored it — which is what an OLD Broker
        # does to a NEW Worker's findings, silently and in green.
        #
        # That detector only works while every `_GATE_NAMES` entry actually shows
        # up in the summary. Add a gate to the evidence contract and forget the
        # summary, and the runbook starts crying wolf on every healthy run.
        s = self._run(self._green_gates())
        rendered = set(s["gates"])
        for name in nar._GATE_NAMES:
            with self.subTest(gate=name):
                self.assertTrue(
                    any(name in key for key in rendered),
                    f"{name!r} is in the evidence contract but appears in no summary "
                    f"gate key ({sorted(rendered)}) — the rollout drift detector "
                    "would report it as ignored on every run")

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


class CountTestsTest(unittest.TestCase):
    """The whole `no_tests_executed` class rests on this parser, so it is tested
    against the literal shapes pytest and unittest actually emit. A parser that
    guessed would either invent an empty suite or hide one."""

    def test_unittest_ran_line(self) -> None:
        self.assertEqual(nar.count_tests("Ran 217 tests in 25.560s\n\nOK\n"), 217)

    def test_unittest_empty_discovery_is_zero_not_unknown(self) -> None:
        # The exact silent zero: discovery found nothing, the runner said OK, the
        # process exited 0.
        self.assertEqual(nar.count_tests("\n----\nRan 0 tests in 0.000s\n\nOK\n"), 0)

    def test_unittest_singular_test(self) -> None:
        self.assertEqual(nar.count_tests("Ran 1 test in 0.001s\n\nOK\n"), 1)

    def test_pytest_quiet_summary(self) -> None:
        self.assertEqual(nar.count_tests("....\n217 passed in 25.56s\n"), 217)

    def test_pytest_mixed_outcomes_are_summed(self) -> None:
        self.assertEqual(
            nar.count_tests("1 failed, 215 passed, 2 skipped in 25.56s\n"), 218)

    def test_pytest_no_tests_ran_is_zero(self) -> None:
        self.assertEqual(nar.count_tests("no tests ran in 0.01s\n"), 0)

    def test_pytest_deselected_only_counts_as_zero_executed(self) -> None:
        # Every test deselected by a marker expression: the suite executed nothing,
        # which is the finding — so `deselected` must NOT count towards the total.
        self.assertEqual(nar.count_tests("no tests ran in 0.02s\n"), 0)
        self.assertEqual(nar.count_tests("4 passed, 120 deselected in 1.20s\n"), 4)

    def test_the_last_run_wins(self) -> None:
        # A gate may run the suite more than once; the final summary is the one the
        # exit code describes.
        self.assertEqual(
            nar.count_tests("Ran 5 tests in 0.1s\nOK\nRan 217 tests in 25.5s\nOK\n"), 217)

    def test_unparseable_output_is_unknown_never_zero(self) -> None:
        # Reporting "no tests" because we could not read the log would invent the
        # very finding this parser exists to catch.
        self.assertEqual(nar.count_tests("segfault\n"), nar.TESTS_UNKNOWN)
        self.assertEqual(nar.count_tests(""), nar.TESTS_UNKNOWN)

    def test_collected_line_is_the_fallback(self) -> None:
        self.assertEqual(nar.count_tests("collected 42 items\n"), 42)

    def test_count_tests_mode_prints_the_number(self) -> None:
        # The measurement path nightly-audit.sh actually calls.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pytest.log"
            log.write_text("Ran 217 tests in 25.560s\n\nOK\n", encoding="utf-8")
            out = io.StringIO()
            old = sys.argv
            sys.argv = ["nightly_audit_report.py", "--count-tests", str(log)]
            try:
                with contextlib.redirect_stdout(out):
                    rc = nar.main()
            finally:
                sys.argv = old
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "217")

    def test_count_tests_mode_reports_unknown_for_a_missing_log(self) -> None:
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["nightly_audit_report.py", "--count-tests", "/does/not/exist.log"]
        try:
            with contextlib.redirect_stdout(out):
                rc = nar.main()
        finally:
            sys.argv = old
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), str(nar.TESTS_UNKNOWN))


if __name__ == "__main__":
    unittest.main()
