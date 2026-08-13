#!/usr/bin/env python3
"""The pure half of `scripts/audit_pin_drift.py`.

The network half is not faked, and that is the same decision
`test_quality_chain_table.py` records for its own subject: a stand-in for
GitHub's answer would only restate this repository's assumption about that
answer, and could never contradict it. What can be tested here is everything
the script decides ONCE IT HAS an answer — including the three states it must
keep apart:

    tag exists + latest matches  -> in sync
    tag does not exist           -> BROKEN, and broken NOW
    could not be measured        -> UNKNOWN, and explicitly not a pass

That third one is the whole point. `None` folded into "differs" would make an
unreachable API indistinguishable from a stale pin, and the fix for those two
is not the same.

`read_pin` is exercised against the real file as well as against synthetic
ones: the real one proves the parser matches the thing it parses today, the
synthetic ones prove it fails loudly on the shapes that would otherwise pass
silently.

STDLIB-ONLY, AND THAT IS NOW ENFORCED RATHER THAN INTENDED. The first version
of this file used pytest — `raises`, `parametrize`, `tmp_path` — and claimed
"stdlib-only" in this very docstring while doing so. `tests.yml` runs
`unittest.defaultTestLoader.discover`, so it did not fail on an assertion; it
failed on the IMPORT, and took this whole module out of discovery with it.
`TheSuiteStaysStdlibOnly` at the bottom turns that into a test rather than a
lesson.

No network.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_pin_drift import (  # noqa: E402
    PIN_FILE,
    compare,
    read_pin,
)

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "audit-pin-drift.yml"


class TempTreeTest(unittest.TestCase):
    """A throwaway directory per test, cleaned up by the framework."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def pin_file(self, body: str) -> Path:
        path = self.tmp / "pinfile.py"
        path.write_text(body, encoding="utf-8")
        return path


class ReadPin(TempTreeTest):
    """The anchor: where the pin is read from, and how it fails."""

    def test_the_real_pin_file_is_readable(self):
        """Against the file as it actually is — not a fixture of it.

        A parser proven only against synthetic input is proven against its own
        author's idea of the format.
        """
        pin = read_pin()
        self.assertTrue(pin.startswith("v"), f"does not look like a tag: {pin!r}")

    def test_the_pin_matches_the_one_the_documentation_uses(self):
        """The connection to the other guard, spelled out.

        Without it, `PIN` could be renamed in the test module and this script
        would go on reading some other constant that happens to still be there.
        """
        pin = read_pin()
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            f"mcp-audit-skill/tree/{pin}",
            readme,
            f"README.md carries no link pinned to {pin} — the pin this script "
            "reads and the pin the documentation uses have come apart.",
        )

    def test_a_missing_file_is_an_error(self):
        with self.assertRaisesRegex(LookupError, "missing"):
            read_pin(self.tmp / "nope.py")

    def test_a_missing_constant_is_an_error(self):
        """The anchor case: renamed away, and nothing invents a default."""
        with self.assertRaisesRegex(LookupError, "no module-level"):
            read_pin(self.pin_file('OTHER = "v1.2.3"\n'))

    def test_the_constant_inside_a_docstring_does_not_count(self):
        """Why this parses instead of grepping.

        The real file mentions `PIN` in prose and in an error message. A regex
        would happily read one of those.
        """
        path = self.pin_file(
            '"""A module that talks about PIN = "v9.9.9" without setting it."""\n'
            '# PIN = "v8.8.8"\n'
            'PIN = "v3.0.0"\n'
        )
        self.assertEqual(read_pin(path), "v3.0.0")

    def test_a_computed_pin_is_refused(self):
        path = self.pin_file('MAJOR = 3\nPIN = f"v{MAJOR}.0.0"\n')
        with self.assertRaisesRegex(LookupError, "not a plain string literal"):
            read_pin(path)

    def test_an_annotated_assignment_is_read(self):
        self.assertEqual(read_pin(self.pin_file('PIN: str = "v4.1.0"\n')), "v4.1.0")

    def test_the_default_resolves_at_call_time(self):
        """Written the obvious way, `PIN_FILE` would be a decoy.

        `def read_pin(path=PIN_FILE)` captures the constant when the function
        is DEFINED; rebinding the module attribute then changes nothing, while
        the code reads as though it does. Measured here — which is why the
        signature takes `None`.
        """
        elsewhere = self.pin_file('PIN = "v7.7.7"\n')
        with mock.patch("scripts.audit_pin_drift.PIN_FILE", elsewhere):
            self.assertEqual(read_pin(), "v7.7.7")


class Compare(unittest.TestCase):
    """The three states, and the rank between them."""

    def test_in_sync(self):
        ok, message = compare("v3.0.0", "v3.0.0", True)
        self.assertTrue(ok)
        self.assertIn("In sync", message)

    def test_drift_names_both_versions(self):
        ok, message = compare("v3.0.0", "v3.2.0", True)
        self.assertFalse(ok)
        self.assertIn("v3.0.0", message)
        self.assertIn("v3.2.0", message)
        # The message must say what it is NOT: a blocker.
        self.assertIn("block", message)

    def test_a_pin_that_points_at_nothing_is_broken(self):
        ok, message = compare("v9.9.9", "v3.0.0", False)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("BROKEN:"))

    def test_broken_outranks_drift(self):
        """A tag that does not exist is a fact; being behind is a decision.

        Reported as drift, somebody raises the pin to the latest release and
        the broken link disappears by accident rather than by diagnosis — and
        the same typo in the next pin repeats it.
        """
        _, message = compare("v9.9.9", "v3.0.0", False)
        self.assertNotIn("DRIFT", message)

    def test_unmeasured_is_unknown_and_never_a_pass(self):
        for latest, exists in ((None, True), ("v3.0.0", None), (None, None)):
            with self.subTest(latest=latest, exists=exists):
                ok, message = compare("v3.0.0", latest, exists)
                self.assertFalse(ok)
                self.assertTrue(message.startswith("UNKNOWN:"))
                self.assertIn("NOT a pass", message)

    def test_unknown_names_what_was_not_measured(self):
        _, message = compare("v3.0.0", None, True)
        self.assertIn("the latest release", message)
        _, message = compare("v3.0.0", "v3.0.0", None)
        self.assertIn("whether the pinned tag exists", message)


class TheScript(unittest.TestCase):
    def test_ANKER_an_unreadable_pin_exits_two_without_reaching_the_network(self):
        """Usage error and finding are different exits, and the first wins.

        Run with a pin file that cannot be read, it must fail on THAT — not on
        a network call it should never have reached.
        """
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, pathlib;"
                "sys.path.insert(0, str(pathlib.Path('.').resolve()));"
                "import scripts.audit_pin_drift as m;"
                "m.PIN_FILE = pathlib.Path('does-not-exist.py');"
                "sys.exit(m.main([]))",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("has no home", done.stderr)

    def test_the_pin_file_constant_points_at_the_real_module(self):
        self.assertTrue(PIN_FILE.is_file(), f"{PIN_FILE} is missing")
        self.assertEqual(PIN_FILE.name, "test_quality_chain_table.py")


class TheSummaryStep(unittest.TestCase):
    """The heredoc in `audit-pin-drift.yml`, against a report `main()` made.

    BUILT IN FROM THE START, because the identical construction was measured
    failing next door: `quality-chain.yml` in `mcp-audit-skill` read a report
    schema that had moved on, and its summary step died on a `KeyError` before
    writing a line — every run, for a week, while the guard itself worked fine.

    The Python of a workflow lives in a heredoc that only the runner ever
    executes. So this pulls it out of the YAML and runs it against a report
    `main()` really produced, rather than one hand-built from an assumption
    about the schema.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    @staticmethod
    def script() -> str:
        """The Python out of the heredoc.

        The heredoc word `PY` is the anchor. If it disappears — somebody moves
        the step into a file — that is an ERROR and not a silent skip: this
        test would otherwise have nothing left to check.
        """
        text = WORKFLOW.read_text(encoding="utf-8")
        match = re.search(r"python - <<'PY'[^\n]*\n(.*?)\n[ \t]*PY\n", text, re.S)
        if match is None:
            raise AssertionError(
                f"{WORKFLOW.name}: no `python - <<'PY' … PY` heredoc found — "
                "the anchor is gone, and this test checked nothing."
            )
        return textwrap.dedent(match.group(1))

    @staticmethod
    def report(*, latest, exists) -> str:
        """A real report out of `main()`, not one written by hand here.

        Hand-written it would carry this test's assumption about the schema,
        and the schema is precisely what is in question.
        """
        import scripts.audit_pin_drift as m

        buf = io.StringIO()
        with (
            mock.patch.object(m, "fetch_latest", lambda *a, **k: (latest, "ok")),
            mock.patch.object(m, "fetch_tag_exists", lambda *a, **k: (exists, "ok")),
            contextlib.redirect_stdout(buf),
        ):
            code = m.main(["--format", "json"])
        assert code in (0, 1), f"unexpected exit {code}"
        return buf.getvalue()

    def run_summary(self, raw: str) -> str:
        (self.tmp / "pin.json").write_text(raw, encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-c", self.script()],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            done.returncode,
            0,
            "the summary step died — exactly the failure that ran unnoticed "
            f"for a week in mcp-audit-skill:\n{done.stderr}",
        )
        return done.stdout

    def test_ANKER_the_in_sync_report_renders(self):
        pin = read_pin()
        out = self.run_summary(self.report(latest=pin, exists=True))
        self.assertIn("Audit pin", out)
        self.assertNotIn("action needed", out)
        self.assertIn(pin, out)

    def test_ANKER_the_drift_report_renders(self):
        out = self.run_summary(self.report(latest="v99.0.0", exists=True))
        self.assertIn("action needed", out)
        self.assertIn("v99.0.0", out)
        # The three steps of raising the pin belong in the summary — that is
        # the part somebody acts on.
        self.assertIn("PIN", out)
        self.assertIn("reading what changed", out)

    def test_ANKER_the_unknown_report_renders(self):
        """Not measured must survive rendering too — it is the state that
        hides."""
        out = self.run_summary(self.report(latest=None, exists=None))
        self.assertIn("action needed", out)
        self.assertIn("not measured", out)

    def test_the_advice_follows_the_state(self):
        """The separation the script makes must survive into the summary.

        An earlier version printed the pin-raising steps for EVERY non-ok
        result. So a run that measured nothing, and a run whose tag does not
        exist, both ended with «raise the pin» — advice with no evidence
        behind it in the first case and the wrong diagnosis in the second.
        Collapsing the three states again at the last step is worse than not
        having separated them: this is where somebody reads and acts.
        """
        cases = {
            "drift": ({"latest": "v99.0.0", "exists": True}, True),
            "broken": ({"latest": "v3.0.0", "exists": False}, False),
            "unknown": ({"latest": None, "exists": None}, False),
        }
        for name, (kwargs, expect_steps) in cases.items():
            with self.subTest(state=name):
                out = self.run_summary(self.report(**kwargs))
                steps = "Raising the pin means" in out
                self.assertEqual(
                    steps,
                    expect_steps,
                    f"{name}: pin-raising steps {'missing' if expect_steps else 'shown'}"
                    " — the advice does not follow the state",
                )

    def test_broken_says_what_to_check_instead_of_bumping(self):
        out = self.run_summary(self.report(latest="v3.0.0", exists=False))
        self.assertIn("does not exist upstream", out)
        self.assertIn("Do NOT bump", out)

    def test_unknown_says_the_pin_is_not_implicated(self):
        out = self.run_summary(self.report(latest=None, exists=None))
        self.assertIn("Nothing was compared", out)
        self.assertIn("not implicated", out)

    def test_a_missing_result_does_not_kill_the_summary(self):
        """If the step before died, this one says WHY nothing is there — it
        does not write a second error over the first."""
        self.assertIn("No result", self.run_summary(""))


class TheSuiteStaysStdlibOnly(unittest.TestCase):
    """`tests.yml` runs `unittest.defaultTestLoader.discover`, and pytest is
    not installed there.

    THIS EXISTS BECAUSE IT HAPPENED, in this very file. The failure was not a
    red assertion — it was `ModuleNotFoundError` during DISCOVERY, which
    unittest reports as one broken test module while every other file still
    runs and reports `ok`. Easy to read as an unrelated hiccup, and it would
    come back with the next file somebody writes in the habit of the sibling
    repository, where pytest is the runner.
    """

    def test_no_test_file_imports_pytest(self):
        """PARSED, NOT GREPPED — and that distinction was measured too.

        The first version matched `^import pytest` per line and immediately
        flagged `test_live_schedule_probe.py:49`, where those words sit INSIDE
        a triple-quoted fixture describing another repository's test file. A
        string's content starts at column 0 just like code does.

        Which is the same lesson `read_pin()` in the script under test already
        applies — written here first and not followed. `ast` sees import
        statements; a regex sees text that looks like one.
        """
        offenders = []
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                if "pytest" in names:
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "these files import pytest, which tests.yml does not install: "
            f"{offenders}. Discovery fails on the import, not on an assertion.",
        )

    def test_ANKER_the_scan_actually_sees_the_test_files(self):
        """A scan over an empty set passes and proves nothing."""
        found = list((REPO_ROOT / "tests").glob("test_*.py"))
        self.assertGreater(len(found), 20, f"only {len(found)} test files found")


if __name__ == "__main__":
    unittest.main()
