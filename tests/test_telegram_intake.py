#!/usr/bin/env python3
"""Tests for scripts/telegram_intake.py — the gateway-free inbound.

Stdlib-only (`python3 -m unittest`). No network: the single Bot API surface is a
fake `TelegramClient`, and GitHub issue creation is monkeypatched. The invariants
pinned here are the security-relevant ones: sender-allowlist gating (silent drop
for unknown senders), the ref charset guard, /audit → issue, /status from the
committed record, and offset-first acknowledgement.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import telegram_intake as ti  # noqa: E402


class FakeClient:
    """Records send_message / acknowledge calls; feeds canned updates."""

    def __init__(self, updates: list[dict] | None = None) -> None:
        self._updates = updates or []
        self.sent: list[dict] = []
        self.acked: list[int] = []

    def get_updates(self, offset=None):  # noqa: ANN001
        return self._updates

    def acknowledge(self, last_update_id: int) -> None:
        self.acked.append(last_update_id)

    def send_message(self, chat_id, text, reply_to=None):  # noqa: ANN001
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to})


def _msg(text: str, sender: int = 42, chat: int = 99, mid: int = 1) -> dict:
    return {
        "update_id": mid,
        "message": {
            "message_id": mid,
            "text": text,
            "from": {"id": sender},
            "chat": {"id": chat, "type": "private"},
        },
    }


@contextlib.contextmanager
def _env(**values: str):
    keys = ["TELEGRAM_ALLOW_FROM", "GITHUB_REPOSITORY", "GITHUB_TOKEN",
            "TELEGRAM_GITHUB_TOKEN", "TARGET_REPO"]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    for k, v in values.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


class AllowlistTest(unittest.TestCase):
    def test_unknown_sender_ignored_silently(self) -> None:
        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42"):
            out = ti.process_update(_msg("/status", sender=7), client, ti.allowed_sender_ids())
        self.assertIn("not authorized", out)
        self.assertEqual(client.sent, [])  # no reply to an unknown sender

    def test_allowed_sender_processed(self) -> None:
        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42"):
            ti.process_update(_msg("/help"), client, ti.allowed_sender_ids())
        self.assertEqual(len(client.sent), 1)
        self.assertIn("gateway-free", client.sent[0]["text"])

    def test_multiple_allowed_ids(self) -> None:
        with _env(TELEGRAM_ALLOW_FROM=" 42 , 7 "):
            self.assertEqual(ti.allowed_sender_ids(), {"42", "7"})


class RefValidationTest(unittest.TestCase):
    def test_empty_defaults_to_main(self) -> None:
        self.assertEqual(ti.validate_ref(""), "main")

    def test_valid_refs(self) -> None:
        for ref in ["main", "v1.2.3", "release/2026-07", "abc123", "feature_x"]:
            self.assertEqual(ti.validate_ref(ref), ref)

    def test_unsafe_refs_rejected(self) -> None:
        for bad in ["a b", "a;rm -rf", "$(x)", "a`b`", "a|b", "a&b"]:
            with self.assertRaises(ValueError):
                ti.validate_ref(bad)


class AuditCommandTest(unittest.TestCase):
    def test_audit_files_issue(self) -> None:
        captured: dict = {}

        def fake_create(repo, token, title, body, labels):  # noqa: ANN001
            captured.update(repo=repo, title=title, body=body, labels=labels)
            return "https://github.com/x/y/issues/1"

        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42", GITHUB_REPOSITORY="malkreide/mcp-continuous-auditor",
                  GITHUB_TOKEN="tok", TARGET_REPO="malkreide/zurich-opendata-mcp"):
            with unittest.mock.patch.object(ti, "create_issue", fake_create):
                out = ti.process_update(_msg("/audit v0.3.3"), client, ti.allowed_sender_ids())
        self.assertEqual(out, "/audit filed as issue")
        self.assertEqual(captured["repo"], "malkreide/mcp-continuous-auditor")
        self.assertEqual(captured["labels"], ["audit-request"])
        self.assertIn("v0.3.3", captured["title"])
        self.assertIn("zurich-opendata-mcp", captured["title"])
        self.assertIn("42", captured["body"])  # requester recorded
        self.assertIn("issues/1", client.sent[0]["text"])

    def test_audit_unsafe_ref_replies_not_raises(self) -> None:
        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42", GITHUB_REPOSITORY="o/r", GITHUB_TOKEN="t"):
            with unittest.mock.patch.object(ti, "create_issue") as create:
                out = ti.process_update(_msg("/audit a;rm -rf"), client, ti.allowed_sender_ids())
        create.assert_not_called()  # unsafe ref never reaches issue creation
        self.assertIn("rejected", out)
        self.assertIn("invalid git ref", client.sent[0]["text"])

    def test_audit_missing_env_replies(self) -> None:
        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42"):  # no repo/token
            out = ti.process_update(_msg("/audit"), client, ti.allowed_sender_ids())
        self.assertIn("failed", out)
        self.assertIn("missing", client.sent[0]["text"].lower())


class StatusCommandTest(unittest.TestCase):
    def test_status_reads_latest_record(self) -> None:
        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42"):
            ti.process_update(_msg("/status"), client, ti.allowed_sender_ids())
        reply = client.sent[0]["text"]
        # The repo ships docs/audits/2026-06-27.md; /status surfaces it.
        self.assertIn("Latest audit record", reply)
        self.assertIn(".md", reply)

    def test_status_no_records(self) -> None:
        with unittest.mock.patch.object(ti, "latest_audit_record", lambda: None):
            self.assertIn("No committed audit record", ti.status_text())


class NonCommandTest(unittest.TestCase):
    def test_plain_text_gets_guidance(self) -> None:
        client = FakeClient()
        with _env(TELEGRAM_ALLOW_FROM="42"):
            out = ti.process_update(_msg("hello there"), client, ti.allowed_sender_ids())
        self.assertEqual(out, "guidance sent (non-command)")
        self.assertIn("only handle commands", client.sent[0]["text"])


class PollLoopTest(unittest.TestCase):
    def test_acknowledge_before_processing(self) -> None:
        # Two updates; ack must advance to the max update_id exactly once.
        updates = [_msg("/help", mid=10), _msg("/status", mid=11)]
        client = FakeClient(updates)
        with _env(TELEGRAM_ALLOW_FROM="42"):
            with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t"}):
                with unittest.mock.patch.object(ti, "TelegramClient", lambda token: client):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = ti.main()
        self.assertEqual(rc, 0)
        self.assertEqual(client.acked, [11])
        self.assertEqual(len(client.sent), 2)

    def test_no_token_is_noop(self) -> None:
        with _env():
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ti.main(), 0)


if __name__ == "__main__":
    unittest.main()
