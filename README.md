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
- **Independent grader** — LLM-graded checks use a different model family than the writer.
- **Continuous red-teaming** — OWASP LLM Top 10 (prompt injection, PII leakage) against the MCP surface.
- **Human merge gate** — the agent opens PRs only; it never pushes to `main`.
- **Proactive** — a daily cron audit posts a report to Telegram.

## Prerequisites

- Node.js 20+ (OpenClaw, promptfoo)
- Python 3.11+ and [uv](https://github.com/astral-sh/uv)
- Docker (agent sandbox)
- A Telegram bot token (via [@BotFather](https://t.me/BotFather)) and your numeric Telegram user ID
- A fine-grained GitHub PAT scoped to the target repo (contents + pull-requests, **no** secrets)
- An Anthropic API key (independent grader)

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
promptfoo eval -c promptfoo/promptfooconfig.yaml
```

## Configuration

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOW_FROM` | Your numeric Telegram user ID (gating) |
| `ANTHROPIC_API_KEY` | Independent grader model |
| `GITHUB_TOKEN` | Fine-grained PAT, target repo, PR-only |
| `TARGET_REPO` | e.g. `malkreide/zurich-opendata-mcp` |

## Project Structure

```
openclaw/    OpenClaw gateway config + policy-as-code (SOUL/AGENTS/TOOLS)
skills/      python-auditor, fastmcp-testing, promptfoo-eval
promptfoo/   deterministic asserts, schema-drift, red-team
.github/     CI = the source of truth (template for the target repo)
docs/plans/  the v2 build plan
```

## Roadmap

Phase 0 baseline → 1 read-only auditor → 2 promptfoo CI gate → 3 PR-only writer → 4 cron + red-team → 5 hardening (forkd, TensorZero). See [docs/plans](docs/plans).

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

## License

MIT License — see [LICENSE](LICENSE)

## Author

Hayal Özkan · [malkreide](https://github.com/malkreide)
