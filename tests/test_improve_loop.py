#!/usr/bin/env python3
"""Tests for scripts/improve-loop.sh — the REAL Phase-6c orchestrator, driven
end-to-end with a fake writer (WRITER_CMD queue of prepared candidate diffs),
the fake suite runner from the acceptance tests, and a LOCAL git target
(TARGET_GIT_URL points at a directory — no network, no promptfoo, no LLM).

Asserts the loop's contract: keeps are committed onto the improve branch (and
only under promptfoo/), discards and writer exhaustion end the run cleanly,
every iteration feeds the improve-own budget state, a writer crash or flaky
baseline hard-fails the whole run, and the keeps ceiling stops early.
Needs `bash` + `git`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOOP = REPO / "scripts" / "improve-loop.sh"

CONFIG_REL = "promptfoo/promptfooconfig.determ.yaml"
BASE_CONFIG = "test-base PASS\n"

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
        ok = {"PASS": True, "FAIL": False, "FLAKY": n % 2 == 0}[parts[1]]
        results.append({"description": parts[0], "success": ok})
    out.write_text(json.dumps({"results": {"results": results}}))
    sys.exit(0 if all(r["success"] for r in results) else 1)
    """
)

# Serves prepared *.patch files from its queue dir, one per call; exit 10
# (NO-PROPOSAL) once exhausted — the WRITER_CMD contract.
WRITER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import shutil, sys
    from pathlib import Path

    queue = Path(__file__).parent / "queue"
    out_patch = Path(sys.argv[2])
    counter = queue / ".served"
    n = int(counter.read_text()) if counter.exists() else 0
    candidates = sorted(queue.glob("*.patch"))
    if n >= len(candidates):
        sys.exit(10)
    counter.write_text(str(n + 1))
    shutil.copy(candidates[n], out_patch)
    Path(str(out_patch) + ".tokens").write_text("1000")
    sys.exit(0)
    """
)


@unittest.skipUnless(LOOP.exists() and shutil.which("bash") and shutil.which("git"),
                     "loop script, bash or git missing")
class ImproveLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin"
        self.audit = root / "audit"
        self.queue = root / "queue"
        self.queue.mkdir()
        (self.origin / "promptfoo").mkdir(parents=True)
        (self.origin / CONFIG_REL).write_text(BASE_CONFIG)
        (self.origin / "README.md").write_text("target\n")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "test")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        runner = root / "fake_runner.py"
        runner.write_text(RUNNER)
        writer = root / "fake_writer.py"
        writer.write_text(WRITER)
        (root / "queue").mkdir(exist_ok=True)
        # the fake writer resolves its queue next to itself
        (writer.parent / "queue").mkdir(exist_ok=True)
        self.runner_cmd = f"{sys.executable} {runner}"
        self.writer_cmd = f"{sys.executable} {writer}"
        self.writer_queue = writer.parent / "queue"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def _git(self, *argv: str) -> None:
        subprocess.run(["git", "-C", str(self.origin), *argv], check=True,
                       capture_output=True)

    def queue_candidate(self, name: str, old_config: str, new_config: str) -> None:
        """Prepare a candidate diff from `old_config` to `new_config`.

        `old_config` is the state the real writer would see at proposal time —
        the config *after* any earlier keeps, not necessarily the base."""
        import difflib

        diff = "".join(difflib.unified_diff(
            old_config.splitlines(keepends=True),
            new_config.splitlines(keepends=True),
            fromfile=f"a/{CONFIG_REL}", tofile=f"b/{CONFIG_REL}",
        ))
        (self.writer_queue / f"{name}.patch").write_text(diff)

    def run_loop(self, **env_overrides: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({
            "TARGET_REPO": "owner/target",
            "TARGET_REF": "main",
            "TARGET_GIT_URL": str(self.origin),
            "AUDIT_DIR": str(self.audit),
            "WRITER_CMD": self.writer_cmd,
            "IMPROVE_RUNNER": self.runner_cmd,
            "IMPROVE_COVERAGE_MODE": "off",
            "IMPROVE_BRANCH": "improve/test",
            "IMPROVE_PUBLISH": "0",
        })
        env.update(env_overrides)
        return subprocess.run(["bash", str(LOOP)], env=env,
                              capture_output=True, text=True, timeout=120)

    def summary(self) -> dict:
        return json.loads((self.audit / "improve-summary.json").read_text())

    def checkout_git(self, *argv: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.audit / "improve" / "target"), *argv],
            check=True, capture_output=True, text=True,
        ).stdout

    # -- the loop contract --------------------------------------------------
    def test_keep_and_discard_end_to_end(self) -> None:
        kept = BASE_CONFIG + "test-a PASS\n"
        self.queue_candidate("001-good", BASE_CONFIG, kept)
        self.queue_candidate("002-red", kept, kept + "test-b FAIL\n")
        r = self.run_loop()
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)

        s = self.summary()
        self.assertEqual(s["outcome"], "completed")
        self.assertEqual(s["iterations"], 2)
        self.assertEqual(len(s["keeps"]), 1)
        self.assertEqual(s["discards"], {"false-positive": 1})

        # exactly one keep committed on the improve branch, base untouched
        log = self.checkout_git("log", "--oneline", "main..improve/test")
        self.assertEqual(len(log.strip().splitlines()), 1)
        changed = self.checkout_git(
            "diff", "--name-only", "main..improve/test"
        ).strip().splitlines()
        self.assertEqual(changed, [CONFIG_REL])

        # every iteration fed the improve-own budget state
        state = json.loads((self.audit / "improve-budget-state.json").read_text())
        outcomes = [run["outcome"] for run in state["runs"]]
        self.assertEqual(outcomes, ["green", "findings"])
        self.assertEqual(state["window"]["tokens_used"], 2000)

    def test_keeps_ceiling_stops_early(self) -> None:
        kept = BASE_CONFIG + "test-a PASS\n"
        self.queue_candidate("001-good", BASE_CONFIG, kept)
        self.queue_candidate("002-also-good", kept, kept + "test-b PASS\n")
        r = self.run_loop(IMPROVE_MAX_KEEPS="1")
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)
        s = self.summary()
        self.assertEqual(s["iterations"], 1)  # second candidate never judged
        self.assertEqual(len(s["keeps"]), 1)

    def test_writer_crash_hard_fails_the_run(self) -> None:
        crash = Path(self.tmp.name) / "crash.py"
        crash.write_text("import sys; sys.exit(3)\n")
        r = self.run_loop(WRITER_CMD=f"{sys.executable} {crash}")
        self.assertEqual(r.returncode, 1, msg=r.stderr + r.stdout)
        self.assertEqual(self.summary()["outcome"], "hard-fail")
        state = json.loads((self.audit / "improve-budget-state.json").read_text())
        self.assertEqual(state["runs"][-1]["outcome"], "hard-fail")

    def test_flaky_baseline_hard_fails_before_any_writer_call(self) -> None:
        (self.origin / CONFIG_REL).write_text(BASE_CONFIG + "test-old FLAKY\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "flaky suite")
        flaky = BASE_CONFIG + "test-old FLAKY\n"
        self.queue_candidate("001-good", flaky, flaky + "test-a PASS\n")
        r = self.run_loop()
        self.assertEqual(r.returncode, 1, msg=r.stderr + r.stdout)
        self.assertEqual(self.summary()["outcome"], "hard-fail")
        self.assertFalse((self.writer_queue / ".served").exists())  # writer never ran

    def test_missing_writer_cmd_hard_fails(self) -> None:
        r = self.run_loop(WRITER_CMD="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("WRITER_CMD", r.stderr)


if __name__ == "__main__":
    unittest.main()
