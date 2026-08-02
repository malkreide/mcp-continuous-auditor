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
- **Recall floors, not just schema** — a collapsed result set keeps its JSON shape, so a structural diff cannot see it. Probes carry `min_count` floors, and a second weekly probe calls the server's *own tools* live: the raw-URL probe verifies the endpoint, the canary verifies the whole chain.
- **Identity probe** — a server whose User-Agent carries a hand-maintained version drifts silently: nothing breaks, no test fails, and it keeps introducing itself to every upstream as a release it stopped being. A portfolio sweep found 12 of 30 servers sending a wrong version, 4 of them a wrong *major*. `scripts/identity_probe.py` checks source and, with `--installed`, the shipped artifact — the only evidence that survives a stale editable install.
- **Shipped probe** — the identity probe asks whether the reported version is *correct*; this asks whether it is *current*, and then whether it *runs*. A repository can be green, audited and entirely fixed while every `pip install` still hands out the broken release, because CI tests the branch and not the artifact. `meteoswiss-mcp` shipped an import error to every fresh install for three days with `main` already corrected, and it took an outside bug report to notice. `scripts/shipped_probe.py` runs at two depths: `--metadata-only` compares the index version, the yank status, the release tags and the unreleased commits for two HTTP requests, weighing a `fix:` differently from a `docs:` — kind beats age, so a user-facing commit is reported the moment it is unreleased and a breaking one at any age, while housekeeping keeps the seven-day clock; the default depth then installs the distribution into a fresh venv, compares the installed sources against the checkout and speaks real MCP to it. Two anchors close the gaps a version number leaves: `NO_TAGS`, because a repository without release tags leaves half these checks measuring nothing and must say so rather than report OK (a shallow clone is *unknown*, not zero), and `STALE_ARTIFACT`, which compares content instead of numbers — the only way to see the case where the index and the checkout both say 0.3.3 and are not the same code. `--pin-version` installs `dist==VERSION` for a re-check after a release: an unpinned install was measured serving the previous artifact for minutes after the new one was listed, `--no-cache-dir` and all. It reads the **Simple API** at `--index-url` — the surface `pip` installs from, in both the PEP 691 JSON and the PEP 503 HTML flavour, since only HTML is guaranteed — because PyPI's JSON API was measured lagging it by minutes on both the latest version and the `yanked` flag; on PyPI the JSON API is read as a second opinion, and where the two disagree the answer is `UNCONFIRMED` rather than guessed. That also closes a blind spot the version number alone cannot see: a *yanked* release looks identical to a healthy one — the version exists, the tag matches, CI is green — while every `pip install` quietly resolves to something older. An unreachable index is reported as a harness failure, never as "in sync".
- **Yank probe** — the shipped probe asks whether the version users install *right now* is withdrawn. This asks the inverse, across the whole catalogue: does a known-broken, **not**-yanked release still exist, with a healthy successor beside it? `zurich-opendata-mcp` 0.5.1 declared `mcp[cli]>=1.28.1` with no upper bound; `mcp` 2.0.0 removed `mcp.server.fastmcp` and every fresh install died on import. 0.6.0 fixed it — and superseding was not enough, because the broken release stayed selectable for anyone a resolver had constrained away from 0.6.0 by an old lockfile or a colliding pin. Two things a naive check misses: **all six** predecessors carried an uncapped `mcp` range, so a probe reading only `latest-1` would have found one of six and reported the catalogue clean; and a yank is *not* a deletion — PEP 592 keeps the release resolvable for an explicit pin, with a warning, so existing lockfiles do not break and the finding never says "delete". `scripts/yank_probe.py` walks every version's `Requires-Dist` over PEP 658 core metadata (no wheel downloads) and raises `UNYANKED_BROKEN_RELEASE` only when four conditions hold together — the release is unyanked, its range is uncapped, the healthy successor's own pin *excludes* that range's floor, and something past the boundary is actually published and selectable. That third condition is what licenses the word "broken": the maintainer's own later release already declared the crossing breaking. A missing yank *reason* is a separate, low finding, because `pip` prints `Reason for being yanked: <none given>` to the one audience a yanked release still has. The probe **recommends** a yank and never performs one: that needs an upload-scoped token and is a maintainer's decision, so there is no flag for it and every request is a GET.
- **Published probe** — the identity probe reads a repository and the shipped probe's metadata depth compares version *numbers*; neither opens the artifact. `swiss-efv-mcp` passes both (PyPI 0.3.0, `main` 0.3.0, `src/` clean) while the package every user installs sends `Mozilla/5.0 … Chrome/124.0` — it impersonates a browser to upstreams. `scripts/published_probe.py` installs the distribution into a throwaway venv and measures what the shipped code does: the User-Agent it puts on the wire — 16 of 33 portfolio packages announced a version they were not, with the fix merged in every one — plus three things a User-Agent cannot tell you. **Imports decide the status** (`broken_import`), and the package root is imported before its submodules so an import-order artefact is not mistaken for a defect: `bag-health-mcp` was reported as a circular import it does not have, because only the private submodule *as the very first import of a process* fails. **Start is not import**: the installed console script is run with stdin closed for a few seconds and must announce a `server.start` event without crashing — a clean exit that announced nothing is not a pass. And **`requires_dist` is checked for a missing upper bound** on the dependencies the package actually imports: `swiss-energy-mcp` 0.3.3 shipped `mcp[cli]>=1.20.0` uncapped, and the day `mcp` 2.0.0 appeared every fresh install of it died — reported the moment the index serves a higher major, which is before the break rather than after it. Where it cannot resolve a value it reports `UNVERIFIED`, never clean — an earlier version that conflated "found nothing" with "there is nothing" called 24 packages unremarkable, 16 of which were drifting.
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
| `TELEGRAM_ANNOUNCE_TO` | Optional — destination for the gateway-independent announce (`scripts/telegram_notify.py`); falls back to the first `TELEGRAM_ALLOW_FROM` id |
| `TELEGRAM_NOTIFY` | Optional — set `1` to have `scripts/nightly-audit.sh` push the report to Telegram without OpenClaw (default off; see [docs](docs/telegram/standalone-notify.md)) |
| `TELEGRAM_GITHUB_TOKEN` | Optional — PAT (issues: read/write) for the gateway-free inbound intake (`scripts/telegram_intake.py`); falls back to the workflow token (see [docs](docs/telegram/standalone-intake.md)) |
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
skills/           python-auditor, fastmcp-testing, promptfoo-eval,
                  identity-probe, published-probe, shipped-probe, yank-probe
                  (shipped-probe absorbed the former release-gap skill)
schemas/          generated tool-output JSON-Schemas = the drift detector
promptfoo/        deterministic asserts, schema-drift, red-team + recorded fixtures
scripts/          audit harness, live-probe, nightly-audit core, budget guard,
                  deterministic findings→issue routing, pinned-promptfoo installer,
                  gateway-independent Telegram announce + intake (telegram_notify.py,
                  telegram_intake.py)
                  portfolio_scan.py = the fan-out: one cheap predicate across
                  EVERY server as a matrix (targets.example.yaml), for the
                  question a single-target nightly cannot answer — which repo
                  is the one out of line. A run must claim its COVERAGE: it
                  names every declared target it did not scan and gives no
                  overall verdict without all of them (--partial acknowledges a
                  narrow run). Each target's default branch is resolved live
                  with `git ls-remote --symref` rather than assumed to be
                  `main` — three of these repositories are on `master`, and
                  `git remote show origin` reads a cached HEAD that answered
                  wrongly for four of them in one sitting
                  yank_probe.py = the inverse question: not "is what users
                  install withdrawn" but "is a known-broken release still
                  installable" — read-only, it never performs a yank
targets.example.yaml  format reference for the fan-out target list; the real
                  targets.yaml is gitignored (inventory, not source)
relay/            optional Cloudflare Worker for real-time Telegram push intake
tensorzero/       Phase 5: LLM-gateway config + stack (cost-caps, A/B, audit-trail)
tests/            stdlib unit tests (605 in 26 files) — run by .github/workflows/tests.yml
.github/          tests.yml = the auditor's own suite; *.yml.template = CI for the target repo
docs/plans/       the v2 build plan
docs/cron/        the daily nightly-audit cron (flow, model hard-fail, install)
docs/deployment/  Raspberry Pi (recommended host), Phase 5 forkd/microVM isolation,
                  worker-broker-rollout.md = updating both sides in the right ORDER
                  (Broker first — an old Broker reports a new Worker's findings as green)
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

## Related repositories

### The MCP quality chain

Five repositories, one lifecycle. Each answers a different question, in the order they come up — this one comes last, and it is the only one that keeps asking. The shared GitHub topic is [`mcp-quality-chain`](https://github.com/topics/mcp-quality-chain), which lists all five on one page.

| Stage | Repository | Question it answers |
|---|---|---|
| before the build | [`mcp-data-source-probe-skill`](https://github.com/malkreide/mcp-data-source-probe-skill) | Is the source usable, and what does it hold? Its step 1.4 recall ground truth is what this project's `min_count` floors are measured against |
| in the build | [`mcp-data-fidelity-skill`](https://github.com/malkreide/mcp-data-fidelity-skill) | Does it return what the source actually holds? Its rule 5 — recall in the tests, not the description — is the reason the probes carry floors rather than schema assertions |
| in the build | [`mcp-transport-hardening-skill`](https://github.com/malkreide/mcp-transport-hardening-skill) | Does it come up, and does it turn away the right callers? The transport path the canary probe exercises live |
| after the build | [`mcp-audit-skill`](https://github.com/malkreide/mcp-audit-skill) | Does it hold up against the catalogue? Its `OPS-005` (pipeline honesty) came from this repository — [#29](https://github.com/malkreide/mcp-continuous-auditor/pull/29), a test suite no workflow ever ran |
| in operation | **`mcp-continuous-auditor`** | **This project:** does it still hold up tomorrow? |

Alongside, not part of the chain: [`mcp-builder`](https://github.com/anthropics/skills/tree/main/skills/mcp-builder) — Anthropic's generic build guidance. It is someone else's repository and cannot carry the topic.

The four skills say what a correct server looks like; this project is the part that keeps checking after everyone has stopped looking. Every probe here exists because a server was green and wrong at the same time — which is the failure class all five were written for.

The servers being audited are the [Swiss Public Data MCP](https://github.com/malkreide/swiss-public-data-mcp) portfolio, which carries its own topic [`swiss-public-data-mcp`](https://github.com/topics/swiss-public-data-mcp).

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

## License

MIT License — see [LICENSE](LICENSE)

## Author

Hayal Özkan · [malkreide](https://github.com/malkreide)
