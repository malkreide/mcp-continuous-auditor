---
name: release-gap
description: Verify that the fix on main is the fix users install — published PyPI version, release tags and unreleased commits against the repository. Deterministic; run it, do not reason about it.
requires:
  bins: [python, git]
---

# Release Gap

`identity-probe` asks whether the version a server reports is *correct*. This
asks whether it is *current*. A repository can be green, audited and entirely
fixed while every `pip install` still hands out the broken release — and
nothing in CI contradicts that, because CI tests the branch, not the artifact.

Run:

```bash
python scripts/release_gap.py --target <path-to-server-repo>
python scripts/release_gap.py --target <path> --max-age-days 14
python scripts/release_gap.py --target <path> --offline    # git-only, says so
```

Exit `0` clean, `1` findings **or an unreachable index**, `2` not a Python MCP
repo. Report every line; the findings are independent.

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
`UNRELEASED [high] … 2 of them user-facing` and exits `1`.

## Reading the output

| Line | Means |
|---|---|
| `PUBLISH_GAP` | A release tag exists that PyPI does not have. Someone cut a release and it did not land — a failed workflow, or a pending environment approval. The sharpest finding here, because the maintainer already believes it shipped. |
| `UNRELEASED` | Commits beyond the last release, with the age of the oldest and a breakdown by Conventional-Commit type. `high` when any are user-facing (`fix`, `feat`, `perf`, `revert`), `low` when it is housekeeping. |
| `UNTAGGED_VERSION` | `pyproject.toml` was bumped, no tag matches. The ordinary state of a prepared release — a finding only once it ages. |
| `CHANGELOG_UNRELEASED` | An `[Unreleased]` section with entries. Weakest signal, reported last: prose lags. |
| `UNKNOWN` | PyPI could not be reached. The comparison that matters **did not happen**. |
| `NOTE` | Informational — `--offline`, not on PyPI, or tags unavailable. |

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
