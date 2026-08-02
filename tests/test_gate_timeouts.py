#!/usr/bin/env python3
"""The gate time bounds in scripts/nightly-audit.sh.

Two things are checked, and they fail differently:

* the REAL ``run_bounded`` helper is lifted out of the committed script and
  driven in bash, so the 124/137 contract the classifier depends on is measured
  rather than assumed. If GNU ``timeout``'s semantics ever shift under us, this
  is where it shows.
* every gate invocation in the script is actually wrapped. That is the
  regression this file mostly exists for: the natural way to lose a time bound
  is not to break ``run_bounded``, it is to add a seventh gate next year and
  call it directly. A gate without a bound cannot hang visibly — it just takes
  the night with it and reports nothing.

Stdlib-only; needs ``bash`` and ``timeout`` (both present in the audit
environment, and the script hard-fails without the latter by design).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "nightly-audit.sh"


def _extract_function(text: str, name: str) -> str:
    """The committed body of a shell function, brace-matched from its header."""
    start = text.index(f"{name}() {{")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("timeout"), "bash or timeout missing"
)
class RunBoundedTest(unittest.TestCase):
    """Drive the real helper, not a copy of what it is supposed to do."""

    @classmethod
    def setUpClass(cls) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        cls.body = _extract_function(text, "run_bounded")

    def _run(self, command: str, kill_after: str = "1") -> int:
        script = (
            f'TIMEOUT_BIN="$(command -v timeout)"\n'
            f'GATE_TIMEOUT_KILL_AFTER="{kill_after}"\n'
            f"{self.body}\n"
            f"{command}\n"
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, timeout=60
        ).returncode

    def test_a_command_that_finishes_keeps_its_own_exit_code(self) -> None:
        self.assertEqual(self._run("run_bounded 10 true"), 0)
        self.assertEqual(self._run("run_bounded 10 bash -c 'exit 3'"), 3)

    def test_a_hanging_command_returns_124(self) -> None:
        # The number the classifier reads as "this gate HUNG". Not 1, not 143.
        self.assertEqual(self._run("run_bounded 1 sleep 30"), 124)

    def test_a_command_ignoring_sigterm_returns_137(self) -> None:
        # --kill-after is not decoration: a process wedged in an uninterruptible
        # read is exactly the shape this guard exists for, and without the SIGKILL
        # the bound would be advisory.
        rc = self._run("run_bounded 1 bash -c 'trap \"\" TERM; sleep 30'")
        self.assertEqual(rc, 137)

    def test_the_whole_process_group_dies_with_it(self) -> None:
        # A gate is `uv run pytest`, so the process that hangs is a grandchild. If
        # the bound only killed the direct child, the suite would keep running and
        # the next gate would compete with it for the port and the CPU.
        marker = "GATEBOUNDMARKER"
        rc = self._run(
            "run_bounded 1 bash -c 'bash -c \"exec -a %s sleep 40\" & sleep 40'"
            % marker
        )
        self.assertEqual(rc, 124)
        alive = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True)
        self.assertNotIn(
            marker,
            alive.stdout,
            "the grandchild outlived the bound — an orphan is still running",
        )


class EveryGateIsBoundedTest(unittest.TestCase):
    """Structural: no gate invocation may reach the target unbounded."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text(encoding="utf-8")
        cls.lines = cls.text.splitlines()

    def _logical_lines(self) -> list[tuple[int, str]]:
        """Comment-free logical commands, backslash continuations joined.

        Joining matters: `run_bounded ... \\` and its `|| hard_fail "git fetch
        failed…"` continuation are ONE command, and a line-at-a-time check reads
        the error message as a second, unbounded invocation.
        """
        out: list[tuple[int, str]] = []
        buf, start = "", 0
        for i, raw in enumerate(self.lines, 1):
            stripped = raw.strip()
            if not buf and (not stripped or stripped.startswith("#")):
                continue
            if not buf:
                start = i
            buf += " " + stripped
            if stripped.endswith("\\"):
                buf = buf[:-1]
                continue
            out.append((start, buf.strip()))
            buf = ""
        if buf:
            out.append((start, buf.strip()))
        return out

    # A launcher that is itself bounded. run_in_target takes the budget as its
    # second argument and hands the command to run_bounded; the test below pins
    # that, so accepting it here is not a hole.
    _BOUNDED = ("run_bounded", "run_in_target")

    def _unbounded(self, predicate) -> list[tuple[int, str]]:
        return [
            (n, ln)
            for n, ln in self._logical_lines()
            if predicate(ln) and not any(w in ln for w in self._BOUNDED)
        ]

    def test_run_in_target_is_itself_bounded(self) -> None:
        body = _extract_function(self.text, "run_in_target")
        self.assertIn("run_bounded", body)
        self.assertIn(
            'local secs="$2"',
            body,
            "run_in_target must take a per-gate budget, not a fixed one",
        )

    def test_every_uv_run_goes_through_a_bounded_launcher(self) -> None:
        # `uv run` is how every in-target gate is launched — ruff, mypy, pytest,
        # the schema generator, the boot probe, the rebinding probe. This is the
        # check that catches next year's seventh gate being added unwrapped.
        unbounded = self._unbounded(lambda ln: "uv run " in ln)
        self.assertEqual(unbounded, [], f"unbounded gate invocation(s): {unbounded}")

    def test_the_shipped_artifact_gate_is_bounded(self) -> None:
        # It builds a venv and does a cold `pip install` from the index — the gate
        # most likely to sit waiting on a socket.
        unbounded = self._unbounded(lambda ln: "shipped_probe.py" in ln)
        self.assertEqual(unbounded, [], f"unbounded shipped-artifact call: {unbounded}")

    def test_the_promptfoo_eval_is_bounded(self) -> None:
        unbounded = self._unbounded(lambda ln: '"${pf_cmd[@]}"' in ln)
        self.assertEqual(unbounded, [], f"unbounded promptfoo invocation: {unbounded}")

    def test_the_provisioning_git_calls_are_bounded(self) -> None:
        # A git fetch stalling against a filtered egress path hangs exactly like a
        # wedged gate; six hours later "the audit could not complete" is not a
        # report anyone can act on.
        unbounded = self._unbounded(
            lambda ln: re.search(
                r"(?:^|\|\||&&|;|\$\()\s*git (?:-C \S+ )?(?:clone|fetch)\b", ln
            )
        )
        self.assertEqual(unbounded, [], f"unbounded provisioning call(s): {unbounded}")

    def test_a_missing_timeout_binary_hard_fails_instead_of_running_unbounded(
        self,
    ) -> None:
        self.assertRegex(
            self.text,
            r'\[ -n "\$\{TIMEOUT_BIN\}" \] \|\| hard_fail',
            "the script must refuse to run rather than fall through to unbounded gates",
        )

    def test_run_bounded_does_not_preserve_status(self) -> None:
        # --preserve-status makes `timeout` return the command's own code instead
        # of 124, which would erase the entire distinction this change adds.
        self.assertNotIn(
            "--preserve-status", _extract_function(self.text, "run_bounded")
        )

    def test_the_probe_remaps_exempt_the_hang_codes(self) -> None:
        # The boot and rebinding gates rewrite "no report written + non-zero" to
        # 127. A killed probe also writes no report, so without the exemption a
        # hang would be relabelled "never ran" — losing the one detail that says
        # where to look.
        # Anchored on the phrase unique to the two REMAPS ("the probe itself did
        # not run"), not on "recording 127" — other gates legitimately record 127
        # for their own reasons, and matching that would make this test fail every
        # time an unrelated gate is added.
        remaps = [ln for ln in self.lines if "the probe itself did not run" in ln]
        self.assertEqual(len(remaps), 2, "expected the boot and rebinding remaps")
        self.assertEqual(self.text.count('-ne 124 ] && [ "${rc_boot}" -ne 137 ]'), 1)
        self.assertEqual(self.text.count('-ne 124 ] && [ "${rc_rebind}" -ne 137 ]'), 1)

    def test_the_test_count_is_measured_and_shipped(self) -> None:
        # The Broker never sees pytest.log, so a green gate's suite size can only
        # reach the classifier through the evidence.
        self.assertIn("--count-tests", self.text)
        self.assertRegex(self.text, r'"tests_collected": \$\{tests_collected\}')
        self.assertIn("--tests-collected", self.text)

    def test_an_unreadable_count_falls_back_to_unknown_not_zero(self) -> None:
        # Reporting "no tests" because the log could not be parsed would invent
        # the very finding the count exists to catch.
        self.assertRegex(self.text, r"\*\[!0-9-\]\*\)\s*tests_collected=-1")

    def test_the_shipped_metadata_pre_run_comes_first_and_is_cheap(self) -> None:
        """The shipped gate is the one most likely to sit on a socket, and when
        it exhausts its budget it is killed before writing a report — rc=124 and
        nothing else, on the gate that knows whether users are installing a
        withdrawn release. The pre-run answers the metadata half in seconds and
        writes it separately, so that verdict survives the hang.

        Order is the whole point: a pre-run that runs *after* the gate it is
        insuring against buys nothing.
        """
        meta = self.text.index("--metadata-only")
        full = self.text.index('--report "${shipped_report}"')
        self.assertLess(meta, full, "the pre-run must precede the full gate")
        self.assertIn("shipped-metadata.json", self.text)
        self.assertIn("GATE_TIMEOUT_SHIPPED_META", self.text)

    def test_the_pre_run_does_not_decide_the_shipped_verdict(self) -> None:
        """`rc_shipped` must come from the full gate alone.

        Letting a green metadata pass lower a 124 would turn "this gate hung"
        into "this gate is fine", which is the exact substitution the 124/137
        handling above exists to prevent — and "the metadata is consistent" was
        never the shipped-artifact gate's question anyway.
        """
        assignments = [
            ln.strip() for ln in self.lines if re.match(r"\s*rc_shipped=", ln)
        ]
        self.assertTrue(assignments, "no rc_shipped assignment found")
        for line in assignments:
            self.assertNotIn(
                "rc_shipped_meta",
                line,
                f"the pre-run must not feed the gate verdict: {line}",
            )


if __name__ == "__main__":
    unittest.main()
