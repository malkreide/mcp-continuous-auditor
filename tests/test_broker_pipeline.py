#!/usr/bin/env python3
"""Integration tests for the Broker receive+classify path (Analysis S2).

These drive the REAL committed handler ``deploy/microvm/channel/_receive-one.sh``
end-to-end — feeding it a one-line header + a tar bundle on stdin exactly as
socat does over vsock — and assert the Broker's authoritative verdict plus the
path-traversal guard. Stdlib-only (``python3 -m unittest``); needs ``bash`` +
``tar`` + ``python3`` on PATH (all present in the audit environment).

Scope note: the exact-name extraction (traversal blocked) is tested here because
it is what the handler implements today. Symlink-member hardening, a stream size
limit and a read timeout are review finding S-D (Iteration 3) — not yet
implemented, so not asserted here.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HANDLER = REPO / "deploy" / "microvm" / "channel" / "_receive-one.sh"
REPORT_PY = REPO / "scripts" / "nightly_audit_report.py"

_GREEN_GATES = {"ruff": 0, "mypy": 0, "pytest": 0, "schema_drift": 0, "promptfoo_rc": 0}


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    """Build an in-memory tar whose members are named EXACTLY by the dict keys —
    including hostile names like ``../escape.json`` or an absolute path, which a
    real compromised Worker could ship."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@unittest.skipUnless(HANDLER.exists() and shutil.which("bash"), "handler or bash missing")
class BrokerPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dropbox = Path(self.tmp.name) / "dropbox"
        self.dropbox.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_payload(self, payload: bytes) -> dict:
        """Feed raw `payload` (header + tar bytes) to the real handler on stdin;
        return the Broker-produced nightly-summary.json as a dict."""
        env = {
            "DROPBOX": str(self.dropbox),
            "REPORT_PY": str(REPORT_PY),
            "PATH": "/usr/sbin:/usr/bin:/bin:/sbin",
        }
        subprocess.run(
            ["bash", str(HANDLER)],
            input=payload,
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )
        run_dirs = [p for p in self.dropbox.iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1, "handler must create exactly one run dir")
        self.run_dir = run_dirs[0]
        return json.loads((self.run_dir / "nightly-summary.json").read_text())

    def _deliver(self, members: dict[str, bytes], header: bytes = b"AUDIT-RESULT rc=0\n") -> dict:
        """Run the real handler with `header + tar(members)` on stdin."""
        return self._run_payload(header + _tar_bytes(members))

    @staticmethod
    def _evidence(gates: dict, target: str = "o/r", sha: str = "abc1234") -> bytes:
        return json.dumps({"target": target, "target_sha": sha, "gates": gates}).encode()

    # --- verdict re-derivation ------------------------------------------------

    def test_green_evidence_classifies_green(self) -> None:
        s = self._deliver({
            "nightly-evidence.json": self._evidence(_GREEN_GATES),
            "promptfoo.json": json.dumps({"results": {"stats": {"errors": 0}, "results": []}}).encode(),
        })
        self.assertEqual(s["outcome"], "green")
        self.assertTrue(s["green"])

    def test_forged_green_exit_codes_caught_by_promptfoo_evidence(self) -> None:
        # Worker claims all gates 0 but the promptfoo JSON it shipped carries real
        # failures. The Broker classifies from that too -> findings, not green.
        pf = {"results": {"stats": {"errors": 0}, "results": [
            {"success": False, "testCase": {"description": "schema"},
             "gradingResult": {"componentResults": [{"pass": False, "assertion": {"type": "is-json"}}]}},
            {"success": False, "testCase": {"description": "pii", "metadata": {"pluginId": "pii"}},
             "gradingResult": {"componentResults": [{"pass": False, "assertion": {"type": "llm-rubric"}}]}},
        ]}}
        s = self._deliver({
            "nightly-evidence.json": self._evidence(_GREEN_GATES),
            "promptfoo.json": json.dumps(pf).encode(),
        })
        self.assertEqual(s["outcome"], "findings")
        self.assertFalse(s["green"])
        self.assertTrue(s["schema_drift"])
        self.assertTrue(s["redteam"])

    def test_garbled_evidence_is_hard_fail(self) -> None:
        s = self._deliver({
            "nightly-evidence.json": b"not json {{{",
            "promptfoo.json": b"{}",
        })
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])

    def test_length_prefixed_frame_reads_exact(self) -> None:
        # Analysis T-G: a header declaring len=<bytes> makes the Broker read exactly
        # that many bytes (no line-framing desync). Exact length -> clean extract.
        members = {
            "nightly-evidence.json": self._evidence(_GREEN_GATES),
            "promptfoo.json": json.dumps({"results": {"stats": {"errors": 0}, "results": []}}).encode(),
        }
        tarb = _tar_bytes(members)
        header = f"AUDIT-RESULT rc=0 len={len(tarb)}\n".encode()
        s = self._run_payload(header + tarb)
        self.assertEqual(s["outcome"], "green")
        self.assertTrue(s["green"])

    # --- the path-traversal guard (exact-name extraction) ---------------------

    def test_hostile_members_are_not_extracted(self) -> None:
        escape = f"pwned-{id(self)}.json".encode()
        s = self._deliver({
            "nightly-evidence.json": self._evidence(_GREEN_GATES),
            "promptfoo.json": json.dumps({"results": {"stats": {"errors": 0}, "results": []}}).encode(),
            # Hostile members a compromised Worker might ship:
            "../escape.json": escape,                 # parent-dir traversal
            "/tmp/mcp-broker-pwned.json": escape,      # absolute path
            "nested/evil.json": escape,                # subdir member
        })
        # The verdict is still derived from the two legit members.
        self.assertEqual(s["outcome"], "green")
        # Only the two exact-name files landed in the run dir.
        extracted = sorted(p.name for p in self.run_dir.iterdir())
        self.assertEqual(extracted, ["header.txt", "nightly-evidence.json",
                                     "nightly-report.md", "nightly-summary.json",
                                     "promptfoo.json"])
        # Nothing escaped upward or to an absolute path.
        self.assertFalse((self.dropbox / "escape.json").exists())
        self.assertFalse(Path("/tmp/mcp-broker-pwned.json").exists())
        self.assertFalse((self.run_dir / "nested").exists())

    def test_symlink_evidence_member_is_rejected(self) -> None:
        # Analysis S-D: a member named nightly-evidence.json that is actually a
        # SYMLINK to a Broker file must be dropped (not followed) — else the
        # classifier becomes an arbitrary-file read. It reads as ABSENT -> hard-fail.
        secret = Path(self.tmp.name) / "broker-secret.json"
        secret.write_text(json.dumps({
            "target": "o/r", "target_sha": "abc1234", "gates": _GREEN_GATES,
        }), encoding="utf-8")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            pj = json.dumps({"results": {"stats": {"errors": 0}, "results": []}}).encode()
            info = tarfile.TarInfo(name="promptfoo.json")
            info.size = len(pj)
            tar.addfile(info, io.BytesIO(pj))
            link = tarfile.TarInfo(name="nightly-evidence.json")
            link.type = tarfile.SYMTYPE
            link.linkname = str(secret)  # point the "evidence" at a Broker file
            tar.addfile(link)

        s = self._run_payload(b"AUDIT-RESULT rc=0\n" + buf.getvalue())
        # The symlink was dropped: no evidence file, so the verdict is hard-fail,
        # and the secret was never consumed as evidence (its green gates ignored).
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertFalse(s["green"])
        self.assertFalse((self.run_dir / "nightly-evidence.json").exists())


if __name__ == "__main__":
    unittest.main()
