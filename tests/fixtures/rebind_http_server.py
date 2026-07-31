#!/usr/bin/env python3
"""An MCP-ish HTTP server with a configurable inbound host allow-list — the
fixture the DNS-rebinding gate is measured against. Stdlib only.

The point of the modes is that several of them look identical from the outside
unless you probe the right way. ``loopback_only`` in particular refuses
``evil.example.com`` just as convincingly as a correctly configured
``allowlist`` does — which is the whole reason the gate probes the allowed
hostname on a wrong port and the allowed hostname on the right one, and reads
the pair rather than any single answer.

``REBIND_FIXTURE_MODE``:

  ``allowlist``     the control done right: ``MCP_ALLOWED_HOSTS`` honoured
                    port-exactly, ``MCP_CORS_ORIGINS`` honoured, and the host
                    check runs BEFORE authentication.
  ``ignores``       reads no allow-list at all — the documented fail-open state
                    of a server that never shipped the knob.
  ``hostname_only`` honours the variable but compares only the hostname, so the
                    port travels with the entry and is then thrown away.
  ``no_origin``     port-exact host list, ``Origin`` never looked at.
  ``auth_first``    a valid token short-circuits the host check. The server has
                    authentication and calls it a rebinding control.
  ``loopback_only`` ignores the variable and allows only loopback names — the
                    fallback policy that rejects an attacker's hostname without
                    any of the operator's configuration being in force.

``REBIND_FIXTURE_IGNORE_AUTH=1`` makes the server ignore ``MCP_AUTH_TOKEN``
entirely — the case where the token pass proves less than it appears to.

``REBIND_FIXTURE_TRANSPORT`` selects ``http`` (streamable, POST) or ``sse``
(GET handshake). ``REBIND_FIXTURE_SSE_ENDLESS=1`` makes an accepted SSE stream
never end, which is what a server with no allow-list really does to a hostile
GET — a gate that read the stream to its end would hang on exactly the case it
is there to measure.

Binds ``HOST``/``PORT`` from the environment, like a real target's entrypoint.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("REBIND_FIXTURE_MODE", "allowlist")
TRANSPORT = os.environ.get("REBIND_FIXTURE_TRANSPORT", "http")
PATH = os.environ.get("REBIND_FIXTURE_PATH", "/mcp/")
SSE_PATH = os.environ.get("REBIND_FIXTURE_SSE_PATH", "/sse/")
ENDLESS = os.environ.get("REBIND_FIXTURE_SSE_ENDLESS") == "1"

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1"}

TOOLS = [
    {"name": "health", "description": "liveness", "inputSchema": {"type": "object"}},
    {"name": "record_count", "description": "count", "inputSchema": {"type": "object"}},
]


def _entries(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _hostname(value: str) -> str:
    """A Host entry minus its port; an IPv6 literal keeps its brackets."""
    value = (value or "").strip()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # keep test output clean
        pass

    # --- the controls under test -----------------------------------------

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if MODE == "ignores":
            return True
        if MODE == "loopback_only":
            return _hostname(host) in LOOPBACK_NAMES
        allowed = _entries(os.environ.get("MCP_ALLOWED_HOSTS", ""))
        if not allowed:
            return True  # nothing configured -> nothing enforced
        if MODE == "hostname_only":
            return _hostname(host) in {_hostname(a) for a in allowed}
        return host in allowed

    def _origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or MODE in ("ignores", "no_origin", "loopback_only"):
            return True
        allowed = _entries(os.environ.get("MCP_CORS_ORIGINS", ""))
        return not allowed or origin in allowed

    def _token_ok(self) -> bool:
        # REBIND_FIXTURE_IGNORE_AUTH: the target never reads the token variable.
        # The gate must then say so rather than claim the host check outranked an
        # auth layer that was never there.
        if os.environ.get("REBIND_FIXTURE_IGNORE_AUTH") == "1":
            return True
        expected = os.environ.get("MCP_AUTH_TOKEN", "")
        if not expected:
            return True
        presented = (self.headers.get("Authorization") or "").strip()
        return presented == f"Bearer {expected}"

    def _refusal(self) -> tuple[int, bytes] | None:
        """The refusal this request earns, or None to serve it."""
        # auth_first is the bug: a valid token is taken as sufficient and the
        # host is never looked at again.
        if MODE == "auth_first" and os.environ.get("MCP_AUTH_TOKEN") and self._token_ok():
            return None
        if not self._host_allowed():
            return 421, b'{"error":"Invalid request host"}'
        if not self._origin_allowed():
            return 400, b'{"error":"Invalid request origin"}'
        if not self._token_ok():
            return 401, b'{"error":"unauthorized"}'
        return None

    # --- plumbing ---------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str = "application/json",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if TRANSPORT != "sse" or self.path.rstrip("/") != SSE_PATH.rstrip("/"):
            self._send(405, b'{"error":"method not allowed"}')
            return
        refusal = self._refusal()
        if refusal:
            self._send(refusal[0], refusal[1], "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"event: endpoint\ndata: /messages/?session=fixture\n\n")
            self.wfile.flush()
            while ENDLESS:
                # What a real SSE endpoint does to an accepted GET: keep the
                # stream open. The gate must return anyway.
                time.sleep(0.5)
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if TRANSPORT == "sse" and self.path.startswith("/messages"):
            refusal = self._refusal()
            if refusal:
                self._send(refusal[0], refusal[1], "text/plain")
                return
            self._send(202, b"")
            return
        if self.path.rstrip("/") != PATH.rstrip("/"):
            self._send(404, b'{"error":"not found"}')
            return
        refusal = self._refusal()
        if refusal:
            self._send(refusal[0], refusal[1], "text/plain")
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
            payload = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rebind-fixture", "version": "1"},
            }}
            body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
            self._send(200, body, "text/event-stream", {"Mcp-Session-Id": "fixture-session"})
            return
        if method == "tools/list":
            payload = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": TOOLS}}
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        payload = {"jsonrpc": "2.0", "id": msg.get("id"),
                   "error": {"code": -32601, "message": f"unknown method {method}"}}
        self._send(200, json.dumps(payload).encode(), "application/json")


def main() -> int:
    host = os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("PORT") or "0")
    if not port:
        print("rebind_http_server: PORT is required", file=sys.stderr)
        return 2
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
