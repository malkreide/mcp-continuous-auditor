#!/usr/bin/env python3
"""Value-domain probe — does a column the code coerces actually hold numbers?

THE INCIDENT
------------
``zh-education-mcp`` against ``www.bista.zh.ch``, 2026-08-03. The code called
``int()`` on the column ``anzahl``. For small case counts the source does not
publish a number: to protect the individuals behind them it publishes the string
**``"1 bis 5"``**. Beside that, ``"NULL"`` and empty cells.

``int("1 bis 5")`` raises. The caller of the tool saw «unerwarteter interner
Fehler» and nothing else — no column, no value, no clue that the source was
doing something deliberate and documented.

The shares are not a rounding error, which is why this is worth measuring rather
than fixing once and forgetting:

===============  ==========  ==============
 rows in the extract  affected  share
===============  ==========  ==============
 13 902           2 586       **18.6 %**
 62 684           11 346      **18.1 %**
 35 903             359        **1.0 %**
===============  ==========  ==============

A one-in-five chance of a crash is not an edge case. It is the normal behaviour
of the endpoint, and no test in the repository could see it: the fixtures carry
the rows somebody chose while writing the fixture, and nobody chooses the
suppressed ones.

WHAT IS MEASURED
----------------
Two halves, the same shape as ``schema_field_probe`` — which this file reuses
outright for the manifest, the fetch and the record extraction, because the
question is asked of the same datasets through the same declared sites, and a
second copy of that code would be a second place for it to be subtly wrong.

**From the code:** which columns are handed to ``int()`` or ``float()`` inside a
declared symbol. Both spellings are accepted, because they are mutually
exclusive ways of writing one thing and a check that takes only the first is
measuring who wrote the line rather than what it does::

    total = int(row["anzahl"])          # direct
    raw = row["anzahl"]                 # name-bound
    total = int(raw)

**From the source:** every value in that column, classified — parses as a
number, empty, a null literal (``NULL``, ``NA``, ``-``, …), or **non-numeric
text**, which is the ``"1 bis 5"`` case. The finding carries the measured share
of each, because «this column sometimes is not a number» and «one value in five
is not a number» call for different urgency and the second is what was actually
true.

THE TRUNCATION RULE
-------------------
Reading the whole of a 60 MB extract to count strings would make this check
expensive enough that somebody switches it off, so the read is capped. That cap
decides what a *clean* result is allowed to mean:

* values outside the domain found ⇒ **finding**, whether or not the read was
  capped. A share measured over 50 000 rows is a measurement, and the report
  says how many rows it is over.
* none found and the response was read **to the end** ⇒ clean.
* none found and the read was **capped** ⇒ ``UNVERIFIED``. The tail was not
  read, and the suppressed rows are exactly the ones that cluster where nobody
  looked. Reporting that as clean is the mistake this whole directory exists to
  prevent.

A partial final line is dropped before parsing. A row cut in half by the byte
cap would otherwise be classified as a domain violation the source never
committed.

WHAT IS DELIBERATELY NOT CLAIMED
--------------------------------
1. **A column whose every coercion is guarded is not a finding.** Where the
   call sits inside a ``try`` that catches the failure, or goes through a
   helper the manifest declares ``tolerant``, the code has answered the
   question this probe asks. The status is ``VALUE_DOMAIN_HANDLED`` — exit 0,
   with the share still measured and printed, because 18.6 % is a fact about
   the source either way. A gate that reddens on correctly handled code is
   switched off within a week, and takes the unguarded columns with it.

   **One** unguarded call site is enough to make the column a finding again.
   Whether the absorbed rows are visible in what the tool finally reports is a
   real question and a different one; this probe does not answer it.
2. **Whether the column NAME is right** belongs to ``schema_field_probe``. When
   a coerced column does not resolve against the live header, this run reports
   that it could not measure the domain and points at the other probe, rather
   than inventing a second opinion about a name.
3. **A share is about the response that was fetched**, not about the dataset for
   all time. The URL, the row count and the truncation state are printed with
   every number so a reader can re-fetch exactly what was measured.

EXIT CODES
  0    VALUE_DOMAIN_OK — every coerced column is numeric throughout a full read
  0    VALUE_DOMAIN_HANDLED — values outside the domain, every coercion guarded
  2    FINDING — VALUE_DOMAIN_DRIFT
  3    NOT MEASURED — no manifest, no coercion found, source unreadable, a
       capped read with nothing found, or a column that does not resolve
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run (no target, or a manifest that does not parse)

Usage:
  python scripts/value_domain_probe.py --target ../zh-education-mcp
  python scripts/value_domain_probe.py --target . --dataset lernende --format json
  python scripts/value_domain_probe.py --target . --max-bytes 33554432
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

# The manifest, the GET, the delimiter sniffing and the record resolution are
# the same questions asked of the same datasets through the same declared
# sites. A second implementation would be a second place for that to drift.
import schema_field_probe as sfp  # noqa: E402

OK = "VALUE_DOMAIN_OK"
# Measured, values outside the domain present, and EVERY coercion of that column
# is guarded. Not a finding — the code answered the question this probe asks —
# and not `OK` either, because something is there and the share belongs in the
# report. Exit 0. See `DatasetResult.status`.
HANDLED = "VALUE_DOMAIN_HANDLED"
DRIFT = "VALUE_DOMAIN_DRIFT"
UNVERIFIED = "UNVERIFIED"
MANIFEST_MISSING = sfp.MANIFEST_MISSING
NO_COERCION = "NO_COERCION"

NOTE_GUARDED = "COERCION_GUARDED"
NOTE_NAME_DRIFT = "COLUMN_NAME_DRIFT"

_FINDINGS = frozenset({DRIFT})

# Enough to see a one-in-five share many times over, small enough that the
# check stays something people leave switched on.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ROWS = 100_000

COERCERS = ("int", "float")

# Spellings that mean «no value here», as opposed to a value that is not a
# number. Kept apart from `non_numeric` because the remedies differ: a null
# literal wants a default, `"1 bis 5"` wants a decision about what the tool
# should say when the source deliberately withheld the number.
_NULL_LITERALS = frozenset({"null", "none", "nan", "na", "n/a", "-", "--", "keine"})

INTEGER = "integer"
FRACTIONAL = "fractional"
EMPTY = "empty"
NULL_LITERAL = "null_literal"
NON_NUMERIC = "non_numeric"


def classify(value: Any) -> str:
    """Which of the five a single cell is.

    ``integer`` and ``fractional`` are kept apart because ``int("12.5")``
    raises just as ``int("1 bis 5")`` does. Whether that matters depends on the
    coercer the code applied, so the split is made here and the judgement is
    made per column, where the coercers are known.
    """
    if value is None:
        return NULL_LITERAL
    if isinstance(value, bool):
        return NON_NUMERIC
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return INTEGER if value.is_integer() else FRACTIONAL
    text = str(value).strip()
    if not text:
        return EMPTY
    if text.lower() in _NULL_LITERALS:
        return NULL_LITERAL
    # A thousands separator or a decimal comma is still a number the source
    # meant as one; reading those as prose would drown the real finding under
    # the ordinary formatting of a Swiss extract.
    probe_text = text.replace("'", "").replace(" ", "").replace("\u00a0", "")
    if probe_text.count(",") == 1 and "." not in probe_text:
        probe_text = probe_text.replace(",", ".")
    else:
        probe_text = probe_text.replace(",", "")
    try:
        number = float(probe_text)
    except ValueError:
        return NON_NUMERIC
    return INTEGER if number.is_integer() and "." not in probe_text else FRACTIONAL


# ---------------------------------------------------------------------------
# The code side
# ---------------------------------------------------------------------------


@dataclass
class Coercion:
    """One place the code turns a named column into a number."""

    column: str
    func: str
    line: int
    guarded: bool
    shape: str  # "direct" | "name-bound"

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "func": self.func,
            "line": self.line,
            "guarded": self.guarded,
            "shape": self.shape,
        }


def _field_name(node: ast.AST) -> str | None:
    """The column read by ``row["x"]`` or ``row.get("x")``, else None."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "pop")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _guarded_spans(root: ast.AST) -> list[tuple[int, int]]:
    """Line ranges of ``try`` bodies whose handler catches a coercion failure.

    A ``try`` catching something unrelated (``KeyError``, ``httpx.HTTPError``)
    is not a guard for this, and counting it as one would silence exactly the
    call sites that crash.
    """
    spans: list[tuple[int, int]] = []
    for node in ast.walk(root):
        if not isinstance(node, ast.Try):
            continue
        catches = False
        for handler in node.handlers:
            names: list[str] = []
            target = handler.type
            if target is None:
                catches = True  # bare `except:` catches it too
                break
            for part in target.elts if isinstance(target, ast.Tuple) else [target]:
                if isinstance(part, ast.Name):
                    names.append(part.id)
                elif isinstance(part, ast.Attribute):
                    names.append(part.attr)
            if {"ValueError", "TypeError", "Exception", "ArithmeticError"} & set(names):
                catches = True
                break
        if not catches or not node.body:
            continue
        start = node.body[0].lineno
        end = max(
            getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno
            for stmt in node.body
        )
        spans.append((start, end))
    return spans


def find_coercions(
    target: Path, site: sfp.Site, declared: tuple[sfp.Coercer, ...] = ()
) -> tuple[list[Coercion], str | None]:
    """Columns handed to a coercer inside the declared symbol.

    ``int`` and ``float`` are always coercers. ``declared`` adds the target's
    own helpers by name — a project that wraps the coercion in one place, which
    is the good pattern and was this portfolio's own fix for the ``"1 bis 5"``
    incident, is otherwise invisible to this probe. A helper marked
    ``tolerant`` returns a sentinel instead of raising, so its call sites count
    as guarded exactly like a ``try`` around them.
    """
    tolerant = {c.name for c in declared if c.tolerant}
    coercers = set(COERCERS) | {c.name for c in declared}
    path = target / site.file
    if not path.is_file():
        return [], f"{site.file} does not exist in the checkout"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [], f"{site.file} could not be parsed: {exc}"
    node = sfp.find_symbol(tree, site.symbol)
    if node is None:
        return [], f"{site.file} has no symbol named {site.symbol!r}"

    # One hop, deliberately: `raw = row["anzahl"]` then `int(raw)`. No chains,
    # no attributes, no augmented assignment — the same shallow rule the
    # reference-drift `wraps` fix settled on, for the same reason. A wider
    # analysis starts reporting a coercion where the value merely passed
    # through, and a false positive costs more here than a missed one.
    bound: dict[str, str] = {}
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Assign)
            and len(child.targets) == 1
            and isinstance(child.targets[0], ast.Name)
        ):
            name = _field_name(child.value)
            if name is not None:
                bound[child.targets[0].id] = name

    spans = _guarded_spans(node)
    ignore = set(site.ignore)
    found: dict[tuple[str, str, int], Coercion] = {}
    for child in ast.walk(node):
        if (
            not isinstance(child, ast.Call)
            or not isinstance(child.func, ast.Name)
            or child.func.id not in coercers
            or not child.args
        ):
            continue
        arg = child.args[0]
        column = _field_name(arg)
        shape = "direct"
        if column is None and isinstance(arg, ast.Name):
            column = bound.get(arg.id)
            shape = "name-bound"
        if column is None:
            # `int(float(row["a"]))` — the coercion that actually meets the raw
            # string is the INNER one, and `ast.walk` reaches it on its own. The
            # outer call is skipped rather than recorded a second time under the
            # stricter name, which would report every such column as holding
            # fractional values it is not being asked to parse as integers.
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id in coercers
            ):
                continue
            # `int(row["a"] or 0)`, `int(x.strip())` — walk in, and accept it
            # only when exactly ONE column is in there. Two would make the
            # attribution a guess.
            names = {n for sub in ast.walk(arg) if (n := _field_name(sub)) is not None}
            if len(names) == 1:
                column = names.pop()
                shape = "wrapped"
        if column is None or column in ignore:
            continue
        guarded = child.func.id in tolerant or any(
            start <= child.lineno <= end for start, end in spans
        )
        key = (column, child.func.id, child.lineno)
        found.setdefault(
            key,
            Coercion(
                column=column,
                func=child.func.id,
                line=child.lineno,
                guarded=guarded,
                shape=shape,
            ),
        )
    return sorted(found.values(), key=lambda c: (c.line, c.column)), None


# ---------------------------------------------------------------------------
# The source side
# ---------------------------------------------------------------------------


@dataclass
class ColumnReading:
    """What the source put in one column, over the rows that were read."""

    column: str
    live_name: str
    coercers: tuple[str, ...] = ()
    # False when every coercion of this column is guarded — by a `try` that
    # catches the failure, or by a declared tolerant helper.
    exposed: bool = True
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def offending_kinds(self) -> tuple[str, ...]:
        """Which buckets are a failure *for the coercer this column got*.

        ``12.5`` is a number and ``int("12.5")`` still raises, so a fractional
        value counts against a column the code sends through ``int()`` and not
        against one it sends through ``float()``. Judging it globally would
        either miss the first or invent a finding on the second.
        """
        kinds = [NON_NUMERIC, EMPTY, NULL_LITERAL]
        if "int" in self.coercers and "float" not in self.coercers:
            kinds.append(FRACTIONAL)
        return tuple(kinds)

    @property
    def offending(self) -> int:
        return sum(self.counts.get(kind, 0) for kind in self.offending_kinds)

    @property
    def share(self) -> float:
        return (self.offending / self.total) if self.total else 0.0

    def summary(self) -> str:
        parts = [
            f"{kind}={self.counts[kind]}"
            for kind in self.offending_kinds
            if self.counts.get(kind)
        ]
        return ", ".join(parts) or "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "live_name": self.live_name,
            "coercers": list(self.coercers),
            "exposed": self.exposed,
            "counts": self.counts,
            "rows": self.total,
            "offending": self.offending,
            "share": round(self.share, 4),
        }


def rows_from_csv(
    body: bytes, dataset: sfp.Dataset, truncated: bool
) -> tuple[list[str], list[list[str]]]:
    text = body.decode(dataset.encoding, errors="replace")
    lines = text.split("\n")
    if truncated and len(lines) > 1:
        # The byte cap almost never lands on a line break. A half row parsed as
        # a whole one is a domain violation the source never committed.
        lines = lines[:-1]
    if not lines or not lines[0].strip():
        raise sfp.SourceError("the first line of the response is empty")
    header, delimiter = sfp.split_header(lines[0].rstrip("\r"), dataset.delimiter)
    if delimiter.startswith("("):
        raise sfp.SourceError("no delimiter could be determined from the header line")
    reader = csv.reader(io.StringIO("\n".join(lines[1:])), delimiter=delimiter)
    return [h for h in header if h], [row for row in reader if row]


def read_values(
    dataset: sfp.Dataset, body: bytes, truncated: bool, max_rows: int
) -> tuple[list[str], list[dict[str, Any]], bool]:
    """(field names, records, whether the row cap also bit)."""
    if dataset.fmt == "csv":
        header, rows = rows_from_csv(body, dataset, truncated)
        capped = len(rows) > max_rows
        records = [dict(zip(header, row, strict=False)) for row in rows[:max_rows]]
        return header, records, capped
    if truncated:
        raise sfp.SourceError(
            "the response exceeded the byte cap and was truncated — a JSON body "
            "cannot be parsed from a prefix, so no value was classified"
        )
    payload = json.loads(body.decode(dataset.encoding, errors="replace"))
    found = sfp.resolve_records(payload, dataset.record_path)
    if found is None:
        raise sfp.SourceError("no record list found in the response")
    if not found:
        raise sfp.SourceError("the source returned zero records — nothing to classify")
    records = [r for r in found if isinstance(r, dict)]
    if not records:
        raise sfp.SourceError("the record list contains no objects")
    return list(records[0].keys()), records[:max_rows], len(records) > max_rows


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass
class DatasetResult:
    id: str
    url: str
    rows_read: int = 0
    truncated: bool = False
    coercions: list[Coercion] = field(default_factory=list)
    readings: list[ColumnReading] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def offenders(self) -> list[ColumnReading]:
        return [r for r in self.readings if r.offending]

    @property
    def status(self) -> str:
        # A column whose every coercion is guarded is not a finding. The code
        # has answered the question this probe asks, and a gate that goes red
        # on correctly handled code is switched off within a week — taking the
        # unguarded ones with it. The share is still reported, under its own
        # status, because 18.6 % is a fact about the source either way.
        if any(r.exposed for r in self.offenders):
            return DRIFT
        if not self.readings:
            return UNVERIFIED
        if self.truncated:
            # Nothing exposed found. That is only clean if the whole response
            # was read — the suppressed rows cluster where nobody looked.
            return UNVERIFIED if not self.offenders else HANDLED
        return HANDLED if self.offenders else OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "rows_read": self.rows_read,
            "truncated": self.truncated,
            "coercions": [c.as_dict() for c in self.coercions],
            "columns": [r.as_dict() for r in self.readings],
            "unverified": self.unverified,
            "notes": self.notes,
        }


@dataclass
class Report:
    target: str
    manifest: str
    status: str = UNVERIFIED
    reason: str = ""
    results: list[DatasetResult] = field(default_factory=list)
    provenance: probe_provenance.Provenance | None = None

    @property
    def finding(self) -> bool:
        return self.status in _FINDINGS

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "manifest": self.manifest,
            "status": self.status,
            "reason": self.reason,
            "datasets": [r.as_dict() for r in self.results],
        }


def probe(
    target: Path,
    manifest_path: Path,
    only: str | None = None,
    fetch: Any = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> Report:
    report = Report(target=str(target), manifest=str(manifest_path))
    if not manifest_path.is_file():
        report.status = MANIFEST_MISSING
        report.reason = (
            f"no {manifest_path.name} in the target — the datasets and the sites that "
            "read them are declared, never guessed. See schema-fields.example.toml"
        )
        return report

    datasets = sfp.load_manifest(manifest_path)
    if only:
        datasets = [d for d in datasets if d.id == only]
        if not datasets:
            raise sfp.ManifestError(f"no dataset named {only!r} in {manifest_path}")

    fetcher = fetch or sfp.http_get
    for dataset in datasets:
        result = DatasetResult(id=dataset.id, url=dataset.url)
        report.results.append(result)

        for site in dataset.sites:
            coercions, why = find_coercions(target, site, dataset.coercers)
            if why:
                result.unverified.append(f"{site.file}::{site.symbol}: {why}")
                continue
            result.coercions.extend(coercions)
        if not result.coercions:
            result.unverified.append(
                f"{NO_COERCION}: no declared site of {dataset.id!r} applies int() or "
                "float() to a named column — this probe measured nothing here"
            )
            continue

        try:
            body = fetcher(dataset.url, max_bytes + 1)
            truncated = len(body) > max_bytes
            header, records, row_capped = read_values(
                dataset, body[:max_bytes] if truncated else body, truncated, max_rows
            )
        except (sfp.SourceError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            result.unverified.append(f"{dataset.id}: {exc}")
            continue
        # The same declared transformation `schema_field_probe` applies: a
        # server that lowercases every key at fetch time does not read the
        # header the wire sent, and comparing against the raw one would
        # attach a COLUMN_NAME_DRIFT note to every column of every
        # Title-Case dataset — noise that means nothing about the values.
        if dataset.normalised:
            header = sfp.apply_normalisation(header, dataset.normalised)
            transform = sfp.NORMALISATIONS[dataset.normalised]
            records = [
                {transform(str(k)): v for k, v in record.items()} for record in records
            ]
        result.rows_read = len(records)
        result.truncated = truncated or row_capped

        by_norm: dict[str, str] = {}
        for name in header:
            by_norm.setdefault(sfp.normalise(name), name)

        for column in sorted({c.column for c in result.coercions}):
            live_name = (
                column if column in header else by_norm.get(sfp.normalise(column))
            )
            if live_name is None:
                result.unverified.append(
                    f"{column!r} is coerced in the code but is not a column of the live "
                    "response — the domain was not measured. Whether the NAME is right "
                    "is schema_field_probe's question, not this one's"
                )
                continue
            if live_name != column:
                result.notes.append(
                    f"{NOTE_NAME_DRIFT}: the code coerces {column!r}, the source sends "
                    f"{live_name!r} — measured against {live_name!r}; the name itself is "
                    "a schema_field_probe finding"
                )
            reading = ColumnReading(
                column=column,
                live_name=live_name,
                coercers=tuple(
                    sorted({c.func for c in result.coercions if c.column == column})
                ),
                exposed=any(
                    not c.guarded for c in result.coercions if c.column == column
                ),
            )
            for record in records:
                kind = classify(record.get(live_name))
                reading.counts[kind] = reading.counts.get(kind, 0) + 1
            result.readings.append(reading)

        # One note per (column, coercer), not per call site: four identical
        # sentences about `_parse_count` on `anzahl` push the line that matters
        # off the screen, and a report nobody finishes reading is the same as a
        # report nobody runs.
        for column, func in sorted(
            {(c.column, c.func) for c in result.coercions if c.guarded}
        ):
            lines = sorted(
                c.line
                for c in result.coercions
                if c.guarded and c.column == column and c.func == func
            )
            reading = next((r for r in result.readings if r.column == column), None)
            share = f" — {reading.share:.1%} of the column" if reading else ""
            result.notes.append(
                f"{NOTE_GUARDED}: {func}() on {column!r} at line(s) "
                f"{', '.join(str(n) for n in lines)} cannot raise here"
                f"{share} is outside the numeric domain and is absorbed rather than "
                "reaching the caller. Not a finding; whether the absorbed rows are "
                "visible in what the tool reports is a different question, and not "
                "this probe's"
            )

    statuses = {r.status for r in report.results}
    if DRIFT in statuses:
        report.status = DRIFT
    elif UNVERIFIED in statuses:
        # A partial run outranks a clean one: it did not look everywhere.
        report.status = UNVERIFIED
    elif HANDLED in statuses:
        report.status = HANDLED
    else:
        report.status = OK

    offending = sum(len(r.offenders) for r in report.results)
    report.reason = (
        f"{len([r for r in report.results if r.readings])} of {len(report.results)} "
        f"declared dataset(s) measured; {offending} coerced column(s) hold values "
        "outside the numeric domain"
    )
    return report


def render(report: Report) -> str:
    head = [report.provenance.render()] if report.provenance is not None else []
    if report.provenance is not None and report.provenance.blocking:
        return "\n".join([*head, report.provenance.moved_detail()])

    out = [f"{report.status:<19} {report.reason or '—'}"]
    for result in report.results:
        scope = (
            f"{result.rows_read} row(s) read"
            + (
                " — CAPPED, the tail was not read"
                if result.truncated
                else " — full read"
            )
            if result.rows_read
            else "no row read"
        )
        out.append(f"  {result.id} [{result.status}] {scope}")
        for reading in result.readings:
            if not reading.offending:
                marker = "ok"
            else:
                marker = DRIFT if reading.exposed else HANDLED
            out.append(
                f"    {marker:<19} {reading.live_name!r}: "
                f"{reading.offending}/{reading.total} = {reading.share:.1%} outside the "
                f"numeric domain ({reading.summary()})"
            )
        for coercion in result.coercions:
            out.append(
                f"      coerced by {coercion.func}() at line {coercion.line} "
                f"({coercion.shape}{', guarded' if coercion.guarded else ''})"
            )
        for line in result.unverified:
            out.append(f"    UNVERIFIED          {line}")
        for note in result.notes:
            out.append(f"    note                {note}")
    return "\n".join([*head, *out])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="value_domain_probe")
    ap.add_argument("--target", default=".", help="path to the MCP server repo")
    ap.add_argument(
        "--manifest",
        default=None,
        help=f"path to the field manifest (default: <target>/{sfp.DEFAULT_MANIFEST})",
    )
    ap.add_argument("--dataset", default=None, help="measure only this dataset id")
    ap.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="byte cap per response; a capped read with no finding is UNVERIFIED",
    )
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"{target}: not a directory", file=sys.stderr)
        return 127
    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else target / sfp.DEFAULT_MANIFEST
    )

    prov = probe_provenance.capture(target)
    try:
        report = probe(
            target,
            manifest_path,
            only=args.dataset,
            max_bytes=args.max_bytes,
            max_rows=args.max_rows,
        )
    except sfp.ManifestError as exc:
        print(f"{exc}", file=sys.stderr)
        return 127
    report.provenance = prov.recheck()

    if args.format == "json":
        payload = report.as_dict()
        payload["provenance"] = prov.as_dict()
        payload["finding"] = None if prov.blocking else report.finding
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render(report))

    if prov.blocking:
        return probe_provenance.EXIT_MOVED
    if report.status in _FINDINGS:
        return 2
    if report.status in (OK, HANDLED):
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
