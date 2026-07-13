#!/usr/bin/env python3
"""Tests for scripts/improve_acceptance.py — the Phase-6a acceptance harness.

Stdlib-only (`python3 -m unittest`), matching test_budget_guard.py: each test
drives the module through its CLI entrypoint. The target checkout is a
throwaway git repo in a tmp dir, and the suite runner is a tiny fake that maps
each `test-<name> PASS|FAIL|FLAKY` line of the fake determ config to a
promptfoo-style result — so keep/flaky/false-positive candidates can be
constructed exactly, without promptfoo or a network. FLAKY flips its outcome
on every invocation (a deterministic simulation of a random-dependent assert:
two consecutive runs always disagree). Needs `git`, like the harness itself.
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

import improve_acceptance as ia  # noqa: E402

CONFIG_REL = "promptfoo/promptfooconfig.determ.yaml"
BASE_CONFIG = "test-base PASS\n"

# Fake suite runner: argv = [config, output.json]. Every `test-<name> MODE`
# line becomes one result; FLAKY passes on even invocations and fails on odd
# ones (a call counter persists next to the runner script).
RUNNER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys
    from pathlib import Path

    config, out = Path(sys.argv[1]), Path(sys.argv[2])
    counter = Path(__file__).with_suffix(".calls")
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))

    results = []
    for line in config.read_text().splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].startswith("test-"):
            continue
        name, mode = parts
        ok = {"PASS": True, "FAIL": False, "FLAKY": n % 2 == 0}[mode]
        results.append({"description": name, "success": ok})
    out.write_text(json.dumps({"results": {"results": results}}))
    sys.exit(0 if all(r["success"] for r in results) else 1)
    """
)


class ImproveAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.target = root / "target"
        self.journal = root / ".audit" / "experiments.jsonl"
        (self.target / "promptfoo").mkdir(parents=True)
        (self.target / CONFIG_REL).write_text(BASE_CONFIG)
        (self.target / "README.md").write_text("target\n")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "test")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        runner = root / "fake_runner.py"
        runner.write_text(RUNNER)
        self.runner = f"{sys.executable} {runner}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def _git(self, *argv: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.target), *argv], check=True, capture_output=True
        )

    def make_candidate(self, name: str, rel_path: str, new_content: str) -> Path:
        """Build a unified diff by editing `rel_path`, diffing, and reverting."""
        target_file = self.target / rel_path
        target_file.write_text(new_content)
        diff = subprocess.run(
            ["git", "-C", str(self.target), "diff"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self._git("checkout", "--", ".")
        patch = Path(self.tmp.name) / f"{name}.patch"
        patch.write_text(diff)
        return patch

    def judge(self, patch: Path) -> int:
        return ia.main(
            [
                "--journal", str(self.journal),
                "judge",
                "--target-dir", str(self.target),
                "--config", CONFIG_REL,
                "--runner", self.runner,
                "--candidate", str(patch),
            ]
        )

    def baseline(self) -> int:
        return ia.main(
            [
                "--journal", str(self.journal),
                "baseline",
                "--target-dir", str(self.target),
                "--config", CONFIG_REL,
                "--runner", self.runner,
            ]
        )

    def journal_entries(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.journal.read_text().splitlines()
            if line.strip()
        ]

    # -- the three fixture cases from the plan (Fertig-wenn, 6a) -----------
    def test_valid_candidate_is_kept(self) -> None:
        patch = self.make_candidate(
            "good", CONFIG_REL, BASE_CONFIG + "test-new PASS\n"
        )
        self.assertEqual(self.judge(patch), ia.EXIT_KEEP)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "keep")
        self.assertIsNone(entry["grund"])
        self.assertEqual(entry["tests"], {"total": 2, "failed": 0})

    def test_flaky_candidate_is_discarded(self) -> None:
        patch = self.make_candidate(
            "flaky", CONFIG_REL, BASE_CONFIG + "test-new FLAKY\n"
        )
        self.assertEqual(self.judge(patch), ia.EXIT_DISCARD)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "discard")
        self.assertEqual(entry["grund"], "flaky")

    def test_red_on_head_candidate_is_discarded_as_false_positive(self) -> None:
        patch = self.make_candidate(
            "red", CONFIG_REL, BASE_CONFIG + "test-new FAIL\n"
        )
        self.assertEqual(self.judge(patch), ia.EXIT_DISCARD)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "discard")
        self.assertEqual(entry["grund"], "false-positive")

    # -- D2 nuance: pre-existing red is the target's finding, not the
    #    candidate's false positive --------------------------------------
    def test_preexisting_red_test_does_not_blame_candidate(self) -> None:
        (self.target / CONFIG_REL).write_text(BASE_CONFIG + "test-old FAIL\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "target has a finding")
        patch = self.make_candidate(
            "good",
            CONFIG_REL,
            BASE_CONFIG + "test-old FAIL\ntest-new PASS\n",
        )
        self.assertEqual(self.judge(patch), ia.EXIT_KEEP)

    # -- candidate-shape discards ------------------------------------------
    def test_out_of_scope_candidate_is_discarded(self) -> None:
        patch = self.make_candidate("scope", "README.md", "hacked\n")
        self.assertEqual(self.judge(patch), ia.EXIT_DISCARD)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["grund"], "out-of-scope")

    def test_garbage_patch_is_discarded_as_invalid(self) -> None:
        patch = Path(self.tmp.name) / "garbage.patch"
        patch.write_text("this is not a diff\n")
        self.assertEqual(self.judge(patch), ia.EXIT_DISCARD)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["grund"], "invalid")

    # -- hard failures are never verdicts -----------------------------------
    def test_flaky_baseline_hard_fails(self) -> None:
        (self.target / CONFIG_REL).write_text(BASE_CONFIG + "test-old FLAKY\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "existing suite is flaky")
        self.assertEqual(self.baseline(), ia.EXIT_HARD_FAIL)
        patch = self.make_candidate(
            "good", CONFIG_REL, BASE_CONFIG + "test-old FLAKY\ntest-new PASS\n"
        )
        self.assertEqual(self.judge(patch), ia.EXIT_HARD_FAIL)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "hard-fail")
        self.assertNotIn(entry["grund"], (None, "flaky", "false-positive"))

    def test_crashing_runner_hard_fails(self) -> None:
        crash = Path(self.tmp.name) / "crash.py"
        crash.write_text("import sys; sys.exit(13)\n")
        patch = self.make_candidate(
            "good", CONFIG_REL, BASE_CONFIG + "test-new PASS\n"
        )
        rc = ia.main(
            [
                "--journal", str(self.journal),
                "judge",
                "--target-dir", str(self.target),
                "--config", CONFIG_REL,
                "--runner", f"{sys.executable} {crash}",
                "--candidate", str(patch),
            ]
        )
        self.assertEqual(rc, ia.EXIT_HARD_FAIL)

    # -- harness hygiene -----------------------------------------------------
    def test_candidate_is_always_reverted(self) -> None:
        for content in (
            BASE_CONFIG + "test-new PASS\n",
            BASE_CONFIG + "test-new FAIL\n",
            BASE_CONFIG + "test-new FLAKY\n",
        ):
            self.judge(self.make_candidate("c", CONFIG_REL, content))
            self.assertEqual((self.target / CONFIG_REL).read_text(), BASE_CONFIG)

    def test_journal_is_append_only_across_judgements(self) -> None:
        self.judge(self.make_candidate("a", CONFIG_REL, BASE_CONFIG + "test-a PASS\n"))
        self.judge(self.make_candidate("b", CONFIG_REL, BASE_CONFIG + "test-b FAIL\n"))
        entries = self.journal_entries()
        self.assertEqual([e["verdict"] for e in entries], ["keep", "discard"])
        for e in entries:
            self.assertEqual(e["schema"], 1)
            for field in ("ts", "candidate_sha", "target_sha", "grund", "dauer_s"):
                self.assertIn(field, e)

    def test_baseline_is_cached_per_sha(self) -> None:
        self.assertEqual(self.baseline(), 0)
        cache = ia.baseline_cache_path(self.journal, ia.target_sha(self.target))
        self.assertTrue(cache.exists())
        first = cache.read_text()
        self.assertEqual(self.baseline(), 0)  # second call: cache hit, no rerun
        self.assertEqual(cache.read_text(), first)


if __name__ == "__main__":
    unittest.main()
