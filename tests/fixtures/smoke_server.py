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

WHICH "FASTMCP" THIS IS — deliberately (b), and deliberately left that way
----------------------------------------------------------------------------
Two different projects carry the name, and they cannot share an environment:

  (a) ``mcp`` — the official SDK. Its server class is
      ``mcp.server.mcpserver.MCPServer``, RENAMED from ``mcp.server.fastmcp.FastMCP``
      in the 2.0 break (the old module was removed with no shim).
  (b) ``fastmcp`` — a SEPARATE PyPI project on its own major line. That is what
      the import below is. ``from fastmcp import FastMCP`` is still correct here
      and must NOT be rewritten to ``MCPServer``.

``fastmcp`` still requires ``mcp`` 1.x, so it cannot be installed next to
``mcp`` 2.x. This fixture therefore exercises the (b) branch of the tooling's
in-memory client dispatch — which is a real branch: the portfolio contains
servers on both. It does NOT stand in for a 2.x target, and a green run here is
not evidence that the (a) path works. See ``schemas/generate_schemas.py``'s
``in_memory_client`` for the dispatch, and the target repos' own ``pyproject.toml``
for which side any given server is on.
"""

from __future__ import annotations

import httpx
from fastmcp import FastMCP  # (b) — the standalone package, NOT the renamed SDK class

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
