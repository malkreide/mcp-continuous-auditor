# Tier 0 — the recommended entry (Docker sandbox + direct path)

Start here. Tier 0 runs the whole auditor on **any Linux box** with just OpenClaw's
Docker sandbox and the deterministic gates — **no** nested KVM, vsock/udev,
cloud-init, TensorZero or ClickHouse. Those are real hardening tiers (below), not
prerequisites, and each one is a separate operational surface that breaks in its
own ways. The security *core* is already present at Tier 0:

- read-only first, **PR-only**, human is the merge gate (SOUL/AGENTS/TOOLS);
- the deterministic truth engine (pytest + promptfoo + schema-drift) in CI;
- **writer ≠ checker** (cross-family grader), enforced (Analysis S-F);
- hard-fail-never-green discipline throughout;
- deterministic issue routing (`scripts/sync_findings_issues.py`, U-C);
- budget guardrails (circuit breaker + token ceiling).

## What you run

```bash
git clone https://github.com/malkreide/mcp-continuous-auditor.git
cd mcp-continuous-auditor
cp .env.example .env        # TELEGRAM_*, ANTHROPIC_API_KEY, a cross-family grader
                            # key (OPENAI_API_KEY) or GRADER_PROVIDER=ollama:…,
                            # GITHUB_TOKEN (contents+pull-requests+issues), TARGET_REPO
npm i -g openclaw promptfoo # or npx

# One-shot audit, direct (no microVM). The key-less determ profile needs no grader:
TARGET_REPO=malkreide/zurich-opendata-mcp PROMPTFOO_PROFILE=determ \
  bash scripts/nightly-audit.sh

# Proactive daily cron (03:00 → Telegram):
OPENCLAW_AUDIT_MODEL="anthropic/claude-opus-4-…" TELEGRAM_ANNOUNCE_TO="123456789" \
  openclaw/cron/install.sh
```

The OpenClaw agent runs in its Docker sandbox (`openclaw.json` → `sandbox: all`).
The model layer (llm-rubric + red-team, the `graded` profile) runs in **CI** on
every PR with secrets — see the CI template — so Tier 0 does not need a grader key
on the host for the nightly `determ` run.

## Recommended host

A **dedicated, network-isolated device** (a Raspberry Pi 5 or a small Linux VM in
its own subnet) — because the OpenClaw process holds the PAT + Anthropic key and
runs shell tools. See [raspberry-pi.md](raspberry-pi.md). That is a *host* choice,
still Tier 0: no microVM, no TensorZero.

## Deployment tiers

| Tier | Isolation of the credential holder | Adds | Guide |
|---|---|---|---|
| **0 (start)** | OpenClaw Docker sandbox on a dedicated host | — | this doc + [raspberry-pi.md](raspberry-pi.md) |
| 1 | + host egress allowlist (nft) + forward-proxy | domain-level egress control | [forward-proxy](../../deploy/microvm/forward-proxy/README.md) |
| 2 | + microVM Broker/Worker split (credential-free reader) | hardware/KVM isolation, vsock-only channel | [phase5-rollout.md](phase5-rollout.md), [forkd-isolation.md](forkd-isolation.md) |
| 3 | + TensorZero gateway | true per-run cost-cap + audit-trail | [tensorzero.md](../observability/tensorzero.md) |

Adopt a tier only once the one below runs green for a while. What you trade at
Tier 0: no hardware/VM isolation of the credential holder (Docker sandbox only),
and the budget token-ceiling counts only the promptfoo eval's tokens, not the
agent's own loop (that becomes exact at Tier 3 — see
[budget/guardrails.md](../budget/guardrails.md)).
