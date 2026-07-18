#!/usr/bin/env python3
"""Gateway-independent Telegram intake for the auditor (stdlib only).

The inbound half of the gateway-free Telegram path; the outbound half is
``telegram_notify.py``. OpenClaw remains the *interactive control plane* — it
runs the agent, answers ``audit`` conversationally and gates a PR on your
per-finding ``OK``. This script is the complement for deployments that run **no**
OpenClaw runtime: it lets an allow-listed user drive a small set of **safe,
deterministic** commands from Telegram, in two delivery modes (same as the
sibling ``future-skills-evidence-graph`` repo):

- **Pull (default):** a scheduled GitHub-Actions workflow polls the Bot API
  (``getUpdates``) — no webhook endpoint, fully serverless.
- **Push (optional real-time):** a Telegram webhook hits the minimal Cloudflare
  relay (``relay/telegram-webhook-relay.js``), which only re-dispatches the
  workflow with the update as the ``TELEGRAM_UPDATE`` input.

What an authorized user can do:

- ``/audit [ref]`` — file an **audit-request issue** (label ``audit-request``)
  in this repo for ``TARGET_REPO`` at an optional git ref. This is a *request
  artifact*, exactly like the target's finding tickets: the read-only audit and
  any ``fix/<slug>`` PR are still produced by the sandboxed OpenClaw agent /
  the nightly pipeline after a human is in the loop. Telegram is transport, not
  policy — nothing goes live from a chat message.
- ``/status`` — reply with the latest committed audit record (``docs/audits/``),
  the same versioned artifact a reviewer reads.
- ``/help`` (``/hilfe``, ``/start``) — usage.

What it deliberately does **not** do: authorize a PR. Cutting a ``fix/<slug>``
PR is the auditor's one write path and stays inside the OpenClaw sandbox
(``openclaw/workspace/AGENTS.md``); a second, less-guarded command path would
only widen the trust surface. So there is no ``OK`` → PR handling here.

Security model (mirrors the outbound half + AGENTS.md):

- Only messages whose **sender** id is in ``TELEGRAM_ALLOW_FROM`` (the repo's
  existing numeric-user-id allowlist) are processed; everything else is ignored
  **silently**, so the bot cannot be used as a spam/probe relay.
- Updates are acknowledged (offset advanced) BEFORE processing, so a crash
  mid-run loses at most one poll's messages instead of re-filing duplicate
  issues on every poll.
- Untrusted input: a chat message is untrusted text. It is only ever used as an
  issue body (never exec'd, never interpolated into a shell) and the git ref is
  charset-validated before it reaches the issue.

    TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOW_FROM=123456789 \
        GITHUB_REPOSITORY=owner/repo GITHUB_TOKEN=... \
        python3 scripts/telegram_intake.py

Setup and operations are documented in docs/telegram/standalone-intake.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_notify import (  # noqa: E402
    API_BASE,
    MAX_MESSAGE_CHARS,
    TRUNCATION_MARKER,
    redact_token,
)

REQUEST_TIMEOUT_SECONDS = 30

DEFAULT_TARGET_REPO = "malkreide/zurich-opendata-mcp"

# A git ref we are willing to put in an issue / hand to the pipeline. Branch,
# tag and short/long sha shapes only — no spaces, no shell metacharacters, so an
# untrusted chat string can never smuggle anything downstream.
_REF_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,120}$")

# The committed audit records the /status command reads.
AUDITS_DIR = Path(__file__).resolve().parents[1] / "docs" / "audits"

HELP_TEXT = (
    "Auditor — gateway-free commands (OpenClaw stays the interactive control plane):\n"
    "/audit [ref] — file a read-only audit request for the target (optional git ref)\n"
    "/status — latest committed audit record\n"
    "/help — this help\n"
    "Note: a chat request only files a ticket. The read-only audit and any "
    "fix/<slug> PR are still produced by the sandboxed agent with a human in "
    "the loop — nothing goes live from Telegram."
)


class TelegramClient:
    """Minimal Bot API client (standard library only).

    Every call goes through :meth:`_request`, so tests exercise the intake logic
    by replacing that single method. Errors are raised as ``RuntimeError`` with
    the bot token redacted (urllib embeds the request URL — which contains the
    token — in its exceptions).
    """

    def __init__(self, token: str) -> None:
        self.token = token

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(params or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{API_BASE}/bot{self.token}/{method}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"Telegram API call {method} failed: {redact_token(str(exc), self.token)}"
            ) from exc
        if not body.get("ok"):
            raise RuntimeError(
                f"Telegram API call {method} rejected: {body.get('description', body)}"
            )
        return body.get("result")

    def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._request("getUpdates", params) or []

    def acknowledge(self, last_update_id: int) -> None:
        """Confirm all updates up to *last_update_id* (Telegram-side state)."""
        self._request("getUpdates", {"offset": last_update_id + 1, "timeout": 0, "limit": 1})

    def send_message(self, chat_id: int | str, text: str, reply_to: int | None = None) -> None:
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        self._request("sendMessage", params)


def allowed_sender_ids() -> set[str]:
    """Sender ids allowed to use the intake, from ``TELEGRAM_ALLOW_FROM``.

    The auditor gates on the *sender* (``message.from.id``), the same numeric
    user id used for OpenClaw's ``allowFrom`` — not the chat id. Comma-separate
    to allow more than one.
    """
    raw = os.environ.get("TELEGRAM_ALLOW_FROM", "").strip()
    return {part.strip() for part in raw.split(",") if part.strip()}


def target_repo() -> str:
    return os.environ.get("TARGET_REPO", "").strip() or DEFAULT_TARGET_REPO


def validate_ref(raw: str) -> str:
    """Return a safe git ref from *raw*, or ``main`` when empty; raise if unsafe.

    A ref is untrusted chat text; refusing anything outside the branch/tag/sha
    charset keeps it from smuggling shell metacharacters or whitespace into the
    issue and, downstream, the pipeline's ``TARGET_REF``.
    """
    ref = raw.strip()
    if not ref:
        return "main"
    if not _REF_RE.match(ref):
        raise ValueError(f"invalid git ref '{ref}' (allowed: letters, digits, . _ - /)")
    return ref


def classify_message(message: dict[str, Any]) -> dict[str, Any]:
    """Map a Telegram *message* to a command action.

    Returns ``{"kind": "command", "name": ..., "args": ...}`` for a slash
    command, else ``{"kind": "ignore"}`` (free text / stickers / photos are not
    actionable in the gateway-free path — use OpenClaw for a conversation).
    """
    text = (message.get("text") or message.get("caption") or "").strip()
    if text.startswith("/"):
        head, _, rest = text.partition(" ")
        name = head.split("@", 1)[0].lstrip("/").lower()
        return {"kind": "command", "name": name, "args": rest.strip()}
    return {"kind": "ignore"}


def latest_audit_record() -> Path | None:
    """The most recent committed audit file in ``docs/audits/`` (README aside)."""
    if not AUDITS_DIR.is_dir():
        return None
    records = sorted(
        p for p in AUDITS_DIR.glob("*.md") if p.name.lower() != "readme.md"
    )
    return records[-1] if records else None


def status_text() -> str:
    """Latest committed audit record, condensed for a chat reply."""
    record = latest_audit_record()
    if record is None:
        return (
            "No committed audit record yet (docs/audits/). Run one via OpenClaw "
            "or file /audit, then the nightly pipeline records it."
        )
    lines = record.read_text(encoding="utf-8").splitlines()
    # Keep the header block up to the first horizontal rule / the toolchain
    # status heading — enough to convey date, target and green/findings — rather
    # than pasting a multi-page report into the chat.
    kept: list[str] = []
    for line in lines:
        if line.strip() == "---" and kept:
            break
        kept.append(line)
        if len(kept) >= 40:
            break
    body = "\n".join(kept).strip()
    return f"Latest audit record — {record.name}:\n\n{body}"


def github_token() -> str:
    """Token for issue creation: a dedicated PAT wins, else the workflow token."""
    return (
        os.environ.get("TELEGRAM_GITHUB_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
    )


def create_issue(repo: str, token: str, title: str, body: str, labels: list[str]) -> str:
    """Create an issue via the GitHub REST API; returns its html_url."""
    payload = json.dumps({"title": title, "body": body, "labels": labels}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mcp-continuous-auditor-telegram-intake",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            created = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"could not create issue ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"could not create issue: {exc}") from exc
    return created.get("html_url", "")


def handle_audit_request(args: str, sender_id: str) -> str:
    """File an ``audit-request`` issue for the target; return the chat reply."""
    ref = validate_ref(args)  # raises ValueError on unsafe input
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = github_token()
    if not repo or not token:
        raise RuntimeError("GITHUB_REPOSITORY or a GitHub token is missing in the environment.")
    target = target_repo()
    title = f"[audit-request] {target}@{ref}"
    body = (
        f"Read-only audit requested via the gateway-free Telegram intake.\n\n"
        f"- **Target:** `{target}`\n"
        f"- **Ref:** `{ref}`\n"
        f"- **Requested by:** Telegram user `{sender_id}` (authorized allowlist)\n\n"
        f"This is a request artifact only. The read-only audit "
        f"(`scripts/nightly-audit.sh` / the OpenClaw agent) and any `fix/<slug>` "
        f"PR are produced with a human in the loop — nothing goes live from this "
        f"ticket. Close it once the audit has run."
    )
    issue_url = create_issue(repo, token, title, body, ["audit-request"])
    return (
        f"Audit request filed: {issue_url}\n"
        f"Target `{target}` @ `{ref}`. It is read-only and opens no PR without a "
        f"human OK."
    )


def process_update(update: dict[str, Any], client: TelegramClient, allowed: set[str]) -> str:
    """Process one update; returns a log line. Replies happen inside."""
    message = update.get("message")
    if not message:
        return "skipped (no message update)"
    sender_id = message.get("from", {}).get("id")
    if str(sender_id) not in allowed:
        # Unknown senders are ignored WITHOUT a reply: answering would turn the
        # bot into a probe/spam target; authorized users are configured.
        return f"ignored (sender {sender_id} not authorized)"

    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    action = classify_message(message)
    if action["kind"] != "command":
        client.send_message(
            chat_id,
            "I only handle commands in the gateway-free path. " + HELP_TEXT,
            reply_to=message_id,
        )
        return "guidance sent (non-command)"

    name = action["name"]
    if name == "audit":
        try:
            reply = handle_audit_request(action.get("args", ""), str(sender_id))
        except ValueError as exc:  # unsafe ref → chat reply, not a crash
            client.send_message(chat_id, f"Could not file the request: {exc}", reply_to=message_id)
            return f"/audit rejected: {exc}"
        except RuntimeError as exc:
            client.send_message(
                chat_id,
                f"Could not file the audit request: {exc}\nPlease try again.",
                reply_to=message_id,
            )
            return f"/audit failed: {exc}"
        client.send_message(chat_id, reply, reply_to=message_id)
        return "/audit filed as issue"
    if name == "status":
        try:
            reply = status_text()
        except Exception as exc:  # a data problem must become a chat reply
            reply = f"Status query failed: {exc}"
        client.send_message(chat_id, reply, reply_to=message_id)
        return "/status answered"
    # /help, /hilfe, /start and any unknown command → usage.
    client.send_message(chat_id, HELP_TEXT, reply_to=message_id)
    return f"/{name} answered with help"


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Telegram not configured (TELEGRAM_BOT_TOKEN) — intake skipped.")
        return 0
    allowed = allowed_sender_ids()
    if not allowed:
        print("No allowed senders (TELEGRAM_ALLOW_FROM) — intake skipped.")
        return 0

    client = TelegramClient(token)

    # Push mode: the webhook relay dispatched this run with exactly one update.
    raw_update = os.environ.get("TELEGRAM_UPDATE", "").strip()
    if raw_update:
        try:
            update = json.loads(raw_update)
        except ValueError as exc:
            print(f"TELEGRAM_UPDATE is not valid JSON: {exc}")
            return 1
        try:
            outcome = process_update(update, client, allowed)
        except RuntimeError as exc:
            print(f"Update {update.get('update_id')}: ERROR: {exc}")
            return 1
        print(f"Update {update.get('update_id')}: {outcome} (push mode)")
        return 0

    try:
        updates = client.get_updates()
    except RuntimeError as exc:
        # With a webhook set (push mode), Telegram rejects getUpdates with 409.
        if "409" in str(exc):
            print("Webhook mode active (getUpdates → 409) — polling skipped.")
            return 0
        raise
    if not updates:
        print("No new messages.")
        return 0

    # Acknowledge first: a crash below must not re-process the same updates on
    # every poll (see module docstring).
    client.acknowledge(max(update["update_id"] for update in updates))

    failures = 0
    for update in updates:
        try:
            outcome = process_update(update, client, allowed)
        except RuntimeError as exc:
            failures += 1
            outcome = f"ERROR: {exc}"
        print(f"Update {update.get('update_id')}: {outcome}")
    print(f"{len(updates)} update(s) processed, {failures} error(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
