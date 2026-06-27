#!/usr/bin/env python3
"""Generate JSON-Schemas of the FastMCP tool outputs from their type hints.

The schema *is* the drift detector (Plan Phase 2). FastMCP derives a JSON-Schema
for each tool from its declared return type; committing those schemas turns any
shape change — an upstream CKAN/WFS field rename, or a careless return-type edit
— into a reviewable git diff and a red CI check, instead of a silent break.

Run this in the TARGET MCP-server repo, where the server is importable. Point it
at the server with MCP_SERVER_IMPORT="package.module:attr" (default
``zurich_opendata_mcp.server:mcp``).

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


async def _collect() -> dict[str, dict]:
    """Return {tool_name: output_schema} for every tool that declares one."""
    from fastmcp import Client  # imported lazily so --help works without the dep

    mcp = _load_server()
    schemas: dict[str, dict] = {}
    async with Client(mcp) as client:
        for tool in await client.list_tools():
            schema = getattr(tool, "outputSchema", None) or getattr(
                tool, "output_schema", None
            )
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
