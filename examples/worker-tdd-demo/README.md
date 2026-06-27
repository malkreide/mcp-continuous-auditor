# Worker TDD demo — RED → GREEN → PR

A runnable, end-to-end demonstration of the **Worker write protocol** in
[`openclaw/workspace/AGENTS.md`](../../openclaw/workspace/AGENTS.md):

- **(a)** changes land via a `fix/<slug>` branch + PR, never on `main`;
- **(b)** no new tool/resource without a **locally-green async test written first**;
- **(c)** `ruff` + `mypy` run (and pass) after every edit.

This is a self-contained stand-in for the target MCP server
(`zurich-opendata-mcp`); the auditor repo itself has no production `src/`.

## The one small issue

> **Add a `dataset_count` tool.** The demo server exposes only `health`. Operators
> want a one-call answer to "how many datasets does the Zürich CKAN catalogue
> currently list?" Add a FastMCP tool `dataset_count` that calls
> `package_list` and returns `{"count": <int>}`.

Tracked as a GitHub issue and closed by the demo PR.

## The loop (reproduce it)

```bash
cd examples/worker-tdd-demo
uv sync --extra dev          # fastmcp, httpx, pytest, pytest-asyncio, ruff, mypy
```

### 1. RED — write the async test first, watch it fail for the right reason

The test exists before the tool. With only `health` on the server:

```text
$ uv run pytest -q
>           raise ToolError(msg)
E           fastmcp.exceptions.ToolError: Unknown tool: 'dataset_count'
...
1 failed in 0.94s            # exit=1
```

`ruff check` and `mypy` are already green at this point — the failure is the
missing tool, not a typo. That is the correct RED.

### 2. GREEN — minimal implementation, then re-run until green

Add `dataset_count` to `src/zurich_opendata_demo/server.py`, then:

```text
$ uv run ruff check .        # exit=0
$ uv run mypy                # exit=0
$ uv run pytest -q
1 passed                     # exit=0
```

The test never hits the network: outbound `httpx` is patched to replay
`tests/fixtures/package_list.json` (the `fastmcp-testing` skill).

### 3. PR — open it, a human merges

Branch `fix/dataset-count-tool` → draft PR → CI is the gate → **you merge**.
No push to `main`; that path stays closed.

## Why each guardrail

| Guardrail | What it prevents |
|---|---|
| Branch + PR only | An agent silently rewriting `main`. |
| Test-first (RED before GREEN) | A tool that was never actually exercised, or a test that passes vacuously. |
| Mock the network | Flaky, non-deterministic tests that depend on a live municipal endpoint. |
| ruff + mypy after every edit | Style/type rot landing in review or CI instead of being fixed at the source. |
