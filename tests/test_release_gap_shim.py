#!/usr/bin/env python3
"""Tests for the `release_gap.py` compatibility shim.

The shim exists because merging `release_gap.py` into `shipped_probe.py` deleted
a file that callers outside this repository were invoking, and changed the exit
codes underneath anyone who moved to the new one. Its whole job is a contract:
old name, old flags, old exit codes.

What is tested here is that contract, and above all the ONE place a shim like
this goes quietly wrong — the exit-code translation. The two vocabularies do not
map one-to-one:

    old 0  no findings                new 0    green
    old 1  findings OR no comparison  new 2    findings
    old 2  not a Python repo          new 127  harness could not run

`127` covers two old codes at once. Translating it by table would be wrong half
the time, so the `2` case is decided before forwarding — and a test pins that,
because the failure mode is a caller being told "this is not a Python repo" when
the truth was an unreachable index.

Stdlib-only. No network: the merged probe's one network door (`_get`) is stubbed.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_gap as shim  # noqa: E402
import shipped_probe as sp  # noqa: E402

PYPROJECT = '[project]\nname = "demo-mcp"\nversion = "0.6.0"\n'


def make_repo(tmp: Path, tag: str = "v0.6.0") -> Path:
    (tmp / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(tmp), *a], check=True, capture_output=True,
        env={**__import__("os").environ, **env})
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("commit", "-qm", "chore: init")
    if tag:
        run("tag", tag)
    return tmp


class served:
    """Stub the merged probe's single network door for the duration of a block."""

    def __init__(self, versions=("0.6.0",), status="ok"):
        self.versions, self.status = list(versions), status

    def _get(self, url, timeout, accept=None):
        if self.status != "ok":
            return None, self.status, "index unreachable: simulated"
        if "/simple/" in url:
            return {"meta": {"api-version": "1.4"}, "versions": self.versions,
                    "files": [{"filename": f"demo_mcp-{v}.tar.gz", "yanked": False}
                              for v in self.versions]}, "ok", ""
        return {"info": {"version": self.versions[-1]},
                "releases": {v: [{"filename": f"demo_mcp-{v}.tar.gz", "yanked": False}]
                             for v in self.versions}}, "ok", ""

    def __enter__(self):
        self._orig = sp._get
        sp._get = self._get  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        sp._get = self._orig  # type: ignore[assignment]
        return False


def run_shim(*argv: str) -> int:
    """The shim's exit code, with its report and deprecation notice swallowed."""
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        return shim.main(list(argv))


class ExitContractTest(unittest.TestCase):
    """The old exit codes, which is the entire reason this file exists."""

    def test_clean_is_zero(self):
        with tempfile.TemporaryDirectory() as d, served():
            repo = make_repo(Path(d))
            self.assertEqual(run_shim("--target", str(repo)), 0)

    def test_findings_are_one_not_two(self):
        """The merged probe says 2 for findings; callers here were promised 1."""
        with tempfile.TemporaryDirectory() as d, served(versions=["0.1.0"]):
            # tag v0.6.0 against an index that only has 0.1.0 -> PUBLISH_GAP
            repo = make_repo(Path(d))
            self.assertEqual(run_shim("--target", str(repo)), 1)

    def test_an_unreachable_index_is_one_not_two(self):
        """The case a table-driven translation gets wrong.

        The merged probe answers 127 here, and 127 is also what it answers when
        it cannot determine a distribution name. Mapping 127 to the old `2`
        would tell this caller "not a Python MCP repo" about a repository that
        is plainly one — the index was simply unreachable.
        """
        with tempfile.TemporaryDirectory() as d, served(status="unreachable"):
            repo = make_repo(Path(d))
            self.assertEqual(run_shim("--target", str(repo)), 1)

    def test_a_non_python_target_is_two(self):
        """Decided before forwarding, exactly where the old script decided it."""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_shim("--target", d), 2)

    def test_the_non_python_check_happens_before_the_network(self):
        """It must not depend on the index being reachable to say `2`."""
        calls: list[str] = []
        orig = sp._get
        sp._get = lambda *a, **k: (calls.append("net"), (None, "unreachable", "x"))[1]
        try:
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(run_shim("--target", d), 2)
        finally:
            sp._get = orig
        self.assertEqual(calls, [], "the shim asked the index about a non-repo")

    def test_translate_collapses_everything_that_is_not_green(self):
        self.assertEqual(shim.translate(sp.EXIT_GREEN), 0)
        self.assertEqual(shim.translate(sp.EXIT_FINDINGS), 1)
        self.assertEqual(shim.translate(sp.EXIT_CANNOT_RUN), 1)
        self.assertEqual(shim.translate(42), 1, "an unknown code is never a pass")


class OldFlagsStillParseTest(unittest.TestCase):
    """Every flag the old script took. A caller that breaks on argparse is a
    caller this file failed."""

    def test_the_old_flag_set_is_accepted(self):
        with tempfile.TemporaryDirectory() as d, served():
            repo = make_repo(Path(d))
            for extra in (["--max-age-days", "14"], ["--offline"], ["--format", "json"],
                          ["--timeout", "30"], ["--index-url", "https://pypi.org/simple"]):
                with self.subTest(extra=extra):
                    self.assertIn(run_shim("--target", str(repo), *extra), (0, 1))

    def test_an_ignored_timeout_is_said_out_loud(self):
        """Swallowing it silently would let someone believe they had set it."""
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, served():
            repo = make_repo(Path(d))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                shim.main(["--target", str(repo), "--timeout", "30"])
        self.assertIn("--timeout is ignored", err.getvalue())

    def test_json_warns_that_the_schema_is_the_new_one(self):
        """The shim restores the contract it can. It does not fake the payload."""
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, served():
            repo = make_repo(Path(d))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                shim.main(["--target", str(repo), "--format", "json"])
        self.assertIn("index_version", err.getvalue())

    def test_every_run_says_it_is_deprecated(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, served():
            repo = make_repo(Path(d))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                shim.main(["--target", str(repo)])
        self.assertIn("deprecated", err.getvalue())
        self.assertIn("--metadata-only", err.getvalue())


class NoSecondImplementationTest(unittest.TestCase):
    """The shim must stay a shim.

    The merge existed to remove a duplicate probe. If this file grows index
    reading or finding logic of its own, the duplication is back under a new
    name — so the guard is on the file itself, not on its behaviour.
    """

    def test_it_holds_no_probe_logic(self):
        source = Path(shim.__file__).read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]  # skip the module docstring
        for marker in ("urllib", "fetch_simple", "fetch_json", "Finding(",
                       "release_key", "def probe"):
            self.assertNotIn(marker, body,
                             f"the shim grew probe logic: {marker}")


if __name__ == "__main__":
    unittest.main()
