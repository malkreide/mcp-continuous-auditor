# Gateway-independent Telegram intake

`scripts/telegram_intake.py` is the **inbound** half of the gateway-free Telegram
path (the outbound half is [`telegram_notify.py`](standalone-notify.md)). It lets
an allow-listed user drive a small set of **safe, deterministic** commands from
Telegram **without** an OpenClaw runtime, running entirely in GitHub Actions —
mirroring the intake of the sibling
[`future-skills-evidence-graph`](https://github.com/malkreide/future-skills-evidence-graph)
repo.

## Why this exists next to OpenClaw

OpenClaw is the **interactive control plane**: you message the bot, it runs the
agent, answers `audit` conversationally, and gates a `fix/<slug>` PR on your
per-finding `OK`. This intake is the complement for deployments that run **no**
gateway — it turns a chat message into a *request artifact*, never an agent run.

| | OpenClaw channel | `telegram_intake.py` |
|---|---|---|
| Runs | the gateway box | GitHub Actions (poll every ~10 min, or push relay) |
| `audit` | runs the agent, replies with a report | files an `audit-request` **issue** |
| Per-finding `OK` → PR | yes (sandboxed) | **no — stays OpenClaw's job** |
| Free-form conversation | yes | no (commands only) |

### What it deliberately does not do

It never authorizes a PR. Cutting a `fix/<slug>` PR is the auditor's one write
path and stays inside the OpenClaw sandbox (`openclaw/workspace/AGENTS.md`); a
second, less-guarded command path would only widen the trust surface. So there
is no `OK` → PR handling here — the intake only produces requests, exactly as
`future-skills-evidence-graph`'s intake only produces *candidates*.

## Commands

| Command | Effect |
|---|---|
| `/audit [ref]` | File a `[audit-request]` issue for `TARGET_REPO` at an optional git ref (default `main`). Read-only; opens no PR without a human OK. |
| `/status` | Reply with the latest committed audit record (`docs/audits/`). |
| `/help` (`/hilfe`, `/start`) | Usage. |

The git ref is untrusted chat text, so it is charset-validated (branch/tag/sha
shape only) before it reaches the issue — a message like `/audit a;rm -rf` is
rejected with a chat reply, never filed.

## Security model

- **Sender allowlist.** Only messages whose `message.from.id` is in
  `TELEGRAM_ALLOW_FROM` are processed; everything else is **ignored silently**
  (no reply), so the bot cannot be used as a probe/spam target. This is the same
  numeric user id OpenClaw uses for `allowFrom`.
- **No-op without configuration.** Without `TELEGRAM_BOT_TOKEN` the workflow is a
  seconds-cheap no-op; without `TELEGRAM_ALLOW_FROM` it skips too.
- **Acknowledge-first.** The poll advances the Telegram offset *before*
  processing, so a crash mid-run loses at most one poll's messages instead of
  re-filing duplicate issues on every poll.
- **Untrusted input.** A chat message is only ever used as an issue body (never
  exec'd, never interpolated into a shell); the ref is charset-validated.

## Setup

1. Same bot as OpenClaw (via [@BotFather](https://t.me/BotFather)). Add repo
   secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ALLOW_FROM` — your numeric user id (from
     [@userinfobot](https://t.me/userinfobot)); comma-separate for more than one.
   - `TELEGRAM_GITHUB_TOKEN` *(optional)* — a fine-grained PAT (this repo,
     issues: read/write). Falls back to the workflow's own `GITHUB_TOKEN`, which
     already has `issues: write` in `telegram-intake.yml`.
   - repo *variable* `TARGET_REPO` *(optional)* — the audited repo named in the
     request issue; defaults to `malkreide/zurich-opendata-mcp`.
2. That's it for **pull mode**: `.github/workflows/telegram-intake.yml` polls
   every ~10 minutes (and on manual dispatch for an instant fetch).

### Optional push mode (real-time)

For seconds-level replies, deploy the minimal Cloudflare Worker
`relay/telegram-webhook-relay.js`. It validates a secret header and only
re-dispatches `telegram-intake.yml` with the raw update — all logic stays in
Actions.

Worker secrets: `WEBHOOK_SECRET`, `GITHUB_PAT` (Actions: read/write, this repo),
`GITHUB_REPOSITORY`, optional `GITHUB_BRANCH`. Register the webhook:

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=<worker-url>&secret_token=<WEBHOOK_SECRET>&allowed_updates=["message"]
```

While a webhook is set, Telegram answers `getUpdates` with 409 and the scheduled
poll no-ops cleanly, so both triggers can stay enabled at once. Revert with
`https://api.telegram.org/bot<TOKEN>/deleteWebhook`.
