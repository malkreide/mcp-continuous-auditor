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
# ones (a call counter persists next to the runner script); CHECK passes iff a
# target file has the expected content (`test-x CHECK <path> <want>`) — that
# is what lets mutant patches against the fake target flip real outcomes.
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
        if len(parts) < 2 or not parts[0].startswith("test-"):
            continue
        name, mode = parts[0], parts[1]
        if mode == "PASS":
            ok = True
        elif mode == "FAIL":
            ok = False
        elif mode == "FLAKY":
            ok = n % 2 == 0
        elif mode == "CHECK":
            ok = Path(parts[2]).read_text().strip() == parts[3]
        else:
            continue
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

    def make_candidate(
        self, name: str, rel_path: str, new_content: str, into: Path | None = None
    ) -> Path:
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
        out_dir = into or Path(self.tmp.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        patch = out_dir / f"{name}.patch"
        patch.write_text(diff)
        return patch

    def judge(
        self, patch: Path, coverage: str = "off", mutants: Path | None = None
    ) -> int:
        # D1/D2 tests judge with D3 off; the D3 tests opt in explicitly.
        argv = [
            "--journal", str(self.journal),
            "judge",
            "--target-dir", str(self.target),
            "--config", CONFIG_REL,
            "--runner", self.runner,
            "--candidate", str(patch),
            "--coverage-mode", coverage,
        ]
        if mutants is not None:
            argv += ["--mutants-dir", str(mutants)]
        return ia.main(argv)

    def baseline(self, coverage: str = "off", mutants: Path | None = None) -> int:
        argv = [
            "--journal", str(self.journal),
            "baseline",
            "--target-dir", str(self.target),
            "--config", CONFIG_REL,
            "--runner", self.runner,
            "--coverage-mode", coverage,
        ]
        if mutants is not None:
            argv += ["--mutants-dir", str(mutants)]
        return ia.main(argv)

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

    # -- D3-lite (schema-path): Phase 6b -------------------------------------
    def _setup_lite_world(self) -> None:
        (self.target / CONFIG_REL).write_text(
            BASE_CONFIG + "test-geo PASS schemas/a.json\n"
        )
        self._git("add", "-A")
        self._git("commit", "-qm", "suite references schemas/a.json")

    def test_lite_duplicate_schema_ref_is_redundant(self) -> None:
        # The Fertig-wenn fixture of 6b: a semantic duplicate of an existing
        # assert is discarded and the journal grund is `redundant`.
        self._setup_lite_world()
        patch = self.make_candidate(
            "dup",
            CONFIG_REL,
            BASE_CONFIG + "test-geo PASS schemas/a.json\ntest-dup PASS schemas/a.json\n",
        )
        self.assertEqual(self.judge(patch, coverage="schema-path"), ia.EXIT_DISCARD)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "discard")
        self.assertEqual(entry["grund"], "redundant")
        self.assertEqual(entry["coverage_mode"], "schema-path")

    def test_lite_candidate_without_any_schema_ref_is_redundant(self) -> None:
        self._setup_lite_world()
        patch = self.make_candidate(
            "noref",
            CONFIG_REL,
            BASE_CONFIG + "test-geo PASS schemas/a.json\ntest-new PASS\n",
        )
        self.assertEqual(self.judge(patch, coverage="schema-path"), ia.EXIT_DISCARD)
        self.assertEqual(self.journal_entries()[0]["grund"], "redundant")

    def test_lite_new_schema_ref_is_kept(self) -> None:
        self._setup_lite_world()
        patch = self.make_candidate(
            "new",
            CONFIG_REL,
            BASE_CONFIG + "test-geo PASS schemas/a.json\ntest-new PASS schemas/b.json\n",
        )
        self.assertEqual(self.judge(patch, coverage="schema-path"), ia.EXIT_KEEP)
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "keep")
        self.assertEqual(entry["new_schema_refs"], ["schemas/b.json"])

    def test_lite_d1_flakiness_beats_redundancy(self) -> None:
        # The rule is D1 ∧ D2 ∧ D3, judged in that order: a flaky duplicate
        # journals `flaky` (the poisonous property), not `redundant`.
        self._setup_lite_world()
        patch = self.make_candidate(
            "flakydup",
            CONFIG_REL,
            BASE_CONFIG + "test-geo PASS schemas/a.json\ntest-dup FLAKY schemas/a.json\n",
        )
        self.assertEqual(self.judge(patch, coverage="schema-path"), ia.EXIT_DISCARD)
        self.assertEqual(self.journal_entries()[0]["grund"], "flaky")

    # -- D3 mutation mode: Phase 6b -------------------------------------------
    def _setup_mutation_world(self) -> Path:
        """Target with two source files; the existing suite checks only a.txt.
        Mutant m1 (flips a.txt) is killed by the existing suite; mutant m2
        (flips b.txt) survives it — the pool a valuable candidate must hit."""
        (self.target / "src").mkdir()
        (self.target / "src/a.txt").write_text("A\n")
        (self.target / "src/b.txt").write_text("B\n")
        (self.target / CONFIG_REL).write_text("test-base CHECK src/a.txt A\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "mutation world")
        mutants = Path(self.tmp.name) / "mutants"
        self.make_candidate("m1", "src/a.txt", "X\n", into=mutants)
        self.make_candidate("m2", "src/b.txt", "X\n", into=mutants)
        return mutants

    def mutation_config(self, extra_line: str) -> str:
        return f"test-base CHECK src/a.txt A\n{extra_line}\n"

    def test_mutation_duplicate_assert_is_redundant(self) -> None:
        # Fertig-wenn (strong form): a duplicate of the existing assert kills
        # no surviving mutant -> discard, journal grund `redundant`.
        mutants = self._setup_mutation_world()
        patch = self.make_candidate(
            "dup", CONFIG_REL, self.mutation_config("test-dup CHECK src/a.txt A")
        )
        self.assertEqual(
            self.judge(patch, coverage="mutation", mutants=mutants), ia.EXIT_DISCARD
        )
        (entry,) = self.journal_entries()
        self.assertEqual(entry["grund"], "redundant")
        self.assertEqual(entry["coverage_mode"], "mutation")

    def test_mutation_killing_surviving_mutant_is_kept(self) -> None:
        mutants = self._setup_mutation_world()
        patch = self.make_candidate(
            "new", CONFIG_REL, self.mutation_config("test-new CHECK src/b.txt B")
        )
        self.assertEqual(
            self.judge(patch, coverage="mutation", mutants=mutants), ia.EXIT_KEEP
        )
        (entry,) = self.journal_entries()
        self.assertEqual(entry["verdict"], "keep")
        self.assertEqual(entry["killed_mutant"], "m2.patch")
        # judging never leaves mutants or the candidate applied
        self.assertEqual((self.target / "src/b.txt").read_text(), "B\n")
        self.assertEqual(
            (self.target / CONFIG_REL).read_text(), "test-base CHECK src/a.txt A\n"
        )

    def test_mutation_empty_pool_hard_fails(self) -> None:
        self._setup_mutation_world()
        empty = Path(self.tmp.name) / "empty-mutants"
        empty.mkdir()
        patch = self.make_candidate(
            "new", CONFIG_REL, self.mutation_config("test-new CHECK src/b.txt B")
        )
        self.assertEqual(
            self.judge(patch, coverage="mutation", mutants=empty), ia.EXIT_HARD_FAIL
        )
        self.assertEqual(self.journal_entries()[0]["verdict"], "hard-fail")

    def test_mutation_fully_killed_pool_hard_fails(self) -> None:
        # A pool the existing suite already kills entirely cannot measure
        # added value — fail closed instead of discarding every candidate.
        mutants = self._setup_mutation_world()
        (mutants / "m2.patch").unlink()  # keep only m1 (killed by test-base)
        patch = self.make_candidate(
            "new", CONFIG_REL, self.mutation_config("test-new CHECK src/b.txt B")
        )
        self.assertEqual(
            self.judge(patch, coverage="mutation", mutants=mutants), ia.EXIT_HARD_FAIL
        )

    def test_mutation_baseline_precompute_and_cache(self) -> None:
        mutants = self._setup_mutation_world()
        self.assertEqual(self.baseline(coverage="mutation", mutants=mutants), 0)
        mcache = ia.mutants_cache_path(self.journal, ia.target_sha(self.target))
        status = json.loads(mcache.read_text())["mutants"]
        self.assertEqual(status, {"m1.patch": "killed", "m2.patch": "survived"})
        first = mcache.read_text()
        self.assertEqual(self.baseline(coverage="mutation", mutants=mutants), 0)
        self.assertEqual(mcache.read_text(), first)  # cache hit, not recomputed


if __name__ == "__main__":
    unittest.main()

