# What has actually been measured — and against what

> The auditor is not deployed. Nothing here has ever spoken to a live MCP server.

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
| pr-health | ✅ | ✅ GitHub API, daily | — | n/a |
| spec | ✅ | ✅ this repo | — | ❌ **never** |
| transport boot | ✅ | ✅ locally launched checkouts | — | ❌ — it starts the server itself |
| rebind | ✅ | ✅ locally launched checkouts | — | ❌ |
| live probe / recall canary | ✅ | — | — | ❌ — template only |

`n/a` means the probe reads source or a catalogue and has no wire to speak to.
`❌` means there is a wire and nobody has ever put a request on it.

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
