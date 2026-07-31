---
name: fastmcp-testing
description: Write and run async tests for an MCP server (official `mcp` SDK or the standalone `fastmcp` package) using pytest-asyncio and recorded fixtures (AsyncMock over httpx) instead of live network calls.
requires:
  bins: [uv, pytest]
---

# MCP server testing (in-memory client + recorded fixtures)

> **The name is ambiguous — read this before you rename anything.** Two
> different projects are called FastMCP, and they cannot be installed in the
> same environment:
>
> | | package | server class | in-memory client |
> |---|---|---|---|
> | **(a)** the official SDK | `mcp` | `mcp.server.mcpserver.MCPServer` — **renamed** from `mcp.server.fastmcp.FastMCP` in the 2.0 break, which removed the old module with no shim | `mcp.client.client.Client` |
> | **(b)** a separate project | `fastmcp` | `fastmcp.FastMCP` — **not** renamed, still current, on its own major line (3.x while the SDK is at 2.x) | `fastmcp.Client` |
>
> `fastmcp` still requires `mcp` 1.x, so in an environment holding `mcp` 2.x even
> `import fastmcp` raises `ImportError: cannot import name 'McpError'`. Find out
> which one the repo in front of you uses — `pyproject.toml` settles it — before
> touching an import. A blind search-and-replace across the two does damage.
>
> This skill's name is kept for continuity; it covers both.

- Use `pytest-asyncio`; mark async tests with `@pytest.mark.asyncio`.
- NEVER hit live municipal APIs in tests. Intercept outbound `httpx` requests
  with `unittest.mock.AsyncMock` and inject recorded JSON fixtures from
  `tests/fixtures/`.
- Prefer the **in-memory client of whichever SDK the server belongs to** for
  calling tools directly — `mcp.client.client.Client` for (a),
  `fastmcp.Client` for (b). Both take the server object and are used the same
  way; two shapes differ:
  - `list_tools()` returns a `ListToolsResult` in (a) and a plain list in (b) —
    read `getattr(listing, "tools", listing)`.
  - the output schema is `output_schema` in (a) and `outputSchema` in (b) —
    read both with a default so a pydantic model's `__getattr__` cannot raise.
- A test that depends on the network is a bug — make it deterministic.

## Pattern
1. Record a real response once into `tests/fixtures/<tool>.json`.
2. Mock `httpx` to return it.
3. Assert the tool output validates against the generated schema in `schemas/`.
   A schema mismatch is upstream drift — open an issue, do not silently adapt.
