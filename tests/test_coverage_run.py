#!/usr/bin/env python3
"""Der Treiber, der eine Sonde ueber das ganze Manifest fuehrt.

Die Gegenprobe des Auftrags steht hier: ein absichtlich fehlender Eintrag muss
Exit != 0 liefern UND den Namen nennen; ein begruendeter Skip muss Exit 0
liefern und den Grund ausgeben; ein leeres Manifest muss abbrechen statt
«0/0 ok» zu melden.

Keine echte Sonde wird gestartet — der Runner ist eine Naht, die hier ersetzt
wird. Stdlib-only, kein Netz, kein Git.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import coverage as cov  # noqa: E402
import coverage_run as cr  # noqa: E402


def _manifest(payload: object) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="coverage-run-tests-")) / "manifest.json"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    return tmp


def _repos(*names: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="coverage-run-repos-"))
    for n in names:
        (root / n).mkdir()
    return root


def _servers(*ids: str) -> dict:
    return {"servers": [{"id": i, "pypi_dist": i} for i in ids]}


def _repositories(*ids: str) -> dict:
    return {
        "repositories": [
            {"id": i, "repository": f"https://github.com/o/{i}", "archived": False}
            for i in ids
        ]
    }


def _runner(codes: dict[str, int], default: int = 0):
    """Ersetzt ``run_probe``: liefert je Ziel einen festen Exit-Code."""

    def run(spec, target, extra, timeout):  # noqa: ANN001
        key = Path(target).name
        return codes.get(key, codes.get(target, default)), f"stub:{key}"

    return run


class SkipTest(unittest.TestCase):
    def test_a_reasoned_skip_is_green_and_prints_the_reason(self) -> None:
        """Gegenprobe 2: begruendeter Skip → Exit 0, Grund in der Ausgabe."""
        run = cr.sweep(
            cr.PROBES["yank"],
            _manifest(_servers("a-mcp", "b-mcp")),
            repos_root=None,
            allow_skip={"b-mcp": "upstream down, Ticket #12"},
            extra=[],
            timeout=1,
            runner=_runner({}),
        )
        self.assertEqual(run.exit_code(), 0)
        self.assertTrue(run.coverage().complete)
        self.assertIn("upstream down, Ticket #12", run.render())
        self.assertIn("2/2 abgedeckt", run.render())

    def test_a_skip_for_a_name_the_manifest_does_not_know_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            cr.sweep(
                cr.PROBES["yank"],
                _manifest(_servers("a-mcp")),
                repos_root=None,
                allow_skip={"tippfehler-mcp": "grund"},
                extra=[],
                timeout=1,
                runner=_runner({}),
            )
        self.assertIn("tippfehler-mcp", str(cm.exception))

    def test_a_manifest_omission_counts_against_the_denominator(self) -> None:
        run = cr.sweep(
            cr.PROBES["yank"],
            _manifest(
                {
                    "servers": [
                        {"id": "a-mcp", "pypi_dist": "a-mcp"},
                        {"id": "nur-github", "pypi_dist": None},
                    ]
                }
            ),
            repos_root=None,
            allow_skip={},
            extra=[],
            timeout=1,
            runner=_runner({}),
        )
        self.assertEqual(run.coverage().expected, 2)
        self.assertTrue(run.coverage().complete)
        self.assertEqual(run.exit_code(), 0)


class EmptyManifestTest(unittest.TestCase):
    def test_an_empty_manifest_aborts_rather_than_reporting_zero_of_zero(self) -> None:
        """Gegenprobe 3. `0/0 ok` waere von einem gepruefen Portfolio nicht zu
        unterscheiden — genau die Verwechslung, gegen die es hier geht."""
        with self.assertRaises(SystemExit):
            cr.sweep(
                cr.PROBES["yank"],
                _manifest({"servers": []}),
                repos_root=None,
                allow_skip={},
                extra=[],
                timeout=1,
                runner=_runner({}),
            )


class MissingCheckoutTest(unittest.TestCase):
    def test_a_target_without_a_checkout_is_named_and_fails_the_run(self) -> None:
        """Gegenprobe 1: ein absichtlich fehlender Eintrag → Exit != 0 UND Name.

        Der Fehlbefund von 2026-07-31 in seiner haeufigsten Gestalt: das Ziel
        ist im Manifest, der Checkout fehlt, und ein Lauf ohne diese Kategorie
        laesst es einfach aus der Liste fallen.
        """
        run = cr.sweep(
            cr.PROBES["identity"],
            _manifest(_repositories("a-mcp", "fehlt-mcp")),
            repos_root=_repos("a-mcp"),
            allow_skip={},
            extra=[],
            timeout=1,
            runner=_runner({}),
        )
        self.assertEqual(run.exit_code(), cov.EXIT_INCOMPLETE)
        self.assertFalse(run.coverage().complete)
        self.assertIn("fehlt-mcp", run.render())
        self.assertIn("fehlt-mcp", run.coverage().render())

    def test_the_incomplete_run_never_reports_green_even_with_all_green(self) -> None:
        run = cr.sweep(
            cr.PROBES["identity"],
            _manifest(_repositories("a-mcp", "fehlt-mcp")),
            repos_root=_repos("a-mcp"),
            allow_skip={},
            extra=[],
            timeout=1,
            runner=_runner({"a-mcp": 0}),
        )
        self.assertNotEqual(run.exit_code(), 0)


class ClassifyTest(unittest.TestCase):
    def test_the_standard_contract(self) -> None:
        spec = cr.PROBES["spec"]
        self.assertEqual(spec.classify(0), cr.GREEN)
        self.assertEqual(spec.classify(2), cr.FINDINGS)
        self.assertEqual(spec.classify(3), cr.NOT_MEASURED)
        self.assertEqual(spec.classify(4), cr.NOT_MEASURED)
        self.assertEqual(spec.classify(127), cr.NOT_MEASURED)

    def test_identity_carries_its_own_contract(self) -> None:
        """identity_probe meldet den Befund mit 1, und 2 heisst 'kein
        pyproject.toml' — also ein Ziel, das diese Sonde nicht messen kann."""
        spec = cr.PROBES["identity"]
        self.assertEqual(spec.classify(1), cr.FINDINGS)
        self.assertEqual(spec.classify(2), cr.NOT_MEASURED)

    def test_an_unknown_exit_code_is_not_a_finding(self) -> None:
        """124/137 (Timeout / SIGKILL) haengen dem Ziel sonst einen Mangel an,
        den niemand gemessen hat."""
        for rc in (124, 137, 42):
            with self.subTest(rc=rc):
                self.assertEqual(cr.PROBES["spec"].classify(rc), cr.NOT_MEASURED)

    def test_not_measured_is_its_own_exit_code_not_green(self) -> None:
        run = cr.sweep(
            cr.PROBES["yank"],
            _manifest(_servers("a-mcp")),
            repos_root=None,
            allow_skip={},
            extra=[],
            timeout=1,
            runner=_runner({"a-mcp": 127}),
        )
        self.assertTrue(run.coverage().complete, "die Sonde LIEF — das ist Deckung")
        self.assertEqual(run.exit_code(), cr.EXIT_NOT_MEASURED)

    def test_findings_outrank_not_measured(self) -> None:
        run = cr.sweep(
            cr.PROBES["yank"],
            _manifest(_servers("a-mcp", "b-mcp")),
            repos_root=None,
            allow_skip={},
            extra=[],
            timeout=1,
            runner=_runner({"a-mcp": 2, "b-mcp": 3}),
        )
        self.assertEqual(run.exit_code(), cr.EXIT_FINDINGS)

    def test_incomplete_coverage_outranks_findings(self) -> None:
        """Ein Lauf, der nicht ueberall hingesehen hat, hat keinen Befund
        erhoben, sondern gar nichts."""
        run = cr.sweep(
            cr.PROBES["identity"],
            _manifest(_repositories("a-mcp", "fehlt-mcp")),
            repos_root=_repos("a-mcp"),
            allow_skip={},
            extra=[],
            timeout=1,
            runner=_runner({"a-mcp": 1}),
        )
        self.assertEqual(run.exit_code(), cov.EXIT_INCOMPLETE)


class EnvShapedProbeTest(unittest.TestCase):
    def test_the_boot_probe_gets_its_target_through_the_environment(self) -> None:
        """Der Unterschied, den ein Treiber einmal uebersetzt statt elfmal."""
        spec = cr.PROBES["boot"]
        self.assertEqual(spec.env_of("/tmp/x"), {"BOOT_TARGET_ROOT": "/tmp/x"})
        self.assertNotIn("--target", spec.argv_of("/tmp/x"))

    def test_a_target_shaped_probe_gets_it_as_an_argument(self) -> None:
        argv = cr.PROBES["identity"].argv_of("/tmp/x")
        self.assertIn("--target", argv)
        self.assertEqual(cr.PROBES["identity"].env_of("/tmp/x"), {})


class CliTest(unittest.TestCase):
    def _main(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = cr.main(list(argv))
        except SystemExit as exc:
            rc = int(exc.code or 0)
            buf.write(str(exc))
        return rc, buf.getvalue()

    def test_a_checkout_probe_without_repos_root_cannot_run(self) -> None:
        rc, out = self._main(
            "--probe", "identity", "--manifest", str(_manifest(_repositories("a")))
        )
        self.assertEqual(rc, cr.EXIT_CANNOT_RUN)
        self.assertIn("--repos-root", out)

    def test_json_carries_the_coverage_block_and_every_target(self) -> None:
        orig = cr.run_probe
        cr.run_probe = _runner({"a-mcp": 0, "b-mcp": 2})  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(cr, "run_probe", orig))
        rc, out = self._main(
            "--probe",
            "yank",
            "--manifest",
            str(_manifest(_servers("a-mcp", "b-mcp"))),
            "--format",
            "json",
        )
        payload = json.loads(out)
        self.assertEqual(rc, cr.EXIT_FINDINGS)
        self.assertEqual(payload["coverage"]["expected"], 2)
        self.assertTrue(payload["coverage"]["complete"])
        self.assertEqual(
            {t["status"] for t in payload["targets"]}, {"green", "findings"}
        )


if __name__ == "__main__":
    unittest.main()
