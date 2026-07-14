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

`test_smoke_target.py` self-**skips** here — it needs `fastmcp`.

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
