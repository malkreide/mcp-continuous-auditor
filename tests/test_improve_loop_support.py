#!/usr/bin/env python3
"""Tests for scripts/improve_loop_support.py — report aggregation + draft-PR
publishing of the Phase-6c improve loop. Stdlib-only; the GitHub opener is
injected, so no network and no real token are used.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import improve_loop_support as ils  # noqa: E402


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.journal = self.dir / "experiments.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def report(self, skip: int = 0) -> dict:
        summary = self.dir / "s.json"
        rc = ils.main([
            "report",
            "--journal", str(self.journal),
            "--skip-lines", str(skip),
            "--target", "o/r", "--sha", "abc1234", "--branch", "improve/2026-07-13",
            "--out-report", str(self.dir / "r.md"),
            "--out-summary", str(summary),
        ])
        self.assertEqual(rc, 0)
        return json.loads(summary.read_text())

    def test_aggregates_keeps_and_discards(self) -> None:
        entries = [
            {"verdict": "keep", "candidate": "candidate-1.patch",
             "candidate_sha": "aaa", "killed_mutant": "m2.patch", "dauer_s": 1.5},
            {"verdict": "discard", "grund": "flaky", "candidate": "candidate-2.patch",
             "dauer_s": 2.0},
            {"verdict": "discard", "grund": "redundant", "candidate": "candidate-3.patch",
             "dauer_s": 0.5},
            {"verdict": "discard", "grund": "flaky", "candidate": "candidate-4.patch",
             "dauer_s": 1.0},
        ]
        self.journal.write_text("".join(json.dumps(e) + "\n" for e in entries))
        s = self.report()
        self.assertEqual(s["outcome"], "completed")
        self.assertEqual(s["iterations"], 4)
        self.assertEqual(len(s["keeps"]), 1)
        self.assertEqual(s["keeps"][0]["killed_mutant"], "m2.patch")
        self.assertEqual(s["discards"], {"flaky": 2, "redundant": 1})
        self.assertEqual(s["dauer_s"], 5.0)
        report = (self.dir / "r.md").read_text()
        self.assertIn("behalten: **1**", report)
        self.assertIn("2× flaky", report)
        self.assertIn("m2.patch", report)

    def test_skip_lines_isolates_this_run(self) -> None:
        old = {"verdict": "keep", "candidate": "old.patch", "dauer_s": 1}
        new = {"verdict": "discard", "grund": "redundant", "candidate": "new.patch", "dauer_s": 1}
        self.journal.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
        s = self.report(skip=1)
        self.assertEqual(s["iterations"], 1)
        self.assertEqual(s["keeps"], [])

    def test_hard_fail_entry_marks_outcome(self) -> None:
        self.journal.write_text(
            json.dumps({"verdict": "hard-fail", "grund": "runner failed", "dauer_s": 1}) + "\n"
        )
        s = self.report()
        self.assertEqual(s["outcome"], "hard-fail")
        self.assertIn("HARD FAILURE", (self.dir / "r.md").read_text())

    def test_empty_journal_reports_zero_iterations(self) -> None:
        s = self.report()
        self.assertEqual(s["iterations"], 0)
        self.assertIn("Keine Keeps", (self.dir / "r.md").read_text())


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.body = Path(self.tmp.name) / "report.md"
        self.body.write_text("# report\n")
        self.requests: list[urllib.request.Request] = []
        self._saved = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "test-token-not-real"

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = self._saved
        self.tmp.cleanup()

    def publish(self, responses: list[object]) -> int:
        it = iter(responses)

        def opener(req: urllib.request.Request) -> _FakeResponse:
            self.requests.append(req)
            return _FakeResponse(json.dumps(next(it)).encode())

        return ils.main([
            "publish",
            "--repo", "owner/target", "--branch", "improve/2026-07-13",
            "--base", "main", "--title", "improve: weekly run",
            "--body-file", str(self.body),
        ], opener=opener)

    def test_creates_draft_pr(self) -> None:
        rc = self.publish([[], {"html_url": "https://github.com/owner/target/pull/9"}])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.requests), 2)
        get_req, post_req = self.requests
        self.assertEqual(get_req.get_method(), "GET")
        self.assertIn("head=owner:improve/2026-07-13", get_req.full_url)
        self.assertEqual(post_req.get_method(), "POST")
        payload = json.loads(post_req.data.decode())
        self.assertTrue(payload["draft"])
        self.assertEqual(payload["base"], "main")
        self.assertEqual(payload["body"], "# report\n")
        self.assertEqual(
            post_req.get_header("Authorization"), "Bearer test-token-not-real"
        )

    def test_existing_open_pr_is_reused(self) -> None:
        rc = self.publish([[{"html_url": "https://github.com/owner/target/pull/7"}]])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.requests), 1)  # no POST — idempotent

    def test_missing_token_fails(self) -> None:
        os.environ["GITHUB_TOKEN"] = ""
        self.assertEqual(self.publish([[]]), 1)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
