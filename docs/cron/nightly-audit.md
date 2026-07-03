# Nightly audit cron (Plan Phase 4)

A daily 03:00 OpenClaw cron job that audits the target MCP server unattended and
reports to Telegram. It is the proactive layer of the plan: *"Morgens ungefragt
ein Audit-Report auf Telegram, Findings als Issues."*

Two halves, by design:

| Half | Artifact | Responsibility |
|---|---|---|
| **Truth** | `scripts/nightly-audit.sh` + `scripts/nightly_audit_report.py` | Run the deterministic gates, classify the result, decide the exit code. |
| **Routing** | the OpenClaw isolated agent (`openclaw/cron/nightly-audit.json`) | Read the result, open/​update issues, gate the PR on a human OK, announce. |

The agent never overrides the exit code — the CI gates are the source of truth,
never an opinion (`openclaw/workspace/SOUL.md`).

## Flow

```
03:00  cron fires (isolated turn)
  └─ bash scripts/nightly-audit.sh
       1. git pull target (read-only, no push)      ← AGENTS.md / TOOLS.md
       2. ruff + mypy + pytest
       3. schema-drift gate  (generate_schemas.py --check)
       4. promptfoo eval     (tool-output contract + OWASP red-team)
       5. write .audit/nightly-report.md + .audit/nightly-summary.json
       └─ exit 0 green | 2 findings | 1 hard-fail
  └─ agent reads the summary and routes:
       exit 1 → announce the hard-failure report, STOP (no issue, no PR)
       exit 0 → announce "all green"
       exit 2 → open/update issue(s): schema-drift and/or redteam
                announce the report, which asks: "reply OK to authorise a draft PR"
  └─ --announce delivers .audit/nightly-report.md to Telegram
```

### The exit-code contract

`scripts/nightly_audit_report.py` reduces the five gate exit codes plus the
promptfoo JSON to one outcome:

- **0 green** — every gate passed.
- **2 findings** — schema drift (the deterministic schema gate diverged, or a
  promptfoo `is-json`/contract assert failed), a red-team hit (an OWASP plugin
  case succeeded against the surface), and/or a red ruff/mypy/pytest.
- **1 hard-fail** — a gate could not *run*: a missing binary, a failed
  `uv sync`, promptfoo could not start, or **promptfoo reported provider/model
  errors**. An unresolvable or unauthorised model is a hard failure, never a
  silent pass (see below).

`schema_drift` and `redteam` are surfaced as separate booleans in
`nightly-summary.json` so the agent opens one issue per finding class with the
right label.

## The two human gates

1. **Issues are automatic, PRs are not.** On findings the agent opens or updates
   a tracking issue (labels `schema-drift` / `redteam`, mirroring the weekly
   `live-probe` job). It does **not** open a PR. The announced report ends with
   *"reply `OK` to authorise a draft PR"*. The draft PR is created only in a
   **later** turn, after your explicit Telegram OK, on a branch `fix/<slug>`,
   never on `main` (`openclaw/workspace/AGENTS.md`). A cron turn is a single
   fresh turn — it cannot block waiting hours for approval, so the approval is a
   separate, human-initiated message.

2. **Merge is always yours.** The PR is a draft; CI is the pass/fail oracle; you
   merge.

## Hard-fail on an unresolvable model

Required behaviour: *"Bei nicht aufloesbarem Modell: hart fehlschlagen, nicht
still ausweichen."* Enforced at three layers:

- **The OpenClaw job** sets an explicit `payload.model` and `payload.fallbacks:
  []` (strict). OpenClaw then *"fails the run with an explicit validation error
  instead of silently falling back"* when the model cannot be resolved.
- **`install.sh`** *requires* `OPENCLAW_AUDIT_MODEL` (no default) and passes
  `--fallbacks ""`, so the job can never be registered against an implicit model.
- **The eval** — if the promptfoo grader/provider model is unresolvable, the
  aggregator counts the provider errors and returns exit **1**; the agent
  announces the hard failure and opens nothing. It never reports the surface as
  "safe" just because the red-team could not run.

## Install

On the gateway host (the dedicated Pi / VM — see
`docs/deployment/raspberry-pi.md`), from the repo root:

```bash
OPENCLAW_AUDIT_MODEL="anthropic/claude-opus-4-6" \   # explicit, resolvable; see note
TELEGRAM_ANNOUNCE_TO="123456789" \                   # your Telegram chat/user id
  openclaw/cron/install.sh
```

- `DRY_RUN=1` prints the exact `openclaw cron …` command without running it.
- `OPENCLAW_CRON_REPLACE=1` replaces an existing `nightly-audit` job.
- For a Telegram forum topic, use `TELEGRAM_ANNOUNCE_TO="-1001234567890:topic:42"`.

> **Writer ≠ checker.** Keep `OPENCLAW_AUDIT_MODEL` a *different* model family
> than the promptfoo grader (`defaultTest.options.provider`, default
> `openai:gpt-4o-mini`; override with `GRADER_PROVIDER`) so the agent interpreting
> the audit is not the model grading it (README "Independent grader"). The writer
> is Anthropic, so the grader must NOT be an Anthropic model.

`openclaw/cron/nightly-audit.json` is the version-controlled source of truth for
the schedule and the agent prompt; `install.sh` reads it and fills the `${…}`
placeholders from the environment. OpenClaw has no config-file import for cron
jobs, so the CLI registration is how the spec goes live.

### Prerequisites on the host

- The repo is checked out and the cron runs from its root (the agent calls
  `scripts/nightly-audit.sh` by repo-relative path).
- `git`, `uv`, `node`/`npx`, `python3` on `PATH`.
- Egress allowlist permits the target repo origin and the Zürich endpoints
  promptfoo's fixtures were recorded against (`TOOLS.md`).
- The GitHub PAT is scoped to the target repo with **issues: write** (to file
  the drift/red-team tickets) in addition to contents + pull-requests.

## Manage

```bash
openclaw cron list                 # confirm it is registered
openclaw cron run nightly-audit    # trigger once now (smoke test)
openclaw cron edit  <id> --model "<ref>" --fallbacks ""   # change model, stay strict
openclaw cron remove <id>          # uninstall
```

## Relationship to the other jobs

- **`ci.yml`** (per-PR, in the target repo) is the merge gate. The nightly job
  does not replace it — it watches `main` between PRs.
- **`live-probe.yml`** (weekly, in the target repo) probes the *live* Zürich
  endpoints for structural drift. The nightly job runs the *offline*
  deterministic suite (recorded fixtures, no live calls) every day.

Outputs land in the gitignored `.audit/` work dir; nothing here is committed by
the job.
