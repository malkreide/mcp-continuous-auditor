#!/usr/bin/env python3
"""A streamable-HTTP MCP server for the boot gate's tests — stdlib only.

One file, three behaviours, selected by ``BOOT_FIXTURE_MODE``:

  ``ok``     serves every request regardless of the ``Host`` header — a correctly
             configured 0.0.0.0 deployment.
  ``host421`` answers HTTP 421 unless the ``Host`` header is a loopback name.
             This is case 2 exactly: the SDK derived its inbound host allow-list
             from the ``127.0.0.1`` default because the configured host was never
             passed to the app builder, so the process runs, answers the local
             health check, and rejects every real request.
  ``hang``   listens but never answers, so the gate's hard per-attempt deadline
             is what ends the probe rather than the probe waiting forever.

Binds ``HOST``/``PORT`` from the environment — the same variables the probe hands
to a real target's entrypoint.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("BOOT_FIXTURE_MODE", "ok")
PATH = os.environ.get("BOOT_FIXTURE_PATH", "/mcp/")
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1"}

TOOLS = [
    {"name": "health", "description": "liveness", "inputSchema": {"type": "object"}},
    {"name": "record_count", "description": "count", "inputSchema": {"type": "object"}},
]


def _host_name(header: str | None) -> str:
    """Host header minus the port — an IPv6 literal keeps its brackets."""
    value = (header or "").strip()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(
        self, fmt: str, *args: object
    ) -> None:  # keep the test output clean
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
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _host_rejected(self) -> bool:
        if MODE != "host421":
            return False
        return _host_name(self.headers.get("Host")) not in LOOPBACK_NAMES

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if MODE == "hang":
            time.sleep(300)
            return
        self._send(405, b'{"error":"method not allowed"}')

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if MODE == "hang":
            time.sleep(300)
            return
        if self.path.rstrip("/") != PATH.rstrip("/"):
            self._send(404, b'{"error":"not found"}')
            return
        if self._host_rejected():
            # 421 Misdirected Request — the shape of the bug, not an invention.
            self._send(421, b'{"error":"Invalid request host"}', "text/plain")
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
                    "serverInfo": {"name": "boot-fixture", "version": "1"},
                },
            }
            # Answer as SSE, the way FastMCP's streamable transport does, so the
            # probe's frame parsing is exercised rather than assumed.
            body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
            self._send(
                200, body, "text/event-stream", {"Mcp-Session-Id": "fixture-session"}
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
        payload = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": -32601, "message": f"unknown method {method}"},
        }
        self._send(200, json.dumps(payload).encode(), "application/json")


def main() -> int:
    host = os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("PORT") or "0")
    if not port:
        print("boot_http_server: PORT is required", file=sys.stderr)
        return 2
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
