#!/usr/bin/env python3
"""Tests for scripts/lockfile_probe.py — is the declared bound in force?

The probe is two file reads and one subprocess, so these tests write real
``pyproject.toml`` / ``uv.lock`` / ``poetry.lock`` files into a temp dir and
run the whole comparison over them. The subprocess seam (``uv lock --check``,
``poetry check --lock``) is the only thing injected; everything that decides a
finding is exercised for real.

The central scenario is the incident the probe was written for: the upper
bounds were merged into ``pyproject.toml`` and ``uv.lock`` was not regenerated,
so ``main`` carried the fix in the file everybody reads and the old range in
the file that installs.

Stdlib-only and offline.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lockfile_probe as lp  # noqa: E402

PYPROJECT_CAPPED = """\
[project]
name = "zurich-opendata-mcp"
version = "0.6.0"
dependencies = ["mcp[cli]>=2.0.0,<3", "httpx>=0.27"]
"""

# The lock as it stood on `main` after the bounds PR merged: resolved BEFORE the
# cap existed, so its recorded requires-dist still carries the open range and
# its pin still sits in the 1.x series.
UV_LOCK_STALE = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "zurich-opendata-mcp"
version = "0.6.0"
source = { editable = "." }
dependencies = [{ name = "mcp" }, { name = "httpx" }]

[package.metadata]
requires-dist = [
    { name = "mcp", extras = ["cli"], specifier = ">=1.28.1" },
    { name = "httpx", specifier = ">=0.27" },
]

[[package]]
name = "mcp"
version = "1.29.0"

[[package]]
name = "httpx"
version = "0.28.1"
"""

UV_LOCK_FRESH = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "zurich-opendata-mcp"
version = "0.6.0"
source = { editable = "." }
dependencies = [{ name = "mcp" }, { name = "httpx" }]

[package.metadata]
requires-dist = [
    { name = "mcp", extras = ["cli"], specifier = ">=2.0.0,<3" },
    { name = "httpx", specifier = ">=0.27" },
]

[[package]]
name = "mcp"
version = "2.1.0"

[[package]]
name = "httpx"
version = "0.28.1"
"""


def write(root: Path, **files: str) -> None:
    for name, text in files.items():
        (root / name.replace("__", ".")).write_text(text, encoding="utf-8")


class Case(unittest.TestCase):
    """A target checkout in a temp dir, with the tool seam stubbed out."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # `--no-tools` in the default runs: `uv` may well be installed on the
        # machine running these tests, and a probe that shells out to it would
        # make the suite depend on a resolver's network access.
        self.kwargs = {"use_tools": False}

    def run_probe(self, **kwargs) -> lp.Report:
        return lp.run(self.root, **{**self.kwargs, **kwargs})

    def codes(self, report: lp.Report) -> list[str]:
        return [f.code for f in report.findings]

    def find(self, report: lp.Report, code: str) -> lp.Finding:
        return next(f for f in report.findings if f.code == code)


class DriftTest(Case):
    def test_the_incident_cap_in_pyproject_missing_from_the_lock(self) -> None:
        write(self.root, pyproject__toml=PYPROJECT_CAPPED, uv__lock=UV_LOCK_STALE)
        report = self.run_probe()

        self.assertIn("LOCK_DRIFT", self.codes(report))
        drift = self.find(report, "LOCK_DRIFT")
        self.assertEqual(drift.dependency, "mcp")
        # Both specifiers in the finding: "the lock is out of date" is a
        # sentence somebody has to act on, and the diverging pair IS the action.
        self.assertIn(">=2.0.0,<3", drift.detail)
        self.assertIn(">=1.28.1", drift.detail)
        # And it names WHY this particular divergence matters.
        self.assertIn("not in force where the install happens", drift.detail)
        self.assertEqual(report.exit_code(), lp.EXIT_FINDINGS)

    def test_the_stale_pin_is_reported_as_well_as_the_stale_range(self) -> None:
        """LOCK_UNSATISFIED is the stronger of the two and stands on its own.

        The range comparison is about metadata hygiene; this one says the
        version that actually gets installed violates what the project declares.
        """
        write(self.root, pyproject__toml=PYPROJECT_CAPPED, uv__lock=UV_LOCK_STALE)
        unsatisfied = self.find(self.run_probe(), "LOCK_UNSATISFIED")
        self.assertEqual(unsatisfied.dependency, "mcp")
        self.assertIn("1.29.0", unsatisfied.detail)

    def test_a_regenerated_lock_is_clean(self) -> None:
        write(self.root, pyproject__toml=PYPROJECT_CAPPED, uv__lock=UV_LOCK_FRESH)
        report = self.run_probe()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), lp.EXIT_GREEN)

    def test_clause_order_and_trailing_zeros_are_not_drift(self) -> None:
        """`>=2.0.0,<3` and `<3.0,>=2.0` are one requirement.

        A probe that reported those as a finding would be switched off within a
        week, and the real drift underneath would go with it.
        """
        write(self.root,
              pyproject__toml=PYPROJECT_CAPPED,
              uv__lock=UV_LOCK_FRESH.replace('specifier = ">=2.0.0,<3"',
                                             'specifier = "<3.0,>=2.0"'))
        self.assertEqual(self.run_probe().findings, [])

    def test_dropped_extras_are_a_finding_of_their_own(self) -> None:
        """Same range, different extras — a different set of packages installs."""
        write(self.root,
              pyproject__toml=PYPROJECT_CAPPED,
              uv__lock=UV_LOCK_FRESH.replace('extras = ["cli"], ', ""))
        drift = self.find(self.run_probe(), "LOCK_DRIFT")
        self.assertIn("extras", drift.detail)

    def test_a_dependency_absent_from_the_lock(self) -> None:
        write(self.root,
              pyproject__toml=PYPROJECT_CAPPED,
              uv__lock=UV_LOCK_FRESH.replace('{ name = "httpx", specifier = ">=0.27" },', "")
                                    .replace('[[package]]\nname = "httpx"\nversion = "0.28.1"\n',
                                             ""))
        codes = self.codes(self.run_probe())
        self.assertIn("LOCK_MISSING_DEP", codes)
        self.assertIn("LOCK_DRIFT", codes)

    def test_conditional_requirements_are_skipped(self) -> None:
        """A marker-gated dependency is not installed by a plain sync.

        Same rule the yank gate documents: deciding a marker without an
        environment to evaluate it against is a guess wearing a finding's
        clothes.
        """
        write(self.root,
              pyproject__toml=PYPROJECT_CAPPED.replace(
                  'dependencies = ["mcp[cli]>=2.0.0,<3", "httpx>=0.27"]',
                  'dependencies = ["mcp[cli]>=2.0.0,<3", "httpx>=0.27", '
                  '"tomli>=2; python_version < \'3.11\'"]'),
              uv__lock=UV_LOCK_FRESH)
        self.assertEqual(self.run_probe().findings, [])


class NotMeasuredTest(Case):
    def test_no_lockfile_is_not_a_finding_and_not_a_pass(self) -> None:
        """Exit 3. A library that ships no lock has made a defensible choice.

        Turning that into a red gate teaches people to commit a lock they never
        sync from, which is strictly worse than having none.
        """
        write(self.root, pyproject__toml=PYPROJECT_CAPPED)
        report = self.run_probe()
        self.assertEqual(report.status, "no_lockfile")
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), lp.EXIT_NOT_MEASURED)
        self.assertIn("nothing was measured", " ".join(report.notes))

    def test_no_pyproject_is_a_harness_failure(self) -> None:
        report = self.run_probe()
        self.assertEqual(report.exit_code(), lp.EXIT_CANNOT_RUN)
        self.assertIn("no pyproject.toml", report.harness_error)

    def test_an_unparseable_lock_is_never_reported_clean(self) -> None:
        write(self.root, pyproject__toml=PYPROJECT_CAPPED,
              uv__lock="this is not toml [[[\n")
        report = self.run_probe()
        self.assertEqual(report.exit_code(), lp.EXIT_CANNOT_RUN)
        self.assertIn("could not be read", report.harness_error)


class PoetryTest(Case):
    POETRY_LOCK = """\
[[package]]
name = "mcp"
version = "1.29.0"
description = ""
optional = false

[[package]]
name = "httpx"
version = "0.28.1"
description = ""
optional = false

[metadata]
content-hash = "deadbeef"
"""

    def test_poetry_pins_are_checked_against_pyproject(self) -> None:
        write(self.root, pyproject__toml=PYPROJECT_CAPPED,
              poetry__lock=self.POETRY_LOCK)
        report = self.run_probe()
        self.assertIn("LOCK_UNSATISFIED", self.codes(report))

    def test_poetry_cannot_answer_the_specifier_question_and_says_so(self) -> None:
        """poetry.lock does not echo the project's own requires-dist.

        The gap is named in the report rather than left to make a poetry
        repository read as more thoroughly checked than it was.
        """
        write(self.root, pyproject__toml=PYPROJECT_CAPPED,
              poetry__lock=self.POETRY_LOCK)
        report = self.run_probe()
        self.assertNotIn("LOCK_DRIFT", self.codes(report))
        self.assertIn("does not record the project's own requires-dist",
                      " ".join(report.notes))


class ToolCheckTest(Case):
    """The `uv lock --check` / `poetry check --lock` seam."""

    def test_the_check_flag_is_not_optional(self) -> None:
        """`uv lock` without `--check` REGENERATES the file under audit.

        Pinned as a property rather than trusted to review: the difference
        between this probe and one that quietly fixes its own finding is a
        single flag, and nothing else in the file would fail if it went missing.
        """
        for kind, expected in (("uv", ["uv", "lock", "--check"]),
                               ("poetry", ["poetry", "check", "--lock"])):
            with self.subTest(kind=kind):
                seen: list[list[str]] = []

                def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
                    seen.append(list(argv))
                    raise AssertionError("not reached")

                original_which, original_run = lp.shutil.which, lp.subprocess.run
                lp.shutil.which = lambda _b: "/usr/bin/" + _b  # type: ignore[assignment]
                lp.subprocess.run = fake_run                   # type: ignore[assignment]
                try:
                    lp.run_tool_check(kind, self.root, 5.0)
                except AssertionError:
                    pass
                finally:
                    lp.shutil.which, lp.subprocess.run = original_which, original_run
                self.assertEqual(seen[0], expected)

    def test_a_missing_tool_is_reported_not_counted_as_agreement(self) -> None:
        original_which = lp.shutil.which
        lp.shutil.which = lambda _b: None  # type: ignore[assignment]
        try:
            check = lp.run_tool_check("uv", self.root, 5.0)
        finally:
            lp.shutil.which = original_which
        self.assertEqual(check.status, "unavailable")
        self.assertIn("never a pass", check.detail)

    def test_a_network_failure_is_not_reported_as_a_stale_lock(self) -> None:
        """The tool failing and the tool disagreeing are different facts.

        Filing the first as LOCK_STALE puts an infrastructure failure on the
        repository's account — the distinction the boot gate's 127 exists for.
        """
        self.assertTrue(lp._failed_rather_than_stale(
            "error: Failed to fetch https://pypi.org/simple/mcp/"))
        self.assertFalse(lp._failed_rather_than_stale(
            "error: The lockfile at `uv.lock` needs to be updated, but "
            "`--check` was provided"))


class ReportShapeTest(Case):
    def test_json_report_carries_provenance_and_both_specifiers(self) -> None:
        write(self.root, pyproject__toml=PYPROJECT_CAPPED, uv__lock=UV_LOCK_STALE)
        report = self.run_probe()
        report.provenance = lp.probe_provenance.capture(self.root).recheck()
        data = json.loads(json.dumps(report.as_dict()))
        self.assertEqual(data["probe"], "lockfile")
        self.assertIn("provenance", data)
        self.assertEqual(data["declared"]["mcp"], ">=2.0.0,<3")
        self.assertIn("LOCK_DRIFT", [f["code"] for f in data["findings"]])

    def test_cli_returns_the_report_exit_code(self) -> None:
        write(self.root, pyproject__toml=PYPROJECT_CAPPED, uv__lock=UV_LOCK_STALE)
        rc = lp.main(["--target", str(self.root), "--no-tools", "--format", "json"])
        self.assertEqual(rc, lp.EXIT_FINDINGS)


if __name__ == "__main__":
    unittest.main()
