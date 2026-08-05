#!/usr/bin/env python3
"""Tests for scripts/sync_findings_issues.py — deterministic issue routing (U-C).

Covers the pure decision logic — which state each label is in, and what that
plans — plus `sync()`'s REST calls against a recorder and `main()`'s no-network
paths. No GitHub calls are made. Stdlib-only, matching the rest of the suite.

Most of these are about the CLOSE. Opening a duplicate issue is a nuisance;
closing one on a gate that never produced a verdict puts a "fixed" stamp on a
comparison that never happened, and the finding goes with it. Every test that
names `unknown` is guarding that direction.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_findings_issues as sfi  # noqa: E402


def _summary(**over: object) -> dict:
    base = {
        "outcome": "findings",
        "target": "o/r",
        "target_sha": "abc1234",
        "schema_drift": False,
        "redteam": False,
        "other_findings": False,
        "toolchain_fail": False,
        # The evidence flags. These defaults describe a run in which every gate
        # actually produced a verdict; the tests that care flip them one at a
        # time, because each one alone must be enough to block a close.
        "graded_layer_ran": True,
        "transport_boot_unmeasured": False,
        "lockfile_unmeasured": False,
        "host_allowlist_unconfigured": False,
    }
    base.update(over)
    return base


def _state(summary: dict, label: str) -> str:
    for entry in sfi.label_states(summary):
        if entry["label"] == label:
            return entry["state"]
    raise AssertionError(f"no entry for label {label!r}")


class FindingClassesTest(unittest.TestCase):
    def test_green_summary_routes_nothing(self) -> None:
        self.assertEqual(sfi.finding_classes(_summary(outcome="green")), [])

    def test_hard_fail_routes_nothing(self) -> None:
        self.assertEqual(
            sfi.finding_classes(_summary(outcome="hard-fail", schema_drift=True)), []
        )

    def test_schema_and_redteam_are_two_issues(self) -> None:
        cls = sfi.finding_classes(_summary(schema_drift=True, redteam=True))
        labels = [c["label"] for c in cls]
        self.assertEqual(labels, ["schema-drift", "redteam"])
        # each carries a distinct hidden marker for dedup
        self.assertEqual(cls[0]["marker"], "<!-- nightly-audit:schema-drift -->")

    def test_other_and_toolchain_collapse_to_one_label(self) -> None:
        cls = sfi.finding_classes(_summary(other_findings=True, toolchain_fail=True))
        self.assertEqual([c["label"] for c in cls], ["audit-finding"])

    def test_a_process_gate_finding_routes_somewhere(self) -> None:
        # A run whose only red gate is the boot probe or the rebinding probe still
        # has to produce an issue. Before these classes existed it classified as
        # `findings` and then opened nothing at all.
        self.assertEqual(
            [
                c["label"]
                for c in sfi.finding_classes(_summary(transport_boot_fail=True))
            ],
            ["audit-finding"],
        )
        self.assertEqual(
            [
                c["label"]
                for c in sfi.finding_classes(_summary(host_allowlist_fail=True))
            ],
            ["dns-rebinding"],
        )

    def test_an_unconfigured_control_is_not_an_issue(self) -> None:
        # Fail-open is a deployment state, not a defect: it belongs in the report,
        # not in a tracking issue that would never close.
        self.assertEqual(
            sfi.finding_classes(
                _summary(outcome="green", host_allowlist_unconfigured=True)
            ),
            [],
        )

    def test_every_routed_label_has_a_colour(self) -> None:
        for _, label, _ in sfi._CLASSES:
            self.assertIn(label, sfi._LABEL_COLORS)


class LabelStateTest(unittest.TestCase):
    """The three states, per label. Closing is what makes these load-bearing."""

    def test_a_green_run_clears_every_label(self) -> None:
        for entry in sfi.label_states(_summary(outcome="green")):
            self.assertEqual(entry["state"], sfi.CLEAR, entry["label"])

    def test_a_hard_fail_leaves_every_label_unknown(self) -> None:
        # The audit did not complete. Nothing it failed to say is an all-clear,
        # and a close here would assert a fix that nothing verified.
        for entry in sfi.label_states(_summary(outcome="hard-fail")):
            self.assertEqual(entry["state"], sfi.UNKNOWN, entry["label"])

    def test_an_unrecognised_outcome_leaves_every_label_unknown(self) -> None:
        for entry in sfi.label_states(_summary(outcome="probably-fine")):
            self.assertEqual(entry["state"], sfi.UNKNOWN, entry["label"])

    def test_a_missing_outcome_leaves_every_label_unknown(self) -> None:
        for entry in sfi.label_states({}):
            self.assertEqual(entry["state"], sfi.UNKNOWN, entry["label"])

    def test_a_findings_run_clears_the_labels_that_are_not_findings(self) -> None:
        # The case that makes closing worth having: the drift was fixed while the
        # red-team hit stands. One issue closes, the other keeps its comment.
        summary = _summary(redteam=True)
        self.assertEqual(_state(summary, "redteam"), sfi.FINDING)
        self.assertEqual(_state(summary, "schema-drift"), sfi.CLEAR)

    def test_a_determ_only_run_cannot_clear_the_redteam_label(self) -> None:
        # THE test for this label. The graded layer never ran, so the summary
        # holds no red-team evidence at all; nightly_audit_report.py already
        # refuses to read that as "red-team clear", and a CLOSED issue is that
        # same claim in a louder form.
        summary = _summary(outcome="green", graded_layer_ran=False)
        self.assertEqual(_state(summary, "redteam"), sfi.UNKNOWN)
        # and it does not contaminate the labels that were measured
        self.assertEqual(_state(summary, "schema-drift"), sfi.CLEAR)

    def test_a_summary_too_old_to_carry_the_flag_cannot_clear_redteam(self) -> None:
        summary = _summary(outcome="green")
        del summary["graded_layer_ran"]
        self.assertEqual(_state(summary, "redteam"), sfi.UNKNOWN)

    def test_an_unconfigured_allow_list_cannot_clear_the_rebinding_label(self) -> None:
        # Fail-open is a deployment state, not a measurement of the control.
        summary = _summary(outcome="green", host_allowlist_unconfigured=True)
        self.assertEqual(_state(summary, "dns-rebinding"), sfi.UNKNOWN)

    def test_an_unmeasured_boot_gate_cannot_clear_the_shared_label(self) -> None:
        summary = _summary(outcome="green", transport_boot_unmeasured=True)
        self.assertEqual(_state(summary, "audit-finding"), sfi.UNKNOWN)

    def test_an_unmeasured_lockfile_gate_cannot_clear_the_shared_label(self) -> None:
        summary = _summary(outcome="green", lockfile_unmeasured=True)
        self.assertEqual(_state(summary, "audit-finding"), sfi.UNKNOWN)

    def test_an_unmeasured_gate_does_not_suppress_its_own_finding(self) -> None:
        # Not-measured blocks the CLOSE, never the OPEN. A red gate beside an
        # unmeasured sibling is still a red gate.
        summary = _summary(other_findings=True, transport_boot_unmeasured=True)
        self.assertEqual(_state(summary, "audit-finding"), sfi.FINDING)

    def test_one_finding_makes_the_whole_shared_label_a_finding(self) -> None:
        # `audit-finding` covers five classes. If the first is clean and a later
        # one is red, the label must not be reported clear on the strength of the
        # first entry alone — that would close an issue mid-finding.
        summary = _summary(lockfile_fail=True)
        self.assertEqual(_state(summary, "audit-finding"), sfi.FINDING)
        labels = [c["label"] for c in sfi.label_states(summary)]
        self.assertEqual(len(labels), len(set(labels)), "one entry per label")


class ClassTableTest(unittest.TestCase):
    """Every summary boolean that turns a run red must route somewhere."""

    def test_every_finding_key_in_the_summary_is_routed(self) -> None:
        # The defect this catches has now happened three times: a gate is added
        # to nightly_audit_report.py's `green = not (...)`, moves the outcome to
        # `findings`, and has no entry here — so the run classifies as a finding
        # and then opens nothing at all. `transport_boot_fail` and
        # `host_allowlist_fail` were the first two, fixed by hand;
        # `shipped_artifact_fail` and `lockfile_fail` slipped in afterwards the
        # same way. Reading the source is the only way this test can be the thing
        # that fails instead of a silent nightly.
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "nightly_audit_report.py"
        ).read_text(encoding="utf-8")
        match = re.search(r"\n    green = not \(\n(.*?)\n    \)", source, re.S)
        self.assertIsNotNone(match, "could not locate the `green = not (...)` block")
        keys = set(re.findall(r"\b([a-z_]+)\b", match.group(1))) - {"or", "not"}
        # `hard_fail` is the outcome above `findings`, routed by the flow rather
        # than by a label — it is announced, never ticketed.
        keys.discard("hard_fail")
        routed = {key for key, _label, _title in sfi._CLASSES}
        self.assertEqual(
            keys - routed,
            set(),
            "summary booleans that make a run red and route to no issue",
        )

    def test_every_routed_key_still_exists_in_the_summary(self) -> None:
        # The other direction: an entry here for a boolean nobody writes any more
        # is dead routing that reads as coverage.
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "nightly_audit_report.py"
        ).read_text(encoding="utf-8")
        for key, _label, _title in sfi._CLASSES:
            self.assertIn(f'"{key}":', source, f"{key} is routed but never written")

    def test_every_evidence_key_belongs_to_a_routed_label(self) -> None:
        labels = {label for _key, label, _title in sfi._CLASSES}
        self.assertEqual(set(sfi._EVIDENCE) - labels, set())


class PlanTest(unittest.TestCase):
    MARKER = "<!-- nightly-audit:redteam -->"

    def _open(self) -> list[dict]:
        return [{"number": 7, "body": f"x\n{self.MARKER}\ny"}]

    def test_finding_creates_when_nothing_is_open(self) -> None:
        self.assertEqual(sfi.plan(sfi.FINDING, [], self.MARKER), ("create", None))

    def test_finding_comments_on_the_tracked_issue(self) -> None:
        self.assertEqual(
            sfi.plan(sfi.FINDING, self._open(), self.MARKER), ("comment", 7)
        )

    def test_clear_closes_the_tracked_issue(self) -> None:
        self.assertEqual(sfi.plan(sfi.CLEAR, self._open(), self.MARKER), ("close", 7))

    def test_clear_with_nothing_open_does_nothing(self) -> None:
        self.assertEqual(sfi.plan(sfi.CLEAR, [], self.MARKER), ("noop", None))

    def test_unknown_never_closes(self) -> None:
        self.assertEqual(
            sfi.plan(sfi.UNKNOWN, self._open(), self.MARKER), ("noop", None)
        )

    def test_unknown_never_creates(self) -> None:
        self.assertEqual(sfi.plan(sfi.UNKNOWN, [], self.MARKER), ("noop", None))

    def test_an_issue_a_human_filed_is_not_closed(self) -> None:
        foreign = [{"number": 3, "body": "same label, opened by a person"}]
        self.assertEqual(sfi.plan(sfi.CLEAR, foreign, self.MARKER), ("noop", None))

    def test_an_unrecognised_state_raises(self) -> None:
        with self.assertRaises(ValueError):
            sfi.plan("probably-fine", [], self.MARKER)


class DecideTest(unittest.TestCase):
    def test_create_when_no_open_issue_matches(self) -> None:
        action, number = sfi.decide([], "<!-- nightly-audit:redteam -->")
        self.assertEqual(action, "create")
        self.assertIsNone(number)

    def test_comment_when_marker_present(self) -> None:
        issues = [{"number": 7, "body": "prefix\n<!-- nightly-audit:redteam -->\nx"}]
        action, number = sfi.decide(issues, "<!-- nightly-audit:redteam -->")
        self.assertEqual((action, number), ("comment", 7))

    def test_ignores_open_issue_without_marker(self) -> None:
        issues = [{"number": 9, "body": "unrelated open issue"}]
        action, _number = sfi.decide(issues, "<!-- nightly-audit:redteam -->")
        self.assertEqual(action, "create")


class SyncTest(unittest.TestCase):
    """`sync()`'s REST calls, with `_req` swapped for a recorder. No network."""

    def setUp(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._real_req = sfi._req
        self._real_open = sfi._open_issues
        self.open_issues: list[dict] = []

        def fake_req(
            method: str, url: str, token: str, body: dict | None = None
        ) -> dict:
            self.calls.append((method, url, body))
            return {"number": 99}

        def fake_open(repo: str, label: str, token: str) -> list[dict]:
            self.calls.append(("GET", f"issues?labels={label}", None))
            return self.open_issues

        sfi._req = fake_req  # type: ignore[assignment]
        sfi._open_issues = fake_open  # type: ignore[assignment]

    def tearDown(self) -> None:
        sfi._req = self._real_req  # type: ignore[assignment]
        sfi._open_issues = self._real_open  # type: ignore[assignment]

    def _cls(self, state: str, label: str = "redteam") -> dict[str, str]:
        return {
            "key": "redteam",
            "label": label,
            "marker": sfi._marker(label),
            "title": "[nightly] Red-team hit",
            "state": state,
        }

    def _tracked(self, label: str = "redteam") -> list[dict]:
        return [{"number": 7, "body": f"x\n{sfi._marker(label)}\ny"}]

    def test_unknown_makes_no_call_at_all(self) -> None:
        # Not even the read-only ones. `_ensure_label` CREATES the label when it
        # is missing, so a label that was never measured would otherwise leave a
        # visible trace in the target repo on the strength of no measurement.
        out = sfi.sync("o/r", self._cls(sfi.UNKNOWN), "body", "tok", dry_run=False)
        self.assertEqual(self.calls, [])
        self.assertIn("not measured", out)

    def test_clear_comments_then_closes(self) -> None:
        self.open_issues = self._tracked()
        out = sfi.sync("o/r", self._cls(sfi.CLEAR), "body", "tok", dry_run=False)
        methods = [c[0] for c in self.calls]
        # ensure_label (GET), list (GET), comment (POST), close (PATCH)
        self.assertEqual(methods[-2:], ["POST", "PATCH"])
        self.assertEqual(
            self.calls[-1][2], {"state": "closed", "state_reason": "completed"}
        )
        self.assertIn("closed #7", out)

    def test_clear_explains_itself_before_closing(self) -> None:
        self.open_issues = self._tracked()
        sfi.sync("o/r", self._cls(sfi.CLEAR), "body", "tok", dry_run=False)
        comment = self.calls[-2][2]["body"]
        self.assertIn("closing", comment)

    def test_clear_with_nothing_open_writes_nothing(self) -> None:
        self.open_issues = []
        out = sfi.sync("o/r", self._cls(sfi.CLEAR), "body", "tok", dry_run=False)
        self.assertEqual([c[0] for c in self.calls if c[0] in ("POST", "PATCH")], [])
        self.assertIn("nothing open", out)

    def test_finding_opens_with_the_marker_in_the_body(self) -> None:
        self.open_issues = []
        sfi.sync("o/r", self._cls(sfi.FINDING), "body", "tok", dry_run=False)
        created = self.calls[-1][2]
        # Without the marker the next nightly cannot find this issue and opens a
        # second one, every night.
        self.assertIn(sfi._marker("redteam"), created["body"])

    def test_finding_comments_on_the_tracked_issue(self) -> None:
        self.open_issues = self._tracked()
        out = sfi.sync("o/r", self._cls(sfi.FINDING), "body", "tok", dry_run=False)
        self.assertIn("commented on #7", out)
        self.assertEqual(self.calls[-1][0], "POST")


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, summary: dict, report: str = "# report") -> tuple[int, str]:
        sp = self.dir / "s.json"
        rp = self.dir / "r.md"
        sp.write_text(json.dumps(summary), encoding="utf-8")
        rp.write_text(report, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = sfi.main(
                [
                    "--summary",
                    str(sp),
                    "--report",
                    str(rp),
                    # A variable guaranteed to be unset, so the run is decided by
                    # the arguments rather than by whatever token the ambient
                    # environment carries. Without it this suite passes on a
                    # laptop and reaches for api.github.com inside CI.
                    "--token-env",
                    "SFI_TEST_TOKEN_DELIBERATELY_UNSET",
                    "--dry-run",
                ]
            )
        return rc, buf.getvalue()

    def test_a_green_run_goes_looking_for_issues_to_close(self) -> None:
        # It used to print "nothing to do" and return. That was the whole bug:
        # the only run that can ever close an issue was the one that stopped
        # before it looked.
        rc, out = self._run(_summary(outcome="green"))
        self.assertEqual(rc, 0)
        self.assertIn("closing what the run cleared", out)
        self.assertNotIn("nothing was measured", out)

    def test_a_hard_fail_touches_nothing(self) -> None:
        rc, out = self._run(_summary(outcome="hard-fail", schema_drift=True))
        self.assertEqual(rc, 0)
        self.assertIn("nothing was measured", out)
        self.assertIn("none closed", out)

    def test_a_hard_fail_says_so_rather_than_going_quiet(self) -> None:
        # A silent no-op is indistinguishable from a pass in a cron log.
        _rc, out = self._run(_summary(outcome="hard-fail"))
        self.assertIn("outcome='hard-fail'", out)

    def test_an_unreadable_summary_closes_nothing(self) -> None:
        sp = self.dir / "s.json"
        rp = self.dir / "r.md"
        sp.write_text("{ truncated", encoding="utf-8")
        rp.write_text("# report", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = sfi.main(["--summary", str(sp), "--report", str(rp), "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("cannot read summary", buf.getvalue())

    def test_a_summary_that_is_not_an_object_closes_nothing(self) -> None:
        rc, out = self._run([])  # type: ignore[arg-type]
        self.assertEqual(rc, 1)
        self.assertIn("not an object", out)

    def test_the_per_label_states_are_printed(self) -> None:
        _rc, out = self._run(_summary(outcome="green", graded_layer_ran=False))
        self.assertIn("redteam: unknown", out)
        self.assertIn("schema-drift: clear", out)

    def test_invalid_target_fails(self) -> None:
        rc, out = self._run(_summary(schema_drift=True, target="invalid"))
        self.assertEqual(rc, 1)
        self.assertIn("no valid target", out)

    def test_dry_run_plans_without_network(self) -> None:
        rc, out = self._run(_summary(schema_drift=True, redteam=True))
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run] create issue for label 'schema-drift'", out)
        self.assertIn("[dry-run] create issue for label 'redteam'", out)


if __name__ == "__main__":
    unittest.main()
