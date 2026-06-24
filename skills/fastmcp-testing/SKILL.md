---
name: fastmcp-testing
description: Write and run async tests for a FastMCP server using pytest-asyncio and recorded fixtures (AsyncMock over httpx) instead of live network calls.
requires:
  bins: [uv, pytest]
---

# FastMCP Testing

- Use `pytest-asyncio`; mark async tests with `@pytest.mark.asyncio`.
- NEVER hit live municipal APIs in tests. Intercept outbound `httpx` requests
  with `unittest.mock.AsyncMock` and inject recorded JSON fixtures from
  `tests/fixtures/`.
- Prefer the FastMCP in-memory client to call tools directly.
- A test that depends on the network is a bug — make it deterministic.

## Pattern
1. Record a real response once into `tests/fixtures/<tool>.json`.
2. Mock `httpx` to return it.
3. Assert the tool output validates against the generated schema in `schemas/`.
   A schema mismatch is upstream drift — open an issue, do not silently adapt.
