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
      - name: Open a PR
        uses: peter-evans/create-pull-request@v6
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
        "ci.yml.template::verify::actions/checkout@v4": {"status": "setup"},
        "ci.yml.template::verify::Open a PR": {
            "status": "target-only",
            "note": "acts on the target repo",
        },
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
    return dg.workflow_steps(TEMPLATE, "ci.yml.template")


def _own():
    return set(dg.workflow_steps(OWN, "lint.yml"))


class ExecutingStepsTest(unittest.TestCase):
    def test_every_step_is_collected_including_uses(self):
        """`uses:` steps carry shipped behaviour too — skipping them left a
        hole in the completeness claim (found in review of #55)."""
        self.assertEqual(
            _steps(),
            [
                "ci.yml.template::verify::actions/checkout@v4",
                "ci.yml.template::verify::Lint",
                "ci.yml.template::verify::Type-check",
                "ci.yml.template::verify::Open a PR",
            ],
        )

    def test_uses_step_identity_falls_back_to_the_action_spec(self):
        wf = "jobs:\n  j:\n    steps:\n      - uses: actions/checkout@v4\n"
        self.assertEqual(
            dg.workflow_steps(wf, "x.yml"), ["x.yml::j::actions/checkout@v4"]
        )

    def test_unnamed_step_still_yields_a_key(self):
        wf = "jobs:\n  j:\n    steps:\n      - run: echo hi\n"
        self.assertEqual(dg.workflow_steps(wf, "x.yml"), ["x.yml::j::(unnamed)"])


class CompareTest(unittest.TestCase):
    def test_fully_classified_passes_with_the_gap_warned(self):
        errors, warnings = dg.compare(_steps(), _own(), TABLE)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["ci.yml.template::verify::Type-check"])

    def test_unclassified_step_is_an_error(self):
        """Genau einen Eintrag entfernen — genau ein Befund."""
        table = {"steps": dict(TABLE["steps"])}
        del table["steps"]["ci.yml.template::verify::Type-check"]
        errors, _ = dg.compare(_steps(), _own(), table)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("UNCLASSIFIED:"), errors[0])
        self.assertIn("Type-check", errors[0])

    def test_empty_table_flags_every_step(self):
        errors, _ = dg.compare(_steps(), _own(), {})
        self.assertEqual(len(errors), 4)
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


class AmbiguityTest(unittest.TestCase):
    """Two steps sharing a name collapse to one key — then a single entry
    classifies both, and a second gate inherits the first one's mirror claim
    without ever being looked at. Found in review of #55."""

    DUPLICATE = """\
jobs:
  verify:
    steps:
      - name: Lint
        run: uv run ruff check
      - name: Lint
        run: uv run bandit -r src/
"""

    def test_duplicate_identities_are_an_error(self):
        steps = dg.workflow_steps(self.DUPLICATE, "ci.yml.template")
        self.assertEqual(len(steps), 2, "both steps must reach compare()")
        table = {
            "steps": {
                "ci.yml.template::verify::Lint": {
                    "status": "mirrored",
                    "by": "lint.yml::ruff::Lint",
                }
            }
        }
        errors, _ = dg.compare(steps, _own(), table)
        self.assertTrue(
            any(e.startswith("AMBIGUOUS:") for e in errors),
            f"a second step named Lint passed unseen: {errors}",
        )

    def test_several_unnamed_steps_in_one_job_are_ambiguous(self):
        wf = "jobs:\n  j:\n    steps:\n      - run: a\n      - run: b\n"
        errors, _ = dg.compare(dg.workflow_steps(wf, "x.yml"), set(), {})
        self.assertTrue(any(e.startswith("AMBIGUOUS:") for e in errors), errors)


class RealRepoTest(unittest.TestCase):
    def test_the_real_repo_passes(self):
        """A guard that does not fit the real files checks nothing."""
        template_steps, own_steps, table = dg.collect(REPO_ROOT)
        self.assertTrue(template_steps, "no shipped templates found")
        errors, _ = dg.compare(template_steps, own_steps, table)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_the_original_gap_is_recorded_as_mirrored(self):
        """`ruff check` is why this guard exists — it must not silently drift
        back to unclassified or target-only.

        The `by` value is asserted to RESOLVE, not to equal a fixed string.
        It named `lint.yml::ruff::Lint` until the gates moved into
        `scripts/checks/` and became one step; pinning the name made this test
        fail on a rename that changed nothing about the guarantee. Resolving
        is also the stronger claim: a typo in `by` would have satisfied the
        old assertion only by accident, and satisfies this one never.
        """
        table = yaml.safe_load(
            (REPO_ROOT / ".github" / "dogfood.yml").read_text(encoding="utf-8")
        )
        entry = table["steps"]["ci.yml.template::verify::Lint"]
        self.assertEqual(entry["status"], "mirrored")

        _, own_steps, _ = dg.collect(REPO_ROOT)
        self.assertIn(
            entry["by"],
            set(own_steps),
            f"dogfood.yml says this gate is mirrored by {entry['by']!r}, but no "
            "own workflow has a step by that name.",
        )


if __name__ == "__main__":
    unittest.main()
