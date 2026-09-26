#!/usr/bin/env python3
"""Tests for scripts/check_ruff_pin.py — the Ruff-pin guard.

Stdlib-only (`python3 -m unittest`), matching the rest of the auditor repo's
tooling.

The pin lives in ONE file, `requirements-lint.txt`. The pre-commit hook must
name the same version, and the workflows must read from that file instead of
carrying a number of their own. This guard is what makes "one source" more than
a comment.

Exercised here is the **pure** side: ``compare()`` receives the file contents
as strings. No filesystem, no mocks — a mock would only reflect our own
assumption about the file format and could never refute it.

Three properties matter more than the individual cases:

* a **missing** pin is a finding, not a silent pass — otherwise a config that
  stopped pinning Ruff at all would pass forever;
* a workflow that pins by itself is a finding **even when its number matches
  today** — the second copy is the defect, not the difference;
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

REQUIREMENTS = """\
# the single source
ruff==0.15.8
"""

WORKFLOW = """\
jobs:
  ruff:
    steps:
      - run: pip install -r requirements-lint.txt pyyaml
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


def workflows(text: str = WORKFLOW) -> dict[str, str]:
    return {"lint.yml": text}


class RuffPinTest(unittest.TestCase):
    def test_matching_pins_pass(self):
        ok, message = crp.compare(REQUIREMENTS, PRECOMMIT, workflows())
        self.assertTrue(ok, message)
        self.assertIn("0.15.8", message)

    def test_v_prefix_is_stripped_before_comparing(self):
        """`rev: v0.15.8` and `ruff==0.15.8` are the same version."""
        self.assertEqual(crp.precommit_pin(PRECOMMIT), "0.15.8")

    def test_diverging_pins_are_reported(self):
        for source_pin, hook_rev in (("0.15.22", "v0.15.8"), ("0.15.8", "v0.15.22")):
            with self.subTest(source=source_pin, hook=hook_rev):
                ok, message = crp.compare(
                    REQUIREMENTS.replace("0.15.8", source_pin),
                    PRECOMMIT.replace("v0.15.8", hook_rev),
                    workflows(),
                )
                self.assertFalse(ok)
                self.assertTrue(message.startswith("DRIFT:"), message)

    def test_missing_source_pin_is_a_finding(self):
        """Without a pin the comparison did not happen — no silent pass."""
        unpinned = REQUIREMENTS.replace("ruff==0.15.8", "ruff")
        ok, message = crp.compare(unpinned, PRECOMMIT, workflows())
        self.assertFalse(ok)
        self.assertTrue(message.startswith("NO PIN:"), message)

    def test_a_pin_only_in_a_comment_is_no_pin(self):
        commented = "# ruff==0.15.8\nruff\n"
        self.assertIsNone(crp.requirements_pin(commented))
        ok, message = crp.compare(commented, PRECOMMIT, workflows())
        self.assertFalse(ok)
        self.assertIn("NO PIN:", message)

    def test_two_versions_in_the_source_are_a_contradiction(self):
        two = REQUIREMENTS + "ruff==0.15.22\n"
        ok, message = crp.compare(two, PRECOMMIT, workflows())
        self.assertFalse(ok)
        self.assertIn("more than once", message)

    def test_missing_hook_rev_is_a_finding(self):
        no_rev = PRECOMMIT.replace("    rev: v0.15.8\n", "")
        ok, message = crp.compare(REQUIREMENTS, no_rev, workflows())
        self.assertFalse(ok)
        self.assertTrue(message.startswith("NO PIN:"), message)

    def test_missing_ruff_repo_entirely_is_a_finding(self):
        ok, message = crp.compare(REQUIREMENTS, "repos: []\n", workflows())
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
        ok, _ = crp.compare(REQUIREMENTS, with_other, workflows())
        self.assertTrue(ok)


class SingleSourceTest(unittest.TestCase):
    """The workflows read the source; they never carry a number themselves."""

    def test_a_workflow_pin_is_a_second_source_even_when_it_matches(self):
        """The copy is the defect, not the difference — it drifts later."""
        pinned = WORKFLOW.replace(
            "-r requirements-lint.txt pyyaml", "-r requirements-lint.txt ruff==0.15.8"
        )
        ok, message = crp.compare(REQUIREMENTS, PRECOMMIT, workflows(pinned))
        self.assertFalse(ok)
        self.assertIn("SECOND SOURCE: lint.yml", message)

    def test_a_workflow_not_installing_from_the_source_is_not_wired(self):
        unwired = WORKFLOW.replace("-r requirements-lint.txt", "ruff")
        ok, message = crp.compare(REQUIREMENTS, PRECOMMIT, workflows(unwired))
        self.assertFalse(ok)
        self.assertIn("NOT WIRED: lint.yml", message)

    def test_the_long_option_counts_as_wired(self):
        long_form = WORKFLOW.replace("-r ", "--requirement ")
        ok, message = crp.compare(REQUIREMENTS, PRECOMMIT, workflows(long_form))
        self.assertTrue(ok, message)

    def test_every_finding_is_named_not_just_the_first(self):
        """One run names all of them; otherwise every fix costs a round."""
        broken = {
            "lint.yml": WORKFLOW.replace("-r requirements-lint.txt", "ruff==0.15.8"),
            "tests.yml": "run: pip install pyyaml\n",
        }
        hook = PRECOMMIT.replace("v0.15.8", "v0.15.22")
        ok, message = crp.compare(REQUIREMENTS, hook, broken)
        self.assertFalse(ok)
        for expected in (
            "DRIFT:",
            "SECOND SOURCE: lint.yml",
            "NOT WIRED: lint.yml",
            "NOT WIRED: tests.yml",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, message)


class RealRepoTest(unittest.TestCase):
    def test_the_real_repo_files_agree(self):
        """The guard checks nothing if it does not fit the real files."""
        paths = [crp.REQUIREMENTS, crp.PRECOMMIT_CONFIG, *crp.PINNED_WORKFLOWS]
        for rel in paths:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"{rel} missing")

        read = lambda rel: (REPO_ROOT / rel).read_text(encoding="utf-8")  # noqa: E731
        ok, message = crp.compare(
            read(crp.REQUIREMENTS),
            read(crp.PRECOMMIT_CONFIG),
            {rel.as_posix(): read(rel) for rel in crp.PINNED_WORKFLOWS},
        )
        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
