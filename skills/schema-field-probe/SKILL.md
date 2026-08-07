---
name: schema-field-probe
description: Compare the field names a server's code reads out of a response against the names the source delivers live — FIELD_CASE_DRIFT when the column exists under a different spelling, FIELD_MISSING when it is gone, UNVERIFIED when the source, the site or the mapping could not be read. The mapping comes from a declared schema_fields.toml and is never guessed. Catches the failure that returns an empty result list instead of an error. Deterministic and read-only; run it, do not reason about it.
requires:
  bins: [python]
  network: [the declared dataset URLs]
---

# Schema-field probe

Does the code read the field names the source is delivering right now?

```bash
python scripts/schema_field_probe.py --target ../zh-education-mcp
python scripts/schema_field_probe.py --target . --dataset schulgemeinden --format json
python scripts/coverage_run.py --probe schema-field --manifest manifest.json \
    --repos-root ~/portfolio
```

Exit `0` aligned, `2` **finding**, `3` NOT MEASURED, `4` the checkout moved
during the run, `127` the harness could not run.

## Why

`zh-education-mcp` against `www.bista.zh.ch`, 2026-08-03: the code read
`r["Schulgemeinde"]`, the source delivered `schulgemeinde`. The result was not
an error but an empty hit list and «Schulgemeinde nicht gefunden» — a failure
wearing the costume of an answer, indistinguishable from a real absence. Four of
six datasets, eight tools.

Every unit test stayed green, because the fixtures pin the old header: the suite
compared the code's assumption against a recording of the same assumption.

## Why this is not the live probe

`live_probe` compares live ↔ **fixture**. This compares live ↔ **code**. The
fixture and the live response can agree perfectly while the code reads a name
neither ever contained — and the code is what runs. Where a fixture is declared
here it is read only to report *why the tests stayed green*.

## The mapping is declared, never guessed

`schema_fields.toml` in the target names each dataset's URL, format and the
sites (`file` + `symbol`) that read it. Format reference:
[schema-fields.example.toml](../../schema-fields.example.toml). No manifest ⇒
`MANIFEST_MISSING`, exit 3 — a server that has not declared its datasets has not
been checked, which is a different sentence from one with no drift.

## The corroboration rule

A key is `FIELD_MISSING` only when at least one **other** key at the same site
resolves. Zero of five resolving means the site is not reading that dataset —
the manifest is wrong or the code moved — and the probe measures nothing there
rather than reporting its own mismatched mapping as five defects of the target.

An over-broad `symbol` therefore costs coverage, not correctness.

## The finding names the form, because they fail differently

* `r["x"]` — the miss **raises**; the caller saw "unexpected internal error".
* `r.get("x")` — the miss returns `None`, the filter matches nothing, and the
  caller gets an empty list with a polite sentence. That is the incident.

## Not measured is not clean

Source unreachable, response unparseable, **zero records** in the response (a
field list cannot be read from an empty list), a truncated body, a header longer
than the byte window, a declared file or symbol that is not there, a site that
resolves nothing — all `UNVERIFIED`, each with what was seen.

## Two notes, never the verdict

`MIXED_CASE_HEADER` — one header mixes `gebiet_Bezeichnung` with lowercase
columns, so normalising on read is a second wrong name, not a fix.

`FIXTURE_PINS_OLD_HEADER` — the declared fixture pins names the source no longer
sends. Printed even when the code is currently aligned: that fixture is the next
incident's hiding place.

Full write-up: [docs/probes/schema-field.md](../../docs/probes/schema-field.md)
