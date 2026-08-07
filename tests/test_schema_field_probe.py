#!/usr/bin/env python3
"""Tests for scripts/schema_field_probe.py — does the code read what the source sends?

The probe is an AST read of a checkout plus one GET, so these tests build a real
checkout in a temp dir and inject the GET. Everything that decides a finding —
the manifest, the extraction, the normalisation, the corroboration rule — runs
for real.

The central scenario is the incident: the code reads ``r["Schulgemeinde"]`` and
the source sends ``schulgemeinde``, so nothing raises, the filter matches
nothing, and the caller is told the thing does not exist.

``NotAFindingTest`` pins the direction that would retire the probe. A site whose
keys resolve nothing is a mismatched manifest, not five drifting fields; a
source that cannot be read is not a clean source. Both come out as UNVERIFIED.

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

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "schema_field_probe.py"
EXAMPLE = ROOT / "schema-fields.example.toml"

MANIFEST = """\
[[dataset]]
id = "schulgemeinden"
url = "https://www.bista.zh.ch/export/SCHULGEMEINDEN.csv"
format = "csv"
delimiter = ";"

[[dataset.site]]
file = "src/pkg/datasets.py"
symbol = "search_schulgemeinde"
"""

# The shape of the incident: an old header name, read through `.get`, so the
# miss is silent and the caller gets an empty list with a polite sentence.
SOURCE_DRIFTED = """\
def search_schulgemeinde(rows, name):
    hits = []
    for r in rows:
        if r.get("Schulgemeinde") == name:
            hits.append({"gemeinde": r.get("Schulgemeinde"), "bfs": r["BFS_Nr"]})
    if not hits:
        return "Schulgemeinde nicht gefunden"
    return hits
"""

SOURCE_ALIGNED = SOURCE_DRIFTED.replace("Schulgemeinde", "schulgemeinde").replace(
    "BFS_Nr", "bfs_nr"
)

LIVE_HEADER = b"schulgemeinde;bfs_nr;anzahl\nZuerich;261;1234\n"


def make_target(tmp: Path, *, source: str, manifest: str = MANIFEST) -> Path:
    root = tmp / "target"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "datasets.py").write_text(source, encoding="utf-8")
    (root / sfp.DEFAULT_MANIFEST).write_text(manifest, encoding="utf-8")
    return root


def fetch(body: bytes):
    return lambda url, limit: body[:limit]


class NormaliseTest(unittest.TestCase):
    """The form two spellings of one column share."""

    def test_case_and_separators_collapse(self):
        self.assertEqual(sfp.normalise("Schulgemeinde"), sfp.normalise("schulgemeinde"))
        self.assertEqual(
            sfp.normalise("gebiet_Bezeichnung"), sfp.normalise("gebiet_bezeichnung")
        )
        self.assertEqual(
            sfp.normalise("staatsangehoerigkeit_ISO2_Code"),
            sfp.normalise("staatsangehoerigkeit_iso2_code"),
        )

    def test_different_columns_stay_different(self):
        self.assertNotEqual(sfp.normalise("anzahl"), sfp.normalise("anzahl_total"))


class ExtractionTest(unittest.TestCase):
    """What is read out of the declared symbol, and in which form."""

    def _keys(self, source: str, symbol: str = "search_schulgemeinde", **kw):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source)
            site = sfp.Site(file="src/pkg/datasets.py", symbol=symbol, **kw)
            return sfp.read_site_keys(root, site)

    def test_subscripts_and_get_are_both_collected_and_told_apart(self):
        keys, why = self._keys(SOURCE_DRIFTED)
        self.assertIsNone(why)
        forms = {k.name: k.form for k in keys}
        self.assertEqual(forms["Schulgemeinde"], "get")
        self.assertEqual(forms["BFS_Nr"], "subscript")

    def test_ignore_removes_a_declared_non_field(self):
        keys, _ = self._keys(SOURCE_DRIFTED, ignore=("BFS_Nr",))
        self.assertNotIn("BFS_Nr", {k.name for k in keys})

    def test_a_method_resolves_as_well_as_a_function(self):
        source = (
            "class Client:\n"
            "    def search_schulgemeinde(self, r):\n"
            '        return r["schulgemeinde"]\n'
        )
        keys, why = self._keys(source)
        self.assertIsNone(why)
        self.assertEqual([k.name for k in keys], ["schulgemeinde"])

    def test_a_missing_symbol_is_a_reason_not_an_empty_list(self):
        keys, why = self._keys(SOURCE_DRIFTED, symbol="does_not_exist")
        self.assertEqual(keys, [])
        self.assertIn("has no symbol", why)


class SourceReadingTest(unittest.TestCase):
    """The live half: a header row, or a stated reason there is none."""

    def _dataset(self, fmt="csv", **kw):
        return sfp.Dataset(
            id="d", url="https://example.invalid/x", fmt=fmt, sites=(), **kw
        )

    def test_csv_header_with_a_declared_delimiter(self):
        fields, delimiter = sfp.fields_from_csv(
            LIVE_HEADER, self._dataset(delimiter=";")
        )
        self.assertEqual(fields, ["schulgemeinde", "bfs_nr", "anzahl"])
        self.assertEqual(delimiter, ";")

    def test_the_delimiter_is_sniffed_and_reported(self):
        fields, delimiter = sfp.fields_from_csv(b"a,b,c\n1,2,3\n", self._dataset())
        self.assertEqual(fields, ["a", "b", "c"])
        self.assertEqual(delimiter, ",")

    def test_a_utf8_bom_does_not_become_part_of_the_first_column(self):
        fields, _ = sfp.fields_from_csv(
            "﻿schulgemeinde;bfs_nr\n".encode(), self._dataset(delimiter=";")
        )
        self.assertEqual(fields[0], "schulgemeinde")

    def test_json_records_via_a_declared_path(self):
        body = json.dumps({"result": {"records": [{"a": 1, "b": 2}]}}).encode()
        fields, _ = sfp.fields_from_json(
            body,
            self._dataset(fmt="json", record_path="result.records"),
            truncated=False,
        )
        self.assertEqual(fields, ["a", "b"])

    def test_zero_records_is_not_an_empty_field_list(self):
        """The state that looks exactly like the incident from the outside."""
        body = json.dumps({"result": {"records": []}}).encode()
        with self.assertRaises(sfp.SourceError) as ctx:
            sfp.fields_from_json(body, self._dataset(fmt="json"), truncated=False)
        self.assertIn("zero records", str(ctx.exception))

    def test_a_truncated_body_is_not_evidence(self):
        with self.assertRaises(sfp.SourceError):
            sfp.fields_from_json(b"{", self._dataset(fmt="json"), truncated=True)

    def test_a_header_longer_than_the_window_is_refused(self):
        body = b"x" * sfp.DEFAULT_HEADER_BYTES
        with self.assertRaises(sfp.SourceError) as ctx:
            sfp.fields_from_csv(body, self._dataset())
        self.assertIn("no line break", str(ctx.exception))


class VerdictTest(unittest.TestCase):
    def _probe(self, source: str, body: bytes = LIVE_HEADER, manifest: str = MANIFEST):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source, manifest=manifest)
            return sfp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(body))

    def test_the_incident(self):
        """`r.get("Schulgemeinde")` against a `schulgemeinde` header."""
        report = self._probe(SOURCE_DRIFTED)
        self.assertEqual(report.status, sfp.FIELD_CASE_DRIFT)
        self.assertTrue(report.finding)
        hit = next(h for h in report.hits if h.read == "Schulgemeinde")
        self.assertEqual(hit.status, sfp.FIELD_CASE_DRIFT)
        self.assertEqual(hit.live, "schulgemeinde")
        # The form decides whether a maintainer is looking for a crash or for
        # silence, so it is part of the finding.
        self.assertEqual(hit.form, "get")

    def test_the_aligned_repository_is_clean(self):
        report = self._probe(SOURCE_ALIGNED)
        self.assertEqual(report.status, sfp.OK)
        self.assertFalse(report.finding)
        self.assertEqual(report.hits, [])

    def test_a_dropped_column_is_field_missing(self):
        source = SOURCE_ALIGNED.replace('r["bfs_nr"]', 'r["gemeinde_nr"]')
        report = self._probe(source)
        self.assertEqual(report.status, sfp.FIELD_MISSING)
        hit = next(h for h in report.hits if h.read == "gemeinde_nr")
        self.assertIsNone(hit.live)
        self.assertEqual(hit.form, "subscript")

    def test_field_missing_outranks_case_drift_in_the_verdict(self):
        source = SOURCE_DRIFTED.replace('r["BFS_Nr"]', 'r["gemeinde_nr"]')
        report = self._probe(source)
        self.assertEqual(report.status, sfp.FIELD_MISSING)
        self.assertEqual(len(report.hits), 2)

    def test_a_mixed_case_header_is_noted(self):
        """`gebiet_Bezeichnung` beside lowercase columns — lowercasing is no fix."""
        body = b"gebiet_Bezeichnung;schulgemeinde;anzahl\nx;y;1\n"
        source = 'def search_schulgemeinde(r):\n    return r["schulgemeinde"]\n'
        report = self._probe(source, body=body)
        self.assertEqual(report.status, sfp.OK)
        notes = " ".join(n for r in report.results for n in r.notes)
        self.assertIn(sfp.NOTE_MIXED_CASE, notes)


class FixtureNoteTest(unittest.TestCase):
    """Why the test suite stayed green — a note, never the standard."""

    def test_a_fixture_pinning_the_old_header_is_named(self):
        manifest = MANIFEST.replace(
            'delimiter = ";"',
            'delimiter = ";"\nfixture = "tests/fixtures/schulgemeinden.csv"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_ALIGNED, manifest=manifest)
            fixtures = root / "tests" / "fixtures"
            fixtures.mkdir(parents=True)
            (fixtures / "schulgemeinden.csv").write_bytes(
                b"Schulgemeinde;BFS_Nr;anzahl\nZuerich;261;1\n"
            )
            report = sfp.probe(
                root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(LIVE_HEADER)
            )
        # The code is aligned with the source, so there is no finding …
        self.assertEqual(report.status, sfp.OK)
        # … and the fixture that would have hidden one is still named.
        notes = " ".join(n for r in report.results for n in r.notes)
        self.assertIn(sfp.NOTE_FIXTURE_STALE, notes)
        self.assertIn("Schulgemeinde", notes)


class NotAFindingTest(unittest.TestCase):
    """Everything the probe could not conclude. None of it is clean, none a finding."""

    def test_a_site_that_resolves_nothing_is_a_mismatched_manifest(self):
        """The corroboration rule: zero of N matching is not N findings."""
        source = (
            "def search_schulgemeinde(cfg):\n"
            '    return cfg["timeout"] + cfg["retries"] + cfg["base_url"]\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source)
            report = sfp.probe(
                root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(LIVE_HEADER)
            )
        self.assertEqual(report.status, sfp.UNVERIFIED)
        self.assertEqual(report.hits, [])
        joined = " ".join(u for r in report.results for u in r.unverified)
        self.assertIn("does not appear to read", joined)

    def test_one_resolving_key_is_enough_to_corroborate(self):
        source = (
            "def search_schulgemeinde(r):\n"
            '    return r["schulgemeinde"], r["Schulgemeinde_alt"]\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=source)
            report = sfp.probe(
                root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(LIVE_HEADER)
            )
        self.assertEqual(report.status, sfp.FIELD_MISSING)
        self.assertEqual([h.read for h in report.hits], ["Schulgemeinde_alt"])

    def test_an_unreachable_source_is_not_a_clean_source(self):
        def boom(url, limit):
            raise sfp.SourceError("URLError: connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_ALIGNED)
            report = sfp.probe(root, root / sfp.DEFAULT_MANIFEST, fetch=boom)
        self.assertEqual(report.status, sfp.UNVERIFIED)
        self.assertIn("connection refused", " ".join(report.results[0].unverified))

    def test_a_missing_file_is_named_rather_than_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_ALIGNED)
            (root / "src" / "pkg" / "datasets.py").unlink()
            report = sfp.probe(
                root, root / sfp.DEFAULT_MANIFEST, fetch=fetch(LIVE_HEADER)
            )
        self.assertEqual(report.status, sfp.UNVERIFIED)
        self.assertIn("does not exist", " ".join(report.results[0].unverified))

    def test_no_manifest_stops_the_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(Path(tmp), source=SOURCE_ALIGNED)
            (root / sfp.DEFAULT_MANIFEST).unlink()
            report = sfp.probe(root, root / sfp.DEFAULT_MANIFEST)
        self.assertEqual(report.status, sfp.MANIFEST_MISSING)
        self.assertFalse(report.finding)


class ManifestValidationTest(unittest.TestCase):
    """Fail closed: a manifest that says nothing must not read as nothing to say."""

    def _load(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema_fields.toml"
            path.write_text(body, encoding="utf-8")
            return sfp.load_manifest(path)

    def test_the_shipped_example_parses(self):
        datasets = sfp.load_manifest(EXAMPLE)
        self.assertEqual(
            [d.id for d in datasets], ["schulgemeinden", "lernende_nach_gebiet"]
        )
        self.assertEqual(datasets[0].sites[0].ignore, ("limit", "offset"))
        self.assertEqual(datasets[1].record_path, "result.records")

    def test_an_empty_manifest_is_refused(self):
        with self.assertRaises(sfp.ManifestError):
            self._load("# nothing here\n")

    def test_a_dataset_without_a_site_is_refused(self):
        with self.assertRaises(sfp.ManifestError) as ctx:
            self._load('[[dataset]]\nid = "d"\nurl = "u"\nformat = "csv"\n')
        self.assertIn("never guessed", str(ctx.exception))

    def test_an_unsupported_format_is_refused(self):
        body = (
            '[[dataset]]\nid = "d"\nurl = "u"\nformat = "xml"\n'
            '[[dataset.site]]\nfile = "a.py"\nsymbol = "b"\n'
        )
        with self.assertRaises(sfp.ManifestError):
            self._load(body)


class ExitCodeTest(unittest.TestCase):
    """The contract coverage_run.py reads. It is the interface, so it is pinned."""

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
            root = make_target(Path(tmp), source=SOURCE_ALIGNED)
            (root / sfp.DEFAULT_MANIFEST).unlink()
            proc = self._run(root)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], sfp.MANIFEST_MISSING)

    def test_an_unreadable_manifest_is_a_harness_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_target(
                Path(tmp), source=SOURCE_ALIGNED, manifest="not toml [[["
            )
            proc = self._run(root)
        self.assertEqual(proc.returncode, 127, proc.stdout)


if __name__ == "__main__":
    unittest.main()
