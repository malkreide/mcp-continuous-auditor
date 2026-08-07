#!/usr/bin/env python3
"""Schema-field probe — does the code read the field names the source delivers?

THE INCIDENT
------------
``zh-education-mcp`` against ``www.bista.zh.ch``, 2026-08-03. The code read
``r["Schulgemeinde"]``. The source delivered ``schulgemeinde``.

What came out was not an error. It was an empty hit list and the sentence
«Schulgemeinde nicht gefunden» — **a failure wearing the costume of an answer**.
A caller cannot tell that apart from a real absence, and neither can a model
summarising the result; the tool reported successfully that the thing does not
exist. Four of six datasets were affected, across eight tools.

Two of the datasets mix the spelling *inside one header row*
(``gebiet_Bezeichnung``, ``staatsangehoerigkeit_ISO2_Code``), so «just lowercase
everything» is not the fix either — it is a second way to read the wrong name.

Every unit test stayed green throughout. They had to: their fixtures pin the old
header, so the suite compared the code's assumption against a recording of the
same assumption. Nothing in the repository could contradict it, because nothing
in the repository was the source.

WHY THIS IS NOT ``live_probe.py``
----------------------------------
``live_probe`` compares the live response against a **fixture** and reports a
structural diff. That catches the source changing under a recording. It cannot
catch this: the fixture and the live response can agree perfectly while the code
reads a name neither of them ever contained. The recording is not what runs.

This probe compares the live response against the **code**. Those are the only
two things whose disagreement produces a wrong answer at a user, and the fixture
sits outside that pair. Where a fixture is declared it is read too, but only to
report *why the tests stayed green* — never as the standard.

THE MAPPING IS DECLARED, NEVER GUESSED
--------------------------------------
Which code reads which dataset is stated in a manifest — ``schema_fields.toml``
in the target by default, format reference in ``schema-fields.example.toml``.
Each dataset names its URL and the sites (``file`` + ``symbol``) that read it.
This follows ``reference_drift_probe``: a guessed mapping produces a finding
nobody can retrace, and a finding nobody can retrace is how a gate gets switched
off. No manifest ⇒ ``MANIFEST_MISSING`` and the probe stops there.

WHAT IS EXTRACTED FROM THE CODE
-------------------------------
Inside the declared symbol: every string literal used as a subscript key
(``row["anzahl"]``) or handed to ``.get(...)`` / ``.pop(...)``. That
over-collects by design — a symbol also indexes dicts that are not records — and
the over-collection is disarmed by the corroboration rule below rather than by
trying to track which dict is which. Guessing that would be the same guess the
manifest exists to avoid.

THE CORROBORATION RULE
----------------------
A key read at a site is only reported as ``FIELD_MISSING`` when **at least one
other key read at the same site does resolve** against the live header. Five
keys read, four of them in the header, the fifth absent: that is drift. Zero of
five in the header: the site is not reading this dataset — the manifest is
wrong, or the code moved — and the probe says ``SITE_UNMATCHED`` and measures
nothing there. A probe that reported five findings in that case would be
reporting its own mismatched manifest as a defect of the target.

THE FINDINGS
------------
``FIELD_CASE_DRIFT`` — the name exists in the live header under a different
spelling. Both are printed. Comparison is on a normalised form (casefolded,
separators removed), so it covers ``Schulgemeinde``/``schulgemeinde`` and
``gebiet_Bezeichnung``/``gebiet_bezeichnung`` alike. This is the sharpest of the
two: an unrelated dictionary key does not accidentally normalise onto a column
of the dataset the manifest points at.

``FIELD_MISSING`` — the name is not in the live header in any spelling, at a
site that otherwise resolves. The column was renamed or dropped.

Neither finding says what the code *does* with the miss. That depends on the
call: ``r["x"]`` raises, ``r.get("x")`` returns ``None`` and the filter silently
matches nothing. The second is the incident, and the report names which form
each hit used so the reader knows whether they are looking for a crash or for
silence.

NOT MEASURED IS NOT CLEAN
-------------------------
``UNVERIFIED`` covers every way this run can fail to conclude, and each carries
what was seen: the source unreachable or too slow, a response that does not
parse, a response with no record in it (a field list cannot be read from an
empty list), a declared file or symbol that is not there, a site that resolves
nothing, and a header longer than the byte window. None of them is silence.

READ-ONLY AND CHEAP ON THE SOURCE. Every request is a GET. For a CSV only the
first ``--header-bytes`` are read — the header is the whole question, and
downloading a 60 MB extract to look at line one would make the check something
people switch off.

EXIT CODES
  0    every declared read resolves against the live header
  2    FINDING — FIELD_MISSING or FIELD_CASE_DRIFT
  3    NOT MEASURED — nothing could be concluded (see above)
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run (no target, unreadable manifest)

Usage:
  python scripts/schema_field_probe.py --target ../zh-education-mcp
  python scripts/schema_field_probe.py --target . --manifest schema_fields.toml
  python scripts/schema_field_probe.py --target . --dataset schulgemeinden --format json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10; the project requires 3.11+
    tomllib = None  # type: ignore[assignment]

OK = "SCHEMA_OK"
FIELD_MISSING = "FIELD_MISSING"
FIELD_CASE_DRIFT = "FIELD_CASE_DRIFT"
UNVERIFIED = "UNVERIFIED"
MANIFEST_MISSING = "MANIFEST_MISSING"

NOTE_MIXED_CASE = "MIXED_CASE_HEADER"
NOTE_FIXTURE_STALE = "FIXTURE_PINS_OLD_HEADER"

_FINDINGS = frozenset({FIELD_MISSING, FIELD_CASE_DRIFT})

DEFAULT_MANIFEST = "schema_fields.toml"

_USER_AGENT = (
    "mcp-continuous-auditor schema-field-probe "
    "(+https://github.com/malkreide/mcp-continuous-auditor)"
)
# The header is the whole question for a CSV. A dataset extract can be tens of
# megabytes; reading all of it to look at line one would make this check
# expensive enough that somebody switches it off.
DEFAULT_HEADER_BYTES = 65536
# A JSON response has to be parsed whole, so it gets a cap instead of a window.
# A truncated body is UNVERIFIED, never a field list read from half a document.
DEFAULT_JSON_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30

# Ordered: the first that resolves to a non-empty list of objects wins. Same
# well-known collection keys `live_probe.count_records` uses, for the same
# reason — CKAN, GeoJSON and the plain shapes cover the portfolio's sources.
_RECORD_KEYS: tuple[tuple[str, ...], ...] = (
    ("result", "records"),
    ("features",),
    ("results",),
    ("records",),
    ("entries",),
    ("data",),
    ("items",),
)

_DELIMITERS = (";", ",", "\t", "|")


class ManifestError(Exception):
    """The manifest is not usable — a harness failure, never a finding."""


class SourceError(Exception):
    """The source could not be read. UNVERIFIED, with what was seen."""


def normalise(name: str) -> str:
    """Casefolded, separator-free — the form two spellings of one column share.

    ``Schulgemeinde`` and ``schulgemeinde`` collapse onto the same key, and so
    do ``gebiet_Bezeichnung`` and ``gebiet_bezeichnung``. Separators go too,
    because a source that renames ``schul_gemeinde`` to ``schulgemeinde`` has
    done the same thing to the caller as a case change: the read returns
    nothing and says so politely.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    file: str
    symbol: str
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dataset:
    id: str
    url: str
    fmt: str
    sites: tuple[Site, ...]
    delimiter: str | None = None
    encoding: str = "utf-8-sig"
    record_path: str | None = None
    fixture: str | None = None


def load_manifest(path: Path) -> list[Dataset]:
    if tomllib is None:  # pragma: no cover - 3.10 only
        raise ManifestError("tomllib is unavailable; Python 3.11+ is required")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: not valid TOML — {exc}") from exc

    entries = raw.get("dataset")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(
            f"{path}: no [[dataset]] entries. An empty manifest would report "
            "«nothing to check» with the same exit code as a checked repository"
        )
    datasets: list[Dataset] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ManifestError(f"{path}: [[dataset]] #{index + 1} is not a table")
        missing = [k for k in ("id", "url", "format") if not entry.get(k)]
        if missing:
            raise ManifestError(
                f"{path}: [[dataset]] #{index + 1} is missing {', '.join(missing)}"
            )
        fmt = str(entry["format"]).lower()
        if fmt not in ("csv", "json"):
            raise ManifestError(
                f"{path}: dataset {entry['id']!r} declares format {fmt!r}; "
                "this probe reads csv and json"
            )
        raw_sites = entry.get("site")
        if not isinstance(raw_sites, list) or not raw_sites:
            raise ManifestError(
                f"{path}: dataset {entry['id']!r} declares no [[dataset.site]]. "
                "Which code reads this dataset is the mapping, and it is never guessed"
            )
        sites: list[Site] = []
        for site in raw_sites:
            if (
                not isinstance(site, dict)
                or not site.get("file")
                or not site.get("symbol")
            ):
                raise ManifestError(
                    f"{path}: a site of dataset {entry['id']!r} lacks file or symbol"
                )
            sites.append(
                Site(
                    file=str(site["file"]),
                    symbol=str(site["symbol"]),
                    ignore=tuple(str(i) for i in site.get("ignore", [])),
                )
            )
        datasets.append(
            Dataset(
                id=str(entry["id"]),
                url=str(entry["url"]),
                fmt=fmt,
                sites=tuple(sites),
                delimiter=entry.get("delimiter"),
                encoding=str(entry.get("encoding", "utf-8-sig")),
                record_path=entry.get("record_path"),
                fixture=entry.get("fixture"),
            )
        )
    return datasets


# ---------------------------------------------------------------------------
# The code side
# ---------------------------------------------------------------------------


@dataclass
class KeyRead:
    """One field name the code reads, and the form it read it in."""

    name: str
    line: int
    form: str  # "subscript" (raises on a miss) | "get" (returns None, silently)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "line": self.line, "form": self.form}


def find_symbol(tree: ast.Module, symbol: str) -> ast.AST | None:
    """A top-level or nested function/class by name — the whole tree is walked.

    A method named in the manifest as if it were a module-level function is the
    mistake `reference_drift_probe` documents from its own field run. Walking
    rather than scanning the top level accepts both spellings instead of
    reporting the manifest's style as a missing symbol.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name == symbol
        ):
            return node
    return None


def read_site_keys(target: Path, site: Site) -> tuple[list[KeyRead], str | None]:
    """Field names read inside the declared symbol, or the reason there are none."""
    path = target / site.file
    if not path.is_file():
        return [], f"{site.file} does not exist in the checkout"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [], f"{site.file} could not be parsed: {exc}"
    node = find_symbol(tree, site.symbol)
    if node is None:
        return [], f"{site.file} has no symbol named {site.symbol!r}"

    ignore = set(site.ignore)
    seen: dict[tuple[str, str], KeyRead] = {}
    for child in ast.walk(node):
        found: tuple[str, int, str] | None = None
        if isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
            if isinstance(child.slice.value, str):
                found = (child.slice.value, child.slice.lineno, "subscript")
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in ("get", "pop")
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and isinstance(child.args[0].value, str)
        ):
            found = (child.args[0].value, child.lineno, "get")
        if found is None or found[0] in ignore:
            continue
        name, lineno, form = found
        key = (name, form)
        if key not in seen:
            seen[key] = KeyRead(name=name, line=lineno, form=form)
    return sorted(seen.values(), key=lambda k: (k.line, k.name)), None


# ---------------------------------------------------------------------------
# The source side
# ---------------------------------------------------------------------------


def http_get(url: str, limit: int, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """A GET, capped at ``limit`` bytes. The only network this probe does."""
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (manifest URLs)
            return response.read(limit)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SourceError(f"{type(exc).__name__}: {exc}") from exc


def split_header(line: str, declared: str | None) -> tuple[list[str], str]:
    """The header row, and the delimiter it was read with.

    A sniffed delimiter is reported alongside the fields, because reading a
    semicolon-separated header as one comma-separated column produces one
    enormous "field name" that matches nothing — which would look exactly like
    total drift.
    """
    if declared:
        return [c.strip().strip('"') for c in line.split(declared)], declared
    best, best_count = _DELIMITERS[0], -1
    for candidate in _DELIMITERS:
        count = line.count(candidate)
        if count > best_count:
            best, best_count = candidate, count
    if best_count <= 0:
        return [line.strip().strip('"')], "(none — single column)"
    return [c.strip().strip('"') for c in line.split(best)], best


def fields_from_csv(body: bytes, dataset: Dataset) -> tuple[list[str], str]:
    text = body.decode(dataset.encoding, errors="replace")
    if "\n" not in text and len(body) >= DEFAULT_HEADER_BYTES:
        raise SourceError(
            f"no line break in the first {DEFAULT_HEADER_BYTES} bytes — the header "
            "was not read, so no field name was compared"
        )
    line = text.split("\n", 1)[0].rstrip("\r")
    if not line.strip():
        raise SourceError("the first line of the response is empty")
    fields, delimiter = split_header(line, dataset.delimiter)
    return [f for f in fields if f], delimiter


def resolve_records(payload: Any, record_path: str | None) -> list[Any] | None:
    if record_path:
        node: Any = payload
        for segment in record_path.split("."):
            if not isinstance(node, dict) or segment not in node:
                return None
            node = node[segment]
        return node if isinstance(node, list) else None
    if isinstance(payload, list):
        return payload
    for keys in _RECORD_KEYS:
        node = payload
        for segment in keys:
            if not isinstance(node, dict) or segment not in node:
                node = None
                break
            node = node[segment]
        if isinstance(node, list):
            return node
    return None


def fields_from_json(
    body: bytes, dataset: Dataset, truncated: bool
) -> tuple[list[str], str]:
    if truncated:
        raise SourceError(
            f"the response exceeded {DEFAULT_JSON_BYTES} bytes and was truncated — "
            "a field list read from half a document is not evidence"
        )
    try:
        payload = json.loads(body.decode(dataset.encoding, errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SourceError(f"the response is not JSON: {exc}") from exc
    records = resolve_records(payload, dataset.record_path)
    if records is None:
        where = (
            f"record_path {dataset.record_path!r}"
            if dataset.record_path
            else ("any of the well-known collection keys")
        )
        raise SourceError(f"no record list found at {where}")
    if not records:
        raise SourceError(
            "the source returned zero records — field names cannot be read from an "
            "empty list, and an empty list is also what the incident looked like"
        )
    first = records[0]
    if not isinstance(first, dict):
        raise SourceError(
            f"the first record is a {type(first).__name__}, not an object"
        )
    return list(first.keys()), "(json object keys)"


def fetch_fields(
    dataset: Dataset, fetch: Callable[[str, int], bytes] | None = None
) -> tuple[list[str], str]:
    """The field names the source delivers right now, and how they were read."""
    get = fetch or http_get
    if dataset.fmt == "csv":
        return fields_from_csv(get(dataset.url, DEFAULT_HEADER_BYTES), dataset)
    body = get(dataset.url, DEFAULT_JSON_BYTES + 1)
    return fields_from_json(body, dataset, truncated=len(body) > DEFAULT_JSON_BYTES)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    dataset: str
    site: str
    status: str
    read: str
    live: str | None
    line: int
    form: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "site": self.site,
            "status": self.status,
            "read": self.read,
            "live": self.live,
            "line": self.line,
            "form": self.form,
        }


@dataclass
class DatasetResult:
    id: str
    url: str
    live_fields: list[str] = field(default_factory=list)
    delimiter: str = ""
    hits: list[Hit] = field(default_factory=list)
    matched: int = 0
    unverified: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(h.status == FIELD_MISSING for h in self.hits):
            return FIELD_MISSING
        if self.hits:
            return FIELD_CASE_DRIFT
        if not self.live_fields or self.unverified:
            return UNVERIFIED
        return OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "live_fields": self.live_fields,
            "delimiter": self.delimiter,
            "hits": [h.as_dict() for h in self.hits],
            "matched": self.matched,
            "unverified": self.unverified,
            "notes": self.notes,
        }


def mixed_case_note(fields: list[str]) -> str | None:
    """Does one header row mix the spellings?

    ``gebiet_Bezeichnung`` beside plain lowercase columns is the shape that
    makes «lowercase everything on read» a second wrong answer rather than a
    fix, so it is worth saying out loud next to the finding.
    """
    upper = [f for f in fields if any(c.isupper() for c in f)]
    lower = [f for f in fields if f.islower()]
    if upper and lower:
        return (
            f"{NOTE_MIXED_CASE}: {len(upper)} of {len(fields)} columns carry an "
            f"uppercase letter ({', '.join(upper[:3])}"
            f"{', …' if len(upper) > 3 else ''}) beside {len(lower)} all-lowercase "
            "ones — normalising on read is not a fix here, it is a second wrong name"
        )
    return None


def compare_site(
    dataset: Dataset, site: Site, keys: list[KeyRead], live_fields: list[str]
) -> tuple[list[Hit], int, str | None]:
    """(hits, keys that resolved, reason the site was not measured)."""
    exact = set(live_fields)
    by_norm: dict[str, list[str]] = {}
    for name in live_fields:
        by_norm.setdefault(normalise(name), []).append(name)

    label = f"{site.file}::{site.symbol}"
    resolved = 0
    candidates: list[Hit] = []
    for key in keys:
        if key.name in exact:
            resolved += 1
            continue
        variants = by_norm.get(normalise(key.name))
        if variants:
            resolved += 1
            candidates.append(
                Hit(
                    dataset=dataset.id,
                    site=label,
                    status=FIELD_CASE_DRIFT,
                    read=key.name,
                    live=variants[0],
                    line=key.line,
                    form=key.form,
                )
            )
        else:
            candidates.append(
                Hit(
                    dataset=dataset.id,
                    site=label,
                    status=FIELD_MISSING,
                    read=key.name,
                    live=None,
                    line=key.line,
                    form=key.form,
                )
            )

    if not keys:
        return [], 0, f"{label}: no field name is read inside this symbol"
    if resolved == 0:
        # The corroboration rule. Nothing here lines up with the dataset, so
        # this site is not reading it — the manifest is wrong or the code
        # moved. Reporting every key as missing would book the probe's own
        # mismatched mapping as a defect of the target.
        return (
            [],
            0,
            f"{label}: none of the {len(keys)} field name(s) read here resolve "
            f"against the live header — this site does not appear to read "
            f"{dataset.id!r}, so nothing was measured for it",
        )
    return candidates, resolved, None


def fixture_note(target: Path, dataset: Dataset, live_fields: list[str]) -> str | None:
    """Does the declared fixture pin a header the source no longer sends?

    Not a finding — the fixture is not what runs. It is the sentence that
    explains why a suite full of green tests said nothing about any of this.
    """
    if not dataset.fixture:
        return None
    path = target / dataset.fixture
    if not path.is_file():
        return f"the declared fixture {dataset.fixture} does not exist in the checkout"
    try:
        head = path.read_bytes()[:DEFAULT_HEADER_BYTES]
    except OSError as exc:
        return f"the declared fixture {dataset.fixture} could not be read: {exc}"
    try:
        if dataset.fmt == "csv":
            fixture_fields, _ = fields_from_csv(head, dataset)
        else:
            fixture_fields, _ = fields_from_json(head, dataset, truncated=False)
    except SourceError as exc:
        return f"the declared fixture {dataset.fixture} could not be read: {exc}"
    if set(fixture_fields) == set(live_fields):
        return None
    only_fixture = [f for f in fixture_fields if f not in set(live_fields)]
    return (
        f"{NOTE_FIXTURE_STALE}: {dataset.fixture} pins "
        f"{len(only_fixture)} field name(s) the source no longer sends "
        f"({', '.join(only_fixture[:4])}{', …' if len(only_fixture) > 4 else ''}) — "
        "every test asserting against it compares the code's assumption with a "
        "recording of the same assumption"
    )


@dataclass
class Report:
    target: str
    manifest: str
    status: str = UNVERIFIED
    reason: str = ""
    results: list[DatasetResult] = field(default_factory=list)
    provenance: probe_provenance.Provenance | None = None

    @property
    def hits(self) -> list[Hit]:
        return [h for r in self.results for h in r.hits]

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
    fetch: Callable[[str, int], bytes] | None = None,
) -> Report:
    report = Report(target=str(target), manifest=str(manifest_path))
    if not manifest_path.is_file():
        report.status = MANIFEST_MISSING
        report.reason = (
            f"no {manifest_path.name} in the target. Which code reads which dataset "
            "is the mapping this probe needs, and it is declared rather than guessed "
            "— see schema-fields.example.toml"
        )
        return report

    datasets = load_manifest(manifest_path)
    if only:
        datasets = [d for d in datasets if d.id == only]
        if not datasets:
            raise ManifestError(f"no dataset named {only!r} in {manifest_path}")

    fetcher = fetch or http_get
    for dataset in datasets:
        result = DatasetResult(id=dataset.id, url=dataset.url)
        report.results.append(result)
        try:
            live_fields, delimiter = fetch_fields(dataset, fetcher)
        except SourceError as exc:
            result.unverified.append(f"{dataset.id}: {exc}")
            continue
        result.live_fields = live_fields
        result.delimiter = delimiter

        for site in dataset.sites:
            keys, why = read_site_keys(target, site)
            if why:
                result.unverified.append(f"{site.file}::{site.symbol}: {why}")
                continue
            hits, resolved, unmeasured = compare_site(dataset, site, keys, live_fields)
            if unmeasured:
                result.unverified.append(unmeasured)
                continue
            result.hits.extend(hits)
            result.matched += resolved

        note = mixed_case_note(live_fields)
        if note:
            result.notes.append(note)
        note = fixture_note(target, dataset, live_fields)
        if note:
            result.notes.append(note)

    statuses = {r.status for r in report.results}
    if FIELD_MISSING in statuses:
        report.status = FIELD_MISSING
    elif FIELD_CASE_DRIFT in statuses:
        report.status = FIELD_CASE_DRIFT
    elif statuses == {OK}:
        report.status = OK
    else:
        report.status = UNVERIFIED

    measured = [r for r in report.results if r.status != UNVERIFIED]
    report.reason = (
        f"{len(measured)} of {len(report.results)} declared dataset(s) measured; "
        f"{len(report.hits)} field name(s) do not resolve against the live source"
    )
    return report


def render(report: Report) -> str:
    head = [report.provenance.render()] if report.provenance is not None else []
    if report.provenance is not None and report.provenance.blocking:
        return "\n".join([*head, report.provenance.moved_detail()])

    out = [f"{report.status:<18} {report.reason or '—'}"]
    for result in report.results:
        out.append(
            f"  {result.id} [{result.status}] {len(result.live_fields)} live "
            f"field(s) via {result.delimiter or 'n/a'}, {result.matched} read "
            "name(s) resolved"
        )
        for hit in result.hits:
            if hit.status == FIELD_CASE_DRIFT:
                detail = f"code reads {hit.read!r}, source sends {hit.live!r}"
            else:
                detail = f"code reads {hit.read!r}, absent from the live header"
            silent = (
                " — .get(), so the miss is silent and the caller sees an empty result"
                if hit.form == "get"
                else " — subscript, so the miss raises"
            )
            out.append(f"    {hit.status:<17} {hit.site}:{hit.line}: {detail}{silent}")
        for line in result.unverified:
            out.append(f"    UNVERIFIED        {line}")
        for note in result.notes:
            out.append(f"    note              {note}")
    return "\n".join([*head, *out])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="schema_field_probe")
    ap.add_argument("--target", default=".", help="path to the MCP server repo")
    ap.add_argument(
        "--manifest",
        default=None,
        help=f"path to the field manifest (default: <target>/{DEFAULT_MANIFEST})",
    )
    ap.add_argument("--dataset", default=None, help="measure only this dataset id")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"{target}: not a directory", file=sys.stderr)
        return 127
    manifest_path = (
        Path(args.manifest).resolve() if args.manifest else target / DEFAULT_MANIFEST
    )

    prov = probe_provenance.capture(target)
    try:
        report = probe(target, manifest_path, only=args.dataset)
    except ManifestError as exc:
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
    if report.status == OK:
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
