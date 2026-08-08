"""The registry under `scripts/checks/` and the checks it carries.

The pure comparison functions are tested where they live —
`tests/test_ruff_pin.py`, `tests/test_ruff_version.py`,
`tests/test_workflow_templates.py`. The registry hangs them in rather than
replacing them, so those files are untouched.

What is new, and what this file covers:

* the verdict logic of the Ruff gates (`verdict`), as values;
* the wiring — that a finding travels out as `CheckFailed` instead of being
  swallowed;
* that the registry itself stays complete.

**No test here needs Ruff on PATH and none writes a shell shim.** Both were
learned the hard way in `mcp-audit-skill`: the test job there does not install
Ruff, and a `#!/bin/sh` shim is not executable on Windows. What can go wrong
hangs on exit codes and texts — so those are what gets passed in.

Stdlib `unittest` only, like the rest of this suite.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.checks as registry  # noqa: E402
from scripts.checks import CheckFailed, all_checks  # noqa: E402
from scripts.checks._core import Check, run, run_all  # noqa: E402
from scripts.checks.ruff_gate import subprojects, verdict  # noqa: E402

CHECKS_BY_NAME = {c.run.__name__: c for c in all_checks()}


class VerdictTest(unittest.TestCase):
    """Die reine Urteilsfunktion der Gates — ohne Ruff, ohne Unterprozess."""

    def test_green_when_every_target_exits_zero(self):
        ok, message = verdict("check", [(".", 0, "All checks passed!")])
        self.assertTrue(ok, message)
        self.assertIn("1 target", message)

    def test_every_failing_target_is_named_not_just_the_first(self):
        """Sonst kostet jeder Fehler eine eigene Runde."""
        ok, message = verdict(
            "check", [(".", 1, "E501 here"), ("examples/x", 1, "F401 there")]
        )
        self.assertFalse(ok)
        self.assertIn("E501", message)
        self.assertIn("F401", message)
        self.assertIn("examples/x", message)

    def test_a_healthy_target_is_not_blamed(self):
        ok, message = verdict("check", [(".", 0, "clean"), ("sub", 1, "F401")])
        self.assertFalse(ok)
        self.assertIn("sub", message)
        self.assertNotIn("clean", message)

    def test_the_format_verdict_says_how_to_fix_it(self):
        ok, message = verdict("format", [(".", 1, "1 file would be reformatted")])
        self.assertFalse(ok)
        self.assertIn("ruff format .", message)

    def test_a_success_message_is_never_empty(self):
        """Ein Lauf, der schweigt, wo er reden soll, ist schwer zu lesen."""
        for kind in ("check", "format"):
            with self.subTest(kind=kind):
                ok, message = verdict(kind, [(".", 0, "")])
                self.assertTrue(ok)
                self.assertTrue(message)


class SubprojectTest(unittest.TestCase):
    def test_the_root_pyproject_is_not_a_subproject(self):
        found = subprojects(REPO_ROOT)
        self.assertNotIn(REPO_ROOT, found)

    def test_vendored_trees_are_left_out(self):
        for path in subprojects(REPO_ROOT):
            rel = path.relative_to(REPO_ROOT).as_posix()
            for skipped in (".venv", "node_modules", ".git"):
                self.assertNotIn(skipped, rel.split("/"))


class WiringTest(unittest.TestCase):
    """Ein Befund muss als CheckFailed herauskommen, nicht verschluckt werden."""

    def test_a_missing_portfolio_file_is_a_finding(self):
        """ANKER: die Datei, deren Formatstabilitaet Check 5 zusichert."""
        with self.assertRaises(CheckFailed) as befund:
            CHECKS_BY_NAME["portfolio_format_stability"].run(REPO_ROOT / "tests")
        self.assertIn("anchor gone", str(befund.exception))

    def test_a_missing_directory_is_a_finding_not_a_skip(self):
        """ANKER: compileall ueber nichts darf nicht «alles parst» heissen."""
        with self.assertRaises(CheckFailed) as befund:
            CHECKS_BY_NAME["every_script_parses"].run(REPO_ROOT / "schemas")
        self.assertIn("anchor gone", str(befund.exception))

    def test_a_tree_without_the_pin_files_is_a_finding(self):
        with self.assertRaises(CheckFailed) as befund:
            CHECKS_BY_NAME["ruff_pin_sync"].run(REPO_ROOT / "schemas")
        self.assertIn("not readable", str(befund.exception))


class RegistryTest(unittest.TestCase):
    def test_numbers_are_unique_and_gapless(self):
        numbers = [c.number for c in all_checks()]
        self.assertEqual(numbers, sorted(set(numbers)))
        self.assertEqual(
            numbers,
            list(range(1, len(numbers) + 1)),
            "A gap is almost always a check that fell out of the registry.",
        )

    def test_registry_covers_every_module(self):
        """A module without an import line in __init__.py registers nothing.

        FULLY STATIC — neither imported nor asked at runtime, and that is the
        whole point. `@register` runs at import: the moment any test imports
        the module (this file imports `ruff_gate` for the verdict tests) it is
        registered regardless of what `__init__.py` says. A runtime query
        could never find the fault — measured in `mcp-audit-skill`, where the
        runtime version stayed green with the import line removed.
        """
        package = pathlib.Path(registry.__file__).parent
        with_register = {
            f.stem
            for f in sorted(package.glob("*.py"))
            if not f.name.startswith("_")
            and "@register(" in f.read_text(encoding="utf-8")
        }
        init = (package / "__init__.py").read_text(encoding="utf-8")
        imported = {
            name.strip()
            for line in re.findall(r"^from \. import (.+)$", init, re.M)
            for name in line.split(",")
        }
        missing = sorted(with_register - imported)
        self.assertFalse(
            missing,
            f"These modules call @register but are absent from the import line "
            f"in __init__.py: {missing}. Without it their checks vanish from "
            "every run of validate.sh without anything turning red.",
        )

    def test_a_crashing_check_is_a_defect_not_a_finding(self):
        def explodes(root):
            raise TypeError("broken")

        result = run(Check(number=99, label="broken", run=explodes), REPO_ROOT)
        self.assertFalse(result.ok)
        self.assertIn("abgestuerzt", result.output)
        self.assertIn("scripts/checks", result.output)
        self.assertIn("TypeError", result.output)

    def test_a_finding_is_not_mistaken_for_a_crash(self):
        def reports(root):
            raise CheckFailed("the repository has a problem")

        result = run(Check(number=98, label="finding", run=reports), REPO_ROOT)
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "the repository has a problem")
        self.assertNotIn("abgestuerzt", result.output)

    def test_one_run_names_every_finding(self):
        """Der Ertrag gegenueber sieben Workflow-Schritten in zwei Jobs."""

        def red(root):
            raise CheckFailed("finding")

        checks = [Check(number=n, label=f"c{n}", run=red) for n in (91, 92, 93)]
        results = run_all(REPO_ROOT, checks)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(not r.ok for r in results))

    def test_the_runner_selects_every_offline_check(self):
        """Der dokumentierte Weg und die Registry duerfen nicht auseinanderlaufen."""
        from scripts.checks.__main__ import select

        chosen = {c.number for c in select([], include_network=False)}
        expected = {c.number for c in all_checks(offline_only=True)}
        self.assertEqual(chosen, expected)


if __name__ == "__main__":
    unittest.main()
