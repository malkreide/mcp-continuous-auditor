# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — gateway-independent Telegram announce
- **`scripts/telegram_notify.py`** — a stdlib-only, one-way notifier that pushes
  an audit report to Telegram over the Bot API **without** an OpenClaw runtime,
  mirroring the self-contained notifier of the sibling `future-skills-evidence-graph`
  repo. OpenClaw stays the interactive control plane (commands, per-finding `OK`,
  cron `--announce`); this is the complement for hosts that produce a report but
  run no gateway (Tier-0 / a keyed operator run / a CI job / the trusted Broker).
  It is **no-op without `TELEGRAM_BOT_TOKEN` + a chat id** (`TELEGRAM_ANNOUNCE_TO`,
  else the first `TELEGRAM_ALLOW_FROM` id) and **best-effort** — every send error
  is a token-redacted warning and the exit code stays 0, so it can never turn a
  green/findings/hard-fail verdict into a crash. Deliberately **outbound only**:
  inbound commands remain OpenClaw's sandboxed job, so no second, less-guarded
  command path is added.
- **`scripts/nightly-audit.sh`** gained an **opt-in** final announce step
  (`TELEGRAM_NOTIFY=1`, default off) that runs *after* `outcome_rc` is captured
  and `|| true`, so the exit-code contract with the cron agent / Broker is
  untouched. No-op on the credential-free Worker, which never holds the token.
- New `tests/test_telegram_notify.py` (17 stdlib `unittest` tests) pinning the
  no-op-without-config, best-effort-exit-0, token-redaction, truncation and
  chat-id-resolution invariants. Docs: `docs/telegram/standalone-notify.md`;
  `.env.example` / README updated with `TELEGRAM_ANNOUNCE_TO` + `TELEGRAM_NOTIFY`.

### Security / Changed — hardening from the solution review (S1–S3, T2)
- **Broker-side classification (S2)** — the untrusted Worker microVM no longer
  ships a self-declared verdict. `scripts/nightly-audit.sh` now emits a raw
  `nightly-evidence.json` (gate exit codes) and the Worker sends only that + the
  promptfoo JSON over vsock; the **trusted Broker** re-derives the verdict with
  its own classifier (`nightly_audit_report.py --from-evidence`). Missing/garbled
  evidence classifies as **hard-fail, never green**, and an exit-code/promptfoo
  mismatch (forged all-zero exit codes) is caught — a compromised Worker can no
  longer forge a pass. New `tests/test_nightly_audit_report.py` (6 tests) pins the
  forgery-resistance. Also fixed a latent portability bug: the Broker's tar
  extraction used the non-portable `--no-absolute-names` (rejected by GNU tar 1.35);
  the path-traversal guard is the explicit exact-name member list, which is portable.
- **Worker egress interlock (S3)** — `deploy/microvm/run-worker.sh` refuses to boot
  unless the host egress allowlist is loaded, and runs qemu as a dedicated
  unprivileged UID so the ruleset actually binds (override `EGRESS_ALLOWLIST=off`
  for isolated dev hosts, loud warning). New `deploy/microvm/egress-allowlist.nft`
  (+ `apply-egress-allowlist.sh`) ships the nftables ruleset as code: DNS + web to
  the public internet only, host-LAN/link-local dropped, all other ports denied.
  Corrected the misleading `restrict=off` comment (SLIRP isolates the guest from
  the host LAN but does NOT limit which internet hosts it reaches — that is the
  host firewall's job) and the `restrict=on` example in `forkd-isolation.md` (it
  would sever the guest from the network entirely). `00-preflight.sh` now checks
  for `nft` + the loaded table.
- **Cross-family grader (S1)** — the llm-rubric grader now defaults to a genuinely
  DIFFERENT model family than the (Anthropic) writer: `openai:gpt-4o-mini`, or a
  local `ollama:chat:llama3.1` via `GRADER_PROVIDER` (zero cloud key), passed to
  promptfoo with `--grader`. Fixed the previous `anthropic:claude-sonnet-4-6` /
  `claude-haiku-4-5` defaults (same family as the writer — not an independent
  check) in `promptfoo/promptfooconfig.yaml`, `tensorzero/tensorzero.toml`, the CI
  template, `.env.example`, both READMEs, and the cron/Pi docs.
- **Pinned truth-engine (T2)** — CI and `nightly-audit.sh` no longer run
  `promptfoo@latest` (a deterministic gate must not reload a moving target). Pinned
  to `0.121.17` via a single `PROMPTFOO_VERSION` knob; bump deliberately.

### Added
- **Phase 5 rollout kit** (`deploy/`) — runnable scripts to actually deploy the
  hardening layers on a local Linux VM (Broker = host VM with OpenClaw +
  credentials + TensorZero; Worker = throwaway microVM per run, no credentials,
  vsock-only result channel):
  - `deploy/00-preflight.sh`: host readiness (nested KVM, qemu, socat,
    cloud-image-utils, envsubst, docker, vhost-vsock) — fails loud with a
    remediation line per blocker, changes nothing.
  - `deploy/tensorzero/up.sh` + `episode-tokens.sh`: bring up the gateway +
    ClickHouse, healthcheck + smoke an inference, and sum one run's tokens from
    ClickHouse so the per-run cost-cap uses the REAL writer+grader total.
  - `deploy/microvm/{build-worker-image.sh,run-worker.sh,worker-cloud-init.yaml.tmpl}`
    + `channel/broker-listener.sh`: build a Debian-cloud-image Worker + cloud-init
    seed, boot ONE throwaway microVM per run (fresh qcow2 overlay, discarded
    after), run `nightly-audit.sh` read-only inside it, and ship only
    summary.json + report.md back over vsock to the host dropbox. Received data
    is treated as untrusted (fixed filename extraction, no exec).
  - `scripts/nightly-audit.sh`: when `TENSORZERO_GATEWAY` is set, tags each run
    with an episode id and feeds the gateway's real per-run token total to
    `budget_guard` (falls back to the promptfoo count otherwise).
  - `docs/deployment/phase5-rollout.md`: the end-to-end runbook (preflight →
    TensorZero → Worker microVM → egress allowlist → cron cutover) with a
    "fertig wenn" gate per step and a one-line rollback. `.env.example` gained
    `TENSORZERO_GATEWAY` / `CLICKHOUSE_HTTP`; `.gitignore` excludes the generated
    images/overlays/seed.
- **Phase 5 hardening & scaling** (optional — budget guardrails implemented;
  forkd/TensorZero prepared as ops guides + config templates):
  - `scripts/budget_guard.py` + `tests/test_budget_guard.py`: the budget
    leitplanken as runnable, stdlib-only code (15 unit tests, `python3 -m
    unittest tests.test_budget_guard`). A **circuit breaker** (opens after N
    consecutive hard-fails or a budget breach; half-open trial after a cooldown;
    a green/findings run closes it), a **token ceiling** (per-run + rolling
    window, read from the promptfoo `--output` JSON), and validation/surfacing of
    the **max-iterations** knob. State lives atomically in the gitignored
    `.audit/budget-state.json`.
  - `scripts/nightly-audit.sh`: wired the guard in as step `0` (preflight — an
    open breaker writes a hard-fail-shaped report+summary and exits, so a skipped
    run is routed like any other "did not pass", never announced green) and step
    `6` (record — feeds the outcome + measured tokens back for the next run,
    without rewriting today's verdict). Opt out with `BUDGET_GUARD=0`.
  - `docs/budget/guardrails.md`: the three guardrails, the breaker state machine,
    the exit-code contract, and how max-iterations maps to OpenClaw/TensorZero.
  - `docs/deployment/forkd-isolation.md`: the microVM/KVM ops guide — the
    untrusted-reader vs. credential-holder two-VM split, both the x86
    (forkd/Cloud Hypervisor/Firecracker) and ARM64 (QEMU `microvm` on KVM) paths,
    the vsock channel invariant (results cross, never raw code), and a migration
    checklist. Marked optional / "erst wenn stabil".
  - `docs/observability/tensorzero.md` + `tensorzero/tensorzero.toml` +
    `tensorzero/docker-compose.yml`: the LLM gateway between OpenClaw and the
    provider — per-run cost-caps (episode-tagged token totals fed to the guard),
    A/B variants (writer vs. cheaper candidate, judged against the deterministic
    gate, never the model), and a ClickHouse audit-trail. Provider key lives only
    in the gateway env; bound to localhost.
  - `.env.example`: documented the optional `BUDGET_*`, `CLICKHOUSE_*` and
    `ANTHROPIC_BASE_URL` knobs (all with safe defaults / commented out).
- **Phase 4 nightly-audit OpenClaw cron** (daily 03:00 Europe/Zurich):
  - `scripts/nightly-audit.sh`: the deterministic core. Pulls the target
    read-only (git over HTTPS, no push), runs ruff + mypy + pytest, the
    schema-drift gate (`generate_schemas.py --check`) and the promptfoo eval
    (tool-output contract + OWASP red-team), then writes a concise report +
    `summary.json` under the gitignored `.audit/`. Exit code is the contract:
    `0` green / `2` findings / `1` hard-fail.
  - `scripts/nightly_audit_report.py`: classifies the gate exit codes + the
    promptfoo JSON into schema-drift vs red-team vs toolchain failure, and —
    crucially — separates a *finding* (a red eval) from an **unresolvable
    model/provider error**, which HARD-fails (exit 1) instead of being reported
    as a pass ("hart fehlschlagen, nicht still ausweichen").
  - `openclaw/cron/nightly-audit.json`: the version-controlled job spec
    (isolated session, explicit `model` + `fallbacks: []` so OpenClaw fails the
    run on an unresolvable model, `--announce` to Telegram) and the agent prompt
    that opens/updates `schema-drift`/`redteam` issues, gates any draft PR behind
    an explicit Telegram OK (branch `fix/<slug>`, never `main`), and pushes the
    report.
  - `openclaw/cron/install.sh`: idempotent registration via `openclaw cron
    create`. Requires an explicit `OPENCLAW_AUDIT_MODEL` (no default) and passes
    `--fallbacks ""`, so the job is never registered against a silent model.
  - `docs/cron/nightly-audit.md`: flow, the issue-auto/PR-gated-on-OK split, the
    three-layer model hard-fail, install + management.
- **Phase 2 deterministic verification artifacts** (target-repo templates):
  - `schemas/generate_schemas.py`: derives each tool's output JSON-Schema from
    its FastMCP return type via the in-memory client; `--check` mode fails CI if
    a committed schema drifts from the type hints. Plus `schemas/README.md` and
    a representative `schemas/zurich_datastore_sql.json` /
    hand-authored `schemas/geojson_featurecollection.json` (RFC 7946).
  - `promptfoo/providers/call_tool.py`: implemented the FastMCP in-memory
    provider — calls a tool (or reads a resource) with outbound `httpx` patched
    via `AsyncMock` against `promptfoo/fixtures/` (no live network) and returns
    the raw JSON. Server import + fixtures dir are env-configurable.
  - `promptfoo/fixtures/`: recorded upstream responses backing the contract and
    injection tests (datastore SQL, two GeoJSON layers, STRB, an IPI payload).
  - `promptfoo/promptfooconfig.yaml`: `is-json` contract checks for
    `zurich_datastore_sql` and the two GeoJSON layer surfaces, SQL/STRB injection
    negative-tests, an indirect-prompt-injection "data stays data" rubric (graded
    by an independent model family), and the `pii`/`prompt-injection`/
    `sql-injection` red-team block.
  - `.github/workflows/ci.yml.template`: the `promptfoo` job is documented as the
    REQUIRED check and now runs the schema-drift gate
    (`generate_schemas.py --check`) before the eval.
  - `.github/workflows/live-probe.yml.template` + `scripts/live_probe.py`
    (+ `scripts/live_probe.manifest.json`): weekly cron that queries the real
    Zürich endpoints once, compares response *structure* (not values) against the
    recorded fixtures, and opens/updates a single `schema-drift` tracking issue on
    divergence. Stdlib-only, never fails the cron on a flaky endpoint.

### Changed
- `docs/audits/2026-06-27.md`: replaced the *blocked* placeholder with the
  **real, completed** read-only audit of `zurich-opendata-mcp` v0.3.3 (run in a
  session with target access, folded back here as the canonical Phase-1 record).
  All gates green; 24 tools / 5 resources enumerated with `file:line`; P0 SQL
  surface confirmed clean (validators re-run offline); one P1 watch-item — an
  unescaped CQL passthrough in `zurich_geo_features.property_filter` (geo.py:100)
  — plus the broad mypy `ignore_errors` override flagged as a frozen type gate.

### Added
- `scripts/audit-target.sh`: provisioning + run harness that unblocks a real
  read-only audit. Clones/pins the target MCP repo into a gitignored `.audit/`
  work dir (read-only against the target — no writes, no push), runs
  `ruff`+`mypy`+`pytest`, and captures each exit code + log under
  `.audit/logs/`. Honors `TOOLS.md` (git-over-HTTPS only, no `curl | sh`, token
  never inlined). Must run on a host whose egress allowlist permits the target.
- `docs/audits/README.md`: how a real audit is produced (harness provisions +
  runs; the `python-auditor` agent interprets the logs and writes the report)
  and where it must run. `docs/audits/2026-06-27.md` §4 now points at the harness.
- `.gitignore`: ignore the `.audit/` work dir (cloned target + run logs).
- `openclaw/workspace/skills/python-auditor/SKILL.md`: workspace copy of the
  Phase-1 auditor skill (OpenClaw loads skills from the configured `workspace`).
  `requires.bins: [uv, ruff, mypy, pytest]`; runs ruff+mypy+pytest on every
  analysis and quotes the exact `file:line` from stderr on any non-zero exit.
  Report-only in Phase 1.
- `docs/audits/2026-06-27.md`: first read-only audit of `zurich-opendata-mcp`.
  Records the toolchain run as **blocked** (target source not present in the
  control-plane env / out of GitHub scope) — no pass/fail claimed without an
  observed exit code — and lays out the tool/resource priority matrix (P0
  SQL-injection for `zurich_datastore_sql`, P1 schema-validation for GeoJSON
  tools).
- `docs/deployment/raspberry-pi.md`: deployment guide for running the OpenClaw
  orchestrator on a dedicated, network-isolated **Raspberry Pi 5 (8 GB)** — now
  the recommended deployment for security reasons (hardware/network isolation of
  the credential-holding process from the work PC). Keeps both alternatives
  (local Linux VM, cheap VPS) with their trade-offs.

- `.env.example`, `.gitignore` and the CI template
  (`.github/workflows/ci.yml.template`) that 0.1.0 referenced but did not ship.
  The CI template targets the MCP-server repo (ruff + mypy + pytest + promptfoo)
  and is inert in the auditor repo by design.

### Changed
- Architecture (`docs/plans/2026-06-24-continuous-auditor-v2.md`): added a **Host
  layer** to the target architecture — dedicated Pi 5 as the recommended isolated
  host, with hardware isolation as the outermost of three security layers
  (host → Docker sandbox → forkd). Both READMEs (EN/DE) gained a **Deployment**
  section linking the guide.
- `openclaw/openclaw.json`: Telegram allowlist now resolves from the
  `TELEGRAM_ALLOW_FROM` env var via `${VAR}` substitution instead of a hardcoded
  numeric ID — no secrets in the repo. (`allowFrom` takes plain user IDs, not
  SecretRef objects, per the OpenClaw config docs.)

## [0.1.0] - 2026-06-24

### Added
- Initial scaffold: README (EN/DE), LICENSE, CHANGELOG, .gitignore, .env.example
- v2 build plan under docs/plans
- OpenClaw config + policy-as-code (SOUL.md, AGENTS.md, TOOLS.md)
- Skills: python-auditor, fastmcp-testing, promptfoo-eval
- promptfoo config scaffold + Python provider stub
- CI template (ruff + mypy + pytest + promptfoo)
