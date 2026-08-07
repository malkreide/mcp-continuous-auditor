# Schema-field probe

> Does the code read the field names the source is delivering right now?

`scripts/schema_field_probe.py`

## The case

`zh-education-mcp` against `www.bista.zh.ch`, 2026-08-03. The code read
`r["Schulgemeinde"]`. The source delivered `schulgemeinde`.

What came out was not an error. It was an empty hit list and the sentence
**«Schulgemeinde nicht gefunden»** — a failure wearing the costume of an answer.
A caller cannot tell that apart from a real absence, and neither can a model
summarising the result: the tool reported, successfully, that the thing does not
exist. Four of six datasets were affected, across eight tools.

Two of the datasets mix the spelling *inside one header row* —
`gebiet_Bezeichnung`, `staatsangehoerigkeit_ISO2_Code` — so "just lowercase
everything on read" is not the fix either. It is a second way to read a name
that is not there.

Every unit test stayed green throughout, and they had to: their fixtures pin the
old header. The suite compared the code's assumption against a recording of the
same assumption, so nothing in the repository could contradict it. Nothing in
the repository *was* the source.

## Why this is not the live probe

`scripts/live_probe.py` compares the live response against a **fixture** and
reports a structural diff. That catches the source changing under a recording.
It cannot catch this: the fixture and the live response can agree perfectly
while the code reads a name neither of them ever contained. The recording is not
what runs.

This probe compares the live response against the **code**. Those two are the
only pair whose disagreement produces a wrong answer at a user, and the fixture
sits outside it. Where a fixture is declared it is read too — but only to say
*why the tests stayed green*, never as the standard.

|  | reads | catches |
|---|---|---|
| `live_probe` | live ↔ fixture | the source changed under the recording |
| `schema_field_probe` | live ↔ **code** | the code reads a name the source does not send |

## The mapping is declared, never guessed

Which code reads which dataset is not derivable from the code, so it is stated
in `schema_fields.toml` in the target — URL, format, and the sites (`file` +
`symbol`) that read it. Format reference:
[`schema-fields.example.toml`](../../schema-fields.example.toml).

Same rule as [`reference-drift`](reference-drift.md): a guessed mapping produces
a finding nobody can retrace, and a finding nobody can retrace is how a gate
gets switched off. No manifest ⇒ `MANIFEST_MISSING`, exit 3, and the probe stops
there — a server that has not declared its datasets has not been checked, which
is a different sentence from "a server with no drift".

## What is read out of the code

Inside the declared symbol: every string literal used as a subscript key
(`row["anzahl"]`) or handed to `.get(...)` / `.pop(...)`. `symbol` resolves a
method as readily as a module-level function — the whole syntax tree is walked,
because naming a method as if it were a free function is the mistake
`reference_drift_probe` recorded from its own first field run.

The two forms are kept apart in the finding, because they fail differently and
a maintainer is looking for different things:

* `r["x"]` — the miss **raises**. The caller of `zh-education-mcp` saw
  "unexpected internal error", which is at least loud.
* `r.get("x")` — the miss returns `None`, the filter matches nothing, and the
  caller gets an empty list with a polite sentence. **That is the incident.**

## The corroboration rule

Extracting keys from a symbol over-collects by design: a function also indexes
dicts that are not records. Rather than guessing which dict is which — the same
guess the manifest exists to avoid — the over-collection is disarmed at the
comparison:

> A key is reported as `FIELD_MISSING` only when **at least one other key read
> at the same site does resolve** against the live header.

Five keys read, four in the header, the fifth absent: that is drift. Zero of
five: the site is not reading this dataset — the manifest is wrong, or the code
moved — and the probe reports it as unmeasured. A probe that produced five
findings there would be booking its own mismatched mapping as a defect of the
target.

So an over-broad `symbol` costs coverage, not correctness.

## The two findings

**`FIELD_CASE_DRIFT`** — the name exists in the live header under a different
spelling. Both are printed. The comparison is on a normalised form (casefolded,
separators removed), so it covers `Schulgemeinde`/`schulgemeinde` and
`gebiet_Bezeichnung`/`gebiet_bezeichnung` alike. Separators are in scope because
a source renaming `schul_gemeinde` to `schulgemeinde` has done the same thing to
the caller as a case change.

This is the sharper of the two: an unrelated dictionary key does not accidentally
normalise onto a column of the dataset the manifest points at.

**`FIELD_MISSING`** — the name is not in the live header in any spelling, at a
site that otherwise resolves. The column was renamed beyond recognition, or
dropped.

## Not measured is not clean

Every way this run can fail to conclude is `UNVERIFIED` and carries what was
seen:

| Seen | Why it is not a pass |
|---|---|
| the source is unreachable, times out, or does not parse | nothing was compared |
| the response contains **zero records** | a field list cannot be read from an empty list — and an empty list is also what the incident looked like from outside |
| a JSON body over the 5 MB cap, truncated | a field list read from half a document is not evidence |
| a CSV header with no line break in the first 64 KiB | the header was not read |
| the declared file or symbol is not in the checkout | the site was not read |
| no key at a site resolves | the corroboration rule — see above |

## Two notes, which never decide the verdict

**`MIXED_CASE_HEADER`** — one header row carries both all-lowercase columns and
columns with uppercase letters. Printed next to any finding on that dataset,
because it is what makes "normalise on read" a second wrong answer rather than a
fix. This is `gebiet_Bezeichnung` in the case history.

**`FIXTURE_PINS_OLD_HEADER`** — a declared fixture pins field names the source no
longer sends. Not a finding: the fixture is not what runs. It is the sentence
that explains why a suite full of green tests said nothing about any of this, and
it is worth printing even on a repository whose code is currently aligned —
that fixture is the next incident's hiding place.

## Cheap on the source, and read-only

Every request is a GET. For a CSV only the first 64 KiB are read: the header is
the whole question, and downloading a 60 MB extract to look at line one makes
the check expensive enough that somebody switches it off. The delimiter is
sniffed when not declared and **reported either way** — reading a
semicolon-separated header as one comma-separated column produces a single
enormous "field name" that matches nothing, which would look exactly like total
drift.

## Running it

```bash
python scripts/schema_field_probe.py --target ../zh-education-mcp
python scripts/schema_field_probe.py --target . --dataset schulgemeinden --format json
python scripts/coverage_run.py --probe schema-field --manifest manifest.json \
    --repos-root ~/portfolio
```

| Exit | Meaning |
|---|---|
| 0 | `SCHEMA_OK` — every declared read resolves against the live header |
| 2 | finding — `FIELD_MISSING` or `FIELD_CASE_DRIFT` |
| 3 | not measured — `UNVERIFIED` or `MANIFEST_MISSING` |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run — no target, or a manifest that does not parse |

An unreadable manifest is 127 and not 3 on purpose: a broken mapping is the
operator's problem with the probe, not an observation about the server.
