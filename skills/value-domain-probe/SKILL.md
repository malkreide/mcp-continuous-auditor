---
name: value-domain-probe
description: Measure whether a column the code sends through int()/float() actually holds numbers in the live source, and report the measured share that does not — VALUE_DOMAIN_DRIFT with the proportion in the finding, UNVERIFIED when nothing could be concluded. Catches the privacy-suppression string ("1 bis 5"), NULL literals and empty cells that turn a tool call into "unexpected internal error". Reuses schema_fields.toml and the fetch of schema-field-probe. Deterministic and read-only; run it, do not reason about it.
requires:
  bins: [python]
  network: [the declared dataset URLs]
---

# Value-domain probe

Does a column the code coerces to a number actually hold numbers?

```bash
python scripts/value_domain_probe.py --target ../zh-education-mcp
python scripts/value_domain_probe.py --target . --dataset lernende --format json
python scripts/coverage_run.py --probe value-domain --manifest manifest.json \
    --repos-root ~/portfolio
```

Exit `0` numeric throughout a full read *or* every coercion guarded, `2`
**finding**, `3` NOT MEASURED, `4` the checkout moved, `127` the harness could
not run.

## Why

`zh-education-mcp` against `www.bista.zh.ch`, 2026-08-03: the code called `int()`
on `anzahl`. For small case counts the source publishes **`"1 bis 5"`** instead
of a number, to protect the individuals behind them — beside `"NULL"` and empty
cells. `int("1 bis 5")` raises, and the caller saw only «unerwarteter interner
Fehler».

Measured shares: **18.6 %** of 13 902 rows, **18.1 %** of 62 684, **1.0 %** of
35 903. A one-in-five chance of a crash is the endpoint's normal behaviour, and
no fixture could show it: a fixture carries the rows somebody chose, and nobody
chooses the suppressed ones.

## What it reads

The same `schema_fields.toml`, the same declared `file` + `symbol`, the same
fetch as `schema-field-probe` — imported, not re-implemented.

Columns handed to `int()`/`float()` in three accepted shapes (direct,
name-bound over one hop, wrapped when exactly one column is inside).
`int(float(x))` is attributed to the **inner** call, the one that meets the raw
string.

Values land in five buckets — `integer`, `fractional`, `empty`, `null_literal`,
`non_numeric`. `1'234` is an integer. Which buckets *offend* depends on the
column's own coercer: `int("12.5")` raises too, so `fractional` counts against
an `int()` column and not against a `float()` one.

## The truncation rule

| Found | Read | Verdict |
|---|---|---|
| something, any coercion unguarded | capped or full | **finding** — the share is reported with the row count it is over |
| something, every coercion guarded | capped or full | `VALUE_DOMAIN_HANDLED` — exit 0, share reported |
| nothing | full | clean |
| nothing | **capped** | `UNVERIFIED` — the tail was not read, and the suppressed rows cluster where nobody looked |

A partial final line is dropped before parsing: a row cut in half by the cap is
not a violation the source committed.

## The coercer may be the target's own helper

`int` and `float` are always coercers. A project that wraps the conversion in
one place — the good pattern, and this portfolio's own fix for `"1 bis 5"` — has
no `int()` left at any call site. Declare the helper by name:

```toml
[[dataset.coercer]]
name = "_parse_count"
tolerant = true      # returns a sentinel instead of raising
```

Without it, `zh-education-mcp` came back `NO_COERCION` for four of six datasets
and `anzahl` — the column the case history is about — was never measured.

## A guarded column is reported, not flagged

`VALUE_DOMAIN_HANDLED` (exit 0): every coercion of the column is inside a `try`
that catches the failure, or goes through a `tolerant` helper. The share still
prints — 18.6 % is a fact about the source either way — but a gate that reddens
on correctly handled code is switched off within a week. **One** unguarded call
site makes it a finding again.

## Notes, never the verdict

`COERCION_GUARDED` — one line per column and coercer, naming the call sites and
the share they absorb.

`COLUMN_NAME_DRIFT` — the column resolves only under normalisation. The domain is
measured against the live spelling; the name itself is `schema-field-probe`'s
finding. Two probes, one opinion each.

## Fixing a finding

Per bucket, because the remedies differ. `null_literal`/`empty` want a default
and a decision about the aggregate. `non_numeric` — `"1 bis 5"` — wants a
decision about what the **tool says**: the source withheld the number on purpose,
a zero is a lie and a crash is worse. `fractional` under `int()` is usually the
wrong coercer, not a wrong source.

Full write-up: [docs/probes/value-domain.md](../../docs/probes/value-domain.md)
