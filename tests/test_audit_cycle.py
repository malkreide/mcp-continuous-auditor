#!/usr/bin/env python3
"""Tests for deploy/microvm/run-audit-cycle.sh — the Broker-side breaker
orchestrator (Analysis T-B).

Drives the real script with a FAKE worker (RUN_WORKER_CMD) so no qemu/microVM is
needed, and asserts the budget breaker is actually fed on the Broker side:
  * a worker that ships a fresh summary -> its outcome is recorded;
  * a worker that ships nothing -> missing result recorded as hard-fail, exit 1.
Stdlib-only; needs bash + python3 (both present in the audit environment).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CYCLE = REPO / "deploy" / "microvm" / "run-audit-cycle.sh"


@unittest.skipUnless(CYCLE.exists() and shutil.which("bash"), "cycle script or bash missing")
class AuditCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.dropbox = self.dir / "dropbox"
        self.dropbox.mkdir()
        self.state = self.dir / "budget-state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake(self, name: str, body: str) -> str:
        p = self.dir / name
        p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        return f"bash {p}"

    def _run(self, worker_cmd: str) -> subprocess.CompletedProcess:
        env = {
            "DROPBOX": str(self.dropbox),
            "BUDGET_STATE": str(self.state),
            "RUN_WORKER_CMD": worker_cmd,
            "SETTLE_SECONDS": "0",
            "TARGET_REPO": "o/r",
            "PATH": "/usr/sbin:/usr/bin:/bin:/sbin",
        }
        return subprocess.run(
            ["bash", str(CYCLE)], env=env, capture_output=True, text=True, timeout=60
        )

    def _last_run(self) -> dict:
        st = json.loads(self.state.read_text(encoding="utf-8"))
        return st["runs"][-1]

    def test_worker_findings_outcome_is_recorded(self) -> None:
        worker = self._fake(
            "worker.sh",
            'd="${DROPBOX}/run-$$"; mkdir -p "$d"\n'
            'printf \'{"outcome":"findings","exit_code":2}\' > "$d/nightly-summary.json"\n',
        )
        r = self._run(worker)
        self.assertEqual(r.returncode, 2, msg=r.stderr)
        self.assertEqual(self._last_run()["outcome"], "findings")

    def test_worker_green_outcome_is_recorded(self) -> None:
        worker = self._fake(
            "worker.sh",
            'd="${DROPBOX}/run-$$"; mkdir -p "$d"\n'
            'printf \'{"outcome":"green","exit_code":0}\' > "$d/nightly-summary.json"\n',
        )
        r = self._run(worker)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(self._last_run()["outcome"], "green")

    def test_missing_evidence_recorded_as_hard_fail(self) -> None:
        # Worker ships nothing (timed out / died) -> the breaker must count it.
        worker = self._fake("noop.sh", "true\n")
        r = self._run(worker)
        self.assertEqual(r.returncode, 1, msg=r.stderr)
        self.assertEqual(self._last_run()["outcome"], "hard-fail")


if __name__ == "__main__":
    unittest.main()
