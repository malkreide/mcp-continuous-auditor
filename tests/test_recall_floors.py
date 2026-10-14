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
import urllib.error
from pathlib import Path
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import drift_issue as di  # noqa: E402
import live_probe as lp  # noqa: E402
import recall_canary as rc  # noqa: E402


def _outputs(path: Path) -> dict[str, str]:
    """$GITHUB_OUTPUT as a dict. A key that was never written is absent."""
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def _route(name: str, out: dict[str, str], report: Path) -> str:
    """What drift_issue.py makes of one probe step — the spec exactly as
    live-probe.yml.template builds it: `NAME:OUTCOME:ALERT:REPORT`, where an
    alert the step never wrote arrives as the empty string."""
    spec = f"{name}:success:{out.get('alert', '')}:{report.name}"
    return di.classify([di.parse_probe(spec, report.parent)])


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


class _LiveProbeRun(unittest.TestCase):
    """Runs live_probe.main() offline. Holds no tests of its own."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _run(
        self, payload, probe_extra: dict, *, fetch=None, probes=None
    ) -> tuple[str, dict]:
        """Drive live_probe.main() with the fetch and fixture stubbed out."""
        manifest = self.dir / "manifest.json"
        if probes is None:
            probes = [
                {
                    "name": "p",
                    "fixture": "f",
                    "url": "https://example.invalid/x",
                    **probe_extra,
                }
            ]
        manifest.write_text(json.dumps({"probes": probes}), encoding="utf-8")

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
            lp._fetch = fetch or (lambda probe: payload)
            lp._load_fixture = lambda name: payload  # identical → no schema drift
            self.assertEqual(lp.main(), 0)
        finally:
            lp._MANIFEST, lp._fetch, lp._load_fixture = (
                orig_manifest,
                orig_fetch,
                orig_fixture,
            )

        report = (self.dir / "report.md").read_text(encoding="utf-8")
        return report, _outputs(outputs)


class LiveProbeRecallReportTest(_LiveProbeRun):
    """A floor breach must be reported and signalled — separately from drift."""

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
        # The floor was never compared, so the run may not clear anything.
        self.assertNotIn("alert", out)

    def test_probe_without_min_count_is_unaffected(self):
        report, out = self._run({"entries": []}, {})
        self.assertNotIn("Recall below floor", report)
        self.assertEqual(out["recall_drop"], "false")
        self.assertEqual(out["alert"], "false")
        self.assertEqual(_route("live-probe", out, self.dir / "report.md"), di.CLEAR)


class LiveProbeVerdictTest(_LiveProbeRun):
    """A probe that failed compared nothing, and the router must hear that.

    `datastore_search_sql` answered HTTP 409 every week until 2026-10, the probe
    wrote `alert=false`, and drift_issue.py — which only knows what `alert`
    says — classified the run `clear`: the state that closes the tracking
    issue. The report said "probe failed"; the verdict said "nothing found".
    """

    def _fails(self, probe):
        raise urllib.error.HTTPError(probe["url"], 409, "Conflict", None, None)

    def test_a_failed_fetch_writes_no_verdict(self):
        report, out = self._run({}, {}, fetch=self._fails)
        self.assertIn("HTTP Error 409", report)
        self.assertNotIn("alert", out)
        self.assertEqual(out["drift"], "false")
        self.assertEqual(_route("live-probe", out, self.dir / "report.md"), di.UNKNOWN)

    def test_one_failed_probe_keeps_the_others_from_clearing(self):
        probes = [
            {"name": "ok", "fixture": "f", "url": "https://example.invalid/ok"},
            {"name": "bad", "fixture": "f", "url": "https://example.invalid/bad"},
        ]

        def fetch(probe):
            if probe["name"] == "bad":
                self._fails(probe)
            return {"entries": [1]}

        report, out = self._run({"entries": [1]}, {}, fetch=fetch, probes=probes)
        self.assertIn("`ok` — structurally in sync", report)
        self.assertNotIn("alert", out)
        self.assertEqual(_route("live-probe", out, self.dir / "report.md"), di.UNKNOWN)

    def test_drift_is_a_finding_even_when_a_sibling_failed(self):
        probes = [
            {"name": "moved", "fixture": "f", "url": "https://example.invalid/m"},
            {"name": "bad", "fixture": "f", "url": "https://example.invalid/bad"},
        ]

        def fetch(probe):
            if probe["name"] == "bad":
                self._fails(probe)
            return {"entries": [1], "new_field": "x"}

        _, out = self._run({"entries": [1]}, {}, fetch=fetch, probes=probes)
        self.assertEqual(out["alert"], "true")
        self.assertEqual(_route("live-probe", out, self.dir / "report.md"), di.FINDING)

    def test_an_empty_manifest_compared_nothing(self):
        _, out = self._run({}, {}, probes=[])
        self.assertNotIn("alert", out)
        self.assertEqual(_route("live-probe", out, self.dir / "report.md"), di.UNKNOWN)


class AlertVerdictTest(unittest.TestCase):
    """The one rule both probes hand to drift_issue.py."""

    def test_a_finding_is_true_whatever_else_failed(self):
        self.assertEqual(lp.alert_verdict(finding=True, incomplete=False), "true")
        self.assertEqual(lp.alert_verdict(finding=True, incomplete=True), "true")

    def test_complete_and_quiet_is_false(self):
        self.assertEqual(lp.alert_verdict(finding=False, incomplete=False), "false")

    def test_incomplete_and_quiet_is_no_verdict(self):
        self.assertIsNone(lp.alert_verdict(finding=False, incomplete=True))


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

    def test_tool_payload_refuses_an_error_result(self):
        """`Unknown tool`, a validation error, a tool that raised: the server
        answers all of them with an ordinary result flagged as an error. Parsed
        as a payload, the text was then "uncountable" and the report advised
        fixing `count_path` — for a tool that does not exist."""
        for flag in ("is_error", "isError"):  # SDK 2.x / SDK 1.x spelling
            with self.subTest(flag=flag):
                result = _Result("Unknown tool: search_terms")
                setattr(result, flag, True)
                with self.assertRaises(rc.ToolCallError) as cm:
                    rc._tool_payload(result)
                self.assertIn("Unknown tool: search_terms", str(cm.exception))

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


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    """A CallToolResult as the official SDK 2.x client returns it."""

    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_Text(text)]
        self.is_error = is_error


class _Client:
    """Stands in for the in-memory client; `answer(tool, args)` is the server."""

    def __init__(self, answer) -> None:
        self.answer = answer

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def call_tool(self, tool: str, args: dict):
        return self.answer(tool, args)


def _entries(n: int) -> _Result:
    return _Result(json.dumps({"entries": list(range(n))}))


class RecallCanaryRoutingTest(unittest.TestCase):
    """What the canary hands to drift_issue.py, through main() and _call().

    Measured 2026-10-14 against zurich-opendata-mcp with the shipped example
    manifest: every tool it names is absent from that server, every call failed
    — and the canary wrote `alert=false`, which drift_issue.py classified
    `clear`, the state that closes the tracking issue. Each part was tested on
    its own; the handover between them was not.
    """

    CANARIES: ClassVar[list[dict[str, Any]]] = [
        {"name": "a", "tool": "t", "args": {"q": "a"}, "min_count": 10},
        {"name": "b", "tool": "t", "args": {"q": "b"}, "min_count": 1},
    ]

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _run(self, answer) -> tuple[str, dict, str]:
        manifest = self.dir / "manifest.json"
        manifest.write_text(json.dumps({"canaries": self.CANARIES}), encoding="utf-8")
        report_path = self.dir / "recall-canary-report.md"
        outputs = self.dir / "gh_out"
        os.environ["RECALL_REPORT"] = str(report_path)
        os.environ["GITHUB_OUTPUT"] = str(outputs)

        saved = rc._MANIFEST, rc._load_server, rc.in_memory_client
        try:
            rc._MANIFEST = manifest
            rc._load_server = lambda: object()
            rc.in_memory_client = lambda server: _Client(answer)
            self.assertEqual(rc.main(), 0)
        finally:
            rc._MANIFEST, rc._load_server, rc.in_memory_client = saved

        out = _outputs(outputs)
        report = report_path.read_text(encoding="utf-8")
        return report, out, _route("recall-canary", out, report_path)

    def test_a_tool_the_server_lacks_is_a_failed_call_and_clears_nothing(self):
        report, out, state = self._run(
            lambda tool, args: _Result(f"Unknown tool: {tool}", is_error=True)
        )
        self.assertIn("call failed", report)
        self.assertIn("Unknown tool: t", report)
        self.assertNotIn("count_path", report, "the manifest path is not the cause")
        self.assertEqual(out["errors"], "true")
        self.assertNotIn("alert", out)
        self.assertEqual(state, di.UNKNOWN)

    def test_one_failed_canary_keeps_the_run_from_clearing(self):
        def answer(tool, args):
            if args["q"] == "b":
                return _Result("upstream 503", is_error=True)
            return _entries(20)

        report, out, state = self._run(answer)
        self.assertIn("`a` (`t`) — 20 ≥ floor 10", report)
        self.assertNotIn("alert", out)
        self.assertEqual(state, di.UNKNOWN)

    def test_an_uncountable_answer_clears_nothing(self):
        report, out, state = self._run(lambda tool, args: _Result("## Markdown"))
        self.assertIn("no countable collection", report)
        self.assertNotIn("alert", out)
        self.assertEqual(state, di.UNKNOWN)

    def test_a_breach_is_a_finding_even_when_a_sibling_failed(self):
        def answer(tool, args):
            if args["q"] == "b":
                return _Result("upstream 503", is_error=True)
            return _entries(1)

        _, out, state = self._run(answer)
        self.assertEqual(out["alert"], "true")
        self.assertEqual(state, di.FINDING)

    def test_every_canary_measured_above_its_floor_clears(self):
        _, out, state = self._run(lambda tool, args: _entries(20))
        self.assertEqual(out["errors"], "false")
        self.assertEqual(out["alert"], "false")
        self.assertEqual(state, di.CLEAR)


if __name__ == "__main__":
    unittest.main()
