#!/usr/bin/env python3
"""Generate JSON-Schemas of the MCP tool outputs from their type hints.

The schema *is* the drift detector (Plan Phase 2). The server derives a
JSON-Schema for each tool from its declared return type; committing those
schemas turns any shape change — an upstream CKAN/WFS field rename, or a
careless return-type edit — into a reviewable git diff and a red CI check,
instead of a silent break.

Run this in the TARGET MCP-server repo, where the server is importable. Point it
at the server with MCP_SERVER_IMPORT="package.module:attr" (default
``zurich_opendata_mcp.server:mcp``). That convention is unaffected by any SDK
rename: it names a MODULE and an ATTRIBUTE, never a class.

    python schemas/generate_schemas.py            # write/update schemas/<tool>.json
    python schemas/generate_schemas.py --check    # CI: exit 1 if a committed schema drifts

Only tools that declare a return type hint get an output schema. Resources have
no output schema in MCP — the GeoJSON contract schema
(``geojson_featurecollection.json``) is hand-maintained against RFC 7946 and is
left untouched by this script.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVER_REF = os.environ.get("MCP_SERVER_IMPORT", "zurich_opendata_mcp.server:mcp")


def _load_server() -> object:
    mod_name, _, attr = _SERVER_REF.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, attr or "mcp")


# --------------------------------------------------------------------------
# TWO DIFFERENT THINGS ARE CALLED FASTMCP, and they cannot share an environment.
#
#   (a) `mcp` — the OFFICIAL SDK. Its server class is
#       `mcp.server.mcpserver.MCPServer`, renamed from `mcp.server.fastmcp.FastMCP`
#       in the 2.0 break (which removed the old module with no shim). Its
#       in-memory client is `mcp.client.client.Client`.
#   (b) `fastmcp` — a SEPARATE project on PyPI with its own major line
#       (3.x while the SDK is at 2.x). `fastmcp.FastMCP` driven by
#       `fastmcp.Client`. Still current, still correct, NOT the thing renamed.
#
# They are mutually exclusive: `fastmcp` (via `fastmcp-slim`) still requires
# `mcp` 1.x, so in an environment holding `mcp` 2.x even `import fastmcp` raises
# `ImportError: cannot import name 'McpError' from 'mcp.shared.exceptions'`.
#
# So this script cannot pin either one — the portfolio contains both, and this
# file runs INSIDE whichever target it is pointed at. It asks the environment,
# preferring the SDK when the server object came from it. Before this, the hard
# `from fastmcp import Client` made the schema gate unrunnable against every
# target that had migrated to the 2.x SDK.
# --------------------------------------------------------------------------

def _sdk_client(server: object) -> object:
    from mcp.client.client import Client  # (a) — mcp >= 2

    return Client(server)


def _fastmcp_client(server: object) -> object:
    from fastmcp import Client  # (b) — the standalone package

    return Client(server)


def in_memory_client(server: object) -> object:
    """An in-memory client for `server`, from whichever SDK owns it.

    Dispatch is on the server object's own module rather than on what happens
    to be importable: that is the one signal that cannot be wrong, and getting
    it backwards would drive a server with the other project's client.
    """
    origin = (type(server).__module__ or "").split(".")[0]
    order = ((_fastmcp_client, _sdk_client) if origin == "fastmcp"
             else (_sdk_client, _fastmcp_client))
    problems: list[str] = []
    for make in order:
        try:
            return make(server)
        except ImportError as exc:
            problems.append(f"{make.__name__}: {exc}")
    raise RuntimeError(
        "no in-memory client available for a server of type "
        f"{type(server).__module__}.{type(server).__name__}. Tried the official "
        "SDK (`mcp>=2`, `mcp.client.client.Client`) and the standalone `fastmcp` "
        "package (`fastmcp.Client`) — they are different projects and cannot be "
        "installed together. Details: " + "; ".join(problems))


def _tools_of(listing: object) -> list:
    """The tool list, however the client wrapped it.

    `mcp` 2.x returns a `ListToolsResult`; `fastmcp` returns a plain list.
    """
    return list(getattr(listing, "tools", listing))  # type: ignore[arg-type]


def output_schema_of(tool: object) -> dict | None:
    """A tool's output schema under either spelling.

    `mcp` 2.x uses `output_schema`, `fastmcp` uses `outputSchema`. Both are read
    with a default so the pydantic model's `__getattr__` cannot raise.
    """
    return getattr(tool, "outputSchema", None) or getattr(tool, "output_schema", None)


async def _collect() -> dict[str, dict]:
    """Return {tool_name: output_schema} for every tool that declares one."""
    mcp = _load_server()
    schemas: dict[str, dict] = {}
    async with in_memory_client(mcp) as client:  # type: ignore[attr-defined]
        for tool in _tools_of(await client.list_tools()):
            schema = output_schema_of(tool)
            if schema:
                schemas[tool.name] = schema
    return schemas


def _serialize(schema: dict) -> str:
    # Stable, diff-friendly output: sorted keys, trailing newline.
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if any committed schema differs from the freshly generated one",
    )
    args = parser.parse_args()

    schemas = asyncio.run(_collect())
    if not schemas:
        print(
            "No tool output schemas found — do the tools declare return type hints, "
            f"and is MCP_SERVER_IMPORT correct ({_SERVER_REF!r})?",
            file=sys.stderr,
        )
        return 1

    drift: list[str] = []
    for name, schema in sorted(schemas.items()):
        path = _HERE / f"{name}.json"
        fresh = _serialize(schema)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if args.check:
            if current != fresh:
                drift.append("new: " + name if current is None else name)
        else:
            path.write_text(fresh, encoding="utf-8")
            print(f"wrote schemas/{name}.json")

    if args.check and drift:
        print(
            "Schema drift detected — regenerate with `python schemas/generate_schemas.py` "
            "and review the diff: " + ", ".join(drift),
            file=sys.stderr,
        )
        return 1
    if args.check:
        print(f"schemas in sync ({len(schemas)} tools checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
