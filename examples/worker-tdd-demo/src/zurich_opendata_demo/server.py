"""FastMCP server (demo) — the surface a Worker would patch under PR + CI.

This file deliberately starts *without* the ``dataset_count`` tool requested in
the demo issue, so the first commit's async test fails (RED). The fix commit
adds the tool (GREEN). See examples/worker-tdd-demo/README.md.
"""
from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("zurich-opendata-demo")

# CKAN catalogue endpoint for the City of Zürich open-data portal.
PACKAGE_LIST_URL = "https://data.stadt-zuerich.ch/api/3/action/package_list"
HTTP_TIMEOUT = 30.0


@mcp.tool
async def health() -> dict[str, str]:
    """Liveness probe — confirms the server is importable and serving tools."""
    return {"status": "ok"}
