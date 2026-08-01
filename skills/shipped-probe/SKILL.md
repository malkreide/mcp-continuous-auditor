---
name: shipped-probe
description: Verify that the fix on main is the fix users install — index version, yank status, release tags and unreleased commits against the repository, and optionally install the artifact and make it run. Deterministic; run it, do not reason about it.
requires:
  bins: [python, git]
---

# Shipped probe

`identity-probe` asks whether the version a server reports is *correct*. This
asks whether it is *current*, and then whether it *runs*. A repository can be
green, audited and entirely fixed while every `pip install` still hands out the
broken release — nothing in CI contradicts that, because CI tests the branch,
not the artifact.

This absorbed the former `release-gap` skill. That question — is the published
metadata consistent with the repository? — is now the **cheap depth** of this
one, not a separate tool.

Run:

```bash
# metadata only: index + git. Two HTTP requests, no venv, no install.
python scripts/shipped_probe.py --target <path> --metadata-only
python scripts/shipped_probe.py --target <path> --offline          # git-only, says so
python scripts/shipped_probe.py --target <path> --max-age-days 14

# full: also install the distribution into a fresh venv and speak MCP to it.
python scripts/shipped_probe.py --dist <name> --target <path>
python scripts/shipped_probe.py --dist <name> --target <path> --tool health --format json

# any PEP 503 index, as pip takes it
python scripts/shipped_probe.py --target <path> --index-url https://pypi.example.com/simple
```

`--dist` defaults to the `[project] name` in the target's `pyproject.toml`.

Exit `0` clean, `2` **findings**, `127` the harness could not run (unreachable
index, venv failure). Report every line; the findings are independent.

> **Changed:** the old `release_gap.py` exited `1` for findings and `2` for "not
> a Python repo". Both moved to this probe's vocabulary. A script testing
> `$? -eq 1` now sees `2`, and a directory with no `pyproject.toml` gives `127`
> — `2` now means *the target has a defect*, which such a directory has not been
> shown to have.
>
> A `scripts/release_gap.py` shim carried the old name, exit codes and
> `--format json` keys for a while so outside callers kept working. It has been
> removed — the old name no longer resolves at all, and the exit codes above are
> the only contract.

## Which depth to use

| | `--metadata-only` | default |
|---|---|---|
| Cost | 2 HTTP requests + git | venv + install + a live tool call |
| Answers | did the release land, is it yanked, has `main` drifted | all of that, **and** does the installed artifact start and answer |
| Use for | a pre-release check, a wide portfolio sweep | the nightly gate, anything before trusting a release |

Phase 1's findings are carried into the full run, never replaced — the deeper
run can only report *more*. Where both depths reach the same conclusion from
different evidence (`PUBLISH_GAP` from metadata, `TAG_NOT_ON_INDEX` after an
install) the metadata code is the one reported, so what you see does not depend
on which depth ran.

## The incident

`meteoswiss-mcp`, 2026-07-30. The migration to the `mcp` 2.x SDK was merged to
`main` on the 29th. PyPI kept serving `0.4.0`, which imports
`mcp.server.fastmcp` — a module `mcp` 2.0.0 had removed the day before. Every
fresh `uvx meteoswiss-mcp` died on import for three days, until an outside user
filed the bug. The repository was, the whole time, fixed.

It recurred within the same afternoon: `0.5.0` was published, three further
fixes landed, and until the next release PyPI served a server whose
`meteo_current`, `meteo_forecast` and `meteo_school_check` all returned
nothing.

Run against a reconstruction of that state, the probe reports
`UNRELEASED [high] … 2 of them user-facing` and exits `2`.

## Reading the output

| Line | Means |
|---|---|
| `PUBLISH_GAP` | A release tag exists that PyPI does not have. Someone cut a release and it did not land — a failed workflow, or a pending environment approval. The sharpest finding here, because the maintainer already believes it shipped. Raised only when the tag is ahead of **both** index APIs. |
| `RELEASE_YANKED` | The release this repository treats as current is on PyPI and **withdrawn**. Every other check reads healthy — the version exists, the tag matches, CI is green — while `pip install` quietly resolves to something older. Older yanked releases are listed as a `NOTE`, not as a finding. |
| `UNCONFIRMED` | PyPI's two index APIs disagree — about the latest version, or about a yank flag. Nothing is claimed from it and the run does not go red. See below. |
| `UNRELEASED` | Commits beyond the last release, with the age of the oldest and a breakdown by Conventional-Commit type. `high` when any are user-facing (`fix`, `feat`, `perf`, `revert`), `low` when it is housekeeping. |
| `UNTAGGED_VERSION` | `pyproject.toml` was bumped, no tag matches. The ordinary state of a prepared release — a finding only once it ages. |
| `CHANGELOG_UNRELEASED` | An `[Unreleased]` section with entries. Weakest signal, reported last: prose lags. |
| `UNKNOWN` | PyPI could not be reached. The comparison that matters **did not happen**. |
| `NOTE` | Informational — `--offline`, not on PyPI, or tags unavailable. |

## Which PyPI API is believed

Both response flavours are read: PEP 691's JSON where the index serves it, and
PEP 503 HTML otherwise — the JSON flavour is optional and HTML is the only
format an index must serve, so a JSON-only reader could not audit a private
index at all.

`--index-url` takes any PEP 503 index, the way `pip` does. **Against anything
but PyPI the JSON cross-check does not run**, and the report says so. That is
not caution, it is correctness: pypi.org would answer about a *different
package* that happens to share the name, so agreement and disagreement are both
noise — and a disagreement would raise `UNCONFIRMED`, or a `PUBLISH_GAP`, from
an unrelated project. Read the resulting run knowing it has one opinion in it,
not two: nothing here can report `UNCONFIRMED` against a private index, so a
mid-propagation index will not be caught.

The **Simple API** (`/simple/{dist}/`, PEP 503/691/700) is primary: it is the
one `pip` and `uv` read, so it is the one that decides what a user gets, and it
carries the per-file `yanked` flag. The **JSON API** (`/pypi/{dist}/json`) is a
fallback and a second opinion.

Measured against `zurich-opendata-mcp` on 2026-07-31, minutes after the
operations involved, the two disagreed twice:

- six freshly yanked releases still read `yanked: false` on the JSON API while
  the Simple API had all six as yanked;
- ~90 s after `0.7.0` was published, the JSON API still answered `0.6.0` while
  the Simple API already served `0.7.0`.

Re-measured on 2026-08-01 both had converged. The divergence is a propagation
window, not a standing property — which is exactly what makes it dangerous: it
is only visible in the minutes right after a release or a yank, the minutes in
which somebody is most likely to be running this probe.

Where the two disagree the probe reports **`UNCONFIRMED`** and claims nothing.
It is not a pass and not a finding: loud in the report, does not turn the run
red — the same shape as the boot gate's `not-selected`. Exit `0` with an
`UNCONFIRMED` line means *read the line*.

To see what the two APIs say right now:

```bash
RELEASE_GAP_LIVE=1 python3 -m unittest tests.test_release_metadata.LiveDivergenceTest -v
```

## Three things not to shortcut

1. **An unreachable index is not a pass.** The probe exits non-zero and says
   `UNKNOWN` rather than printing "in sync" from git alone. A check that
   degrades into a plausible-looking success is the exact failure this script
   exists to catch — reporting green from half the evidence would reproduce it
   one level up.

2. **A `--depth 1` clone has no tags.** It fetches none, so an empty tag set is
   reported as *unknown*, never as "never released". Concluding the latter
   inverts the finding. If you need this probe to be conclusive, clone with
   tags or `git fetch --tags` first.

3. **Age is the finding, not the gap.** Every repository is ahead of PyPI for
   the minutes after a merge. Firing on that gets the check muted, and a muted
   check catches nothing — the same reasoning that keeps recall floors at half
   the observed count. `--max-age-days` defaults to 7.

4. **`UNCONFIRMED` is not "probably fine".** It means the two indexes were
   asked and gave different answers. Re-run it a minute later — propagation is
   measured in seconds — and if it persists, that is worth a look at PyPI
   rather than at the target.

## Why the commit type matters

`fix:` sitting unreleased is a different fact from `docs:`. In the incident
above, every unreleased day was a user hitting a `ModuleNotFoundError` on
install. The probe separates the two rather than counting commits, so a
portfolio sweep does not drown a real gap in documentation churn.

This assumes [Conventional Commits](https://www.conventionalcommits.org/),
which is the portfolio's convention. A repository that does not follow it gets
everything classed as `other` — housekeeping severity, and the count is still
reported.

## Phase discipline

Same as `python-auditor`. **Phase 1: report only.** The remedy is a human
decision in any case: cutting a release is not something an agent should do on
its own, and the probe deliberately stops at naming the gap.
