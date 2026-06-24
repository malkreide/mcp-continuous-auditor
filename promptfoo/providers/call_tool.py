"""promptfoo Python provider: calls a FastMCP tool in-process.

Deterministic — no live network. httpx is expected to be mocked against
recorded fixtures in tests/fixtures/ (see the fastmcp-testing skill).

promptfoo invokes ``call_api(prompt, options, context)`` and expects a dict:
``{"output": <str>}``.
"""
from __future__ import annotations

import asyncio
import json


def call_api(prompt, options, context):
    vars_ = (context or {}).get("vars", {})
    tool = vars_.get("tool")
    args = json.loads(vars_.get("args", "{}"))

    output = asyncio.run(_invoke(tool, args))
    return {"output": output}


async def _invoke(tool: str, args: dict) -> str:
    # TODO: import your server and call the tool via the FastMCP in-memory client.
    #
    #   from fastmcp import Client
    #   from zurich_opendata_mcp.server import mcp
    #   async with Client(mcp) as client:
    #       result = await client.call_tool(tool, args)
    #       return result[0].text
    #
    raise NotImplementedError(
        f"Wire up the FastMCP in-memory client for tool={tool!r}"
    )
