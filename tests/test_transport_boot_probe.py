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
        self.assertEqual(
            sorted(d.floor_added), sorted([tbp.STDIO, tbp.STREAMABLE_HTTP])
        )

    def test_sse_is_derived_from_source_not_assumed(self) -> None:
        # SSE is only probed when the target still offers it — it is NOT in the
        # floor, so a target that dropped SSE is not held to a transport it retired.
        self._write(
            "server.py",
            """
            from fastmcp import FastMCP
            mcp = FastMCP("t")
            if __name__ == "__main__":
                mcp.run(transport="sse", host="0.0.0.0", port=8000)
        """,
        )
        d = tbp.derive(self.root, env={})
        self.assertIn(tbp.SSE, d.transports)
        self.assertNotIn(tbp.SSE, d.floor_added)
        self.assertTrue(any("server.py" in s for s in d.sources))

    def test_dockerfile_transport_flag_is_derived(self) -> None:
        self._write(
            "Dockerfile",
            """
            FROM python:3.12-slim
            CMD ["serve", "--transport", "http", "--host", "0.0.0.0"]
        """,
        )
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
        self.assertEqual(
            tbp.normalise_transport("streamable_http"), tbp.STREAMABLE_HTTP
        )
        self.assertEqual(tbp.normalise_transport("'sse'"), tbp.SSE)
        self.assertIsNone(tbp.normalise_transport("grpc"))

    @unittest.skipUnless(_HAVE_TOMLLIB, "tomllib requires Python 3.11+")
    def test_declared_commands_win_over_the_generic_launcher(self) -> None:
        # A target that declares how it is booted gets the faithful check: we run
        # ITS command, so its own startup code — where case 2 lives — is exercised.
        self._write(
            "pyproject.toml",
            """
            [project]
            name = "t"
            version = "0"

            [tool.mcp_auditor.boot.commands]
            "streamable-http" = ["serve", "--host", "{host}", "--port", "{port}"]
        """,
        )
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
        broken = tbp.probe_stdio(
            plan, timeout=20, cwd=FIXTURES, _close_stdin_early=True
        )
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
        plan = _plan(
            tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_http_server.py")]
        )
        return tbp.probe_streamable_http(
            plan,
            timeout=timeout,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="mcp-boot-probe.audit.invalid",
            paths=["/mcp/"],
            env=_fixture_env(BOOT_FIXTURE_MODE=mode),
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
        plan = _plan(
            tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_http_server.py")]
        )
        port = tbp.free_port()
        env = tbp.launch_env(
            plan, "127.0.0.1", port, _fixture_env(BOOT_FIXTURE_MODE="host421")
        )
        proc = subprocess.Popen(
            plan.argv,
            cwd=str(FIXTURES),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self.assertEqual(tbp.wait_for_port(port, time.monotonic() + 15, proc), "")
            loopback = tbp.http_post(
                port,
                "/mcp/",
                f"127.0.0.1:{port}",
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                timeout=10,
            )
            self.assertEqual(loopback.status, 200)  # looks perfectly healthy…
            hostile = tbp.http_post(
                port,
                "/mcp/",
                "real.example.org",
                {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
                timeout=10,
            )
            self.assertEqual(hostile.status, 421)  # …and is unusable
        finally:
            tbp._terminate(proc)

    def test_server_that_never_listens_is_a_finding(self) -> None:
        # Case 1 over HTTP: the process raises at start, so no port ever opens.
        plan = _plan(
            tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_stdio_crash.py")]
        )
        res = tbp.probe_streamable_http(
            plan,
            timeout=15,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="mcp-boot-probe.audit.invalid",
            paths=["/mcp/"],
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
        self.assertLess(
            elapsed, 25.0, msg=f"probe overran its deadline: {elapsed:.1f}s"
        )

    def test_sse_frames_are_parsed(self) -> None:
        payload = tbp._parse_sse_payload(
            'event: message\ndata: {"jsonrpc":"2.0","id":1}\n\n'
        )
        self.assertEqual(payload, {"jsonrpc": "2.0", "id": 1})


# ---------------------------------------------------------------------------
# "the gate never got to ASK" is not "the server does not come up"
# ---------------------------------------------------------------------------


class TransportSelectionTest(unittest.TestCase):
    """The false finding this branch removes.

    `zurich-opendata-mcp` selects HTTP with `--http`, not with the env vars the
    gate sets. Measured against the real target: the env-var invocation exits
    rc 0 without listening, while `--http --port N` serves happily. The gate
    reported "the server never came up" — a claim about a target whose HTTP
    transport is healthy. `boot_flag_transport_server.py` is that shape in
    miniature.
    """

    FLAG_SERVER = FIXTURES / "boot_flag_transport_server.py"

    def _probe(self, mode: str = "flag", timeout: float = 20.0) -> tbp.ProbeResult:
        plan = tbp.LaunchPlan(
            tbp.STREAMABLE_HTTP, "entrypoint", [sys.executable, str(self.FLAG_SERVER)]
        )
        return tbp.probe_streamable_http(
            plan,
            timeout=timeout,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="mcp-boot-probe.audit.invalid",
            paths=["/mcp"],
            env=_fixture_env(BOOT_FLAG_FIXTURE_MODE=mode),
        )

    def test_a_flag_selected_transport_is_found_and_reported_healthy(self) -> None:
        # The gate now tries `--http --port N` after the env vars come to nothing,
        # and the attempt is self-verifying: it only counts because the port then
        # opened and the server answered real MCP.
        res = self._probe("flag")
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.status, tbp.OK)
        self.assertEqual(res.tools, 2)

    def test_a_clean_exit_without_listening_is_not_a_finding(self) -> None:
        # The same fixture asked for SSE, which it does not serve under any
        # spelling: the bare call exits rc 0 (it ran "stdio" and finished) and
        # the `--sse` guesses are rejected by its argparse. The bare call is what
        # decides, so this is "we never got to ask", not "it does not come up".
        plan = tbp.LaunchPlan(
            tbp.SSE, "entrypoint", [sys.executable, str(self.FLAG_SERVER)]
        )
        res = tbp.probe_sse(
            plan,
            timeout=12,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="x.invalid",
            paths=["/sse"],
            env=_fixture_env(BOOT_FLAG_FIXTURE_MODE="flag"),
        )
        self.assertEqual(res.status, tbp.NOT_SELECTED)
        self.assertFalse(res.ok)  # not a pass either
        self.assertEqual(res.evidence.get("case"), "transport-not-selected")
        self.assertIn("says NOTHING about whether the transport works", res.detail)
        self.assertIn("tool.mcp_auditor.boot.commands", res.detail)

    def test_a_rejected_guess_does_not_become_a_finding(self) -> None:
        # The guessed `--sse` flags make the fixture's argparse exit NON-zero.
        # If a variant's failure could set the verdict, the case above would come
        # back as "the server does not come up" — swapping one false finding for
        # another. This pins that the guess cannot vote.
        plan = tbp.LaunchPlan(
            tbp.SSE, "entrypoint", [sys.executable, str(self.FLAG_SERVER)]
        )
        variants = tbp.argv_variants(plan, "127.0.0.1", 9000)
        self.assertGreater(len(variants), 1, "the SSE guesses should be tried")
        rc = subprocess.run(
            variants[1],
            cwd=str(FIXTURES),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        ).returncode
        self.assertNotEqual(rc, 0, "the guessed flag really is rejected here")

    def test_a_crash_at_start_is_still_a_finding(self) -> None:
        # The discriminator must not swallow case 1. A server that TRIED and died
        # leaves a non-zero status, and that stays a finding about the target.
        res = self._probe("crash", timeout=12)
        self.assertEqual(res.status, tbp.FAIL)
        self.assertIn("never came up", res.detail)

    def test_a_declared_argv_is_never_extended_with_guesses(self) -> None:
        # The target told us exactly how it wants to be started. Appending flags
        # to that would override an explicit instruction with a guess.
        plan = tbp.LaunchPlan(
            tbp.STREAMABLE_HTTP, "declared", ["serve", "--port", "{port}"]
        )
        self.assertEqual(
            tbp.argv_variants(plan, "0.0.0.0", 9000), [["serve", "--port", "9000"]]
        )

    def test_stdio_gets_no_transport_flags(self) -> None:
        plan = tbp.LaunchPlan(tbp.STDIO, "entrypoint", ["srv"])
        self.assertEqual(tbp.argv_variants(plan, "0.0.0.0", 9000), [["srv"]])

    def test_the_bare_invocation_comes_first(self) -> None:
        # A target that DOES read the environment must not be handed a flag it
        # might reject before it ever gets its chance.
        variants = tbp.argv_variants(
            tbp.LaunchPlan(tbp.STREAMABLE_HTTP, "entrypoint", ["srv"]), "0.0.0.0", 9000
        )
        self.assertEqual(variants[0], ["srv"])
        self.assertIn(["srv", "--http", "--port", "9000"], variants)

    def test_only_the_first_attempt_decides_the_verdict(self) -> None:
        # A guessed flag that makes an unrelated binary exit non-zero (argparse:
        # "unrecognized arguments") must not turn into a boot failure. `true`
        # exits 0 and never listens -> not-selected, despite later variants
        # failing differently.
        plan = tbp.LaunchPlan(tbp.STREAMABLE_HTTP, "entrypoint", ["/bin/true"])
        res = tbp.probe_streamable_http(
            plan,
            timeout=10,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="x.invalid",
            paths=["/mcp"],
            env=_fixture_env(),
        )
        self.assertEqual(res.status, tbp.NOT_SELECTED)

    def test_a_first_attempt_that_dies_is_a_failure(self) -> None:
        plan = tbp.LaunchPlan(tbp.STREAMABLE_HTTP, "entrypoint", ["/bin/false"])
        res = tbp.probe_streamable_http(
            plan,
            timeout=10,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="x.invalid",
            paths=["/mcp"],
            env=_fixture_env(),
        )
        self.assertEqual(res.status, tbp.FAIL)


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
        os.environ.update(
            {
                "BOOT_TARGET_ROOT": str(self.root),
                "BOOT_REPORT": str(report),
                "BOOT_TIMEOUT": "15",
                **env,
            }
        )
        # The probe narrates to stdout/stderr for the operator reading the Worker
        # log; keep that out of the test output.
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = tbp.main()
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return rc, data

    def test_a_target_that_cannot_be_booted_is_a_finding_not_hard_fail(self) -> None:
        # Nothing importable in an empty dir, so the generic launcher fails for
        # both floor transports. That is a statement about the TARGET -> exit 2.
        rc, data = self._run(
            MCP_SERVER_IMPORT="nonexistent_module_xyz:mcp", BOOT_TRANSPORTS="stdio"
        )
        self.assertEqual(rc, tbp.EXIT_FINDINGS)
        self.assertEqual(data.get("outcome"), "findings")
        self.assertNotEqual(rc, tbp.EXIT_CANNOT_RUN)

    def test_report_records_the_mode_so_weak_evidence_is_visible(self) -> None:
        rc, data = self._run(
            MCP_SERVER_IMPORT="nonexistent_module_xyz:mcp", BOOT_TRANSPORTS="stdio"
        )
        self.assertEqual(rc, tbp.EXIT_FINDINGS)
        self.assertEqual(data["transports"][0]["mode"], "generic")

    def test_an_unselected_transport_exits_three_not_two(self) -> None:
        # The gate's own contract for the new state, end to end through main().
        root = self.root
        (root / "pyproject.toml").write_text(
            '[project]\nname = "t"\nversion = "0"\n\n'
            "[tool.mcp_auditor.boot.commands]\n"
            f'"streamable-http" = ["{sys.executable}", '
            f'"{FIXTURES / "boot_flag_transport_server.py"}"]\n',
            encoding="utf-8",
        )
        rc, data = self._run(BOOT_TRANSPORTS="streamable-http", BOOT_TIMEOUT="12")
        # A declared argv is never extended with guesses, so `--http` is not sent
        # and the fixture exits cleanly without listening.
        self.assertEqual(rc, tbp.EXIT_NOT_MEASURED)
        self.assertEqual(data["outcome"], "not-measured")
        self.assertEqual(data["transports"][0]["status"], tbp.NOT_SELECTED)

    def test_a_real_failure_outranks_an_unselected_transport(self) -> None:
        # If anything genuinely did not come up, that is the finding — whatever
        # we could not manage to ask of another transport.
        results = [
            tbp.ProbeResult(tbp.STDIO, "entrypoint", False, "crashed", status=tbp.FAIL),
            tbp.ProbeResult(
                tbp.STREAMABLE_HTTP, "entrypoint", False, "n/a", status=tbp.NOT_SELECTED
            ),
        ]
        failed = [r for r in results if r.status == tbp.FAIL]
        unselected = [r for r in results if r.status == tbp.NOT_SELECTED]
        self.assertTrue(failed and unselected)
        self.assertEqual(
            tbp.EXIT_FINDINGS if failed else tbp.EXIT_NOT_MEASURED, tbp.EXIT_FINDINGS
        )

    def test_render_names_the_fix_for_an_unselected_transport(self) -> None:
        text = tbp.render(
            [
                tbp.ProbeResult(
                    tbp.STREAMABLE_HTTP,
                    "entrypoint",
                    False,
                    "n/a",
                    status=tbp.NOT_SELECTED,
                )
            ],
            tbp.Derivation(transports=[tbp.STREAMABLE_HTTP]),
        )
        self.assertIn("NOT a statement about the target", text)
        self.assertIn("[tool.mcp_auditor.boot.commands]", text)

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


@unittest.skipUnless(
    _HAVE_FASTMCP, "fastmcp not installed (uv run --with fastmcp to enable)"
)
class FastMCPBootTest(unittest.TestCase):
    """The stdlib fixtures prove the probe's logic; this proves it against a real
    FastMCP server, so a change in the SDK's startup or transport handling shows up
    here rather than in a target repo at 03:00."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.env = _fixture_env(MCP_SERVER_IMPORT="smoke_server:mcp")
        cls.plan_stdio = tbp.LaunchPlan(
            tbp.STDIO, "generic", [sys.executable, "-c", tbp._GENERIC_LAUNCHER]
        )
        cls.plan_http = tbp.LaunchPlan(
            tbp.STREAMABLE_HTTP,
            "generic",
            [sys.executable, "-c", tbp._GENERIC_LAUNCHER],
        )

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
            self.plan_http,
            timeout=60,
            cwd=FIXTURES,
            bind_host="0.0.0.0",
            probe_host="mcp-boot-probe.audit.invalid",
            env=self.env,
        )
        self.assertTrue(res.ok, msg=res.detail)


# ---------------------------------------------------------------------------
# spec 2026-07-28 — a refused handshake is not a broken server
# ---------------------------------------------------------------------------


class StatelessCoreTest(unittest.TestCase):
    """The false finding this branch exists to prevent.

    The gate opened every probe with ``initialize``. Spec 2026-07-28 removes the
    method, so a MIGRATED server answered -32601, the gate reported "the server
    never came up", and exit 2 travelled through nightly_audit_report.py into
    sync_findings_issues.py as a GitHub issue. The first server to finish the
    migration would have been issued a bug report for finishing it.

    The three modes of the fixture are the three cases that must stay apart:
    migrated (pass), genuinely broken (fail), and refused-handshake-but-also-
    broken (fail). If the middle one ever passes, the branch has turned a real
    gate into a blanket excuse.
    """

    def _probe(self, mode: str, timeout: float = 20.0) -> tbp.ProbeResult:
        plan = _plan(
            tbp.STREAMABLE_HTTP,
            [sys.executable, str(FIXTURES / "stateless_http_server.py")],
        )
        return tbp.probe_streamable_http(
            plan,
            timeout=timeout,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="mcp-boot-probe.audit.invalid",
            paths=["/mcp/"],
            env=_fixture_env(BOOT_FIXTURE_MODE=mode),
        )

    def test_a_migrated_server_is_a_pass_not_a_finding(self) -> None:
        res = self._probe("stateless")
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.status, tbp.STATELESS)
        self.assertEqual(res.tools, 3)
        self.assertTrue(res.evidence.get("stateless"))
        self.assertIn("no handshake", res.detail)

    def test_a_migrated_server_issues_no_session_id(self) -> None:
        # The other half of the stateless core. A server that still handed one out
        # would be a LEGACY_TRANSPORT signal, and spec_probe.py reads this field.
        res = self._probe("stateless")
        self.assertFalse(res.evidence.get("session_id_issued"))

    def test_a_genuinely_broken_server_still_fails(self) -> None:
        # -32603 is an internal error, not a removed method. If this passes, the
        # stateless branch has stopped being a discriminator and started being an
        # excuse — which is worse than the false finding it replaced.
        res = self._probe("broken")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, tbp.FAIL)

    def test_a_refused_handshake_alone_does_not_earn_a_pass(self) -> None:
        # initialize is refused like a migrated server, but the handshake-free
        # call fails too. Neither migrated nor healthy.
        res = self._probe("halfway")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, tbp.FAIL)

    def test_the_discriminator_is_the_error_code_not_the_failure(self) -> None:
        self.assertTrue(tbp._handshake_refused({"error": {"code": -32601}}))
        self.assertTrue(
            tbp._handshake_refused(
                {"error": {"message": "Method not found: initialize"}}
            )
        )
        self.assertFalse(tbp._handshake_refused({"error": {"code": -32603}}))
        self.assertFalse(tbp._handshake_refused({"result": {}}))

    def test_the_negotiated_version_is_read_back(self) -> None:
        # The measurement that was arriving on every successful boot and being
        # discarded: only result.tools was ever looked at.
        self.assertEqual(
            tbp.negotiated_version({"result": {"protocolVersion": "2025-06-18"}}),
            "2025-06-18",
        )
        self.assertEqual(tbp.negotiated_version({"result": {}}), "")
        self.assertEqual(tbp.negotiated_version({"error": {}}), "")

    def test_a_legacy_server_still_reports_its_negotiated_version(self) -> None:
        plan = _plan(
            tbp.STREAMABLE_HTTP, [sys.executable, str(FIXTURES / "boot_http_server.py")]
        )
        res = tbp.probe_streamable_http(
            plan,
            timeout=20.0,
            cwd=FIXTURES,
            bind_host="127.0.0.1",
            probe_host="mcp-boot-probe.audit.invalid",
            paths=["/mcp/"],
            env=_fixture_env(BOOT_FIXTURE_MODE="ok"),
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.status, tbp.OK)
        self.assertEqual(res.evidence.get("negotiated_protocol_version"), "2025-06-18")

    def test_the_sent_version_is_overridable(self) -> None:
        # It used to be a bare literal — the exact class identity_probe exists to
        # catch, in the auditor's own source.
        self.assertEqual(
            tbp._initialize_params()["protocolVersion"], tbp._PROTOCOL_VERSION
        )
        self.assertTrue(tbp.DEFAULT_PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
