#!/usr/bin/env python3
"""End-to-end smoke test of the two target-only code paths against a local FastMCP
fixture (review finding U-B).

``schemas/generate_schemas.py`` (the drift detector) and
``promptfoo/providers/call_tool.py`` (the promptfoo provider) normally only run in
the external target repo. Here they are driven against ``tests/fixtures/
smoke_server.py`` so the auditor's own pipeline is verifiable locally, green.

This test needs ``fastmcp``. The rest of the suite is stdlib-only, so it SKIPS
cleanly when fastmcp is absent. To run it, provide fastmcp, e.g.:

    uv run --with fastmcp python -m unittest tests.test_smoke_target
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SMOKE_FIXTURES = Path(__file__).resolve().parent / "smoke_fixtures"

try:
    import fastmcp  # noqa: F401

    _HAVE_FASTMCP = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_FASTMCP = False


@unittest.skipUnless(
    _HAVE_FASTMCP, "fastmcp not installed (uv run --with fastmcp to enable)"
)
class SmokeTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Point the scripts at the smoke server + its fixtures BEFORE importing
        # them (both read MCP_SERVER_IMPORT / MCP_FIXTURES_DIR at import time).
        os.environ["MCP_SERVER_IMPORT"] = "smoke_server:mcp"
        os.environ["MCP_FIXTURES_DIR"] = str(SMOKE_FIXTURES)
        for p in (FIXTURES, REPO / "schemas", REPO / "promptfoo" / "providers"):
            sys.path.insert(0, str(p))

    def test_schema_gate_derives_output_schemas(self) -> None:
        import generate_schemas as gs

        schemas = asyncio.run(gs._collect())
        # Every tool with a return type hint gets an output schema — the drift
        # detector has something to pin.
        self.assertIn("health", schemas)
        self.assertIn("record_count", schemas)
        self.assertIsInstance(schemas["health"], dict)

    def test_provider_replays_fixture_and_returns_is_json(self) -> None:
        import call_tool as ct

        out = ct.call_api(
            "",
            {},
            {
                "vars": {
                    "tool": "record_count",
                    "fixture": "records",
                    "args": "{}",
                }
            },
        )
        self.assertIn("output", out, msg=out)
        parsed = json.loads(out["output"])  # is-json would pass
        self.assertEqual(parsed["count"], 3)  # records.json has 3 entries

    def test_provider_reads_resource(self) -> None:
        import call_tool as ct

        out = ct.call_api("", {}, {"vars": {"resource": "smoke://info"}})
        self.assertIn("output", out, msg=out)
        self.assertEqual(json.loads(out["output"])["name"], "smoke")

    # --- what the determ/graded profiles now lean on -----------------------
    # Each of these closes a way the profiles passed without measuring: a test
    # whose arguments never reached the tool, a "not forwarded" read off the
    # output, one fixture answering two different requests, and a determinism
    # check that looked at one listing.

    def test_upstream_requests_are_recorded_for_the_assertions(self) -> None:
        import call_tool as ct

        out = ct.call_api(
            "", {}, {"vars": {"tool": "record_count", "fixture": "records"}}
        )
        reqs = out["metadata"]["requests"]
        self.assertEqual(len(reqs), 1, msg=reqs)
        self.assertEqual(reqs[0]["method"], "GET")
        self.assertEqual(reqs[0]["url"], "https://smoke.invalid/api/records")

    def test_a_tool_that_makes_no_request_records_none(self) -> None:
        """An empty list is a measurement; a missing one would not be."""
        import call_tool as ct

        out = ct.call_api("", {}, {"vars": {"tool": "health"}})
        self.assertEqual(out["metadata"]["requests"], [], msg=out)

    def test_arguments_the_tool_does_not_declare_are_refused(self) -> None:
        """The state both profiles were in: flat arguments for a `params` tool.
        It has to be a provider ERROR — no assertion can turn that into a pass."""
        import call_tool as ct

        out = ct.call_api(
            "",
            {},
            {"vars": {"tool": "record_count", "args": '{"sql": "DELETE FROM x"}'}},
        )
        self.assertNotIn("output", out, msg=out)
        self.assertIn("do not match its input schema", out["error"])
        self.assertIn("'sql'", out["error"])
        # The wrapper the SDK puts around it is gone; the cause is what is shown.
        self.assertNotIn("TaskGroup", out["error"])

    def test_a_tool_the_server_does_not_offer_is_an_error(self) -> None:
        import call_tool as ct

        out = ct.call_api("", {}, {"vars": {"tool": "no_such_tool"}})
        self.assertIn("offers no tool 'no_such_tool'", out["error"])

    def test_a_route_answers_the_request_it_matches(self) -> None:
        import tempfile

        import call_tool as ct

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "routed.json").write_text(
                json.dumps(
                    {
                        "_routes": [
                            {"match": "smoke.invalid", "payload": {"result": [1, 2]}}
                        ],
                        "result": ["only", "the", "default", "has", "five"],
                    }
                ),
                encoding="utf-8",
            )
            old = os.environ["MCP_FIXTURES_DIR"]
            os.environ["MCP_FIXTURES_DIR"] = d
            try:
                out = ct.call_api(
                    "", {}, {"vars": {"tool": "record_count", "fixture": "routed"}}
                )
            finally:
                os.environ["MCP_FIXTURES_DIR"] = old
        self.assertEqual(json.loads(out["output"])["count"], 2, msg=out)

    def test_list_is_asked_twice_and_compared(self) -> None:
        import call_tool as ct

        out = ct.call_api("", {}, {"vars": {"list": "tools"}})
        listing = json.loads(out["output"])
        self.assertIs(listing["repeat_identical"], True)
        self.assertEqual(sorted(listing["names"]), ["health", "record_count"])


if __name__ == "__main__":
    unittest.main()
