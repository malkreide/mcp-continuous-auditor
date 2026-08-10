#!/usr/bin/env python3
"""Tests for scripts/pr_health.py — open pull requests that are not green.

Covers the pure classification (which states mean "CI cannot run"), manifest
reading including the two ways it can produce a false green, and skip parsing.
No GitHub calls are made. Stdlib-only, matching the rest of the suite.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pr_health as ph  # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="pr-health-tests-")
_SEQ = itertools.count()


def _pr(**over: object) -> dict:
    base = {"number": 1, "title": "t", "mergeable_state": "clean", "draft": False}
    base.update(over)
    return base


def _manifest(**over: object) -> Path:
    base = {
        "repositories": [
            {
                "id": "a-mcp",
                "repository": "https://github.com/o/a-mcp",
                "archived": False,
            },
            {
                "id": "b-mcp",
                "repository": "https://github.com/o/b-mcp",
                "archived": False,
            },
        ]
    }
    base.update(over)
    p = Path(_TMPDIR) / f"manifest-{next(_SEQ)}.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


class ClassifyTest(unittest.TestCase):
    def test_clean_with_checks_is_no_finding(self) -> None:
        self.assertIsNone(ph.classify(_pr(), runs=3, age_minutes=60, grace_minutes=10))

    def test_dirty_is_unbuildable(self) -> None:
        """The incident: GitHub cannot build a merge ref, so no workflow starts."""
        self.assertEqual(
            ph.classify(_pr(mergeable_state="dirty"), 0, 60, 10), "unbuildable"
        )

    def test_unstable_is_buildable(self) -> None:
        """`unstable` means a check failed — that is red, and red is visible.

        This probe is for the pull requests that are NOT visibly red.
        """
        self.assertIsNone(ph.classify(_pr(mergeable_state="unstable"), 2, 60, 10))

    def test_blocked_and_behind_are_buildable(self) -> None:
        for state in ("blocked", "behind"):
            with self.subTest(state=state):
                self.assertIsNone(ph.classify(_pr(mergeable_state=state), 1, 60, 10))

    def test_unknown_is_not_reported(self) -> None:
        """`mergeable_state` is computed lazily; the first read is often unknown.

        Reporting it would make a finding out of every freshly pushed branch.
        """
        self.assertIsNone(ph.classify(_pr(mergeable_state="unknown"), 1, 60, 10))

    def test_future_state_counts_as_unbuildable(self) -> None:
        """An allow-list, not a deny-list: a state GitHub adds later is surfaced.

        The opposite default would let a new unbuildable state pass silently,
        which is exactly the failure mode this probe exists for.
        """
        self.assertEqual(
            ph.classify(_pr(mergeable_state="has_hooks_gone_wrong"), 5, 60, 10),
            "unbuildable",
        )

    def test_zero_checks_past_the_grace_period(self) -> None:
        self.assertEqual(
            ph.classify(_pr(), runs=0, age_minutes=11, grace_minutes=10), "no_checks"
        )

    def test_zero_checks_within_the_grace_period_is_silent(self) -> None:
        """Below the grace period, "no checks" and "about to start" look alike."""
        self.assertIsNone(ph.classify(_pr(), runs=0, age_minutes=2, grace_minutes=10))

    def test_unbuildable_wins_over_no_checks(self) -> None:
        """Both are true for a dirty PR; the cause is the one worth reporting."""
        self.assertEqual(
            ph.classify(_pr(mergeable_state="dirty"), 0, 999, 10), "unbuildable"
        )

    def test_draft_is_buildable(self) -> None:
        """A draft pull request runs its workflows — it is not unbuildable.

        Measured: swiss-public-data-mcp#31 and #32 both ran full CI as drafts.
        Getting this wrong would turn every draft into a finding, and a probe
        that fires on everything is one nobody reads.
        """
        self.assertIsNone(
            ph.classify(_pr(mergeable_state="draft", draft=True), 1, 60, 10)
        )


class WiringTest(unittest.TestCase):
    """inspect() end to end with the REST layer stubbed.

    The classification is pure and tested above; what is untested without this
    is the wiring — that the state comes from the single-PR endpoint (the list
    endpoint omits `mergeable_state`, verified against a live pull request) and
    that the head SHA reaches the workflow-run and commit calls.
    """

    def _stub(self, ph_module: object, routes: dict[str, object]) -> None:
        def fake_get(url: str, token: str) -> object:
            for frag, payload in routes.items():
                if frag in url:
                    return payload
            raise AssertionError(f"unerwarteter Aufruf: {url}")

        self._orig = ph_module._get  # type: ignore[attr-defined]
        ph_module._get = fake_get  # type: ignore[attr-defined]
        self.addCleanup(lambda: setattr(ph_module, "_get", self._orig))

    def test_dirty_pr_becomes_a_finding_with_its_observation(self) -> None:
        self._stub(
            ph,
            {
                "/pulls?state=open": [{"number": 58}],
                "/pulls/58": {
                    "number": 58,
                    "title": "Startereignis",
                    "mergeable_state": "dirty",
                    "draft": True,
                    "head": {"sha": "3b342b7d971dfe2fd3c762e403b5579b0d42d99b"},
                },
                "/actions/runs": {"total_count": 0},
                "/commits/3b342b7": {
                    "commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}
                },
            },
        )
        now = dt.datetime(2020, 1, 2, tzinfo=dt.UTC)
        found = ph.inspect("o/r", "tok", grace=10, now=now)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].status, "unbuildable")
        self.assertEqual(found[0].evidence["mergeable_state"], "dirty")
        self.assertEqual(found[0].evidence["workflow_runs"], 0)

    def test_clean_pr_with_checks_yields_nothing(self) -> None:
        self._stub(
            ph,
            {
                "/pulls?state=open": [{"number": 1}],
                "/pulls/1": {
                    "number": 1,
                    "title": "ok",
                    "mergeable_state": "clean",
                    "head": {"sha": "abc1234"},
                },
                "/actions/runs": {"total_count": 3},
                "/commits/abc1234": {
                    "commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}
                },
            },
        )
        now = dt.datetime(2020, 1, 2, tzinfo=dt.UTC)
        self.assertEqual(ph.inspect("o/r", "tok", grace=10, now=now), [])

    def test_a_missing_commit_date_does_not_suppress_the_finding(self) -> None:
        """No timestamp must not be read as "brand new" — that would hide it."""
        self._stub(
            ph,
            {
                "/pulls?state=open": [{"number": 7}],
                "/pulls/7": {
                    "number": 7,
                    "title": "x",
                    "mergeable_state": "clean",
                    "head": {"sha": "def5678"},
                },
                "/actions/runs": {"total_count": 0},
                "/commits/def5678": {},
            },
        )
        now = dt.datetime(2020, 1, 2, tzinfo=dt.UTC)
        found = ph.inspect("o/r", "tok", grace=10, now=now)
        self.assertEqual([f.status for f in found], ["no_checks"])

    def test_the_run_count_comes_from_actions_not_from_check_runs(self) -> None:
        """`/commits/{sha}/check-runs` must not come back.

        The Checks API cannot be reached by ANY fine-grained token: there is no
        repository permission to grant, so it answers 403 however the token is
        configured (community/discussions#129512). A sweep asking it would report
        all 47 repositories as unreached — which is at least loud, but the probe
        would never run.

        This pins the endpoint rather than the count, because the count is the
        part that looks the same either way. The head SHA has to reach the query
        as `head_sha=`: without it the endpoint answers with every run the
        repository ever had, and `no_checks` would then never fire again.
        """
        seen: list[str] = []

        def recording_get(url: str, token: str) -> object:
            seen.append(url)
            if "/pulls?state=open" in url:
                return [{"number": 4}]
            if "/pulls/4" in url:
                return {
                    "number": 4,
                    "title": "t",
                    "mergeable_state": "clean",
                    "head": {"sha": "cafe123"},
                }
            if "/actions/runs" in url:
                return {"total_count": 0}
            return {"commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}}

        orig = ph._get
        ph._get = recording_get  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(ph, "_get", orig))

        now = dt.datetime(2020, 1, 2, tzinfo=dt.UTC)
        found = ph.inspect("o/r", "tok", grace=10, now=now)

        self.assertEqual([f.status for f in found], ["no_checks"])
        self.assertEqual(found[0].evidence["workflow_runs"], 0)
        self.assertFalse(
            [u for u in seen if "check-runs" in u],
            "check-runs ist fuer fein granulierte Tokens 403 — kein Aufruf dorthin",
        )
        self.assertTrue(
            [u for u in seen if "/actions/runs" in u and "head_sha=cafe123" in u],
            f"kein /actions/runs mit head_sha in {seen}",
        )


class ManifestTest(unittest.TestCase):
    def test_targets_and_total(self) -> None:
        total, targets, skipped = ph.read_manifest(_manifest())
        self.assertEqual(total, 2)
        self.assertEqual([slug for slug, _ in targets], ["o/a-mcp", "o/b-mcp"])
        self.assertEqual(skipped, [])

    def test_archived_is_a_named_skip_not_an_omission(self) -> None:
        m = _manifest(
            repositories=[
                {"id": "a", "repository": "https://github.com/o/a", "archived": False},
                {"id": "z", "repository": "https://github.com/o/z", "archived": True},
            ]
        )
        total, targets, skipped = ph.read_manifest(m)
        self.assertEqual(total, 2)
        self.assertEqual([s for s, _ in targets], ["o/a"])
        self.assertEqual(skipped[0][0], "o/z")
        self.assertTrue(skipped[0][1], "ein Skip ohne Grund ist eine Luecke mit Alibi")

    def test_total_counts_skipped_entries_too(self) -> None:
        """Regression: a denominator that counts only the swept entries.

        Shipped once already — an all-green run exited 1 because
        `2 swept + 1 skipped` was compared against an expected 2.
        """
        m = _manifest(
            repositories=[
                {"id": "a", "repository": "https://github.com/o/a", "archived": True},
                {"id": "b", "repository": "https://github.com/o/b", "archived": True},
            ]
        )
        total, targets, skipped = ph.read_manifest(m)
        self.assertEqual((total, len(targets), len(skipped)), (2, 0, 2))

    def test_missing_repositories_key_is_refused(self) -> None:
        """Absent is not empty: a renamed field would sweep nothing and exit 0."""
        m = _manifest()
        m.write_text(json.dumps({"servers": []}), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            ph.read_manifest(m)
        self.assertIn("repositories", str(cm.exception))

    def test_empty_repository_list_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            ph.read_manifest(_manifest(repositories=[]))

    def test_non_slug_repository_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            ph.read_manifest(
                _manifest(
                    repositories=[{"id": "a", "repository": "git@github.com:o/a.git"}]
                )
            )


class ExitCodeTest(unittest.TestCase):
    """The three outcomes must stay apart: nothing found / found / did not look."""

    def _run(
        self, routes: dict[str, object] | None, repos: list[dict], *extra: str
    ) -> tuple[int, str]:
        orig = ph._get
        if routes is None:

            def fake(url: str, token: str) -> object:
                raise OSError("Netz weg")
        else:

            def fake(url: str, token: str) -> object:
                for frag, payload in routes.items():
                    if frag in url:
                        return payload
                raise AssertionError(url)

        ph._get = fake  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(ph, "_get", orig))
        os.environ["GITHUB_TOKEN"] = "tok"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = ph.main(["--manifest", str(_manifest(repositories=repos)), *extra])
        except SystemExit as e:  # argparse / manifest refusals
            rc = int(e.code or 0)
        return rc, buf.getvalue()

    _REPO: ClassVar[list[dict]] = [
        {"id": "a", "repository": "https://github.com/o/a", "archived": False}
    ]
    _CLEAN: ClassVar[dict[str, object]] = {
        "/pulls?state=open": [{"number": 1}],
        "/pulls/1": {
            "number": 1,
            "title": "t",
            "mergeable_state": "clean",
            "head": {"sha": "abc"},
        },
        "/actions/runs": {"total_count": 2},
        "/commits/abc": {"commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}},
    }

    def test_clean_sweep_is_zero(self) -> None:
        rc, out = self._run(self._CLEAN, self._REPO)
        self.assertEqual(rc, 0)
        self.assertIn("1/1 Repos geprueft", out)

    def test_findings_are_two_not_one(self) -> None:
        """A finding and an incomplete sweep are different outcomes."""
        routes = dict(self._CLEAN)
        routes["/pulls/1"] = {
            "number": 1,
            "title": "t",
            "mergeable_state": "dirty",
            "head": {"sha": "abc"},
        }
        rc, _ = self._run(routes, self._REPO)
        self.assertEqual(rc, 2)

    def test_unreachable_repo_is_one_and_not_double_counted(self) -> None:
        """Regression: a repo that errored sat in BOTH `swept` and `errors`.

        Adding the two reported "2 von 1" — more coverage than there were
        targets. Found by measuring the exit path, not by reading it.
        """
        rc, out = self._run(None, self._REPO)
        self.assertEqual(rc, 1)
        self.assertIn("Deckung unvollstaendig", out)
        self.assertIn("0/1 abgedeckt", out)
        self.assertNotIn("2/1", out)


class AllowSkipTest(unittest.TestCase):
    def test_reason_is_mandatory(self) -> None:
        with self.assertRaises(SystemExit):
            ph.parse_allow_skip(["o/a"])

    def test_blank_reason_is_mandatory_too(self) -> None:
        with self.assertRaises(SystemExit):
            ph.parse_allow_skip(["o/a:   "])

    def test_reason_is_kept(self) -> None:
        self.assertEqual(ph.parse_allow_skip(["o/a:Umbau"]), {"o/a": "Umbau"})


class FindingTest(unittest.TestCase):
    def test_line_carries_the_observation(self) -> None:
        """A finding you have to verify by hand is a finding nobody reads."""
        f = ph.Finding(
            repo="o/a",
            number=58,
            status="unbuildable",
            title="Startereignis",
            evidence={"mergeable_state": "dirty", "workflow_runs": 0},
        )
        line = f.line()
        self.assertIn("o/a#58", line)
        self.assertIn("dirty", line)
        self.assertIn("workflow_runs=0", line)


if __name__ == "__main__":
    unittest.main()
