# What has actually been measured — and against what

> The auditor is not deployed. Nothing here has ever spoken to a live MCP **server**
> — though as of 2026-08-07 two probes have spoken to a live **data source**, and
> `live_schedule_probe` has now been read three times against the same five
> repositories: before a remediation, after it, and against the scheduled runs
> it prescribed (2026-08-10).

This page applies the repository's own rule to the repository itself.

Every probe in `docs/probes/` distinguishes three answers and never two: **clean**,
**finding**, and **not measured**. `portfolio_scan` goes further and refuses an
overall verdict when a sweep did not cover what it claimed to cover, because
«we did not look» and «we looked and found nothing» read identically in a report
and are not the same statement.

That rule has never been applied one level up, to the auditor. It is applied here.
A green test suite is a green **fixture**; it is not evidence that a probe has
ever met a server it did not itself start.

## The blocker

**`mcp-continuous-auditor` has no deployment.** There is no Tier-0 host, no
Raspberry Pi, no Worker microVM — see
[`docs/deployment/tier-0.md`](../deployment/tier-0.md) for what standing one up
involves. Everything that needs a *host* is therefore written, tested and unrun:

* `scripts/nightly-audit.sh` and `openclaw/cron/nightly-audit.json` — the daily
  deep audit of one target. Never installed anywhere.
* the Worker/Broker pipeline under `deploy/microvm/` — never provisioned.
* `.github/workflows/live-probe.yml.template` and
  `redteam-regen.yml.template` — these are templates **for the target repo**, and
  ship as `.yml.template` precisely so GitHub does not run them here. They have
  not been activated in any target repo either.

Two schedules *do* run, both inside GitHub Actions and needing no host:
`pr-health.yml` (daily) and `telegram-intake.yml` (polling). Neither touches a
live server.

## The coverage table

What each probe has been exercised against. The rightmost column is the one this
page exists for, and it is empty on purpose.

| Probe | Unit fixtures | A real repository | A package index | A **live server, over the wire** |
|---|---|---|---|---|
| identity | ✅ | ✅ portfolio sweep, 30 servers (2026-07-29) | — | n/a |
| shipped | ✅ | ✅ | ✅ PyPI | ❌ |
| yank | ✅ | — | ✅ PyPI | n/a |
| published | ✅ | — | ✅ PyPI | ❌ |
| lockfile | ✅ | ✅ | — | n/a |
| doc-claim | ✅ | ✅ this repo | — | n/a |
| parity | ✅ | ✅ this repo | — | n/a |
| reference-drift | ✅ | ✅ 19 sites in 18 repos — see below | — | n/a |
| live-schedule | ✅ | ✅ 5 portfolio servers, three readings (2026-08-07/10) — see below | — | n/a |
| schema-field | ✅ | ✅ `zh-education-mcp`, 6/6 datasets, twice (2026-08-07) | — | ✅ `www.bista.zh.ch`, live — see below |
| value-domain | ✅ | ✅ `zh-education-mcp`, 6/6 datasets, 122 379 rows (2026-08-07) | — | ✅ `www.bista.zh.ch`, live — see below |
| pr-health | ✅ | ✅ GitHub API, daily | — | n/a |
| spec | ✅ | ✅ this repo | — | ❌ **never** |
| transport boot | ✅ | ✅ locally launched checkouts | — | ❌ — it starts the server itself |
| rebind | ✅ | ✅ locally launched checkouts | — | ❌ |
| live probe / recall canary | ✅ | — | — | ❌ — template only |

`n/a` means the probe reads source or a catalogue and has no wire to speak to.
`❌` means there is a wire and nobody has ever put a request on it.

The last column holds two different wires, and the distinction matters. Most rows
point at a deployed **MCP server**, and that column is still empty because there
is no deployment. The `schema-field` and `value-domain` rows point at a **data
source** — the cantonal endpoint a server reads — and those were reached for real
on 2026-08-07. A live
run against `www.bista.zh.ch` says nothing about whether any MCP server boots.

## The full run, and what it found: `reference_drift_probe`

Run against the real `reference/` of `mcp-data-source-probe-skill` and 18
adoption sites in 17 server repositories, over the manifest committed there.
Coverage 19 of 19 — no `UNVERIFIED` entry, so nothing below rests on a site that
was not read.

**How the mapping was established, since a guessed one is refused by design.**
41 repositories were swept. A grep on the obvious markers over-collects —
`httpx.HTTPStatusError` matches any httpx caller — so candidates were ranked on
the fingerprint that makes *this* template recognisable: a bounded retry loop,
BOTH httpx error classes handled, and the 4xx-except-429 carve-out
re-implemented by hand. `file` and `symbol` were then read out of each server's
own code. Five entries named a method as if it were a module-level function; the
probe reported them `SITE_UNREADABLE` rather than silently passing, and they were
corrected before the manifest was committed.

**What it found.** Five `REFERENCE_STALE`, no `REFERENCE_UNADOPTED`:

| Property | Sites that have it |
|---|---|
| `reads_retry_after` | 0 of 18 |
| `jitters` | 0 of 18 |
| `caps_after_jitter` | 0 of 18 |
| `wall_clock_budget` | 7 of 18 |
| `no_bare_runtime_error` | 15 of 18 |

The last two are the case this probe was built for: fixes made downstream, one
repository at a time, that never came back to the template. The first three are
something the case history did not predict — a gap across the whole portfolio,
which the report states as "declared and implemented nowhere" rather than
implying the servers are ahead.

**Still not exercised outside the tests: the unanimity layer.** It had 18
readable sites and reported nothing, because unanimity across 18 independently
written implementations is a high bar and none of the differences cleared it —
15 of 18 removed the bare `RuntimeError`, not 18 of 18. On this portfolio the
declared properties are doing the work, and the layer that needs no declaration
has still never produced a finding against real code.

## The full run, and what it found: `live_schedule_probe`

The probe was written from a hand sweep of ten portfolio servers on 2026-08-03,
which found five with a scheduled live run and five without. On **2026-08-07** it
was run against the five «without» — the first time it measured anything but its
own fixtures.

**Four of five confirmed. The fifth was a false finding, and it was the
probe's.**

| target | hand sweep, 2026-08-03 | probe, 2026-08-07 |
|---|---|---|
| `zh-education-mcp` | no scheduled run | `LIVE_UNSCHEDULED` ✓ |
| `swiss-transport-mcp` | no scheduled run | `LIVE_UNSCHEDULED` ✓ |
| `register-mcp` | no scheduled run | `LIVE_UNSCHEDULED` ✓ |
| `fedlex-mcp` | no scheduled run | `LIVE_UNSCHEDULED` ✓ |
| `swiss-snb-mcp` | no scheduled run | **`LIVE_SCHEDULED_SILENT`** — it runs nightly |

`swiss-snb-mcp` has run its live suite every night since it was written, through
two steps that read `python tests/test_live_scenarios.py`. No pytest on the line,
so the probe recognised neither a pytest call nor an opaque wrapper and concluded
absence — the direction its own docstring calls the dangerous one. Such a call is
now resolved against the checkout, and the server's real defect is what the probe
reports instead: the nightly run exists, and nothing in it reacts to `failure()`.

The hand sweep made the same mistake, which is why the two agreed. **Two readers
agreeing is not two measurements.**

Against this repository the probe reports `NO_LIVE_TESTS` — correctly, the
auditor's own suite is stdlib `unittest` with no `live` marker anywhere.

### The second reading, after the remediation landed

The five pull requests merged the same day, and the probe was run again against
each repository's `main`. **All five flipped**, which is the first time anything
in this directory has measured the same target before and after a change:

| target | first reading | remediation | second reading |
|---|---|---|---|
| `zh-education-mcp` | `LIVE_UNSCHEDULED` | new `live-tests.yml` | `LIVE_SCHEDULED` |
| `swiss-transport-mcp` | `LIVE_UNSCHEDULED` | new `live-tests.yml` | `LIVE_SCHEDULED` |
| `register-mcp` | `LIVE_UNSCHEDULED` | new `live-tests.yml` | `LIVE_SCHEDULED` |
| `fedlex-mcp` | `LIVE_UNSCHEDULED` | new `live-tests.yml` | `LIVE_SCHEDULED` |
| `swiss-snb-mcp` | `LIVE_SCHEDULED_SILENT` | notification in the *existing* nightly job | `LIVE_SCHEDULED` |

The last row is the one worth reading twice. Its remediation was **not** a second
cron — the nightly job had been running since the repository was written. What it
gained was the half `DRIFT-005` names beside the schedule: somebody sees a red
result. A probe that had only the two states «scheduled / not scheduled» would
have prescribed a duplicate nightly run against `data.snb.ch` and called that a
fix.

`swiss-transport-mcp`'s second reading is `LIVE_SCHEDULED` and its **first
scheduled run will be red**: the live tests skip without `TRANSPORT_API_KEY`,
which the repository does not yet hold. That is the correct answer — a secret
nobody set is not a green contract with the source — but it is also the reason
this row is a mechanism and not yet a measurement.

### The third reading: the first Monday

The second reading said a cron exists and would be visible. **2026-08-10 is the
day that claim became checkable**, because the crons fired for real. All five
ran, and the two things the probe cannot see — whether a workflow does what its
YAML says, and whether the notification path works — are now measured.

| target | run | classified state | issue |
|---|---|---|---|
| `zh-education-mcp` | 06:46 | **`finding`** | #44, third comment |
| `swiss-transport-mcp` | 06:41 | **`unknown`** — «alle 7 Test(s) uebersprungen» | none, correctly |
| `register-mcp` | 06:52 | `clear` | — |
| `fedlex-mcp` | 06:53 | `clear` | — |
| `swiss-snb-mcp` | nightly, 08-08/09/10 | `clear` ×3 | — |

**No `LIVE_SCHEDULED` was contradicted.** Five for five, the workflow fired,
installed, ran the suite and reached a verdict. That gap is closed.

#### The notification path, which was fixture-tested only

Two promises were made in YAML and never observed:

* **A `finding` opens one issue and then comments.** `zh-education-mcp` has been
  red since 2026-08-08. There is exactly **one** issue — #44, label `upstream`,
  opened by `github-actions` — with **three comments**, one per red run. Not four
  issues. The stable-prefix lookup does what it was written for.
* **An `unknown` opens nothing and closes nothing.** `swiss-transport-mcp`
  classified `unknown` and has **zero** open issues. The run went red, the
  Actions tab shows it, and no issue claims a comparison that never happened.

Both held.

#### The `finding` is real, and today it is not an upstream break

Read the two red runs against each other, because they are different things
under one issue thread:

* **2026-08-08:** every endpoint returned `502 Bad Gateway`, 14 of 15 live tests
  down. That is the source being out — exactly what the issue body says a red run
  usually means.
* **2026-08-10:** 14 passed, **1 failed**. All twelve field-checking live tests
  passed, so the contract with BISTA holds. The failure is
  `test_live_a_dns_hiccup_costs_an_attempt_not_the_call` — `assert 3 == 2`, a
  test about the retry loop's own DNS behaviour on the runner.

So the sentence «rot heisst nicht zwingend unser Fehler» earned its place twice
over, in opposite directions: once the source was down, once the suite's own
environment assumption was.

#### Two things the first Monday also measured

**GitHub's cron delay is over an hour.** Every one of the five started 70–96
minutes after its scheduled minute (05:19 → 06:41, 05:23 → 06:46, 05:31 → 06:52,
05:43 → 06:53; the nightly 03:17 → 04:27/04:36/04:53). The odd-minute choice was
about not colliding at `:00` and that reasoning is untouched — but anything that
schedules a *check* on one of these runs has to allow for the hour, not for the
minute.

**`zh-education-mcp` runs a different classifier from its four siblings.** It was
the first repository remediated, before the JUnit-based
`scripts/classify_live_run.py` existed, so its classification is still a `case`
block inline in YAML. Two consequences visible in today's log:

* its `finding` branch sets no `reason`, so the run printed `Live-Suite: finding`
  followed by an empty line;
* it classifies on pytest's exit code, so it cannot see the **all-skipped** case
  — the one that made the script necessary in the first place.

Neither bit today (its live tests do not skip). It is drift between siblings,
introduced by building one before the design was finished, and it is written down
here rather than left to be rediscovered.

### What is still not covered

* **No `UNVERIFIED` branch of the probe has fired against a real target.** The
  opaque-command and undecidable-marker paths are fixture-tested only — and they
  are the two that keep a false finding from being printed. (The opaque path now
  *appears* in four real reports, against the `python scripts/classify_live_run.py`
  step, but it is never the verdict there because a pytest call was found in the
  same job.)
* **The `hollow_scripts` branch has never fired.** A live test file executed as
  a script *without* a `__main__` block is the sharpest finding this probe can
  make; `swiss-snb-mcp` had the block, so the branch is fixture-tested only.
* **No issue has been closed by a recovering run.** The `clear` branch that
  comments and closes has not been exercised: the three green targets never had
  an issue to close, and `zh-education-mcp` has not gone green yet. Half the
  notification path is still only a promise.
* **`swiss-transport-mcp` still has no live measurement.** `TRANSPORT_API_KEY` is
  unset, so its weekly run reports `unknown` and will keep doing so. The
  mechanism is verified there; the contract with `opentransportdata.swiss` is not.

## The full run, and what it found: `schema_field_probe` + `value_domain_probe`

**2026-08-07, `zh-education-mcp` against `www.bista.zh.ch`.** The first time
anything in this repository put a request on a data source rather than on a
package index or a server it started itself. Both probes read the same committed
`schema_fields.toml`; coverage was 6 of 6 declared datasets for both, with no
`UNVERIFIED` dataset in either run, so nothing below rests on something that was
not read.

### What the manifest cost, and what writing it taught

Two things the probes could not express turned up while the manifest was being
written, and both were fixed in the probes rather than worked around in the
manifest:

* **`normalised`.** `zh-education-mcp` lowercases every key once at fetch time
  (`_normalise_keys`), so the code does not read the header the wire sent.
  Against the raw header the schema-field probe reported **six** findings — one
  real, five invented by the comparison. Declared, it reports one.
* **`[[dataset.coercer]]`.** The same repository wraps every numeric conversion
  in `_parse_count`, so no `int()` remains at any call site. The value-domain
  probe reported `NO_COERCION` for four of six datasets and never saw `anzahl`
  — the column the entire case history is about. Declared by name, all six are
  measured.

Both are the same shape of gap: a repository that *fixed* the incident properly
became unreadable to the probe written from the incident. Neither was
predictable from the fixtures.

### schema-field: one finding

```
FIELD_CASE_DRIFT  tools.py::zh_edu_maturitaetsquote:608:
  code reads 'Total_19_Jahre_alt', source sends 'total_19_jahre_alt'
  — .get(), so the miss is silent and the caller sees an empty result
```

The incident's own residue. The 2026-08-03 fix normalised every key; this one
call site kept the old spelling. It raises nothing and logs nothing, and the
"19-Jährige" column of the rendered table has been a dash for every row of every
response since. Five other datasets: `SCHEMA_OK`. Two `MIXED_CASE_HEADER` notes
confirmed the case history against the live header
(`staatsangehoerigkeit_ISO2_Code`; `gebietstyp_Code` / `gebiet_Code` /
`gebiet_Bezeichnung`).

### value-domain: the hand counts reproduced, plus one that was not recorded

122 379 rows, full reads throughout — no truncation, so a 0.0 % is a measured
zero rather than a budget that ran out.

| dataset | rows | share | bucket | hand count, 2026-08-03 |
|---|---:|---:|---|---|
| `sek1_anforderungstyp` | 13 902 | **18.6 %** | `non_numeric` | 18.6 %, 13 902 rows |
| `staatsangehoerigkeit_regional` | 62 684 | **18.1 %** | `non_numeric` | 18.1 %, 62 684 rows |
| `wohnort` | 35 903 | **1.0 %** | `null_literal` | 1.0 %, 35 903 rows |
| `maturitaetsquote` | 1 981 | **16.3 %** | `empty` | *not recorded* |
| `uebersicht_alle_lernende` | 3 192 | 0.0 % | — | — |
| `mittelschulen` | 4 717 | 0.0 % | — | — |

The row counts match the hand counts exactly; the affected counts differ by a few
dozen, which is four days of the source moving rather than the two methods
disagreeing. The fourth row was in nobody's notes: `maturitaetsquote_gymnasial`
is empty for 323 of 1 981 municipalities.

Verdict `VALUE_DOMAIN_HANDLED`, exit 0 — every coercion of every one of those
columns is guarded. That is the repository having fixed the incident, and the
probe saying so **with the number** instead of with silence. The `HANDLED` status
exists because of this run: the previous code would have gone red on four
correctly handled datasets, which is how a gate gets switched off.

### The second reading, after the finding was fixed

`zh-education-mcp` fixed `Total_19_Jahre_alt` the same day, and the probe was run
again against its `main`:

```
SCHEMA_OK   6 of 6 declared dataset(s) measured;
            0 field name(s) do not resolve against the live source
```

Both `MIXED_CASE_HEADER` notes are still there, which is the point of a note: the
source has not changed its habits, and the next reader of that code needs to know
that lowercasing on read is a second wrong name rather than a fix.

`value_domain_probe`, second reading against the same commit:
`VALUE_DOMAIN_HANDLED`, the same four shares (18.6 %, 18.1 %, 16.3 %, 1.0 %) over
the same row counts. A stable number four days running is not nothing — it is the
difference between «the source suppresses small counts» as a claim and as a
measurement.

### `FIXTURE_PINS_OLD_HEADER`: the reader ran, the finding could not

`zh-education-mcp` gained recorded CSV fixtures on 2026-08-07
(`tests/fixtures/`, with a `PROVENANCE.md` naming the recording date). The
committed `schema_fields.toml` does **not** declare them yet, so the probe was
run against a local manifest with all six `fixture =` lines added.

Result: all six fixtures were read and compared, and **no** finding — correctly,
because they were recorded from the same source on the same day. So:

* the branch that **reads** a declared fixture has now run against real recorded
  files, six of them;
* the branch that **reports** a stale one still has not fired, and could not
  have. A fixture recorded today cannot pin yesterday's header.

That is a weaker statement than «exercised», and it is the true one. The finding
direction stays unconfirmed until a fixture has had time to go stale — or until
the same measurement runs against a repository whose fixtures are older.

Declaring the six in the committed manifest is the obvious next step and is not
done here; this page records what was measured, not what was configured.

### What is still not covered

* **Only one target.** Six datasets in one repository. No other server in the
  portfolio ships a `schema_fields.toml`, so the manifest-writing cost above has
  been paid exactly once and the two gaps it exposed may not be the last two.
* **No JSON source has been read live.** Both probes support it; every dataset
  measured so far is CSV.
* **No `FIELD_MISSING` has been produced against a real target.** The one live
  finding so far was `FIELD_CASE_DRIFT`. The sharper-sounding of the two codes is
  fixture-tested only.

## The named gap: `spec_probe --url`

The one that matters for the `2026-07-28` migration. Its `wire` source is the
only one that is **evidence** rather than a claim — the other three (`code`,
`artifact`, `portfolio`) are all somebody's assertion about what a server does —
and it has never been run against a deployment.

Verified so far: both local fixtures (`tests/fixtures/stateless_http_server.py`
and `boot_http_server.py`), covering the migrated and the legacy shape. That
proves the probe reads a wire correctly. It proves nothing about
`zurich-opendata-mcp`.

To close it, against a deployed target:

```bash
python scripts/spec_probe.py \
  --url https://<domain>/mcp \
  --target ../zurich-opendata-mcp \
  --installed \
  --now <YYYY-MM-DD> \
  --format json --report spec-report.json
```

Read the result against [`spec.md`](spec.md): exit `2` with `LEGACY_TRANSPORT` is
the expected outcome for a `/sse` deployment; exit `0` means the server is ahead
of `portfolio.json` and the tracker is what needs updating; a `UNVERIFIED wire:`
line means the endpoint was never reached and **nothing was measured** — fix the
URL and re-run rather than recording it as a result.

## The coverage layer itself has never seen the real manifest

Added 2026-08-06: `scripts/coverage.py` (the manifest reader and the
denominator) and `scripts/coverage_run.py` (one probe over every manifest
entry). They are the answer to the sweep that reported «33 von 33 ok» against 43
active servers, and the same rule applies to them as to everything else on this
page.

**Run:** the unit fixtures, `tests/test_coverage.py` and
`tests/test_coverage_run.py` — including the three counter-checks the mechanism
exists for (a deliberately absent entry exits non-zero *and* names itself; a
reasoned skip exits 0 *and* prints its reason; an empty manifest aborts instead
of reporting `0/0 ok`). Plus `tests/test_nightly_sweep.py`, which lifts the real
`sweep_over_manifest` out of `scripts/nightly-audit.sh` and drives it in bash
with a stubbed child.

**Not run:** against the actual `coverage_manifest.py --format json` output of
`swiss-public-data-mcp`. Every manifest this repository has read so far was
written by a test. The validation is deliberately fail-closed for exactly that
reason — a field this tool does not recognise stops the run rather than turning
every entry into a justified omission — but «the fixture agreed» is all the
green above it means, same as every other row here.

To close it: run any probe through the driver against a real manifest and a real
`--repos-root`, and record the `n/44 abgedeckt` line with its date.

## Why this is written down rather than remembered

A probe suite that has only ever run against its own fixtures is in exactly the
state `identity_probe` was written to catch: everything green, nothing
contradicted, and no claim in it anchored to something outside itself. The
difference between this repository and the servers it audits is not that this one
has been verified — it is that this one says so.

When the auditor is deployed, this page is where the ❌ column gets filled in,
one row at a time, with the date and the run that did it. Until then, every green
badge above it means «the fixture agreed», and that is all it means.
