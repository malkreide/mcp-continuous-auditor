#!/usr/bin/env python3
"""Tests for the recall-floor layer: live_probe.count_records + recall_canary.

The failure these guard against is a quiet one. A structural signature diff is
blind to a collapsed result set — same JSON shape, a twentieth of the records —
which is precisely how termdat-mcp#11 survived a full audit and 33 green tests.
So the assertions below care about the *values* the rest of live_probe.py
deliberately ignores.

Stdlib-only (`python3 -m unittest`), matching the rest of the repo's tooling.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import live_probe as lp  # noqa: E402
import recall_canary as rc  # noqa: E402


class CountRecordsTest(unittest.TestCase):
    """count_records must find the collection, or admit it cannot."""

    def test_top_level_list(self):
        self.assertEqual(lp.count_records([1, 2, 3]), 3)

    def test_empty_list_is_zero_not_none(self):
        # The distinction matters: 0 breaches a floor, None is a manifest bug.
        self.assertEqual(lp.count_records([]), 0)

    def test_ckan_datastore_shape(self):
        payload = {"success": True, "result": {"records": [{"a": 1}, {"a": 2}]}}
        self.assertEqual(lp.count_records(payload), 2)

    def test_geojson_feature_collection(self):
        payload = {"type": "FeatureCollection", "features": [{}, {}, {}, {}]}
        self.assertEqual(lp.count_records(payload), 4)

    def test_explicit_count_path_wins_over_inference(self):
        payload = {"features": [1, 2, 3], "entries": [1]}
        self.assertEqual(lp.count_records(payload, "entries"), 1)

    def test_nested_count_path(self):
        payload = {"result": {"inner": {"rows": [1, 2]}}}
        self.assertEqual(lp.count_records(payload, "result.inner.rows"), 2)

    def test_unresolvable_count_path_is_none(self):
        self.assertIsNone(lp.count_records({"entries": []}, "nope.missing"))

    def test_uncountable_payload_is_none(self):
        self.assertIsNone(lp.count_records({"status": "ok"}))

    def test_scalar_at_count_path_is_none(self):
        # A number where a collection was expected is a manifest error, not a count.
        self.assertIsNone(lp.count_records({"total": 42}, "total"))


class LiveProbeRecallReportTest(unittest.TestCase):
    """A floor breach must be reported and signalled — separately from drift."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _run(self, payload, probe_extra: dict) -> tuple[str, dict]:
        """Drive live_probe.main() with the fetch and fixture stubbed out."""
        manifest = self.dir / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "probes": [
                        {
                            "name": "p",
                            "fixture": "f",
                            "url": "https://example.invalid/x",
                            **probe_extra,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        outputs = self.dir / "gh_out"
        os.environ["DRIFT_REPORT"] = str(self.dir / "report.md")
        os.environ["GITHUB_OUTPUT"] = str(outputs)

        orig_manifest, orig_fetch, orig_fixture = (
            lp._MANIFEST,
            lp._fetch,
            lp._load_fixture,
        )
        try:
            lp._MANIFEST = manifest
            lp._fetch = lambda probe: payload
            lp._load_fixture = lambda name: payload  # identical → no schema drift
            self.assertEqual(lp.main(), 0)
        finally:
            lp._MANIFEST, lp._fetch, lp._load_fixture = (
                orig_manifest,
                orig_fetch,
                orig_fixture,
            )

        report = (self.dir / "report.md").read_text(encoding="utf-8")
        parsed = dict(
            line.split("=", 1)
            for line in outputs.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        return report, parsed

    def test_breached_floor_sets_recall_drop_and_alert_but_not_drift(self):
        report, out = self._run(
            {"entries": [1]}, {"min_count": 10, "count_path": "entries"}
        )
        self.assertIn("Recall below floor", report)
        self.assertIn("**1** record(s), floor is **10**", report)
        self.assertEqual(out["recall_drop"], "true")
        self.assertEqual(out["alert"], "true")
        self.assertEqual(out["drift"], "false", "a recall drop is not schema drift")

    def test_satisfied_floor_is_quiet(self):
        report, out = self._run(
            {"entries": list(range(20))}, {"min_count": 10, "count_path": "entries"}
        )
        self.assertNotIn("Recall below floor", report)
        self.assertIn("recall 20 ≥ floor 10", report)
        self.assertEqual(out["alert"], "false")

    def test_zero_records_breaches_the_floor(self):
        # The termdat case: a well-formed, entirely empty result.
        _, out = self._run({"entries": []}, {"min_count": 1, "count_path": "entries"})
        self.assertEqual(out["recall_drop"], "true")

    def test_uncountable_payload_is_an_error_not_a_silent_pass(self):
        report, out = self._run({"status": "ok"}, {"min_count": 5})
        self.assertIn("no countable collection", report)
        self.assertEqual(out["recall_drop"], "false")

    def test_probe_without_min_count_is_unaffected(self):
        report, out = self._run({"entries": []}, {})
        self.assertNotIn("Recall below floor", report)
        self.assertEqual(out["recall_drop"], "false")
        self.assertEqual(out["alert"], "false")


class RecallCanaryTest(unittest.TestCase):
    """The canary drives the server's own tools; evaluate() is the pure core."""

    CANARIES: ClassVar[list[dict[str, Any]]] = [
        {
            "name": "many",
            "tool": "search",
            "args": {"q": "a"},
            "min_count": 10,
            "count_path": "entries",
        },
        {
            "name": "few",
            "tool": "search",
            "args": {"q": "b"},
            "min_count": 1,
            "count_path": "entries",
        },
    ]

    def test_floor_breach_is_reported_per_canary(self):
        payloads = {"a": {"entries": [1]}, "b": {"entries": [1, 2]}}
        recall, errors, ok = rc.evaluate(
            self.CANARIES, lambda tool, args: payloads[args["q"]]
        )
        self.assertEqual(len(recall), 1)
        self.assertIn("`many`", recall[0])
        self.assertEqual(errors, [])
        self.assertEqual(len(ok), 1)

    def test_a_failing_call_does_not_stop_the_rest(self):
        def caller(tool, args):
            if args["q"] == "a":
                raise RuntimeError("upstream 503")
            return {"entries": [1, 2]}

        _recall, errors, ok = rc.evaluate(self.CANARIES, caller)
        self.assertEqual(len(errors), 1)
        self.assertIn("upstream 503", errors[0])
        self.assertEqual(len(ok), 1, "the second canary must still have run")

    def test_uncountable_output_is_an_error(self):
        recall, errors, _ok = rc.evaluate(
            [{"name": "x", "tool": "t", "min_count": 1, "count_path": "nope"}],
            lambda tool, args: {"entries": [1]},
        )
        self.assertEqual(recall, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no countable collection", errors[0])

    def test_report_names_the_scope_default_as_first_suspect(self):
        report = rc.build_report(self.CANARIES, ["- 📉 `many`: 1"], [], [])
        self.assertIn("Recall below floor", report)
        self.assertIn("omitted", report)

    def test_tool_payload_parses_a_json_text_block(self):
        class Block:
            text = '{"entries": [1, 2, 3]}'

        class Result:
            content = [Block()]  # noqa: RUF012 - throwaway stub, not shared state

        self.assertEqual(rc._tool_payload(Result()), {"entries": [1, 2, 3]})

    def test_tool_payload_falls_back_to_structured_content(self):
        class Result:
            content = None
            structured_content = {"entries": [1]}  # noqa: RUF012 - throwaway stub, not shared state

        self.assertEqual(rc._tool_payload(Result()), {"entries": [1]})

    def test_manifest_ships_valid_json_with_required_keys(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "recall_canary.manifest.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("canaries", data)
        for entry in data["canaries"]:
            for key in ("name", "tool", "min_count"):
                self.assertIn(key, entry, f"{entry.get('name')} lacks {key}")
            self.assertIsInstance(entry["min_count"], int)


if __name__ == "__main__":
    unittest.main()
