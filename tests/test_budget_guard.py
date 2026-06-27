#!/usr/bin/env python3
"""Tests for scripts/budget_guard.py — the Phase-5 budget guardrails.

Stdlib-only (`python3 -m unittest`), no third-party deps, matching the rest of
the auditor repo's stdlib tooling (scripts/live_probe.py). Each test drives the
module through its CLI entrypoint against a throwaway state file in a tmp dir,
and manipulates `os.environ` to set the limits — exactly how nightly-audit.sh
invokes it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import budget_guard as bg  # noqa: E402


class BudgetGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "budget-state.json"
        # Deterministic, small limits so the tests are fast and explicit.
        self._env = {
            "BUDGET_TOKENS_PER_RUN": "1000",
            "BUDGET_TOKENS_WINDOW": "2500",
            "BUDGET_WINDOW_SECONDS": "86400",
            "BUDGET_BREAKER_THRESHOLD": "3",
            "BUDGET_BREAKER_COOLDOWN_SECONDS": "21600",
            "BUDGET_MAX_ITERATIONS": "25",
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def preflight(self) -> int:
        return bg.main(["--state", str(self.state), "preflight"])

    def record(self, exit_code: int, tokens: int = 0, strict: bool = False) -> int:
        argv = ["--state", str(self.state), "record", "--exit-code", str(exit_code),
                "--tokens", str(tokens)]
        if strict:
            argv.append("--strict")
        return bg.main(argv)

    def load(self) -> dict:
        return json.loads(self.state.read_text())

    # -- tests ------------------------------------------------------------
    def test_fresh_state_runs(self) -> None:
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_green_run_keeps_breaker_closed(self) -> None:
        self.record(exit_code=0, tokens=100)
        self.assertEqual(self.load()["breaker"]["state"], "closed")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_findings_run_does_not_trip_breaker(self) -> None:
        # exit 2 (findings) is a successful audit, not a hard failure.
        for _ in range(5):
            self.record(exit_code=2, tokens=100)
        self.assertEqual(self.load()["breaker"]["state"], "closed")
        self.assertEqual(self.load()["breaker"]["consecutive_hard_fails"], 0)

    def test_consecutive_hard_fails_trip_breaker_and_skip(self) -> None:
        for _ in range(3):  # threshold = 3
            self.record(exit_code=1, tokens=10)
        self.assertEqual(self.load()["breaker"]["state"], "open")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_SKIP)

    def test_two_hard_fails_below_threshold_still_runs(self) -> None:
        self.record(exit_code=1, tokens=10)
        self.record(exit_code=1, tokens=10)
        self.assertEqual(self.load()["breaker"]["state"], "closed")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_success_after_fails_clears_streak(self) -> None:
        self.record(exit_code=1, tokens=10)
        self.record(exit_code=1, tokens=10)
        self.record(exit_code=0, tokens=10)  # green resets
        self.assertEqual(self.load()["breaker"]["consecutive_hard_fails"], 0)

    def test_cooldown_half_open_then_close_on_success(self) -> None:
        for _ in range(3):
            self.record(exit_code=1, tokens=10)
        self.assertEqual(self.load()["breaker"]["state"], "open")
        # Force the cooldown to have elapsed by backdating opened_at.
        os.environ["BUDGET_BREAKER_COOLDOWN_SECONDS"] = "0"
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)  # half-open trial
        self.assertEqual(self.load()["breaker"]["state"], "half_open")
        self.record(exit_code=0, tokens=10)  # trial succeeds
        self.assertEqual(self.load()["breaker"]["state"], "closed")

    def test_half_open_trial_failure_reopens(self) -> None:
        for _ in range(3):
            self.record(exit_code=1, tokens=10)
        os.environ["BUDGET_BREAKER_COOLDOWN_SECONDS"] = "0"
        self.preflight()  # -> half_open
        self.record(exit_code=1, tokens=10)  # trial fails again
        self.assertEqual(self.load()["breaker"]["state"], "open")

    def test_per_run_token_ceiling_trips_breaker_even_when_green(self) -> None:
        self.record(exit_code=0, tokens=1500)  # > per-run ceiling 1000
        self.assertEqual(self.load()["breaker"]["state"], "open")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_SKIP)

    def test_rolling_window_exhaustion_skips(self) -> None:
        # window cap 2500; three 900-token green runs => 2700 > 2500.
        self.record(exit_code=0, tokens=900)
        self.record(exit_code=0, tokens=900)
        self.record(exit_code=0, tokens=900)
        self.assertGreater(self.load()["window"]["tokens_used"], 2500)
        self.assertEqual(self.preflight(), bg.PREFLIGHT_SKIP)

    def test_strict_record_returns_nonzero_on_breach(self) -> None:
        self.assertEqual(self.record(exit_code=0, tokens=1500, strict=True), 1)

    def test_reset_closes_breaker(self) -> None:
        for _ in range(3):
            self.record(exit_code=1, tokens=10)
        bg.main(["--state", str(self.state), "reset", "--reason", "test"])
        self.assertEqual(self.load()["breaker"]["state"], "closed")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_preflight_skip_writes_hardfail_summary(self) -> None:
        for _ in range(3):
            self.record(exit_code=1, tokens=10)
        report = Path(self.tmp.name) / "r.md"
        summary = Path(self.tmp.name) / "s.json"
        rc = bg.main([
            "--state", str(self.state), "preflight",
            "--target", "owner/repo",
            "--out-report", str(report), "--out-summary", str(summary),
        ])
        self.assertEqual(rc, bg.PREFLIGHT_SKIP)
        s = json.loads(summary.read_text())
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertTrue(s["hard_fail"])
        self.assertTrue(s["skipped_by_budget_guard"])
        self.assertFalse(s["green"])
        self.assertIn("circuit breaker OPEN", report.read_text())

    def test_corrupt_state_starts_closed(self) -> None:
        self.state.write_text("{ not json")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_tokens_from_promptfoo_json(self) -> None:
        pf = Path(self.tmp.name) / "pf.json"
        pf.write_text(json.dumps({"results": {"stats": {"tokenUsage": {"total": 4242}}}}))
        self.assertEqual(bg.tokens_from_promptfoo(pf), 4242)
        # bare top-level stats also supported
        pf.write_text(json.dumps({"stats": {"tokenUsage": {"total": 7}}}))
        self.assertEqual(bg.tokens_from_promptfoo(pf), 7)
        # absent / garbage -> 0
        self.assertEqual(bg.tokens_from_promptfoo(Path(self.tmp.name) / "nope.json"), 0)


if __name__ == "__main__":
    unittest.main()
