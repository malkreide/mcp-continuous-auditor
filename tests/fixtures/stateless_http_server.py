#!/usr/bin/env python3
"""A streamable-HTTP MCP server on the STATELESS core (spec 2026-07-28) — stdlib only.

This fixture exists next to ``boot_http_server.py`` rather than replacing it, and
that is the point. The legacy handshake stays valid for the length of the
deprecation window, so the gate has to keep working against BOTH shapes; a
fixture suite that only spoke the new one would drop the regression for the
transport 39 of the 42 portfolio servers are still on.

Selected by ``BOOT_FIXTURE_MODE``:

  ``stateless``  ``initialize`` is answered with JSON-RPC -32601 (the method the
                 spec removed), and a real ``tools/list`` with no handshake in
                 front of it succeeds. No ``Mcp-Session-Id`` is ever issued. This
                 is a HEALTHY, migrated server — the gate must report a pass, not
                 a finding.
  ``broken``     ``initialize`` fails with an INTERNAL error (-32603) and
                 ``tools/list`` fails too. The control: a rejected handshake must
                 not become a blanket excuse, or the gate stops being a gate.
  ``halfway``    ``initialize`` is refused like a migrated server, but the
                 handshake-free call fails as well. Neither migrated nor healthy,
                 and it must still be reported as a failure.

Binds ``HOST``/``PORT`` from the environment, like the other boot fixtures.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("BOOT_FIXTURE_MODE", "stateless")
PATH = os.environ.get("BOOT_FIXTURE_PATH", "/mcp/")

TOOLS = [
    {"name": "alpha", "description": "first", "inputSchema": {"type": "object"}},
    {"name": "beta", "description": "second", "inputSchema": {"type": "object"}},
    {"name": "gamma", "description": "third", "inputSchema": {"type": "object"}},
]

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # keep test output clean
        pass

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # DELIBERATELY no Mcp-Session-Id: the stateless core removed it, and a
        # fixture that issued one anyway would let a regression through.
        self.end_headers()
        self.wfile.write(body)

    def _error(self, ident: object, code: int, message: str) -> None:
        self._send(
            200,
            {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": code, "message": message},
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # No /sse endpoint: a migrated server does not serve the legacy transport.
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") != PATH.rstrip("/"):
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return

        ident = msg.get("id")
        method = msg.get("method")

        if method and method.startswith("notifications/"):
            self._send(202, {})
            return

        if method == "initialize":
            if MODE == "broken":
                self._error(ident, INTERNAL_ERROR, "settings object is read-only")
                return
            # stateless / halfway: the method the spec removed.
            self._error(ident, METHOD_NOT_FOUND, "Method not found: initialize")
            return

        if method == "tools/list":
            if MODE in ("broken", "halfway"):
                self._error(ident, INTERNAL_ERROR, "the tool registry did not load")
                return
            self._send(200, {"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}})
            return

        self._error(ident, METHOD_NOT_FOUND, f"unknown method {method}")


def main() -> int:
    host = os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("PORT") or "0")
    if not port:
        print("stateless_http_server: PORT is required", file=sys.stderr)
        return 2
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
