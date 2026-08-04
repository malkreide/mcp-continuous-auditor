# pr-health — open pull requests that are not green

`scripts/pr_health.py`

## The incident

`mcp-continuous-auditor#50` and `#58` both sat at `mergeable_state: dirty`.

That reads like "there will be conflicts when you merge it". It is worse than
that: GitHub cannot build a merge ref for such a pull request, so it starts **no
workflows at all**. Both had zero check runs. Neither showed a red mark, because
nothing had run that could be red.

Looking at "no failing checks" saw the same picture it sees on a green pull
request. In both cases the pull request sat that way until somebody happened to
open it — `#58` for about two hours, `#50` rather longer.

This is `OPS-005` ("skipped is not passed") one level up: not a skipped test, a
skipped pipeline. And it is the same shape as every other finding in this
repository — **absence of a report gets read as absence of a problem.**

## What it reports

| Status | Condition | Why it matters |
|---|---|---|
| `unbuildable` | `mergeable_state` is outside the buildable set | GitHub cannot build the merge ref, so CI cannot run |
| `no_checks` | zero check runs on the head commit, and that commit is older than `--grace-minutes` (default 10) | the pipeline did not start, and by now it is not going to |

The buildable set is an **allow-list**: `clean`, `unstable`, `blocked`, `behind`,
`draft`. A state GitHub adds later therefore surfaces rather than passing
silently — which is the whole point of the probe, and the opposite default would
reproduce the incident it exists for.

Two values need care, and both were measured rather than assumed:

* **`unknown`** is not a finding. `mergeable_state` is computed lazily, so the
  first read after a push often returns it. The probe asks a second time. Were
  it treated as unbuildable, every freshly pushed branch would be a finding.
* **`draft`** is buildable. A draft pull request runs its workflows —
  `swiss-public-data-mcp#31` and `#32` both ran full CI while still drafts. In
  practice today's API reports their state as `clean`/`unstable` and never
  `draft` at all; the value is in the list because it is documented, and being
  wrong in that direction would make every draft a finding.

The `--grace-minutes` window exists because below it "no checks" and "the
workflows are about to start" are indistinguishable. Reporting inside it would
make a finding out of every push, and a probe that fires on everything is a
probe nobody reads.

## Every finding carries its observation

```
malkreide/mcp-continuous-auditor#58 [unbuildable] Startereignis je Ziel …
    — mergeable_state=dirty, draft=True, head=3b342b7, check_runs=0, head_age_min=134
```

Not decoration. A report you have to verify by hand is a report nobody reads,
and that lesson cost a portfolio sweep a full round: 38 identically worded
findings, none of which said what the server had actually done. `published_probe`
carries the same rule in its `no_event` branch.

## Targets

From the coverage manifest — `coverage_manifest.py --format json` in the
portfolio repo, the `repositories` block — never from a list in this file. A
hand-maintained target list drifts exactly the way a hand-maintained version
number drifts, and for the same reason: nothing downstream disagrees with it.

The manifest is validated rather than read optimistically, because both ways it
can be wrong end in a false green:

* a **missing** `repositories` key read like an empty one would sweep nothing
  and exit 0 — indistinguishable from a portfolio with no problems;
* an **empty** list would report `0/0 geprueft` and exit 0.

Archived repositories are skipped **by name and with a reason**: they are
read-only, so an open pull request there is stuck by definition. `--allow-skip
repo:grund` adds more, and the reason is mandatory — a skip without one is not a
skip, it is a gap with an alibi.

A repository the sweep could not reach counts against the expected total but is
**not** coverage: an HTTP 404 does not mean "there is nothing there".

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every declared repository swept or named as a skip, no findings |
| 2 | swept completely, findings present |
| 1 | coverage incomplete — repositories unreached or unaccounted for |

The split between 1 and 2 is the shared rule of this directory: "I did not look"
and "there was nothing there" must not share an exit code.

## Running it

```
python .portfolio/scripts/coverage_manifest.py --format json > manifest.json
GITHUB_TOKEN=… python scripts/pr_health.py --manifest manifest.json
```

`.github/workflows/pr-health.yml` runs it daily. It needs
`PORTFOLIO_READ_TOKEN` — a fine-grained PAT covering the portfolio repos; the
workflow's own `GITHUB_TOKEN` is scoped to this repository alone, and 47 are in
question. Without the secret the workflow **fails** rather than no-opping: a
sweep that checked nothing and a sweep that found nothing must not share an
outcome.

The sweep reads three endpoint families, so "read access to pull requests" is
not enough on its own — a token with only that permission gets through the
first call and then takes a 403 mid-sweep:

| Repository permission | Endpoint | Used for |
|---|---|---|
| Pull requests: Read | `/repos/…/pulls`, `/repos/…/pulls/{n}` | the open list, and `mergeable_state` |
| Checks: Read | `/repos/…/commits/{sha}/check-runs` | the check-run count behind `no_checks` |
| Contents: Read | `/repos/…/commits/{sha}` | commit timestamp for `--grace-minutes` |

`Metadata: Read` comes with every fine-grained PAT and is not a separate
choice. Nothing here writes, so no write scope belongs on this token.
