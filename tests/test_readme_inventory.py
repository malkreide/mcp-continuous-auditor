#!/usr/bin/env python3
"""The README's inventory against what is actually on disk.

Both checks here exist because the drift they catch already happened. Merging
`release_gap.py` into `shipped_probe.py` renamed a skill and deleted a script,
and the README kept advertising the old names for two more pull requests —
`skills/` still listed three skills when there were six, and the file the
Project Structure block pointed at no longer existed.

That is the worst kind of stale documentation: it is not vague, it is *specific
and wrong*, and it sends a reader to a path that is not there.

WHAT IS DELIBERATELY NOT PINNED HERE
------------------------------------
The test COUNT in the same block. A test asserting "the README says 499" would
be correct and would fail on every pull request that adds a test — friction on
the common path to catch a number nobody navigates by. The skill list and the
script paths are different: they drift rarely, and when they drift they send
someone somewhere that does not exist.

Stdlib-only, no network, no git.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


class SkillInventoryTest(unittest.TestCase):
    """The `skills/` line in Project Structure, against `skills/` itself."""

    def _listed(self) -> set[str]:
        text = README.read_text(encoding="utf-8")
        block = re.search(r"^skills/\s+(.*?)(?=^\S)", text, re.M | re.S)
        self.assertIsNotNone(block, "no `skills/` entry in the Project Structure block")
        # Parentheticals are prose, not inventory ("(x absorbed the former y)").
        body = re.sub(r"\([^)]*\)", "", block.group(1))
        return {name.strip() for name in body.replace("\n", " ").split(",") if name.strip()}

    def _on_disk(self) -> set[str]:
        return {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()}

    def test_every_skill_on_disk_is_listed(self):
        missing = self._on_disk() - self._listed()
        self.assertEqual(missing, set(), f"skills exist but the README omits them: {missing}")

    def test_every_listed_skill_exists(self):
        """The direction that sends a reader to a path that is not there."""
        phantom = self._listed() - self._on_disk()
        self.assertEqual(phantom, set(), f"README lists skills that are gone: {phantom}")


class ScriptPathTest(unittest.TestCase):
    """Every `scripts/*.py` the README names must exist.

    Deleting a script and leaving the README pointing at it is exactly what the
    merge did. This is the cheap guard against repeating it.
    """

    def test_named_scripts_exist(self):
        text = README.read_text(encoding="utf-8")
        named = set(re.findall(r"(scripts/[a-z_0-9]+\.py)", text))
        self.assertTrue(named, "the README names no scripts — did the format change?")
        missing = sorted(n for n in named if not (ROOT / n).exists())
        self.assertEqual(missing, [], f"README points at scripts that do not exist: {missing}")


if __name__ == "__main__":
    unittest.main()
