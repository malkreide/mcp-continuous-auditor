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
| values outside the domain | capped or full | **finding** — a share over 50 000 rows is a measurement, and the report says how many rows it is over |
| nothing | full | clean |
| nothing | **capped** | `UNVERIFIED` — the tail was not read |

The last row is the whole discipline of this directory in one line. The
suppressed rows cluster exactly where nobody looked; a capped read that found
nothing has established nothing.

A partial final line is dropped before parsing. A row cut in half by the byte cap
would otherwise be classified as a domain violation the source never committed —
the probe inventing its own finding out of its own budget.

## What it does not claim

**A guarded coercion is a note.** Where the call sits inside a `try` whose
handler catches `ValueError` / `TypeError` / `Exception` (or is bare), the report
says `COERCION_GUARDED`. A `try` catching `KeyError` or an HTTP error is not a
guard for this and is not counted as one — that would silence exactly the call
sites that crash.

The share is still printed, and the dataset is still a finding. The guard
answers "does this raise"; it does not answer "is one row in five silently
dropped from the total the tool reports". That second question is real and this
probe is not the one to absolve it.

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
| 2 | finding — `VALUE_DOMAIN_DRIFT` |
| 3 | not measured — no manifest, `NO_COERCION`, source unreadable, a capped read with nothing found, or a column that does not resolve |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |

`NO_COERCION` is exit 3 and not 0: a dataset whose declared sites never call
`int()` or `float()` is one this probe measured nothing about, not one it found
clean.
