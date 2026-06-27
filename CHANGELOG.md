# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
