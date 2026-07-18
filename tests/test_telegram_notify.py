#!/usr/bin/env python3
"""Tests for scripts/telegram_notify.py — the gateway-independent announce.

Stdlib-only (`python3 -m unittest`), matching the rest of the repo's tooling. No
network is touched: `send_message`'s single `urllib.request.urlopen` call is
monkeypatched. The invariants pinned here are the ones the audit script relies
on: no-op without config, best-effort (main() always exits 0), token redaction,
truncation, and the chat-id resolution order.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import telegram_notify as tn  # noqa: E402


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@contextlib.contextmanager
def _env(**values: str):
    """Set exactly the given TELEGRAM_* env vars for the block; restore after."""
    keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ANNOUNCE_TO", "TELEGRAM_ALLOW_FROM"]
    import os

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


class ConfigTest(unittest.TestCase):
    def test_no_config_is_none(self) -> None:
        with _env():
            self.assertIsNone(tn.telegram_config())

    def test_token_without_chat_is_none(self) -> None:
        with _env(TELEGRAM_BOT_TOKEN="t"):
            self.assertIsNone(tn.telegram_config())

    def test_announce_to_preferred(self) -> None:
        with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ANNOUNCE_TO="99", TELEGRAM_ALLOW_FROM="11,22"):
            self.assertEqual(tn.telegram_config(), ("t", "99"))

    def test_falls_back_to_first_allow_from(self) -> None:
        with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ALLOW_FROM=" 11 , 22 "):
            self.assertEqual(tn.telegram_config(), ("t", "11"))

    def test_explicit_override_wins(self) -> None:
        with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ANNOUNCE_TO="99"):
            self.assertEqual(tn.telegram_config("42"), ("t", "42"))


class FormatTest(unittest.TestCase):
    def test_title_lines_text(self) -> None:
        msg = tn.format_message("Title", ["a", "", "b"], "body")
        self.assertEqual(msg, "Title\n• a\n• b\nbody")

    def test_truncation(self) -> None:
        msg = tn.format_message("x" * 5000)
        self.assertEqual(len(msg), tn.MAX_MESSAGE_CHARS)
        self.assertTrue(msg.endswith(tn.TRUNCATION_MARKER))

    def test_redaction(self) -> None:
        self.assertEqual(tn.redact_token("url/bot SECRET /x", "SECRET"), "url/bot *** /x")


class SendTest(unittest.TestCase):
    def test_success(self) -> None:
        calls: list = []

        def fake_urlopen(request, timeout=0):  # noqa: ANN001
            calls.append(request)
            return _FakeResponse({"ok": True})

        with unittest.mock.patch.object(tn.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(tn.send_message("tok", "42", "hi"))
        self.assertEqual(len(calls), 1)
        self.assertIn("/bottok/sendMessage", calls[0].full_url)

    def test_empty_text_not_sent(self) -> None:
        self.assertFalse(tn.send_message("tok", "42", "   "))

    def test_http_error_is_false_not_raised(self) -> None:
        def boom(request, timeout=0):  # noqa: ANN001
            raise urllib.error.URLError("bottok is in this url")

        buf = io.StringIO()
        with unittest.mock.patch.object(tn.urllib.request, "urlopen", boom):
            with contextlib.redirect_stderr(buf):
                self.assertFalse(tn.send_message("tok", "42", "hi"))
        # the token must be redacted out of the warning
        self.assertNotIn("bottok", buf.getvalue())

    def test_api_not_ok_is_false(self) -> None:
        def not_ok(request, timeout=0):  # noqa: ANN001
            return _FakeResponse({"ok": False, "description": "bad"})

        with unittest.mock.patch.object(tn.urllib.request, "urlopen", not_ok):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(tn.send_message("tok", "42", "hi"))


class MainTest(unittest.TestCase):
    def _run(self, argv: list[str]) -> int:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with unittest.mock.patch.object(sys, "argv", ["telegram_notify.py", *argv]):
                return tn.main()

    def test_no_config_exits_zero(self) -> None:
        with _env():
            self.assertEqual(self._run(["--title", "x"]), 0)

    def test_empty_message_exits_zero(self) -> None:
        with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ANNOUNCE_TO="1"):
            self.assertEqual(self._run([]), 0)

    def test_send_failure_still_exits_zero(self) -> None:
        def boom(request, timeout=0):  # noqa: ANN001
            raise OSError("network down")

        with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ANNOUNCE_TO="1"):
            with unittest.mock.patch.object(tn.urllib.request, "urlopen", boom):
                self.assertEqual(self._run(["--title", "x"]), 0)

    def test_report_file_becomes_body(self) -> None:
        sent: list = []

        def fake_urlopen(request, timeout=0):  # noqa: ANN001
            sent.append(request.data.decode("utf-8"))
            return _FakeResponse({"ok": True})

        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "nightly-report.md"
            report.write_text("AUDIT green\n", encoding="utf-8")
            with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ANNOUNCE_TO="1"):
                with unittest.mock.patch.object(tn.urllib.request, "urlopen", fake_urlopen):
                    self.assertEqual(self._run(["--report", str(report)]), 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("AUDIT+green", sent[0])  # urlencoded body

    def test_missing_report_file_exits_zero(self) -> None:
        with _env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_ANNOUNCE_TO="1"):
            self.assertEqual(self._run(["--report", "/no/such/file.md"]), 0)


if __name__ == "__main__":
    unittest.main()
