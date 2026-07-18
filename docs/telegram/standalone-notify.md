# Gateway-independent Telegram announce

`scripts/telegram_notify.py` is a small, **stdlib-only** notifier that pushes an
audit report to Telegram over the Bot API **without** an OpenClaw runtime. It
mirrors the self-contained notifier pattern of the sibling
[`future-skills-evidence-graph`](https://github.com/malkreide/future-skills-evidence-graph)
repo (`scripts/telegram_notify.py`).

## Why this exists next to OpenClaw

OpenClaw is the **interactive control plane**: you message the bot, it runs the
agent, and the daily cron delivers the report via `--announce`
(`openclaw/cron/nightly-audit.json`). OpenClaw owns the token and the two-way
conversation — commands like `audit`, per-finding `OK` before a PR, etc.

This script is the **one-way complement** for deployments that produce a report
but run *no* OpenClaw gateway:

| | OpenClaw channel | `telegram_notify.py` |
|---|---|---|
| Direction | two-way (commands + replies) | one-way (announce only) |
| Needs OpenClaw runtime | yes | no |
| Holds the bot token | the gateway | the calling host |
| Typical host | the gateway box | Tier-0 / a keyed operator run / a CI job / the trusted Broker |

It is deliberately **outbound only**. Inbound commands stay OpenClaw's job: the
auditor centralises command handling in the sandboxed gateway (`openclaw/workspace/`
policy-as-code), and a second, less-guarded command path would widen the trust
surface for no gain. So there is no standalone "intake" counterpart here — only
announce.

## Security posture

- **No-op without configuration.** Without `TELEGRAM_BOT_TOKEN` *and* a chat id
  (`TELEGRAM_ANNOUNCE_TO`, else the first `TELEGRAM_ALLOW_FROM` id) it prints a
  note and exits 0. The **credential-free audit Worker** in the microVM rollout
  never holds the token, so it sends nothing — the announce belongs to a trusted
  host that deliberately holds the secret.
- **Best-effort, never fatal.** Any HTTP/API error is a **token-redacted**
  warning and the exit code stays 0. `scripts/nightly-audit.sh` calls it as its
  last step, *after* `outcome_rc` is captured and with `|| true`, so a broken or
  absent notification can never rewrite a green / findings / hard-fail verdict.
- **Plain text.** Messages are sent without a Markdown/HTML parse mode, so report
  content (which is Markdown) can neither break rendering nor inject formatting.

## Setup

Same bot as OpenClaw — created via [@BotFather](https://t.me/BotFather). Set, on
the host that will send:

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...        # from @BotFather
export TELEGRAM_ANNOUNCE_TO=123456789          # chat/user id (see below)
```

`TELEGRAM_ANNOUNCE_TO` is the destination. For a DM bot it equals your numeric
user id, so if you have only `TELEGRAM_ALLOW_FROM` set the script falls back to
its first id automatically. Get the id from
[@userinfobot](https://t.me/userinfobot), or from
`https://api.telegram.org/bot<TOKEN>/getUpdates` → `message.from.id`. A forum
topic uses the `-100…:topic:<id>` form, same as the cron `--to`.

## Usage

Send a produced report file (e.g. the nightly report):

```bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_ANNOUNCE_TO=... \
  python3 scripts/telegram_notify.py --report .audit/nightly-report.md
```

Or an ad-hoc message:

```bash
python3 scripts/telegram_notify.py \
  --title "Audit malkreide/zurich-opendata-mcp" \
  --line "schema-drift: clean" --line "red-team: clean" \
  --text "All gates green."
```

### Wired into the nightly audit

`scripts/nightly-audit.sh` runs the announce as an **opt-in** final step:

```bash
TELEGRAM_NOTIFY=1 TELEGRAM_BOT_TOKEN=... TELEGRAM_ANNOUNCE_TO=... \
  TARGET_REPO=malkreide/zurich-opendata-mcp scripts/nightly-audit.sh
```

`TELEGRAM_NOTIFY` defaults to off. When on, the report at `.audit/nightly-report.md`
is announced after classification — but only if the token resolves, so leaving it
on in an environment without the secret is harmless (no-op). This lets you run the
whole nightly audit → Telegram loop on a plain host, no OpenClaw required.

## Flags

| Flag | Meaning |
|---|---|
| `--report FILE` | Send the file's contents as the body (e.g. `.audit/nightly-report.md`). |
| `--title` | First line of the message. |
| `--line` | A bullet line; repeatable; empty values dropped. |
| `--text` | Free-text paragraph after the bullets. |
| `--chat-id` | Override the destination (else `TELEGRAM_ANNOUNCE_TO` / first `TELEGRAM_ALLOW_FROM`). |
