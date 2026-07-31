---
name: published-probe
description: Measure what the package on PyPI actually sends as User-Agent — install it into a throwaway venv and read the value, rather than reading the repository. Use when a fix is merged and you need to know whether it reached users. Deterministic; run it, do not reason about it.
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
```

Exit `0` clean, `1` drift or unresolved, `2` the distribution would not install.

Each run creates a throwaway venv per distribution and removes it afterwards.
Expect roughly a minute per package; this is not a fast check and is not meant
to run on every commit. Its place is after a release, and in a periodic sweep.

## Reading the output

| Line | Means |
|---|---|
| `OK` | Every resolved User-Agent carries the installed version. |
| `DRIFT` | A User-Agent announces a version the package is not. This is what upstreams see. |
| `FOREIGN-UA` | The User-Agent is not this package's identity at all — see below. |
| `NO-UA` | No custom User-Agent anywhere — requests go out under the HTTP client default. Not drift, but the server is anonymous to every upstream. |
| `UNVERIFIED` | The source mentions a User-Agent and no strategy could resolve a value. **Not a pass.** |
| `INSTALL` | The published distribution would not install or import. |

`UNVERIFIED` is the line to take seriously, and the reason the probe is built
the way it is. See below.

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
