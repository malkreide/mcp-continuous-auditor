# Shipped probe

> The identity probe asks whether the reported version is *correct*. This asks
> whether it is *current*, and then whether it *runs*.

`scripts/shipped_probe.py` · skill: `skills/shipped-probe/SKILL.md`

## The case

A repository can be green, audited and entirely fixed while every `pip install`
still hands out the broken release, because CI tests the branch and not the
artifact. `meteoswiss-mcp` shipped an import error to every fresh install for
three days with `main` already corrected, and it took an outside bug report to
notice.

## Two depths

`--metadata-only` compares the index version, the yank status, the release tags
and the unreleased commits for **two HTTP requests**. It weighs a `fix:`
differently from a `docs:` — kind beats age, so a user-facing commit is reported
the moment it is unreleased and a breaking one at any age, while housekeeping
keeps the seven-day clock.

The default depth then installs the distribution into a fresh venv, compares the
installed sources against the checkout, and speaks real MCP to it.

`nightly-audit.sh` runs the metadata depth first, on purpose: the release verdict
has to survive the full gate hanging, and it only survives because it is cheap.
Anything that made the metadata depth expensive would break that.

## Two anchors a version number cannot provide

**`NO_TAGS`** — a repository without release tags leaves half these checks
measuring nothing, and must say so rather than report OK. A shallow clone is
*unknown*, not zero: `git clone --depth 1` fetches no tags and `git tag --list`
then succeeds with empty output, indistinguishable from a project that never cut
a release unless the probe asks.

**`STALE_ARTIFACT`** — compares content instead of numbers. It is the only way to
see the case where the index and the checkout both say 0.3.3 and are not the same
code.

## Reading the index the way pip does

It reads the **Simple API** at `--index-url` — the surface `pip` installs from —
in both the PEP 691 JSON and the PEP 503 HTML flavour, since only HTML is
guaranteed. PyPI's JSON API was measured lagging the Simple API by minutes on
both the latest version and the `yanked` flag; on PyPI the JSON API is read as a
second opinion, and where the two disagree the answer is `UNCONFIRMED` rather
than guessed.

That also closes a blind spot the version number alone cannot see: a *yanked*
release looks identical to a healthy one — the version exists, the tag matches,
CI is green — while every `pip install` quietly resolves to something older.

An unreachable index is reported as a harness failure, never as "in sync".

## Running it

```bash
python scripts/shipped_probe.py --dist zurich-opendata-mcp --target ../zurich-opendata-mcp
python scripts/shipped_probe.py --target . --metadata-only    # no venv, no install
python scripts/shipped_probe.py --target . --offline          # git-only, says so
python scripts/shipped_probe.py --target . --pin-version 0.6.1
```

`--pin-version` installs `dist==VERSION` for a re-check after a release. Use it
every time: an unpinned install was measured serving the *previous* artifact for
minutes after the new one was listed, `--no-cache-dir` and all.

| Exit | Meaning |
|---|---|
| 0 | the published artifact matches the repository and ran |
| 2 | finding — absent from the index, stale, version divergence, or the installed server did not answer |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run (no network to the index, venv creation failed) |
