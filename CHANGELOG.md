# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `openclaw/openclaw.json`: Telegram allowlist now resolves from the
  `TELEGRAM_ALLOW_FROM` env var via `${VAR}` substitution instead of a hardcoded
  numeric ID — no secrets in the repo. (`allowFrom` takes plain user IDs, not
  SecretRef objects, per the OpenClaw config docs.)

### Added
- `.env.example`, `.gitignore` and the CI template
  (`.github/workflows/ci.yml.template`) that 0.1.0 referenced but did not ship.
  The CI template targets the MCP-server repo (ruff + mypy + pytest + promptfoo)
  and is inert in the auditor repo by design.

## [0.1.0] - 2026-06-24

### Added
- Initial scaffold: README (EN/DE), LICENSE, CHANGELOG, .gitignore, .env.example
- v2 build plan under docs/plans
- OpenClaw config + policy-as-code (SOUL.md, AGENTS.md, TOOLS.md)
- Skills: python-auditor, fastmcp-testing, promptfoo-eval
- promptfoo config scaffold + Python provider stub
- CI template (ruff + mypy + pytest + promptfoo)
