#!/usr/bin/env python3
"""Tests for scripts/value_domain_probe.py — does a coerced column hold numbers?

The probe is an AST read plus one GET, so these tests build a real checkout in a
temp dir and inject the GET. The manifest, the extraction, the classification and
the truncation rule all run for real.

The central scenario is the incident: the code calls ``int()`` on ``anzahl``, and
for small case counts the source publishes ``"1 bis 5"`` instead of a number, so
the caller gets «unerwarteter interner Fehler» once in five requests.

``TruncationRuleTest`` pins the rule that decides what *clean* is allowed to
mean. A capped read that found nothing has not established that there is nothing;
the suppressed rows cluster exactly where nobody looked.

Stdlib-only and offline — the network seam is a callable, never a socket.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import schema_field_probe as sfp  # noqa: E402
import value_domain_probe as vdp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "value_domain_probe.py"

MANIFEST = """\
[[dataset]]
id = "lernende"
url = "https://www.bista.zh.ch/export/LERNENDE.csv"
format = "csv"
delimiter = ";"

[[dataset.site]]
file = "src/pkg/datasets.py"
symbol = "aggregate"
"""

SOURCE_DIRECT = """\
def aggregate(rows):
    total = 0
    for r in rows:
        total += int(r["anzahl"])
    return total
"""

SOURCE_NAME_BOUND = """\
def aggregate(rows):
    total = 0
    for r in rows:
        raw = r["anzahl"]
        total += int(raw)
    return total
"""

SOURCE_GUARDED = """\
def aggregate(rows):
    total = 0
    for r in rows:
        try:
            total += int(r["anzahl"])
        except ValueError:
            continue
    return total
"""

SOURCE_NO_COERCION = """\
def aggregate(rows):
    return [r["anzahl"] for r in rows]
"""

# The source: eight rows, of which two carry the privacy suppression string and
# one is empty. 3/8 = 37.5 % — the same shape as the 18.6 % in the field data,
# small enough to assert on exactly.
LIVE_CSV = (
    b"gemeinde;anzahl\n"
    b"Zuerich;1200\n"
    b"Winterthur;800\n"
    b"Adlikon;1 bis 5\n"
    b"Bachs;1 bis 5\n"
    b"Buch;42\n"
    b"Dachsen;\n"
    b"Elgg;17\n"
    b"Fehraltorf;9\n"
)
CLEAN_CSV = (
    b"gemeinde;anzahl\nZuerich;1200\nWinterthur;800\nBuch;42\nElgg;17\nFehraltorf;9\n"
)


def make_target(tmp: Path, *, source: str, manifest: str = MANIFEST) -> Path:
    root = tmp / "target"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "datasets.py").write_text(source, encoding="utf-8")
    (root / sfp.DEFAULT_MANIFEST).write_text(manifest, encoding="utf-8")
    return root


def fetch(body: bytes):
    return lambda url, limit: body[:limit]


class ClassifyTest(unittest.TestCase):
    """One cell at a time. `integer` and `fractional` are apart on purpose."""

    def test_the_privacy_suppression_string(self):
        self.assertEqual(vdp.classify("1 bis 5"), vdp.NON_NUMERIC)

    def test_nulls_and_blanks_are_told_apart(self):
        self.assertEqual(vdp.classify(""), vdp.EMPTY)
        self.assertEqual(vdp.classify("   "), vdp.EMPTY)
        self.assertEqual(vdp.classify("NULL"), vdp.NULL_LITERAL)
        self.assertEqual(vdp.classify(None), vdp.NULL_LITERAL)
        self.assertEqual(vdp.classify("n/a"), vdp.NULL_LITERAL)

    def test_swiss_formatting_is_still_a_number(self):
        """`1'234` read as prose would drown the real finding in noise."""
        for value in ("1'234", "1 234", "1234"):
            with self.subTest(value=value):
                self.assertEqual(vdp.classify(value), vdp.INTEGER)

    def test_a_fraction_is_a_number_but_not_an_integer(self):
        self.assertEqual(vdp.classify("12.5"), vdp.FRACTIONAL)
        self.assertEqual(vdp.classify("12,5"), vdp.FRACTIONAL)
        self.assertEqual(vdp.classify(12.5), vdp.FRACTIONAL)
        self.assertEqual(vdp.classify(12), vdp.INTEGER)

    def test_a_bool_is_not_a_measurement(self):
        self.assertEqual(vdp.classify(True), vdp.NON_NUMERIC)


class CoercionExtractionTest(unittest.TestCase):
    """Which columns the code turns into numbers, and how it was written."""

    def _find(self, source: str, symbol: str = "aggregate", **kw):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source)
            site = sfp.Site(file="src/pkg/datasets.py", symbol=symbol, **kw)
            return vdp.find_coercions(root, site)

    def test_both_spellings_are_accepted(self):
        """They are mutually exclusive ways of writing one thing."""
        for source, shape in (
            (SOURCE_DIRECT, "direct"),
            (SOURCE_NAME_BOUND, "name-bound"),
        ):
            with self.subTest(shape=shape):
                found, why = self._find(source)
                self.assertIsNone(why)
                self.assertEqual([c.column for c in found], ["anzahl"])
                self.assertEqual(found[0].shape, shape)
                self.assertFalse(found[0].guarded)

    def test_a_wrapped_argument_resolves_when_it_is_unambiguous(self):
        found, _ = self._find('def aggregate(r):\n    return int(r["anzahl"] or 0)\n')
        self.assertEqual([c.column for c in found], ["anzahl"])
        self.assertEqual(found[0].shape, "wrapped")

    def test_two_columns_in_one_argument_are_not_attributed(self):
        found, _ = self._find('def aggregate(r):\n    return int(r["a"] + r["b"])\n')
        self.assertEqual(found, [])

    def test_int_around_float_is_attributed_to_the_inner_call_only(self):
        """The inner coercion is the one that meets the raw string."""
        found, _ = self._find('def aggregate(r):\n    return int(float(r["anzahl"]))\n')
        self.assertEqual([(c.column, c.func) for c in found], [("anzahl", "float")])

    def test_a_guarded_call_is_recognised(self):
        found, _ = self._find(SOURCE_GUARDED)
        self.assertTrue(found[0].guarded)

    def test_a_try_catching_something_else_is_not_a_guard(self):
        source = SOURCE_GUARDED.replace("except ValueError:", "except KeyError:")
        found, _ = self._find(source)
        self.assertFalse(found[0].guarded)

    def test_ignore_removes_a_declared_non_column(self):
        found, _ = self._find(SOURCE_DIRECT, ignore=("anzahl",))
        self.assertEqual(found, [])


class VerdictTest(unittest.TestCase):
    def _probe(self, source: str, body: bytes = LIVE_CSV, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source)
            return vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(body), **kw)

    def test_the_incident(self):
        report = self._probe(SOURCE_DIRECT)
        self.assertEqual(report.status, vdp.DRIFT)
        self.assertTrue(report.finding)
        reading = report.results[0].readings[0]
        self.assertEqual(reading.total, 8)
        self.assertEqual(reading.counts[vdp.NON_NUMERIC], 2)
        self.assertEqual(reading.counts[vdp.EMPTY], 1)
        self.assertEqual(reading.offending, 3)
        self.assertAlmostEqual(reading.share, 0.375)

    def test_the_share_reaches_the_rendered_report(self):
        """«sometimes not a number» and «one in five» call for different urgency."""
        report = self._probe(SOURCE_DIRECT)
        self.assertIn("37.5%", vdp.render(report))
        self.assertIn("non_numeric=2", vdp.render(report))

    def test_a_numeric_column_read_to_the_end_is_clean(self):
        report = self._probe(SOURCE_DIRECT, body=CLEAN_CSV)
        self.assertEqual(report.status, vdp.OK)
        self.assertFalse(report.finding)
        self.assertFalse(report.results[0].truncated)

    def test_a_fraction_offends_int_and_not_float(self):
        body = b"gemeinde;anzahl\nZuerich;12.5\nBuch;3\n"
        self.assertEqual(self._probe(SOURCE_DIRECT, body=body).status, vdp.DRIFT)
        as_float = SOURCE_DIRECT.replace("int(", "float(")
        self.assertEqual(self._probe(as_float, body=body).status, vdp.OK)

    def test_a_fully_guarded_column_is_reported_but_is_not_a_finding(self):
        """The code answered this. A gate that reddens on handled code is switched off."""
        report = self._probe(SOURCE_GUARDED)
        self.assertEqual(report.status, vdp.HANDLED)
        self.assertFalse(report.finding)
        reading = report.results[0].readings[0]
        # Still measured, still printed: 18.6 % is a fact about the source
        # whether or not the caller crashes on it.
        self.assertEqual(reading.offending, 3)
        self.assertFalse(reading.exposed)
        self.assertIn(vdp.NOTE_GUARDED, " ".join(report.results[0].notes))

    def test_one_unguarded_call_site_is_enough_to_make_it_a_finding(self):
        source = SOURCE_GUARDED + '\n\ndef also(r):\n    return int(r["anzahl"])\n'
        manifest = MANIFEST + (
            '\n[[dataset.site]]\nfile = "src/pkg/datasets.py"\nsymbol = "also"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source, manifest=manifest)
            report = vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(LIVE_CSV))
        self.assertEqual(report.status, vdp.DRIFT)
        self.assertTrue(report.results[0].readings[0].exposed)

    def test_a_column_name_that_drifted_is_measured_and_handed_on(self):
        body = b"gemeinde;Anzahl\nZuerich;1 bis 5\n"
        report = self._probe(SOURCE_DIRECT, body=body)
        self.assertEqual(report.status, vdp.DRIFT)
        notes = " ".join(report.results[0].notes)
        self.assertIn(vdp.NOTE_NAME_DRIFT, notes)
        self.assertIn("schema_field_probe", notes)


class DeclaredCoercerTest(unittest.TestCase):
    """A project that wraps its coercion in one helper — the good pattern.

    `zh-education-mcp` fixed the `"1 bis 5"` incident with `_parse_count`, so
    `int()` no longer appears at any call site and the column that produced the
    incident became invisible to this probe. The helper is declared by name.
    """

    SOURCE = """\
def _parse_count(value):
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() else None


def aggregate(rows):
    return [_parse_count(r.get("anzahl")) for r in rows]
"""

    def _probe(self, manifest: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=self.SOURCE, manifest=manifest)
            return vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(LIVE_CSV))

    def test_undeclared_the_column_is_not_measured_at_all(self):
        report = self._probe(MANIFEST)
        self.assertEqual(report.status, vdp.UNVERIFIED)
        self.assertIn(vdp.NO_COERCION, " ".join(report.results[0].unverified))

    def test_declared_the_share_is_measured(self):
        manifest = MANIFEST + (
            '\n[[dataset.coercer]]\nname = "_parse_count"\ntolerant = true\n'
        )
        report = self._probe(manifest)
        reading = report.results[0].readings[0]
        self.assertEqual(reading.column, "anzahl")
        self.assertEqual(reading.offending, 3)
        self.assertAlmostEqual(reading.share, 0.375)

    def test_a_tolerant_helper_guards_its_call_sites(self):
        manifest = MANIFEST + (
            '\n[[dataset.coercer]]\nname = "_parse_count"\ntolerant = true\n'
        )
        report = self._probe(manifest)
        self.assertEqual(report.status, vdp.HANDLED)
        self.assertFalse(report.finding)

    def test_a_helper_not_declared_tolerant_still_exposes_the_caller(self):
        manifest = MANIFEST + '\n[[dataset.coercer]]\nname = "_parse_count"\n'
        report = self._probe(manifest)
        self.assertEqual(report.status, vdp.DRIFT)

    def test_a_coercer_without_a_name_is_refused(self):
        manifest = MANIFEST + "\n[[dataset.coercer]]\ntolerant = true\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema_fields.toml"
            path.write_text(manifest, encoding="utf-8")
            with self.assertRaises(sfp.ManifestError):
                sfp.load_manifest(path)


class TruncationRuleTest(unittest.TestCase):
    """What a capped read is allowed to conclude."""

    def _probe(self, body: bytes, max_bytes: int, source: str = SOURCE_DIRECT):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source)
            return vdp.probe(
                root,
                root / sfp.DEFAULT_MANIFEST,
                fetch=fetch(body),
                max_bytes=max_bytes,
            )

    def test_a_capped_read_that_found_nothing_is_not_clean(self):
        report = self._probe(CLEAN_CSV, max_bytes=40)
        self.assertEqual(report.status, vdp.UNVERIFIED)
        self.assertTrue(report.results[0].truncated)
        self.assertIn("CAPPED", vdp.render(report))

    def test_a_capped_read_that_found_something_is_still_a_finding(self):
        """A share over 50 000 rows is a measurement, not half an opinion."""
        report = self._probe(LIVE_CSV, max_bytes=70)
        self.assertEqual(report.status, vdp.DRIFT)
        self.assertTrue(report.results[0].truncated)

    def test_a_half_row_is_dropped_rather_than_classified(self):
        """A row cut by the cap is not a domain violation the source committed."""
        body = b"gemeinde;anzahl\nZuerich;1200\nWinterthur;800\n"
        # The cap lands mid-row, which is where it almost always lands.
        report = self._probe(body, max_bytes=len(body) - 3)
        self.assertTrue(report.results[0].truncated)
        self.assertEqual(report.results[0].rows_read, 1)
        # And the surviving row is the whole one, not `80`.
        self.assertEqual(report.results[0].readings[0].counts, {vdp.INTEGER: 1})


class NormalisationTest(unittest.TestCase):
    """The declared key transformation, shared with schema_field_probe."""

    TITLE_CASE = b"Gemeinde;Anzahl\nZuerich;1 bis 5\nBuch;42\n"
    NORMALISED_MANIFEST = MANIFEST.replace(
        'delimiter = ";"', 'delimiter = ";"\nnormalised = "lower"'
    )

    def _probe(self, manifest: str, body: bytes = TITLE_CASE):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_DIRECT, manifest=manifest)
            return vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(body))

    def test_the_values_are_measured_through_the_transformation(self):
        report = self._probe(self.NORMALISED_MANIFEST)
        self.assertEqual(report.status, vdp.DRIFT)
        reading = report.results[0].readings[0]
        self.assertEqual(reading.live_name, "anzahl")
        self.assertEqual(reading.counts[vdp.NON_NUMERIC], 1)

    def test_no_spurious_name_drift_note_when_it_is_declared(self):
        """Without this, every column of every Title-Case dataset gets a note."""
        report = self._probe(self.NORMALISED_MANIFEST)
        self.assertNotIn(vdp.NOTE_NAME_DRIFT, " ".join(report.results[0].notes))

    def test_undeclared_it_still_measures_and_still_says_so(self):
        report = self._probe(MANIFEST)
        self.assertEqual(report.status, vdp.DRIFT)
        self.assertIn(vdp.NOTE_NAME_DRIFT, " ".join(report.results[0].notes))


class NotAFindingTest(unittest.TestCase):
    def _probe(self, source: str, body: bytes = LIVE_CSV, manifest: str = MANIFEST):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source, manifest=manifest)
            return vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(body))

    def test_no_coercion_means_nothing_was_measured(self):
        report = self._probe(SOURCE_NO_COERCION)
        self.assertEqual(report.status, vdp.UNVERIFIED)
        self.assertIn(vdp.NO_COERCION, " ".join(report.results[0].unverified))

    def test_a_coerced_column_that_is_not_in_the_response(self):
        body = b"gemeinde;total\nZuerich;12\n"
        report = self._probe(SOURCE_DIRECT, body=body)
        self.assertEqual(report.status, vdp.UNVERIFIED)
        self.assertIn("schema_field_probe", " ".join(report.results[0].unverified))

    def test_an_unreachable_source_is_not_a_clean_source(self):
        def boom(url, limit):
            raise sfp.SourceError("URLError: connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_DIRECT)
            report = vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=boom)
        self.assertEqual(report.status, vdp.UNVERIFIED)

    def test_no_manifest_stops_the_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_DIRECT)
            (root / sfp.DEFAULT_MANIFEST).unlink()
            report = vdp.probe(root, root / sfp.DEFAULT_MANIFEST)
        self.assertEqual(report.status, vdp.MANIFEST_MISSING)
        self.assertFalse(report.finding)


class JsonSourceTest(unittest.TestCase):
    MANIFEST_JSON = """\
[[dataset]]
id = "lernende"
url = "https://www.bista.zh.ch/api/records"
format = "json"
record_path = "result.records"

[[dataset.site]]
file = "src/pkg/datasets.py"
symbol = "aggregate"
"""

    def test_records_are_classified_like_csv_cells(self):
        body = json.dumps(
            {"result": {"records": [{"anzahl": 12}, {"anzahl": "1 bis 5"}]}}
        ).encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp), source=SOURCE_DIRECT, manifest=self.MANIFEST_JSON
            )
            report = vdp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(body))
        self.assertEqual(report.status, vdp.DRIFT)
        self.assertAlmostEqual(report.results[0].readings[0].share, 0.5)

    def test_a_truncated_json_body_cannot_be_parsed_from_a_prefix(self):
        body = json.dumps({"result": {"records": [{"anzahl": 12}]}}).encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp), source=SOURCE_DIRECT, manifest=self.MANIFEST_JSON
            )
            report = vdp.probe(
                root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(body), max_bytes=10
            )
        self.assertEqual(report.status, vdp.UNVERIFIED)


class ExitCodeTest(unittest.TestCase):
    """The contract coverage_run.py reads."""

    def _run(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--target", str(root), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_missing_target_is_a_harness_failure(self):
        self.assertEqual(self._run(Path("/nonexistent/target")).returncode, 127)

    def test_no_manifest_exits_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_DIRECT)
            (root / sfp.DEFAULT_MANIFEST).unlink()
            proc = self._run(root)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], vdp.MANIFEST_MISSING)

    def test_an_unreadable_manifest_is_a_harness_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_DIRECT, manifest="not toml [[[")
            self.assertEqual(self._run(root).returncode, 127)


if __name__ == "__main__":
    unittest.main()
