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


@unittest.skipUnless(_HAVE_FASTMCP, "fastmcp not installed (uv run --with fastmcp to enable)")
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

        out = ct.call_api("", {}, {"vars": {
            "tool": "record_count", "fixture": "records", "args": "{}",
        }})
        self.assertIn("output", out, msg=out)
        parsed = json.loads(out["output"])  # is-json would pass
        self.assertEqual(parsed["count"], 3)  # records.json has 3 entries

    def test_provider_reads_resource(self) -> None:
        import call_tool as ct

        out = ct.call_api("", {}, {"vars": {"resource": "smoke://info"}})
        self.assertIn("output", out, msg=out)
        self.assertEqual(json.loads(out["output"])["name"], "smoke")


if __name__ == "__main__":
    unittest.main()
