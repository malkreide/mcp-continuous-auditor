"""FastMCP server (demo) — the surface a Worker would patch under PR + CI.

The ``dataset_count`` tool below was added GREEN: only after its async test
(``tests/test_dataset_count.py``) was written and seen to fail RED. See
examples/worker-tdd-demo/README.md.
"""
from __future__ import annotations

import httpx
from fastmcp import FastMCP

mcp = FastMCP("zurich-opendata-demo")

# CKAN catalogue endpoint for the City of Zürich open-data portal.
PACKAGE_LIST_URL = "https://data.stadt-zuerich.ch/api/3/action/package_list"
HTTP_TIMEOUT = 30.0


@mcp.tool
async def health() -> dict[str, str]:
    """Liveness probe — confirms the server is importable and serving tools."""
    return {"status": "ok"}


@mcp.tool
async def dataset_count() -> dict[str, int]:
    """Return how many datasets the Zürich CKAN catalogue currently lists."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(PACKAGE_LIST_URL)
        resp.raise_for_status()
        payload = resp.json()
    datasets = payload.get("result", [])
    return {"count": len(datasets)}
