#!/usr/bin/env python3
"""Tests for scripts/spec_probe.py — which protocol version does this server speak?

The probe exists because the answer lived nowhere: the boot gate carried one
hand-maintained literal, sent it in every request, and discarded the version the
server named. So the tests are built around the four properties that make the
answer trustworthy rather than merely present:

* a source that could NOT be read is ``UNVERIFIED``, never «in sync» — the rule
  every probe in this repository shares, and the one the whole thing turns on;
* ``SPEC_UNDECLARED`` is a note and not a finding, because 39 of the 42 servers
  correctly declare nothing and a check that reddened all of them would be
  switched off within a day;
* ``LEGACY_TRANSPORT`` fires on measured evidence — an answering ``/sse``, an
  issued session id, a refused stateless call — and carries a date;
* the countdown is reproducible: ``--now`` pins it, because a report that says
  something different tomorrow about the same commit breaks the provenance
  promise from the other side.

Everything is offline. The wire tests replace ``spec_probe.request`` rather than
starting a server: what is under test is the CLASSIFICATION of a set of replies,
and a real socket would only add flakiness to it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import spec_probe as sp  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 8, 5)


def _reply(status: int = 200, payload: object = None, headers: dict | None = None):
    return sp.WireReply(status, headers or {}, json.dumps(payload or {}), payload)


def _rpc_ok(result: object) -> sp.WireReply:
    return _reply(200, {"jsonrpc": "2.0", "id": 1, "result": result})


def _rpc_err(code: int = -32601, status: int = 200) -> sp.WireReply:
    return _reply(status, {"jsonrpc": "2.0", "id": 1, "error": {"code": code}})


# ---------------------------------------------------------------------------
# the deprecation clock
# ---------------------------------------------------------------------------


class DeadlineTest(unittest.TestCase):
    def test_a_twelve_month_window_lands_a_year_later(self) -> None:
        self.assertEqual(sp.deadline("2026-07-28", 12), date(2027, 7, 28))

    def test_a_window_that_crosses_a_year_boundary(self) -> None:
        self.assertEqual(sp.deadline("2026-11-30", 6), date(2027, 5, 30))

    def test_the_countdown_is_days_not_a_boolean(self) -> None:
        # "deprecated" is not actionable; "357 days" is.
        text = sp.countdown(TODAY, date(2027, 7, 28))
        self.assertIn("357 day(s) left", text)
        self.assertIn("2027-07-28", text)

    def test_a_closed_window_says_so_instead_of_printing_a_negative(self) -> None:
        # A negative countdown reads as a bug and gets ignored, which is the one
        # outcome a deadline must not have.
        text = sp.countdown(date(2027, 8, 1), date(2027, 7, 28))
        self.assertIn("closed 4 day(s) ago", text)
        self.assertNotIn("-4", text)

    def test_the_date_is_pinnable_so_the_report_reproduces(self) -> None:
        report = sp.run(REPO, today=TODAY, until=date(2027, 7, 28))
        self.assertEqual(report.as_dict()["days_left"], 357)


# ---------------------------------------------------------------------------
# source 1 — the code
# ---------------------------------------------------------------------------


class CodeScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _src(self, body: str) -> Path:
        src = self.dir / "src" / "demo"
        src.mkdir(parents=True, exist_ok=True)
        (src / "server.py").write_text(textwrap.dedent(body), encoding="utf-8")
        return self.dir

    def test_a_declared_protocol_version_is_found(self) -> None:
        root = self._src('PROTOCOL_VERSION = "2026-07-28"\n')
        found = sp.scan_code(root)
        self.assertEqual(found.value, "2026-07-28")
        self.assertIn("src/demo/server.py:1", found.sites[0])

    def test_a_release_date_is_not_mistaken_for_a_protocol_version(self) -> None:
        # A date-shaped literal is only taken when the line is ABOUT a protocol
        # version. Without that rule every changelog entry becomes a source.
        root = self._src('RELEASED_ON = "2025-06-18"\nBUILD_DATE = "2024-11-05"\n')
        self.assertEqual(sp.scan_code(root).value, "")

    def test_a_comment_is_not_a_declaration(self) -> None:
        # Same rule and the same reason as identity_probe: a check that turns red
        # on documentation teaches people to delete the documentation.
        root = self._src('# protocolVersion was "2025-06-18" before the migration\n')
        self.assertEqual(sp.scan_code(root).value, "")

    def test_a_flat_layout_is_scanned_too(self) -> None:
        # Looking only in src/ would report a flat-layout server as declaring
        # nothing, which is a false statement rather than a missing one.
        (self.dir / "server.py").write_text(
            'MCP_PROTOCOL_VERSION = "2026-07-28"\n', encoding="utf-8"
        )
        self.assertEqual(sp.scan_code(self.dir).value, "2026-07-28")

    def test_vendored_trees_are_not_scanned(self) -> None:
        vendor = self.dir / ".venv" / "lib"
        vendor.mkdir(parents=True)
        (vendor / "x.py").write_text(
            'protocol_version = "2024-11-05"\n', encoding="utf-8"
        )
        self.assertEqual(sp.scan_code(self.dir).value, "")


# ---------------------------------------------------------------------------
# source 3 — portfolio.json
# ---------------------------------------------------------------------------


class PortfolioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "portfolio.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, data: object) -> Path:
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def test_the_tracked_version_is_read(self) -> None:
        path = self._write(
            {
                "servers": [
                    {
                        "name": "zurich-opendata-mcp",
                        "mcp_spec_version": "2025-06-18",
                        "migration_wave": "A",
                    }
                ]
            }
        )
        got = sp.scan_portfolio(path, "zurich-opendata-mcp")
        self.assertEqual(got["version"], "2025-06-18")
        self.assertEqual(got["wave"], "A")

    def test_a_server_matched_by_its_repo_slug(self) -> None:
        path = self._write(
            {
                "servers": [
                    {
                        "repo": "malkreide/bag-health-mcp",
                        "mcp_spec_version": "2026-07-28",
                    }
                ]
            }
        )
        self.assertEqual(
            sp.scan_portfolio(path, "bag-health-mcp")["version"], "2026-07-28"
        )

    def test_an_absent_server_is_unverified_not_agreement(self) -> None:
        # The whole point of the status. A tracker that never mentioned this
        # server has said nothing about it, and a blank must not read as a match.
        path = self._write(
            {"servers": [{"name": "other-mcp", "mcp_spec_version": "x"}]}
        )
        got = sp.scan_portfolio(path, "zurich-opendata-mcp")
        self.assertEqual(got["status"], sp.UNVERIFIED)
        self.assertIn("NOT compared", got["detail"])

    def test_an_unreadable_tracker_is_unverified(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(sp.scan_portfolio(self.path, "x")["status"], sp.UNVERIFIED)

    def test_a_tracked_server_without_the_field_is_undeclared(self) -> None:
        path = self._write({"servers": [{"name": "x-mcp", "migration_wave": "frozen"}]})
        self.assertEqual(sp.scan_portfolio(path, "x-mcp")["status"], sp.SPEC_UNDECLARED)


# ---------------------------------------------------------------------------
# source 4 — the wire
# ---------------------------------------------------------------------------


class WireTest(unittest.TestCase):
    """Replies are injected. What is under test is the reading of them."""

    def _wire(self, replies: list[sp.WireReply]) -> sp.WireResult:
        with unittest.mock.patch.object(sp, "request", side_effect=replies):
            return sp.probe_wire("https://example.invalid/mcp")

    def test_a_migrated_server_is_read_as_stateless(self) -> None:
        result = self._wire(
            [
                _rpc_ok({"tools": [{"name": "a"}]}),  # stateless, with headers
                _rpc_ok({"tools": [{"name": "a"}]}),  # stateless, without headers
                _rpc_err(-32601),  # initialize is gone
                _reply(404),  # no /sse
                _rpc_ok({"tools": []}),  # server/discover answers
            ]
        )
        self.assertTrue(result.reachable)
        self.assertTrue(result.stateless_ok)
        self.assertFalse(result.handshake_ok)
        self.assertEqual(result.session_id, "")
        self.assertEqual(result.sse_endpoint, "")

    def test_a_legacy_server_is_read_as_legacy(self) -> None:
        result = self._wire(
            [
                _rpc_err(-32600),  # a call without a handshake is refused
                _rpc_err(-32600),
                _reply(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "result": {"protocolVersion": "2025-06-18"},
                    },
                    {"mcp-session-id": "abc123def456"},
                ),
                sp.WireReply(
                    200, {"content-type": "text/event-stream"}, "data: {}", None
                ),
                _reply(404),
            ]
        )
        self.assertFalse(result.stateless_ok)
        self.assertTrue(result.handshake_ok)
        self.assertEqual(result.negotiated, "2025-06-18")
        self.assertEqual(result.session_id, "abc123def456")
        self.assertTrue(result.sse_endpoint.endswith("/sse"))

    def test_an_unreachable_endpoint_stops_and_says_so(self) -> None:
        # Not a finding and not a pass — the endpoint was never measured.
        result = self._wire([sp.WireReply(0, error="ConnectionRefusedError: nope")])
        self.assertFalse(result.reachable)
        self.assertIsNone(result.stateless_ok)
        self.assertIn("could not be reached", result.notes[0])

    def test_the_header_requirement_is_measured_not_assumed(self) -> None:
        # The brief says Mcp-Method/Mcp-Name are mandatory. This probe was not
        # written against the spec document, so it sends the call BOTH ways and
        # reports two status codes instead of asserting the rule.
        result = self._wire(
            [
                _rpc_ok({"tools": []}),  # with the headers: fine
                _reply(400),  # without them: rejected
                _rpc_err(-32601),
                _reply(404),
                _reply(404),
            ]
        )
        self.assertEqual(result.stateless_with_headers, 200)
        self.assertEqual(result.stateless_without_headers, 400)
        self.assertTrue(any("has not verified" in n for n in result.notes))

    def test_a_server_that_rejects_the_new_headers_is_reported_too(self) -> None:
        result = self._wire(
            [
                _reply(400),  # with the headers: rejected
                _rpc_ok({"tools": []}),  # without them: fine
                _rpc_err(-32601),
                _reply(404),
                _reply(404),
            ]
        )
        self.assertTrue(result.stateless_ok)
        self.assertTrue(any("headers it does not know" in n for n in result.notes))


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


class ClassifyTest(unittest.TestCase):
    def _report(self, **kw) -> sp.Report:
        report = sp.Report(target="/tmp/x", today=TODAY, until=date(2027, 7, 28), **kw)
        sp.classify(report)
        return report

    def test_two_disagreeing_sources_are_spec_drift(self) -> None:
        report = self._report(
            code=sp.Declared(versions=["2025-06-18"]),
            portfolio={"status": "ok", "version": "2026-07-28"},
        )
        codes = [f.code for f in report.findings]
        self.assertIn(sp.SPEC_DRIFT, codes)
        self.assertEqual(report.exit_code(), sp.EXIT_FINDINGS)

    def test_agreeing_sources_are_green(self) -> None:
        report = self._report(
            code=sp.Declared(versions=["2026-07-28"]),
            portfolio={"status": "ok", "version": "2026-07-28"},
        )
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), sp.EXIT_GREEN)

    def test_an_undeclared_source_is_a_note_not_a_finding(self) -> None:
        # 39 of 42 servers declare nothing and are right not to — the version
        # belongs to the SDK. A finding here would be switched off within a day.
        report = self._report()
        self.assertEqual(report.findings, [])
        self.assertTrue(any(sp.SPEC_UNDECLARED in n for n in report.notes))

    def test_an_answering_sse_endpoint_is_legacy_transport_with_a_date(self) -> None:
        report = self._report(
            wire=sp.WireResult(
                url="https://x.invalid/mcp",
                reachable=True,
                sse_endpoint="https://x.invalid/sse",
                stateless_ok=True,
            )
        )
        finding = next(f for f in report.findings if f.code == sp.LEGACY_TRANSPORT)
        self.assertIn("357 day(s) left", finding.detail)
        self.assertIn("RECOMMENDATION", finding.detail)

    def test_an_issued_session_id_is_legacy_transport(self) -> None:
        report = self._report(
            wire=sp.WireResult(
                url="u", reachable=True, session_id="abc", stateless_ok=True
            )
        )
        self.assertIn(sp.LEGACY_TRANSPORT, [f.code for f in report.findings])

    def test_a_refused_stateless_call_is_legacy_transport(self) -> None:
        report = self._report(
            wire=sp.WireResult(
                url="u", reachable=True, stateless_ok=False, stateless_with_headers=400
            )
        )
        self.assertIn(sp.LEGACY_TRANSPORT, [f.code for f in report.findings])

    def test_a_migrated_wire_is_green(self) -> None:
        report = self._report(
            wire=sp.WireResult(
                url="u", reachable=True, stateless_ok=True, handshake_ok=False
            )
        )
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), sp.EXIT_GREEN)

    def test_an_unreachable_wire_is_named_as_a_gap(self) -> None:
        report = self._report(
            wire=sp.WireResult(url="u", reachable=False, notes=["down"])
        )
        self.assertEqual(report.findings, [])
        self.assertTrue(any(g.startswith("wire:") for g in report.unmeasured))

    def test_a_missing_portfolio_is_named_rather_than_left_blank(self) -> None:
        report = self._report(code=sp.Declared(versions=["2026-07-28"]))
        self.assertTrue(any("Absence is not agreement" in g for g in report.unmeasured))

    def test_nothing_measured_at_all_is_exit_three(self) -> None:
        report = self._report()
        self.assertEqual(report.exit_code(), sp.EXIT_NOT_MEASURED)


# ---------------------------------------------------------------------------
# rendering and the CLI contract
# ---------------------------------------------------------------------------


class RenderTest(unittest.TestCase):
    def test_gaps_are_never_rendered_as_green_cells(self) -> None:
        report = sp.Report(target="/tmp/x", today=TODAY, until=date(2027, 7, 28))
        sp.classify(report)
        text = sp.render(report)
        self.assertIn("UNVERIFIED", text)
        self.assertIn("These are gaps, not green cells", text)

    def test_the_report_admits_its_rules_are_unverified(self) -> None:
        report = sp.Report(target="/tmp/x", today=TODAY, until=date(2027, 7, 28))
        self.assertIn("not from the spec document", sp.render(report))

    def test_the_verified_flag_changes_wording_and_not_a_verdict(self) -> None:
        # A verdict that moved when somebody set a flag would not be a measurement.
        plain = sp.run(REPO, today=TODAY, until=date(2027, 7, 28))
        flagged = sp.run(REPO, today=TODAY, until=date(2027, 7, 28), spec_verified=True)
        self.assertEqual(
            [f.code for f in plain.findings], [f.code for f in flagged.findings]
        )
        self.assertEqual(plain.exit_code(), flagged.exit_code())
        self.assertNotIn("not from the spec document", sp.render(flagged))

    def test_the_json_report_carries_provenance_and_the_deadline(self) -> None:
        report = sp.run(REPO, today=TODAY, until=date(2027, 7, 28))
        data = report.as_dict()
        self.assertEqual(data["probe"], "spec")
        self.assertEqual(data["deprecation_deadline"], "2027-07-28")
        self.assertIn("provenance", data)
        self.assertIn("sources", data)

    def test_the_probe_finds_this_repositorys_own_literal(self) -> None:
        # The occasion, as a test: the auditor carried a hand-maintained protocol
        # version in transport_boot_probe.py, which is the exact defect class
        # identity_probe exists to catch.
        found = sp.scan_code(REPO / "scripts")
        self.assertIn("2025-06-18", found.versions)


class CliTest(unittest.TestCase):
    def test_a_bad_date_is_a_harness_error_not_a_verdict(self) -> None:
        self.assertEqual(
            sp.main(["--target", str(REPO), "--now", "not-a-date"]), sp.EXIT_CANNOT_RUN
        )

    def test_a_nonexistent_target_without_a_url_cannot_run(self) -> None:
        report = sp.run(Path("/nonexistent/x"))
        self.assertTrue(report.harness_error)
        self.assertEqual(report.exit_code(), sp.EXIT_CANNOT_RUN)


if __name__ == "__main__":
    unittest.main()
