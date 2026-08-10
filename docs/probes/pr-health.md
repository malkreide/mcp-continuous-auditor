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
| `no_checks` | zero workflow runs on the head commit, and that commit is older than `--grace-minutes` (default 10) | the pipeline did not start, and by now it is not going to |

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

## Workflow runs, not check runs

`no_checks` counted check runs until it could not. **The Checks API is closed to
fine-grained personal access tokens.** Not a permission you forgot to tick —
there is none to tick. `Checks` is absent from the token UI and from GitHub's own
[permissions reference][perms]; GitHub support in
[community/discussions#129512][disc]: *"it isn't possible to assign Checks
permissions to a Fine-grained PAT — only GitHub Apps can access this API."* The
thread has been open since 2024.

`/commits/{sha}/check-runs` therefore answers 403 however the token is
configured. The sweep catches that per repository, so it would have reported all
47 as `NICHT ERHOBEN` and exited 1 — loud rather than falsely green, but a probe
that never runs.

`/actions/runs?head_sha=` needs only `Actions: Read`, which fine-grained tokens
do offer. It also measures the incident more directly: what was observed in `#50`
and `#58` was *GitHub started no workflows*, and this asks that question instead
of inferring it from the reports workflows leave behind.

The narrowing is real and belongs in writing: **a check run posted by something
other than GitHub Actions no longer counts.** A repository whose CI lives
entirely in an external app would turn every open pull request into a
`no_checks` finding. Nothing in the portfolio does that today; if something
starts to, the evidence line says `workflow_runs=0` and a reader can tell that
case from a genuine one without opening the pull request.

The status string stayed `no_checks`. It names the condition — no CI ran — which
did not change, and it is what the reports written so far already carry. What
changed is the measurement, so the evidence key changed with it:
`check_runs` → `workflow_runs`.

[perms]: https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
[disc]: https://github.com/orgs/community/discussions/129512

## The summary carries its numerator

```
45/47 Repos geprueft, 2 uebersprungen, 0 nicht erreichbar — 38 offene PRs geprueft, 0 Befunde
```

The run of 2026-08-10 printed that line without `38 offene PRs geprueft`, and it
could not be read. Zero findings out of forty inspected pull requests is a
healthy portfolio; zero findings out of none is a sweep that examined nothing.
Both printed `0 Befunde`.

That is this file's subject one level below where it started. `#50` and `#58`
were mistaken for green because nothing had run that could be red; a sweep with
nothing to inspect gets mistaken for a sweep that found nothing wrong, for the
same reason and in the same shape.

The count sits next to `findings` in the JSON as `pulls_examined`, deliberately
**not** inside `coverage`: that block counts repositories, and a pull-request
number living there would sooner or later be added to a denominator that means
something else. The repository denominator has been wrong once already.

## Every finding carries its observation

```
malkreide/mcp-continuous-auditor#58 [unbuildable] Startereignis je Ziel …
    — mergeable_state=dirty, draft=True, head=3b342b7, workflow_runs=0, head_age_min=134
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
GITHUB_TOKEN=… python scripts/pr_health.py --manifest manifest.json --json-out pr-health.json
```

`--json-out` writes the report to a file **and** still prints the summary, from
the one run. The workflow needs both — an artifact to keep and a log a human
reads — and 47 repositories are around 200 API calls, so a second pass for the
second format is not free and can disagree with the first.

It is also the fix for a run that was green and had reported nothing. The
workflow used to redirect `--format json` into the file and let a second Python,
written inline in the YAML, build the summary out of it. That reader reached for
`coverage.swept` and `skipped[].repo`; the report has `probed`/`measured` and
`skipped[].name`, and never had either of the other two. Nothing caught it,
twice over: every run before the secret existed died at the token gate, so the
reader had never once executed — and when it finally did, the step captured only
`pr_health.py`'s exit code, so the `KeyError` did not turn it red. **A sweep that
printed nothing came out looking like a clean one** — this file's own subject,
scored against the file itself.

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
| Actions: Read | `/repos/…/actions/runs?head_sha=` | the workflow-run count behind `no_checks` |
| Contents: Read | `/repos/…/commits/{sha}` | commit timestamp for `--grace-minutes` |

`Metadata: Read` comes with every fine-grained PAT and is not a separate
choice. Nothing here writes, so no write scope belongs on this token.

The same token also checks out the portfolio repository the target list comes
from, so **`swiss-public-data-mcp` belongs in the PAT's repository list** — not
just the repositories the sweep then queries. That checkout crosses a repository
boundary like every other call here; left on the workflow's own `GITHUB_TOKEN`
it takes a 404 before the sweep starts, which is the same failure as a missing
token wearing a different error message.
