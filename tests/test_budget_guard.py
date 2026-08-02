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
            "BUDGET_MAX_FANOUT": "25",
            "BUDGET_MAX_FANOUT_EXPENSIVE": "10",
            "BUDGET_TOKENS_PER_TARGET": "0",
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
        argv = [
            "--state",
            str(self.state),
            "record",
            "--exit-code",
            str(exit_code),
            "--tokens",
            str(tokens),
        ]
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
        rc = bg.main(
            [
                "--state",
                str(self.state),
                "preflight",
                "--target",
                "owner/repo",
                "--out-report",
                str(report),
                "--out-summary",
                str(summary),
            ]
        )
        self.assertEqual(rc, bg.PREFLIGHT_SKIP)
        s = json.loads(summary.read_text())
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertTrue(s["hard_fail"])
        self.assertTrue(s["skipped_by_budget_guard"])
        self.assertFalse(s["green"])
        self.assertIn("circuit breaker OPEN", report.read_text())

    # -- fan-out: a portfolio sweep multiplies whatever one target costs -----

    def fanout_preflight(self, n: int, expensive: int = 0) -> int:
        return bg.main(
            [
                "--state",
                str(self.state),
                "preflight",
                "--fanout",
                str(n),
                "--fanout-expensive",
                str(expensive),
            ]
        )

    def test_a_sweep_within_the_cap_runs(self) -> None:
        os.environ["BUDGET_MAX_FANOUT"] = "25"
        self.assertEqual(self.fanout_preflight(12), bg.PREFLIGHT_RUN)

    def test_a_sweep_wider_than_the_cap_is_refused_before_it_clones_anything(
        self,
    ) -> None:
        # Width is the one property knowable in advance. The breaker and the token
        # window are both retrospective, and a fan-out's whole risk is that it
        # spends N times before anyone looks.
        os.environ["BUDGET_MAX_FANOUT"] = "10"
        self.assertEqual(self.fanout_preflight(11), bg.PREFLIGHT_SKIP)

    def test_the_expensive_predicate_count_has_its_own_cap(self) -> None:
        # `boot` starts a real server per target: wall-clock and sockets, not CPU.
        os.environ["BUDGET_MAX_FANOUT"] = "50"
        os.environ["BUDGET_MAX_FANOUT_EXPENSIVE"] = "3"
        self.assertEqual(self.fanout_preflight(20, expensive=3), bg.PREFLIGHT_RUN)
        self.assertEqual(self.fanout_preflight(20, expensive=4), bg.PREFLIGHT_SKIP)

    def test_a_projected_spend_over_the_window_is_refused_before_spending(self) -> None:
        os.environ["BUDGET_MAX_FANOUT"] = "50"
        os.environ["BUDGET_TOKENS_PER_TARGET"] = "500"  # window cap is 2500
        self.assertEqual(self.fanout_preflight(4), bg.PREFLIGHT_RUN)  # 2000
        self.assertEqual(self.fanout_preflight(6), bg.PREFLIGHT_SKIP)  # 3000

    def test_deterministic_predicates_project_nothing_by_default(self) -> None:
        # Today's predicates call no model, so the projection is a deliberate
        # no-op — the knob exists so the first predicate that DOES call one is
        # bounded on the day it is added, not discovered afterwards in a bill.
        os.environ.pop("BUDGET_TOKENS_PER_TARGET", None)
        os.environ["BUDGET_MAX_FANOUT"] = "50"
        self.assertEqual(bg.Limits().tokens_per_target, 0)
        self.assertEqual(self.fanout_preflight(40), bg.PREFLIGHT_RUN)

    def test_an_ordinary_single_target_run_is_unaffected(self) -> None:
        # The nightly passes no --fanout at all; nothing about its behaviour moves.
        os.environ["BUDGET_MAX_FANOUT"] = "1"
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_an_open_breaker_still_wins_over_a_legal_width(self) -> None:
        for _ in range(3):
            self.record(exit_code=1)
        self.assertEqual(self.fanout_preflight(2), bg.PREFLIGHT_SKIP)

    def test_the_width_is_recorded_so_the_history_can_be_read_back(self) -> None:
        # A hard-fail from a 15-target sweep and one from a single nightly are the
        # same row otherwise, and they are not the same event.
        bg.main(
            [
                "--state",
                str(self.state),
                "record",
                "--exit-code",
                "0",
                "--tokens",
                "0",
                "--fanout",
                "15",
            ]
        )
        self.assertEqual(self.load()["runs"][-1]["fanout"], 15)

    def test_the_refusal_names_the_knob_to_change(self) -> None:
        limits = bg.Limits()
        reason = bg.fanout_refusal(999, 0, 0, limits)
        self.assertIsNotNone(reason)
        self.assertIn("BUDGET_MAX_FANOUT", reason or "")

    def test_corrupt_state_starts_closed(self) -> None:
        self.state.write_text("{ not json")
        self.assertEqual(self.preflight(), bg.PREFLIGHT_RUN)

    def test_tokens_from_promptfoo_json(self) -> None:
        pf = Path(self.tmp.name) / "pf.json"
        pf.write_text(
            json.dumps({"results": {"stats": {"tokenUsage": {"total": 4242}}}})
        )
        self.assertEqual(bg.tokens_from_promptfoo(pf), 4242)
        # bare top-level stats also supported
        pf.write_text(json.dumps({"stats": {"tokenUsage": {"total": 7}}}))
        self.assertEqual(bg.tokens_from_promptfoo(pf), 7)
        # absent / garbage -> 0
        self.assertEqual(bg.tokens_from_promptfoo(Path(self.tmp.name) / "nope.json"), 0)


if __name__ == "__main__":
    unittest.main()
