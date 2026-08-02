#!/usr/bin/env python3
"""Tests for scripts/check_ruff_pin.py — the Ruff-pin guard.

Stdlib-only (`python3 -m unittest`), matching the rest of the auditor repo's
tooling.

The pre-commit hook promises to enforce locally exactly what the lint job
checks. That promise rests entirely on both naming the same Ruff version — and
the version sits in two places with nothing binding them together but a comment
asking whoever bumps one to bump the other.

Exercised here is the **pure** side: ``compare()`` receives both file contents
as strings. No filesystem, no mocks — a mock would only reflect our own
assumption about the file format and could never refute it.

Two properties matter more than the individual cases:

* a **missing** pin is a finding, not a silent pass — otherwise a config that
  stopped pinning Ruff at all would pass forever;
* the real repo files must agree with each other, or the guard is green while
  reality is red.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_ruff_pin as crp  # noqa: E402

WORKFLOW = """\
jobs:
  ruff:
    steps:
      - run: pip install ruff==0.15.8
      - run: ruff check .
"""

PRECOMMIT = """\
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.8
    hooks:
      - id: ruff-check
      - id: ruff-format
"""


class RuffPinTest(unittest.TestCase):
    def test_matching_pins_pass(self):
        ok, message = crp.compare(WORKFLOW, PRECOMMIT)
        self.assertTrue(ok, message)
        self.assertIn("0.15.8", message)

    def test_v_prefix_is_stripped_before_comparing(self):
        """`rev: v0.15.8` and `ruff==0.15.8` are the same version."""
        self.assertEqual(crp.precommit_pin(PRECOMMIT), "0.15.8")

    def test_diverging_pins_are_reported(self):
        for workflow_pin, hook_rev in (("0.15.22", "v0.15.8"), ("0.15.8", "v0.15.22")):
            with self.subTest(workflow=workflow_pin, hook=hook_rev):
                ok, message = crp.compare(
                    WORKFLOW.replace("0.15.8", workflow_pin),
                    PRECOMMIT.replace("v0.15.8", hook_rev),
                )
                self.assertFalse(ok)
                self.assertTrue(message.startswith("DRIFT:"), message)

    def test_missing_workflow_pin_is_a_finding(self):
        """Without a pin the comparison did not happen — no silent pass."""
        ok, message = crp.compare(WORKFLOW.replace("ruff==0.15.8", "ruff"), PRECOMMIT)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("NO PIN:"), message)

    def test_missing_hook_rev_is_a_finding(self):
        ok, message = crp.compare(WORKFLOW, PRECOMMIT.replace("    rev: v0.15.8\n", ""))
        self.assertFalse(ok)
        self.assertTrue(message.startswith("NO PIN:"), message)

    def test_missing_ruff_repo_entirely_is_a_finding(self):
        ok, message = crp.compare(WORKFLOW, "repos: []\n")
        self.assertFalse(ok)
        self.assertTrue(message.startswith("NO PIN:"), message)

    def test_rev_of_another_repo_is_not_mistaken_for_ruffs(self):
        """A second repo with its own `rev` must not skew the comparison."""
        with_other = (
            "repos:\n"
            "  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
            "    rev: v9.9.9\n"
            "    hooks:\n"
            "      - id: end-of-file-fixer\n"
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.15.8\n"
            "    hooks:\n"
            "      - id: ruff-check\n"
        )
        self.assertEqual(crp.precommit_pin(with_other), "0.15.8")
        ok, _ = crp.compare(WORKFLOW, with_other)
        self.assertTrue(ok)

    def test_several_workflow_pins_must_all_match(self):
        two = WORKFLOW + "      - run: pip install ruff==0.15.22\n"
        self.assertEqual(crp.workflow_pins(two), ["0.15.8", "0.15.22"])
        ok, message = crp.compare(two, PRECOMMIT)
        self.assertFalse(ok)
        self.assertIn("0.15.22", message)

    def test_the_real_repo_files_agree(self):
        """The guard checks nothing if it does not fit the real files."""
        workflow = REPO_ROOT / ".github" / "workflows" / "lint.yml"
        precommit = REPO_ROOT / ".pre-commit-config.yaml"
        self.assertTrue(workflow.is_file(), f"{workflow} missing")
        self.assertTrue(precommit.is_file(), f"{precommit} missing")

        ok, message = crp.compare(
            workflow.read_text(encoding="utf-8"),
            precommit.read_text(encoding="utf-8"),
        )
        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
