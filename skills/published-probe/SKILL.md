---
name: published-probe
description: Measure what the package on PyPI actually does — install it into a throwaway venv, read the User-Agent it puts on the wire, check that every module imports, start its console script, and check its declared dependency ranges for a missing upper bound. Use when a fix is merged and you need to know whether it reached users, or after a release. Deterministic; run it, do not reason about it.
requires:
  bins: [python]
---

# Published Probe

`identity-probe` reads a repository. This one reads the artifact.

The two answer different questions, and the gap between them is a place bugs
live for weeks: a source tree can be clean while everyone who runs
`pip install` still gets the old identity, because the fix was merged and never
released. A merged pull request changes nothing for users.

Measured on 2026-07-30 across 33 published portfolio packages: **16 sent a
version that disagreed with the version they were installed as. All 16 had the
fix merged. None had it released.**

Run:

```bash
python scripts/published_probe.py lobbywatch-mcp
python scripts/published_probe.py --format json bakom-mcp srgssr-mcp
python scripts/published_probe.py --constraint 'mcp<2' swiss-statistics-mcp
python scripts/published_probe.py --version 0.3.4 swiss-energy-mcp   # after a release
```

Exit `0` clean, `1` a finding, `2` the distribution would not install (or
`--version` was pinned and the venv came back holding something else).

Each run creates a throwaway venv per distribution and removes it afterwards.
Expect roughly a minute per package; this is not a fast check and is not meant
to run on every commit. Its place is after a release, and in a periodic sweep.

## Portfolio-wide: take the target list from the manifest

Never hand-assemble the list of distributions for a fleet-wide run. On
2026-07-31 such a run reported **"33 of 33 ok"** — true, and about the wrong
set: `portfolio.json` listed 43 active servers, and ten had never been on the
command line. Seven were `core`; one was `meteoswiss-mcp`, whose broken release
is the incident `shipped_probe.py` exists for. Nothing contradicted the number,
because nothing compared it against the source of truth.

```bash
# in swiss-public-data-mcp
python scripts/coverage_manifest.py --format json > manifest.json

# here
python scripts/published_probe.py --manifest manifest.json
python scripts/published_probe.py --manifest manifest.json \
    --allow-skip meteoswiss-mcp:"upstream down, ticket #12"
```

Every such run ends with a coverage line, and an entry may only be left out with
a stated reason:

```
42/43 geprueft — uebersprungen: MCP-Server-for-patent-research- (kein Paket auf dem Index …)
```

Three rules hold the mechanism up, and each of them exists because dropping it
produces a green run that measured nothing:

* Coverage counts **every** manifest entry, including the ones that publish no
  package — otherwise the denominator depends on the same judgement the check
  exists to audit.
* An **empty** manifest is refused. `0/0 geprueft` with exit 0 is not
  distinguishable from an audited portfolio.
* A **missing** `pypi_dist` key is refused rather than read as `null`. If the
  producer ever renames the field, every entry would otherwise become a
  justified omission: nothing measured, coverage complete, exit 0.

A target that produced no result at all appears as `OHNE ERGEBNIS` and fails the
run: a sweep that quietly stops halfway looks exactly like a clean one.

## Always pass `--version` after a release

`pip install <dist>` was measured serving the PREVIOUS artifact for minutes
after the new version was already listed on the index — `--no-cache-dir` empties
pip's cache and not the index's. **A re-check after a release that does not pin
the version is a re-check of the release before it.** `--version 0.3.4` makes it
`dist==0.3.4`, and a venv that comes back holding anything else exits `2` rather
than quietly measuring the wrong artifact.

`shipped_probe.py --pin-version` is the same flag on the same reasoning. Its
default stays unpinned, because a *gate* is asking what a user's `pip install`
resolves to today; a *re-check* is asking about one named release.

## Reading the output

| Line | Means |
|---|---|
| `OK` | Every resolved User-Agent carries the installed version; everything imports and the entrypoint announced its start. |
| `DRIFT` | A User-Agent announces a version the package is not. This is what upstreams see. |
| `FOREIGN-UA` | The User-Agent is not this package's identity at all — see below. |
| `NO-UA` | No custom User-Agent anywhere — requests go out under the HTTP client default. Not drift, but the server is anonymous to every upstream. |
| `BROKEN-IMP` | A module does not import from the installed distribution, with the package root imported first. **Not an import-order artefact — see below.** |
| `IMPORT-ORD` | A module fails only as the very first import of a process. Reported, deliberately **not** a finding. |
| `IMPORT-OPT` | A module needs something declared only behind an extra. Not installed by `pip install`, and not meant to be. |
| `SMOKE-FAIL` | The installed console script crashed within seconds of starting. |
| `SMOKE-?` | It ran, did not crash, and never announced `server.start`. **Not a pass.** |
| `SMOKE-NONE` | The distribution declares no console script — nothing to start. |
| `UNCAPPED` | An import-critical dependency has no upper bound and the index already serves a higher major. |
| `UNVERIFIED` | The source mentions a User-Agent and no strategy could resolve a value. **Not a pass.** |
| `INSTALL` | The published distribution would not install. |

`UNVERIFIED` and `SMOKE-?` are the lines to take seriously, and the reason the
probe is built the way it is. See below.

Every layer gets its own line. Only one of them can be the *status*, so a
package that is drifting **and** has a broken import **and** carries an open
dependency range prints all three — hiding two true facts behind a precedence
rule is not a summary.

## Import errors: the root first, then the submodules

`bag-health-mcp` was reported as having a circular import. It has none.
`import bag_health_mcp.server` runs cleanly in a fresh venv. What failed was
importing the private submodule as the *very first import of the process*,
before its own package root had initialised — an artefact of the order the probe
walked the modules in.

So the rule is: **import the package root first, then the submodules, and
whatever still fails after that is real.** Every failure the bulk scan sees is
re-measured in two fresh interpreters — `cold` (the submodule first) and `warm`
(the root, then the submodule). `warm` is what a user's code does and is what
decides. A verification that could not be run at all counts as real: absence of
proof is not a pass.

## Start is not import

A package can import perfectly and still not start — `parlament-mcp#29` raised
`ValueError: "Settings" object has no field "host"` at start and the process
never came up. So the probe runs the installed **console script** with stdin
closed for a few seconds and expects two things: a `server.start` event, and no
crash.

stdin closed is deliberate: a stdio server reads EOF and shuts down cleanly, so
the exit code is not the signal — the announcement before it is. A clean exit
that announced nothing is `SMOKE-?`, not a pass, for the same reason
`UNVERIFIED` exists: not seeing the server reach serving is not evidence that it
did. Use `--start-event` if a server announces something else, `--no-smoke` to
skip the stage.

## Upper bounds are part of the published metadata

`swiss-energy-mcp` 0.3.3 shipped `mcp[cli]>=1.20.0` with no upper bound. The day
`mcp` 2.0.0 was published, every fresh install of that release died on import.
The artifact did not change; the resolver's answer did.

So `requires_dist` of the installed artifact is read, and a missing upper bound
is reported for the dependencies that are **import-critical — measured**, from
the modules that actually appear in `sys.modules` after importing the package,
not from a list of names somebody thought looked important. Two tiers, because
they are two different days:

* `UNCAPPED` — the index **already** serves a higher major than the declared
  floor. The next fresh install can take it. A finding, and one that arrives
  before the break rather than after it.
* *armed* (reported in `--format json`, no line, not a finding) — no higher
  major is published yet. The trap is set and has not sprung.

An index that could not be read is `unknown`, never `capped`: a bound this probe
failed to check is not a bound.

## `FOREIGN-UA`: not drift, and worth more attention

`swiss-efv-mcp 0.3.0` sends
`Mozilla/5.0 (X11; Linux x86_64) … Chrome/124.0 Safari/537.36`. Read naively as
"product token / version" that parses to `5.0` and gets reported as drift
against `0.3.0` — wrong twice over. The package is not announcing a stale
version of itself, and the thing it *is* doing goes unnamed: it presents itself
to an upstream as a browser.

So the product token is compared against the distribution name (case- and
separator-insensitively, since `swisstopo-mcp` legitimately sends
`SwisstopoMCP/…`). A token that is somebody else's is reported as `FOREIGN-UA`,
not as a version problem. Real drift outranks it — a stale own identity stays
the headline when both are present.

Whether impersonating a browser is acceptable is a question for the operator,
often bound up with an upstream that blocks default clients. The probe's job is
to make sure nobody finds out by accident.

## Why `--constraint` exists

`swiss-statistics-mcp 0.6.0` cannot be imported against current `mcp`: 2.0.0
removed `mcp.server.fastmcp`, which the published code imports. Anyone
installing it today gets a server that dies at import. The probe reports that
honestly as `UNVERIFIED` rather than guessing; `--constraint 'mcp<2'` pins the
dependency so the measurement can proceed.

If you need a constraint to measure a package, that is itself a finding about
the package. Report it.

## Do not replace this with a grep

Three strategies were tried against the same 33 packages. Each reported a clean
result for packages that were drifting, and each was blind somewhere different:

1. **Regex for `f"…{__version__}…"`** missed `lobbywatch-mcp`, which spells the
   variable `PACKAGE_VERSION`. A pattern knows only the spellings its author
   thought of.
2. **Reading the module namespace at runtime** missed `seco-labor-mcp`, whose
   User-Agent sits in `_HTTP_KWARGS["headers"]["User-Agent"]`, and
   `swiss-transport-mcp`, which passes the literal inline to the `httpx`
   constructor *inside a function* — it exists in no module attribute at all.
3. **Scanning source for literals** misses every f-string User-Agent: there is
   no digit after the slash to anchor on.

All three run. Every finding records which produced it (`evidence`), so a
result can be argued with rather than believed.

## The rule that carries the weight

**A probe that cannot find a User-Agent must not report that there is none.**

"This server sends no custom User-Agent" is a finding. "I did not recognise the
shape" is a failure of the probe. They look identical in a report and mean
opposite things. Conflating them is how the first version of this check
pronounced 24 packages unremarkable, 16 of which were drifting — and it looked
like good news.

So the probe separates them: `no_user_agent` only when the installed source
never mentions a User-Agent at all; `unverified` when it does and nothing could
be read. `unverified` exits non-zero.

When reporting, carry the distinction through. "Not verified" is not "verified
clean", and the difference is exactly where this class of bug survives.

## Phase discipline

Same as `python-auditor`. **Phase 1: report only.** The fix for drift is a
release, not a code change — which is a decision for a human, not something to
trigger from an audit.
