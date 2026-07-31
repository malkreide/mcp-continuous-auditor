# Tests

Two tiers, so the default run needs nothing but the standard library.

## Default (stdlib only)

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Covers:

- `test_budget_guard.py` — the Phase-5 budget guardrails.
- `test_nightly_audit_report.py` — the audit classifier (unit level), including
  the two ways a gate can say nothing while looking like it said yes: a gate
  that **hung** (exit 124/137 from the gate timeout, named in the report and
  deliberately kept out of the finding classes — a timeout is not "ruff found
  problems") and a test gate that returned green having executed **0 tests**.
  `CountTestsTest` pins the parser both classes rest on against the literal
  shapes pytest and unittest emit, above all that unreadable output reads as
  *unknown* and never as zero.
- `test_shipped_probe.py` — the shipped-artifact gate
  (`scripts/shipped_probe.py`), which installs the target's package from PyPI and
  makes *that* prove it runs. The network half is not deterministically testable,
  so the module keeps it in three named seams — the index lookup, the install and
  the subprocess — and everything that decides anything lives outside them: these
  tests own the version comparison, the publication states (absent ≠ stale ≠ index
  ahead), the tool-result classification and the finding set, with the seams
  injected. The one thing **not** faked is the stdio conversation: it runs against
  a real subprocess, because the stdin trap is a *timing* property no fake can
  reproduce, and the fixture delays its `tools/call` answer precisely so closing
  stdin early fabricates a failure. Two tests guard the muting risk from both
  sides — an error that reads like the sandbox's egress raises nothing, while an
  empty content list (the incident's own shape) is never excused as one. A last
  pair asserts the Worker's proxy allowlist still permits the index and that the
  credential-holding Broker's has *not* been widened to match.
- `test_portfolio_scan.py` — the portfolio fan-out (`scripts/portfolio_scan.py`).
  Built around the three properties that would have caught the nested server
  left on the old SDK: `nested_manifests` flags an unclaimed manifest below the
  root **fail-closed** (a heuristic that only flagged server-shaped ones would
  let through the one that does not match the heuristic — the same bet that lost
  the first time); the outlier pass finds the target that disagrees with the
  majority **with no expectation configured**, because mid-migration nobody
  knows which version is right until they see fourteen agree and one not; and an
  unreachable target yields a row of "could not run" cells while the sweep
  continues, with `incomplete` outranking `findings` so a partial run can never
  read as a clean bill. One test hides PyYAML and asserts the stdlib subset
  reader parses the committed `targets.example.yaml` identically — the Worker has
  no PyYAML, and a targets file that parses differently there drops a server
  from the sweep, which is this module's own failure mode turned on itself.
  Offline: targets carry a local `path:` so nothing is cloned.
- `test_gate_timeouts.py` — the time bounds in the **real**
  `scripts/nightly-audit.sh`. The committed `run_bounded` helper is lifted out of
  the script and driven in bash (124 on a hang, 137 when `SIGTERM` is ignored,
  and the whole process group dying — a gate is `uv run pytest`, so the process
  that hangs is a grandchild). The rest is structural: every gate invocation,
  the promptfoo eval and the provisioning `git` calls must go through a bounded
  launcher. That is the regression it mostly exists for — the natural way to
  lose a time bound is not to break the helper but to add a gate next year and
  call it directly. Needs `bash` + `timeout`.
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
- `test_rebind_probe.py` — the DNS-rebinding gate (`scripts/rebind_probe.py`),
  which boots the target with an inbound `Host`/`Origin` allow-list configured
  and then tries to walk past it. The fixture
  (`tests/fixtures/rebind_http_server.py`) ships six servers that are hard to
  tell apart from outside, and the tests exist to keep the gate from collapsing
  any two of them into one answer. Two carry the design: `loopback_only` refuses
  the attacker's hostname exactly as convincingly as a correct server does, so a
  probe against `evil.example.com` alone would have called it protected — and
  against `hostname_only` probes 1, 3 and 4 answer *identically* to the healthy
  server, so the wrong-port probe is the only thing separating a loose allow-list
  from a strict one. A third pins the token case: `auth_first` holds the host
  check for anonymous requests and folds the moment a valid token appears, which
  is authentication wearing the control's name. The gate's third outcome —
  *control not configured*, exit 3 — is asserted to be neither a pass nor a
  finding, and `FastMCPRebindTest` proves a vanilla FastMCP server lands there
  rather than producing a false alarm.
- `test_release_gap.py` — the release-gap probe (`scripts/release_gap.py`).
  Hardest on the two properties whose failure would turn it into noise or into
  a lie: an unreachable PyPI must not read as "in sync", and a missing tag set
  (a `--depth 1` clone fetches none) must not read as "never released". The
  git-backed cases build a real repository in a temp dir rather than mocking
  `git log`, which would only assert that the mock matches the assumption.
  Needs `git`; no network — the index lookup is injected.

`test_smoke_target.py`, `test_transport_boot_probe.FastMCPBootTest` and
`test_rebind_probe.FastMCPRebindTest` self-**skip** here — all three need
`fastmcp`.

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

`test_rebind_probe.FastMCPRebindTest` joins them: a vanilla FastMCP server on a
non-loopback bind is the fail-open case, and the gate must report it as *control
not configured* rather than as a finding. If that ever flips, the SDK changed its
default transport-security posture — and we want to hear it here, not from a real
target at 03:00.

`test_transport_boot_probe.FastMCPBootTest` joins it there: it boots the same
`smoke_server.py` for real, over stdio and over streamable-http, and speaks
`initialize` + `tools/list` to it. The stdlib fixtures prove the probe's logic;
this proves it against the actual SDK, so a change in FastMCP's startup or
transport handling (the 307 from `/mcp/` to `/mcp` was found exactly this way)
surfaces here instead of in a target repo at 03:00. Still no network — only
`tools/list` is called, never a tool.
