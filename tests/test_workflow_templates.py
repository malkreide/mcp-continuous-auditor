"""Tests for the workflow-template guard.

This check used to be a heredoc inside `tests.yml`. A heredoc can only be
exercised by putting the whole repository into the state it is meant to
complain about, so it was never established that it bites at all — the very
failure it exists to prevent.

The pure side is what gets tested: `compare()` takes a mapping of name to
text. No filesystem, no mocks.

The **anchor** case weighs most: an empty glob must be a finding. Exiting 0
because nothing was found would turn a rename into a silent all-clear.

Stdlib `unittest` only, like the rest of this suite — the runner is
`unittest.defaultTestLoader.discover("tests")` and pytest is not installed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_workflow_templates as cwt  # noqa: E402

GOOD = "name: ci\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
BROKEN = "name: ci\non:\n  push:\n   bad: [unclosed\n"


class WorkflowTemplateTest(unittest.TestCase):
    def test_well_formed_templates_pass(self):
        ok, message = cwt.compare({"ci.yml.template": GOOD, "b.yml.template": GOOD})
        self.assertTrue(ok, message)
        self.assertIn("2 template(s) parse", message)

    def test_a_broken_template_is_named(self):
        ok, message = cwt.compare({"ci.yml.template": GOOD, "bad.yml.template": BROKEN})
        self.assertFalse(ok)
        self.assertIn("bad.yml.template", message)
        # Der gute darf nicht mitbeschuldigt werden — sonst sucht jemand im
        # falschen File.
        self.assertNotIn("ci.yml.template", message)

    def test_every_broken_template_is_reported_not_just_the_first(self):
        """Ein Lauf soll alle nennen; sonst kostet jeder Fehler eine Runde."""
        ok, message = cwt.compare({"a.yml.template": BROKEN, "b.yml.template": BROKEN})
        self.assertFalse(ok)
        self.assertIn("a.yml.template", message)
        self.assertIn("b.yml.template", message)


class AnchorTest(unittest.TestCase):
    def test_no_templates_is_a_finding_not_a_pass(self):
        ok, message = cwt.compare({})
        self.assertFalse(ok)
        self.assertIn("did they move", message)

    def test_the_real_templates_are_found_and_parse(self):
        """The guard must not be green because the glob found nothing."""
        templates = cwt.read_templates(REPO_ROOT)
        self.assertTrue(
            templates,
            f"no {cwt.TEMPLATE_GLOB} found in this repository — then this guard "
            "checks nothing when it matters.",
        )
        ok, message = cwt.compare(templates)
        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
