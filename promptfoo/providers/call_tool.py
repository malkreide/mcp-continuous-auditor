"""promptfoo Python provider: call an MCP tool/resource in-process.

Deterministic by construction — there is **no live network**. Outbound httpx is
patched with an ``AsyncMock`` that replays a recorded fixture from
``promptfoo/fixtures/``; the server's in-memory client then drives the real tool
against that fixture. The provider returns the tool's **raw output** — JSON for
a tool that returns structured data, its text for one that does not — and, as
``metadata.requests``, every upstream request the tool made. An assertion that a
payload "is not forwarded" reads that list, not the wording of the output.

Which in-memory client that is depends on the target — see ``in_memory_client``
below. Two different projects are called FastMCP and they cannot share an
environment, so this file asks the server object rather than pinning either.

promptfoo calls ``call_api(prompt, options, context)``; it reads these vars:

  tool       name of the tool to call (mutually exclusive with `resource`/`list`)
  resource   a resource URI to read, e.g. "zurich://geo/stadtkreise"
  list       a LIST response to fetch: "tools" | "resources" | "prompts". Spec
             2026-07-28 requires these to carry `ttlMs`/`cacheScope` and to come
             back in a deterministic order, and both are payload properties — so
             they can be asserted here honestly, offline and key-less.
             What CANNOT be asserted through this provider is anything about the
             HANDSHAKE: the in-memory client speaks to the server in-process and
             there is no wire, so "a call without initialize succeeds" would be a
             statement about the SDK and would be green for every server, migrated
             or not. That question belongs to scripts/spec_probe.py --url, which
             measures it on a real connection.
  args       JSON object string of tool arguments (default "{}"), exactly as the
             tool's input schema declares them — a tool taking one Pydantic
             model wants ``{"params": {...}}``. Arguments that cannot match the
             schema are refused before the call (see ``_check_args``).
  fixture    fixture basename under promptfoo/fixtures/ (the recorded UPSTREAM
             httpx response the mocked client should replay). Omit for tools
             whose path is rejected before any network call (e.g. an SQL guard).
             A fixture may carry ``_routes`` for a tool that makes more than one
             upstream request (see ``_split_routes``).

The target server is imported via MCP_SERVER_IMPORT="package.module:attr"
(default ``zurich_opendata_mcp.server:mcp``). Fixture dir override:
MCP_FIXTURES_DIR.
"""

from __future__ import annotations

import asyncio
import builtins
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
    listing = vars_.get("list")
    args = json.loads(vars_.get("args", "{}"))
    fixture = vars_.get("fixture")

    if not tool and not resource and not listing:
        return {"error": "provider needs `tool`, `resource` or `list` in vars"}

    try:
        output, requests = asyncio.run(_invoke(tool, resource, args, fixture, listing))
        # `requests` is every upstream call the tool made, as the httpx mock saw
        # it. An assertion reads it as context.providerResponse.metadata.requests:
        # "the payload was not forwarded" is a statement about THIS list, and an
        # error message that happens to omit the payload is not evidence of it.
        return {"output": output, "metadata": {"requests": requests}}
    except Exception as exc:  # surface to promptfoo as a provider error, not a crash
        return {"error": _describe(exc)}


def _describe(exc: BaseException) -> str:
    """The leaf exceptions, not their wrapper.

    The in-memory clients run inside an anyio task group, so an exception raised
    while the client is open arrives as "ExceptionGroup: unhandled errors in a
    TaskGroup (1 sub-exception)" — which names the mechanism and hides the cause.
    """
    group_type = getattr(builtins, "BaseExceptionGroup", None)  # Python >= 3.11
    if group_type is not None and isinstance(exc, group_type):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


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


def _split_routes(payload: Any) -> tuple[Any, list[dict]]:
    """Separate a fixture's default response from its `_routes`.

    ONE FIXTURE PER TEST USED TO MEAN ONE RESPONSE PER TEST, and a tool that makes
    two upstream calls got the same answer twice. `search_stadtratsbeschluesse`
    asks for the records and, in parallel, for `COUNT(*) AS cnt`; handed the
    records payload for both, it raised `KeyError: 'cnt'` before producing any
    output — and a test asserting what the output does NOT contain passed on the
    traceback.

    `_routes` is a list of `{"match": <substring>, "payload": <response>}`. Each
    upstream request is serialised (method, URL, params, body) and the first
    route whose `match` occurs in it answers; everything else gets the fixture
    itself. Matching on the request rather than on call order keeps it correct
    under `asyncio.gather`, which promises no order.
    """
    if isinstance(payload, dict) and "_routes" in payload:
        payload = dict(payload)
        return payload, list(payload.pop("_routes") or [])
    return payload, []


def _request_record(method: str, url: Any, kwargs: dict) -> dict:
    """What a tool sent upstream, JSON-safe, for routing and for the assertions."""
    record = {"method": str(method).upper(), "url": str(url)}
    for key in ("params", "json", "data", "content"):
        if kwargs.get(key) is not None:
            record[key] = json.loads(json.dumps(kwargs[key], default=str))
    return record


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


# --------------------------------------------------------------------------
# TWO DIFFERENT THINGS ARE CALLED FASTMCP, and they cannot share an environment.
#
#   (a) `mcp` — the OFFICIAL SDK. Server class `mcp.server.mcpserver.MCPServer`
#       (renamed from `mcp.server.fastmcp.FastMCP` in the 2.0 break, which
#       removed the old module with no shim). Client: `mcp.client.client.Client`.
#   (b) `fastmcp` — a SEPARATE PyPI project on its own major line (3.x while the
#       SDK is at 2.x). `fastmcp.FastMCP` driven by `fastmcp.Client`. Still
#       current, still correct, NOT the thing that was renamed.
#
# `fastmcp` still requires `mcp` 1.x, so alongside `mcp` 2.x even `import
# fastmcp` raises. This provider runs INSIDE the target's environment, so a hard
# import of either one makes it unrunnable against half the portfolio — which is
# what the previous `from fastmcp import Client` did to every migrated target.
# --------------------------------------------------------------------------


def _sdk_client(server: Any) -> Any:
    from mcp.client.client import Client  # (a) — mcp >= 2

    return Client(server)


def _fastmcp_client(server: Any) -> Any:
    from fastmcp import Client  # (b) — the standalone package

    return Client(server)


def in_memory_client(server: Any) -> Any:
    """An in-memory client for `server`, from whichever SDK owns it.

    Dispatch is on the server object's own module, not on what happens to be
    importable — that is the one signal that cannot be wrong.
    """
    origin = (type(server).__module__ or "").split(".")[0]
    order = (
        (_fastmcp_client, _sdk_client)
        if origin == "fastmcp"
        else (_sdk_client, _fastmcp_client)
    )
    problems: list[str] = []
    for make in order:
        try:
            return make(server)
        except ImportError as exc:
            problems.append(f"{make.__name__}: {exc}")
    raise RuntimeError(
        "no in-memory client available for a server of type "
        f"{type(server).__module__}.{type(server).__name__}. Tried the official "
        "SDK (`mcp>=2`) and the standalone `fastmcp` package — different "
        "projects, cannot be installed together. Details: " + "; ".join(problems)
    )


_LIST_METHODS = {
    "tools": "list_tools",
    "resources": "list_resources",
    "prompts": "list_prompts",
}


def _list_to_json(kind: str, result: Any) -> str:
    """A list response, normalised to a JSON object the asserts can read.

    The SDKs hand back either a typed result object or a bare list of items,
    depending on version and client. Both are reduced to
    ``{"kind", "names", "ttlMs", "cacheScope", "raw_shape"}``:

      * ``names`` is the ORDER, which spec 2026-07-28 requires to be
        deterministic — an assert compares two calls, and a set comparison would
        be blind to exactly the property being checked;
      * ``ttlMs``/``cacheScope`` are ``null`` when the SDK does not surface them.
        NULL IS NOT ZERO AND NOT ABSENT-AS-COMPLIANT: the assert that reads them
        must distinguish "the server omits the field" from "this client cannot
        see it", which is why ``raw_shape`` records which of the two happened.
    """
    items = getattr(result, kind, None)
    raw_shape = type(result).__name__
    if items is None and isinstance(result, list):
        items, raw_shape = result, "list"
    if items is None:
        items = []
    names = []
    for item in items:
        name = getattr(item, "name", None) or getattr(item, "uri", None)
        names.append(str(name) if name is not None else repr(item))
    ttl = getattr(result, "ttlMs", None)
    if ttl is None:
        ttl = getattr(result, "ttl_ms", None)
    scope = getattr(result, "cacheScope", None)
    if scope is None:
        scope = getattr(result, "cache_scope", None)
    return json.dumps(
        {
            "kind": kind,
            "count": len(names),
            "names": names,
            "ttlMs": ttl,
            "cacheScope": str(scope) if scope is not None else None,
            # Which shape the SDK returned, so an absent ttlMs can be attributed
            # to the server or to this client rather than silently to neither.
            "raw_shape": raw_shape,
        },
        ensure_ascii=False,
    )


async def _list(client: Any, kind: str) -> str:
    method = _LIST_METHODS.get(kind)
    if method is None:
        raise ValueError(
            f"unknown list kind {kind!r} — expected one of {sorted(_LIST_METHODS)}"
        )
    fn = getattr(client, method, None)
    if fn is None:
        raise RuntimeError(
            f"the in-memory client exposes no `{method}` — the list response was "
            "NOT measured, which is not the same as a compliant one"
        )
    # Asked TWICE, because "deterministic order" is a claim about two answers.
    # The test that says so used to receive one listing and check that `names`
    # was an array — which every server passes, ordered or not.
    first = json.loads(_list_to_json(kind, await fn()))
    second = json.loads(_list_to_json(kind, await fn()))
    first["repeat_identical"] = first["names"] == second["names"]
    return json.dumps(first, ensure_ascii=False)


def _input_schema_of(tool: Any) -> dict:
    """A tool's input schema under either spelling (`mcp` 2.x snake_cases it)."""
    return (
        getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    )


async def _check_args(client: Any, tool: str, args: dict) -> None:
    """Refuse a call whose arguments cannot reach the tool body.

    WHY THIS EXISTS. zurich-opendata-mcp has taken one Pydantic model per tool,
    `params`, since its first commit. Every tool test in both profiles passed its
    arguments flat. So every call stopped in argument validation, the tool never
    ran — and three tests PASSED on the validation error: "DELETE is rejected"
    found the word "Error" in Pydantic's message, "not raw GeoJSON" found that a
    message is not JSON. That is a gate measuring its own input, green.

    The check is the protocol's own contract, not a guess about the SDK: the
    tool's `inputSchema` names its required top-level keys and its properties. A
    missing required key or a key the tool does not declare means the test, not
    the server, is malformed — and that must surface as a provider ERROR, which
    no assertion can turn into a pass.

    Deliberately top-level only. Whether the VALUES inside `params` are valid is
    the tool's behaviour, and a test may well send invalid ones on purpose (an
    unknown layer, a non-SELECT statement) to see them rejected.
    """
    listing = await client.list_tools()
    tools = list(getattr(listing, "tools", listing))  # `fastmcp` returns a plain list
    match = next((t for t in tools if getattr(t, "name", None) == tool), None)
    if match is None:
        raise LookupError(
            f"the server offers no tool {tool!r} — the test names a tool that does not exist"
        )
    schema = _input_schema_of(match)
    declared = set((schema.get("properties") or {}).keys())
    missing = [k for k in (schema.get("required") or []) if k not in args]
    # `properties: {}` is a statement — the tool takes nothing — and every key
    # is then undeclared. Only a schema with no `properties` at all says nothing.
    knows_properties = isinstance(schema.get("properties"), dict)
    unknown = [k for k in args if knows_properties and k not in declared]
    if missing or unknown:
        raise ValueError(
            f"arguments for {tool!r} do not match its input schema — the call could "
            f"not reach the tool body, so nothing about the tool was measured. "
            f"missing required: {missing or '-'}; not declared: {unknown or '-'}; "
            f"declared: {sorted(declared) or '-'}"
        )


async def _invoke(
    tool: str | None,
    resource: str | None,
    args: dict,
    fixture: str | None,
    listing: str | None = None,
) -> tuple[str, list[dict]]:
    mcp = _load_server()
    payload, routes = _split_routes(_fixture_payload(fixture))
    requests: list[dict] = []

    async def _fake_request(
        self: Any, method: str, url: Any, *a: Any, **k: Any
    ) -> _FakeResponse:
        # The single network chokepoint for httpx.AsyncClient.get/post/... is request().
        record = _request_record(method, url, k)
        requests.append(record)
        seen = json.dumps(record, ensure_ascii=False)
        for route in routes:
            if route.get("match") and route["match"] in seen:
                return _FakeResponse(route.get("payload"))
        return _FakeResponse(payload)

    with patch("httpx.AsyncClient.request", new=_fake_request):
        async with in_memory_client(mcp) as client:
            if listing:
                return await _list(client, listing), requests
            if resource:
                return _resource_to_json(await client.read_resource(resource)), requests
            await _check_args(client, tool, args)
            return _result_to_json(await client.call_tool(tool, args)), requests
