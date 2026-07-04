"""Minimal FastMCP smoke target — a self-contained fixture for the auditor's own
pipeline tests (Iteration 0 / review finding U-B).

The real target (``zurich-opendata-mcp``) is an external repo, so the two code
paths that only make sense against a live server — the schema-drift gate
(``schemas/generate_schemas.py``) and the promptfoo provider
(``promptfoo/providers/call_tool.py``) — can normally not be exercised from this
repo at all. This tiny server closes that gap without any network:

  * ``health`` and ``record_count`` declare return type hints, so FastMCP derives
    an output schema for each — the drift detector then has something to check.
  * ``record_count`` performs a single ``httpx`` GET, so the provider's fixture
    replay (``httpx.AsyncClient.request`` mocked against ``tests/smoke_fixtures/``)
    is driven exactly as it is for the real tools.
  * ``smoke://info`` exercises the resource read path.

Import path follows the ``MCP_SERVER_IMPORT`` convention: ``smoke_server:mcp``.
"""
from __future__ import annotations

import httpx
from fastmcp import FastMCP

mcp = FastMCP("smoke")

# Deliberately unroutable — every call in the tests goes through the provider's
# httpx mock, never the real network.
UPSTREAM = "https://smoke.invalid/api/records"
HTTP_TIMEOUT = 5.0


@mcp.tool
async def health() -> dict[str, str]:
    """Liveness probe — no network, always the same shape."""
    return {"status": "ok"}


@mcp.tool
async def record_count() -> dict[str, int]:
    """Count records from the (mocked) upstream — drives the fixture-replay path."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(UPSTREAM)
        resp.raise_for_status()
        payload = resp.json()
    return {"count": len(payload.get("result", []))}


@mcp.resource("smoke://info")
def info() -> str:
    """A static JSON resource body for the read_resource path."""
    return '{"name": "smoke", "kind": "fixture"}'
