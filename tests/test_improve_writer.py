#!/usr/bin/env python3
"""Tests for scripts/improve_writer.py — the Phase-6c proposal step.

Stdlib-only (`python3 -m unittest`). The Anthropic transport is injected, so
no network and no real key are used; a placeholder ANTHROPIC_API_KEY is set
per test. Covers the WRITER_CMD exit contract (0 proposed / 10 no-proposal /
1 hard-fail), fence unwrapping, refusal handling, and token accounting.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import improve_writer as iw  # noqa: E402

DIFF = (
    "--- a/promptfoo/promptfooconfig.determ.yaml\n"
    "+++ b/promptfoo/promptfooconfig.determ.yaml\n"
    "@@ -1 +1,2 @@\n"
    " test-base PASS\n"
    "+test-new PASS\n"
)


def response(text: str, stop: str = "end_turn", tokens: tuple[int, int] = (100, 50)) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


class ImproveWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.target = root / "target"
        (self.target / "promptfoo").mkdir(parents=True)
        (self.target / "promptfoo/promptfooconfig.determ.yaml").write_text("test-base PASS\n")
        self.patch = root / "candidate.patch"
        self._env = {
            "ANTHROPIC_API_KEY": "test-key-not-real",
            "IMPROVE_JOURNAL": str(root / "experiments.jsonl"),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_writer(self, resp: dict | Exception) -> int:
        def transport(url: str, headers: dict, body: bytes, timeout: float) -> dict:
            self.last_url, self.last_headers = url, headers
            if isinstance(resp, Exception):
                raise resp
            return resp

        return iw.main([str(self.target), str(self.patch)], transport=transport)

    # -- the WRITER_CMD exit contract ---------------------------------------
    def test_diff_response_is_written(self) -> None:
        self.assertEqual(self.run_writer(response(DIFF)), iw.EXIT_PROPOSED)
        self.assertEqual(self.patch.read_text(), DIFF)
        self.assertEqual(
            Path(str(self.patch) + ".tokens").read_text().strip(), "150"
        )
        self.assertTrue(self.last_url.endswith("/v1/messages"))
        self.assertEqual(self.last_headers["x-api-key"], "test-key-not-real")

    def test_fenced_diff_is_unwrapped(self) -> None:
        self.assertEqual(
            self.run_writer(response(f"```diff\n{DIFF}```")), iw.EXIT_PROPOSED
        )
        self.assertEqual(self.patch.read_text(), DIFF)

    def test_no_proposal_ends_run(self) -> None:
        self.assertEqual(self.run_writer(response("NO-PROPOSAL")), iw.EXIT_NO_PROPOSAL)
        self.assertFalse(self.patch.exists())

    def test_refusal_ends_run_gracefully(self) -> None:
        self.assertEqual(
            self.run_writer(response("", stop="refusal")), iw.EXIT_NO_PROPOSAL
        )
        self.assertFalse(self.patch.exists())
        # the call happened — its cost must still reach the budget guard
        self.assertEqual(
            Path(str(self.patch) + ".tokens").read_text().strip(), "150"
        )

    def test_non_diff_output_is_hard_fail(self) -> None:
        self.assertEqual(
            self.run_writer(response("Sure! Here is my plan: ...")), iw.EXIT_HARD_FAIL
        )
        self.assertFalse(self.patch.exists())

    def test_transport_error_is_hard_fail(self) -> None:
        self.assertEqual(
            self.run_writer(RuntimeError("API unreachable")), iw.EXIT_HARD_FAIL
        )

    def test_missing_api_key_is_hard_fail(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = ""
        self.assertEqual(self.run_writer(response(DIFF)), iw.EXIT_HARD_FAIL)

    # -- prompt assembly -----------------------------------------------------
    def test_journal_tail_reaches_the_prompt(self) -> None:
        Path(os.environ["IMPROVE_JOURNAL"]).write_text(
            '{"verdict": "discard", "grund": "flaky", "candidate": "candidate-1.patch"}\n'
        )
        captured: dict = {}

        def transport(url: str, headers: dict, body: bytes, timeout: float) -> dict:
            import json

            captured.update(json.loads(body))
            return response(DIFF)

        self.assertEqual(
            iw.main([str(self.target), str(self.patch)], transport=transport),
            iw.EXIT_PROPOSED,
        )
        user_msg = captured["messages"][0]["content"]
        self.assertIn("discard(flaky): candidate-1.patch", user_msg)
        self.assertIn("test-base PASS", user_msg)  # current config is included
        self.assertIn("NO-PROPOSAL", captured["system"])  # output contract
        self.assertEqual(captured["thinking"], {"type": "adaptive"})


if __name__ == "__main__":
    unittest.main()
