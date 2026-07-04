#!/usr/bin/env python3
"""Tests for the Worker egress interlock (Analysis S3).

Drives the real sourceable helper ``deploy/microvm/_egress-interlock.sh`` — the
same code run-worker.sh sources — and asserts it FAILS CLOSED: with the allowlist
required but the nft table absent it refuses (non-zero), and only an explicit
``EGRESS_ALLOWLIST=off`` opens egress (with a loud warning + empty RUN_AS).

Stdlib-only. Does not need /dev/kvm, qemu or a base image (that is exactly why the
interlock was factored out of run-worker.sh). Assumes the test host does NOT have
the ``inet mcp_worker_egress`` table loaded, which is true anywhere the rollout
has not been applied.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "deploy" / "microvm" / "_egress-interlock.sh"


def _resolve(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Source the helper, call resolve_worker_run_as, print rc + RUN_AS."""
    script = (
        f'source "{HELPER}"; '
        f'resolve_worker_run_as; rc=$?; '
        f'echo "RC=${{rc}}"; echo "RUN_AS=[${{RUN_AS[*]-}}]"'
    )
    env = {"PATH": "/usr/sbin:/usr/bin:/bin:/sbin", **env_overrides}
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )


@unittest.skipUnless(HELPER.exists(), "egress interlock helper missing")
class EgressInterlockTest(unittest.TestCase):
    def test_refuses_to_boot_without_allowlist(self) -> None:
        # Default EGRESS_ALLOWLIST=on, table absent -> refuse (non-zero).
        r = _resolve({})
        self.assertIn("RC=1", r.stdout, msg=r.stderr)
        self.assertIn("refusing to boot with open egress", r.stderr)
        # No privilege-drop prefix is offered when the interlock fails.
        self.assertIn("RUN_AS=[]", r.stdout)

    def test_explicit_on_still_refuses_without_table(self) -> None:
        r = _resolve({"EGRESS_ALLOWLIST": "on"})
        self.assertIn("RC=1", r.stdout, msg=r.stderr)

    def test_off_opens_egress_with_warning_and_empty_run_as(self) -> None:
        r = _resolve({"EGRESS_ALLOWLIST": "off"})
        self.assertIn("RC=0", r.stdout, msg=r.stderr)
        self.assertIn("UNRESTRICTED outbound egress", r.stderr)
        # RUN_AS stays empty: qemu runs unwrapped on an already-isolated dev host.
        self.assertIn("RUN_AS=[]", r.stdout)


if __name__ == "__main__":
    unittest.main()
