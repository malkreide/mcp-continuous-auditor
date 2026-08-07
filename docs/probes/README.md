# The probes

One page per probe. The README lists what each one asks in three lines; this is
where the case histories live — the incident that produced the check, what the
finding is and is not allowed to claim, and the flags that matter.

Every probe here is deterministic. You run it and read the answer; you do not
ask a model what it thinks. And every one of them exists because a server was
green and wrong at the same time.

| Probe | The question it asks | Page |
|---|---|---|
| identity | Does the server report the version it actually is? | [identity.md](identity.md) |
| shipped | Is the release users install current — and does it run? | [shipped.md](shipped.md) |
| yank | Is a known-broken release still installable? | [yank.md](yank.md) |
| published | What does the *installed* artifact do on the wire? | [published.md](published.md) |
| lockfile | Is the declared bound in force where the install happens? | [lockfile.md](lockfile.md) |
| doc-claim | Do the identifiers the documentation cites exist? | [doc-claim.md](doc-claim.md) |
| parity | Does the translated documentation still say the same thing? | [parity.md](parity.md) |
| reference-drift | Is the copied template still the best version of itself — in both directions? | [reference-drift.md](reference-drift.md) |
| schema-field | Does the code read the field names the source delivers? | [schema-field.md](schema-field.md) |
| value-domain | Does a column the code coerces to a number actually hold numbers? | [value-domain.md](value-domain.md) |
| live-schedule | Do the live tests run anywhere, or are they only marked? | [live-schedule.md](live-schedule.md) |
| spec | Which MCP protocol version does this server actually speak? | [spec.md](spec.md) |
| provenance | Which commit is this report about? | [provenance.md](provenance.md) |
| pr-health | Which open pull requests are not green — including the ones that are not red either? | [pr-health.md](pr-health.md) |

Two more files are not probes but the layer under them:
`scripts/coverage.py` reads the portfolio's coverage manifest and owns the
denominator; `scripts/coverage_run.py` runs any single-target probe over every
entry in it. Both are described below.

Three more gates run inside the nightly audit rather than as standalone probes,
and are documented at the top of their own files:

* `scripts/transport_boot_probe.py` — does the server come up on the transports
  it declares, and does it answer `tools/list` — with a handshake, or without one
  under the stateless core of spec `2026-07-28`?
* `scripts/rebind_probe.py` — is the inbound host allow-list actually enforced?
* `scripts/live_probe.py` and `scripts/recall_canary.py` — does the live endpoint
  still return the shape *and the volume* it used to? The first checks the
  endpoint, the second the server's own tools, which is the whole chain.

## Coverage: the denominator comes from the manifest, never from the run

On 2026-07-31 a portfolio sweep reported **«33 von 33 ok»**. The sentence was
true and the set was wrong: `portfolio.json` listed 43 active servers and ten of
them were never in the run. Nothing contradicted the number, because nothing
ever held it against the source of truth — the denominator came out of the same
list as the numerator.

`coverage_manifest.py --format json` in `swiss-public-data-mcp` is that source of
truth. **`scripts/coverage.py`** is the one place in this repository that reads
it, validates it, and holds a run's own number against it. Every check in it
exists because the alternative ends in a false green:

| What goes wrong | What it looks like without the check |
|---|---|
| the `servers` / `repositories` key is renamed | every entry becomes a justified omission — `0/0 geprueft`, exit 0 |
| the block is empty | `0/0 geprueft`, exit 0 — indistinguishable from an audited portfolio |
| the per-entry field is missing (`pypi_dist`) | read like an explicit `null`: nothing measured, coverage "complete" |
| a target produced no result | quietly dropped from the list, and the list is what gets reported |

The last row is the one that carries the rule of this directory into the sweep:
a target with **no result counts against the denominator and is not coverage**.
An HTTP 404, a missing checkout or a probe that died mid-run all mean *nobody
looked*, and "I did not look" must not share an exit code with "there was
nothing there". That is why incomplete coverage exits `1` and **outranks**
findings (`2`): a run that did not look everywhere has not raised a finding, it
has raised nothing.

`--allow-skip NAME:REASON` is how an entry is left out on purpose. The reason is
mandatory and it is printed by the run itself — a skip without one is not a
skip, it is a gap with an alibi. A skip naming something the manifest does not
contain is refused rather than ignored: a typo skips nothing and still reads
like a decision.

### One driver, not eleven loops

Two probes read the manifest **themselves**, because they measure many targets
in one process and it would be wasteful not to: `published_probe.py --manifest`
(one venv, one index session) and `pr_health.py --manifest` (one token, one HTTP
session). Both do their arithmetic with `scripts/coverage.py`.

Every other probe measures **one** target — `--target <checkout>`, `--dist
<name>`, or `BOOT_TARGET_ROOT=<checkout>` for the two that take their target
from the environment. For those, the portfolio fan-out lives in one place:
**`scripts/coverage_run.py`**.

```bash
python scripts/coverage_run.py --probe identity \
    --manifest manifest.json --repos-root ~/portfolio
python scripts/coverage_run.py --probe shipped --manifest manifest.json \
    --allow-skip meteoswiss-mcp:"upstream down, Ticket #12" --format json
```

The decision, and why it went this way rather than a loop inside each probe:

* **The arithmetic would live eleven times.** It already lived twice — in
  `published_probe.py` and, in a second slightly different copy, in
  `pr_health.py` — and one of the two shipped with the denominator wrong (`2
  swept + 1 skipped` compared against an expected `2`, so an all-green run exited
  1). Eleven copies are eleven chances to repeat that.
* **The probes do not name their target the same way.** `--target`, `--dist`, an
  environment variable. A loop per probe inherits that difference; the driver
  translates it once, in one table (`PROBES`).
* **Each probe owns a different exit-code contract** (`identity` reports its
  finding with `1` and uses `2` for "not a Python repo"; the rest use
  `0/2/3/4/127`). An in-probe loop would still have to fold those into one
  aggregate verdict — the same code as the driver, minus anything anyone can
  test in isolation. The driver's table is tested (`tests/test_coverage_run.py`),
  including the rule that **an exit code no contract claims is never a finding**:
  124 and 137 mean the probe was killed, not that the target is broken.
* **The boot and rebind probes start servers.** They bind ports and can leave
  processes behind; that belongs in one process per target, which a subprocess
  driver gives for free and an in-process loop does not.

The nightly audit follows the same shape one level up: `scripts/nightly-audit.sh`
in sweep mode re-execs *itself* once per manifest entry rather than looping its
gates internally, so single-target mode stays the only code path that runs a
gate. See [`../cron/nightly-audit.md`](../cron/nightly-audit.md).

### PyPI has two APIs and they do not agree in real time

Relevant to every probe that reads the index (`published`, `shipped`, `yank`),
and written down because it produced a false finding:

* the **Simple API** (PEP 503, `https://pypi.org/simple/<dist>/`) is the one
  `pip` and `uv` actually read. It is what a user installs from, so it is the
  one an artifact-level claim must be made against.
* the **JSON API** (`https://pypi.org/pypi/<dist>/json`) is a different cache and
  **lags a publish or a yank by minutes**.

A disagreement between the two is therefore the normal appearance of a release
in flight, not evidence about the package. I read one as evidence and wrote
«auf keiner der beiden APIs — mehr als Propagationsverzug». It was exactly
propagation lag. `shipped_probe` now reports that state as `UNCONFIRMED` with
both readings named, raises no finding from it, and computes a publish gap only
against the **highest** version either API reported — a tag ahead of both is a
gap whichever cache you believe; a tag ahead of only the staler one is the false
alarm this rule prevents.

## What has actually been run

[coverage.md](coverage.md) applies the rule below to this repository itself: it
records what each probe has been exercised against, and names the column that is
still empty. **The auditor is not deployed, so no probe here has ever spoken to a
live MCP server** — every wire-level result so far is against a fixture or a
checkout the probe started itself.

## The shared rule

Every one of these reports distinguishes three answers, never two:

* **clean** — the check ran and found nothing,
* **finding** — the check ran and found something,
* **not measured** — the check did not run, or could not conclude.

The third is the one that takes discipline. A probe that cannot resolve a value
and reports "OK" has told you something false about a state it never read, and
that mistake is invisible in exactly the way the defects here are. Every exit
code in this directory keeps the three apart, and every "UNVERIFIED",
"NOT MEASURED", "UNCONFIRMED" and "MOVED_DURING_RUN" in the output is that rule
being applied.

### Every negative status carries its observation

Naming the state is half of it. The other half is saying **what was seen**, and
that half is not decoration — it is what makes the report readable at portfolio
scale. `published_probe`'s `no_event` branch left `evidence` empty, so «is
silent» and «phrases it differently» were the same sentence 38 times out of 42;
a check that reports unconfirmed almost everywhere gets clicked away, and then
it misses the one real case (`zh-education-mcp` 0.2.4 did not start at all).
Without fixing it, a survey across 42 servers was not runnable.

Every branch that sets a negative status was walked on 2026-08-06. One defect
came out of it, and it was the worse direction of the same error:
`published_probe`'s entrypoint listing failed *closed into a finding*. `_run`
reports a failed subprocess as `{"error": …}` and `.get("scripts") or []`
flattened that into an empty list, which then read as «the distribution declares
no console script» — `no_entrypoint`, i.e. `smoke_failed`. The probe's own
blindness was booked as a defect of the target. It is now `error` (not measured)
with the failure text as its evidence, and the two genuine shapes — declares
none, versus declares some the install never placed — are separate sentences
with the declared names in each.

## The rule reaches the delivery, not just the report

A probe that keeps the three answers apart and then hands them to a delivery
path that knows only two has given the distinction away at the last step.
`live-probe.yml.template` did exactly that: the probes reported three states,
and the inline block that turned them into a tracking issue collapsed them into
"alert" and "not alert". It survived only because the block never closed
anything — with no close, "not alert" costs nothing.

The close is what the guard needs to stay credible (an issue that only grows is
noise, and noise gets guards switched off), and it is also what makes the
collapse dangerous: closing on "not alert" closes on a comparison that may never
have happened. `scripts/drift_issue.py` is that step done under the same rule —
**finding**, **clear**, **unknown**, where unknown opens nothing and closes
nothing. It is a script and not a heredoc for the same reason: the collapse sat
in inline YAML for the life of the file because nothing there can be tested.
