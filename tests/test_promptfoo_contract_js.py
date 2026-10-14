#!/usr/bin/env python3
"""The shared promptfoo assertions (promptfoo/assertions/contract.js), driven by
node directly.

They decide whether the determ and graded profiles pass, and until now nothing
in this repository exercised them except a promptfoo run against a target. The
cases that matter most are the REFUSALS: a missing request log is "not
measured", never "nothing forwarded"; a query that went upstream unescaped
fails even when the escaped form went too; an error text is never a result.

Needs `node` on PATH (GitHub's ubuntu runners have it). Without it the class
skips with the reason "node: no such executable on PATH".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

CONTRACT = (
    Path(__file__).resolve().parents[1] / "promptfoo" / "assertions" / "contract.js"
)
NODE = shutil.which("node")

FENCED = "## SQL-Ergebnis\n\n```json\n{rows}\n```"


def _call(fn: str, output: str, context: dict) -> dict:
    script = (
        f"const c = require({json.dumps(str(CONTRACT))});"
        f"process.stdout.write(JSON.stringify(c[{json.dumps(fn)}]("
        f"{json.dumps(output)}, {json.dumps(context)})));"
    )
    done = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(done.stdout)


def _ctx(args: dict | None = None, requests: list | None = None) -> dict:
    ctx: dict = {"vars": {"args": json.dumps(args or {})}}
    if requests is not None:
        ctx["providerResponse"] = {"metadata": {"requests": requests}}
    return ctx


@unittest.skipUnless(NODE, "node: no such executable on PATH")
class ContractAssertionsTest(unittest.TestCase):
    def test_rows_within_limit_pass(self) -> None:
        out = FENCED.format(rows=json.dumps([{"a": 1}, {"a": 2}]))
        r = _call("datastoreRows", out, _ctx({"params": {"sql": "SELECT * LIMIT 5"}}))
        self.assertTrue(r["pass"], r)

    def test_rows_beyond_limit_fail(self) -> None:
        out = FENCED.format(rows=json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]))
        r = _call("datastoreRows", out, _ctx({"params": {"sql": "SELECT * LIMIT 2"}}))
        self.assertFalse(r["pass"], r)
        self.assertIn("3 records for LIMIT 2", r["reason"])

    def test_rows_that_are_not_objects_fail(self) -> None:
        out = FENCED.format(rows=json.dumps([{"a": 1}, "flat string"]))
        self.assertFalse(_call("datastoreRows", out, _ctx())["pass"])

    def test_an_error_text_is_never_a_result(self) -> None:
        """The state the profiles were in: an error message standing in for the
        tool. Every function that reads output must refuse it."""
        err = "Error executing tool zurich_datastore_sql: 1 validation error"
        for fn in ("datastoreRows", "datastoreEmpty", "notAnError"):
            with self.subTest(fn=fn):
                self.assertFalse(_call(fn, err, _ctx())["pass"])
        self.assertFalse(_call("notAnError", "Fehler: Nur SELECT", _ctx())["pass"])

    def test_empty_result_with_rows_fails(self) -> None:
        out = FENCED.format(rows=json.dumps([{"a": 1}]))
        self.assertFalse(_call("datastoreEmpty", out, _ctx())["pass"])
        self.assertTrue(
            _call("datastoreEmpty", "SQL-Abfrage lieferte keine Ergebnisse.", _ctx())[
                "pass"
            ]
        )

    def test_not_forwarded_needs_the_log_and_an_empty_one(self) -> None:
        self.assertTrue(_call("notForwarded", "Fehler", _ctx(requests=[]))["pass"])
        sent = _ctx(requests=[{"url": "https://x", "params": {"sql": "DELETE"}}])
        self.assertFalse(_call("notForwarded", "Fehler", sent)["pass"])
        missing = _call("notForwarded", "Fehler", _ctx())
        self.assertFalse(missing["pass"], "no log must never read as 'nothing sent'")
        self.assertIn("not measured", missing["reason"])

    def test_quote_must_arrive_doubled_and_only_doubled(self) -> None:
        args = {"params": {"query": "x'; DROP TABLE y;--"}}
        ok = [{"params": {"sql": "WHERE t ILIKE '%x''; DROP TABLE y;--%'"}}]
        raw = [{"params": {"sql": "WHERE t ILIKE '%x'; DROP TABLE y;--%'"}}]
        both = ok + raw
        self.assertTrue(_call("quoteDoubledUpstream", "", _ctx(args, ok))["pass"])
        self.assertFalse(_call("quoteDoubledUpstream", "", _ctx(args, raw))["pass"])
        self.assertFalse(_call("quoteDoubledUpstream", "", _ctx(args, both))["pass"])
        self.assertFalse(_call("quoteDoubledUpstream", "", _ctx(args, []))["pass"])
        self.assertFalse(_call("quoteDoubledUpstream", "", _ctx(args))["pass"])

    def test_forwarded_means_the_exact_query_went_upstream(self) -> None:
        """notForwarded's counterpart. Without it a guard that refused every
        query would pass every refusal test."""
        sql = 'SELECT "a" FROM "t1" UNION SELECT "a" FROM "t2"'
        args = {"params": {"sql": sql}}
        rows = FENCED.format(rows=json.dumps([{"a": 1}]))
        sent = [{"url": "https://x", "params": {"sql": sql}}]
        self.assertTrue(_call("forwardedVerbatim", rows, _ctx(args, sent))["pass"])

        altered = [{"url": "https://x", "params": {"sql": sql + " LIMIT 1"}}]
        self.assertFalse(_call("forwardedVerbatim", rows, _ctx(args, altered))["pass"])
        self.assertFalse(_call("forwardedVerbatim", rows, _ctx(args, []))["pass"])
        missing = _call("forwardedVerbatim", rows, _ctx(args))
        self.assertFalse(missing["pass"])
        self.assertIn("not measured", missing["reason"])
        refused = _call("forwardedVerbatim", "Fehler: nur DataStore", _ctx(args, sent))
        self.assertFalse(refused["pass"], "an error text is never a forwarded result")
        self.assertFalse(_call("forwardedVerbatim", rows, _ctx({}, sent))["pass"])


if __name__ == "__main__":
    unittest.main()
