#!/usr/bin/env python3
"""An entrypoint that selects its transport with a CLI FLAG, not the environment.

This is `zurich-opendata-mcp` in miniature, and it is the shape that made the
boot gate report a healthy server as dead:

    $ MCP_TRANSPORT=http PORT=41999 zurich-opendata-mcp   -> rc=0, never listens
    $ zurich-opendata-mcp --http --port 41999             -> serves

Without `--http` it runs "stdio", finds stdin closed (the probe passes
``stdin=DEVNULL`` for network transports) and exits **cleanly**. A clean exit is
the whole discriminator: a server that crashed would leave a non-zero status and
a traceback.

``BOOT_FLAG_FIXTURE_MODE``:
  ``flag``  (default) serves only with ``--http``; otherwise exits rc 0.
  ``crash`` raises at start whatever the argv — a genuine boot failure, which
            must still be reported as one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("BOOT_FLAG_FIXTURE_MODE", "flag")

TOOLS = [
    {"name": "health", "description": "liveness", "inputSchema": {"type": "object"}},
    {"name": "record_count", "description": "count", "inputSchema": {"type": "object"}},
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str = "application/json",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") != "/mcp":
            self._send(404, b'{"error":"not found"}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, b'{"error":"bad json"}')
            return
        method = msg.get("method")
        if method and method.startswith("notifications/"):
            self._send(202, b"")
            return
        if method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "flag-fixture", "version": "1"},
                },
            }
            self._send(
                200,
                json.dumps(payload).encode(),
                "application/json",
                {"Mcp-Session-Id": "flag-fixture"},
            )
            return
        if method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {"tools": TOOLS},
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        self._send(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32601, "message": f"unknown {method}"},
                }
            ).encode(),
        )


def main() -> int:
    if MODE == "crash":
        # A real case-1 boot failure: it tried and died. Non-zero status.
        raise ValueError('"Settings" object has no field "host"')

    parser = argparse.ArgumentParser(prog="flag-fixture")
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if not args.http:
        # The default path: "stdio". stdin is DEVNULL under the probe, so it ends
        # immediately — cleanly. Exactly what the real target does.
        sys.stdin.read()
        return 0

    ThreadingHTTPServer(
        (os.environ.get("HOST") or "127.0.0.1", args.port), Handler
    ).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
