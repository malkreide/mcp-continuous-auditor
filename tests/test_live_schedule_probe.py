#!/usr/bin/env python3
"""Tests for scripts/live_schedule_probe.py — do the live tests run anywhere?

The probe reads a checkout, so these tests build real checkouts in a temp dir:
a ``tests/`` file carrying ``@pytest.mark.live`` and a ``.github/workflows/``
directory, and then run the whole verdict over them.

Two groups carry most of the weight.

``MarkerExpressionTest`` pins the piece a substring match gets wrong in both
directions: ``-m "not live"`` excludes and ``-m "not slow"`` does not, though
only one of them contains the word.

``NotAFindingTest`` pins the direction that would retire the probe. A scheduled
workflow whose test step is a wrapper, a marker expression too big to decide, a
workflow file that does not parse — none of those are evidence that the suite is
unscheduled, and every one of them must come out as UNVERIFIED rather than as a
finding.

Stdlib-only and offline. The workflow parsing needs PyYAML; the classes that
depend on it skip loudly when it is missing, the same way the rest of the suite
does — see .github/workflows/tests.yml, which installs it so the skip cannot
hide a check that never ran.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import live_schedule_probe as lsp  # noqa: E402

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - CI installs it
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_schedule_probe.py"

LIVE_TEST = """\
import pytest


@pytest.mark.live
def test_the_real_endpoint_answers():
    assert True
"""

SCHEDULED_WORKFLOW = """\
name: live
on:
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch: {}
jobs:
  live:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -m live -v
      - name: Issue on failure
        if: failure()
        uses: actions/github-script@v7
        with:
          script: console.log("open an issue")
"""

PR_ONLY_WORKFLOW = """\
name: ci
on:
  pull_request:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest tests/ -m "not live"
"""


def make_target(tmp: Path, *, tests: dict[str, str], workflows: dict[str, str]) -> Path:
    root = tmp / "target"
    (root / "tests").mkdir(parents=True)
    for name, body in tests.items():
        (root / "tests" / name).write_text(body, encoding="utf-8")
    if workflows:
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        for name, body in workflows.items():
            (wf / name).write_text(body, encoding="utf-8")
    return root


class MarkerExpressionTest(unittest.TestCase):
    """`-m` decides which tests run, so the probe has to decide what `-m` means."""

    def test_no_marker_expression_admits_live(self):
        """`pytest tests/` runs the live suite. That is the question, not a loophole."""
        self.assertTrue(lsp.admits_live(None))
        self.assertTrue(lsp.admits_live(""))

    def test_not_live_excludes(self):
        self.assertFalse(lsp.admits_live("not live"))
        self.assertFalse(lsp.admits_live("not  live"))

    def test_live_selects(self):
        self.assertTrue(lsp.admits_live("live"))
        self.assertTrue(lsp.admits_live("live and not slow"))
        self.assertTrue(lsp.admits_live("live or integration"))

    def test_an_unrelated_exclusion_still_admits_live(self):
        """The case a substring match gets wrong: no `live` in the text, live selected."""
        self.assertTrue(lsp.admits_live("not slow"))
        self.assertTrue(lsp.admits_live("not slow and not flaky"))

    def test_an_exclusion_that_happens_to_mention_live(self):
        self.assertFalse(lsp.admits_live("not live and not slow"))
        self.assertFalse(lsp.admits_live("(not live)"))

    def test_undecidable_expressions_raise_rather_than_pass(self):
        with self.assertRaises(lsp.MarkerExprError):
            lsp.admits_live("live and (")
        with self.assertRaises(lsp.MarkerExprError):
            lsp.admits_live("live > 3")
        with self.assertRaises(lsp.MarkerExprError):
            lsp.admits_live(" or ".join(f"m{i}" for i in range(20)))


class PytestCallTest(unittest.TestCase):
    """Which `run:` lines are a pytest invocation — and which only look like one."""

    def test_installing_pytest_is_not_running_it(self):
        """The false positive that would make an install satisfy the check."""
        calls, opaque = lsp.parse_pytest_calls("pip install pytest pytest-asyncio")
        self.assertEqual(calls, [])
        self.assertEqual(opaque, [])

    def test_the_runner_prefixes(self):
        for line in (
            "pytest -m live",
            "python -m pytest -m live",
            "python3 -m pytest -m live",
            "uv run pytest -m live",
            "uv run --frozen pytest -m live",
            "poetry run pytest -m live",
            "LIVE=1 pytest -m live",
            ".venv/bin/pytest -m live",
        ):
            with self.subTest(line=line):
                calls, _ = lsp.parse_pytest_calls(line)
                self.assertEqual(len(calls), 1, line)
                self.assertEqual(calls[0].marker_expr, "live")
                self.assertIs(calls[0].admits, True)

    def test_chained_commands_are_split(self):
        calls, _ = lsp.parse_pytest_calls('uv sync && pytest tests/ -m "not live"')
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].admits, False)

    def test_a_wrapper_is_recorded_as_opaque_not_as_absence(self):
        for line in (
            "make live-test",
            "tox -e live",
            "./scripts/run-live.sh",
            "nox -s live",
        ):
            with self.subTest(line=line):
                calls, opaque = lsp.parse_pytest_calls(line)
                self.assertEqual(calls, [])
                self.assertEqual(opaque, [line])

    def test_an_explicit_live_flag_counts(self):
        calls, _ = lsp.parse_pytest_calls("pytest tests/ --run-live")
        self.assertIs(calls[0].admits, True)


class CronCadenceTest(unittest.TestCase):
    """`SPARSE_CADENCE` is a note, but it still has to be right about the cron."""

    def test_weekly_and_more_often(self):
        for expr in ("17 6 * * 1", "0 3 * * *", "0 3 */3 * *", "0 3 1,8,15,22 * *"):
            with self.subTest(expr=expr):
                self.assertIs(lsp.fires_at_least_weekly(expr), True)

    def test_monthly_is_sparse(self):
        for expr in ("0 3 1 * *", "0 3 1 1 *"):
            with self.subTest(expr=expr):
                self.assertIs(lsp.fires_at_least_weekly(expr), False)

    def test_both_day_fields_restricted_uses_crons_or(self):
        """cron ORs day-of-month with day-of-week: the weekday alone is weekly."""
        self.assertIs(lsp.fires_at_least_weekly("0 3 1 * 1"), True)

    def test_an_unreadable_cron_is_not_decided(self):
        self.assertIsNone(lsp.fires_at_least_weekly("@weekly"))
        self.assertIsNone(lsp.fires_at_least_weekly("0 3 L * *"))


class NoLiveSuiteTest(unittest.TestCase):
    def test_a_repo_without_the_marker_is_not_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_unit.py": "def test_x():\n    assert True\n"},
                workflows={"ci.yml": PR_ONLY_WORKFLOW},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.NO_LIVE_TESTS)
        self.assertFalse(report.finding)
        self.assertFalse(report.measured)

    def test_a_declared_but_unapplied_marker_is_a_plan_not_a_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_unit.py": "def test_x():\n    assert True\n"},
                workflows={"ci.yml": PR_ONLY_WORKFLOW},
            )
            (root / "pyproject.toml").write_text(
                textwrap.dedent(
                    """\
                    [tool.pytest.ini_options]
                    markers = [
                        "live: talks to the real API",
                    ]
                    """
                ),
                encoding="utf-8",
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.NO_LIVE_TESTS)
        self.assertEqual(report.marker_declared_in, ["pyproject.toml"])


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class VerdictTest(unittest.TestCase):
    """The three answers, over real checkouts."""

    def test_the_incident_shape_is_a_finding(self):
        """zh-education-mcp: live tests marked, excluded from CI, run nowhere."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"ci.yml": PR_ONLY_WORKFLOW},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.UNSCHEDULED)
        self.assertTrue(report.finding)
        self.assertIn("test_live.py", report.reason)

    def test_a_scheduled_visible_run_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"ci.yml": PR_ONLY_WORKFLOW, "live.yml": SCHEDULED_WORKFLOW},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.SCHEDULED)
        self.assertFalse(report.finding)
        self.assertEqual(report.notes, [])

    def test_on_is_read_though_yaml_makes_it_a_boolean(self):
        """YAML 1.1 turns a bare `on:` key into True. Missing that finds no cron
        in any real workflow and reports every repository as unscheduled."""
        loaded = yaml.safe_load(SCHEDULED_WORKFLOW)
        self.assertIn(True, loaded, "the premise of this test has changed")
        self.assertEqual(lsp._crons(lsp._triggers(loaded)), ["17 6 * * 1"])

    def test_a_scheduled_run_nobody_sees_is_its_own_finding(self):
        silent = SCHEDULED_WORKFLOW.split("      - name: Issue on failure")[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"live.yml": silent},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.SILENT)
        self.assertTrue(report.finding)

    def test_a_notifying_job_in_the_same_workflow_counts(self):
        split = SCHEDULED_WORKFLOW.split("      - name: Issue on failure")[0] + (
            "  notify:\n"
            "    needs: live\n"
            "    if: failure()\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo telegram\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"live.yml": split},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.SCHEDULED)

    def test_a_plain_scheduled_pytest_run_counts(self):
        plain = SCHEDULED_WORKFLOW.replace("pytest tests/ -m live -v", "pytest tests/")
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"live.yml": plain},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.SCHEDULED)

    def test_notes_never_decide_the_verdict(self):
        sparse = SCHEDULED_WORKFLOW.replace('"17 6 * * 1"', '"17 6 1 * *"').replace(
            "  workflow_dispatch: {}\n", ""
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"live.yml": sparse},
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.SCHEDULED)
        joined = " ".join(report.notes)
        self.assertIn(lsp.NOTE_SPARSE, joined)
        self.assertIn(lsp.NOTE_NO_DISPATCH, joined)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class NotAFindingTest(unittest.TestCase):
    """Everything the probe might simply not have SEEN. None of it is a finding."""

    def _probe_with_workflow(self, body: str) -> lsp.Report:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"live.yml": body},
            )
            return lsp.probe(root)

    def test_a_wrapper_command_is_unverified(self):
        report = self._probe_with_workflow(
            SCHEDULED_WORKFLOW.replace("pytest tests/ -m live -v", "make live-test")
        )
        self.assertEqual(report.status, lsp.UNVERIFIED)
        self.assertIn("make live-test", report.reason)

    def test_an_undecidable_marker_expression_is_unverified(self):
        wide = " or ".join(f"m{i}" for i in range(20))
        report = self._probe_with_workflow(
            SCHEDULED_WORKFLOW.replace(
                "pytest tests/ -m live -v", f'pytest tests/ -m "{wide}"'
            )
        )
        self.assertEqual(report.status, lsp.UNVERIFIED)

    def test_an_unparseable_workflow_is_unverified(self):
        report = self._probe_with_workflow("name: live\n  on: [\n")
        self.assertEqual(report.status, lsp.UNVERIFIED)
        self.assertTrue(report.unreadable)

    def test_no_workflows_directory_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp), tests={"test_live.py": LIVE_TEST}, workflows={}
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.UNVERIFIED)

    def test_a_documented_external_auditor_is_recorded_not_believed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"ci.yml": PR_ONLY_WORKFLOW},
            )
            (root / "README.md").write_text(
                "## Tests\n\nThe live suite is run weekly by mcp-continuous-auditor.\n",
                encoding="utf-8",
            )
            report = lsp.probe(root)
        self.assertEqual(report.status, lsp.EXTERNAL)
        self.assertFalse(report.finding)
        self.assertFalse(report.measured)
        self.assertIn("README.md:3", report.external_claim)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class ExitCodeTest(unittest.TestCase):
    """The contract coverage_run.py reads. It is the interface, so it is pinned."""

    def _run(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--target", str(root), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_finding_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"ci.yml": PR_ONLY_WORKFLOW},
            )
            proc = self._run(root)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], lsp.UNSCHEDULED)
        self.assertIs(payload["finding"], True)

    def test_clean_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_live.py": LIVE_TEST},
                workflows={"live.yml": SCHEDULED_WORKFLOW},
            )
            proc = self._run(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_not_measured_exits_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp),
                tests={"test_unit.py": "def test_x():\n    assert True\n"},
                workflows={"ci.yml": PR_ONLY_WORKFLOW},
            )
            proc = self._run(root)
        self.assertEqual(proc.returncode, 3, proc.stderr)

    def test_a_missing_target_is_a_harness_failure(self):
        proc = self._run(Path("/nonexistent/target"))
        self.assertEqual(proc.returncode, 127)


if __name__ == "__main__":
    unittest.main()
