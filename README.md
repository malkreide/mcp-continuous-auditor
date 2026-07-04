# MCP Continuous Auditor

![Version](https://img.shields.io/badge/version-0.1.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11+-blue)
![Node](https://img.shields.io/badge/node-20+-green)

> A persistent, multi-agent auditor that continuously tests and hardens MCP servers — with promptfoo + CI as the deterministic source of truth, never an LLM's opinion.

[🇩🇪 Deutsche Version](README.de.md)

## Overview

This project runs a continuous auditor for [MCP](https://modelcontextprotocol.io) servers, starting with [`zurich-opendata-mcp`](https://github.com/malkreide/zurich-opendata-mcp). An [OpenClaw](https://docs.openclaw.ai) gateway exposes the auditor on Telegram as a control plane. Unlike a "vibecoding" agent, verification is a **versioned artifact** (pytest + promptfoo running in GitHub Actions), and a human is always the merge gate.

## Features

- **Read-only first** — the agent reports before it ever writes.
- **Deterministic ground truth** — promptfoo YAML asserts + JSON-schema drift checks, run in CI.
- **Independent grader** — LLM-graded checks use a genuinely different model *family* than the writer (writer is Anthropic → grader defaults to `openai:gpt-4o-mini`, or a local Ollama model), so a correlated blind spot can't pass its own output.
- **Continuous red-teaming** — OWASP LLM Top 10 (prompt injection, PII leakage) against the MCP surface.
- **Human merge gate** — the agent opens PRs only; it never pushes to `main`.
- **Proactive** — a daily cron audit posts a report to Telegram.

## Prerequisites

- Node.js 20+ (OpenClaw, promptfoo)
- Python 3.11+ and [uv](https://github.com/astral-sh/uv)
- Docker (agent sandbox)
- A Telegram bot token (via [@BotFather](https://t.me/BotFather)) and your numeric Telegram user ID
- A fine-grained GitHub PAT scoped to the target repo (contents + pull-requests + **issues**, **no** secrets) — `issues: write` is required for the nightly findings flow (it files the schema-drift / red-team tickets)
- An Anthropic API key (writer / tool-provider family) **and** an independent grader of a *different* family — an OpenAI key (default `openai:gpt-4o-mini`) or a local Ollama model (`GRADER_PROVIDER=ollama:chat:llama3.1`, no cloud key)

## Installation

```bash
git clone https://github.com/malkreide/mcp-continuous-auditor.git
cd mcp-continuous-auditor
cp .env.example .env        # fill in tokens
npm i -g openclaw promptfoo # or use npx
```

## Usage / Quickstart

```bash
# 1. Start the gateway (reads openclaw/openclaw.json)
openclaw start --config openclaw/openclaw.json

# 2. On Telegram, message your bot:
#    audit
#    -> returns a ruff/mypy/pytest report, read-only, no code changes

# 3. Run the deterministic verification locally
#    key-less profile (no model key needed):
promptfoo eval -c promptfoo/promptfooconfig.determ.yaml
#    full graded profile (llm-rubric + red-team; needs a grader key):
promptfoo eval -c promptfoo/promptfooconfig.yaml
```

> promptfoo is split into two profiles at the credential boundary — a key-less
> **determ** profile (the credential-free Worker runs only this) and a **graded**
> profile (llm-rubric + red-team, needs a grader key). See [promptfoo/README.md](promptfoo/README.md).

## Configuration

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOW_FROM` | Your numeric Telegram user ID (gating) |
| `ANTHROPIC_API_KEY` | Writer / tool-provider family |
| `OPENAI_API_KEY` | Independent grader (default `openai:gpt-4o-mini`; a different family than the writer) |
| `GRADER_PROVIDER` | Optional grader override, e.g. `ollama:chat:llama3.1` (local, no cloud key) |
| `GITHUB_TOKEN` | Fine-grained PAT, target repo: contents + pull-requests + issues, **no** secrets |
| `TARGET_REPO` | e.g. `malkreide/zurich-opendata-mcp` |

## Deployment

The LLM inference runs in the cloud (Anthropic API) — locally, only the OpenClaw
orchestrator runs. Since that process holds the GitHub PAT + Anthropic key and
runs shell tools, the **recommended deployment** is a dedicated, network-isolated
device rather than your work PC.

**Start at Tier 0** — the whole auditor on one Linux box with OpenClaw's Docker
sandbox + the deterministic gates, **no** microVM / TensorZero. The security core
(read-only, PR-only, writer≠checker, deterministic truth, hard-fail discipline) is
already there; the heavier isolation tiers are optional and adopted one at a time.
See **[docs/deployment/tier-0.md](docs/deployment/tier-0.md)** for the tier table.

**Recommended host: a dedicated Raspberry Pi 5 (8 GB)** (still Tier 0 — a *host*
choice). A separate device adds a real hardware/network isolation layer on top of
the Docker sandbox and fine-grained PAT. See
**[docs/deployment/raspberry-pi.md](docs/deployment/raspberry-pi.md)** for the full
guide. Equivalent alternatives: a local Linux VM in its own subnet, or a cheap VPS.

Optional hardening tiers: host egress allowlist + forward-proxy → microVM
Broker/Worker split → TensorZero cost-cap (see the tier table).

## Project Structure

```
openclaw/         OpenClaw gateway config + policy-as-code (SOUL/AGENTS/TOOLS)
openclaw/cron/    nightly-audit cron job spec + installer (daily 03:00 → Telegram)
skills/           python-auditor, fastmcp-testing, promptfoo-eval
schemas/          generated tool-output JSON-Schemas = the drift detector
promptfoo/        deterministic asserts, schema-drift, red-team + recorded fixtures
scripts/          audit harness, live-probe, nightly-audit core, budget guard,
                  deterministic findings→issue routing, pinned-promptfoo installer
tensorzero/       Phase 5: LLM-gateway config + stack (cost-caps, A/B, audit-trail)
tests/            stdlib unit tests (budget guard)
.github/          CI = the source of truth (template for the target repo)
docs/plans/       the v2 build plan
docs/cron/        the daily nightly-audit cron (flow, model hard-fail, install)
docs/deployment/  Raspberry Pi (recommended host) + Phase 5 forkd/microVM isolation
docs/budget/      Phase 5 budget guardrails (token ceiling, circuit breaker)
docs/observability/ Phase 5 TensorZero gateway (cost-caps, A/B, audit-trail)
```

## Roadmap

Phase 0 baseline → 1 read-only auditor → 2 promptfoo CI gate → 3 PR-only writer → 4 cron + red-team → 5 hardening (forkd, TensorZero). See [docs/plans](docs/plans).

> **Phase 3 status — the finding → fix → PR write path is agent-assisted and
> human-initiated, not an automated pipeline.** It is demonstrated end-to-end in
> [`examples/worker-tdd-demo/`](examples/worker-tdd-demo/) (RED test → fix → GREEN
> → PR) and governed by the TDD invariants in `openclaw/workspace/AGENTS.md`, but a
> Worker only cuts a `fix/<slug>` PR after your explicit Telegram OK, per finding —
> there is no committed automation that turns a finding into a PR on its own.

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

## License

MIT License — see [LICENSE](LICENSE)

## Author

Hayal Özkan · [malkreide](https://github.com/malkreide)
