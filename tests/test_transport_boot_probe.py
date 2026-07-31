#!/usr/bin/env python3
"""Tests for scripts/transport_boot_probe.py — the transport boot gate.

The gate exists because two real bugs were invisible to every other gate: a
server that crashed at start under the new SDK, and one that answered HTTP 421 to
every request made under a real hostname. So the tests are built around fixtures
that reproduce exactly those two failures, plus the stdin trap that made an
earlier hand-rolled version of this probe report a failure that was not there.

Everything here is stdlib-only and offline: the fixtures under ``tests/fixtures/``
speak enough JSON-RPC to be probed without fastmcp. ``FastMCPBootTest`` at the
bottom is the one class that needs fastmcp and skips cleanly without it, keeping
the suite's stdlib-only property intact.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import transport_boot_probe as tbp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

try:
    import tomllib  # noqa: F401
    _HAVE_TOMLLIB = True
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    _HAVE_TOMLLIB = False

try:
    import fastmcp  # noqa: F401
    _HAVE_FASTMCP = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_FASTMCP = False


def _plan(transport: str, argv: list[str], mode: str = "declared") -> tbp.LaunchPlan:
    return tbp.LaunchPlan(transport=transport, mode=mode, argv=argv)


def _fixture_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# derivation — read the target's config, do not guess
# ---------------------------------------------------------------------------

class DerivationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def test_floor_applies_to_a_target_that_declares_nothing(self) -> None:
        # The blind spot this gate removes is a transport nobody thought to
        # exercise, so probing only what the target advertises would rebuild it.
        d = tbp.derive(self.root, env={})
        self.assertEqual(d.transports, [tbp.STDIO, tbp.STREAMABLE_HTTP])
        self.assertEqual(sorted(d.floor_added), sorted([tbp.STDIO, tbp.STREAMABLE_HTTP]))

    def test_sse_is_derived_from_source_not_assumed(self) -> None:
        # SSE is only probed when the target still offers it — it is NOT in the
        # floor, so a target that dropped SSE is not held to a transport it retired.
        self._write("server.py", '''
            from fastmcp import FastMCP
            mcp = FastMCP("t")
            if __name__ == "__main__":
                mcp.run(transport="sse", host="0.0.0.0", port=8000)
        ''')
        d = tbp.derive(self.root, env={})
        self.assertIn(tbp.SSE, d.transports)
        self.assertNotIn(tbp.SSE, d.floor_added)
        self.assertTrue(any("server.py" in s for s in d.sources))

    def test_dockerfile_transport_flag_is_derived(self) -> None:
        self._write("Dockerfile", '''
            FROM python:3.12-slim
            CMD ["serve", "--transport", "http", "--host", "0.0.0.0"]
        ''')
        d = tbp.derive(self.root, env={})
        self.assertIn(tbp.STREAMABLE_HTTP, d.transports)
        self.assertNotIn(tbp.STREAMABLE_HTTP, d.floor_added)  # derived, not floored
        self.assertTrue(any("Dockerfile" in s for s in d.sources))

    def test_env_example_transport_is_derived(self) -> None:
        self._write(".env.example", "MCP_TRANSPORT=sse\nMCP_PORT=8080\n")
        d = tbp.derive(self.root, env={})
        self.assertIn(tbp.SSE, d.transports)

    def test_explicit_override_suppresses_derivation_and_floor(self) -> None:
        self._write("server.py", 'mcp.run(transport="sse")')
        d = tbp.derive(self.root, env={"BOOT_TRANSPORTS": "stdio"})
        self.assertEqual(d.transports, [tbp.STDIO])
        self.assertEqual(d.floor_added, [])

    def test_transport_spellings_normalise(self) -> None:
        self.assertEqual(tbp.normalise_transport("HTTP"), tbp.STREAMABLE_HTTP)
        self.assertEqual(tbp.normalise_transport("streamable_http"), tbp.STREAMABLE_HTTP)
        self.assertEqual(tbp.normalise_transport("'sse'"), tbp.SSE)
        self.assertIsNone(tbp.normalise_transport("grpc"))

    @unittest.skipUnless(_HAVE_TOMLLIB, "tomllib requires Python 3.11+")
    def test_declared_commands_win_over_the_generic_launcher(self) -> None:
        # A target that declares how it is booted gets the faithful check: we run
        # ITS command, so its own startup code — where case 2 lives — is exercised.
        self._write("pyproject.toml", '''
            [project]
            name = "t"
            version = "0"

            [tool.mcp_auditor.boot.commands]
            "streamable-http" = ["serve", "--host", "{host}", "--port", "{port}"]
        ''')
        d = tbp.derive(self.root, env={})
        plan = tbp.build_launch_plan(tbp.STREAMABLE_HTTP, d)
        self.assertEqual(plan.mode, "declared")
        self.assertEqual(
            tbp.substitute(plan.argv, "0.0.0.0", 9999),
            ["serve", "--host", "0.0.0.0", "--port", "9999"],
        )

    def test_dunder_main_package_becomes_the_entrypoint(self) -> None:
        self._write("mypkg/__init__.py", "")
        self._write("mypkg/__main__.py", "print('hi')")
        d = tbp.derive(self.root, env={})
        plan = tbp.build_launch_plan(tbp.STDIO, d)
        self.assertEqual(plan.mode, "entrypoint")
        self.assertEqual(plan.argv[1:], ["-m", "mypkg"])

    def test_generic_mode_is_the_last_resort_and_is_labelled(self) -> None:
        d = tbp.derive(self.root, env={})
        plan = tbp.build_launch_plan(tbp.STDIO, d)
        self.assertEqual(plan.mode, "generic")
        self.assertIn("partial", plan.note)  # the weaker-evidence caveat is carried


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------

class StdioProbeTest(unittest.TestCase):
    def test_healthy_server_passes(self) -> None:
        plan = _plan(tbp.STDIO, [sys.executable, str(FIXTURES / "boot_stdio_ok.py")])
        res = tbp.probe_stdio(plan, timeout=20, cwd=FIXTURES)
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.tools, 2)

    def test_crash_at_start_is_caught(self) -> None:
        # Case 1: the read-only settings object. Import succeeds, tools are
        # declared, schemas match — and the process never comes up.
        plan = _plan(tbp.STDIO, [sys.executable, str(FIXTURES / "boot_stdio_crash.py")])
        res = tbp.probe_stdio(plan, timeout=20, cwd=FIXTURES)
        self.assertFalse(res.ok)
        self.assertIn("Settings", res.detail)
        self.assertIn("no field", res.detail)

    def test_closing_stdin_early_fabricates_a_failure(self) -> None:
        # THE TRAP, pinned. The same healthy server that passes above is measured
        # as broken the moment stdin is closed after the write — it shuts down
        # before the network-bound tools/list answer is out. If this test ever goes
        # green with _close_stdin_early=True, the probe has started closing stdin
        # somewhere and every slow target will be reported as a false finding.
        plan = _plan(tbp.STDIO, [sys.executable, str(FIXTURES / "boot_stdio_ok.py")])
        broken = tbp.probe_stdio(plan, timeout=20, cwd=FIXTURES, _close_stdin_early=True)
        self.assertFalse(broken.ok)

        healthy = tbp.probe_stdio(plan, timeout=20, cwd=FIXTURES)
        self.assertTrue(healthy.ok, msg=healthy.detail)

    def test_unspawnable_command_is_reported_not_raised(self) -> None:
        plan = _plan(tbp.STDIO, [str(FIXTURES / "does-not-exist")])
        res = tbp.probe_stdio(plan, timeout=5, cwd=FIXTURES)
        self.assertFalse(res.ok)
        self.assertIn("could not spawn", res.detail)


# ---------------------------------------------------------------------------
# streamable-http
# ---------------------------------------------------------------------------

class HttpProbeTest(unittest.TestCase):
    def _probe(self, mode: str, timeout: float = 20.0) -> tbp.ProbeResult:
        plan = _plan(tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_http_server.py")])
        return tbp.probe_streamable_http(
            plan, timeout=timeout, cwd=FIXTURES,
            bind_host="127.0.0.1", probe_host="mcp-boot-probe.audit.invalid",
            paths=["/mcp/"], env=_fixture_env(BOOT_FIXTURE_MODE=mode),
        )

    def test_healthy_server_passes_under_both_hosts(self) -> None:
        res = self._probe("ok")
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.tools, 2)
        self.assertEqual(res.evidence.get("path"), "/mcp/")

    def test_host_allowlist_421_is_a_finding_with_the_case_named(self) -> None:
        # Case 2: loopback works, a real hostname does not. The two-Host probe is
        # what makes this diagnostic instead of merely alarming.
        res = self._probe("host421")
        self.assertFalse(res.ok)
        self.assertIn("421", res.detail)
        self.assertEqual(res.evidence.get("case"), "host-allowlist-421")
        self.assertEqual(res.evidence.get("status"), 421)
        self.assertEqual(res.evidence.get("loopback_status"), 200)

    def test_a_loopback_only_probe_would_have_missed_it(self) -> None:
        # The reason the probe varies the Host header at all: against the very same
        # broken server, the loopback request succeeds completely. A gate that only
        # talked to 127.0.0.1 would call this deployment healthy.
        plan = _plan(tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_http_server.py")])
        port = tbp.free_port()
        env = tbp.launch_env(plan, "127.0.0.1", port, _fixture_env(BOOT_FIXTURE_MODE="host421"))
        proc = subprocess.Popen(plan.argv, cwd=str(FIXTURES), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            self.assertEqual(tbp.wait_for_port(port, time.monotonic() + 15, proc), "")
            loopback = tbp.http_post(port, "/mcp/", f"127.0.0.1:{port}",
                                     {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {}}, timeout=10)
            self.assertEqual(loopback.status, 200)          # looks perfectly healthy…
            hostile = tbp.http_post(port, "/mcp/", "real.example.org",
                                    {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                     "params": {}}, timeout=10)
            self.assertEqual(hostile.status, 421)           # …and is unusable
        finally:
            tbp._terminate(proc)

    def test_server_that_never_listens_is_a_finding(self) -> None:
        # Case 1 over HTTP: the process raises at start, so no port ever opens.
        plan = _plan(tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_stdio_crash.py")])
        res = tbp.probe_streamable_http(
            plan, timeout=15, cwd=FIXTURES, bind_host="127.0.0.1",
            probe_host="mcp-boot-probe.audit.invalid", paths=["/mcp/"],
            env=_fixture_env(),
        )
        self.assertFalse(res.ok)
        self.assertIn("never came up", res.detail)

    def test_hanging_server_is_bounded_by_the_deadline(self) -> None:
        # A server that hangs must not hang the gate. The bound is per attempt.
        started = time.monotonic()
        res = self._probe("hang", timeout=4.0)
        elapsed = time.monotonic() - started
        self.assertFalse(res.ok)
        self.assertLess(elapsed, 25.0, msg=f"probe overran its deadline: {elapsed:.1f}s")

    def test_sse_frames_are_parsed(self) -> None:
        payload = tbp._parse_sse_payload('event: message\ndata: {"jsonrpc":"2.0","id":1}\n\n')
        self.assertEqual(payload, {"jsonrpc": "2.0", "id": 1})


# ---------------------------------------------------------------------------
# the gate's own exit-code contract
# ---------------------------------------------------------------------------

class ExitContractTest(unittest.TestCase):
    """0 / 2 / 127 — and above all: a target that does not boot is a FINDING, not
    an infrastructure failure. Blurring those two is what the whole classifier
    contract rests on."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def _run(self, **env: str) -> tuple[int, dict]:
        report = self.root / "boot.json"
        os.environ.update({
            "BOOT_TARGET_ROOT": str(self.root),
            "BOOT_REPORT": str(report),
            "BOOT_TIMEOUT": "15",
            **env,
        })
        # The probe narrates to stdout/stderr for the operator reading the Worker
        # log; keep that out of the test output.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = tbp.main()
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return rc, data

    def test_a_target_that_cannot_be_booted_is_a_finding_not_hard_fail(self) -> None:
        # Nothing importable in an empty dir, so the generic launcher fails for
        # both floor transports. That is a statement about the TARGET -> exit 2.
        rc, data = self._run(MCP_SERVER_IMPORT="nonexistent_module_xyz:mcp",
                             BOOT_TRANSPORTS="stdio")
        self.assertEqual(rc, tbp.EXIT_FINDINGS)
        self.assertEqual(data.get("outcome"), "findings")
        self.assertNotEqual(rc, tbp.EXIT_CANNOT_RUN)

    def test_report_records_the_mode_so_weak_evidence_is_visible(self) -> None:
        rc, data = self._run(MCP_SERVER_IMPORT="nonexistent_module_xyz:mcp",
                             BOOT_TRANSPORTS="stdio")
        self.assertEqual(rc, tbp.EXIT_FINDINGS)
        self.assertEqual(data["transports"][0]["mode"], "generic")

    def test_render_flags_generic_http_as_weaker_evidence(self) -> None:
        results = [tbp.ProbeResult(tbp.STREAMABLE_HTTP, "generic", True, "ok")]
        text = tbp.render(results, tbp.Derivation(transports=[tbp.STREAMABLE_HTTP]))
        self.assertIn("generic", text)
        self.assertIn("cannot fully exercise", text)

    def test_render_does_not_nag_when_the_entrypoint_was_used(self) -> None:
        results = [tbp.ProbeResult(tbp.STREAMABLE_HTTP, "entrypoint", True, "ok")]
        text = tbp.render(results, tbp.Derivation(transports=[tbp.STREAMABLE_HTTP]))
        self.assertNotIn("cannot fully exercise", text)


# ---------------------------------------------------------------------------
# the real thing (needs fastmcp)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAVE_FASTMCP, "fastmcp not installed (uv run --with fastmcp to enable)")
class FastMCPBootTest(unittest.TestCase):
    """The stdlib fixtures prove the probe's logic; this proves it against a real
    FastMCP server, so a change in the SDK's startup or transport handling shows up
    here rather than in a target repo at 03:00."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = _fixture_env(MCP_SERVER_IMPORT="smoke_server:mcp")
        cls.plan_stdio = tbp.LaunchPlan(
            tbp.STDIO, "generic", [sys.executable, "-c", tbp._GENERIC_LAUNCHER])
        cls.plan_http = tbp.LaunchPlan(
            tbp.STREAMABLE_HTTP, "generic", [sys.executable, "-c", tbp._GENERIC_LAUNCHER])

    def test_smoke_server_boots_over_stdio(self) -> None:
        res = tbp.probe_stdio(self.plan_stdio, timeout=60, cwd=FIXTURES, env=self.env)
        self.assertTrue(res.ok, msg=res.detail)
        self.assertIsNotNone(res.tools)
        self.assertGreaterEqual(res.tools or 0, 2)  # health + record_count

    def test_smoke_server_boots_over_streamable_http(self) -> None:
        # Bound to 0.0.0.0 like a real deployment and probed under a non-loopback
        # Host. If this ever goes red, the SDK's host-allow-list semantics changed
        # and the gate would start emitting false 421 findings against healthy
        # targets — which is precisely when we want to hear about it.
        res = tbp.probe_streamable_http(
            self.plan_http, timeout=60, cwd=FIXTURES,
            bind_host="0.0.0.0", probe_host="mcp-boot-probe.audit.invalid",
            env=self.env,
        )
        self.assertTrue(res.ok, msg=res.detail)


if __name__ == "__main__":
    unittest.main()
