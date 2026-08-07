# Live-schedule probe

> Do the live tests run anywhere, or are they only marked?

`scripts/live_schedule_probe.py`

## The case

The portfolio's test doctrine says: the tests that talk to the real upstream
carry `@pytest.mark.live`, and the pull-request run excludes them with
`pytest -m "not live"`. That half is correct and worth defending. A foreign API
returning 503 must not turn somebody's unrelated pull request red, because a
suite that does that gets switched off, and a switched-off suite catches
nothing.

The exclusion then produces exactly the blindness the doctrine was written to
prevent. **`-m "not live"` is not a place where tests run. It is the absence of
one.** A test that runs nowhere is documentation, not a guard, and it rots
silently, because nothing goes red when it breaks.

That would be a hygiene point if the live tests were interchangeable with the
rest. They are not. Every other test in the repository asserts against a
fixture, and the fixture was written from the same assumption as the code. A
live test is the only thing in the tree that can contradict that assumption.

**`meteoswiss-mcp`, 2026-07-30** — the case behind catalogue item `DRIFT-005`.
The first execution of the live suite in months put three of six tests on the
floor. They had not broken recently: the upstream endpoint had been retired two
days earlier, and before that nobody had started the suite either. The marker
was set correctly, the doctrine was followed, and two tools were dead without
anyone knowing.

**`zh-education-mcp`, 2026-08-03** — the same mechanism, further along. Four of
six datasets were read under field names the source had stopped using
(`r["Schulgemeinde"]` against a `schulgemeinde` header), so eight tools answered
every query with an empty result list and the sentence «Schulgemeinde nicht
gefunden» — a failure wearing the costume of an answer. Every unit test stayed
green; the fixtures pin the old header. Only a live run could have contradicted
it, and no live run was scheduled.

A sweep of ten servers on 2026-08-03:

| scheduled live run | none |
|---|---|
| `srgssr`, `lindas`, `termdat`, `swisstopo`, `parlament` | `zh-education`, `swiss-transport`, `register`, `fedlex`, `swiss-snb` |

Five of ten violate a check the catalogue has carried as `enforced` since it
was written. And `zh-education` — where the schema drift above sat unnoticed for
months — is one of the five. That is not a coincidence in the sample; it is the
mechanism.

`DRIFT-005` was enforced and unmeasured. This is the measurement.

## What has to be true to pass

Three things, all read out of the checkout and none of them from the network:

1. **A live suite exists** — the `live` marker is *applied* to a test, not
   merely declared under `markers =` in the config. A declaration with nothing
   behind it is a plan.
2. **A scheduled workflow runs it** — a `schedule:`/`cron:` trigger, in a job
   whose pytest invocation *selects* the marker.
3. **A failure is visible** — a step or job that reacts to `failure()`, or a
   known notifier action. `DRIFT-005` is explicit about this and it is the part
   people skip: a scheduled run whose red result only lands in the Actions tab
   is a more expensive way of not running. Red crons stop being looked at in
   the second week.

## The marker expression is evaluated, not pattern-matched

This is the piece that decides whether the probe is usable, and a substring
match gets it wrong in both directions at once:

| `-m` | contains "live" | selects live tests |
|---|---|---|
| `not live` | yes | **no** |
| `live and not slow` | yes | yes |
| `not slow` | **no** | **yes** |
| *(absent)* | no | yes |

So the expression is parsed with `ast` and asked one question: *is there any
assignment of the other marker names under which a test carrying `live` is
selected?* That is satisfiability with `live` pinned True, enumerated over the
remaining names. Constructs outside `and` / `or` / `not` / names raise rather
than resolve, and above twelve free names the answer is `UNVERIFIED` — an
expression nobody can decide is not evidence of a schedule and not evidence
against one.

The last row of that table is not a loophole either. `pytest tests/` with no
`-m` runs the live suite, and whether the live suite runs is the whole question.

## A test file run as a script is resolved, not dismissed

`swiss-snb-mcp` runs its live suite nightly with two steps that read

```yaml
- run: python tests/test_live_scenarios.py
- run: python tests/test_live_warehouse.py
```

No pytest on the line at all. Both files carry `pytestmark = pytest.mark.live`
**and** an `if __name__ == "__main__":` block that runs every scenario and exits
non-zero on failure. Read as "not a pytest call", that repository came back
`LIVE_UNSCHEDULED` — a false finding against a server that has been running its
live tests every single night.

So `python <file>.py` is recorded and then resolved against the checkout, which
is the only place the answer is:

| The file | Verdict |
|---|---|
| carries the marker **and** has a `__main__` block | it runs — the schedule counts |
| carries the marker, **no** `__main__` block | finding, with its own sentence |
| anything else | opaque ⇒ `UNVERIFIED` |

The middle row is worth the code it costs. A live test file executed as a script
without a `__main__` block imports its dependencies and exits 0 — **a green cron
that runs no test.** That is not the absence of an answer, it is a wrong one,
and it is the DRIFT-005 failure mode one level further in.

## What a finding is not allowed to claim

The dangerous direction here is a false `LIVE_UNSCHEDULED` — the workflow does
run the suite, through something this file cannot read. One such finding and the
probe is argued with instead of acted on. So every reason the probe might simply
not have *seen* the run is spent before the finding is reached:

| Seen | Reported |
|---|---|
| a scheduled step runs `make live`, `tox`, `nox`, `just`, a shell script | `UNVERIFIED`, with the command quoted |
| a marker expression too wide, or not a marker expression | `UNVERIFIED`, with the expression quoted |
| a workflow file that does not parse, or PyYAML not importable | `UNVERIFIED`, with the reason |
| no `.github/workflows/` at all | `UNVERIFIED` — GitHub Actions is what this probe reads, and it cannot see a CI system it was not shown |
| no test applies the marker | `NO_LIVE_TESTS` |
| documentation claims an external auditor runs the suite | `CLAIMS_EXTERNAL_COVERAGE` |

`NO_LIVE_TESTS` is exit 3 and not exit 0 on purpose. A server with no live tests
has not passed this check; it is outside what this check can say anything about,
and whether it *needs* live tests is a different question (`OPS-001`). Booking
it green would let every untested server count as covered.

`CLAIMS_EXTERNAL_COVERAGE` is the escape hatch `DRIFT-005` allows — a server may
be covered by an external auditor running the live suite against it instead of
shipping its own cron. It is exit 3 for the reason the whole directory exists:
the claim is a sentence in a README, and whether the auditor actually runs is
not in this checkout. A probe that read the sentence as a pass would have
believed prose instead of measuring a fact. The claim is printed with its file
and line so a human can go and check.

Two more, small and deliberate:

* `pip install pytest` in a scheduled workflow is **not** a live run. The
  `pytest` token has to be the command — reached through `python -m`,
  `uv run`, `poetry run`, a leading env assignment, a path — not merely a word
  on the line. Without that rule, installing the tool satisfies the check the
  tool exists to enforce.
* `mark.live` inside a docstring or a string literal is **not** an applied
  marker. It is decided on the syntax tree. The test suite for this probe
  carries `@pytest.mark.live` in a fixture string, and a textual match makes
  this repository look like one with an unscheduled live suite.

## Notes, which never decide the verdict

`NO_MANUAL_TRIGGER` — a cron without `workflow_dispatch`, so the suite cannot be
started by hand the moment an upstream change is suspected.

`SPARSE_CADENCE` — the cron fires less often than weekly. Computed from the
five fields, including cron's OR between day-of-month and day-of-week: with both
restricted the job fires on either, so a restricted weekday alone already
guarantees a weekly run. An expression the parser does not model (`@weekly`,
`L`) is reported as *not determined*, not as sparse.

Both are `DRIFT-005` pass criteria, and neither is worth a red gate on its own.
A monthly run is a different animal from no run at all, and collapsing the two
would cost `LIVE_UNSCHEDULED` its meaning.

## Fixing a finding

`LIVE_UNSCHEDULED` — add a second workflow. The pull-request run stays at
`-m "not live"`; this check asks for an *additional* run, not a rebuild.

```yaml
on:
  schedule:
    - cron: "17 6 * * 1"    # weekly, on an odd minute, against the stampede
  workflow_dispatch: {}
jobs:
  live:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -m live -v
      - name: Issue on failure
        if: failure()
        uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.create({ /* … */ })
```

`LIVE_SCHEDULED_SILENT` — the cron is there and the notification is not. Add the
`if: failure()` step, or wire the existing notifier. And write the expectation
down somewhere: a red live run does not necessarily mean *our* bug, it means the
contract with the source changed or the source is down. Both belong seen; only
the first belongs fixed. Without that sentence in CONTRIBUTING, the job gets
disabled on the first transient red.

## Running it

```bash
python scripts/live_schedule_probe.py --target ../zh-education-mcp
python scripts/live_schedule_probe.py --target . --format json
python scripts/coverage_run.py --probe live-schedule --manifest manifest.json \
    --repos-root ~/portfolio
```

PyYAML is the one dependency, and its absence is `UNVERIFIED` rather than a
skipped file. The auditor's own `tests.yml` installs it.

| Exit | Meaning |
|---|---|
| 0 | `LIVE_SCHEDULED` — the suite runs, on a schedule, visibly |
| 2 | finding — `LIVE_UNSCHEDULED` or `LIVE_SCHEDULED_SILENT` |
| 3 | not measured — `NO_LIVE_TESTS`, `CLAIMS_EXTERNAL_COVERAGE`, `UNVERIFIED` |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |
