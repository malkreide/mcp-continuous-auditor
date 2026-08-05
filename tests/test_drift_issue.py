#!/usr/bin/env python3
"""Tests for scripts/drift_issue.py — the three-state tracking-issue router.

The classification this covers used to be an inline `actions/github-script`
block in `live-probe.yml.template`, where it had two states and could not be
tested at all. The tests below are written against the failure the third state
exists to prevent: a run that compared nothing must never close an issue, and
must never open one either.

Each guarantee is stated once, as a test that fails if the guarantee is removed.
The mutation results are recorded in the PR description; the short version is
that every branch in `classify()` and `plan()` is load-bearing.

Stdlib-only, no network, no token.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import drift_issue as di  # noqa: E402


def _ran(name: str = "p", alert: str = "false") -> di.Probe:
    """A probe that completed: success, an explicit verdict, a report on disk."""
    return di.Probe(name=name, outcome="success", alert=alert, report_ok=True)


class ProbeRanTest(unittest.TestCase):
    def test_success_with_verdict_and_report_ran(self) -> None:
        self.assertTrue(di.probe_ran(_ran()))

    def test_a_crashed_step_did_not_run(self) -> None:
        self.assertFalse(di.probe_ran(_ran()._replace(outcome="failure")))

    def test_a_skipped_step_did_not_run(self) -> None:
        # The recall canary is skipped whenever `pip install -e .` fails. A
        # skipped check reads exactly like a passing one unless this is false.
        self.assertFalse(di.probe_ran(_ran()._replace(outcome="skipped")))

    def test_a_step_that_never_started_did_not_run(self) -> None:
        self.assertFalse(di.probe_ran(_ran()._replace(outcome="")))

    def test_a_missing_verdict_did_not_run(self) -> None:
        # A probe that crashed before writing `alert=` to $GITHUB_OUTPUT leaves
        # the empty string, which is neither true nor false.
        self.assertFalse(di.probe_ran(_ran()._replace(alert="")))

    def test_a_garbled_verdict_did_not_run(self) -> None:
        self.assertFalse(di.probe_ran(_ran()._replace(alert="TRUE")))

    def test_an_empty_or_missing_report_did_not_run(self) -> None:
        # Exit 0 without a report is a real failure mode: the script returned
        # before it wrote anything. The exit code alone would call that a pass.
        self.assertFalse(di.probe_ran(_ran()._replace(report_ok=False)))


class ClassifyTest(unittest.TestCase):
    def test_all_probes_ran_and_none_alerted_is_clear(self) -> None:
        self.assertEqual(di.classify([_ran("a"), _ran("b")]), di.CLEAR)

    def test_a_probe_that_alerted_is_a_finding(self) -> None:
        self.assertEqual(di.classify([_ran("a"), _ran("b", "true")]), di.FINDING)

    def test_one_probe_missing_is_unknown_not_clear(self) -> None:
        # THE test. Fold `unknown` into `clear` and this run closes an open issue
        # on the strength of a comparison that never happened.
        probes = [_ran("a"), _ran("b")._replace(outcome="skipped")]
        self.assertEqual(di.classify(probes), di.UNKNOWN)

    def test_one_probe_missing_is_unknown_not_a_finding(self) -> None:
        # And the other direction: a probe that could not run is a deployment
        # problem, not a finding. Opening a ticket for it every week is the noise
        # that gets guards switched off.
        self.assertEqual(di.classify([_ran("a")._replace(report_ok=False)]), di.UNKNOWN)

    def test_an_alert_survives_an_incomplete_run(self) -> None:
        # Order inside classify() is load-bearing: a real, reported finding must
        # not be swallowed because a sibling probe also failed to run.
        probes = [_ran("a", "true"), _ran("b")._replace(outcome="failure")]
        self.assertEqual(di.classify(probes), di.FINDING)

    def test_an_alert_from_a_probe_that_did_not_run_is_not_a_finding(self) -> None:
        # `alert=true` left over from a step that then crashed proves nothing.
        probes = [_ran("a", "true")._replace(report_ok=False)]
        self.assertEqual(di.classify(probes), di.UNKNOWN)

    def test_no_probes_at_all_is_unknown(self) -> None:
        # An empty list is the absence of evidence. `all([])` is True, so the
        # naive implementation calls this CLEAR and closes on nothing at all.
        self.assertEqual(di.classify([]), di.UNKNOWN)


class PlanTest(unittest.TestCase):
    MARKER = "<!-- live-probe-drift -->"

    def _open(self) -> list[dict]:
        """The guard's own open issue, as the REST API would return it."""
        return [{"number": 12, "body": f"head\n{self.MARKER}\nbody"}]

    def test_finding_with_no_open_issue_creates(self) -> None:
        self.assertEqual(di.plan(di.FINDING, [], self.MARKER), ("create", None))

    def test_finding_with_an_open_issue_comments(self) -> None:
        self.assertEqual(
            di.plan(di.FINDING, self._open(), self.MARKER), ("comment", 12)
        )

    def test_clear_with_an_open_issue_closes(self) -> None:
        # The half that was missing. Without it the issue only ever grows.
        self.assertEqual(di.plan(di.CLEAR, self._open(), self.MARKER), ("close", 12))

    def test_clear_with_no_open_issue_does_nothing(self) -> None:
        self.assertEqual(di.plan(di.CLEAR, [], self.MARKER), ("noop", None))

    def test_unknown_never_closes(self) -> None:
        self.assertEqual(di.plan(di.UNKNOWN, self._open(), self.MARKER), ("noop", None))

    def test_unknown_never_creates(self) -> None:
        self.assertEqual(di.plan(di.UNKNOWN, [], self.MARKER), ("noop", None))

    def test_an_issue_without_the_marker_is_not_ours(self) -> None:
        # Same label, opened by a human. Closing it would be the guard reaching
        # outside what it owns.
        foreign = [{"number": 3, "body": "a human filed this under the same label"}]
        self.assertEqual(di.plan(di.CLEAR, foreign, self.MARKER), ("noop", None))
        self.assertEqual(di.plan(di.FINDING, foreign, self.MARKER), ("create", None))

    def test_a_different_guards_marker_is_not_ours(self) -> None:
        other = [{"number": 4, "body": "<!-- some-other-guard -->"}]
        self.assertEqual(di.plan(di.CLEAR, other, self.MARKER), ("noop", None))

    def test_an_issue_with_no_body_is_handled(self) -> None:
        # The REST API returns `"body": null` for a body-less issue.
        self.assertEqual(
            di.plan(di.CLEAR, [{"number": 5, "body": None}], self.MARKER),
            ("noop", None),
        )

    def test_an_unrecognised_state_raises(self) -> None:
        with self.assertRaises(ValueError):
            di.plan("probably-fine", [], self.MARKER)


class ParseProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_three_fields_without_a_report(self) -> None:
        probe = di.parse_probe("canary:success:true", self.dir)
        self.assertEqual(probe, di.Probe("canary", "success", "true", report_ok=True))

    def test_a_present_report_is_ok(self) -> None:
        (self.dir / "r.md").write_text("# drift\n", encoding="utf-8")
        self.assertTrue(di.parse_probe("p:success:false:r.md", self.dir).report_ok)

    def test_a_missing_report_is_not_ok(self) -> None:
        self.assertFalse(di.parse_probe("p:success:false:gone.md", self.dir).report_ok)

    def test_a_whitespace_only_report_is_not_ok(self) -> None:
        # `: > report.md` in a shell step leaves an empty file behind, which the
        # existence check alone would accept.
        (self.dir / "r.md").write_text("   \n\n", encoding="utf-8")
        self.assertFalse(di.parse_probe("p:success:false:r.md", self.dir).report_ok)

    def test_a_report_path_may_contain_colons(self) -> None:
        probe = di.parse_probe("p:success:false:a:b.md", self.dir)
        self.assertEqual(probe.name, "p")
        self.assertEqual(probe.alert, "false")

    def test_too_few_fields_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            di.parse_probe("p:success", self.dir)


class MainTest(unittest.TestCase):
    """main()'s no-network paths: classification, exit codes, dry-run plan."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.body = self.dir / "body.md"
        self.body.write_text("# report\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *probes: str, extra: list[str] | None = None) -> tuple[int, str]:
        argv = [
            "--repo",
            "o/r",
            "--marker",
            "live-probe-drift",
            "--label",
            "schema-drift",
            "--title",
            "[live-probe] drift",
            "--body-file",
            str(self.body),
            # Point at a variable that is guaranteed unset, so the run is decided
            # by the arguments and not by whatever token the ambient environment
            # happens to carry. Without this the suite passes on a laptop and
            # tries to reach api.github.com inside CI.
            "--token-env",
            "DRIFT_ISSUE_TEST_TOKEN_DELIBERATELY_UNSET",
            "--dry-run",
        ]
        for spec in probes:
            argv += ["--probe", spec]
        argv += extra or []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = di.main(argv)
        return rc, buf.getvalue()

    def test_unknown_run_exits_zero_and_plans_nothing(self) -> None:
        # A cron that cannot run its probes must not go red — a red cron nobody
        # opens is the problem this file exists to solve — but it must say so.
        rc, out = self._run("probe:failure::")
        self.assertEqual(rc, 0)
        self.assertIn("state: unknown", out)
        self.assertIn("DID NOT RUN", out)
        self.assertIn("[dry-run] noop", out)

    def test_unknown_run_says_why_it_touched_nothing(self) -> None:
        _rc, out = self._run("probe:skipped::")
        self.assertIn("compared nothing", out)

    def test_clear_run_plans_against_the_issue_list(self) -> None:
        rc, out = self._run("probe:success:false")
        self.assertEqual(rc, 0)
        self.assertIn("state: clear", out)
        # No token in the environment, so the plan is honest about its blind spot
        # rather than silently reporting `noop` as if it had looked.
        self.assertIn("cannot appear below", out)

    def test_finding_run_is_classified(self) -> None:
        rc, out = self._run("probe:success:true")
        self.assertEqual(rc, 0)
        self.assertIn("state: finding", out)

    def test_a_malformed_probe_spec_is_a_usage_error(self) -> None:
        rc, out = self._run("nonsense")
        self.assertEqual(rc, 2)
        self.assertIn("NAME:OUTCOME:ALERT", out)

    def test_no_probes_at_all_is_unknown_not_clear(self) -> None:
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("state: unknown", out)

    def test_an_invalid_repo_fails_only_when_it_would_act(self) -> None:
        rc, _out = self._run("probe:success:false", extra=["--repo", "not-a-repo"])
        self.assertEqual(rc, 2)

    def test_an_invalid_repo_is_irrelevant_when_the_state_is_unknown(self) -> None:
        # Nothing is going to be called, so nothing needs to be valid. Failing
        # here would turn a benign incomplete run into a red cron.
        rc, _out = self._run("probe:failure::", extra=["--repo", "not-a-repo"])
        self.assertEqual(rc, 0)


class ExecuteTest(unittest.TestCase):
    """The REST layer, with `_req` swapped for a recorder. No network."""

    def setUp(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._real = di._req

        def fake(method: str, url: str, token: str, body: dict | None = None) -> dict:
            self.calls.append((method, url, body))
            return {"number": 99}

        di._req = fake  # type: ignore[assignment]

    def tearDown(self) -> None:
        di._req = self._real  # type: ignore[assignment]

    def _kw(self) -> dict:
        return {
            "repo": "o/r",
            "label": "schema-drift",
            "title": "t",
            "marker": "<!-- m -->",
            "body": "b",
            "token": "x",
        }

    def test_noop_makes_no_call(self) -> None:
        di.execute("noop", None, **self._kw())
        self.assertEqual(self.calls, [])

    def test_create_posts_the_marker_into_the_body(self) -> None:
        di.execute("create", None, **self._kw())
        _method, _url, body = self.calls[0]
        # Without the marker in the body the next run cannot find this issue and
        # opens a second one every week.
        self.assertIn("<!-- m -->", body["body"])

    def test_close_comments_before_it_closes(self) -> None:
        di.execute("close", 12, **self._kw())
        methods = [c[0] for c in self.calls]
        self.assertEqual(methods, ["POST", "PATCH"])
        self.assertEqual(self.calls[1][2]["state"], "closed")

    def test_close_names_a_reason(self) -> None:
        di.execute("close", 12, **self._kw())
        self.assertEqual(self.calls[1][2]["state_reason"], "completed")

    def test_an_unrecognised_action_raises(self) -> None:
        with self.assertRaises(ValueError):
            di.execute("delete-the-repo", 1, **self._kw())


if __name__ == "__main__":
    unittest.main()
