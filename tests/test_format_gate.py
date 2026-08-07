#!/usr/bin/env python3
"""The counter-check for the format gate shipped in `ci.yml.template`.

Adding `ruff format --check` beside `ruff check` is only worth a CI step if the
two disagree about something. Asserting that they do is easy and proves nothing;
the claim is only established by code the LINTER passes and the FORMATTER
rejects. That is what these tests build, and they fail if the gap ever closes —
if a future Ruff moves formatting concerns into the lint rule set, the shipped
step becomes a duplicate and this suite is where that is noticed.

The second half is the subproject loop the same two steps carry, where the
measurement contradicted the obvious rationale:

  * a subproject's own `line-length` is ALREADY honoured by a plain
    `ruff format --check .` at the root — ruff resolves each file's nearest
    config — so a loop justified by differing widths catches nothing;
  * a root `exclude` covering the subproject DOES hide it from the directory
    run, and an explicitly passed path is checked anyway.

Both are asserted below, the first one deliberately as a negative result. It is
the reason the loop's comment in the workflow says `exclude` and not
`line-length`, and if ruff ever changes either behaviour the loop's rationale
needs rewriting — the tests say which one moved.

Runs the real `ruff` binary against fixtures in a temporary directory. No
network. `ruff` is installed by .github/workflows/tests.yml, pinned to the same
version as lint.yml and .pre-commit-config.yaml.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RUFF = shutil.which("ruff")

# A lint-clean file that no formatter would leave alone: padded parentheses,
# inconsistent quotes, doubled internal spaces, a missing space around `+`.
# None of it violates a lint rule — that is the entire point.
BADLY_FORMATTED = """def greet( name ):
    parts = [ 'hello',   "world" ]
    return  ' '.join(parts)+name
"""

ROOT_CONFIG = """[tool.ruff]
line-length = 88
[tool.ruff.lint]
select = ["E4", "E7", "E9", "F"]
"""


@unittest.skipIf(RUFF is None, "ruff binary not installed")
class FormatGateIsNotTheLinterTest(unittest.TestCase):
    """`ruff check` green and `ruff format --check` red, on the same file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pyproject.toml").write_text(ROOT_CONFIG, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _ruff(self, *args: str) -> subprocess.CompletedProcess[str]:
        assert RUFF is not None
        return subprocess.run(
            [RUFF, *args], cwd=self.root, capture_output=True, text=True
        )

    def test_the_linter_passes_code_the_format_gate_rejects(self) -> None:
        (self.root / "badformat.py").write_text(BADLY_FORMATTED, encoding="utf-8")

        lint = self._ruff("check", ".")
        fmt = self._ruff("format", "--check", ".")

        # Both halves matter. A red linter here would mean the fixture is not a
        # formatting-only defect and the counter-check proves nothing.
        self.assertEqual(
            lint.returncode,
            0,
            f"`ruff check` should stay green on formatting-only damage:\n{lint.stdout}",
        )
        self.assertEqual(
            fmt.returncode,
            1,
            "`ruff format --check` must reject it — otherwise the shipped step "
            f"is a second linter:\n{fmt.stdout}",
        )
        self.assertIn("badformat.py", fmt.stdout)

    def test_both_gates_agree_on_well_formatted_code(self) -> None:
        # The other direction of the same claim: the gate is not simply always
        # red, which would make the test above vacuous.
        assert RUFF is not None
        proc = subprocess.run(
            [RUFF, "format", "-"],
            cwd=self.root,
            input=BADLY_FORMATTED,
            capture_output=True,
            text=True,
        )
        (self.root / "ok.py").write_text(proc.stdout, encoding="utf-8")

        self.assertEqual(self._ruff("check", ".").returncode, 0)
        self.assertEqual(self._ruff("format", "--check", ".").returncode, 0)


@unittest.skipIf(RUFF is None, "ruff binary not installed")
class SubprojectLoopRationaleTest(unittest.TestCase):
    """What the per-subproject loop in the shipped steps does and does not add."""

    def _make(self, root_config: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "pyproject.toml").write_text(root_config, encoding="utf-8")
        sub = root / "sub"
        sub.mkdir()
        (sub / "pyproject.toml").write_text(
            '[tool.ruff]\nline-length = 50\n[tool.ruff.lint]\nselect = ["F"]\n',
            encoding="utf-8",
        )
        # 52 characters: fine at the root's 88, too long for the sub's 50.
        (sub / "wide.py").write_text(
            "result = some_function(alpha, beta, gamma, delta, e)\n", encoding="utf-8"
        )
        return root

    def _ruff(self, root: Path, *args: str) -> int:
        assert RUFF is not None
        return subprocess.run(
            [RUFF, *args], cwd=root, capture_output=True, text=True
        ).returncode

    def test_a_root_run_already_applies_the_subprojects_own_width(self) -> None:
        # NEGATIVE RESULT, kept deliberately. This is the rationale the obvious
        # reading would give the loop, and it does not hold: the plain directory
        # run already fails on the subproject's own line-length, so a loop added
        # for differing widths would be a step that catches nothing.
        root = self._make(ROOT_CONFIG)
        self.assertEqual(
            self._ruff(root, "format", "--check", "."),
            1,
            "ruff no longer resolves per-directory config on a root run — the "
            "loop's rationale in ci.yml.template needs rewriting",
        )

    def test_a_root_exclude_hides_the_subproject_from_the_directory_run(self) -> None:
        # The real gap, and the reason the loop exists.
        root = self._make(
            '[tool.ruff]\nline-length = 88\nexclude = ["sub"]\n'
            '[tool.ruff.lint]\nselect = ["F"]\n'
        )
        self.assertEqual(
            self._ruff(root, "format", "--check", "."),
            0,
            "expected the root run to skip the excluded subproject in silence",
        )

    def test_an_explicit_path_is_checked_despite_the_root_exclude(self) -> None:
        # ...and the reason it passes a path instead of relying on `.`.
        root = self._make(
            '[tool.ruff]\nline-length = 88\nexclude = ["sub"]\n'
            '[tool.ruff.lint]\nselect = ["F"]\n'
        )
        self.assertEqual(
            self._ruff(root, "format", "--check", "sub"),
            1,
            "an explicitly passed path must still be checked, and still at the "
            "subproject's own width",
        )


if __name__ == "__main__":
    unittest.main()
