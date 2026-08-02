#!/usr/bin/env python3
"""Tests for scripts/probe_provenance.py — does a report name the state it read?

The module is two git reads with a comparison between them, so these tests
build real repositories in a temp dir and move them underneath a captured
provenance. Nothing is mocked: the value of this check is entirely in whether
it notices a real ``git commit`` landing mid-run, and a fake ``_git`` would
test the comparison while assuming away the part that has to work.

Stdlib-only. Skips when git is not on PATH — an environment-driven skip, the
kind ``tests.yml`` deliberately still tolerates.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_provenance as pv  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root),
         "-c", "user.email=probe@audit.invalid", "-c", "user.name=probe",
         *args],
        check=True, capture_output=True, text=True)


def new_repo(root: Path) -> None:
    git(root, "init", "--quiet")
    (root / "server.py").write_text("VERSION = '0.4.0'\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "initial")


@unittest.skipUnless(HAS_GIT, "git is not installed")
class CaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        new_repo(self.root)

    def test_clean_checkout_is_pinned_and_names_the_commit(self) -> None:
        prov = pv.capture(self.root).recheck()
        self.assertEqual(prov.status, pv.PINNED)
        self.assertFalse(prov.moved)
        self.assertFalse(prov.blocking)
        self.assertEqual(len(prov.head or ""), 40)
        self.assertIn(prov.short, prov.render())

    def test_a_commit_during_the_run_is_a_move(self) -> None:
        prov = pv.capture(self.root)
        (self.root / "server.py").write_text("VERSION = '0.5.0'\n", encoding="utf-8")
        git(self.root, "commit", "--quiet", "-am", "bump")
        prov.recheck()
        self.assertEqual(prov.status, pv.MOVED_DURING_RUN)
        self.assertTrue(prov.blocking)
        self.assertNotEqual(prov.head, prov.head_after)
        # The whole point of the module: the report says which two states it
        # saw, not merely that something happened.
        self.assertIn(prov.short, prov.moved_detail())
        self.assertIn("HEAD moved", " ".join(prov.moves))

    def test_an_uncommitted_edit_during_the_run_is_also_a_move(self) -> None:
        """HEAD alone would call this pinned — and it is the commoner case.

        A rebase is rare; an editor saving a file, a `git stash pop` or a
        half-applied patch while a minutes-long probe reads the tree is not.
        """
        prov = pv.capture(self.root)
        (self.root / "server.py").write_text("VERSION = '9.9.9'\n", encoding="utf-8")
        prov.recheck()
        self.assertEqual(prov.head, prov.head_after)
        self.assertEqual(prov.status, pv.MOVED_DURING_RUN)
        self.assertIn("working tree changed", " ".join(prov.moves))

    def test_a_dirty_tree_that_does_not_change_is_pinned_dirty(self) -> None:
        """Dirty is not moved — but it is not plain PINNED either.

        `PINNED` promises that checking out that commit reproduces what was
        read. With uncommitted changes in the tree it does not, and a status
        that claimed otherwise would be a promise the reader cannot cash.
        """
        (self.root / "server.py").write_text("VERSION = '0.4.1'\n", encoding="utf-8")
        prov = pv.capture(self.root).recheck()
        self.assertEqual(prov.status, pv.PINNED_DIRTY)
        self.assertFalse(prov.moved)
        self.assertTrue(prov.dirty)
        self.assertIn("uncommitted", prov.render())

    def test_probe_byproducts_are_not_a_move(self) -> None:
        """The probe's own footprints must not trip the probe.

        A boot probe starts the target in its checkout and leaves a
        `__pycache__` behind; an install leaves an egg-info. If those counted,
        every full-depth run would report MOVED_DURING_RUN about itself, and
        the check would be switched off within a week.
        """
        prov = pv.capture(self.root)
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "server.cpython-311.pyc").write_bytes(b"\x00")
        (self.root / "pkg.egg-info").mkdir()
        (self.root / "pkg.egg-info" / "PKG-INFO").write_text("x", encoding="utf-8")
        prov.recheck()
        self.assertEqual(prov.status, pv.PINNED, prov.moves)

    def test_a_real_untracked_file_is_a_move(self) -> None:
        """The exclusion above is a named list, not "ignore untracked"."""
        prov = pv.capture(self.root)
        (self.root / "extra_tool.py").write_text("x = 1\n", encoding="utf-8")
        prov.recheck()
        self.assertEqual(prov.status, pv.MOVED_DURING_RUN)

    def test_index_probes_report_the_move_without_withdrawing_the_verdict(self) -> None:
        """`decisive=False` — the yank/published case.

        Those probes read a package index; the checkout only tells them which
        distribution to ask about. Suppressing a catalogue finding because
        somebody committed locally would be superstition, not rigour — but the
        move is still recorded, and the rendering still says so.
        """
        prov = pv.capture(self.root, decisive=False)
        git(self.root, "commit", "--quiet", "--allow-empty", "-m", "unrelated")
        prov.recheck()
        self.assertTrue(prov.moved)
        self.assertFalse(prov.blocking)
        self.assertIn("verdict is read from the index", prov.render())

    def test_as_dict_carries_both_measurements(self) -> None:
        prov = pv.capture(self.root).recheck()
        data = prov.as_dict()
        self.assertEqual(data["head"], data["head_after"])
        for key in ("status", "head", "head_after", "started", "finished",
                    "worktree_digest", "decisive", "blocking"):
            self.assertIn(key, data)


class UnpinnedTest(unittest.TestCase):
    """No git checkout: say so, do not pretend and do not fail."""

    def test_plain_directory_is_unpinned_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # An unpacked sdist is a legitimate target and has no history.
            prov = pv.capture(Path(tmp)).recheck()
            self.assertEqual(prov.status, pv.UNPINNED)
            self.assertFalse(prov.blocking)
            self.assertTrue(prov.unavailable)
            self.assertIn("UNPINNED", prov.render())

    def test_missing_directory_is_unpinned_not_an_exception(self) -> None:
        prov = pv.capture(Path("/nonexistent-target-for-a-test")).recheck()
        self.assertEqual(prov.status, pv.UNPINNED)
        self.assertIn("not a directory", prov.unavailable)

    def test_status_before_recheck_is_open(self) -> None:
        """A captured-but-not-rechecked run has made no claim yet."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = pv.Provenance(target=tmp, head="a" * 40)
            self.assertEqual(prov.status, pv.OPEN)
            self.assertFalse(prov.moved)


if __name__ == "__main__":
    unittest.main()
