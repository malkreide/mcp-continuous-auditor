#!/usr/bin/env python3
"""A stdio MCP server standing in for an INSTALLED entrypoint — stdlib only.

Behaviours via ``SHIPPED_FIXTURE_MODE``:

  ``ok``       lists two tools and answers ``tools/call`` with content.
  ``empty``    answers the call with an EMPTY content list — the incident's
               actual shape: the tool works, responds, and returns nothing.
  ``error``    answers with ``isError``.
  ``rpcerror`` answers with a JSON-RPC error object.
  ``needsargs`` every tool declares a required parameter, so there is nothing
               the probe may call blind.

Like ``boot_stdio_ok.py`` it exits on stdin EOF and delays the ``tools/call``
answer, so a probe that closes stdin after writing measures a failure that is
not there. That is the trap being made reproducible, not incidental.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("SHIPPED_FIXTURE_MODE", "ok")

TOOLS_FREE = [
    {
        "name": "health",
        "description": "liveness",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_count",
        "description": "count",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
TOOLS_REQUIRED = [
    {
        "name": "search",
        "description": "search",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    },
]


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:  # EOF on stdin ends the process
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, ident = msg.get("method"), msg.get("id")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "shipped-fixture", "version": "1"},
                    },
                }
            )
        elif method == "tools/list":
            tools = TOOLS_REQUIRED if MODE == "needsargs" else TOOLS_FREE
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": tools}})
        elif method == "tools/call":
            # The delay is the point: a tool call is the most network-bound thing
            # a server does, so this is where closing stdin early hurts most.
            time.sleep(0.4)
            if MODE == "rpcerror":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "error": {"code": -32602, "message": "bad arguments"},
                    }
                )
            elif MODE == "error":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "upstream refused"}],
                        },
                    }
                )
            elif MODE == "empty":
                send({"jsonrpc": "2.0", "id": ident, "result": {"content": []}})
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "result": {"content": [{"type": "text", "text": "42 records"}]},
                    }
                )
        elif method and method.startswith("notifications/"):
            continue
        elif ident is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32601, "message": f"unknown {method}"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
