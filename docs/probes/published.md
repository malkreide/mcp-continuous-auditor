# Published probe

> The identity probe reads a repository and the shipped probe's metadata depth
> compares version *numbers*. Neither opens the artifact.

`scripts/published_probe.py` · skill: `skills/published-probe/SKILL.md`

## The case

`swiss-efv-mcp` passes both of the above — PyPI 0.3.0, `main` 0.3.0, `src/`
clean — while the package every user installs sends
`Mozilla/5.0 … Chrome/124.0`. It impersonates a browser to upstreams.

So this probe installs the distribution into a throwaway venv and measures what
the shipped code actually does.

## What it measures

**The User-Agent it puts on the wire.** 16 of 33 portfolio packages announced a
version they were not, with the fix merged in every one of them.

**Imports decide the status** (`broken_import`), and the package root is imported
before its submodules so an import-order artefact is not mistaken for a defect:
`bag-health-mcp` was reported as a circular import it does not have, because only
the private submodule *as the very first import of a process* fails.

**Start is not import.** The installed console script is run with stdin closed
for a few seconds and must announce a `server.start` event without crashing. A
clean exit that announced nothing is not a pass.

**`requires_dist` upper bounds**, on the dependencies the package actually
imports. `swiss-energy-mcp` 0.3.3 shipped `mcp[cli]>=1.20.0` uncapped, and the
day `mcp` 2.0.0 appeared every fresh install of it died — reported the moment the
index serves a higher major, which is before the break rather than after it.

## Found nothing is not "there is nothing"

Where it cannot resolve a value it reports `UNVERIFIED`, never clean. An earlier
version that conflated the two called 24 packages unremarkable; 16 of them were
drifting. That single confusion is the reason this probe exists in the shape it
does.

The same rule governs coverage. With `--manifest`, the target list comes from the
manifest and the run must account for every name on it: each one is probed,
explicitly skipped with a reason (`--allow-skip NAME:REASON`), or reported as
missing. A sweep that quietly covered a prefix of the list would look exactly
like a complete one.

## Running it

```bash
python scripts/published_probe.py zurich-opendata-mcp swiss-efv-mcp
python scripts/published_probe.py --manifest coverage.json --format json
python scripts/published_probe.py foo-mcp --version 0.6.1     # re-check after a release
python scripts/published_probe.py foo-mcp --constraint 'mcp<2'
```

`--version` pins the install. Use it for every post-release re-check: an unpinned
install was measured serving the previous artifact for minutes after the new one
was on the index, `--no-cache-dir` and all.

| Exit | Meaning |
|---|---|
| 0 | every probed package is unremarkable |
| 1 | findings, or the manifest's coverage was not met |
| 2 | an install failed |

The JSON report is an object carrying `provenance`, `results` and — with
`--manifest` — `coverage`. It names the auditor commit the run was launched from;
see [provenance.md](provenance.md).
