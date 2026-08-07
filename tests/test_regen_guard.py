#!/usr/bin/env python3
"""Tests for scripts/regen_guard.py — the red-team regeneration classifier.

`redteam-regen.yml.template` ran on `schedule` with no `issues: write` and no
routing at all, so a failed weekly regeneration reached nobody. The fix is the
same shape as the one `live-probe.yml.template` already got, and so is the trap:
adding an addressee to a two-valued classification produces a guard that closes
an open issue on a comparison that never happened.

Each guarantee below is stated once, as a test that fails if that guarantee is
removed from the script. The mutation results are recorded in the PR
description.

Stdlib-only, no network, no token.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import regen_guard as rg  # noqa: E402


def _healthy(**overrides: object) -> rg.Run:
    """A run where everything happened: key present, both steps green, file on disk."""
    base = {
        "have_key": True,
        "generate": "success",
        "pull_request": "success",
        "artifact_ok": True,
    }
    base.update(overrides)
    return rg.Run(**base)  # type: ignore[arg-type]


class AttemptedTest(unittest.TestCase):
    def test_success_is_an_attempt(self) -> None:
        self.assertTrue(rg.attempted("success"))

    def test_failure_is_an_attempt(self) -> None:
        # Load-bearing: a failed regeneration has answered the guard's question
        # ("is the set still being refreshed?") with no. Excusing it as
        # not-measured is how the finding gets lost.
        self.assertTrue(rg.attempted("failure"))

    def test_skipped_is_not_an_attempt(self) -> None:
        self.assertFalse(rg.attempted("skipped"))

    def test_cancelled_is_not_an_attempt(self) -> None:
        self.assertFalse(rg.attempted("cancelled"))

    def test_a_step_that_never_started_is_not_an_attempt(self) -> None:
        self.assertFalse(rg.attempted(""))


class ClassifyTest(unittest.TestCase):
    def test_everything_ran_and_delivered_is_clear(self) -> None:
        self.assertEqual(rg.classify(_healthy()), rg.CLEAR)

    def test_no_attacker_key_is_unknown(self) -> None:
        # The state a template needs most: a repository that copied this file
        # and never configured the secret must not be told its red-team set is
        # broken, and must not have a real ticket closed on its behalf either.
        self.assertEqual(rg.classify(_healthy(have_key=False)), rg.UNKNOWN)

    def test_a_repository_that_never_enabled_the_workflow_is_unknown(self) -> None:
        # The whole shape of a fresh copy of the template: no secret, every step
        # skipped, and an artifact that is whatever was committed upstream. It
        # must move nothing in either direction.
        #
        # This does NOT pin the ORDER of the checks inside `classify()`. Swapping
        # the key test with the `attempted(generate)` test survives the suite,
        # and no test can catch it: both branches return UNKNOWN, so the order is
        # unobservable in the output. It is an equivalent mutation, recorded here
        # rather than papered over with a test that would only appear to cover
        # it. The order in the script is for the reader.
        never_enabled = _healthy(
            have_key=False, generate="skipped", pull_request="skipped"
        )
        self.assertEqual(rg.classify(never_enabled), rg.UNKNOWN)

    def test_a_failed_regeneration_is_a_finding(self) -> None:
        self.assertEqual(rg.classify(_healthy(generate="failure")), rg.FINDING)

    def test_a_skipped_regeneration_is_unknown_not_a_finding(self) -> None:
        self.assertEqual(rg.classify(_healthy(generate="skipped")), rg.UNKNOWN)

    def test_a_cancelled_run_is_unknown(self) -> None:
        self.assertEqual(rg.classify(_healthy(generate="cancelled")), rg.UNKNOWN)

    def test_green_regeneration_with_no_artifact_is_a_finding(self) -> None:
        # Exit 0 and nothing on disk. Trusting the exit code books this as
        # clear and CLOSES the issue — a silent failure signing off its own
        # all-clear.
        self.assertEqual(rg.classify(_healthy(artifact_ok=False)), rg.FINDING)

    def test_a_failed_pull_request_step_is_a_finding(self) -> None:
        # Regenerated cases that never reach review are not delivered.
        self.assertEqual(rg.classify(_healthy(pull_request="failure")), rg.FINDING)

    def test_a_cancelled_delivery_step_is_unknown_not_a_finding(self) -> None:
        # The regeneration succeeded and the artifact is on disk; only the step
        # carrying it to review never ran. Nothing was observed about delivery,
        # so nothing is claimed about it — the same rule as the run-level one,
        # applied one level down.
        self.assertEqual(rg.classify(_healthy(pull_request="")), rg.UNKNOWN)
        self.assertEqual(rg.classify(_healthy(pull_request="cancelled")), rg.UNKNOWN)


class RenderTest(unittest.TestCase):
    def test_every_state_names_what_was_observed(self) -> None:
        # Naming the state is half of it; the other half is the evidence. A
        # report that is only a verdict gets clicked away at portfolio scale.
        for state, run in (
            (rg.FINDING, _healthy(generate="failure")),
            (rg.CLEAR, _healthy()),
            (rg.UNKNOWN, _healthy(have_key=False)),
        ):
            body = rg.render(run, state, artifact="a/b.yaml")
            self.assertIn("a/b.yaml", body, state)
            self.assertIn("generate.sh", body, state)

    def test_unknown_does_not_claim_the_set_is_broken(self) -> None:
        body = rg.render(_healthy(have_key=False), rg.UNKNOWN, artifact="x.yaml")
        self.assertIn("Not measured", body)
        self.assertNotIn("did not deliver", body)


class ArtifactPresentTest(unittest.TestCase):
    def test_a_file_with_content_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.yaml"
            p.write_text("tests: []\n", encoding="utf-8")
            self.assertTrue(rg.artifact_present(p))

    def test_a_missing_file_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(rg.artifact_present(Path(d) / "nope.yaml"))

    def test_an_empty_file_is_absent(self) -> None:
        # `generate.sh` truncating its output and exiting 0 is the case this
        # catches; a zero-byte file is not a regenerated set.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.yaml"
            p.write_text("   \n", encoding="utf-8")
            self.assertFalse(rg.artifact_present(p))


class TruthyTest(unittest.TestCase):
    def test_the_string_true_is_a_key(self) -> None:
        self.assertTrue(rg._truthy("true"))

    def test_an_unset_secret_is_not_a_key(self) -> None:
        # GitHub renders `secrets.X != ''` as the empty string when the whole
        # expression is unavailable, not as `false`.
        self.assertFalse(rg._truthy(""))

    def test_the_string_false_is_not_a_key(self) -> None:
        self.assertFalse(rg._truthy("false"))


class EmitTest(unittest.TestCase):
    """`alert=` is the handover to drift_issue.py, and unknown must not write one."""

    def _emit(self, state: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "gh_out"
            out.write_text("", encoding="utf-8")
            import os

            prev = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(out)
            try:
                rg._emit(state)
            finally:
                if prev is None:
                    del os.environ["GITHUB_OUTPUT"]
                else:
                    os.environ["GITHUB_OUTPUT"] = prev
            return out.read_text(encoding="utf-8")

    def test_finding_alerts(self) -> None:
        self.assertIn("alert=true", self._emit(rg.FINDING))

    def test_clear_explicitly_does_not_alert(self) -> None:
        # `alert=false` rather than no alert: drift_issue.py requires an
        # explicit verdict before it will close anything.
        self.assertIn("alert=false", self._emit(rg.CLEAR))

    def test_unknown_writes_no_verdict_at_all(self) -> None:
        # The whole handover. An absent `alert=` is what makes drift_issue.py's
        # own `probe_ran()` classify this run as "did not run" — one rule, in
        # one tested place, rather than a second copy of it here.
        written = self._emit(rg.UNKNOWN)
        self.assertIn("state=unknown", written)
        self.assertNotIn("alert=", written)


class EndToEndTest(unittest.TestCase):
    """The states the routing step will actually see, through main()."""

    def test_unknown_writes_no_report_so_the_router_agrees(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.md"
            rc = rg.main(
                [
                    "--have-key",
                    "",
                    "--generate-outcome",
                    "skipped",
                    "--pr-outcome",
                    "skipped",
                    "--artifact",
                    str(Path(d) / "absent.yaml"),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertFalse(out.exists())

    def test_a_finding_writes_a_report_and_still_exits_zero(self) -> None:
        # Exit 0 on a finding is deliberate: the issue is the delivery, and a
        # red cron nobody watches is the problem being fixed, not the fix.
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "r.yaml"
            art.write_text("tests: []\n", encoding="utf-8")
            out = Path(d) / "report.md"
            rc = rg.main(
                [
                    "--have-key",
                    "true",
                    "--generate-outcome",
                    "failure",
                    "--pr-outcome",
                    "skipped",
                    "--artifact",
                    str(art),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertIn("did not deliver", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
