#!/usr/bin/env python3
"""Tests for scripts/dogfood_gate.py — the auditor answering for itself.

Stdlib-only plus PyYAML (which the suite already requires), matching the rest
of the repo's tooling.

Exercised here is the **pure** side: `compare()` receives the three inputs as
data. No filesystem, no mocks — a mock would only reflect our own assumption
about the workflow format and could never refute it.

Four properties matter more than the individual cases, and each of them is a
way the guard could quietly stop guarding:

* an **unclassified** template step is an error, not a silent pass — that is
  the exact shape of the gap this guard exists for (`ruff check` shipped to
  every target, never run here);
* a **stale** entry is an error — otherwise the table accumulates claims about
  steps that no longer exist and slowly stops describing reality;
* a **broken mirror** is an error — the claim "we run this too" must fail the
  day the own step is deleted, not stay standing as prose;
* the **real repo files** must pass, or the guard fits nothing it is pointed at.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dogfood_gate as dg  # noqa: E402

TEMPLATE = """\
jobs:
  verify:
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: uv run ruff check
      - name: Type-check
        run: uv run mypy .
"""

OWN = """\
jobs:
  ruff:
    steps:
      - name: Lint
        run: ruff check .
"""

TABLE = {
    "steps": {
        "ci.yml.template::verify::Lint": {
            "status": "mirrored",
            "by": "lint.yml::ruff::Lint",
        },
        "ci.yml.template::verify::Type-check": {
            "status": "gap",
            "note": "applicable, not run yet",
        },
    }
}


def _steps():
    return dg.executing_steps(TEMPLATE, "ci.yml.template")


def _own():
    return set(dg.executing_steps(OWN, "lint.yml"))


class ExecutingStepsTest(unittest.TestCase):
    def test_only_steps_that_run_are_collected(self):
        """`uses:`-only steps impose nothing and must not need a classification."""
        self.assertEqual(
            _steps(),
            ["ci.yml.template::verify::Lint", "ci.yml.template::verify::Type-check"],
        )

    def test_unnamed_step_still_yields_a_key(self):
        wf = "jobs:\n  j:\n    steps:\n      - run: echo hi\n"
        self.assertEqual(dg.executing_steps(wf, "x.yml"), ["x.yml::j::(unnamed)"])


class CompareTest(unittest.TestCase):
    def test_fully_classified_passes_with_the_gap_warned(self):
        errors, warnings = dg.compare(_steps(), _own(), TABLE)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["ci.yml.template::verify::Type-check"])

    def test_unclassified_step_is_an_error(self):
        table = {"steps": {k: v for k, v in TABLE["steps"].items() if "Lint" in k}}
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("UNCLASSIFIED:"), errors[0])

    def test_empty_table_flags_every_step(self):
        errors, _ = dg.compare(_steps(), _own(), {})
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e.startswith("UNCLASSIFIED:") for e in errors), errors)

    def test_stale_entry_is_an_error(self):
        table = {"steps": dict(TABLE["steps"])}
        table["steps"]["ci.yml.template::verify::Gone"] = {"status": "setup"}
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("STALE:"), errors[0])

    def test_mirror_pointing_at_a_missing_own_step_is_an_error(self):
        """The claim must fail the day the own step is deleted."""
        errors, _ = dg.compare(_steps(), set(), TABLE)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("BROKEN MIRROR:"), errors[0])

    def test_mirror_without_by_is_an_error(self):
        table = {"steps": dict(TABLE["steps"])}
        table["steps"]["ci.yml.template::verify::Lint"] = {"status": "mirrored"}
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertTrue(errors[0].startswith("BROKEN MIRROR:"), errors[0])

    def test_target_only_without_note_is_an_error(self):
        table = {"steps": dict(TABLE["steps"])}
        table["steps"]["ci.yml.template::verify::Type-check"] = {
            "status": "target-only"
        }
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertTrue(errors[0].startswith("UNJUSTIFIED:"), errors[0])

    def test_gap_without_note_is_an_error(self):
        table = {"steps": dict(TABLE["steps"])}
        table["steps"]["ci.yml.template::verify::Type-check"] = {"status": "gap"}
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertTrue(errors[0].startswith("UNJUSTIFIED:"), errors[0])

    def test_unknown_status_is_an_error(self):
        table = {"steps": dict(TABLE["steps"])}
        table["steps"]["ci.yml.template::verify::Type-check"] = {"status": "maybe"}
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertTrue(errors[0].startswith("BAD STATUS:"), errors[0])

    def test_setup_needs_no_note(self):
        table = {"steps": dict(TABLE["steps"])}
        table["steps"]["ci.yml.template::verify::Type-check"] = {"status": "setup"}
        errors, warnings = dg.compare(_steps(), _own(), table)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])


class RealRepoTest(unittest.TestCase):
    def test_the_real_repo_passes(self):
        """A guard that does not fit the real files checks nothing."""
        template_steps, own_steps, table = dg.collect(REPO_ROOT)
        self.assertTrue(template_steps, "no shipped templates found")
        errors, _ = dg.compare(template_steps, own_steps, table)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_the_original_gap_is_recorded_as_mirrored(self):
        """`ruff check` is why this guard exists — it must not silently drift
        back to unclassified or target-only."""
        table = yaml.safe_load(
            (REPO_ROOT / ".github" / "dogfood.yml").read_text(encoding="utf-8")
        )
        entry = table["steps"]["ci.yml.template::verify::Lint"]
        self.assertEqual(entry["status"], "mirrored")
        self.assertEqual(entry["by"], "lint.yml::ruff::Lint")


if __name__ == "__main__":
    unittest.main()
