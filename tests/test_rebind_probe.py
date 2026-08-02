#!/usr/bin/env python3
"""Tests for scripts/rebind_probe.py — the DNS-rebinding gate.

The gate's whole claim is that it can tell four things apart that look alike
from the outside:

  * an allow-list that is actually in force,
  * a fallback policy that refused an attacker's hostname by accident,
  * a target that never shipped the control (fail-open, and not a bug),
  * a target that shipped it and lets a valid token walk past it.

So the fixture (``tests/fixtures/rebind_http_server.py``) implements all four,
and the tests below are built to fail if the gate ever collapses any two of them
into the same answer. Two carry the design:

  ``test_a_foreign_host_probe_alone_would_have_been_fooled`` — the
  ``loopback_only`` server refuses ``rebind.attacker.probe.invalid`` exactly as
  convincingly as a correct one, and would have been called protected by a gate
  that probed only that.

  ``test_only_the_wrong_port_probe_separates_a_loose_list_from_a_strict_one`` —
  against ``hostname_only`` probes 1, 3 and 4 behave *identically* to the healthy
  server. Only the wrong-port probe differs, which is why it is in the matrix.

Everything is stdlib-only and offline. ``FastMCPRebindTest`` at the bottom needs
fastmcp and self-skips without it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rebind_probe as rp  # noqa: E402
import transport_boot_probe as tbp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SERVER = FIXTURES / "rebind_http_server.py"

ALLOWED = "mcp-audit-allowed.probe.invalid"
FOREIGN = "rebind.attacker.probe.invalid"
TOKEN = "s3cr3t-probe-token"

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


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    # Never let the outer environment's own values reach the fixture: they would
    # silently replace the allow-list the gate is supposed to be configuring.
    for name in rp.ALLOWLIST_ENV + rp.ORIGIN_ENV + rp.AUTH_ENV:
        env.pop(name, None)
    env.update(extra)
    return env


def _plan(transport: str = tbp.STREAMABLE_HTTP) -> tbp.LaunchPlan:
    return tbp.LaunchPlan(
        transport=transport, mode="declared", argv=[sys.executable, str(SERVER)]
    )


def _probe(
    mode: str,
    transport: str = tbp.STREAMABLE_HTTP,
    timeout: float = 25.0,
    **fixture_env: str,
) -> rp.TransportResult:
    paths = ["/sse/"] if transport == tbp.SSE else ["/mcp/"]
    env = _env(
        REBIND_FIXTURE_MODE=mode,
        REBIND_FIXTURE_TRANSPORT="sse" if transport == tbp.SSE else "http",
        **fixture_env,
    )
    return rp.probe_transport(
        _plan(transport),
        timeout=timeout,
        cwd=FIXTURES,
        bind_host="127.0.0.1",
        allowed_host=ALLOWED,
        foreign_host=FOREIGN,
        paths=paths,
        token=TOKEN,
        env=env,
    )


def _cases(result: rp.TransportResult, label: str) -> dict[str, rp.CaseResult]:
    for p in result.passes:
        if p.label == label:
            return {c.case.name: c for c in p.cases}
    raise AssertionError(f"no {label} pass in {result.as_dict()}")


def _verdict(result: rp.TransportResult, label: str) -> str:
    return next(p.verdict for p in result.passes if p.label == label)


# ---------------------------------------------------------------------------
# the probe matrix itself
# ---------------------------------------------------------------------------


class ProbeMatrixTest(unittest.TestCase):
    def test_the_wrong_port_case_differs_from_the_allowed_case_only_in_the_port(
        self,
    ) -> None:
        # The pair is the evidence. If a change ever makes these two differ in
        # hostname as well, the matrix stops discriminating and this test says so.
        cases = {c.name: c for c in rp.build_cases(ALLOWED, FOREIGN, 8000)}
        self.assertEqual(cases["wrong-port"].host, f"{ALLOWED}:8001")
        self.assertEqual(cases["allowed"].host, f"{ALLOWED}:8000")
        self.assertEqual(cases["wrong-port"].expect, "reject")
        self.assertEqual(cases["allowed"].expect, "accept")

    def test_the_wrong_port_stays_a_valid_port_at_the_top_of_the_range(self) -> None:
        cases = {c.name: c for c in rp.build_cases(ALLOWED, FOREIGN, 65535)}
        self.assertEqual(cases["wrong-port"].host, f"{ALLOWED}:65534")

    def test_the_configured_list_keeps_loopback_and_is_port_exact(self) -> None:
        # Loopback is not padding: the operator keeps it for health checks and the
        # harness needs it to find the endpoint. It carries the port like the rest,
        # so a loosely-compared list still fails the wrong-port probe.
        value = rp.allowlist_value(ALLOWED, 9000)
        self.assertEqual(value, f"127.0.0.1:9000,localhost:9000,{ALLOWED}:9000")

    def test_the_auth_variables_are_cleared_when_no_token_is_configured(self) -> None:
        # A token inherited from the surrounding environment would make the
        # token-less pass a second copy of the token pass, and the comparison
        # between them is the whole evidence for auth-independence.
        base = {name: "leftover" for name in rp.AUTH_ENV}
        env = rp.pass_env(base, ALLOWED, 9000, token="")
        for name in rp.AUTH_ENV:
            self.assertNotIn(name, env)
        self.assertEqual(env["MCP_ALLOWED_HOSTS"], rp.allowlist_value(ALLOWED, 9000))


# ---------------------------------------------------------------------------
# a server that gets it right
# ---------------------------------------------------------------------------


class EnforcedTest(unittest.TestCase):
    def test_a_correct_allowlist_is_reported_as_enforced(self) -> None:
        res = _probe("allowlist")
        self.assertEqual(res.verdict, rp.ENFORCED, msg=res.detail)
        self.assertEqual(_verdict(res, rp.PASS_NO_TOKEN), rp.ENFORCED)
        self.assertEqual(_verdict(res, rp.PASS_VALID_TOKEN), rp.ENFORCED)

    def test_a_valid_token_does_not_rescue_a_foreign_host(self) -> None:
        # The second load-bearing probe. The attacking page runs in a context that
        # holds a token, so a control that only holds for anonymous requests is no
        # control at all.
        res = _probe("allowlist")
        cases = _cases(res, rp.PASS_VALID_TOKEN)
        self.assertEqual(cases["foreign-host"].status, 421)
        self.assertTrue(cases["foreign-host"].matched)
        self.assertTrue(cases["allowed"].matched)  # the same token IS accepted

    def test_the_token_pass_verifies_that_auth_was_enforced_at_all(self) -> None:
        res = _probe("allowlist")
        token_pass = next(p for p in res.passes if p.label == rp.PASS_VALID_TOKEN)
        self.assertEqual(token_pass.auth_enforced, "yes")

    def test_a_target_that_ignores_the_token_makes_the_pass_weaker_not_wrong(
        self,
    ) -> None:
        # The host check still holds, so this is still enforced — but the report
        # must not claim the control beat an auth layer the target never ran.
        res = _probe("allowlist", REBIND_FIXTURE_IGNORE_AUTH="1")
        self.assertEqual(res.verdict, rp.ENFORCED, msg=res.detail)
        token_pass = next(p for p in res.passes if p.label == rp.PASS_VALID_TOKEN)
        self.assertEqual(token_pass.auth_enforced, "no")
        self.assertIn("WRONG token", res.detail)

    def test_both_passes_probe_the_same_endpoint(self) -> None:
        res = _probe("allowlist")
        paths = {p.path for p in res.passes}
        self.assertEqual(len(paths), 1, msg=f"passes diverged on the endpoint: {paths}")


# ---------------------------------------------------------------------------
# the discriminations — the reason the matrix has four rows and not one
# ---------------------------------------------------------------------------


class DiscriminationTest(unittest.TestCase):
    def test_a_foreign_host_probe_alone_would_have_been_fooled(self) -> None:
        # `loopback_only` reads no configuration at all and simply refuses every
        # non-loopback name. It therefore rejects the attacker's hostname exactly
        # as a correct server does — a gate that probed only that would report the
        # control as working while nothing the operator configured is in force.
        res = _probe("loopback_only")
        cases = _cases(res, rp.PASS_NO_TOKEN)
        self.assertTrue(cases["foreign-host"].matched)  # looks protected…
        self.assertEqual(cases["foreign-host"].status, 421)
        self.assertFalse(cases["allowed"].matched)  # …and is not measurable
        self.assertEqual(res.verdict, rp.INCONCLUSIVE)
        self.assertIn("was itself refused", res.detail)

    def test_only_the_wrong_port_probe_separates_a_loose_list_from_a_strict_one(
        self,
    ) -> None:
        # `hostname_only` honours the variable but drops the port. Against it,
        # probes 1, 3 and 4 answer EXACTLY as the healthy server does; the whole
        # difference is the wrong-port probe. This is the test that would fail if
        # that probe were ever dropped as redundant.
        loose = _cases(_probe("hostname_only"), rp.PASS_NO_TOKEN)
        strict = _cases(_probe("allowlist"), rp.PASS_NO_TOKEN)
        for name in ("foreign-host", "foreign-origin", "allowed"):
            self.assertEqual(
                loose[name].matched,
                strict[name].matched,
                msg=f"{name} differed, so it — not the port — carries the check",
            )
        self.assertTrue(strict["wrong-port"].matched)
        self.assertFalse(loose["wrong-port"].matched)

    def test_a_loose_list_is_a_control_applied_wrongly_not_one_that_is_absent(
        self,
    ) -> None:
        # It refused the foreign host, so our allow-list demonstrably reached the
        # transport. A target that merely never switched the control on cannot
        # produce that mix — so this is a defect, and the text scan does not get
        # a vote.
        res = _probe("hostname_only")
        self.assertEqual(res.verdict, rp.NOT_ENFORCED, msg=res.detail)
        self.assertIn("wrong-port", res.detail)
        self.assertEqual(res.evidence.get("case"), "partial-enforcement")
        outcome, code, _ = rp.classify([res], rp.Knob(advertised=False))
        self.assertEqual(outcome, rp.OUT_FINDINGS)
        self.assertEqual(code, rp.EXIT_FINDINGS)

    def test_an_unchecked_origin_is_caught(self) -> None:
        res = _probe("no_origin")
        self.assertEqual(res.verdict, rp.NOT_ENFORCED, msg=res.detail)
        self.assertIn("foreign-origin", res.detail)
        self.assertEqual(res.evidence.get("case"), "partial-enforcement")

    def test_a_token_that_short_circuits_the_host_check_is_named_as_such(self) -> None:
        # The control holds for anonymous requests and folds the moment a valid
        # token appears. That is authentication wearing the control's name.
        res = _probe("auth_first")
        self.assertEqual(_verdict(res, rp.PASS_NO_TOKEN), rp.ENFORCED)
        self.assertEqual(_verdict(res, rp.PASS_VALID_TOKEN), rp.NOT_ENFORCED)
        self.assertEqual(res.verdict, rp.NOT_ENFORCED)
        self.assertEqual(res.evidence.get("case"), "token-bypasses-host-check")

    def test_a_server_with_no_check_at_all_lets_everything_through(self) -> None:
        # All three served alike — the one shape with nothing observable to
        # distinguish "absent" from "broken", so this is where the target's own
        # tree gets to decide.
        res = _probe("ignores")
        self.assertEqual(res.verdict, rp.NOT_ENFORCED, msg=res.detail)
        self.assertEqual(res.evidence.get("case"), "host-check-absent")
        cases = _cases(res, rp.PASS_NO_TOKEN)
        for name in ("foreign-host", "wrong-port", "foreign-origin"):
            self.assertFalse(
                cases[name].matched, msg=f"{name} was refused unexpectedly"
            )
        self.assertTrue(cases["allowed"].matched)


# ---------------------------------------------------------------------------
# SSE — the other network transport
# ---------------------------------------------------------------------------


class SseTest(unittest.TestCase):
    def test_the_allowlist_is_enforced_on_the_sse_handshake_too(self) -> None:
        res = _probe("allowlist", transport=tbp.SSE)
        self.assertEqual(res.verdict, rp.ENFORCED, msg=res.detail)

    def test_an_unprotected_sse_stream_does_not_hang_the_gate(self) -> None:
        # A server with no allow-list answers a hostile GET with an endless event
        # stream. Reading it to the end would hang the gate on precisely the case
        # it is measuring — the swiss-transport-mcp#25 suite hit exactly this.
        started = time.monotonic()
        res = _probe(
            "ignores", transport=tbp.SSE, timeout=12.0, REBIND_FIXTURE_SSE_ENDLESS="1"
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 90.0, msg=f"the gate overran: {elapsed:.1f}s")
        self.assertEqual(res.verdict, rp.NOT_ENFORCED, msg=res.detail)


# ---------------------------------------------------------------------------
# "not configured" is its own category
# ---------------------------------------------------------------------------


class KnobDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")

    def test_a_target_that_names_the_variable_advertises_the_knob(self) -> None:
        self._write(".env.example", "MCP_ALLOWED_HOSTS=mcp.example.org:8000\n")
        knob = rp.detect_knob(self.root)
        self.assertTrue(knob.advertised)
        # Exactly the one it names. ALLOWED_HOSTS is a substring of
        # MCP_ALLOWED_HOSTS, and the report's job is to tell the operator which
        # variable to set — not to list every knob whose name happens to fit.
        self.assertEqual(knob.names, ["MCP_ALLOWED_HOSTS"])
        self.assertTrue(any(".env.example" in s for s in knob.sources))

    def test_the_bare_variable_is_still_found_on_its_own(self) -> None:
        self._write(
            "docker-compose.yaml",
            "    environment:\n      ALLOWED_HOSTS: mcp.example.org:8000\n",
        )
        self.assertEqual(rp.detect_knob(self.root).names, ["ALLOWED_HOSTS"])

    def test_a_target_that_never_heard_of_it_does_not(self) -> None:
        self._write("server.py", 'mcp.run(transport="http")')
        self.assertFalse(rp.detect_knob(self.root).advertised)


class ClassificationTest(unittest.TestCase):
    """The three-way outcome. Same observation, two different verdicts — the
    difference must come from what the TARGET advertises, never from us."""

    @staticmethod
    def _result(verdict: str, case: str = "") -> rp.TransportResult:
        return rp.TransportResult(
            tbp.STREAMABLE_HTTP,
            "declared",
            verdict,
            "detail",
            evidence={"case": case} if case else {},
        )

    def test_a_fail_open_target_without_the_knob_is_not_configured(self) -> None:
        outcome, code, reasons = rp.classify(
            [self._result(rp.NOT_ENFORCED, "host-check-absent")],
            rp.Knob(advertised=False),
        )
        self.assertEqual(outcome, rp.OUT_NOT_CONFIGURED)
        self.assertEqual(code, rp.EXIT_NOT_CONFIGURED)
        self.assertNotEqual(code, rp.EXIT_GREEN)  # not a pass
        self.assertNotEqual(code, rp.EXIT_FINDINGS)  # and not a finding
        self.assertTrue(any("fail-open" in r for r in reasons))

    def test_the_same_observation_with_the_knob_shipped_is_a_finding(self) -> None:
        knob = rp.Knob(advertised=True, names=["MCP_ALLOWED_HOSTS"])
        outcome, code, reasons = rp.classify(
            [self._result(rp.NOT_ENFORCED, "host-check-absent")], knob
        )
        self.assertEqual(outcome, rp.OUT_FINDINGS)
        self.assertEqual(code, rp.EXIT_FINDINGS)
        self.assertTrue(any("MCP_ALLOWED_HOSTS" in r for r in reasons))

    def test_a_control_that_is_present_and_wrong_is_a_finding_without_the_knob(
        self,
    ) -> None:
        # Two shapes that prove the target HAS the control: one that works until a
        # token shows up, and one that refuses some hostile probes and serves
        # others. "Not configured" is the wrong shelf for either, and the text scan
        # must not be able to move them onto it.
        for case in ("token-bypasses-host-check", "partial-enforcement"):
            with self.subTest(case=case):
                outcome, code, _ = rp.classify(
                    [self._result(rp.NOT_ENFORCED, case)], rp.Knob(advertised=False)
                )
                self.assertEqual(outcome, rp.OUT_FINDINGS)
                self.assertEqual(code, rp.EXIT_FINDINGS)

    def test_an_unattributable_result_fails_closed(self) -> None:
        outcome, code, _ = rp.classify([self._result(rp.INCONCLUSIVE)], rp.Knob())
        self.assertEqual(outcome, rp.OUT_FINDINGS)
        self.assertEqual(code, rp.EXIT_FINDINGS)

    def test_no_network_transport_means_there_is_no_surface(self) -> None:
        outcome, code, reasons = rp.classify([], rp.Knob())
        self.assertEqual(outcome, rp.OUT_NOT_APPLICABLE)
        self.assertEqual(code, rp.EXIT_GREEN)
        self.assertTrue(any("no network transport" in r for r in reasons))

    def test_the_not_configured_report_says_the_attack_is_unopposed(self) -> None:
        text = rp.render(
            [self._result(rp.NOT_ENFORCED, "host-check-absent")],
            rp.Knob(advertised=False),
            rp.OUT_NOT_CONFIGURED,
            ["nothing was configured to enforce"],
        )
        self.assertIn("NOT CONFIGURED", text)
        self.assertIn("unopposed", text)
        self.assertNotIn("✅", text)


# ---------------------------------------------------------------------------
# the gate's exit contract, end to end
# ---------------------------------------------------------------------------


@unittest.skipUnless(_HAVE_TOMLLIB, "tomllib requires Python 3.11+")
class ExitContractTest(unittest.TestCase):
    """0 enforced / 2 finding / 3 not configured / 127 harness — driven through
    main() against the real fixture, because the exit code is the whole contract
    with nightly-audit.sh and the Broker."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = dict(os.environ)
        # A declared boot table is the faithful launch mode: the gate runs the
        # target's OWN command, exactly as it does against a real target.
        (self.root / "pyproject.toml").write_text(
            textwrap.dedent(f'''
            [project]
            name = "t"
            version = "0"

            [tool.mcp_auditor.boot.commands]
            "streamable-http" = ["{sys.executable}", "{SERVER}"]
        '''),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def _run(self, **env: str) -> tuple[int, dict]:
        report = self.root / "rebind.json"
        for name in rp.ALLOWLIST_ENV + rp.ORIGIN_ENV + rp.AUTH_ENV:
            os.environ.pop(name, None)
        os.environ.update(
            {
                "BOOT_TARGET_ROOT": str(self.root),
                "BOOT_TRANSPORTS": "streamable-http",
                "REBIND_REPORT": str(report),
                "REBIND_TIMEOUT": "25",
                "REBIND_BIND_HOST": "127.0.0.1",
                "REBIND_ALLOWED_HOST": ALLOWED,
                "REBIND_FOREIGN_HOST": FOREIGN,
                "REBIND_AUTH_TOKEN": TOKEN,
                "REBIND_HTTP_PATHS": "/mcp/",
                "REBIND_FIXTURE_TRANSPORT": "http",
                **env,
            }
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = rp.main()
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return rc, data

    def test_enforced_exits_green(self) -> None:
        rc, data = self._run(REBIND_FIXTURE_MODE="allowlist")
        self.assertEqual(
            rc, rp.EXIT_GREEN, msg=json.dumps(data.get("reasons"), indent=2)
        )
        self.assertEqual(data["outcome"], rp.OUT_ENFORCED)

    def test_fail_open_without_the_knob_exits_three(self) -> None:
        rc, data = self._run(REBIND_FIXTURE_MODE="ignores")
        self.assertEqual(rc, rp.EXIT_NOT_CONFIGURED)
        self.assertEqual(data["outcome"], rp.OUT_NOT_CONFIGURED)
        self.assertFalse(data["knob"]["advertised"])

    def test_the_same_target_with_the_knob_shipped_exits_two(self) -> None:
        # Identical server behaviour; the only change is that the target's own
        # tree now names the variable. That is what turns fail-open into a defect.
        (self.root / ".env.example").write_text(
            "MCP_ALLOWED_HOSTS=mcp.example.org:8000\n", encoding="utf-8"
        )
        rc, data = self._run(REBIND_FIXTURE_MODE="ignores")
        self.assertEqual(rc, rp.EXIT_FINDINGS)
        self.assertEqual(data["outcome"], rp.OUT_FINDINGS)
        self.assertTrue(data["knob"]["advertised"])

    def test_a_loose_list_exits_two_even_though_the_target_names_nothing(self) -> None:
        # Same target tree as the exit-3 case above — no variable named anywhere.
        # The probes alone are enough here, because a server that never switched
        # the control on cannot refuse one hostile probe and serve another.
        rc, data = self._run(REBIND_FIXTURE_MODE="hostname_only")
        self.assertEqual(rc, rp.EXIT_FINDINGS)
        self.assertFalse(data["knob"]["advertised"])
        self.assertEqual(
            data["transports"][0]["evidence"]["case"], "partial-enforcement"
        )

    def test_a_token_bypass_exits_two(self) -> None:
        rc, data = self._run(REBIND_FIXTURE_MODE="auth_first")
        self.assertEqual(rc, rp.EXIT_FINDINGS)
        self.assertEqual(
            data["transports"][0]["evidence"]["case"], "token-bypasses-host-check"
        )

    def test_a_target_that_never_comes_up_is_a_finding_not_a_hard_fail(self) -> None:
        # Same rule as the boot gate: a statement about the target is exit 2. The
        # boot gate is where the *reason* is diagnosed; this gate only records
        # that it could not measure.
        rc, data = self._run(
            REBIND_FIXTURE_MODE="allowlist",
            REBIND_HTTP_PATHS="/nope/",
            REBIND_TIMEOUT="8",
        )
        self.assertEqual(rc, rp.EXIT_FINDINGS)
        self.assertNotEqual(rc, rp.EXIT_CANNOT_RUN)
        self.assertEqual(data["outcome"], rp.OUT_FINDINGS)

    def test_the_report_records_every_probe_so_the_verdict_can_be_rechecked(
        self,
    ) -> None:
        _, data = self._run(REBIND_FIXTURE_MODE="allowlist")
        cases = data["transports"][0]["passes"][0]["cases"]
        self.assertEqual(
            [c["name"] for c in cases],
            ["foreign-host", "wrong-port", "foreign-origin", "allowed"],
        )
        self.assertTrue(all("status" in c and "why" in c for c in cases))


# ---------------------------------------------------------------------------
# the real thing (needs fastmcp)
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    _HAVE_FASTMCP, "fastmcp not installed (uv run --with fastmcp to enable)"
)
class FastMCPRebindTest(unittest.TestCase):
    """A vanilla FastMCP server on a non-loopback bind is the fail-open case, and
    the gate must classify it as *not configured* rather than cry wolf — that is
    the single most common shape it will meet in the portfolio.

    If this ever flips to a finding or to enforced, the SDK changed its default
    transport-security posture, and the gate's categories need revisiting before
    the next nightly run does it for us against a real target at 03:00.
    """

    def test_a_vanilla_fastmcp_server_reports_as_not_configured(self) -> None:
        plan = tbp.LaunchPlan(
            tbp.STREAMABLE_HTTP,
            "generic",
            [sys.executable, "-c", tbp._GENERIC_LAUNCHER],
        )
        res = rp.probe_transport(
            plan,
            timeout=60,
            cwd=FIXTURES,
            bind_host="0.0.0.0",
            allowed_host=ALLOWED,
            foreign_host=FOREIGN,
            paths=["/mcp/", "/mcp"],
            token=TOKEN,
            env=_env(MCP_SERVER_IMPORT="smoke_server:mcp"),
        )
        self.assertEqual(res.verdict, rp.NOT_ENFORCED, msg=res.detail)
        outcome, code, _ = rp.classify([res], rp.Knob(advertised=False))
        self.assertEqual(outcome, rp.OUT_NOT_CONFIGURED)
        self.assertEqual(code, rp.EXIT_NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
