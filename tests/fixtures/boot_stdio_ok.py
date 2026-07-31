#!/usr/bin/env python3
"""A healthy stdio MCP server — stdlib only, no fastmcp.

Speaks just enough newline-delimited JSON-RPC for the transport boot gate:
``initialize``, the ``notifications/initialized`` notification, and ``tools/list``.

Two deliberate properties, both there to make the stdin trap testable:

  * it EXITS with code 3 the moment stdin reaches EOF. A probe that closes stdin
    after writing its request kills this server before the answer is out, and
    then measures a failure that does not exist;
  * ``tools/list`` sleeps ``BOOT_FIXTURE_LATENCY`` seconds first, standing in for
    the network-bound work a real server does there. Without a delay the trap
    could pass by luck.
"""
from __future__ import annotations

import json
import os
import sys
import time

LATENCY = float(os.environ.get("BOOT_FIXTURE_LATENCY", "0.4"))

TOOLS = [
    {"name": "health", "description": "liveness", "inputSchema": {"type": "object"}},
    {"name": "record_count", "description": "count", "inputSchema": {"type": "object"}},
]


def reply(ident: object, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> int:
    while True:
        line = sys.stdin.readline()
        if line == "":
            # stdin closed: the client is gone (or closed it too early). A real
            # server shuts down here too — that is exactly the trap.
            return 3
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        if method == "initialize":
            reply(msg.get("id"), {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "boot-fixture", "version": "1"},
            })
        elif method == "tools/list":
            time.sleep(LATENCY)  # stand-in for the network-bound work
            reply(msg.get("id"), {"tools": TOOLS})
        elif method and method.startswith("notifications/"):
            continue
        elif "id" in msg:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
