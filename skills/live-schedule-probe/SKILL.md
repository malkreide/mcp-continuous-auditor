---
name: live-schedule-probe
description: Measure whether a server's live tests actually RUN — a cron-triggered workflow whose pytest call selects the `live` marker and whose failure reaches somebody — instead of only being marked and excluded from CI with `-m "not live"`. LIVE_UNSCHEDULED when nothing runs them, LIVE_SCHEDULED_SILENT when a red cron reaches nobody, NO_LIVE_TESTS / CLAIMS_EXTERNAL_COVERAGE / UNVERIFIED when nothing could be measured. Deterministic and read-only; run it, do not reason about it.
requires:
  bins: [python]
  python: [pyyaml]
---

# Live-schedule probe

Do the live tests run anywhere, or are they only marked?

```bash
python scripts/live_schedule_probe.py --target ../zh-education-mcp
python scripts/live_schedule_probe.py --target . --format json
python scripts/coverage_run.py --probe live-schedule --manifest manifest.json \
    --repos-root ~/portfolio
```

Exit `0` scheduled and visible, `2` **finding**, `3` NOT MEASURED, `4` the
checkout moved during the run, `127` the harness could not run.

## Why

The doctrine is right: mark the tests that talk to the real upstream `live`,
and keep them out of the pull-request run with `-m "not live"`, so a foreign
503 cannot turn an unrelated pull request red.

The exclusion then produces the blindness the doctrine was written to prevent.
`-m "not live"` is not a place where tests run — it is the absence of one. A
test that runs nowhere is documentation, and it rots without anything going
red, because nothing runs it. These are also the only tests in the repository
that can contradict a wrong assumption about the source: every other test
asserts against a fixture written from the same assumption.

`meteoswiss-mcp`, 2026-07-30: the first live run in months, three of six tests
on the floor, the upstream endpoint retired two days earlier. `zh-education-mcp`,
2026-08-03: four of six datasets read under field names the source had stopped
using, eight tools answering every query with an empty list, every unit test
green. A live run would have contradicted both. Neither had one scheduled.

Catalogue item `DRIFT-005` has been `enforced` the whole time. Nothing measured
it. A sweep of ten servers on 2026-08-03 found five in violation.

## What it takes to pass

Three things, all read out of the checkout:

1. A `live` marker **applied** to a test — decided on the syntax tree, so the
   marker inside a docstring or a fixture string is not a suite.
2. A workflow with a `schedule:`/`cron:` trigger whose pytest call **selects**
   that marker.
3. Something that makes a red run visible — a step or job reacting to
   `failure()`, or a known notifier action. A scheduled run nobody sees is a
   more expensive way of not running.

## The marker expression is evaluated, not grepped

`-m "not live"` excludes. `-m "not slow"` **selects** live tests, and contains
neither the word nor any hint of it. A substring match gets that pair wrong in
both directions, so the expression is parsed and asked whether any assignment
of the other markers selects a test carrying `live`. Over
`_MAX_FREE_MARKERS` free names it says `UNVERIFIED` instead of guessing.

A call with no `-m` at all admits live tests. `pytest tests/` runs the suite,
and whether the suite runs is the entire question.

## Not measured is not clean

The finding this probe must never invent is `LIVE_UNSCHEDULED` on a repository
that does run its suite through something unreadable. So:

| Seen | Reported |
|---|---|
| a scheduled step runs `make live` / `tox` / `nox` / a shell script | `UNVERIFIED`, with the command named |
| the marker expression is too wide to decide | `UNVERIFIED`, with the expression named |
| a workflow file does not parse, or PyYAML is missing | `UNVERIFIED`, with the reason |
| no `.github/workflows/` at all | `UNVERIFIED` — this probe reads GitHub Actions and cannot see a CI system it was not shown |
| no test applies the marker | `NO_LIVE_TESTS` — nothing to schedule, and nothing measured about whether there should be |
| the README claims an external auditor runs it | `CLAIMS_EXTERNAL_COVERAGE` — the claim is recorded, not believed |

The last row is the escape hatch `DRIFT-005` allows, and it stays at exit 3
deliberately. Whether the auditor actually runs the suite is not in this
checkout; a probe that read the sentence as a pass would have believed prose
instead of measuring a fact.

## Notes, which never decide the verdict

`NO_MANUAL_TRIGGER` (a cron without `workflow_dispatch`, so the suite cannot be
started by hand after an upstream hint) and `SPARSE_CADENCE` (the cron fires
less often than weekly) are both `DRIFT-005` pass criteria and neither is worth
a red gate. A monthly run is a different animal from no run at all; collapsing
the two would cost the finding its meaning.

## Fixing a finding

`LIVE_UNSCHEDULED`: add a second workflow — weekly cron, `workflow_dispatch`,
`-m live`, and an `if: failure()` step that opens an issue. The pull-request
run stays at `-m "not live"`; this check asks for an *additional* run, not a
rebuild. `LIVE_SCHEDULED_SILENT`: the cron is there, the notification is not.

Full write-up: [docs/probes/live-schedule.md](../../docs/probes/live-schedule.md)
