#!/usr/bin/env python3
"""Tests for scripts/parity_probe.py — do the two language versions still match?

The probe compares two Markdown files, so these tests write two Markdown files.
The one seam is git, and it is exercised for real in a temp repository rather
than mocked: "how far has the base moved since the translation was last
touched" is a question about git's actual behaviour, and a stub would test the
arithmetic while assuming the part that has to work.

The correct-translation cases matter as much as the drift ones. The two files
are *supposed* to differ in every word; a probe that cannot tell a translation
from a regression is worse than no probe, because it produces a red run that
everybody learns to ignore.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import parity_probe as pp  # noqa: E402

HAS_GIT = shutil.which("git") is not None

EN = """\
# Title

Intro paragraph with a [link](https://example.invalid/a).

[🇩🇪 Deutsche Version](README.de.md)

## Overview

- one
- two

## Usage

```bash
python scripts/gate.py --target .   # run the gate
```
"""

DE = """\
# Titel

Einleitender Absatz mit einem [Link](https://example.invalid/a).

[🇬🇧 English version](README.md)

## Übersicht

- eins
- zwei

## Verwendung

```bash
python scripts/gate.py --target .   # Gate ausführen
```
"""


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, en: str = EN, de: str = DE) -> None:
        (self.root / "README.md").write_text(en, encoding="utf-8")
        (self.root / "README.de.md").write_text(de, encoding="utf-8")

    def probe(self) -> pp.Report:
        return pp.run(self.root)

    def codes(self, report: pp.Report) -> list[str]:
        return [f.code for f in report.findings]


class TranslationIsNotDriftTest(Case):
    """What a correct translation is allowed to change."""

    def test_a_faithful_translation_is_clean(self) -> None:
        self.write()
        report = self.probe()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), pp.EXIT_GREEN)

    def test_translated_headings_are_not_drift(self) -> None:
        """`Overview` and `Übersicht` are a correct translation.

        The skeleton is the sequence of heading LEVELS, never their text —
        comparing the words would report every properly translated file.
        """
        self.write(de=DE.replace("## Übersicht", "## Ganz anderer Titel"))
        self.assertEqual(self.probe().findings, [])

    def test_a_translated_comment_inside_a_command_is_not_drift(self) -> None:
        """`# run the gate` vs `# Gate ausführen` — prose in a monospace font."""
        self.write()
        self.assertEqual(self.codes(self.probe()), [])
        # And the stripping is quote-aware: a `#` inside an argument is part of
        # the command, not a comment, and truncating it would invent a
        # difference between two identical examples.
        self.assertEqual(
            pp._strip_trailing_comment("""gate --args '{"q": "#tag"}'  # los""").strip(),
            """gate --args '{"q": "#tag"}'""")

    def test_an_untagged_fence_is_not_compared(self) -> None:
        """A directory tree in a fence is prose, and prose gets translated.

        Only fences that declare a language are compared, because those are the
        ones carrying commands a reader will paste.
        """
        tree_en = EN + "\n## Layout\n\n```\nscripts/   the probes\n```\n"
        tree_de = DE + "\n## Aufbau\n\n```\nscripts/   die Probes\n```\n"
        self.write(tree_en, tree_de)
        self.assertEqual(self.probe().findings, [])

    def test_the_cross_language_link_is_not_link_drift(self) -> None:
        """Each file links to the other. That is the one link that must differ."""
        self.write()
        self.assertNotIn("LINK_DRIFT", self.codes(self.probe()))


class DriftTest(Case):
    def test_a_section_missing_from_the_translation(self) -> None:
        self.write(en=EN + "\n## Roadmap\n\n- later\n")
        report = self.probe()
        self.assertIn("SECTION_COUNT_DRIFT", self.codes(report))
        mismatch = next(f for f in report.findings if f.code == "SECTION_MISMATCH")
        # Both sides named: "position 4 differs" is not something anybody can act on.
        self.assertIn("Roadmap", mismatch.detail)
        self.assertIn("nothing (the document ends)", mismatch.detail)

    def test_only_the_first_divergence_is_reported(self) -> None:
        """After one missing section every later position is shifted.

        Printing the consequential mismatches too would bury the one that has
        to be fixed under a diff of the whole document.
        """
        self.write(de=DE.replace("## Übersicht\n\n- eins\n- zwei\n", ""))
        report = self.probe()
        self.assertEqual(sum(1 for c in self.codes(report) if c == "SECTION_MISMATCH"), 1)

    def test_a_bullet_added_on_one_side_only(self) -> None:
        """The shape the observed drift took: a feature listed once."""
        self.write(en=EN.replace("- one\n- two\n", "- one\n- two\n- three\n"))
        report = self.probe()
        drift = next(f for f in report.findings if f.code == "BULLET_DRIFT")
        self.assertIn("3 top-level item(s)", drift.detail)
        self.assertIn("has 2", drift.detail)

    def test_a_command_that_differs_is_a_high_finding(self) -> None:
        """A German README installing a renamed script is invisible to its reader."""
        self.write(de=DE.replace("scripts/gate.py", "scripts/old_gate.py"))
        report = self.probe()
        finding = next(f for f in report.findings if f.code == "CODE_BLOCK_CONTENT_DRIFT")
        self.assertEqual(finding.severity, "high")
        self.assertIn("old_gate.py", finding.detail)

    def test_a_link_present_on_one_side_only(self) -> None:
        self.write(en=EN.replace("(https://example.invalid/a)",
                                 "(https://example.invalid/b)"))
        details = [f.detail for f in self.probe().findings if f.code == "LINK_DRIFT"]
        self.assertEqual(len(details), 2)  # one missing on each side

    def test_a_code_block_count_difference(self) -> None:
        self.write(en=EN + "\n```bash\necho extra\n```\n")
        self.assertIn("CODE_BLOCK_DRIFT", self.codes(self.probe()))


@unittest.skipUnless(HAS_GIT, "git is not installed")
class LagTest(Case):
    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.root),
             "-c", "user.email=p@audit.invalid", "-c", "user.name=p", *args],
            check=True, capture_output=True, text=True)

    def setUp(self) -> None:
        super().setUp()
        self.write()
        self.git("init", "--quiet")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "docs: both languages")

    def test_updating_both_files_in_one_commit_leaves_no_lag(self) -> None:
        """Good practice produces zero on its own; the check fires on the rest."""
        self.write(en=EN + "\n## Roadmap\n\n- later\n", de=DE + "\n## Fahrplan\n\n- später\n")
        self.git("commit", "--quiet", "-am", "docs: roadmap in both")
        report = self.probe()
        self.assertEqual(self.codes(report), [])
        self.assertTrue(report.pairs[0].lag_measured)

    def test_a_base_only_commit_is_lag(self) -> None:
        """The check that fires while every structural check is green.

        A paragraph rewritten on one side keeps the skeleton, the bullets, the
        blocks and the links identical — and says something the other no longer
        does.
        """
        (self.root / "README.md").write_text(
            EN.replace("Intro paragraph", "Rewritten intro paragraph"), encoding="utf-8")
        self.git("commit", "--quiet", "-am", "docs: rewrite the intro")
        report = self.probe()
        lag = next(f for f in report.findings if f.code == "TRANSLATION_LAG")
        self.assertIn("rewrite the intro", lag.detail)
        # And nothing structural fired: this is the case the other checks miss.
        self.assertEqual(self.codes(report), ["TRANSLATION_LAG"])

    def test_without_git_the_freshness_question_is_left_open(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            root = Path(plain)
            (root / "README.md").write_text(EN, encoding="utf-8")
            (root / "README.de.md").write_text(DE, encoding="utf-8")
            report = pp.run(root)
            self.assertFalse(report.pairs[0].lag_measured)
            self.assertIn("freshness was not measured", " ".join(report.notes))


class NotMeasuredTest(Case):
    def test_a_monolingual_repository_is_not_a_pass(self) -> None:
        (self.root / "README.md").write_text(EN, encoding="utf-8")
        report = self.probe()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), pp.EXIT_NOT_MEASURED)
        self.assertIn("nothing was measured", " ".join(report.notes))

    def test_discovery_pairs_every_translated_root_document(self) -> None:
        self.write()
        (self.root / "SECURITY.md").write_text("# S\n", encoding="utf-8")
        (self.root / "SECURITY.de.md").write_text("# S\n", encoding="utf-8")
        self.assertEqual(
            [(b.name, t.name) for b, t in pp.discover(self.root, "de")],
            [("README.md", "README.de.md"), ("SECURITY.md", "SECURITY.de.md")])

    def test_a_translation_without_a_base_is_not_a_pair(self) -> None:
        (self.root / "NOTES.de.md").write_text("# N\n", encoding="utf-8")
        self.assertEqual(pp.discover(self.root, "de"), [])


class OwnDocumentationTest(unittest.TestCase):
    """This repository's own README pair, held to the check it ships.

    A tool that holds others to a discipline and exempts itself is the more
    embarrassing of the two failures — the same reason `tests.yml` exists at
    all. The German side of this very README had drifted before the probe was
    written.
    """

    def test_the_readme_pair_is_structurally_parallel(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = pp.run(root)
        self.assertTrue(report.pairs, "README.de.md disappeared?")
        self.assertEqual(
            [f"{f.code}: {f.detail}" for f in report.findings], [],
            "the English and German README have come apart — "
            "run `python scripts/parity_probe.py --target .`")


if __name__ == "__main__":
    unittest.main()
