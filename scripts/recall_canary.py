#!/usr/bin/env python3
"""Recall canary — call the server's OWN tools live and assert a floor.

Why this exists next to live_probe.py, which already talks to the live APIs:
live_probe hits **raw upstream URLs**. That verifies the endpoint, not the
server. A recall bug usually lives in the layer between the two — in how the
server builds its request — and a raw-URL probe is blind to it by construction.

termdat-mcp#11 is the worked example. The upstream API was fine throughout. The
server sent an optional scope parameter only when the caller supplied one, and
the API restricts an ID-less search to one of 23 classifications, so every
default search covered a 23rd of the corpus and reported the shortfall as an
ordinary empty result. Probing the URL directly would have shown a healthy
endpoint; probing the tool shows the truth.

So this script drives the target server through the FastMCP **in-memory client**
with the network left ALIVE — the same in-process path promptfoo uses via
promptfoo/providers/call_tool.py, minus the httpx mock. What it measures is the
full chain the user actually gets: tool arguments → request construction →
upstream → parsing → result.

Canaries are declared in scripts/recall_canary.manifest.json:

    {"canaries": [
      {"name": "search_common_term",
       "tool": "search_terms",
       "args": {"search_term": "Pensionskasse"},
       "min_count": 10,
       "count_path": "entries"}
    ]}

Pick floors at roughly half the observed value: the canary must catch a collapse
from 21 to 1, not go red on routine corpus maintenance. A check that cries wolf
gets muted, and a muted check catches nothing.

Requires `fastmcp` and an importable target server, so — like live-probe.yml —
this belongs in the TARGET repo's workflow, not the auditor's CI.

  MCP_SERVER_IMPORT   "package.module:attr"  (default "server:mcp")
  RECALL_REPORT       report path            (default recall-canary-report.md)
  RECALL_MANIFEST     manifest path          (default alongside this script)

Exit code is always 0 — a flaky upstream must not fail the cron. Findings are
signalled via $GITHUB_OUTPUT: `recall_drop`, `errors`, `alert`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_MANIFEST = Path(
    os.environ.get("RECALL_MANIFEST", _HERE / "recall_canary.manifest.json")
)
_SERVER_REF = os.environ.get("MCP_SERVER_IMPORT", "server:mcp")
_TIMEOUT = int(os.environ.get("RECALL_CANARY_TIMEOUT", "60"))

# Reuse live_probe's counting so a floor means the same thing in both jobs.
sys.path.insert(0, str(_HERE))
from live_probe import count_records  # noqa: E402


def _load_server() -> Any:
    mod_name, _, attr = _SERVER_REF.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, attr or "mcp")


def _tool_payload(result: Any) -> Any:
    """Parse a FastMCP CallToolResult into plain JSON.

    Mirrors promptfoo/providers/call_tool.py: the text block carries the
    structured output for dict-returning tools; fall back to the structured
    attributes when a tool returns Markdown or the block is absent.
    """
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return text
    for attr in ("structured_content", "structuredContent", "data"):
        value = getattr(result, attr, None)
        if value is not None:
            return value
    return content


# TWO DIFFERENT THINGS ARE CALLED FASTMCP and they cannot share an environment:
# (a) the official `mcp` SDK, whose 2.x server class is
# `mcp.server.mcpserver.MCPServer` (renamed from `mcp.server.fastmcp.FastMCP`)
# and whose client is `mcp.client.client.Client`; and (b) the separate PyPI
# project `fastmcp`, whose `fastmcp.Client` is still correct and was NOT renamed.
# `fastmcp` still needs `mcp` 1.x, so a hard import of either makes this canary
# unrunnable against half the portfolio. Dispatch on the server object's module —
# see schemas/generate_schemas.py for the long version.


def _sdk_client(server: Any) -> Any:
    from mcp.client.client import Client  # (a) — mcp >= 2

    return Client(server)


def _fastmcp_client(server: Any) -> Any:
    from fastmcp import Client  # (b) — the standalone package

    return Client(server)


def in_memory_client(server: Any) -> Any:
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
        "no in-memory client available for "
        f"{type(server).__module__}.{type(server).__name__}: " + "; ".join(problems)
    )


async def _call(mcp: Any, tool: str, args: dict) -> Any:
    async with in_memory_client(mcp) as client:
        result = await asyncio.wait_for(client.call_tool(tool, args), timeout=_TIMEOUT)
    return _tool_payload(result)


def evaluate(
    canaries: list[dict], caller: Any
) -> tuple[list[str], list[str], list[str]]:
    """Run every canary. Returns (recall_rows, error_rows, ok_rows).

    `caller` is a callable (name, args) -> payload, injected so the tests can
    drive this without a live server.
    """
    recall_rows: list[str] = []
    error_rows: list[str] = []
    ok_rows: list[str] = []

    for canary in canaries:
        name = canary["name"]
        tool = canary["tool"]
        floor = canary["min_count"]
        try:
            payload = caller(tool, canary.get("args", {}))
        except Exception as exc:  # noqa: BLE001 — one bad canary must not stop the rest
            error_rows.append(
                f"- ⚠️ `{name}`: call failed — `{type(exc).__name__}: {exc}`"
            )
            continue

        count = count_records(payload, canary.get("count_path"))
        if count is None:
            where = canary.get("count_path") or "inferred collection key"
            error_rows.append(
                f"- ⚠️ `{name}`: no countable collection in the tool output "
                f"({where}) — fix `count_path` in the manifest."
            )
        elif count < floor:
            recall_rows.append(
                f"- 📉 `{name}` (`{tool}`): **{count}** record(s), floor is **{floor}**."
            )
        else:
            ok_rows.append(f"- ✅ `{name}` (`{tool}`) — {count} ≥ floor {floor}")

    return recall_rows, error_rows, ok_rows


def build_report(
    canaries: list[dict],
    recall_rows: list[str],
    error_rows: list[str],
    ok_rows: list[str],
) -> str:
    report = [
        "# Recall-canary report\n",
        f"Called {len(canaries)} tool(s) against the live upstream.\n",
    ]
    if recall_rows:
        report.append("## 📉 Recall below floor\n")
        report.append(
            "A tool returned fewer records than its floor. The upstream endpoint "
            "may be perfectly healthy — this job measures the whole chain, so the "
            "cause is just as likely in how the server builds its request.\n\n"
            "First check: for every optional filter/scope parameter, compare a call "
            "with the parameter omitted against one with it explicitly maximal. A "
            "changed upstream default narrows the result set without changing "
            "anything the server can see.\n"
        )
        report.extend(recall_rows)
        report.append("")
    if error_rows:
        report.append("## ⚠️ Canary errors (transient upstream, or a manifest bug)\n")
        report.extend(error_rows)
        report.append("")
    if ok_rows:
        report.append("## Above floor\n")
        report.extend(ok_rows)
        report.append("")
    return "\n".join(report)


def main() -> int:
    if not _MANIFEST.exists():
        print(
            f"No recall-canary manifest at {_MANIFEST} — nothing to do.",
            file=sys.stderr,
        )
        return 0

    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    canaries = manifest["canaries"] if isinstance(manifest, dict) else manifest
    if not canaries:
        print("Recall-canary manifest is empty — nothing to do.", file=sys.stderr)
        return 0

    mcp = _load_server()

    def caller(tool: str, args: dict) -> Any:
        return asyncio.run(_call(mcp, tool, args))

    recall_rows, error_rows, ok_rows = evaluate(canaries, caller)
    report_text = build_report(canaries, recall_rows, error_rows, ok_rows)

    report_path = Path(os.environ.get("RECALL_REPORT", "recall-canary-report.md"))
    report_path.write_text(report_text, encoding="utf-8")
    print(report_text)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"recall_drop={'true' if recall_rows else 'false'}\n")
            fh.write(f"errors={'true' if error_rows else 'false'}\n")
            fh.write(f"alert={'true' if recall_rows else 'false'}\n")
            fh.write(f"report_path={report_path}\n")

    return 0  # a flaky upstream must not fail the cron


if __name__ == "__main__":
    raise SystemExit(main())
