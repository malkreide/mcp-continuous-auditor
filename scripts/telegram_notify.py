#!/usr/bin/env python3
"""Gateway-independent Telegram announce for audit results (stdlib only).

The auditor's *interactive* control plane is the OpenClaw gateway
(``openclaw/openclaw.json`` → channel ``telegram``): you message the bot,
OpenClaw runs the agent, and the daily cron delivers its report via
``--announce`` (``openclaw/cron/nightly-audit.json``). OpenClaw owns the token
and the two-way conversation.

This script is the *one-way, gateway-independent* complement: it lets a trusted
host that already holds the bot token push a report to Telegram over the Bot API
**without** an OpenClaw runtime — a Tier-0 / operator run, a CI job with the
secret, or the trusted Broker after it has classified a Worker's evidence. It
mirrors the self-contained notifier pattern of the sibling
``future-skills-evidence-graph`` repo (``scripts/telegram_notify.py``).

Design invariants (matching the rest of the repo's tooling):

- **stdlib only** — no third-party deps, runs anywhere ``python3`` does.
- **No-op without configuration** — without ``TELEGRAM_BOT_TOKEN`` *and* a chat
  id (``TELEGRAM_ANNOUNCE_TO``, falling back to the first ``TELEGRAM_ALLOW_FROM``
  id) it prints a note and exits 0. The credential-free audit Worker therefore
  sends nothing; only a host that deliberately holds the secret does.
- **Best-effort, never fatal** — any HTTP/API error is a token-redacted warning
  and the exit code stays 0, so a broken notification never turns a green audit
  red (or a hard-fail into a crash). This is why ``scripts/nightly-audit.sh`` can
  call it as a final step without touching its exit-code contract.

Usage::

    # send a produced report file (e.g. the nightly report)
    TELEGRAM_BOT_TOKEN=... TELEGRAM_ANNOUNCE_TO=... \
        python3 scripts/telegram_notify.py --report .audit/nightly-report.md

    # or an ad-hoc message
    python3 scripts/telegram_notify.py --title "Audit" --line "green" --text "…"

Setup and the security framing are in ``docs/telegram/standalone-notify.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.telegram.org"

# Telegram rejects messages longer than 4096 characters; truncate instead of
# failing so a long report still produces a (shortened) notification.
MAX_MESSAGE_CHARS = 4096
TRUNCATION_MARKER = "\n… [truncated]"

REQUEST_TIMEOUT_SECONDS = 20


def _first_id(raw: str) -> str:
    """First non-empty, comma-separated id in *raw* (``TELEGRAM_ALLOW_FROM``)."""
    for part in raw.split(","):
        part = part.strip()
        if part:
            return part
    return ""


def telegram_config(chat_id_override: str = "") -> tuple[str, str] | None:
    """Return ``(bot_token, chat_id)`` from the environment, or ``None``.

    The chat id is resolved from, in order: an explicit ``--chat-id`` override,
    ``TELEGRAM_ANNOUNCE_TO`` (the delivery target used by the OpenClaw cron), then
    the first id in ``TELEGRAM_ALLOW_FROM`` (a DM bot's chat id equals the user
    id, so the minimal ``.env`` already suffices). Both a token and a chat id must
    resolve; a half-configured environment behaves like an unconfigured one
    (no-op) rather than erroring.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (
        chat_id_override.strip()
        or os.environ.get("TELEGRAM_ANNOUNCE_TO", "").strip()
        or _first_id(os.environ.get("TELEGRAM_ALLOW_FROM", ""))
    )
    if not token or not chat_id:
        return None
    return token, chat_id


def format_message(title: str, lines: list[str] | tuple[str, ...] = (), text: str = "") -> str:
    """Compose a plain-text message from *title*, bullet *lines*, and *text*.

    Plain text (no Markdown/HTML parse mode) so a report containing ``*_[`` `` ` ``
    characters — audit reports are Markdown — can never break rendering or inject
    formatting. The result is truncated to Telegram's message limit.
    """
    parts = [title.strip()] if title.strip() else []
    parts.extend(f"• {line.strip()}" for line in lines if line.strip())
    if text.strip():
        parts.append(text.strip())
    message = "\n".join(parts)
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return message


def redact_token(text: str, token: str) -> str:
    """Strip the bot *token* out of *text* (urllib errors embed the request URL)."""
    return text.replace(token, "***") if token else text


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send *text* to *chat_id* via the Bot API; ``True`` on success.

    Failures are printed (token-redacted) and reported as ``False``, never
    raised: callers treat notifications as best-effort.
    """
    if not text.strip():
        return False
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            # Link previews would balloon every report notification.
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print(
            f"WARNING: Telegram message not sent: {redact_token(str(exc), token)}",
            file=sys.stderr,
        )
        return False
    if not body.get("ok"):
        print(
            f"WARNING: Telegram API rejected the message: {body.get('description', body)}",
            file=sys.stderr,
        )
        return False
    return True


def notify(
    title: str,
    lines: list[str] | tuple[str, ...] = (),
    text: str = "",
    chat_id_override: str = "",
) -> bool:
    """Send a formatted notification if Telegram is configured; ``True`` when sent."""
    config = telegram_config(chat_id_override)
    if config is None:
        print(
            "Telegram not configured (TELEGRAM_BOT_TOKEN + TELEGRAM_ANNOUNCE_TO/"
            "TELEGRAM_ALLOW_FROM) — skipped."
        )
        return False
    token, chat_id = config
    return send_message(token, chat_id, format_message(title, lines, text))


def _read_report(path: str) -> str:
    """Read a report file for the message body; empty string on any error.

    Best-effort like the rest of the module: a missing/unreadable report must not
    raise into the calling audit script.
    """
    try:
        return open(path, encoding="utf-8").read()
    except OSError as exc:
        print(f"WARNING: could not read report '{path}': {exc}", file=sys.stderr)
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send an optional Telegram announce (no-op without configuration)."
    )
    parser.add_argument("--title", default="", help="First line of the message.")
    parser.add_argument(
        "--line",
        action="append",
        default=[],
        help="Bullet line, repeatable; empty values are dropped.",
    )
    parser.add_argument("--text", default="", help="Free-text paragraph after the bullets.")
    parser.add_argument(
        "--report",
        default="",
        help="Path to a report file whose contents become the message body "
        "(e.g. .audit/nightly-report.md). Appended after --title/--line/--text.",
    )
    parser.add_argument(
        "--chat-id",
        default="",
        help="Override the destination chat id (else TELEGRAM_ANNOUNCE_TO / the "
        "first TELEGRAM_ALLOW_FROM id).",
    )
    args = parser.parse_args()

    text = args.text
    if args.report:
        report_body = _read_report(args.report)
        text = f"{text}\n\n{report_body}".strip() if text.strip() else report_body

    if not (args.title.strip() or text.strip() or any(l.strip() for l in args.line)):
        print("Empty message — nothing sent.")
        return 0

    if notify(args.title, args.line, text, chat_id_override=args.chat_id):
        print("Telegram notification sent.")
    # A failed or skipped notification must never fail the calling script.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
