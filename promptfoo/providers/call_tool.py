"""promptfoo Python provider: call a FastMCP tool/resource in-process.

Deterministic by construction — there is **no live network**. Outbound httpx is
patched with an ``AsyncMock`` that replays a recorded fixture from
``promptfoo/fixtures/``; the FastMCP in-memory client then drives the real tool
against that fixture. The provider returns the tool's **raw JSON** so promptfoo
can ``is-json``-validate it against the generated schema (schema drift = red CI).

promptfoo calls ``call_api(prompt, options, context)``; it reads these vars:

  tool       name of the tool to call (mutually exclusive with `resource`)
  resource   a resource URI to read, e.g. "zurich://geo/stadtkreise"
  args       JSON object string of tool arguments (default "{}")
  fixture    fixture basename under promptfoo/fixtures/ (the recorded UPSTREAM
             httpx response the mocked client should replay). Omit for tools
             whose path is rejected before any network call (e.g. an SQL guard).

The target server is imported via MCP_SERVER_IMPORT="package.module:attr"
(default ``zurich_opendata_mcp.server:mcp``). Fixture dir override:
MCP_FIXTURES_DIR.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_DEFAULT_FIXTURES = _HERE.parent / "fixtures"
_SERVER_REF = os.environ.get("MCP_SERVER_IMPORT", "zurich_opendata_mcp.server:mcp")


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """promptfoo provider entrypoint. Returns {"output": <json str>} or {"error": ...}."""
    vars_ = (context or {}).get("vars", {})
    tool = vars_.get("tool")
    resource = vars_.get("resource")
    args = json.loads(vars_.get("args", "{}"))
    fixture = vars_.get("fixture")

    if not tool and not resource:
        return {"error": "provider needs either `tool` or `resource` in vars"}

    try:
        output = asyncio.run(_invoke(tool, resource, args, fixture))
        return {"output": output}
    except Exception as exc:  # surface to promptfoo as a provider error, not a crash
        return {"error": f"{type(exc).__name__}: {exc}"}


def _load_server() -> Any:
    mod_name, _, attr = _SERVER_REF.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, attr or "mcp")


def _fixture_payload(fixture: str | None) -> Any:
    if not fixture:
        return None
    base = Path(os.environ.get("MCP_FIXTURES_DIR", _DEFAULT_FIXTURES))
    name = fixture if fixture.endswith(".json") else f"{fixture}.json"
    return json.loads((base / name).read_text(encoding="utf-8"))


class _FakeResponse:
    """Just enough of httpx.Response for the tools' happy path — no network."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def json(self, **_: Any) -> Any:  # httpx.Response.json is synchronous
        return self._payload

    def raise_for_status(self) -> _FakeResponse:
        return self


def _coerce_json(value: Any) -> str:
    """Return a JSON string for whatever the in-memory client handed back."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _result_to_json(result: Any) -> str:
    """Extract the raw tool output from a FastMCP CallToolResult.

    Prefer the text content block: for a dict-returning tool FastMCP mirrors the
    structured output there as a JSON string (is-json validates it), while a
    Markdown/str tool yields its raw text — not FastMCP's ``{"result": ...}``
    auto-wrapper. Fall back to structured content only when no text block exists.
    """
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if text is not None:
            return text
    for attr in ("structured_content", "structuredContent", "data"):
        value = getattr(result, attr, None)
        if value is not None:
            return _coerce_json(value)
    return _coerce_json(content if content is not None else result)


def _resource_to_json(result: Any) -> str:
    """Extract the raw text/JSON from a FastMCP read_resource result."""
    blocks = getattr(result, "contents", result)
    if isinstance(blocks, list) and blocks:
        first = blocks[0]
        text = getattr(first, "text", None)
        if text is not None:
            return text
        blob = getattr(first, "blob", None)
        if blob is not None:
            return blob if isinstance(blob, str) else _coerce_json(blob)
    return _coerce_json(blocks)


async def _invoke(
    tool: str | None, resource: str | None, args: dict, fixture: str | None
) -> str:
    from fastmcp import Client

    mcp = _load_server()
    payload = _fixture_payload(fixture)

    async def _fake_request(self: Any, method: str, url: Any, *a: Any, **k: Any) -> _FakeResponse:
        # The single network chokepoint for httpx.AsyncClient.get/post/... is request().
        return _FakeResponse(payload)

    with patch("httpx.AsyncClient.request", new=_fake_request):
        async with Client(mcp) as client:
            if resource:
                return _resource_to_json(await client.read_resource(resource))
            return _result_to_json(await client.call_tool(tool, args))
