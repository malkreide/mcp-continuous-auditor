#!/usr/bin/env python3
"""Tests for scripts/portfolio_scan.py — the portfolio fan-out.

The module exists because a nested server fell out of every hand-written
enumeration during an SDK major migration and was left on the old API. So the
tests are built around the three properties that would have caught it, and
around the one that keeps the sweep usable at all:

* ``nested_manifests`` reports a manifest below the root that no target claims —
  fail-closed, whether or not it looks like a server.
* the ``outliers`` pass finds the one target that disagrees with the majority
  WITHOUT any configured expectation, because mid-migration nobody knows which
  version is the right one until they see fourteen agree and one not.
* a target that cannot be checked out produces a row of "could not run" cells
  and the sweep continues — partial results are the deliverable, and an
  incomplete sweep must never read as "no findings".

Everything is offline: targets carry a local ``path:`` instead of a repo, so no
clone is attempted. Stdlib-only; the YAML cross-check self-skips without PyYAML.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import portfolio_scan as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "targets.example.yaml"

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover - environment dependent
    _HAVE_YAML = False


def _server(
    root: Path,
    name: str,
    sdk: str = "fastmcp>=2.0,<3",
    settings_write: bool = False,
    allowlist: bool = False,
) -> Path:
    """A minimal target checkout on disk."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        textwrap.dedent(f'''
        [project]
        name = "{name}"
        version = "0.1.0"
        dependencies = ["{sdk}", "httpx>=0.27"]
    ''').strip()
        + "\n",
        encoding="utf-8",
    )
    src = root / name.replace("-", "_")
    src.mkdir(exist_ok=True)
    body = "from fastmcp import FastMCP\nmcp = FastMCP('t')\n"
    if settings_write:
        body += "mcp.settings.host = '0.0.0.0'\n"
    (src / "server.py").write_text(body, encoding="utf-8")
    if allowlist:
        (root / ".env.example").write_text(
            "MCP_ALLOWED_HOSTS=x.example:8000\n", encoding="utf-8"
        )
    return root


class TargetsFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, text: str) -> Path:
        p = self.dir / "targets.yaml"
        p.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return p

    def test_the_committed_example_parses(self) -> None:
        targets, defaults = ps.load_targets(EXAMPLE)
        self.assertGreaterEqual(len(targets), 3)
        # No `ref` default, and that is the point: an unset ref is resolved
        # against the remote at clone time. Three repositories in this portfolio
        # are on `master`, and a default of `main` clones nothing there.
        self.assertIsNone(defaults.get("ref"))
        self.assertEqual([t.ref for t in targets], [""] * len(targets))
        self.assertEqual(targets[0].repo, "malkreide/zurich-opendata-mcp")
        # Per-target overrides win over defaults.
        opted_in = [t for t in targets if "boot" in t.predicates]
        self.assertTrue(opted_in, "the example should demonstrate opting into `boot`")
        acknowledged = [t for t in targets if t.known_manifests]
        self.assertTrue(
            acknowledged,
            "the example should demonstrate acknowledging a nested manifest",
        )

    @staticmethod
    def _stdlib_parse(text: str) -> object:
        """parse_targets_yaml with PyYAML hidden, i.e. the reader the Worker uses."""
        saved = sys.modules.get("yaml", "<absent>")
        sys.modules["yaml"] = None  # type: ignore[assignment]
        try:
            return ps.parse_targets_yaml(text)
        finally:
            if saved == "<absent>":
                sys.modules.pop("yaml", None)
            else:
                sys.modules["yaml"] = saved  # type: ignore[assignment]

    @unittest.skipUnless(
        _HAVE_YAML, "PyYAML is absent, so there is nothing to compare to"
    )
    def test_the_stdlib_reader_agrees_with_pyyaml(self) -> None:
        # The Worker has no PyYAML, so the subset reader is what actually parses
        # the portfolio there. If the two ever disagree, a target silently drops
        # out of the sweep ON THE WORKER ONLY — the exact class of bug this module
        # was written to prevent, reintroduced by its own config loader.
        import yaml as real

        samples = [
            EXAMPLE.read_text(encoding="utf-8"),
            "schema: 1\ntargets:\n  - repo: a/b\n",
            # inline comments, quoted scalars, flow lists, a wrapped flow list
            textwrap.dedent("""
                defaults:              # trailing comment
                  ref: "main"
                  predicates: [manifest, sdk_major]
                  sdk_major_expect: "2"
                targets:
                  - repo: a/b
                    ref: v1.0
                    predicates: [manifest,
                                 sdk_major, boot]
                  - repo: c/d
                    known_manifests: [x/pyproject.toml]
            """).lstrip(),
        ]
        for i, text in enumerate(samples):
            with self.subTest(sample=i):
                self.assertEqual(self._stdlib_parse(text), real.safe_load(text))

    def test_the_stdlib_reader_alone_understands_the_example(self) -> None:
        # Runs with or without PyYAML: whatever the runner has, the fallback must
        # stand on its own, because on the Worker it is the only reader there is.
        data = self._stdlib_parse(EXAMPLE.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual(len(data["targets"]), 4)
        self.assertEqual(data["defaults"]["sdk_major_expect"], "2")
        self.assertIn("boot", data["targets"][2]["predicates"])
        self.assertEqual(
            data["targets"][3]["known_manifests"], ["examples/demo/pyproject.toml"]
        )

    def test_a_duplicate_target_is_refused(self) -> None:
        # A duplicate halves that target's coverage while the matrix still shows
        # a full-looking row.
        p = self._write("""
            targets:
              - repo: a/b
              - repo: a/b
        """)
        with self.assertRaises(ps.TargetsError) as cm:
            ps.load_targets(p)
        self.assertIn("twice", str(cm.exception))

    def test_a_malformed_repo_is_refused(self) -> None:
        p = self._write("targets:\n  - repo: not-a-slug\n")
        with self.assertRaises(ps.TargetsError):
            ps.load_targets(p)

    def test_an_empty_target_list_is_refused(self) -> None:
        p = self._write("defaults:\n  ref: main\ntargets: []\n")
        with self.assertRaises(ps.TargetsError):
            ps.load_targets(p)

    def test_defaults_apply_and_entries_override(self) -> None:
        p = self._write("""
            defaults:
              ref: main
              predicates: [manifest]
              sdk_major_expect: "2"
            targets:
              - repo: a/b
              - repo: c/d
                ref: v1.2.3
                predicates: [manifest, sdk_major]
        """)
        targets, _ = ps.load_targets(p)
        self.assertEqual(targets[0].ref, "main")
        self.assertEqual(targets[0].predicates, ["manifest"])
        self.assertEqual(targets[1].ref, "v1.2.3")
        self.assertEqual(targets[1].predicates, ["manifest", "sdk_major"])
        self.assertEqual(targets[1].sdk_major_expect, "2")


class PredicateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ctx(self, root: Path, **over) -> ps.Ctx:
        t = ps.Target(repo="o/r", **over)
        return ps.Ctx(target=t, root=root, timeout=10)

    def test_manifest_reads_name_and_version(self) -> None:
        root = _server(self.dir / "a", "demo-mcp")
        cell = ps.pred_manifest(self._ctx(root))
        self.assertEqual(cell.status, ps.OK)
        self.assertIn("demo-mcp", cell.value)

    def test_a_missing_manifest_is_flagged(self) -> None:
        (self.dir / "empty").mkdir()
        self.assertEqual(
            ps.pred_manifest(self._ctx(self.dir / "empty")).status, ps.FLAG
        )

    def test_sdk_major_is_read_off_the_constraint(self) -> None:
        for spec, want in (
            ("fastmcp>=2.0,<3", "2"),
            ("mcp>=1.2", "1"),
            ("fastmcp==2.3.1", "2"),
            ("mcp~=1.0", "1"),
        ):
            with self.subTest(spec=spec):
                root = _server(
                    self.dir
                    / spec.replace(">", "_")
                    .replace("=", "_")
                    .replace("<", "_")
                    .replace(",", "_")
                    .replace("~", "_"),
                    "x",
                    sdk=spec,
                )
                self.assertEqual(ps.pred_sdk_major(self._ctx(root)).value, want)

    def test_the_wrong_major_is_flagged_when_an_expectation_is_set(self) -> None:
        root = _server(self.dir / "old", "old-mcp", sdk="mcp>=1.2")
        cell = ps.pred_sdk_major(self._ctx(root, sdk_major_expect="2"))
        self.assertEqual(cell.status, ps.FLAG)
        self.assertIn("expects 2", cell.detail)

    def test_an_unpinned_sdk_is_a_note_not_a_pass(self) -> None:
        # "whatever resolved last" is how a portfolio drifts apart in the first
        # place, so it must not render as a clean tick.
        root = _server(self.dir / "loose", "loose-mcp", sdk="fastmcp")
        cell = ps.pred_sdk_major(self._ctx(root))
        self.assertEqual(cell.status, ps.NOTE)
        self.assertEqual(cell.value, "unpinned")

    def test_a_settings_assignment_is_flagged_with_its_location(self) -> None:
        # The parlament-mcp#29 crash-at-start, greppable across the portfolio in
        # a second — which is what makes it a good predicate rather than a gate.
        root = _server(self.dir / "crash", "crash-mcp", settings_write=True)
        cell = ps.pred_settings_write(self._ctx(root))
        self.assertEqual(cell.status, ps.FLAG)
        self.assertIn("server.py", cell.detail)
        self.assertIn(".host", cell.detail)

    def test_a_comparison_is_not_mistaken_for_an_assignment(self) -> None:
        root = _server(self.dir / "cmp", "cmp-mcp")
        (root / "cmp_mcp" / "check.py").write_text(
            "if mcp.settings.host == '0.0.0.0':\n    pass\n", encoding="utf-8"
        )
        self.assertEqual(ps.pred_settings_write(self._ctx(root)).status, ps.OK)

    def test_a_missing_allowlist_knob_is_a_note_not_a_flag(self) -> None:
        # Consistent with the rebinding gate: fail-open is a deployment state, not
        # a defect. In a matrix it is still exactly what you want to see.
        root = _server(self.dir / "nolist", "nolist-mcp")
        cell = ps.pred_host_allowlist_knob(self._ctx(root))
        self.assertEqual(cell.status, ps.NOTE)
        root2 = _server(self.dir / "haslist", "haslist-mcp", allowlist=True)
        self.assertEqual(ps.pred_host_allowlist_knob(self._ctx(root2)).status, ps.OK)


class NestedManifestTest(unittest.TestCase):
    """The occasion, pinned. A server that is not the root package of its
    repository is invisible to every list written by hand."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ctx(self, root: Path, known=()) -> ps.Ctx:
        return ps.Ctx(
            target=ps.Target(repo="o/r", known_manifests=list(known)),
            root=root,
            timeout=10,
        )

    def test_a_root_only_repo_is_clean(self) -> None:
        root = _server(self.dir / "solo", "solo-mcp")
        self.assertEqual(ps.pred_nested_manifests(self._ctx(root)).status, ps.OK)

    def test_a_nested_server_is_found(self) -> None:
        root = _server(self.dir / "multi", "multi-mcp")
        _server(root / "services" / "inner", "inner-mcp", sdk="mcp>=1.0")
        cell = ps.pred_nested_manifests(self._ctx(root))
        self.assertEqual(cell.status, ps.FLAG)
        self.assertIn("services/inner/pyproject.toml", cell.detail)
        self.assertIn("MCP SDK", cell.detail)

    def test_a_nested_manifest_is_flagged_even_when_it_looks_harmless(self) -> None:
        # Fail-closed on purpose. A heuristic that only flagged server-shaped
        # manifests would let through the one that does not match the heuristic —
        # the same bet that lost the first time.
        root = _server(self.dir / "harmless", "harmless-mcp")
        (root / "tools").mkdir()
        (root / "tools" / "package.json").write_text(
            '{"name":"helper"}', encoding="utf-8"
        )
        cell = ps.pred_nested_manifests(self._ctx(root))
        self.assertEqual(cell.status, ps.FLAG)
        self.assertIn("tools/package.json", cell.detail)

    def test_acknowledging_it_silences_it(self) -> None:
        root = _server(self.dir / "ack", "ack-mcp")
        _server(root / "examples" / "demo", "demo", sdk="mcp>=1.0")
        noisy = ps.pred_nested_manifests(self._ctx(root))
        self.assertEqual(noisy.status, ps.FLAG)
        quiet = ps.pred_nested_manifests(
            self._ctx(root, known=["examples/demo/pyproject.toml"])
        )
        self.assertEqual(quiet.status, ps.OK)

    def test_vendored_trees_are_not_reported(self) -> None:
        root = _server(self.dir / "vend", "vend-mcp")
        vendored = root / "node_modules" / "dep"
        vendored.mkdir(parents=True)
        (vendored / "package.json").write_text('{"name":"dep"}', encoding="utf-8")
        self.assertEqual(ps.pred_nested_manifests(self._ctx(root)).status, ps.OK)


class MatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _row(name: str, **cells) -> ps.Row:
        return ps.Row(
            target=ps.Target(repo=name),
            cells={
                k: (v if isinstance(v, ps.Cell) else ps.Cell(ps.OK, v))
                for k, v in cells.items()
            },
        )

    def test_the_odd_one_out_is_found_without_any_expectation(self) -> None:
        # THE POINT. Mid-migration nobody knows which major is "right" until they
        # see that four repos agree and one does not — so the outlier pass must
        # work with no `sdk_major_expect` configured at all.
        rows = [self._row(f"o/r{i}", sdk_major="2") for i in range(4)]
        rows.append(self._row("o/legacy", sdk_major="1"))
        outs = ps.outliers(rows, ["sdk_major"])
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0]["majority"], "2")
        self.assertEqual([d["target"] for d in outs[0]["deviants"]], ["o/legacy"])

    def test_unanimity_produces_no_outlier(self) -> None:
        rows = [self._row(f"o/r{i}", sdk_major="2") for i in range(4)]
        self.assertEqual(ps.outliers(rows, ["sdk_major"]), [])

    def test_an_even_split_is_not_called_an_outlier(self) -> None:
        # Two camps of two is a decision to make, not a deviation to report.
        rows = [
            self._row("o/a", sdk_major="1"),
            self._row("o/b", sdk_major="1"),
            self._row("o/c", sdk_major="2"),
            self._row("o/d", sdk_major="2"),
        ]
        self.assertEqual(ps.outliers(rows, ["sdk_major"]), [])

    def test_an_error_cell_is_not_counted_as_a_dissenting_opinion(self) -> None:
        # A missing measurement is not a minority view. Counting it as one would
        # invent an outlier out of an unreachable repo.
        rows = [self._row(f"o/r{i}", sdk_major="2") for i in range(3)]
        rows.append(self._row("o/broken", sdk_major=ps.Cell(ps.ERROR, "no checkout")))
        self.assertEqual(ps.outliers(rows, ["sdk_major"]), [])

    def test_an_unreachable_target_does_not_end_the_sweep(self) -> None:
        good = _server(self.dir / "good", "good-mcp")
        targets = [
            ps.Target(repo="", path=str(good), predicates=["manifest"]),
            ps.Target(
                repo="o/gone", path=str(self.dir / "nope"), predicates=["manifest"]
            ),
        ]
        rows = [ps.scan_target(t, self.dir, t.predicates, 5, 5, 5) for t in targets]
        self.assertEqual(rows[0].cells["manifest"].status, ps.OK)
        self.assertEqual(rows[1].cells["manifest"].status, ps.ERROR)
        self.assertIn("does not exist", rows[1].error)

    def test_an_incomplete_sweep_outranks_findings(self) -> None:
        # "We did not look" and "we looked and found nothing" are different
        # claims; only one of them is a sweep. So a partial run cannot report a
        # clean bill — even though its real findings are still listed.
        rows = [
            self._row("o/a", p=ps.Cell(ps.FLAG, "bad")),
            self._row("o/b", p=ps.Cell(ps.ERROR, "no checkout")),
        ]
        outcome, code = ps.classify(rows)
        self.assertEqual(outcome, "incomplete")
        self.assertEqual(code, ps.EXIT_INCOMPLETE)
        text = ps.render(rows, ["p"], [], outcome)
        self.assertIn("INCOMPLETE", text)
        self.assertIn("Flagged", text)  # the real finding is still shown

    def test_a_flag_alone_is_findings(self) -> None:
        rows = [self._row("o/a", p=ps.Cell(ps.FLAG, "bad")), self._row("o/b", p="fine")]
        self.assertEqual(ps.classify(rows), ("findings", ps.EXIT_FINDINGS))

    def test_notes_do_not_make_a_run_red(self) -> None:
        rows = [
            self._row("o/a", p=ps.Cell(ps.NOTE, "absent")),
            self._row("o/b", p="fine"),
        ]
        self.assertEqual(ps.classify(rows), ("green", ps.EXIT_GREEN))
        self.assertIn("Noted (not findings)", ps.render(rows, ["p"], [], "green"))

    def test_the_matrix_renders_one_row_per_target(self) -> None:
        rows = [self._row("o/a", sdk_major="2"), self._row("o/b", sdk_major="1")]
        text = ps.render(rows, ["sdk_major"], ps.outliers(rows, ["sdk_major"]), "green")
        self.assertIn("| `o/a` |", text)
        self.assertIn("| `o/b` |", text)

    def test_the_matrix_names_the_ref_it_measured(self) -> None:
        """Three of these repositories are on `master`. A report that does not
        say which branch it read cannot be checked."""
        row = self._row("o/a", sdk_major="2")
        row.ref = "master"
        text = ps.render([row], ["sdk_major"], [], "green")
        self.assertIn("| `master` |", text)


class CoverageTest(unittest.TestCase):
    """Three servers fell out of the tracking for half a day.

    Nothing was red. Nothing was even reported — they were simply not in the
    runs, and every run that did happen said "no findings" about the servers it
    had looked at, which reads exactly like "no findings". It was noticed only
    because 26 + 4 did not come to 33.
    """

    @staticmethod
    def _targets(n: int) -> list[ps.Target]:
        return [ps.Target(repo=f"o/r{i}") for i in range(n)]

    def test_a_full_run_is_complete(self) -> None:
        ts = self._targets(3)
        cov = ps.assess_coverage(ts, ts, None, False)
        self.assertTrue(cov.complete)
        self.assertEqual(cov.skipped, [])

    def test_a_narrowed_run_names_what_it_left_out(self) -> None:
        """By NAME, not by count. A count is what the campaign already had."""
        ts = self._targets(3)
        cov = ps.assess_coverage(ts, ts[:2], None, False)
        self.assertFalse(cov.complete)
        self.assertEqual(cov.skipped, ["o/r2"])
        self.assertIn("o/r2", cov.detail)

    def test_a_short_targets_file_cannot_be_covered_by_any_selection(self) -> None:
        ts = self._targets(30)
        cov = ps.assess_coverage(ts, ts, 33, False)
        self.assertFalse(cov.complete)
        self.assertIn("33 were expected", cov.detail)

    def test_an_incomplete_run_gives_no_overall_verdict(self) -> None:
        ts = self._targets(3)
        cov = ps.assess_coverage(ts, ts[:2], None, False)
        outcome, code = ps.classify([], cov)
        self.assertEqual((outcome, code), ("incomplete", ps.EXIT_INCOMPLETE))
        text = ps.render([], ["p"], [], outcome, cov)
        self.assertIn("NO OVERALL VERDICT", text)
        self.assertIn("`o/r2`", text)

    def test_coverage_outranks_a_clean_matrix(self) -> None:
        """The sentence three servers hid behind was "no findings"."""
        ts = self._targets(3)
        rows = [ps.Row(target=t, cells={"p": ps.Cell(ps.OK, "fine")}) for t in ts[:2]]
        cov = ps.assess_coverage(ts, ts[:2], None, False)
        self.assertEqual(ps.classify(rows, cov)[0], "incomplete")

    def test_partial_acknowledges_a_narrow_run_and_still_names_the_gap(self) -> None:
        ts = self._targets(3)
        cov = ps.assess_coverage(ts, ts[:2], None, acknowledged=True)
        self.assertFalse(cov.complete)
        self.assertTrue(cov.claimable)
        self.assertEqual(ps.classify([], cov)[0], "green")
        text = ps.render([], ["p"], [], "green", cov)
        self.assertIn("PARTIAL (acknowledged with --partial)", text)
        self.assertIn("`o/r2`", text)

    def test_no_coverage_object_leaves_the_verdict_alone(self) -> None:
        rows = [ps.Row(target=ps.Target(repo="o/a"), cells={"p": ps.Cell(ps.OK)})]
        self.assertEqual(ps.classify(rows)[0], "green")


class DefaultBranchTest(unittest.TestCase):
    """`git remote show origin` answered wrongly for four repositories in one
    sitting: it reads `refs/remotes/origin/HEAD` out of the local clone, which
    is written once at clone time and never refreshed. `ls-remote` asks.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _origin(self, branch: str) -> Path:
        root = self.dir / f"origin-{branch}"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "x-mcp"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        for cmd in (
            ["git", "init", "-q", "-b", branch],
            ["git", "config", "user.email", "t@example.invalid"],
            ["git", "config", "user.name", "T"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "chore: init"],
        ):
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)
        return root

    def test_a_master_repository_is_resolved_not_assumed(self) -> None:
        origin = self._origin("master")
        branch, reason = ps.default_branch("file://" + str(origin), 30)
        self.assertEqual((branch, reason), ("master", ""))

    def test_a_main_repository_resolves_too(self) -> None:
        origin = self._origin("main")
        self.assertEqual(ps.default_branch("file://" + str(origin), 30)[0], "main")

    def test_an_unreachable_remote_yields_a_reason_and_no_guess(self) -> None:
        branch, reason = ps.default_branch("file://" + str(self.dir / "absent"), 30)
        self.assertEqual(branch, "")
        self.assertTrue(reason, "a failure must carry its reason, not an empty string")

    def test_the_symref_line_is_parsed_and_the_sha_line_is_not(self) -> None:
        out = "ref: refs/heads/release/2.x\tHEAD\n0123456789abcdef\tHEAD\n"
        self.assertEqual(ps._SYMREF.search(out).group("branch"), "release/2.x")

    def test_an_unset_ref_asks_the_remote(self) -> None:
        asked: list[str] = []

        def resolver(url: str, timeout: float) -> tuple[str, str]:
            asked.append(url)
            return "master", ""

        target = ps.Target(repo="o/r")
        root, reason, ref = ps.checkout(target, self.dir / "w", 30, resolver)
        # The clone itself fails (there is no such repo) — what matters here is
        # that the branch was ASKED for rather than assumed.
        self.assertEqual(asked, ["https://github.com/o/r.git"])
        self.assertEqual(ref, "master")
        self.assertIsNone(root)
        self.assertTrue(reason)

    def test_an_explicit_ref_is_not_second_guessed(self) -> None:
        def resolver(url: str, timeout: float) -> tuple[str, str]:
            raise AssertionError("an explicit ref must not be resolved away")

        _, _, ref = ps.checkout(
            ps.Target(repo="o/r", ref="v1.2.3"), self.dir / "w", 30, resolver
        )
        self.assertEqual(ref, "v1.2.3")

    def test_an_unresolvable_default_still_clones_the_remote_head(self) -> None:
        """No `--branch` at all: git takes the remote's HEAD, which is the same
        answer by another route. What it must never do is fall back to `main`."""
        origin = self._origin("master")
        target = ps.Target(repo="o/r")
        with unittest.mock.patch.object(
            ps, "default_branch", lambda url, t: ("", "ls-remote refused")
        ):
            # Point the clone at the local origin by using a path target's URL
            # shape: the resolver failing is what is under test.
            cmds: list[list[str]] = []
            real = subprocess.run

            def spy(cmd, *a, **k):
                cmds.append(list(cmd))
                if cmd[:2] == ["git", "clone"]:
                    cmd = [
                        c if not c.startswith("https://") else "file://" + str(origin)
                        for c in cmd
                    ]
                return real(cmd, *a, **k)

            with unittest.mock.patch.object(subprocess, "run", spy):
                root, reason, ref = ps.checkout(target, self.dir / "w2", 30)
        clone = next(c for c in cmds if c[:2] == ["git", "clone"])
        self.assertNotIn("--branch", clone)
        self.assertIsNotNone(root, msg=reason)
        self.assertEqual(ref, "master")


class EndToEndTest(unittest.TestCase):
    """main() over local checkouts — no network, no clone."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, targets_text: str, *extra: str) -> tuple[int, dict, str]:
        tf = self.dir / "targets.yaml"
        tf.write_text(textwrap.dedent(targets_text).lstrip(), encoding="utf-8")
        report = self.dir / "matrix.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = ps.main(
                [
                    "--targets",
                    str(tf),
                    "--report",
                    str(report),
                    "--workdir",
                    str(self.dir / "work"),
                    *extra,
                ]
            )
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return rc, data, out.getvalue()

    def test_a_portfolio_with_one_laggard_produces_the_matrix_and_the_outlier(
        self,
    ) -> None:
        for i in range(3):
            _server(self.dir / f"ok{i}", f"ok{i}-mcp", sdk="fastmcp>=2.0,<3")
        _server(self.dir / "legacy", "legacy-mcp", sdk="mcp>=1.2")
        rc, data, text = self._run(f"""
            defaults:
              predicates: [manifest, sdk_major]
            targets:
              - repo: o/ok0
                path: {self.dir / "ok0"}
              - repo: o/ok1
                path: {self.dir / "ok1"}
              - repo: o/ok2
                path: {self.dir / "ok2"}
              - repo: o/legacy
                path: {self.dir / "legacy"}
        """)
        self.assertEqual(rc, ps.EXIT_GREEN, msg=text)
        self.assertEqual(data["outcome"], "green")
        # Nothing is a *finding* — no expectation was configured. The value is
        # entirely in the row that breaks the pattern.
        self.assertEqual(len(data["outliers"]), 1)
        self.assertEqual(data["outliers"][0]["predicate"], "sdk_major")
        self.assertEqual(
            [d["target"] for d in data["outliers"][0]["deviants"]], ["o/legacy"]
        )
        self.assertIn("Out of line", text)

    def test_a_nested_server_is_reported_end_to_end(self) -> None:
        root = _server(self.dir / "host", "host-mcp")
        _server(root / "packages" / "hidden", "hidden-mcp", sdk="mcp>=1.0")
        rc, data, text = self._run(f"""
            defaults:
              predicates: [nested_manifests]
            targets:
              - repo: o/host
                path: {root}
        """)
        self.assertEqual(rc, ps.EXIT_FINDINGS)
        cell = data["targets"][0]["cells"]["nested_manifests"]
        self.assertEqual(cell["status"], ps.FLAG)
        self.assertIn("packages/hidden/pyproject.toml", cell["detail"])

    def test_one_dead_target_still_yields_the_others(self) -> None:
        _server(self.dir / "alive", "alive-mcp")
        rc, data, text = self._run(f"""
            defaults:
              predicates: [manifest]
            targets:
              - repo: o/alive
                path: {self.dir / "alive"}
              - repo: o/dead
                path: {self.dir / "nowhere"}
        """)
        self.assertEqual(rc, ps.EXIT_INCOMPLETE)
        by_name = {t["target"]: t for t in data["targets"]}
        self.assertEqual(
            by_name[str(self.dir / "alive")]["cells"]["manifest"]["status"]
            if str(self.dir / "alive") in by_name
            else by_name["o/alive"]["cells"]["manifest"]["status"],
            ps.OK,
        )
        self.assertEqual(by_name["o/dead"]["cells"]["manifest"]["status"], ps.ERROR)
        self.assertIn("Could not run", text)

    def test_predicate_override_applies_to_every_target(self) -> None:
        _server(self.dir / "one", "one-mcp")
        rc, data, _ = self._run(
            f"""
            defaults:
              predicates: [manifest, sdk_major, settings_write]
            targets:
              - repo: o/one
                path: {self.dir / "one"}
        """,
            "--predicates",
            "manifest",
        )
        self.assertEqual(data["predicates"], ["manifest"])

    def test_only_narrows_the_sweep_and_costs_it_its_verdict(self) -> None:
        """`--only` is how three servers left the tracking without anything
        going red. The run still happens; it just may not call itself green."""
        for name in ("a", "b"):
            _server(self.dir / name, f"{name}-mcp")
        text_targets = f"""
            defaults:
              predicates: [manifest]
            targets:
              - repo: o/a
                path: {self.dir / "a"}
              - repo: o/b
                path: {self.dir / "b"}
        """
        rc, data, text = self._run(text_targets, "--only", "o/a")
        self.assertEqual(rc, ps.EXIT_INCOMPLETE)
        self.assertEqual(data["coverage"]["skipped"], ["o/b"])
        self.assertIn("NO OVERALL VERDICT", text)

        rc, data, text = self._run(text_targets, "--only", "o/a", "--partial")
        self.assertEqual(rc, ps.EXIT_GREEN)
        self.assertIn("PARTIAL (acknowledged", text)
        self.assertEqual(data["coverage"]["skipped"], ["o/b"])

    def test_a_targets_file_shorter_than_expected_is_not_a_sweep(self) -> None:
        _server(self.dir / "a", "a-mcp")
        rc, data, text = self._run(f"""
            expect_targets: 33
            defaults:
              predicates: [manifest]
            targets:
              - repo: o/a
                path: {self.dir / "a"}
        """)
        self.assertEqual(rc, ps.EXIT_INCOMPLETE)
        self.assertEqual(data["coverage"]["expected"], 33)
        self.assertIn("33 were expected", text)

    def test_the_expected_count_can_be_given_on_the_command_line(self) -> None:
        _server(self.dir / "a", "a-mcp")
        rc, data, _ = self._run(
            f"""
            defaults:
              predicates: [manifest]
            targets:
              - repo: o/a
                path: {self.dir / "a"}
        """,
            "--expect-targets",
            "1",
        )
        self.assertEqual(rc, ps.EXIT_GREEN)
        self.assertEqual(data["coverage"]["expected"], 1)
        self.assertTrue(data["coverage"]["complete"])

    def test_an_unreadable_targets_file_is_not_a_green_sweep(self) -> None:
        missing = self.dir / "nope.yaml"
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = ps.main(["--targets", str(missing)])
        self.assertEqual(rc, ps.EXIT_INCOMPLETE)

    def test_list_predicates_names_the_expensive_one(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ps.main(["--list-predicates"])
        self.assertEqual(rc, ps.EXIT_GREEN)
        self.assertIn("boot", out.getvalue())
        self.assertIn("expensive", out.getvalue())

    def test_print_egress_says_github_is_one_entry_not_n(self) -> None:
        _server(self.dir / "e", "e-mcp")
        tf = self.dir / "targets.yaml"
        tf.write_text(
            f"targets:\n  - repo: o/e\n    path: {self.dir / 'e'}\n", encoding="utf-8"
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ps.main(["--targets", str(tf), "--print-egress"])
        self.assertEqual(rc, ps.EXIT_GREEN)
        self.assertIn("github", out.getvalue())
        self.assertIn("ONE entry covers all N", out.getvalue())
        # …and it must not let the reader think the job is done.
        self.assertIn("UPSTREAM", out.getvalue())


if __name__ == "__main__":
    unittest.main()
