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
| reference-drift | ✅ | ⚠️ template side only — see below | — | n/a |
| pr-health | ✅ | ✅ GitHub API, daily | — | n/a |
| spec | ✅ | ✅ this repo | — | ❌ **never** |
| transport boot | ✅ | ✅ locally launched checkouts | — | ❌ — it starts the server itself |
| rebind | ✅ | ✅ locally launched checkouts | — | ❌ |
| live probe / recall canary | ✅ | — | — | ❌ — template only |

`n/a` means the probe reads source or a catalogue and has no wire to speak to.
`❌` means there is a wire and nobody has ever put a request on it.

## The half-run: `reference_drift_probe`

The ⚠️ above is one of its two halves, and the row would be misleading either
way without saying which.

**Run:** the template side, against the real `reference/` of
`mcp-data-source-probe` — `retry_backoff.py` and `response_envelope.py`, not a
fixture. Eight properties declared, five reported `REFERENCE_STALE`: no
`Retry-After`, no jitter, no cap after jitter, no wall-clock budget, and the
`raise RuntimeError` of the 2026-08-03 case still in place. Those five are a
measurement of a real template, and they stand.

**Not run:** the server side. No `reference/adoption.toml` is committed anywhere
yet, so no `[[template.adoption]]` has ever been resolved to a checkout. Nothing
here has ever compared a template to a server that copied it — which means
`REFERENCE_UNADOPTED` has fired only in tests, and the unanimity layer, which
needs three readable sites, has never run outside them. The layer that found the
`RuntimeError` line without a declaration is exactly the layer with no
production evidence.

To close it: commit an adoption manifest naming the servers that carry each
copy, and run with `--repos-root` pointed at their checkouts.

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

## Why this is written down rather than remembered

A probe suite that has only ever run against its own fixtures is in exactly the
state `identity_probe` was written to catch: everything green, nothing
contradicted, and no claim in it anchored to something outside itself. The
difference between this repository and the servers it audits is not that this one
has been verified — it is that this one says so.

When the auditor is deployed, this page is where the ❌ column gets filled in,
one row at a time, with the date and the run that did it. Until then, every green
badge above it means «the fixture agreed», and that is all it means.
