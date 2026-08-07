# Value-domain probe

> Does a column the code coerces to a number actually hold numbers?

`scripts/value_domain_probe.py`

## The case

`zh-education-mcp` against `www.bista.zh.ch`, 2026-08-03. The code called `int()`
on the column `anzahl`. For small case counts the source does not publish a
number — to protect the individuals behind them it publishes the string
**`"1 bis 5"`**. Beside that: `"NULL"`, and empty cells.

`int("1 bis 5")` raises. The caller of the tool saw «unerwarteter interner
Fehler» and nothing else. No column, no value, no hint that the source was doing
something deliberate and documented.

The shares are the reason this is worth a probe rather than a one-off fix:

| rows in the extract | affected | share |
|---:|---:|---:|
| 13 902 | 2 586 | **18.6 %** |
| 62 684 | 11 346 | **18.1 %** |
| 35 903 | 359 | **1.0 %** |

A one-in-five chance of a crash is not an edge case. It is the normal behaviour
of the endpoint. And no test in the repository could see it: a fixture carries
the rows somebody chose while writing the fixture, and nobody chooses the
suppressed ones.

## The two halves, and the one it shares

The datasets, the sites that read them and the fetch are the same as
[`schema-field`](schema-field.md)'s, so this probe imports that module outright
rather than keeping a second copy of the manifest reader, the HTTP GET, the
delimiter sniffing and the record resolution. Same manifest file
(`schema_fields.toml`), same declared `file` + `symbol`.

**From the code:** which columns are handed to `int()` or `float()` inside the
declared symbol. Three shapes are accepted, because they are ways of writing one
thing and a check that takes only the first is measuring who wrote the line:

```python
total = int(row["anzahl"])          # direct
raw = row["anzahl"]; total = int(raw)   # name-bound, one hop, no chains
total = int(row["anzahl"] or 0)     # wrapped — accepted when exactly one column is inside
```

`int(float(row["anzahl"]))` is attributed to the **inner** call, which is the one
that meets the raw string. Recording the outer one too would report every such
column as holding fractional values it is not being asked to parse as integers.

**From the source:** every value in that column, in five buckets — `integer`,
`fractional`, `empty`, `null_literal`, `non_numeric`. `1'234` and `1 234` are
integers; reading Swiss thousands separators as prose would drown the real
finding in noise.

Which buckets count against a column depends on **its** coercer: `int("12.5")`
raises just as `int("1 bis 5")` does, so `fractional` offends a column the code
sends through `int()` and not one it sends through `float()`.

## The truncation rule

Reading all of a 60 MB extract to count strings would make this check expensive
enough that somebody switches it off, so the read is capped (`--max-bytes`,
default 8 MiB; `--max-rows`, default 100 000). What the cap costs is precisely
what *clean* is allowed to mean:

| Found | Read | Verdict |
|---|---|---|
| values outside the domain, at least one coercion unguarded | capped or full | **finding** — a share over 50 000 rows is a measurement, and the report says how many rows it is over |
| values outside the domain, every coercion guarded | capped or full | `VALUE_DOMAIN_HANDLED` — exit 0, share reported |
| nothing | full | clean |
| nothing | **capped** | `UNVERIFIED` — the tail was not read |

The last row is the whole discipline of this directory in one line. The
suppressed rows cluster exactly where nobody looked; a capped read that found
nothing has established nothing.

A partial final line is dropped before parsing. A row cut in half by the byte cap
would otherwise be classified as a domain violation the source never committed —
the probe inventing its own finding out of its own budget.

## The coercer may be the target's own helper

`int` and `float` are always coercers. A project that wraps the coercion in one
place — which is the *good* pattern, and was this portfolio's own fix for the
`"1 bis 5"` incident — has no `int()` at any call site left, and becomes
invisible to a probe that only knows the builtins.

So the helper is declared, by name, beside the sites:

```toml
[[dataset.coercer]]
name = "_parse_count"
tolerant = true      # returns a sentinel instead of raising
```

Measured against `zh-education-mcp` on 2026-08-07: without this block the probe
reports `NO_COERCION` for four of six datasets and never sees `anzahl`, the
column the whole case history is about. With it, all six are measured.

## What it does not claim

**A column whose every coercion is guarded is not a finding.** Where the call
sits inside a `try` that catches the failure, or goes through a helper declared
`tolerant`, the code has answered the question this probe asks. The status is
**`VALUE_DOMAIN_HANDLED`** — exit 0, with the share still measured and printed,
because 18.6 % is a fact about the source either way.

A gate that reddens on correctly handled code is switched off within a week, and
takes the unguarded columns with it. **One** unguarded call site is enough to
make the column a finding again.

A `try` catching `KeyError` or an HTTP error is not a guard for this and is not
counted as one — that would silence exactly the call sites that crash.

Whether the absorbed rows are visible in what the tool finally reports is a real
question and a different one. This probe does not answer it.

**Whether the column name is right** is [`schema-field`](schema-field.md)'s
question. When a coerced column does not resolve against the live response at
all, this run reports that it could not measure the domain and points there. When
it resolves only under normalisation (`Anzahl` for `anzahl`), the domain *is*
measured against the live spelling and a `COLUMN_NAME_DRIFT` note hands the name
itself to the other probe. Two probes, one opinion each.

**A share is about the response that was fetched.** The URL, the row count and
the truncation state are printed with every number, so a reader can re-fetch
exactly what was measured.

## Fixing a finding

The remedies differ per bucket, which is why the buckets are separate:

* `null_literal` and `empty` want a default, and a decision about whether the
  default is a zero or an omission from the aggregate.
* `non_numeric` — `"1 bis 5"` — wants a decision about what the **tool should
  say**. The source withheld the number on purpose; a zero is a lie and a crash
  is worse. Carrying the string through to the caller with the reason is the only
  answer that keeps the tool's output true.
* `fractional` under `int()` is usually a wrong coercer, not a wrong source.

## What it found, run for real

`zh-education-mcp` against `www.bista.zh.ch`, **2026-08-07**, six datasets,
122 379 rows, full reads throughout. The hand counts of 2026-08-03 are
reproduced:

| dataset | rows | share outside the domain | bucket |
|---|---:|---:|---|
| `sek1_anforderungstyp` | 13 902 | **18.6 %** | `non_numeric` — `"1 bis 5"` |
| `staatsangehoerigkeit_regional` | 62 684 | **18.1 %** | `non_numeric` |
| `wohnort` | 35 903 | **1.0 %** | `null_literal` |
| `maturitaetsquote` | 1 981 | **16.3 %** | `empty` |
| `uebersicht_alle_lernende` | 3 192 | 0.0 % | — |
| `mittelschulen` | 4 717 | 0.0 % | — |

The first three are the shares the case history records, measured four days
later by this code rather than by hand; the row counts match exactly and the
affected counts differ by a few dozen, which is the source moving, not the
method disagreeing.

The fourth was not in the case history at all. `maturitaetsquote_gymnasial` is
empty for 323 of 1 981 municipalities, and the sort key maps those to `-1.0`,
so they land at the bottom of every ranked table — correct behaviour, and
nowhere written down before this run.

Verdict `VALUE_DOMAIN_HANDLED`, exit 0: every coercion of every one of those
columns is guarded. That is the repository having fixed the incident, and the
probe saying so with the number rather than with silence.

## Running it

```bash
python scripts/value_domain_probe.py --target ../zh-education-mcp
python scripts/value_domain_probe.py --target . --dataset lernende --format json
python scripts/value_domain_probe.py --target . --max-bytes 33554432
python scripts/coverage_run.py --probe value-domain --manifest manifest.json \
    --repos-root ~/portfolio
```

| Exit | Meaning |
|---|---|
| 0 | `VALUE_DOMAIN_OK` — every coerced column is numeric throughout a full read |
| 0 | `VALUE_DOMAIN_HANDLED` — values outside the domain, every coercion guarded |
| 2 | finding — `VALUE_DOMAIN_DRIFT` |
| 3 | not measured — no manifest, `NO_COERCION`, source unreadable, a capped read with nothing found, or a column that does not resolve |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |

`NO_COERCION` is exit 3 and not 0: a dataset whose declared sites never call
`int()` or `float()` is one this probe measured nothing about, not one it found
clean.
