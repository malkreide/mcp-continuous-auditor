#!/usr/bin/env python3
"""Die geteilte Deckungsschicht — die Rechnung, an der ein Portfolio-Lauf haengt.

Jeder Test hier haelt einen Zustand fest, in dem ein Lauf **gruen und falsch**
waere. Das ist der ganze Zweck des Moduls: «Ich habe nicht hingesehen» und «da
war nichts» duerfen sich nicht denselben Exit-Code teilen.

Stdlib-only, kein Netz, kein Git.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import coverage as cov  # noqa: E402


def _write(payload: object) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="coverage-tests-")) / "manifest.json"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    return tmp


class DenominatorTest(unittest.TestCase):
    """DER Regressionstest. Er hat einen eigenen Klassennamen, weil der Fehler
    schon einmal ausgeliefert war."""

    def test_the_total_counts_every_entry_not_just_the_probeable_ones(self) -> None:
        """Zwei pruefbare, eine begruendete Auslassung → Nenner 3, nicht 2.

        Die erste Fassung zaehlte nur die pruefbaren. Ein vollstaendiger,
        gruener Lauf kam damit auf ``2 geprueft + 1 uebersprungen = 3`` gegen
        ein erwartetes ``2`` und meldete die Deckung als unvollstaendig —
        Exit 1 bei lauter gruenen Ergebnissen. Ein Nenner, der von derselben
        Einschaetzung abhaengt, die der Deckungscheck pruefen soll, prueft
        nichts.
        """
        path = _write(
            {
                "servers": [
                    {"id": "a-mcp", "pypi_dist": "a-mcp"},
                    {"id": "b-mcp", "pypi_dist": "b-mcp"},
                    {"id": "nur-github", "pypi_dist": None},
                ]
            }
        )
        total, targets, omissions = cov.read_manifest(
            path, field="pypi_dist", null_reason="kein Paket auf dem Index"
        )
        self.assertEqual(total, 3)
        self.assertEqual(len(targets) + len(omissions), total)

    def test_a_complete_green_run_is_complete_and_exits_zero(self) -> None:
        """Die andere Haelfte desselben Fehlers: die Rechnung selbst."""
        c = cov.build(3, 2, [cov.Omission("nur-github", "kein Paket")])
        self.assertTrue(c.complete, c.render())
        self.assertIn("3/3 abgedeckt", c.covered())
        self.assertIn("vollstaendig", c.covered())

    def test_a_short_run_is_incomplete(self) -> None:
        c = cov.build(3, 2)
        self.assertFalse(c.complete)
        self.assertEqual(cov.EXIT_INCOMPLETE, 1)

    def test_a_target_without_a_result_is_named_and_is_not_coverage(self) -> None:
        """Ein Ziel ohne Ergebnis zaehlt gegen den Nenner und ist keine Deckung."""
        c = cov.build(2, 1, [], ["b-mcp"])
        self.assertFalse(c.complete)
        self.assertIn("b-mcp", c.render())
        self.assertIn("OHNE ERGEBNIS", c.render())

    def test_zero_of_zero_is_never_complete(self) -> None:
        """`0/0 ok` ist die Meldung, gegen die dieses Modul gebaut ist."""
        self.assertFalse(cov.build(0, 0).complete)


class ManifestValidationTest(unittest.TestCase):
    def test_a_missing_section_is_refused(self) -> None:
        """Fehlend ist nicht leer: ein umbenanntes Feld pruefte sonst nichts."""
        path = _write({"eintraege": []})
        with self.assertRaises(SystemExit) as cm:
            cov.read_manifest(path, field="pypi_dist")
        self.assertIn("servers", str(cm.exception))

    def test_an_empty_section_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            cov.read_manifest(_write({"servers": []}), field="pypi_dist")
        self.assertIn("0/0", str(cm.exception))

    def test_an_entry_without_an_id_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            cov.read_manifest(
                _write({"servers": [{"pypi_dist": "a"}]}), field="pypi_dist"
            )

    def test_a_missing_field_is_not_read_as_null(self) -> None:
        """Benennt der Erzeuger das Feld um, waere jeder Eintrag sonst eine
        begruendete Auslassung: nichts gemessen, Deckung vollstaendig, Exit 0."""
        with self.assertRaises(SystemExit) as cm:
            cov.read_manifest(
                _write({"servers": [{"id": "a", "dist_name": "a"}]}),
                field="pypi_dist",
                null_reason="kein Paket",
            )
        self.assertIn("pypi_dist", str(cm.exception))

    def test_a_value_that_is_neither_name_nor_null_is_refused(self) -> None:
        for bad in ("", "   ", 42, []):
            with self.subTest(value=bad), self.assertRaises(SystemExit):
                cov.read_manifest(
                    _write({"servers": [{"id": "a", "pypi_dist": bad}]}),
                    field="pypi_dist",
                    null_reason="kein Paket",
                )

    def test_null_without_a_reason_is_an_error_not_an_omission(self) -> None:
        """Fuer ``repository: null`` gibt es keine Lesart — anders als fuer
        ``pypi_dist: null``. Ohne ``null_reason`` bricht der Lauf ab."""
        with self.assertRaises(SystemExit):
            cov.read_manifest(
                _write({"repositories": [{"id": "a", "repository": None}]}),
                field="repository",
                section=cov.REPOSITORIES,
            )

    def test_null_with_a_reason_is_a_named_omission(self) -> None:
        _, targets, omissions = cov.read_manifest(
            _write({"servers": [{"id": "nur-github", "pypi_dist": None}]}),
            field="pypi_dist",
            null_reason="kein Paket auf dem Index",
        )
        self.assertEqual(targets, [])
        self.assertEqual(omissions[0].name, "nur-github")
        self.assertIn("laut Manifest", omissions[0].reason)

    def test_unreadable_json_is_refused_with_the_path(self) -> None:
        path = Path(tempfile.mkdtemp()) / "broken.json"
        path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            cov.read_manifest(path, field="pypi_dist")
        self.assertIn("broken.json", str(cm.exception))

    def test_a_missing_file_is_refused_rather_than_read_as_empty(self) -> None:
        with self.assertRaises(SystemExit):
            cov.read_manifest(Path("/nonexistent/manifest.json"), field="pypi_dist")


class GithubSlugTest(unittest.TestCase):
    def test_an_https_url_becomes_owner_name(self) -> None:
        _, targets, _ = cov.read_manifest(
            _write(
                {"repositories": [{"id": "a", "repository": "https://github.com/o/a/"}]}
            ),
            field="repository",
            section=cov.REPOSITORIES,
            value_of=cov.github_slug(Path("m.json")),
        )
        self.assertEqual(targets[0].value, "o/a")

    def test_an_ssh_url_is_refused(self) -> None:
        """`git@github.com:o/a.git` hat genau einen Schraegstrich und kaeme
        sonst als Slug durch — um dann bei jedem Repo als HTTP-Fehler zu enden,
        also als 'nicht erhoben' statt als kaputter Manifest-Eintrag."""
        with self.assertRaises(SystemExit):
            cov.read_manifest(
                _write(
                    {
                        "repositories": [
                            {"id": "a", "repository": "git@github.com:o/a.git"}
                        ]
                    }
                ),
                field="repository",
                section=cov.REPOSITORIES,
                value_of=cov.github_slug(Path("m.json")),
            )


class OmitWhenTest(unittest.TestCase):
    def test_archived_is_a_named_omission_and_still_counts(self) -> None:
        total, targets, omissions = cov.read_manifest(
            _write(
                {
                    "repositories": [
                        {"id": "a", "repository": "https://github.com/o/a"},
                        {
                            "id": "z",
                            "repository": "https://github.com/o/z",
                            "archived": True,
                        },
                    ]
                }
            ),
            field="repository",
            section=cov.REPOSITORIES,
            value_of=cov.github_slug(Path("m.json")),
            omit_when=lambda raw: "archiviert" if raw.get("archived") else None,
        )
        self.assertEqual(total, 2)
        self.assertEqual([e.value for e in targets], ["o/a"])
        self.assertEqual(omissions[0].name, "o/z")
        self.assertTrue(omissions[0].reason)


class AllowSkipTest(unittest.TestCase):
    def test_the_reason_is_mandatory(self) -> None:
        for bad in ("a-mcp", "a-mcp:   ", ":grund"):
            with self.subTest(arg=bad), self.assertRaises(SystemExit):
                cov.parse_allow_skip([bad])

    def test_a_colon_in_the_reason_survives(self) -> None:
        self.assertEqual(
            cov.parse_allow_skip(["x:siehe https://example.org/a"]),
            {"x": "siehe https://example.org/a"},
        )

    def test_a_skip_may_name_the_id_or_the_resolved_value(self) -> None:
        entries = [cov.Entry(id="a-mcp", value="o/a-mcp", raw={})]
        kept, skipped = cov.split_allowed(entries, {"a-mcp": "Umbau"})
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0].name, "o/a-mcp")

        kept, skipped = cov.split_allowed(entries, {"o/a-mcp": "Umbau"})
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0].reason, "Umbau")

    def test_a_skip_that_skips_nothing_is_reported(self) -> None:
        """Ein Tippfehler im Skip bewirkt nichts und sieht wie eine
        Entscheidung aus. Der gefaehrliche Fall ist der umbenannte Eintrag."""
        entries = [cov.Entry(id="a-mcp", value="o/a-mcp", raw={})]
        self.assertEqual(
            cov.unknown_skips({"b-mcp": "weg"}, entries, []),
            ["b-mcp"],
        )
        self.assertEqual(cov.unknown_skips({"a-mcp": "weg"}, entries, []), [])

    def test_a_skip_may_name_a_manifest_omission(self) -> None:
        self.assertEqual(
            cov.unknown_skips(
                {"nur-github": "x"}, [], [cov.Omission("nur-github", "")]
            ),
            [],
        )


class RenderTest(unittest.TestCase):
    def test_the_numerator_never_appears_without_the_denominator(self) -> None:
        c = cov.build(44, 41, [cov.Omission("x", "kein Paket")], ["y", "z"])
        line = c.render()
        self.assertIn("41/44", line)
        self.assertIn("x (kein Paket)", line)
        self.assertIn("y, z", line)
        self.assertIn("42/44 abgedeckt", c.covered())
        self.assertIn("UNVOLLSTAENDIG", c.covered())

    def test_the_json_block_carries_both_halves(self) -> None:
        d = cov.build(2, 1, [cov.Omission("x", "grund")]).as_dict()
        self.assertEqual(d["expected"], 2)
        self.assertEqual(d["probed"], 1)
        self.assertEqual(d["skipped"], [{"name": "x", "reason": "grund"}])
        self.assertTrue(d["complete"])


if __name__ == "__main__":
    unittest.main()
