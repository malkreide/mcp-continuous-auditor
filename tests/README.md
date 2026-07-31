# Tests

Two tiers, so the default run needs nothing but the standard library.

## Default (stdlib only)

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Covers:

- `test_budget_guard.py` — the Phase-5 budget guardrails.
- `test_nightly_audit_report.py` — the audit classifier (unit level).
- `test_sync_findings_issues.py` — deterministic findings→issue routing: which
  issues a summary implies + open-vs-comment dedup, no network (Analysis U-C).
- `test_broker_pipeline.py` — the **real** Broker handler
  (`deploy/microvm/channel/_receive-one.sh`) driven end-to-end: verdict
  re-derivation + the tar path-traversal guard (Analysis S2). Needs `bash` + `tar`.
- `test_egress_interlock.py` — the **real** egress interlock
  (`deploy/microvm/_egress-interlock.sh`): fail-closed without the nft allowlist
  (Analysis S3). Needs `bash` + `nft`.
- `test_audit_cycle.py` — the **real** Broker orchestrator
  (`deploy/microvm/run-audit-cycle.sh`) with a fake worker: the budget breaker is
  fed the worker's outcome, and a missing result is recorded as hard-fail
  (Analysis T-B). Needs `bash`.
- `test_promptfoo_profiles.py` — the split promptfoo profiles are structurally
  correct: determ is key-less, graded carries the model layer + committed
  red-team, the generative spec is isolated (Analysis T-C / T-A). Needs `PyYAML`;
  self-skips without it.
- `test_improve_loop.py` — the **real** Phase-6c orchestrator
  (`scripts/improve-loop.sh`) end-to-end with a fake writer queue, the fake
  suite runner and a local git target: keeps are committed (only under
  `promptfoo/`), discards journaled, per-iteration budget records land in the
  improve-own state, writer crash / flaky baseline hard-fail the run, and the
  keeps ceiling stops early. Needs `bash` + `git`.
- `test_improve_writer.py` — the Phase-6c writer (`scripts/improve_writer.py`)
  with an injected API transport: exit contract 0/10/1, fence unwrapping,
  refusal → graceful stop, token accounting, journal tail in the prompt. No
  network, no real key.
- `test_improve_loop_support.py` — report aggregation (keep/discard tallies,
  skip-lines run isolation, hard-fail outcome) and idempotent draft-PR
  publishing with an injected GitHub opener.
- `test_improve_acceptance.py` — the Phase-6 acceptance harness
  (`scripts/improve_acceptance.py`): a valid candidate is kept, a flaky one
  (D1) and a red-on-HEAD one (D2) are discarded, out-of-scope/invalid patches
  are discards, a flaky baseline or crashing runner is a HARD failure, the
  candidate is always reverted, and the journal is append-only. Phase 6b adds
  D3: in `schema-path` (lite) mode a duplicate/no-new-schema-ref candidate is
  discarded as `redundant`; in `mutation` mode a candidate is kept only if it
  kills a mutant surviving the existing suite (kill map cached per target SHA;
  empty or fully-killed pools HARD-fail). Needs `git`; the suite runner is a
  local fake and mutants are plain diffs — no promptfoo, no mutmut, no network.
- `test_transport_boot_probe.py` — the transport boot gate
  (`scripts/transport_boot_probe.py`), the only gate that observes the *running*
  process. Built around the two bugs no other gate can see: a crash at start
  under the new SDK (`boot_stdio_crash.py`, the read-only settings object) and an
  HTTP 421 for every request under a real hostname (`boot_http_server.py` in
  `host421` mode). One test deliberately proves that a loopback-only probe calls
  the 421 server healthy — that is why the gate varies the `Host` header. Another
  pins the stdin trap: the *same* healthy stdio server is measured as broken the
  moment stdin is closed after the write, so a regression there shows up as a
  test failure rather than as false findings against slow targets. The fixtures
  speak enough JSON-RPC on their own, so all of this is stdlib-only and offline;
  `FastMCPBootTest` is the one class needing `fastmcp` and self-skips without it.
- `test_release_gap.py` — the release-gap probe (`scripts/release_gap.py`).
  Hardest on the two properties whose failure would turn it into noise or into
  a lie: an unreachable PyPI must not read as "in sync", and a missing tag set
  (a `--depth 1` clone fetches none) must not read as "never released". The
  git-backed cases build a real repository in a temp dir rather than mocking
  `git log`, which would only assert that the mock matches the assumption.
  Needs `git`; no network — the index lookup is injected.

`test_smoke_target.py` and `test_transport_boot_probe.FastMCPBootTest`
self-**skip** here — both need `fastmcp`.

## With fastmcp (the smoke target, finding U-B)

`test_smoke_target.py` runs `schemas/generate_schemas.py` and
`promptfoo/providers/call_tool.py` against `tests/fixtures/smoke_server.py`, a
tiny local FastMCP server — the two code paths that otherwise only run in the
external target repo. Provide fastmcp, e.g. with uv:

```bash
uv run --with fastmcp python -m unittest tests.test_smoke_target
```

No network is used: the provider's httpx call is mocked against
`tests/smoke_fixtures/`.

`test_transport_boot_probe.FastMCPBootTest` joins it there: it boots the same
`smoke_server.py` for real, over stdio and over streamable-http, and speaks
`initialize` + `tools/list` to it. The stdlib fixtures prove the probe's logic;
this proves it against the actual SDK, so a change in FastMCP's startup or
transport handling (the 307 from `/mcp/` to `/mcp` was found exactly this way)
surfaces here instead of in a target repo at 03:00. Still no network — only
`tools/list` is called, never a tool.
