# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — `schema_field_probe.py`: the code's field names against the live source

`zh-education-mcp` against `www.bista.zh.ch`, 2026-08-03. The code read
`r["Schulgemeinde"]`. The source delivered `schulgemeinde`.

What came out was not an error. It was an empty hit list and the sentence
«Schulgemeinde nicht gefunden» — **a failure wearing the costume of an answer**.
A caller cannot tell that apart from a real absence, and neither can a model
summarising the result: the tool reported, successfully, that the thing does not
exist. Four of six datasets, eight tools. Two of them mix the spelling inside one
header row (`gebiet_Bezeichnung`, `staatsangehoerigkeit_ISO2_Code`), so
lowercasing on read is a second wrong name rather than a fix.

Every unit test stayed green, and had to: the fixtures pin the old header, so the
suite compared the code's assumption against a recording of the same assumption.

**Why this is not `live_probe.py`.** That one compares live against a **fixture**
and catches the source moving under a recording. It cannot catch this: the
fixture and the live response can agree perfectly while the code reads a name
neither ever contained. The recording is not what runs. This probe compares live
against the **code** — the only pair whose disagreement reaches a user. Where a
fixture is declared it is read too, but only to report *why the tests stayed
green* (`FIXTURE_PINS_OLD_HEADER`, a note), never as the standard.

The mapping is declared in `schema_fields.toml` in the target and never guessed
— same rule as `reference_drift_probe`, format reference in the new
`schema-fields.example.toml`. No manifest is `MANIFEST_MISSING` at exit 3: a
server that has not declared its datasets has not been checked, which is a
different sentence from one with no drift.

Findings: `FIELD_CASE_DRIFT` (the column exists under another spelling; compared
on a casefolded, separator-free form, so `gebiet_Bezeichnung` and
`gebiet_bezeichnung` are one name) and `FIELD_MISSING` (gone in every spelling).
Each names the **form** of the read, because they fail differently: `r["x"]`
raises and the caller sees an internal error; `r.get("x")` returns `None`, the
filter matches nothing, and the caller is told politely that nothing exists.

**The corroboration rule** is what keeps the extraction honest. Collecting every
subscript key inside a declared symbol over-collects — a function also indexes
dicts that are not records — so a key is `FIELD_MISSING` only when at least one
*other* key at the same site does resolve. Zero of five resolving means the site
is not reading that dataset; the probe measures nothing there rather than booking
its own mismatched mapping as five defects of the target. An over-broad symbol
costs coverage, not correctness.

Every non-conclusion is `UNVERIFIED` with what was seen: source unreachable or
unparseable, **zero records** in the response (a field list cannot be read from
an empty list — and an empty list is also what the incident looked like from
outside), a truncated body, a header longer than the byte window, a declared file
or symbol that is not there. Only the first 64 KiB of a CSV are read: the header
is the whole question, and downloading a 60 MB extract to look at line one makes
the check expensive enough that somebody switches it off. The delimiter is
reported whether declared or sniffed — reading a semicolon-separated header as
one comma-separated column produces a single enormous "field name" that matches
nothing, which would look exactly like total drift.

Registered in `coverage_run.py` as `--probe schema-field`.
[`docs/probes/schema-field.md`](docs/probes/schema-field.md),
`skills/schema-field-probe/`, `tests/test_schema_field_probe.py`,
`schema-fields.example.toml`. `docs/probes/coverage.md` records the row as
**never run against anything**: no target ships a manifest yet, and this code has
never fetched `www.bista.zh.ch`. The case history is a hand investigation, not a
run.

### Added — `live_schedule_probe.py`: `-m "not live"` is not a place where tests run

`DRIFT-005` has been `enforced` in the audit catalogue since the `meteoswiss-mcp`
incident of 2026-07-30 — the first live run in months, three of six tests on the
floor, the upstream endpoint retired two days earlier. Nothing measured it. A
hand sweep of ten portfolio servers on 2026-08-03 found **five in violation**:
`srgssr`, `lindas`, `termdat`, `swisstopo` and `parlament` schedule a live run;
`zh-education`, `swiss-transport`, `register`, `fedlex` and `swiss-snb` do not.

`zh-education` is on the second list, and that is the point. It is where four of
six datasets were read under field names the source had stopped using, where
eight tools answered every query with an empty list and «Schulgemeinde nicht
gefunden», and where every unit test stayed green because the fixtures pin the
old header. Only a live run could have contradicted it. There was none.

The probe reads the checkout and asks three things: is the `live` marker
**applied** to a test (decided on the syntax tree — the marker in a docstring is
not a suite); does a `schedule:`/`cron:` workflow run a pytest call that
**selects** it; would a failure be **seen** (a step or job reacting to
`failure()`, or a known notifier action). Findings are `LIVE_UNSCHEDULED` and
`LIVE_SCHEDULED_SILENT`; exit 2.

The `-m` expression is **evaluated, not grepped**, because a substring match
gets it wrong in both directions at once: `not live` contains the word and
excludes, `not slow` does not contain it and selects. The expression is parsed
with `ast` and asked whether any assignment of the other markers selects a test
carrying `live`. Over twelve free names it says so instead of guessing.

Every reason the probe might simply not have *seen* a live run is spent before
the finding is reached — a scheduled step running `make`/`tox`/`nox`/a shell
script, an undecidable marker expression, a workflow that does not parse, no
`.github/workflows/` at all: `UNVERIFIED`, with the command or expression
quoted. `pip install pytest` is explicitly not a live run; without that rule,
installing the tool satisfies the check the tool exists to enforce.

Two exit-3 states carry the rule of this directory. `NO_LIVE_TESTS` — no marker
applied anywhere — is not green: whether the server *needs* live tests is
`OPS-001`'s question, and booking it clean would let every untested server count
as covered. `CLAIMS_EXTERNAL_COVERAGE` is the escape hatch `DRIFT-005` allows,
and it stays at 3 because the claim is a sentence in a README; whether the
auditor actually runs the suite is not in the checkout. The claim is printed
with its file and line, recorded rather than believed.

`NO_MANUAL_TRIGGER` and `SPARSE_CADENCE` are notes and never decide the exit
code. A monthly run is a different animal from no run at all, and collapsing the
two would cost `LIVE_UNSCHEDULED` its meaning.

Registered in `coverage_run.py` as `--probe live-schedule`.
[`docs/probes/live-schedule.md`](docs/probes/live-schedule.md),
`skills/live-schedule-probe/`, `tests/test_live_schedule_probe.py`.
`docs/probes/coverage.md` records the hand sweep as **not reproduced** by the
probe: ten numbers read off ten repositories by a person are the reason the
file exists, not a result this tool has produced.

### Fixed — `wraps` measured a coding habit, not the ordering it exists to check

`caps_after_jitter` asks whether the ceiling is applied AFTER the jitter. The
check walked the outer call's argument subtrees looking for the inner call, and
so recognised exactly one way of writing it:

```python
return min(base * random.uniform(0.5, 1.5), CAP)     # lexikalisch — erkannt
jittered = base * random.uniform(0.5, 1.5)           # namensbindend — NICHT
return min(jittered, CAP)
```

Measured against the eleven-server sweep of 2026-08-03: **all six** repositories
in the portfolio that apply the cap after the jitter write the second form.
Read lexically only, the property failed 6 of 6 adoptions that hold exactly the
behaviour it describes — every finding it produced pointed at correct code.

The two shapes are **mutually exclusive**: no single expression satisfies both,
because the choice between them *is* whether a name is bound. A check that
accepts only one is not measuring the ordering; it is measuring who wrote the
line. Both are accepted now.

Deliberately shallow — a direct assignment inside the same symbol, no transitive
chains, no attributes, no augmented assignment. A wider analysis would start
reporting a cap where the value merely passed through, and a false *positive* is
worse here than the false negative it replaces: this property exists to catch
`min(cap, base) * jitter`, which is not a bound at all. `WrapsShapeTest` pins
all five cases, the wrong order among them.

### Documented — `calls` cannot tell timing from limiting

From the same reading, and not fixable in the check: `wall_clock_budget` is
declared as `kind = "calls"` over `time.monotonic` / `time.perf_counter`, and a
clock read looks identical whether it bounds something or merely measures it.
`i14y-mcp` called `time.perf_counter()` twice to compute an `elapsed_ms` for a
log line and nothing else — and counted as the one server of eleven with a
wall-clock budget. It had none.

The honest count for that sweep was 6 of 18, not 7. The limitation is now stated
where the property kinds are described, with the guidance that follows from it:
declare the bound itself where the manifest can, and read the two lines before
trusting a lone clock call.


### Fixed — `_sleep = asyncio.sleep` is an alias, and the probe could not see it

`reference_drift_probe.py` resolved `import x as y` and `from x import y` but not
a module-level assignment of a dotted name to a name. Four servers in the
portfolio bind the backoff sleep exactly that way, so a test can patch the module
attribute without reaching every import in the process. Read literally,
`await _sleep(delay)` looks like a symbol that does not sleep — and the probe
reported four correct adoptions as lagging.

Found by running the probe against the portfolio for the first time, not by
reading it. That is the noise class the whole design exists to avoid: a probe
that cannot tell a renamed adoption from a regression produces a red run
everybody learns to ignore, and a naming convention is the most ordinary rename
there is.

Only module level, and only a bare dotted name on the right: a rebinding inside a
function is control flow, and `x = mod.factory()` binds a value rather than
another name for one. Both are tested. An assignment resolves through what is
already known, so `import asyncio as aio` followed by `_sleep = aio.sleep` still
lands on `asyncio.sleep`.

One indirection is still invisible and is left that way: two servers wrap the
sleep in a one-line helper of their own. Seeing through that is inter-procedural
analysis, which this probe does not do — so the property was dropped from the
manifest rather than shipped with two findings nobody can fix.

`coverage.md` now records a full run instead of a half one: 19 of 19 declared
sites read, five `REFERENCE_STALE`, no `REFERENCE_UNADOPTED`. Two of the five are
the case this probe was built for — a wall-clock budget in 7 of 18 servers and a
typed upstream error in 15 of 18, neither of which came back to the template. The
other three (`Retry-After`, jitter, cap-after-jitter) are in **no** server at all,
which the case history did not predict and the report states plainly rather than
implying the servers are ahead. The unanimity layer had 18 readable sites and
still reported nothing: 15 of 18 is not unanimity, and it remains the one part
with no evidence outside the tests.


### Added — `scripts/coverage.py`: der Nenner kommt aus dem Manifest, nicht aus dem Lauf

Am 2026-07-31 meldete ein Portfolio-Lauf «33 von 33 ok». Der Satz war wahr und
die Menge falsch: `portfolio.json` listete 43 aktive Server, zehn davon waren nie
im Lauf. Nichts widersprach der Zahl, weil Zähler und Nenner aus derselben Liste
kamen.

`coverage_manifest.py --format json` im Portfolio-Repo ist die Antwort darauf,
und bis jetzt las genau eine Sonde es. Inzwischen waren es zwei — mit zwei
eigenen Kopien derselben Regel, von denen eine den Nenner schon einmal falsch
hatte (`2 geprüft + 1 übersprungen` gegen erwartete `2`, also Exit 1 bei lauter
grünen Ergebnissen).

Das Lesen, die Validierung, die Auslassungs-Gründe und der Deckungs-Exit-Code
stehen jetzt einmal in `scripts/coverage.py`. `published_probe.py` und
`pr_health.py` rechnen damit; ihre `read_manifest`/`parse_allow_skip` sind dünne
Sichten darauf geblieben. Validiert wird fail-closed, weil jede der vier
Fehlerarten sonst in demselben falschen Grün endet: fehlender Abschnitt, leere
Liste, fehlendes Feld (fehlend ≠ `null`), Wert weder Name noch `null`. Neu ist
ausserdem, dass ein `--allow-skip` auf einen Namen, den das Manifest nicht kennt,
abgelehnt statt ignoriert wird — ein Skip, der nichts überspringt, sieht wie eine
Entscheidung aus.

### Added — `scripts/coverage_run.py`: eine Sonde über jeden Manifest-Eintrag

Die meisten Sonden messen EIN Ziel (`--target`, `--dist`, `BOOT_TARGET_ROOT`).
Statt elf Schleifen mit elf Kopien derselben Rechnung iteriert ein Treiber. Er
übersetzt die drei Arten, ein Ziel zu benennen, einmal, kennt den Exit-Vertrag
jeder Sonde (`identity` meldet seinen Befund mit `1`, `2` heisst dort «kein
pyproject.toml») und bucht **jeden Code, den kein Vertrag kennt — 124, 137 —
als nicht gemessen statt als Befund**. Ein Ziel ohne Checkout wird namentlich
ausgewiesen und zählt gegen den Nenner, ist aber keine Deckung. Unvollständige
Deckung liefert `1` und steht vor einem Befund (`2`). Die Begründung für Treiber
statt Schleifen steht in `docs/probes/README.md`.

### Changed — `nightly-audit.sh` iteriert über das Manifest statt über eine Konstante

Bis jetzt stand in Zeile 168 `TARGET_REPO="${TARGET_REPO:-malkreide/zurich-opendata-mcp}"`.
Der nächtliche Lauf berichtete damit über einen Server von 44, und nichts in
seiner Ausgabe sagte das — dieselbe Klasse wie der Lauf oben. Die Vorgabe ist
weg. Das Skript verlangt eine von zwei ausdrücklichen Antworten: `AUDIT_MANIFEST`
(Sweep über das Portfolio) oder `TARGET_REPO` (ein benanntes Ziel); beides
zusammen wird abgelehnt, keines von beiden auch. Im Sweep liest es die Zielliste
über `scripts/coverage.py` — die Shell zerlegt das Manifest nicht selbst — und
ruft sich je Ziel selbst auf, mit eigenem `AUDIT_DIR` und einer äusseren
Zeitschranke (`SWEEP_TIMEOUT_TARGET`, 7200s) über den Gate-Schranken. Ein Kind,
das hart fehlschlägt, ist weder sauber noch ein Befund: es steht unter
`OHNE ERGEBNIS` und kostet den Sweep seine Vollständigkeit.

### Fixed — die eigene Blindheit der Published-Probe wurde als Befund gebucht

`_run` meldet einen gescheiterten Unterprozess als `{"error": …}`, und
`.get("scripts") or []` machte daraus eine leere Liste. Ein fehlgeschlagenes
Auflisten der Console-Scripts las sich dadurch als «die Distribution deklariert
kein Console-Script» — `no_entrypoint`, also `smoke_failed`, also ein Fund gegen
das Ziel. Die schlimmere Richtung von «nicht hingesehen = nichts da».

Jetzt drei Sätze statt einem: ein gescheitertes Auflisten ist `error` (nicht
gemessen) mit dem Fehlertext als Beleg; eine leere, aber gelaufene Auflistung
bleibt ein Befund und nennt ihre Beobachtung; und «deklariert, aber nicht
installiert» ist ein eigener Befund mit den deklarierten Namen. Alle Zweige der
übrigen Sonden, die `unverified`/`no_*`/`unconfirmed` setzen, wurden dabei
durchgegangen — sie tragen ihre Beobachtung bereits.

### Fixed — a declared property was only checked when the checkouts were there

`reference_drift_probe.py` skipped the whole declared-property comparison when no
adoption site could be read. That conflated two questions with different
prerequisites. Whether a *server* lags needs that server to have been read.
Whether the *template* satisfies a property the manifest itself declares needs
nothing but the template — it is a claim about one file.

The cost was concrete and it showed up on the first real use. A manifest is
written on a machine that has the skill repository and usually not the eleven
server checkouts; on exactly that run the probe reported no declared finding at
all. Against the real `reference/` of `mcp-data-source-probe` the ungated check
now reports five: no `Retry-After`, no jitter, no cap applied after the jitter,
no wall-clock budget, and the `raise RuntimeError` of the 2026-08-03 case still
in place. Under the old behaviour every one of them was silent.

The finding now states which half was measured — "no adoption site could be read,
so how far the servers are ahead of it was not measured" — rather than implying
a comparison that did not happen. `REFERENCE_UNADOPTED` still requires a readable
site and stays quiet without one.

`coverage.md` records the same split honestly: the template side has now been run
against a real template, the server side never has. The unanimity layer — the one
that finds a fix nobody wrote a property for — needs three readable sites and has
therefore never run outside the tests.


### Added — `reference_drift_probe.py`: the template nobody is looking at

On 2026-08-03 `reference/retry_backoff.py` in `mcp-data-source-probe-skill` was
found carrying five defects that eleven servers had already fixed, one at a time,
over about three months. The originating commit of `swiss-efv-mcp` contained the
same line word for word — `raise RuntimeError(f"Upstream unreachable after
retries: {last_error}")` — and the CI failure it produced was what started the
round.

None of the eleven reviews saw it, and none of them could have. In each
repository only the copied fragment was visible; the template was somewhere else,
and nobody had both open at once. The artefact with the largest blast radius is
the one no diff contains.

The probe asks it in **both** directions, and reports the worse one first.
`REFERENCE_STALE` — the template behind the servers — is a defect being
*distributed*: it is handed to every server built next, at the moment somebody is
least likely to read the code they are copying. `REFERENCE_UNADOPTED` — a server
behind the template — is one repository carrying one defect, and it is bounded.
`UNVERIFIED` is the third answer this repository always keeps: a repository not
on disk, a symbol that moved, a template that does not parse. Coverage is stated
on every run, n of m declared sites read, so a narrow run cannot pass for a clean
one.

**The mapping is declared and never guessed.** It comes from
`reference/adoption.toml` in the skill repository — which template, which
repositories, which file and symbol, since when — with no name-similarity
fallback. A guessed mapping is worse here than no mapping: "this function
resembles the template and differs from it" is unretraceable, and unretraceable
findings are how a gate gets switched off. A missing manifest beside a shipped
template is therefore itself the first finding (`MANIFEST_MISSING`), not a
"not measured", and the probe stops there. Format reference:
`adoption.example.toml`.

**What gets compared was the decision worth making.** A full-text diff is
unusable — the adopters rename the constants, rewrap the lines, reword the
messages and rename the function, and every one of those is a *correct* adoption.
A function-name comparison is the opposite failure: it was satisfied by all
eleven servers throughout. So the probe compares the properties the template
guarantees, as small AST predicates: does it read `Retry-After`, does it jitter,
does it cap *after* jittering (`min` wrapping the jitter, not beside it), does it
bound wall-clock time, does it fail with a typed error. Import aliasing is
resolved first, because `import random as rnd` is a naming convention and not
drift.

That leaves one blind spot, and it is exactly the incident: whoever forgets the
fix in the template forgets to write the property down too. So a second layer
compares three facts that need no declaration — called function names, raised
types, caught types, last dotted segment only, because those are what copying
does not rename — and reports one only on **unanimity**: every readable adoption
site agrees, at least three of them, and the template differs. Eleven
independently maintained repositories agreeing is evidence; two are a
coincidence. On a reconstruction of that tree it names the `RuntimeError` line
with nothing declared about it. The layer produces `REFERENCE_STALE` and nothing
else — an `UNADOPTED` verdict needs a declared property, because one server
differing from ten is ordinary variation, not drift.

Nothing is fetched. `--repos-root` reads checkouts already on disk, so a network
failure cannot masquerade as a repository nobody has cloned.

`skills/reference-drift-probe/SKILL.md`, `docs/probes/reference-drift.md`, 50
tests — more of them for correct-but-renamed adoptions than for drift, since a
probe that cannot tell those apart produces a red run everybody learns to ignore.

### Changed — the chain row for `mcp-transport-hardening-skill`, and the probe it credits

That skill went to **v2.0.0**, seven rules to twelve, following spec
`2026-07-28`. Its row here still asked only «does it come up, and does it turn
away the right callers?» — which is now two thirds of the question. The
stateless half is missing, and this repository is the one that measures it:
`spec_probe.py` exists precisely because «which spec is this server on?» had no
answer anywhere before it.

**And the row credited the wrong probe.** It said the transport path was «the
transport path the canary probe exercises live». The canary
(`recall_canary.py`) calls a server's own tools and asserts a recall floor — it
is the answer to `mcp-data-fidelity-skill`, not to transport hardening. The
probe that boots the target under each configured transport and speaks real MCP
to it is `transport_boot_probe.py`, whose own docstring cites
`parlament-mcp#29` — the same incident the transport skill's rules 1 and 2 come
from. Both repositories were describing the same event and only one of them
named it.

Corrected in both language versions, with `spec_probe.py`'s `SPEC_DRIFT` and
`LEGACY_TRANSPORT` named for the stateless and legacy-SSE rules. No probe, gate
or schema changed — this is the map, not the territory.

### Fixed — the nightly routing closes its issues, and knows when it may not

The other half of the delivery path #66 repaired. `sync_findings_issues.py` ran
on `exit == 2` only and never closed anything, so a schema drift fixed in March
kept its ticket open into June. An issue nothing closes becomes noise, and the
answer to noise is to switch the routing off.

**Closing on `outcome == "green"` would have been the wrong fix, and the summary
says so itself.** `nightly_audit_report.py` computes `green` deliberately
*without* the not-measured flags — its own comment reads "transport_boot_unmeasured
and lockfile_unmeasured deliberately absent" — so a green night can carry a gate
that produced no verdict at all. Two are load-bearing:

* `graded_layer_ran` false — the determ-only promptfoo profile ran, so the
  model-graded red-team layer was never exercised. The report already refuses to
  read that as "red-team clear"; closing the `redteam` issue on it is the same
  claim in a louder form, because a closed issue reads as *fixed*.
* `host_allowlist_unconfigured` — fail-open is a documented deployment state, and
  not a measurement of the control.

So each label now gets the three answers this repository already applies to every
probe report: **finding** opens or updates, **clear** closes, **unknown** touches
nothing. A hard-fail leaves every label unknown. Unknown short-circuits before
any call at all, read-only ones included — `_ensure_label` creates a label when
it is missing, and an unmeasured class must not leave even that trace.

The close only becomes reachable if the flow calls the script on a green night,
so `openclaw/cron/nightly-audit.json` and `docs/cron/nightly-audit.md` now do.
Without that the whole state machine would have been unreachable code.

**Also fixed, found while mapping the classes:** `shipped_artifact_fail` and
`lockfile_fail` both move `outcome` to `findings` and had no entry in `_CLASSES`,
so a night whose only red gate was one of them classified as a finding and then
opened nothing at all. That is the third instance of one defect — the comment in
`_CLASSES` records the first two being fixed by hand in an earlier iteration.
`test_every_finding_key_in_the_summary_is_routed` now reads the `green = not (…)`
block out of `nightly_audit_report.py` and fails on the next one, in both
directions: a routed key that nobody writes any more is dead routing that reads
as coverage.

16 mutations, all 16 caught. Two survived the first pass — both in `sync()`,
which had no tests at all, only its callers did.

### Fixed — the shipped templates now hold to two rules the auditor already had

Two gaps in `.github/workflows/*.yml.template`. A defect in a template multiplies
across every repository it was copied into, which is what makes both of these
worth more than their size.

**`ci.yml.template` demanded less than `lint.yml` runs.** The template shipped
`uv run ruff check` and nothing else, while the auditor has run `ruff check` *and*
`ruff format --check` on its own code since #52. `.github/dogfood.yml` recorded the
asymmetry in prose — "which the template does not demand" — and left it standing.
`ruff check` establishes nothing about formatting; a green linter beside a red
`ruff format --check` is an ordinary state. Both now ship as separate steps, so the
log names which of the two failed.

Alongside them, a step stating where the pin belongs. Ruff comes from the target's
own dev dependencies, so the version is the target's `uv.lock` — the right place
for it, but only if there is one. Without a lockfile entry `uv sync` re-resolves on
every run, and the next release that touches the formatter turns CI red on
untouched code in every generated repository at once. The step fails closed on a
missing lockfile or a missing ruff entry. No `--line-length` anywhere: the width is
the target's decision, read from its own config, and the portfolio runs 88, 100,
110 and 120 side by side.

**`live-probe.yml.template` had a guard that could only ever get louder.** It
opened a drift issue and commented on it, and never closed it. An issue that only
grows is read as noise within weeks, and the answer to noise is to switch the guard
off — so the missing close decides whether the job survives, not how polite it is.

Adding a close to what was there would have been worse than leaving it. The inline
`github-script` block classified two states, gated on `alert == 'true'`, so "no
finding" and "the probes never ran" were the same value; a close on that value
closes an open issue on the strength of a comparison that never happened, and the
close then reads as evidence the finding was fixed. This repository already has the
rule that prevents it — clean / finding / **not measured**, `docs/probes/README.md`
— applied to every probe's report and to nothing that delivered one.

`scripts/drift_issue.py` applies it to the delivery: **finding** opens or updates,
**clear** closes, **unknown** touches nothing in either direction. Unknown covers
the crashed probe, the canary skipped because `pip install -e .` failed, and the
report that is missing or empty. The predictable consequence — an open issue stays
open while the probes cannot run — is the intended one. The routing step now runs
under `always()`, without which clear and unknown are unreachable, and passes step
outcomes through the environment rather than interpolating them into a shell.

It is a script with `tests/test_drift_issue.py` against it rather than a heredoc
because the two-state collapse sat in inline YAML for the life of the file:
nothing there can be unit-tested, so nothing there can be mutation-tested. 14
mutations, one per guarantee — fold unknown into clear, drop the report check,
accept a skipped step as having run, ignore the dedup marker, close without
commenting first — and all 14 are caught by a named test. No survivors.

### Added — the auditor's own coverage claim: nothing here has met a live server

`docs/probes/coverage.md`. Every probe in this repository distinguishes clean
from finding from **not measured**, and `portfolio_scan` refuses an overall
verdict when a sweep did not cover what it claimed to. That rule had never been
applied one level up, to the auditor itself — and applied, it says something
uncomfortable: **`mcp-continuous-auditor` is not deployed, so no probe here has
ever spoken to a live MCP server.**

The page records what each probe HAS been exercised against — unit fixtures, a
real repository, a package index — and leaves the wire column empty where it is
empty. `spec_probe --url` is the named gap: its `wire` source is the only one
that is evidence rather than somebody's claim, it is verified against both local
fixtures (migrated and legacy shapes), and it has never been pointed at a
deployment. The command that would close it is in the page, together with how to
read each of the three outcomes — including that an unreachable endpoint is
**not a result**.

Also recorded: what needs a host and therefore does not run at all
(`nightly-audit.sh`, the `openclaw/cron` jobs, the Worker/Broker pipeline, the
`live-probe`/`redteam-regen` templates), against the two schedules that do run
inside GitHub Actions and need none (`pr-health`, `telegram-intake`).

Cross-linked from `docs/probes/README.md`, `docs/probes/spec.md` and
`docs/deployment/tier-0.md`, so the claim is visible from the pages that would
otherwise imply the opposite. A green suite is a green fixture; the difference
between this repository and the servers it audits is not that this one has been
verified, but that this one says so.

### Fixed — the spec probe was reading a summary, and got four rules wrong

`spec_probe.py` was written against a written summary of MCP `2026-07-28` rather
than the specification. It said so in its own output and measured rather than
assumed where it could — and it was still wrong about all three rules it named,
plus a fourth in the promptfoo profile. Two of the four would have produced
exactly the false finding the probe exists to prevent: a `LEGACY_TRANSPORT`
against a fully migrated server.

Read from the [specification](https://modelcontextprotocol.io/specification/2026-07-28)
on 2026-08-05:

1. **`_meta` was in the wrong place, with the wrong keys.** It belongs in
   `params`, not at the JSON-RPC message root, and its keys are namespaced —
   `io.modelcontextprotocol/protocolVersion` / `clientInfo` /
   `clientCapabilities`. The `MCP-Protocol-Version` header **MUST** match the
   `_meta` value, and a mismatch is a mandatory `400` with `-32020
   HeaderMismatch`. A compliant server would have rejected the probe's own
   stateless call, and the probe would have called that «still requires
   `initialize`». The same bug was in `transport_boot_probe.py`, where it would
   have re-created the false «the server never came up» the `STATELESS` status
   had just removed.

2. **`Mcp-Name` was sent on every call, empty.** It mirrors `params.name` or
   `params.uri` and is required only for `tools/call`, `resources/read` and
   `prompts/get`. An empty header with no body field to match is a
   `HeaderMismatch` by the same rule that requires it. Same false finding,
   second route. `transport_boot_probe` now also sends the `Mcp-Method` header
   it was missing entirely.

3. **The deprecation clock is not one clock.** The twelve-month window governs
   Roots, Sampling, Logging and DCR (deprecated `2026-07-28`, eligible
   `2027-07-28`). The HTTP+SSE transport was deprecated in `2025-03-26` and its
   earliest removal is «three months after SEP-2596 reaches Final» — a date the
   registry does not give, so it is **not computable**. The `2027-07-28` the
   probe printed for an answering `/sse` endpoint appears nowhere in the
   specification. `Mcp-Session-Id` is not deprecated at all: removed outright,
   no window. And «earliest removal» is **eligibility, never a deadline** —
   «Features may remain Deprecated, without removal, for much longer than the
   minimum deprecation window.»

4. **`cacheScope`'s vocabulary was invented.** The determ assert listed
   `session|client|global|none`; the spec says `public` | `private`. It would
   have failed every compliant server and passed none — the precise inversion of
   what an assert is for.

`--spec-verified` is gone. A flag that promised to change only wording was the
wrong shape for this: the rules are now read from the document, and every one of
them names the page it came from in the source, so the next reader re-checks
instead of trusting the file. `SPEC_SOURCE` and `SPEC_VERIFIED_ON` go into every
report. `deprecation_deadline` in the JSON report is renamed
`twelve_month_eligibility`, because a single field applied to every signal is how
the invented date got in.

Also corrected: `server/discover` is stronger than assumed — **servers MUST
implement it**, it is the client that MAY call it — and its advertised versions
are now read. Results carry a required `resultType` from this revision on, which
an older server cannot produce by accident, so it is recorded as positive
evidence.

### Fixed — a migrated server is no longer reported as a broken one

`scripts/transport_boot_probe.py` opened every probe with `initialize`. Spec
`2026-07-28` removes that method, so a server that had completed the migration
answered JSON-RPC `-32601`, the gate reported *«the server never came up»*, and
exit `2` travelled through `nightly_audit_report.py` into
`sync_findings_issues.py` and ended as a GitHub issue. The first server in the
portfolio to finish the migration would have been issued a bug report for
finishing it.

A rejected handshake now triggers a second question instead of a verdict: can the
server serve a real call with no handshake at all? If it can, the outcome is the
new status `STATELESS` — a pass with a label. If it cannot, the original failure
stands. Only `-32601` (or a *method not found* message) takes that branch; an
internal error or a crash keeps failing, because those are what the gate is for.
The same shape as the existing `NOT_SELECTED` outcome, for the same reason.

`initialize` was also the endpoint **discovery** mechanism for streamable HTTP
(`_resolve_path`), so a stateless server broke path resolution itself and the
gate reported a broken transport about a path it had never reached. Discovery now
falls back to a handshake-free call.

### Added — the spec probe: which protocol version does this server actually speak?

`scripts/spec_probe.py`. The question had no answer anywhere in this repository,
and not because it was hard to measure. The boot gate carried one hand-maintained
literal — `_PROTOCOL_VERSION = "2025-06-18"` — sent it in `initialize` and in the
`MCP-Protocol-Version` header of every POST, and **never read the answer back**:
`result.protocolVersion` arrived on every successful boot and was discarded, since
only `result.tools` was ever looked at. One literal fanned out by import through
`shipped_probe.py` and `rebind_probe.py` into three gates, none of which could say
what they had been talking to. That is the defect class `identity_probe` was
written to catch, in the auditor's own source.

The probe compares four sources — the target's code, the **installed** SDK, the
index repo's `mcp_spec_version`, and the live wire — and reports `SPEC_DRIFT`,
`LEGACY_TRANSPORT` (each signal on its own deprecation footing), or
`UNVERIFIED`. The artifact level is the one the source cannot reach: during this
migration a target's source does not change at all, the SDK version does.

`SPEC_UNDECLARED` is a **note and not a finding**. Under the current SDKs the
protocol version belongs to the SDK; 39 of the 42 servers declare nothing and are
right not to, and a predicate that turned red on all 39 would be switched off
within a day.

The wire mode sends the stateless call twice, once with the required request
headers and once without, and reports both status codes — which is what tells a
server that **enforces** them from one that merely tolerates them.

`--now` pins the date. A countdown is time-dependent, which breaks the
provenance promise from the other side — the same commit would otherwise produce
a different report tomorrow.

### Added — `transport_boot_probe` reads the version the server names

`negotiated_version()` pulls `result.protocolVersion` out of the `initialize`
result into the per-transport evidence and the report (`schema` 2 → 3), and
`MCP_PROTOCOL_VERSION` overrides what the gate sends. A successful SSE boot now
also records `legacy_transport`, because HTTP+SSE is the deprecated form.

### Added — two migration predicates in the portfolio fan-out

* `sdk_upper_bound` — is the resolved SDK major fixed, or whatever the next
  install picks? `fastmcp` 4.0 is released and breaking, and `fastmcp` 3.x pins
  `mcp<2.0`, so an unbounded server can be moved onto an incompatible line by an
  unrelated `uv sync` while nothing in its own repository changes.
* `sdk_flavour` — the official `mcp` SDK or the standalone `fastmcp` package. Two
  different projects that cannot share an environment, and `sdk_major` reports
  `2` for both, which is not the same statement.

Both report **every** target and flag only the concrete risk; neither goes looking
for a known outlier, because a predicate built around an expected answer confirms
it. Neither is in `DEFAULT_PREDICATES`: adding a predicate to the default set
changes the verdict of every existing targets file without anybody editing it.

### Added — list-response asserts in the key-less promptfoo profile

A `list:` variable in `promptfoo/providers/call_tool.py` and four tests asserting
that `tools/list` and `resources/list` come back in a deterministic order and
carry a well-formed `ttlMs`/`cacheScope` pair.

There is deliberately **no** stateless assert here. The provider drives the
in-memory client in-process and there is no wire, so *«a call without `initialize`
succeeds»* would be a statement about the SDK and would pass for every server,
migrated or not — the false green this repository exists to prevent. That question
is measured in `spec_probe.py --url` or it is not measured. The `ttlMs` pair is
baseline-gated for the mirror-image reason: whether the installed SDK surfaces the
fields is a fact about the SDK, so the assert fails only on a value that is
present and malformed, and `raw_shape` keeps «the server omitted it» apart from
«this client cannot see it».

## [0.2.0] - 2026-08-03 — the probe family, and what a report may not claim

Six weeks of accumulation get a version. `0.1.0` described an auditor with one
deterministic gate (promptfoo plus schema drift) against one target repository.
What ships here is a **family of probes**, each answering a question that the
gate before it could not see — and, running through all of them, one rule about
what a report is allowed to say.

**The probes, in the order the gaps appeared.** Recall floors, because a
collapsed result set keeps its JSON shape and a structural diff cannot see it.
Identity, because a hand-maintained version in the User-Agent drifts silently —
12 of 30 servers were sending a wrong one, 4 of them a wrong major. Shipped,
because CI tests the branch and users install the artifact. Yank, published,
lockfile, doc-claim and bilingual parity followed, each from a case where every
existing check was green and the answer was still wrong.

**The rule underneath them.** Every probe distinguishes *«could not measure»*
from *«in order»* — `NO_TAGS`, `STALE_ARTIFACT`, `UNCONFIRMED`, `UNVERIFIED`,
`LOCK_DRIFT` — and every report names the commit it is about. If the tree moved
mid-run, the status is `MOVED_DURING_RUN` and the exit code is `4`: no verdict,
because the run did not read one tree. A probe that degrades into a plausible
success is the exact failure it exists to catch.

**Operationally**, the nightly audit gained the gates that go with those probes,
including the lockfile gate placed deliberately *before* `uv sync`, because
`uv sync` re-locks and a gate after it reads a file its own harness just
repaired.

Version `0.2.0` rather than `0.1.1`: several of these entries change existing
behaviour (`release_gap.py` merged into `shipped_probe.py`, its compatibility
shim removed, two tests re-encoding a changed policy). Under semantic versioning
before `1.0.0`, that is a minor bump — the API is not yet stable, and this
release does not claim it is.

The individual entries below are unchanged; only this summary and the heading
are new.

### Added — the lockfile gate runs in the nightly audit, and it runs BEFORE `uv sync`

`lockfile_probe.py` shipped standalone. It is now step **1b** of
`scripts/nightly-audit.sh`, and where it sits in that script is the substance of
this change rather than a detail of it.

**`uv sync` re-locks.** Measured, not assumed: against a checkout whose `uv.lock`
recorded `mcp[cli]>=1.28.1` while `pyproject.toml` said `>=2.0.0,<3`, a single
`uv sync --offline` rewrote the recorded specifier to the pyproject one — the
install then failed for want of a cached wheel, and the drift was gone either
way. The nightly runs `uv sync --all-extras --dev` immediately after
provisioning, so a lockfile gate placed with the other gates would read a file
its own harness had just repaired and report every target clean. That is not a
gate, it is a mirror.

So it runs on the checkout as cloned, before anything touches it, launched with
`python3` rather than `uv run` — the target venv does not exist yet at that
point, which is why the probe is stdlib-only. Nothing else in the script would
fail if somebody moved the block down, so `tests/test_gate_timeouts.py` pins the
order, the boundedness, and the absence of `uv run`.

- **Exit 3 ("no lockfile") does not turn the run red.** It gets its own gate line
  (`🟡 no lockfile in the target — not measured`) and its own block in the
  report. Across this portfolio 19 of 20 servers commit no lock, and a gate that
  went red on all of them for a defensible choice would be switched off within a
  day, taking the one real finding with it. It is equally not a pass: the report
  says a lock nobody syncs from and no lock at all look identical from here.
- **Exit 4 (`MOVED_DURING_RUN`) and 126/127 are hard failures, not findings.** A
  run that read two different trees, or none, has established nothing about the
  target, and charging it a dependency defect would be a claim nothing measured.
- **`LOCKFILE_GATE=off`** disables it; **`GATE_TIMEOUT_LOCKFILE`** bounds it
  (default 300s — `uv lock --check` may consult the index).

**This is a rollout step, not a cosmetic one.** `lockfile` joins `_GATE_NAMES`,
so an evidence file that does not carry it reads as 127 and hard-fails — correct
for a Worker image that genuinely did not run the gate. Roll the Worker and the
Broker together; `docs/deployment/worker-broker-rollout.md` lists the new field
and its verification snippet checks for it.

The case it was wired in for is on the record: `swiss-procurement-mcp` #37 merged
`structlog>=24.1,<27` and `starlette>=0.37,<2` into `pyproject.toml` at 14:42 UTC
and did not regenerate `uv.lock`. The gap was found by hand and closed at 20:33
UTC. Run against that merge commit, the probe reports both diverging specifiers
and `LOCK_STALE` — six hours earlier, and without anyone looking.

### Added — every report names the commit it is about (`probe_provenance.py`)

An identity-probe finding (`VERSION = "0.4.0"` as a hand-maintained literal) was
correct when it was measured and false ten minutes later: `main` had moved, and
the report named no commit. It was not wrong — it was *unanchored*, which is
worse, because a wrong finding gets argued with and an unanchored one gets filed.

- **`scripts/probe_provenance.py`** — `capture(target)` at the start of a run,
  `recheck()` at the end. Records `HEAD`, the branch, whether the checkout is
  shallow, and a digest of `git status --porcelain`; the digest is what catches
  a `git stash pop` or an editor save, which move the tree while `HEAD` stays
  put and which are the commoner case. Probe byproducts (`__pycache__`,
  egg-info, venvs) are excluded by name — a probe crying wolf at its own
  footprints is the fastest way to get a check switched off.
- **Four statuses, not two**: `PINNED`, `PINNED_DIRTY` (it did not move, but the
  SHA does not reproduce it either), `MOVED_DURING_RUN`, and `UNPINNED` for a
  target that is not a git checkout — an unpacked sdist is a legitimate target
  and must not be failed for it.
- **Exit code `4` = `MOVED_DURING_RUN`**, shared by every probe that reads a
  tree, and outside the 0/2/3/127 vocabulary the gates already read.
  `portfolio_scan` maps an unknown code to an error cell, which is the correct
  reading of a run that reached no verdict.
- **`decisive=False` for the index probes.** `yank_probe` and `published_probe`
  read a package index; the checkout only tells them which distribution to ask
  about. Withdrawing a catalogue finding because somebody committed locally
  would be superstition, so those runs record and print the move and keep their
  verdict. `blocking`, not `moved`, is what a probe branches on.
- Wired into `identity_probe`, `shipped_probe`, `yank_probe`, `published_probe`,
  `transport_boot_probe`, `rebind_probe`, `live_probe` and `recall_canary`. The
  three manifest-driven ones pin the *auditor's* commit: "the canary was green"
  is a different claim depending on which revision of the manifest it walked.
- `published_probe --format json` now always emits an object
  (`provenance` / `results` / `coverage`) rather than a bare list. The bare list
  had nowhere to put the commit, which is the defect being fixed.

### Added — `lockfile_probe.py`: is the declared bound in force where the install happens?

The upper bounds from a portfolio PR were merged, reviewed and green — in
`pyproject.toml`. `uv.lock` was not regenerated, so its recorded `requires-dist`
still carried the uncapped range and its pins came from the pre-bound
resolution. On `main`, the fix was in the file everybody reads and absent from
the file that installs. `yank_probe` could not see it: that one reads published
metadata off the index, and this happens earlier, on the branch.

- **`LOCK_DRIFT`** prints *both* diverging specifiers, because "the lock is out
  of date" is a sentence somebody has to act on and the pair is the whole of the
  action. Where the difference is specifically a missing cap it says so: *the
  upper bound is in pyproject.toml and NOT in the lock*.
- **`LOCK_UNSATISFIED`** — the pinned version is not admitted by the declared
  specifier. The stronger claim: what installs violates what is declared.
- **`LOCK_STALE`** from `uv lock --check` / `poetry check --lock`, and a missing
  tool is *reported*, never counted as agreement. A resolver that could not
  reach an index has not disagreed with the lock; that is classified apart from
  staleness so an infrastructure failure is not filed against the repository.
- Specifiers are compared as parsed clause sets: `>=2.0.0,<3` and `<3,>=2.0.0`
  are one requirement, `<3` and `<3.0` one bound. Reporting those as drift would
  have the check muted inside a week.
- **No lockfile is exit `3`, not a finding.** A library that ships no lock has
  made a defensible choice, and a red gate there teaches people to commit a lock
  they never sync from.
- `uv lock` is only ever invoked **with `--check`** — without it, the command
  regenerates the very file under audit. A test asserts on the exact argv.

### Added — `doc_claim_probe.py`: the identifiers the documentation cites must exist

An `ARCH-003` justification named ten rubric codes as the ones it had been
graded against. None of the ten was in `GREEN_RUBRICS`. Review did not catch it,
because checking meant opening ten files to look up ten constants.

- Checks three shapes, and only inside backticks: **identifier codes**
  (`LOCK_DRIFT`, `ARCH-003` — a lowercase letter takes a token out of scope,
  which keeps `Requires-Dist` and `User-Agent` out of the findings), **paths**,
  and **collection membership** against a constant the code actually defines.
- Resolution runs against the repository's **non-Markdown** files, so a rubric
  defined in promptfoo YAML resolves and a code that appears only in the README,
  the German README and the CHANGELOG does not: that is repetition, not a
  definition.
- Fenced blocks are read for **paths only** — a command in an example is a
  claim, its sample output is illustration.
- Standards citations and identifiers on lines linking to another repository are
  exempt **and listed**: this README's `OPS-005` belongs to `mcp-audit-skill`,
  and an exemption nobody can see is indistinguishable from a blind spot.

### Added — `parity_probe.py`: the EN/DE documentation pair must stay parallel

The portfolio is bilingual, the English side moves first, and both files render
— so a section that exists in one and not the other is invisible without reading
both and counting.

- Compares the **heading level skeleton** (never the heading text: `Overview` and
  `Übersicht` are a correct translation), top-level bullet counts per section,
  **language-tagged** code blocks, and link targets. Untagged fences are counted
  but not compared — a directory tree is prose in a monospace font. Comments
  inside blocks are stripped, whole-line and trailing.
- Only the **first** heading divergence is reported: after one missing section
  every later position is shifted, and forty consequential mismatches bury the
  one that has to be fixed.
- **`TRANSLATION_LAG`** counts commits touching the base after the last commit
  touching the translation — the only check that fires while every structural
  one is green, which is what a half-translated paragraph looks like.
- The cross-language link is excluded in both directions; it is the one link
  that is supposed to differ.

### Changed — the README's `## Features` is a reference again, and the case histories moved to `docs/probes/`

Four of the bullets had grown into 400-word paragraphs. The substance was right
and the section had stopped being usable as a reference — nobody scans a feature
list to read an incident report.

- Each probe is now **three lines**: the question it asks, what the script does,
  and the one fact that licenses it — with a link to its page.
- **`docs/probes/`** carries the case histories, one page per probe (`identity`,
  `shipped`, `yank`, `published`, `lockfile`, `doc-claim`, `parity`,
  `provenance`) plus an index that states the rule they all follow: clean,
  finding and *not measured* are three answers, never two.
- Both READMEs were changed in the same commit, and `parity_probe` was run
  against this repository to prove it. `tests/test_parity_probe.py` and
  `tests/test_doc_claim_probe.py` now each carry a case that holds this
  repository's own documentation to the check it ships.
- New skills: `skills/lockfile-probe`, `skills/doc-claim-probe`,
  `skills/parity-probe`.

### Added — the repository says which chain it belongs to, and a test keeps it saying so

This repository is the last of five that were written for the same failure class
— a system reporting success while being wrong — and it was the only one that
said so nowhere. The four skills each carried a table naming their siblings;
this one had no such section, in either language. Where it was mentioned, in the
probe skill's README, it was a trailing sentence *after* the table, so it read
as an aside rather than as the fifth link.

That is not a cosmetic gap. On GitHub the intersection of the five
repositories' topics was **empty**, and this repository had no topics at all —
so nothing tied it to the other four for anyone who had not already found one
of them.

- **`## Related repositories` in both READMEs**, with the chain table and a link
  to the shared topic [`mcp-quality-chain`](https://github.com/topics/mcp-quality-chain).
  Each row says what that repository contributes here: the probe skill's step
  1.4 recall ground truth is what the `min_count` floors measure against, and
  `mcp-audit-skill`'s `OPS-005` came out of this repository's own [#29](https://github.com/malkreide/mcp-continuous-auditor/pull/29).
- **`tests/test_quality_chain_table.py`** — the table must name all five in both
  languages and link the topic page. It deliberately does not try to check the
  GitHub topic itself: that lives outside every working copy, and the guard for
  it (`tools/check_quality_chain.py`) belongs to `mcp-audit-skill`, the one
  repository carrying the manifest. Stdlib-only, no network, no git.

### Added — `published_probe.py`: imports and start decide the status, and the published dependency ranges are read

The probe installed the artifact and read one thing off it: the User-Agent. Every
import failure it hit on the way landed in `import_errors` and changed nothing,
so a package that could not be imported at all could still be reported
`no_user_agent` — a statement about code the probe never executed.

**Import errors are now a finding (`broken_import`, exit 1)** — and the naive
version of that check is wrong, which the portfolio had already proved.
`bag-health-mcp` was reported as having a circular import. It has none:
`import bag_health_mcp.server` runs cleanly in a fresh venv. What failed was
importing the private submodule as the *very first import of the process*,
before its own package root had initialised. So the rule is the one the evidence
supports — **import the package root first, then the submodules, and whatever
still fails after that is real** — and every failure the bulk scan sees is
re-measured in two fresh interpreters, `cold` (submodule first) and `warm` (root,
then submodule). `warm` is what a user's code does and is what decides. Three
non-findings are named apart from each other rather than lumped together: an
import-order artefact, a failure that reproduces in neither fresh interpreter,
and a module missing something the distribution declares only behind an extra (a
shipped test module importing `pytest` is not the server being broken). A
verification that could not run at all counts as real — absence of proof is not a
pass, the same rule `unverified` already followed.

**A smoke stage, because import success is not start success.** `parlament-mcp#29`
raised at start and the process never came up, which no amount of importing
shows. The installed **console script** — the thing a user types — is now run
with stdin closed for a few seconds and must announce a `server.start` event
without crashing. stdin closed is deliberate: a stdio server reads EOF and shuts
down cleanly, so the exit code is not the signal and the announcement before it
is. A clean exit that announced nothing is `smoke_unverified`, not a pass.
`--start-event` renames the event, `--no-smoke` skips the stage.

**Missing upper bounds in the published metadata.** `swiss-energy-mcp` 0.3.3
shipped `mcp[cli]>=1.20.0` uncapped; the day `mcp` 2.0.0 was published, every
fresh install of that release died on import. The artifact did not change, the
resolver's answer did. `requires_dist` of the installed artifact is now read, and
a missing upper bound is reported for the **import-critical** dependencies —
measured, from the modules that actually appear in `sys.modules` after importing
the package, not from a list of names somebody thought looked important. Two
tiers: `UNCAPPED` when the index **already** serves a higher major than the
declared floor (the finding, and it arrives before the break rather than after
it), and *armed* when no higher major is published yet. An index that could not
be read is `unknown`, never `capped`. Measured end to end against `httpie` 3.2.4,
which carries three such ranges. `Requires-Dist` parsing and the PEP 440 bound
semantics are reused from `yank_probe.py` rather than reimplemented.

**`--version` pins the install.** `pip install <dist>` was measured serving the
PREVIOUS artifact for minutes after the new version was listed — `--no-cache-dir`
empties pip's cache and not the index's. A re-check after a release that does not
pin is a re-check of the release before it. A venv that comes back holding
another version exits 2 rather than quietly measuring the wrong artifact.

Every layer keeps its own line in the output and its own field in `--format
json`: only one of them can be the *status*, and hiding two true facts behind a
precedence rule is not a summary. The status precedence is
`broken_import` > `smoke_failed` > `drift`/`foreign_user_agent` >
`unbounded_dependency` > `smoke_unverified` > `unverified` > `ok`, ordered by how
much of the rest of the report each failure invalidates.

### Added — `shipped_probe.py`: three anchors, and a version pin

* **`NO_TAGS`** — a repository with no release tags at all is now its own
  finding. `PUBLISH_GAP`, `TAG_NOT_ON_INDEX` and `UNTAGGED_VERSION` all measure
  against the last tag and therefore measured *nothing* there, and `UNRELEASED`
  counted every commit in history rather than the ones past a release. A green
  run on an untagged repository was a statement about how little was compared.
  A shallow clone is emphatically **not** this: `git tag --list` succeeds with
  empty output in a `--depth 1` clone, so `release_tags` now asks
  `git rev-parse --is-shallow-repository` and returns *undeterminable* rather
  than accusing every shallow checkout in the fan-out.
* **Commit kind beats age.** A `fix:` and a `docs:` sitting unreleased shared one
  seven-day clock, so a fix that had been merged and not released for six days
  was reported as nothing at all — while every one of those days is a day users
  run the behaviour the fix removed. They no longer share a clock: a breaking
  change (`feat!:`, or `BREAKING CHANGE` in the subject) is reported at **any**
  age, user-facing work past `--max-age-days-user-facing` (default **0**, i.e.
  immediately), housekeeping still past `--max-age-days` (default 7). The knob
  exists so anyone wanting a grace period after a merge has one; the default says
  the delay itself is the thing worth seeing.
* **`STALE_ARTIFACT`** — content, not numbers. Every other comparison in the
  probe is between version *numbers*, and numbers agree in exactly the case that
  matters most: the artifact on the index and the tree in the repository both say
  0.3.3 and are not the same code. At the full depth the installed package's
  `*.py` are now compared against the checkout's, and **only** when the two
  version numbers agree — where they differ, `STALE_ON_INDEX` says it more
  directly from cheaper evidence. Line endings are normalised (a wheel built on
  one platform and a checkout on another differ in that byte and nothing else),
  and a file present in the wheel but absent from the tree is reported and is not
  on its own a finding, because a generated `_version.py` from setuptools-scm
  lives exactly there.
* **`--pin-version`** installs `dist==VERSION` for a post-release re-check, on
  the same measurement as `published_probe.py --version`. The default stays
  unpinned: a gate is asking what a user's `pip install` resolves to today, which
  is a different and equally real question. A venv holding another version sets
  `harness_error` (127) — no claim is made about either release.

Report schema bumped 2 → 3 for the `tree` block.

### Added — `portfolio_scan.py`: a sweep must claim its coverage, and ask for the branch

Two failures from the same campaign, neither of which turned anything red.

**Three servers dropped out of the tracking for half a day.** Nothing was
reported about them — they were simply not in the runs, and every run that did
happen said "no findings" about the servers it *had* looked at, which reads
exactly like "no findings". It was noticed because 26 + 4 did not come to 33. So
every run now compares what the targets file declares against what it scanned,
names each gap **by name** (a count is what that campaign already had), and gives
**no overall verdict at all** when the two disagree — which outranks both
`findings` and a clean matrix. `expect_targets:` in the targets file is the
second anchor, against the file itself quietly losing an entry. `--partial` is
how a deliberately narrow `--only` run says so: it keeps the verdict, and the
report still names everything it left out.

**`git remote show origin` answered wrongly for four repositories in one
sitting** — it reads `refs/remotes/origin/HEAD` out of the local clone, written
once at clone time and never refreshed. A target that names no `ref` now has its
default branch resolved with `git ls-remote --symref`, which opens a connection
and asks. Three of this portfolio's repositories run on `master`; a
`--branch main` clone against them fails, and a row of error cells for a healthy
repository is the most expensive kind of false finding because it looks like the
target's fault. `defaults: ref: main` is gone from `targets.example.yaml` — an
unset ref stays unset — the resolved ref is a column in the matrix and a field in
the report, and a resolution that fails clones the remote HEAD with no `--branch`
at all rather than falling back to a name nobody verified.

Report schema bumped 1 → 2 for the `coverage` block and the per-row
`requested_ref`.

### Changed — two tests encode the new policy rather than the old

`test_recent_work_is_not_a_finding` asserted that a `fix:` half a day old is not
a finding. That is the behaviour being replaced; it is now
`test_recent_housekeeping_is_not_a_finding` and asserts the same thing about a
`docs:` commit, which still holds. `test_offline_is_declared_and_not_a_failure`
now tags its fixture repository: `--offline` not being a failure is the only
property it tests, and an untagged repository raises `NO_TAGS` on its own.

Suite: 525 tests in 26 files → 605 in 26.


### Removed — the `release_gap.py` compatibility shim

The shim existed to unbreak callers outside this repository after the merge
deleted the name they invoked. Removed on request now that those callers have
moved.

**This re-breaks anything still calling `scripts/release_gap.py`** — the name no
longer resolves at all, which is a loud failure rather than a quiet one. The
replacement is `shipped_probe.py --target <path> --metadata-only`, and the exit
codes are that probe's: `0` clean, `2` findings, `127` the harness could not
run. A caller testing `$? -eq 1` needs `2`; a directory with no `pyproject.toml`
now gives `127` rather than `2`.

Gone with it: `tests/test_release_gap_shim.py` (17 tests), the shim's entries in
both READMEs, and the paragraph in the shipped-probe skill that described it as
still present.

Kept deliberately: every *historical* mention of the old script — the exit-code
note in the skill and in `shipped_probe.py`'s docstring, and the comments that
explain why the merged probe is shaped the way it is. Those describe how the
current contract came to be, and they are more useful now that the shim no
longer softens the transition, not less. `RELEASE_GAP_LIVE` also keeps its name;
it is the opt-in flag for the live index re-measurement in
`tests/test_release_metadata.py` and renaming it would break a documented
invocation to remove a word.

### Added — `yank_probe.py`: a known-broken release that is still installable

The auditor knew what a yank *is* — `shipped_probe.py` parses PEP 592 flags off
the Simple API and raises `RELEASE_YANKED` when the version users install has
been withdrawn. It had no idea a yank could be **missing**. Nothing anywhere
asked the inverse question, and the inverse is the one the portfolio actually
got wrong:

> Does a known-unusable, **not**-yanked release still exist, with a healthy
> successor beside it?

**The case.** `zurich-opendata-mcp` 0.5.1 declared `mcp[cli]>=1.28.1` with no
upper bound. `mcp` 2.0.0 removed `mcp.server.fastmcp`, so every fresh install
died on import. 0.6.0 fixed it with `mcp[cli]>=2.0.0,<3` — and superseding was
not enough. The broken release stayed selectable for any resolver constrained
away from 0.6.0: an old lockfile, a colliding pin, a `==0.5.1` in somebody's
Dockerfile. It had to be yanked, and it was.

**Two details that decided the design.**

*All six predecessors were affected* — 0.2.0, 0.3.0, 0.3.3, 0.4.0, 0.5.0 and
0.5.1 each carried an uncapped `mcp` range. A probe checking `latest-1` would
have found 0.5.1, reported it, and left five installable broken releases behind
while reading green. So this walks **every** version, and groups the answer by
the dependency boundary rather than by release — because the fix is a list, and
what matters is that the list has six entries and not one. A test asserts the
membership of that list against the real captured metadata of all eight
releases.

*A yank is not a deletion.* After the yank, `pip install
'zurich-opendata-mcp==0.5.1'` still resolves, with a warning. That is PEP 592
working as designed: existing lockfiles do not break. So the finding never says
"delete" — it says "yank", and it says what a yank does and does not do. Tested.

**What the finding is allowed to claim.** "Known-unusable" is a strong word and
metadata alone rarely earns it, so `UNYANKED_BROKEN_RELEASE` needs four
conditions together: the release is unyanked and not a pre-release; its range on
the dependency has no upper bound; the healthy successor declares the same
dependency and its requirement **excludes the older release's own floor**; and
the newest non-pre-release past that boundary is actually published and actually
admitted. The third is the one that licenses the accusation — the maintainer's
own later release has already declared the crossing breaking — and the fourth is
what keeps it from being theoretical. Each condition has a test that removes it
and asserts silence. Without the third, every uncapped dependency on the
internet is a finding and the gate gets muted; `httpx`, `pydantic`, `sqlparse`,
`uvicorn` and `defusedxml` are uncapped in the same six releases and correctly
produce nothing.

**A missing yank reason is its own, lower finding.** `YANK_REASON_MISSING`,
severity `low`. The reason travels through the Simple API and `pip` prints it
verbatim; measured against the live index, all six yanks carry none, so anyone
an old lockfile drops onto them sees `Reason for being yanked: <none given>` and
cannot tell a security withdrawal from a bad build. Cheap to fix, and not in the
same class as a broken release still being installable — hence separate, lower,
and reported second.

**It recommends; it does not act.** There is no `--yank`, no credential is read,
and every request is a GET. Yanking needs a PyPI token with upload scope, it
changes what every resolver on the internet sees, and whether a release was
really unusable is the maintainer's judgement. `ReadOnlyTest` pins all three
properties, because the difference between this probe and a credential-holding
one is exactly one flag and nothing else in the file would fail if somebody
added it.

**Why a separate script and not a third depth of `shipped_probe.py`.**
`--metadata-only` promises two HTTP requests, and `nightly-audit.sh` leans on
that promise — the metadata pre-run documented below exists so the release
verdict survives the full gate hanging, and it only survives because it is
cheap. Answering this question costs O(versions + dependencies) requests, since
the evidence lives in each release's `Requires-Dist`. Folding it into the
metadata depth would break the exact property the pre-run was added for; folding
it into the full depth would hide a catalogue question behind a venv build. It
imports `shipped_probe`'s index primitives rather than copying them — the same
relationship `shipped_probe` has with `transport_boot_probe`.

Reading `Requires-Dist` does **not** download wheels: PEP 658's `core-metadata`
sidecar is fetched from the Simple API, which is also what makes the walk
affordable at all. PyPI's per-version JSON API is the fallback, and only when
the index *is* PyPI — the same precedence `shipped_probe.reconcile` documents.

**One bug found in the writing, worth recording because it is invisible.** PyPI
inlines the whole MIT licence as a *folded* `License:` header, and the blank
lines inside the licence arrive as whitespace-only continuation lines. The first
draft's parser tested "is this line blank" before "is this line a continuation",
ended the header block on the second line of the licence, and read six
dependencies as **zero** — which the probe then reports as a clean catalogue.
Nothing about that failure looks like a failure. It is now a regression test
against the real captured bytes, which is the only way to catch it.

Not wired into `_GATE_NAMES`. That list is fail-closed by design — a name added
there hard-fails every Worker still running the previous `nightly-audit.sh` — so
turning this into a nightly gate is a Worker/Broker rollout step and an
operational decision, not part of adding the probe.

New: `scripts/yank_probe.py`, `skills/yank-probe/SKILL.md`,
`tests/test_yank_probe.py` (40 tests, offline, asserted so under a blocked
socket), and three captured fixtures under `tests/fixtures/pypi/`.

### Fixed — `release_gap.py` is back as a shim, because deleting it broke callers

The merge deleted `scripts/release_gap.py` and folded its question into
`shipped_probe.py --metadata-only`. That was the requested change, and it broke
every caller outside this repository twice over: the name vanished, and anyone
who ported to the new script found the exit codes had moved underneath them.

The alternative on the table was reverting the merge. That was measured — both
merges revert cleanly in the order #44 → #43, 472 tests green — and rejected:
it would have taken the nightly metadata pre-run with it, which is a real
capability, to solve a CLI problem.

So `release_gap.py` exists again as a **deprecated shim**: old name, old flags,
old exit codes, no probe logic. It prints a deprecation notice naming its
replacement on every run, and can be deleted once nothing invokes that name.

**The exit-code translation is the part that had to be thought about**, because
the two vocabularies are not a bijection:

| old (shim) | new (`shipped_probe.py`) |
|---|---|
| `0` no findings | `0` green |
| `1` findings, **or** the comparison could not be made | `2` findings |
| `2` not a Python MCP repo | `127` the harness could not run — unreachable index **or** no distribution name |

`127` covers two old codes at once. A table-driven translation is wrong half the
time: as `2` it would tell a caller *"this is not a Python MCP repo"* about a
repository that plainly is one, when the truth was an unreachable index. So the
`2` case is decided in the shim before forwarding — exactly where the old script
decided it, and a test pins that it is decided without touching the network at
all. Everything else that is not green collapses to `1`, which is the collapse
the old script already made.

**`--format json` is translated too**, key for key. A JSON consumer is a
*program*, and a renamed key breaks it silently — it reads `None` where it used
to read a version and carries on. Only five of the seventeen keys actually
moved, which is why this is a rename table and not a second serialiser:

```
version      <- versions.repo      pypi_status  <- index_status
pypi_version <- index_version      pypi_detail  <- index_detail
                                   ok           <- exit_code == 0
```

The merged report's own additions (`schema`, `depth`, `publication`,
`tool_call`, …) are dropped rather than passed through: a consumer written
against the old contract expects that key set, and handing it a third, wider
shape would be its own kind of surprise. A test pins the emitted keys against
the exact set the old `to_json` produced, taken from
`git show 9dc1934^:scripts/release_gap.py` and hardcoded so it still holds in a
shallow clone.

Not reproduced, and said out loud rather than faked: the **report text** is the
merged probe's. Reproducing the old rendering would mean keeping a second
formatter alive, which is the duplication the merge removed — and a human
reading a report notices a changed layout, where a program reading a renamed key
does not. The finding codes are unchanged, so a caller grepping `PUBLISH_GAP`
still works. A structural test keeps the shim a shim — if it ever grows
`urllib`, `fetch_simple` or a `Finding(`, the duplication is back under a new
name.

### Added — the shipped gate's metadata pre-run reaches the nightly summary

The pre-run below wrote `shipped-metadata.json` and nothing read it, so its
verdict lived in the Worker's log directory — which is exactly where it is *not*
read when the full gate hangs and the summary says only "this gate hung".

`nightly_audit_report.py` now takes `--shipped-metadata-json`, carries the
parsed verdict in the summary as `shipped_metadata`, and renders it as a
sub-line under the shipped gate: what the index serves, how many releases are
yanked, whether the two index APIs disagreed, and any metadata finding codes.

**It is evidence, not a gate**, and that distinction is load-bearing here.
`_GATE_NAMES` is fail-closed — a name in it that an evidence file does not carry
reads as 127 and hard-fails the run. That is correct for a gate an older Worker
genuinely did not run, and wrong for a supplementary report: adding the pre-run
there would have hard-failed every Worker image that predates it. So it is
outside that list, and a missing file is simply `present: false`.

Three properties, all tested:

- **An absent or unparseable report reads as *unknown*, never as clean.** A
  Worker image predating the pre-run reaches this code, and so does one whose
  pre-run was itself killed; neither has shown the release to be healthy.

- **It never moves the outcome.** A `RELEASE_YANKED` from the pre-run does not
  turn a green shipped gate into `findings`. The gate's own exit code is the
  verdict — letting a second probe override it is the same substitution the
  124/137 handling exists to prevent, from the other direction.

- **A hung gate stays a hang**, and the report gains what the pre-run *did*
  establish. With findings, that goes under `Findings`; with none, the nuance
  goes on the gate's own line instead, because "nothing is wrong" does not
  belong under a 🚨 heading. The line then says which half is still open:

```
- shipped-artifact gate (install from PyPI + run it): ⏱ HUNG — killed by the gate timeout (exit 124)
  - release metadata pre-run: index serves `0.7.0` · 6 yanked release(s): 0.2.0 … 0.5.1 ·
    no metadata findings · the gate itself returned no verdict, so what is still UNKNOWN
    is whether the installed artifact starts and answers
```

### Added — the nightly shipped gate runs a fast metadata pre-run first

Uses the `--metadata-only` depth the merge below created, for the shipped gate's
most likely failure mode rather than for speed.

The full gate builds a venv and does a cold `pip install` from the index: it is
the gate most likely to sit waiting on a socket, and `GATE_TIMEOUT_SHIPPED` is
900s. When it exhausts that budget the probe is **killed before it writes
anything** — leaving `rc=124` and no report, on the one gate that knows whether
users are installing a withdrawn release. "This gate hung" was the entire
output.

The pre-run answers the metadata half in two HTTP requests
(`GATE_TIMEOUT_SHIPPED_META`, default 120s) and writes it to
`shipped-metadata.json`, so the release/yank verdict survives a hang of the
second pass. When the full gate does hang, the log now says so and points at
that file rather than leaving it to be discovered.

Two things it deliberately does **not** do:

- **It does not decide the verdict.** `rc_shipped` still comes from the full
  gate alone. Letting a green metadata pass lower a 124 would turn "this gate
  hung" into "this gate is fine" — the exact substitution the classifier's
  124/137 handling exists to prevent — and "the metadata is consistent" was
  never the shipped-artifact gate's question. A test pins that no `rc_shipped=`
  assignment reads the pre-run's result.

- **It does not skip the full run when the package is absent from the index.**
  That branch looks like an obvious saving and is worth nothing: `shipped_probe`
  already returns before the venv when the index says `not-published`, so the
  skip would save no time and add a way to be wrong.

A structural test pins that the pre-run *precedes* the gate it insures against —
a pre-run placed after it buys nothing — and the existing "every shipped_probe
invocation is bounded" test covers the new call.

### Changed — `release_gap.py` merged into `shipped_probe.py` as its cheap depth

Requested after the two probes had been made to agree on how they read an index.
Both previously read PyPI, compared versions against tags, and disagreed about
what to call the result; `shipped_probe.py` imported eight helpers from
`release_gap.py` to do it.

`release_gap.py` is **deleted**. Its question is now a depth of the surviving
probe rather than a second tool:

```
shipped_probe.py --target . --metadata-only   # index + git. Two requests, no venv.
shipped_probe.py --dist X --target .          # the above, then install and run.
```

`--dist` now defaults to the `[project] name` in the target's `pyproject.toml`,
which is what kept the cheap depth a one-flag invocation instead of making every
caller repeat what the file already says.

**The cost objection that blocked this twice is answered by the depth flag, not
waved away.** Three of the absorbed findings — `UNRELEASED`, `UNTAGGED_VERSION`,
`CHANGELOG_UNRELEASED` — read git history and need no artifact at all. Making
them cost a venv and a `pip install` would have been a real regression, and
`--metadata-only` is what stops that.

Four things had to be decided rather than mechanically moved:

- **Two vocabularies overlapped.** `PUBLISH_GAP` (a tag the index does not have,
  from metadata) and `TAG_NOT_ON_INDEX` (a tag the *installed* version is behind,
  after paying for a venv) are one statement reached twice. Reporting both is not
  extra information — it is double-counting with different provenance. The
  metadata code wins, because it is the one that also fires under
  `--metadata-only`, so what a maintainer sees does not depend on which depth ran.

- **Two exit-code conventions collided**, and this is user-visible. `release_gap`
  used `1` for findings and `2` for "not a Python repo"; this probe uses `0`/`2`
  FINDINGS/`127` cannot-run, and that is the vocabulary the nightly gate reads.
  A caller testing `$? -eq 1` now sees `2`. A target with no `pyproject.toml` now
  gives `127` rather than `2` — also more correct, since `2` now means *the
  target has a defect*, which such a directory has not been shown to have.

- **Phase 1's findings are carried into phase 2, never replaced.** The first
  wiring recomputed the finding list after the install and silently dropped the
  yank and unreleased-commit findings, so the expensive run reported *less* than
  the cheap one.

- **One network door, not two.** The merged probe initially read the index twice
  — phase 1 through `fetch_simple`/`fetch_json`, phase 2 through the old
  `index_lookup` seam — which meant two chances to disagree, and made the
  existing tests reach the real network in phase 1 while believing they had
  stubbed it. `lookup_index()` is gone: `reconcile()` already encoded its
  PyPI-only fallback and its 404 corroboration. Everything now goes through
  `_get`, which is also the single point the tests stub.

A side effect worth naming: the release-gap cross-check now guards the
shipped-artifact gate too. Both index APIs are read on PyPI and a disagreement
between them is `UNCONFIRMED` rather than a finding, so the nightly gate no
longer fires during the minutes after a publish. That costs one extra HTTP
request per run.

The skill moves `skills/release-gap/` → `skills/shipped-probe/` and documents
both depths and the exit-code change. `tests/test_release_gap.py` becomes
`tests/test_release_metadata.py`, still 55 tests, still pinning the two measured
API divergences. Verified end to end at all three depths against the live index,
including a full run that installed `zurich-opendata-mcp` 0.7.0, listed 26 tools
and made a real tool call.

### Added — `release_gap.py` takes `--index-url`, and refuses the cross-check that would lie

`shipped_probe.py` got this in the change below; `release_gap.py` was still
hardcoded to pypi.org, so a target publishing to a private index could not be
audited by the cheap check at all.

The flag itself is small. The part worth reading is what happens to the JSON
API cross-check, because the obvious implementation is a bug: **against a
non-PyPI index the cross-check does not run**, and the report says so.
Querying pypi.org about a distribution that lives on a private index is not a
weaker second opinion — it is a *different package* that happens to share a
name. Agreement would be a coincidence and disagreement would be noise, and the
noise is the dangerous half: it would raise `UNCONFIRMED`, or suppress a real
`PUBLISH_GAP`, on the strength of an unrelated project's release history. This
is the same mistake the change below fixed in `shipped_probe.py`, and it would
have been reintroduced here by doing the easy thing.

So the JSON view gets a third status, `not_applicable`, kept distinct from
`unreachable` on purpose: one is a source that failed, the other a source that
does not exist. A degraded run and a correctly narrower one must not read the
same. It is stated in the report rather than silently skipped, because every
`UNCONFIRMED` outcome in this script depends on having two opinions — on a
private index there is exactly one, and the reader is entitled to know the
mid-propagation check is not armed.

Verified end to end against a real PEP 503 HTML index served locally: the yank
is read from `data-yanked`, `RELEASE_YANKED` fires, and the detail names `0.5.0`
as where installs land. That run also surfaced two messages still hardcoding
"PyPI" while pointed at another host; both now name the index that was actually
asked.

`is_pypi()` moved to `release_gap.py` and `shipped_probe.py` uses it from there
— two copies of one host check are two chances to answer it differently. The
`pypi_*` field names in `--format json` are kept as historical spellings for
"the index that was asked"; renaming them would break every consumer to rephrase
a field that now sits next to the new `index_url` key.

### Fixed — the existence check ignored `--index-url` and asked pypi.org regardless

The item left open by the previous change, and it was worse than "inconsistent".
`shipped_probe.py` installs from `--index-url`; the check deciding whether the
distribution exists at all was hardcoded to pypi.org. For a target publishing to
a private index those are **different hosts**, so the check could answer
confidently about a package it was never looking at — and the two failure modes
are both bad: a 404 becomes `NOT_ON_INDEX` against a package that is published
and installable, or an unrelated public package of the same name is found and
the check waves through a distribution nobody involved has ever seen.

`lookup_index()` now asks the index the install will resolve against.

**This required reading PEP 503 HTML, which is why it was flagged as a bigger
bet.** PEP 691's JSON flavour is *optional*; the only response format a Simple
index is required to serve is HTML. PyPI content-negotiates to JSON, but a
devpi, an Artifactory or a plain directory listing answers HTML, so a
JSON-only reader would have refused to audit exactly the private indexes this
change exists for. `release_gap._get` now parses both flavours into one shape,
so nothing downstream knows which one it got. Two details in the parser:

- **`data-yanked` is a yank by its PRESENCE**, per PEP 592 — its value is an
  optional reason, so `data-yanked=""` is still yanked. Reading it as a truthy
  value would have called every reasonless yank healthy, which is the same
  mistake as trusting the JSON API's lagging flag.
- **No `versions` key is emitted.** PEP 700 added that to the JSON flavour only;
  HTML has no equivalent, and an empty list would read as "this project has no
  releases". The version list is derived from the filenames instead, a path that
  already existed.

Verified against the live index by fetching PyPI's *own* project page in both
flavours and comparing: identical version list, identical yank set, identical
latest-installable. That equivalence is pinned as an opt-in live test
(`RELEASE_GAP_LIVE=1`) — the fixtures prove the parser handles *a* page, only
the live index proves it handles PyPI's actual markup.

The JSON API now falls back **only for PyPI**, matched on hostname rather than
by URL prefix, because only PyPI has a JSON API. On any other index a failed
Simple read is reported as unreachable (127) instead of being papered over with
an answer about a different host. Distribution names are normalised per PEP 503
before the request: an index need only serve the normalised spelling, so
`Foo.Bar_Baz` would 404 on a strict one — and this probe would have called that
"never published".

### Fixed — the shipped-artifact gate checked one cache of the index and installed from another

Follow-up to the release-gap change below, and the narrower half of the same
mistake. `shipped_probe.py` installs from the **Simple API** (`--index-url`,
default `https://pypi.org/simple`) but answered "does this distribution exist at
all?" from the **JSON API**. Two caches of one index, for the question whose
wrong answer is the most accusatory thing this probe says: `NOT_ON_INDEX` tells
a maintainer there is no publish process to repair, only one to create.

The exposure was always narrower than the release gap's — a JSON 404 only lags
for a package's *first ever* release — but that is precisely the moment
`NOT_ON_INDEX` fires, and precisely the maintainer who has in fact just built
the release process being told they never did.

`lookup_index()` now reads the Simple API first, and two details in it are not
decoration:

- **The JSON API stays as a fallback**, because a Simple response that cannot be
  read as PEP 691 JSON — an index or a caching proxy serving only the HTML
  flavour — is *not* an index that is down: `pip` installs from it perfectly
  well. A bare swap would have turned those setups into exit 127, trading one
  wrong answer for another. If both are unreadable the probe still exits 127, as
  before: a comparison that did not happen is never a pass.

- **A 404 is corroborated, not believed.** If the Simple API 404s and the JSON
  API has heard of the package, the probe returns "published" and lets the
  INSTALL settle it. Unlike `release_gap.py` this probe has a tiebreaker, so a
  disagreement does not have to be reported unresolved — it can be resolved.

The version it reports is now also the one `pip` would actually resolve to: a
yanked newest release is skipped, which the JSON API's `info.version` gave no
way to do.

Not changed, and still open: `--index-url` continues to be honoured only by the
install, while this check is hardcoded to pypi.org. For a private index the two
still consult different hosts. Fixing that means depending on PEP 691 support in
whatever index a target points at, which is a bigger bet than this change makes.

### Fixed — the release gap read the wrong PyPI API, and could not see a yank at all

Measured externally against `zurich-opendata-mcp` on 2026-07-31, and the two
halves of this finding did not survive re-measurement equally. Both are reported
here, because the difference is the point.

**What did not reproduce.** The report was that PyPI's two index APIs diverge:
after six releases were yanked the JSON API still answered `yanked: false` for
all six while the Simple API had them all as yanked, and ~90 s after `0.7.0` was
published the JSON API still said `latest = 0.6.0` while the Simple API already
served `0.7.0`. Re-measured on 2026-08-01, both had converged:

```
JSON   latest=0.7.0  yanked={0.2.0…0.5.1: true, 0.6.0: false, 0.7.0: false}
SIMPLE latest=0.7.0  yanked={0.2.0…0.5.1: true, 0.6.0: false, 0.7.0: false}
```

So the divergence is a **propagation window, not a standing property** of either
API, and no claim is made here that the JSON API is wrong in general. That is
not a reason to dismiss it — a window that only opens in the minutes right after
a release or a yank opens in exactly the minutes somebody is most likely to be
running this probe, and it is closed by the time you go looking. It could not be
captured on demand; `tests/fixtures/pypi/README.md` says which fixtures are the
index's own bytes and which are reconstructed from them.

**What did reproduce, and does not depend on any of the above.** `release_gap.py`
had no `yanked` field of any kind. "Published and healthy" and "published and
withdrawn" were the same report — the version exists, the tag matches, CI is
green, and `release OK` came out. That is a permanent hole in a script whose one
job is asking whether the fix on `main` is the fix users install, and a yanked
release is precisely a release users are no longer installing.

Three changes:

- **The Simple API is the primary source; the JSON API is a fallback.** The
  Simple API (PEP 503/691/700, with `Accept: application/vnd.pypi.simple.v1+json`
  and a cache-buster) is what `pip` and `uv` read, so it is what decides what a
  user gets. Asking the convenient API about an install is asking the wrong
  party. Two details that only show up once you read it: its `versions` list
  includes pre-releases where `info.version` does not — `pydantic` served
  `2.14.0a1` there against `2.13.4` on the JSON API — and PEP 700 does not
  promise the list is ordered, so `versions[-1]` would have reported an alpha as
  the current release. Ranking is done here, pre-releases excluded.

- **`yanked` is a field in its own right**, in the text report and in
  `--format json`, alongside which API it came from. A withdrawn release that is
  *not* the current one is a `NOTE` — history, not a defect. The current one is
  `RELEASE_YANKED`, high, and the detail names the version installs actually
  resolve to instead. A version counts as yanked only when every one of its
  files is: PEP 592 yanks files, and a version with one live wheel left is still
  installable.

- **Divergence is `UNCONFIRMED`, never a guess.** Where both APIs are readable
  and disagree, nothing is claimed: loud in the report, and it does not turn the
  run red. Same shape as the boot gate's `not-selected`. An auditor that fires
  `PUBLISH_GAP` because one PyPI cache is 90 s behind another gets muted, and a
  muted auditor catches nothing. The suppression is deliberately narrow — a tag
  ahead of *both* readings is a publish gap whichever cache you believe, and is
  still reported as one.

Regression tests cover both measured cases against recorded payloads, with no
live calls in the default run; `RELEASE_GAP_LIVE=1` re-runs the measurement
against the real index.

**Not done, and deliberately.** `shipped_probe.py` installs from the Simple API
already and would catch a yanked current release indirectly — pip would resolve
to the older version and the installed-vs-repo comparison would fire
`STALE_ON_INDEX`. It would attribute it wrongly, though: that finding says "the
release was never cut", which sends the maintainer to a publish workflow that
ran fine. The two probes also cost differently — one HTTP request against a venv
plus a subprocess — so folding `release_gap.py` into it would make the cheap
check as expensive as the expensive one. Merging them is a real option and it is
not being taken unasked.

### Fixed — the boot gate reported a healthy target as dead

Found by running the rollout runbook's step 4 against the real
`zurich-opendata-mcp`, which is exactly what that step is for.

The gate asks for a transport through env vars (`MCP_TRANSPORT`,
`FASTMCP_TRANSPORT`, `PORT`, …). That target selects HTTP with a **`--http`
flag** and reads none of them, so the entrypoint ran its stdio default, found
stdin closed (the probe passes `stdin=DEVNULL` for network transports) and exited
**rc 0**. The gate called that *"the server never came up"* — a finding against a
target whose HTTP transport is fine. Measured:

```
env-var way          -> rc=0     (exits at once, never listens)
--http --port N way  -> rc=124   (still serving when the timeout kills it)
```

Two changes, and the second is the one that matters:

- **The gate now tries the common CLI spellings** (`--http --port N`,
  `--transport http --port N`, `--sse …`) after the env vars come to nothing.
  This is not the guessing the module docstring refuses: deriving *which*
  transports a target serves must never be guessed, because a wrong guess there
  invents a requirement — but guessing how to *invoke* one is self-verifying, an
  attempt only counting once the port opens and the server answers real MCP.
  Only the **first** attempt — the target's own argv plus the env — decides the
  verdict; the flag attempts are chances to succeed and never chances to fail,
  since an argparse error from a guessed flag says something about the guess.
  A **declared** argv is never extended: the target said how it wants to start.

- **`not-selected` is its own outcome, exit 3.** When nothing listens, the exit
  code of the target's own invocation decides which of two statements the
  evidence supports: `rc != 0` means it tried and died (case 1, a real finding);
  `rc == 0` means it ran something else and finished, which is *"we never got to
  ask"*. Same shape as the rebinding gate's "control not configured" — not a
  pass, not a finding, loud in the report, and it does not turn the run red. A
  real failure outranks it. The fix belongs in the target, one
  `[tool.mcp_auditor.boot.commands]` entry, and the report says so.

**What the gate found once it could actually start the server** is the finding it
was built for: HTTP 421 for a non-loopback `Host` while loopback passes.
`main()` calls `mcp.run(transport="streamable-http", port=args.port)` with **no
`host`**, and there is no `--host` option, so the SDK derives its inbound
allow-list from the `127.0.0.1` default. The practical symptom is that the server
cannot be served beyond loopback at all; the 421 is that root cause's visible
signature. Case 2, on a real target, previously hidden behind a false negative
that looked like a false positive.

### Fixed — the auditor could not drive a target on the 2.x SDK at all

Two different projects are called FastMCP, and conflating them is expensive:

|  | package | server class | in-memory client |
|---|---|---|---|
| **(a)** the official SDK | `mcp` (2.0.0) | `mcp.server.mcpserver.MCPServer` — **renamed** from `mcp.server.fastmcp.FastMCP` in the 2.0 break, old module removed with no shim | `mcp.client.client.Client` |
| **(b)** a separate project | `fastmcp` (3.4.5) | `fastmcp.FastMCP` — **not** renamed, still current, own major line | `fastmcp.Client` |

**This was a real break, not labelling.** `fastmcp` (via `fastmcp-slim`) still
requires `mcp` 1.x, so the two cannot share an environment — measured:

```
fastmcp alone                 -> resolves mcp 1.29.0, imports fine
fastmcp + "mcp>=2.0.0,<3"     -> ImportError: cannot import name 'McpError'
                                 from 'mcp.shared.exceptions'
```

Three auditor scripts run **inside the target's environment** and hard-imported
`fastmcp.Client`: `schemas/generate_schemas.py` (the schema-drift gate),
`promptfoo/providers/call_tool.py` (the promptfoo provider) and
`scripts/recall_canary.py`. Against `zurich-opendata-mcp` — which pins
`mcp[cli]>=2.0.0,<3` and whose `app.py` is `from mcp.server.mcpserver import
MCPServer` — none of the three could even import their client. Adding `fastmcp`
to the target's dependencies could not have fixed it; the two exclude each other.

- All three now go through an **`in_memory_client(server)`** dispatch that asks
  the server object which project it belongs to (`type(server).__module__`) and
  builds the matching client, falling back to the other. Dispatch is on the
  object, not on what happens to be importable — that is the one signal that
  cannot be wrong, and it correctly sends an *old-named* `mcp.server.fastmcp.FastMCP`
  down the SDK branch rather than the standalone one. Two result shapes are read
  defensively because they genuinely differ: `list_tools()` returns a
  `ListToolsResult` in (a) and a plain list in (b), and the schema is
  `output_schema` in (a) but `outputSchema` in (b).
- Verified end to end in both directions: the schema gate now derives an output
  schema from a real `mcp` 2.x `MCPServer`, and `test_smoke_target.py` still
  drives the `fastmcp` path unchanged.

### Changed — labelling, so the confusion does not recur

`MCP_SERVER_IMPORT` names a **module and an attribute, never a class**, so the
rename never touched the convention — only the prose beside it claimed a class.
Corrected in `.github/workflows/ci.yml.template` (including two step names that
advertised installing a "FastMCP in-memory client" the target never had),
`.github/workflows/live-probe.yml.template`, `scripts/nightly-audit.sh`,
`schemas/README.md` and `skills/fastmcp-testing/SKILL.md`, which now opens with
the (a)/(b) table and the warning that a blind search-and-replace across the two
does damage.

`tests/fixtures/smoke_server.py` **stays on (b)** and says so at length: it is a
valid exercise of that branch of the dispatch, and rewriting it to `MCPServer`
would have been exactly the damaging replacement. Its docstring also states what
it does *not* prove — a green run there is not evidence that the (a) path works.

`tests/test_sdk_dispatch.py` pins the choosing (the part that silently
regresses) with fake objects and no SDK installed, and asserts that each of the
three call sites imports a client exactly once, inside its own branch helper —
so a hard pin sneaking back in turns CI red.

### Added — the shipped-artifact gate: green CI is not shipped software

`main` stood at 0.6.0. The GitHub release was never cut, so the publish workflow
never fired, and PyPI served 0.5.0 for an entire release cycle — with three tools
that were demonstrably broken in it. Every nightly run was green, because every
gate read the source and none of them ever read what users install.

- **`scripts/shipped_probe.py`** installs the target's distribution from the
  index into a **fresh venv** — never the checkout — and makes that artifact
  prove itself: the installed version is held against the repository's version
  *and* the last git tag, then the installed console script is started and asked
  for a real `initialize`, `tools/list` and `tools/call`. `--no-cache-dir` is not
  optional: a warm wheel cache measures what pip kept on disk last time, which is
  precisely the stale artifact being hunted, so the check would confirm the bug
  as healthy. `--index-url` is pinned for the same reason — a `pip.conf` mirror
  must not get to answer for PyPI.

- **Absent and stale are different findings.** `NOT_ON_INDEX` means the release
  process has never run for this package — there is no process to repair, there
  is one to create. `STALE_ON_INDEX` means it exists and did not fire this time —
  look at the workflow *run*, which usually failed on an approval or an OIDC
  trust nobody was watching. `INDEX_AHEAD` is the rarer inverse: something was
  published from a tree this checkout does not have. Reporting all three as "PyPI
  is out of date" sends the maintainer to the wrong place.

- **The stdin trap from the boot gate, where it bites hardest.** A tool call is
  the most network-bound thing a server does, so closing stdin after the write
  fabricates a failure more readily here than anywhere else. stdin is held open
  until the last answer is read, and `_close_stdin_early` exists purely so a test
  can demonstrate that the *same* healthy server is measured as broken the moment
  it is closed.

- **An empty answer is a finding; a blocked socket is not.** `TOOL_EMPTY` — the
  tool answered, and answered with nothing — is the incident's own shape and stays
  a finding. But this gate runs behind a default-deny egress allowlist, where a
  tool whose upstream is not listed fails in the same place a broken one does, so
  failures whose text reads like the sandbox (`connection refused`, `getaddrinfo`,
  `tunnel connection failed: 403`, `timed out`, …) are recorded as
  **unattributable** and raise nothing. A gate that fires on every target whose
  upstream nobody has allowlisted yet gets muted, and a muted gate catches
  nothing — the same reasoning that keeps recall floors at half the observed
  count. An empty content list looks nothing like a blocked socket and is
  deliberately not in that list.

- **An unreachable index is 127, never "in sync".** A comparison that did not
  happen is not a pass. The gate joins the evidence `gates` object and the
  classifier as `shipped_artifact`; missing from evidence still defaults to 127
  and hard-fails, so **roll the Worker image and the Broker together**.
  `SHIPPED_GATE=off` opts out, `SHIPPED_DIST` overrides the distribution name
  (derived from the target's own manifest rather than guessed from the repo slug,
  which is wrong often enough — underscores, prefixes — to be worth not doing).

- **Egress: nothing had to be added, and that is worth saying precisely.**
  `pypi.org` and `files.pythonhosted.org` were already in `worker-allow.txt` (for
  `uv sync`), and `egress-allowlist.nft` is a port/LAN/resolver layer that cannot
  express per-domain rules at all — it already allows 443 and already names PyPI
  in its comment. What changed is that those two entries are now **load-bearing
  for a gate** rather than incidental, so they are annotated as such and
  `tests/test_shipped_probe.py` asserts their presence: a cleanup pass that prunes
  them as unused now turns CI red instead of silently disabling the gate. The
  Broker's allowlist is deliberately **not** extended — it holds the credentials
  and never installs anything, so mirroring the entries there would widen the one
  side that matters to buy nothing. A test pins that too.

- **Three bugs the tests found while being written**, all in the reused
  `release_gap` helpers: `read_project` returns the `[project]` table rather than
  the whole document, it raises when there is no `pyproject.toml`, and
  `release_tags` sorts **newest first** — so taking `[-1]` compared the index
  against the *oldest* release the repository ever cut, which is always behind and
  would therefore have made this gate right by accident on every target.

### Added — the portfolio fan-out: one predicate wide, as a matrix

`TARGET_REPO` is one repository per run. That is the right shape for the nightly
deep pass and the wrong shape for the question this auditor is actually good at:
*does this failure class exist anywhere in the portfolio?*

The occasion was an SDK major migration across a dozen-odd servers. One server
was not the root package of its repository, so it fell out of every enumeration
written by hand, and was still on the old API long after everything else had
moved. No per-target report would have shown that — the finding is not inside any
single report. It is in the **row that breaks the pattern**, which only exists
once the results are a matrix.

- **`scripts/portfolio_scan.py`** — N targets × M predicates → a grid, plus an
  explicit **outlier pass**. The outlier pass needs no configured expectation:
  during a migration nobody knows which version is "right" until they see that
  fourteen repos agree and one does not, so a majority is enough. Predicates are
  deliberately small — greppable, or checkable in seconds against a shallow
  checkout: `manifest`, `sdk_major` (which SDK major each target pins),
  `settings_write` (the `mcp.settings.host = …` crash-at-start from
  `parlament-mcp#29`, findable across fifteen repos in a second),
  `host_allowlist_knob` (reuses the rebinding gate's detector) and
  `nested_manifests`. `boot` — actually starting the server, via the transport
  boot gate — is the one expensive predicate and is **opt-in per target**.

- **`nested_manifests` is the one the occasion is about.** It reports every
  manifest below the root that no target entry claims, and it is **fail-closed**:
  every undeclared manifest is flagged whether or not it looks like a server. A
  heuristic that only flagged the server-shaped ones would let through exactly
  the one that does not match the heuristic — the same bet that lost the first
  time. Acknowledging a manifest in `known_manifests` costs one line and makes
  the omission deliberate.

- **Cells have five statuses, and the middle one is load-bearing.** `ok`, `flag`,
  `note` (observed and deliberately *not* a finding — a host allow-list that is
  simply absent is the documented fail-open state, consistent with the rebinding
  gate), `na`, and `error`. **Partial results are the deliverable**: a target
  that cannot be cloned becomes a row of `error` cells with the reason attached
  and the sweep continues. What an incomplete sweep must never do is report "no
  findings", so `incomplete` (exit 1) outranks `findings` (exit 2) — "we did not
  look" and "we looked and found nothing" are different claims. The report still
  lists every flag it did find.

- **`targets.example.yaml`** is committed; the real `targets.yaml` is gitignored,
  because a portfolio list names every server you run and that is inventory
  rather than source. PyYAML is used when present, and a strict stdlib subset
  reader stands in when it is not — the Worker carries no dependencies. A test
  hides PyYAML and asserts the two agree on the committed example: a targets file
  that parses differently on the Worker drops a server from the sweep, which is
  this module's own failure mode turned on itself.

- **The budget guard now knows about width.** A fan-out multiplies whatever one
  target costs, and every existing knob had only ever seen one target — a sweep
  could walk past all of them simply by being wide. `BUDGET_MAX_FANOUT` (25) and
  `BUDGET_MAX_FANOUT_EXPENSIVE` (10) bound it at **preflight, before the first
  clone**, because the breaker and the token window are both retrospective and a
  fan-out's whole risk is that it spends N times before anyone looks. Being
  honest about what actually multiplies: today's predicates are deterministic and
  call **no model**, so tokens are not the binding constraint — wall-clock, disk
  and sockets are. `BUDGET_TOKENS_PER_TARGET` defaults to 0 for exactly that
  reason and exists so the first predicate that *does* call a model is bounded on
  the day it is added rather than discovered afterwards in a bill. `record
  --fanout N` stamps the width into the run history, so a hard-fail from a
  15-target sweep does not read back as an ordinary single-target run.

- **Egress, spelled out rather than assumed.** N target repositories need **no**
  new proxy entries — they all sit behind `github.com`, which is already allowed.
  What is *not* covered is each target's own **upstream data origin**:
  `worker-allow.txt` names only Zürich's, because that is the one target the
  fixtures were recorded against. The cheap predicates reach no upstream at all,
  but `boot` starts the real server, and a startup that touches an unlisted
  origin fails against the default-deny proxy in a way that reads exactly like a
  target defect and is not one. Documented in
  `deploy/microvm/forward-proxy/README.md` with a worked example per server, and
  `portfolio_scan.py --print-egress` prints what a targets file implies while
  saying out loud that the upstreams are not among them. The nft ruleset needs no
  change: it filters ports, LAN and resolvers, none of which vary with N.

### Added — two ways a gate can say nothing while looking like it said yes

Both of these are failure modes of the *harness*, not of any one gate, so they
apply to every gate at once — the six that exist today and whatever is added
next.

**A gate that HUNG.** Twice in recent security work a mutation test surfaced as a
**hanging** suite rather than a red one — in one case because, with the control
under test removed, an SSE `GET` under a foreign `Host` is admitted and opens an
endless event stream that the test client then waits on at teardown. A timeout
that reads as generic infrastructure noise swallows exactly that finding.

- Every gate in `scripts/nightly-audit.sh` now runs through `run_bounded`, a GNU
  `timeout` wrapper, so a gate that never returns is recorded as **exit 124**
  (or **137** when it ignored `SIGTERM` and had to be `SIGKILL`ed after
  `--kill-after`). Provisioning (`git clone`/`fetch`, `uv sync`) is bounded too:
  a fetch stalling against a filtered egress path hangs exactly like a wedged
  gate. Budgets are per gate and tunable — `GATE_TIMEOUT` plus
  `GATE_TIMEOUT_PYTEST`, `_PROMPTFOO`, `_BOOT`, `_REBIND` and friends. `timeout`
  being absent is a hard failure rather than a quiet fall-through to unbounded
  execution: running unbounded is the state this removes.
- `nightly_audit_report.py` gains **`hung`** as its own class, carrying the
  **name** of every gate that hung. That name is the actionable content —
  "pytest hung" and "promptfoo hung" call for entirely different next steps. A
  hung gate is deliberately excluded from the finding classes: a timeout is not
  "ruff found problems", and folding it into `toolchain_fail` would put a defect
  claim in the report that no gate ever made. The boot and rebinding probes'
  existing "wrote no report + non-zero → 127" remap now exempts 124/137, since a
  killed probe writes no report either and relabelling "it HUNG" as "it never
  ran" loses the one detail that says where to look.

**A gate that ran nothing.** `unittest discover` finding no tests prints
`Ran 0 tests`, says `OK`, and **exits 0** — green, empty, and indistinguishable
from success to anything that only reads exit codes.

- The Worker measures how many tests the suite actually reported on, straight
  from the runner's own output (`nightly_audit_report.py --count-tests`), and
  ships the number in the evidence — the Broker never sees the log, so the count
  has to travel. A count that cannot be read stays **-1 (unknown)**, never 0:
  reporting "no tests" on the strength of an unreadable log would invent the very
  finding this hunts.
- **`no_tests_executed`** is a class of its own, and so is the weaker
  **`tests_unverified`** (green, but the suite size could not be established) —
  on the same rule that already makes promptfoo returning rc 0 with no parseable
  output a hard failure. The gate line withdraws its tick rather than printing
  `✅ pass — 0 test(s)`, which is the exact sentence the class exists to prevent.

Both are **hard failures (rc 1), not findings (rc 2)**. In both cases a gate
produced no verdict, and `findings` would route the run to a tracking issue
asserting a defect class nothing observed — `sync_findings_issues.py` only fires
on the `findings` outcome, so rc 2 would literally open an issue claiming a
result that does not exist. The report also stops saying "re-run": for a hang,
an attempt that passes the second time has not been explained.

Rollout: `tests_collected` is now read from the evidence, and evidence without it
classifies as unverified rather than green. Same rule as the gate names — **roll
the Worker image and the Broker together.**

`tests/test_gate_timeouts.py` lifts the real `run_bounded` out of the committed
script and drives it in bash (124, 137, and that the whole process group dies —
the gate is `uv run pytest`, so the process that hangs is a grandchild), then
checks structurally that *every* gate invocation is wrapped. That second half is
what the file mostly exists for: the natural way to lose a time bound is not to
break the helper, it is to add a seventh gate next year and call it directly.

### Added — the DNS-rebinding gate: the control CORS and a token cannot provide

A page in the operator's network resolves its own hostname to the MCP server's
address and then talks to it straight out of the browser. Two things that look
like they should stop that do not: **CORS** cannot, because after the rebind the
browser considers the request same-origin and there is no preflight to fail; and
an **auth token** cannot, because the attacking page runs in a context that
already holds one. The only control that answers the question is the transport's
own check of the `Host` (and `Origin`) header — the inbound counterpart to the
egress allow-list these servers already have. It was retrofitted in
`malkreide/bag-health-mcp#51` and `malkreide/swiss-transport-mcp#25`; this gate is
what keeps it honest.

- **`scripts/rebind_probe.py`** — boots the target through the boot gate's own
  launch plan (`transport_boot_probe`: declared argv > entrypoint > imported
  server object), configures an inbound allow-list, and then tries to walk past
  it. Four probes, all to `127.0.0.1` on the bound port because there is no DNS
  for an invented name — only the headers differ:

  | probe | request | expected |
  |---|---|---|
  | 1 | foreign `Host` | rejected |
  | 2 | allowed hostname, **wrong port** | rejected |
  | 3 | allowed `Host`, foreign `Origin` | rejected |
  | 4 | allowed `Host` and port | **accepted** |

  **Probe 2 is load-bearing, and probe 4 is why.** A probe against
  `evil.example.com` alone proves nothing: a server that falls back to a loopback
  default policy rejects it just as convincingly, and so does one broken in some
  unrelated way. What no fallback can imitate is the *pair* — two requests
  differing in exactly one thing, the port, one rejected and one accepted. A
  loopback fallback rejects both; a list compared on hostname only accepts both.
  Without probe 4, probes 1–3 measure "something said no", not "the configured
  allow-list said no". A test pins this from both sides: against the
  `loopback_only` fixture the foreign-host probe passes and the gate still refuses
  to call the control verified, and against `hostname_only` probes 1, 3 and 4
  answer *identically* to the healthy server so the wrong-port probe is the only
  thing left carrying the check.

  **The whole matrix runs twice — once anonymous, once with a VALID token.** A
  server that lets a foreign `Host` through the moment an `Authorization` header
  appears has not implemented this control; it has implemented authentication,
  which the attack defeats by construction. That case is named in the report
  rather than folded into a generic failure. The token pass carries its own
  control too: one request with a deliberately *wrong* token. If that is served,
  the target never enforced auth here, and the pass is recorded as weaker evidence
  instead of being claimed as proof of independence.

- **Three outcomes, not two.** A target with no allow-list configured rejects
  nothing on a non-loopback bind. That is the *documented* fail-open state — both
  servers above ship the check off by default, because guessing a list on
  `0.0.0.0` would reject the very deployment it is meant to protect. So it is
  reported as its own visible category, **`control not configured`** (exit 3):
  never as a pass, because the control is absent and the attack is unopposed;
  never as a finding, because nothing is broken.

  Two things separate it from a real defect, in that order. First what the probes
  themselves show: a target that refuses *some* hostile probes and serves others
  took the allow-list and applied it wrongly — nothing that is merely switched off
  can produce that mix — so it is a finding regardless of what its docs say, as is
  a check that holds until a valid token appears. Only when all three hostile
  probes are served alike is there nothing observable to go on, and only then does
  the target's own tree decide: a source file, README, `.env.example` or compose
  file naming an allow-list variable means the knob is there and not honouring it
  is a defect (exit 2). A `FastMCPRebindTest` boots a real FastMCP server to prove
  a vanilla one lands in `not configured` rather than producing a false alarm.

- **Classifier + report.** `host_allowlist` joins the evidence `gates` object and
  the Broker-side classifier with its own three-way rendering: exit 3 shows as
  `🟡 control not configured`, gets its own report block, and — since it does not
  turn the run red — rewrites the green headline so "All gates green" is never the
  last word on a server whose rebinding control is absent. As with the boot gate,
  a gate name missing from evidence still defaults to 127 and hard-fails, so a
  Worker image predating this gate cannot classify green. Roll the Worker image
  and the Broker together. `REBIND_GATE=off` opts out.

- **Two smaller fixes found on the way.** `sync_findings_issues.py` had no
  routing class for `transport_boot_fail`, so a run whose only red gate was the
  boot probe classified as `findings` and then opened no issue at all; both
  process gates now route (the rebinding one under its own `dns-rebinding`
  label). And `detect_knob` matches variable names on identifier boundaries —
  `ALLOWED_HOSTS` is a substring of `MCP_ALLOWED_HOSTS`, and the report's job is
  to tell the operator which variable to set, not to list every name that fits.

Note the two gates have opposite polarities and do not contradict each other. In
the boot gate a rejection is the *bug* (nobody configured a list, so refusing a
real hostname can only mean `host` never reached the app builder); here a
rejection is the *control working*, because this gate configures a list first.
They never disagree about the same server: the boot gate sets no allow-list
variable, so a target that honours one is untouched by it. A target with a
**hardcoded** non-loopback list is the one shape that trips the boot gate while
this gate calls it healthy — read both reports together, and see
`REBIND_ALLOWED_HOST` for pointing this gate at a name such a target really
allows.

### Added — the transport boot gate: the first gate that watches the process run

Every gate before this one reads the target. `ruff` reads it, `mypy` reads it,
`generate_schemas.py --check` imports it and compares shapes, promptfoo drives
its tools in-process. All of them can be green while the server does not start.
Two real cases, both of which walked through the full gate set untouched:

1. after the SDK major bump the settings object is read-only, so the surviving
   `mcp.settings.host = ...` raises `ValueError: "Settings" object has no field
   "host"` at start and the process never comes up
   (`malkreide/parlament-mcp#29`);
2. when `host` is not passed through to the app builder, the SDK derives its
   inbound host allow-list from the `127.0.0.1` default and answers HTTP 421 to
   every request made under a real hostname. The process runs, the local health
   check passes, and the deployment is unusable.

- **`scripts/transport_boot_probe.py`** — starts the target under each transport
  it configures and runs a real JSON-RPC `initialize` followed by `tools/list`.
  Transports are derived from the target's own config (source, `Dockerfile`,
  compose, `.env.example`, `[tool.mcp_auditor.boot]`), never guessed — with a
  floor of stdio + streamable-http, because probing only what a target
  *advertises* rebuilds the very blind spot the gate removes. SSE is probed only
  when the target still offers it.

  **How the target is started decides whether case 2 is visible at all.** Booting
  it ourselves via `mcp.run(host=...)` means passing `host` correctly on the
  target's behalf — straight past the bug, which lives in the target's own
  startup code. So the probe prefers the target's own entrypoint (`declared` argv
  > `[project.scripts]` / `python -m pkg` > the imported server object) and stamps
  the mode it used into the report. A green `generic` HTTP result is weaker
  evidence than a green `entrypoint` one, and says so in the report rather than
  reading like the same all-clear.

  **HTTP is probed under two `Host` headers, not one.** Bound to `0.0.0.0` like a
  real deployment, first with a loopback `Host` (which must work, or the
  transport is simply broken) and then with a non-loopback name. A 421 on the
  second when the first passed is case 2 and nothing else. A test pins the reason
  this matters: against the *same* broken server the loopback request returns a
  perfectly healthy 200, so a probe that only talked to `127.0.0.1` would call
  that deployment fine.

  **stdin stays open until the answers are in.** Closing it after writing the
  request shuts the server down before network-bound calls finish, and you record
  a failure that does not exist. The stdio fixture exits on stdin EOF and delays
  its `tools/list` answer specifically so that mistake cannot pass by luck, and a
  test asserts the healthy server *is* measured as broken when stdin is closed
  early.

  Every start attempt is under a hard deadline (`BOOT_TIMEOUT`, default 30s) with
  the whole process group killed after it, so a hung target cannot hang the night.

- **Exit-code contract, deliberately matching the auditor's own**: `0` green,
  `2` finding, `127` the harness could not run. **A target that will not start is
  a FINDING about the target — only the harness failing is a hard failure.** That
  line is the one the rest of the classifier rests on, so `nightly-audit.sh` also
  maps "the probe wrote no report and returned non-zero" onto 127: claiming a
  boot failure we never actually observed is the worse of the two errors.
  `BOOT_GATE=off` disables the gate, with the same fail-closed reasoning as
  `SCHEMA_GATE` — and the knowledge that you are switching off the only check
  that sees this class of bug.

- **`transport_boot` is now part of the evidence contract.** The Worker writes it
  into `nightly-evidence.json`'s `gates`, and the Broker re-derives the verdict
  from it (`nightly_audit_report.py --from-evidence`). Because a gate name absent
  from the evidence defaults to 127, **evidence from a Worker image still running
  the previous `nightly-audit.sh` now hard-fails instead of classifying green** —
  intended, and the reason the Worker image and the Broker roll out together.
  Nothing new leaves the machine: the probe only ever connects to loopback inside
  the guest, so no egress-allowlist or forward-proxy entry is required.

### Fixed — published_probe went red on a comment

`bakom-mcp` 2.0.4 sends the correct `bakom-mcp/2.0.4 (+github…)`, verified by
importing the installed package. The probe reported `DRIFT … sends 1.0` anyway,
because `__init__.py` records the old incident in a comment:

```python
# in server.py carried "bakom-mcp/1.0" to the BAKOM endpoints all the while.
```

The literal scan read the documentation as evidence. `identity_probe.py`
learned this lesson early — *a rule that turns CI red on good documentation
teaches people to delete the documentation* — and strips comments before
scanning. This probe was written without it and had to relearn it against a
real package on the day it shipped.

- Source is passed through `code_only()` before the f-string and literal scans.
  `tokenize`, not `split("#")`: a `#` inside a string literal must not truncate
  the line. Unparseable source is scanned whole rather than skipped.
- Four regression tests, including the `#`-in-a-string case and the guarantee
  that a genuine literal on a line that *also* has a comment still counts.

### Added — the published probe, and the false all-clear it was built to end

`identity_probe.py` reads a repository; `release_gap.py` compares version
*numbers*. Neither opens the artifact, and `swiss-efv-mcp` walks straight
through both: PyPI 0.3.0, `main` 0.3.0, `src/` clean, exit 0 from each — while
the package every user installs sends
`Mozilla/5.0 (X11; Linux x86_64) … Chrome/124.0`. It presents itself to
upstreams as a browser, and nothing in this repository could see it.

- **`scripts/published_probe.py`** — installs a distribution from the index
  into a throwaway venv and measures the User-Agent the shipped code actually
  puts on the wire. Across 33 published portfolio packages, 16 announced a
  version they were not, with the fix merged in every one and released in none.

  Three detection strategies run together because each is blind somewhere
  different, and each reported a clean result for packages that were drifting:
  a regex for `f"…{__version__}…"` misses `lobbywatch-mcp`, which spells the
  variable `PACKAGE_VERSION`; reading the module namespace misses
  `seco-labor-mcp`, whose User-Agent sits in
  `_HTTP_KWARGS["headers"]["User-Agent"]`, and `swiss-transport-mcp`, which
  passes the literal inline to the `httpx` constructor inside a function, where
  it exists in no module attribute at all; scanning source for literals misses
  every f-string, since there is no digit after the slash to anchor on. Every
  finding records which strategy produced it.

  **`unverified` is the load-bearing state.** "This server sends no custom
  User-Agent" and "I did not recognise the shape" look identical in a report
  and mean opposite things. Conflating them is how the first version of this
  check pronounced 24 packages unremarkable, 16 of which were drifting — and it
  read like good news. `no_user_agent` is now claimed only when the installed
  source never mentions one; otherwise `unverified`, exit 1.

  `FOREIGN-UA` is separated from drift: parsed naively, `Mozilla/5.0` yields
  version `5.0` and reports as drift against `0.3.0`, which is wrong twice —
  the package is not announcing a stale version of itself, and the browser
  impersonation goes unnamed. Product tokens are compared to the distribution
  name case- and separator-insensitively, since `swisstopo-mcp` legitimately
  sends `SwisstopoMCP/…`.

  `--constraint` pins a dependency so a package can be measured at all:
  published `swiss-statistics-mcp 0.6.0` imports `mcp.server.fastmcp`, which
  `mcp` 2.0.0 removed, so a fresh install dies at import. Needing a constraint
  is itself a finding about the package.

- **`skills/published-probe/SKILL.md`** — when to reach for it and how to read
  each line, with the reasons the shortcuts fail.

### Fixed — identity_probe reported a false all-clear

`find_hardcoded` matched the User-Agent's product token against the
distribution name *literally*. `swisstopo-mcp` sends `SwisstopoMCP/0.1`, so a
repository hardcoding a wrong version came back `identity OK … src/ clean` with
exit 0 — the probe producing exactly the failure it exists to catch.

- Tokens are compared case- and separator-insensitively.
- New `UNVERIFIED` category: when `src/` mentions a User-Agent and neither a
  hand-maintained nor a runtime-assembled value can be resolved, that is
  reported and exits non-zero instead of passing as clean.
- **`tests/test_identity_probe.py`** — the script had no tests. Eight cases,
  including both false all-clears above and the regressions that must not come
  back: comments are not findings, `0.0.0+source` is not a finding, and badge
  drift must not hide the source scan.
- **`tests/test_published_probe.py`** — 16 cases over the classification rules,
  no network.

### Added — the auditor runs its own tests

The test suite existed and nothing ran it. `ci.yml` ships as a `.yml.template` for
the target repo; `telegram-intake.yml` runs no tests. A regression in
`live_probe.py`, the budget guard or the Broker pipeline would have reached
`main` unchallenged. A tool that holds other repositories to test discipline and
keeps none itself is the more embarrassing of the two failures.

- **`.github/workflows/tests.yml`** — the stdlib `unittest` suite on Python 3.11
  and 3.13, on push to `main` and on every pull request. Least privilege
  (`contents: read`), concurrency-cancelled per ref.
- **No dependency-driven skips.** `pyyaml`, `fastmcp` and `httpx` are installed
  not to make the suite run — it is stdlib-only — but to stop three test classes
  from skipping. A test skipped for a missing dependency is a check that did not
  run while looking exactly like one that passed, which is the failure mode
  `live-probe.yml.template` already guards against for the recall canary. The
  job fails on any skip whose reason contains «not installed»; environment
  skips (no `bash`, no `git`) stay permitted since they cannot occur on the
  runner. Verified in both directions: without `fastmcp` the gate exits 1 and
  names the three tests, with it the suite is green and zero skipped.
- **`compileall` over `scripts/`, `tests/`, `schemas/`, `promptfoo/providers/`** —
  most scripts have no test that imports them, so a syntax error would have sat
  in `main` until the nightly cron tripped over it at 03:00.
- **The `.yml.template` files are parsed as YAML.** Nothing else touches them, so
  a broken template would fail first in whichever target repo copied it. Parsing
  is not validation, but it catches the class that hurts.

### Added — the release gap

The identity probe asks whether the version a server reports is *correct*. This
asks whether it is *current*, and it is a separate blind spot: CI tests the
branch, never the artifact, so a repository can be green, audited and entirely
fixed while every `pip install` still hands out the broken release.

`meteoswiss-mcp`, 2026-07-30, is the case. The migration to the `mcp` 2.x SDK
was merged to `main` on the 29th; PyPI kept serving `0.4.0`, which imports
`mcp.server.fastmcp` — a module `mcp` 2.0.0 had removed the day before. Every
fresh `uvx meteoswiss-mcp` died on import for three days, until an outside user
filed the bug. It then recurred the same afternoon: `0.5.0` shipped, three
further fixes landed, and until the next release PyPI served a server whose
`meteo_current`, `meteo_forecast` and `meteo_school_check` all returned
nothing.

- `scripts/release_gap.py` — deterministic, `--format text|json`, exit
  `0`/`1`/`2`. Compares the published PyPI version, the release tags and the
  unreleased commits against the repository. Reports `PUBLISH_GAP` (a tag the
  index does not have — someone cut a release and it never landed),
  `UNRELEASED`, `UNTAGGED_VERSION` and `CHANGELOG_UNRELEASED`, in that order of
  sharpness.
- `skills/release-gap/SKILL.md` — in the shape of `identity-probe`, phase-1
  report-only. Cutting a release stays a human decision; the probe stops at
  naming the gap.
- `tests/test_release_gap.py` — 17 stdlib tests. The git-backed cases build a
  real repository in a temp dir rather than mocking `git log`, which would only
  assert that the mock matches the assumption.

Run against a reconstruction of the incident state — `main` at the merge
commit, latest tag `v0.4.0`, PyPI at `0.4.0` — the probe reports
`UNRELEASED [high] … 2 of them user-facing` and exits `1`.

Three design decisions:

1. **Age is the finding, not the gap.** Every repository is ahead of PyPI for
   the minutes after a merge. Firing on that gets the check muted, and a muted
   check catches nothing — the same reasoning that keeps recall floors at half
   the observed count. `--max-age-days` defaults to 7.
2. **An unreachable index is reported, never assumed away.** If PyPI cannot be
   reached the comparison that matters did not happen, so the probe exits
   non-zero with `UNKNOWN` instead of printing "in sync" from git alone.
   Reporting green from half the evidence would reproduce, one level up, the
   exact failure the script exists to catch.
3. **A `--depth 1` clone has no tags, and that is not "never released".** An
   empty tag set is reported as unknown; concluding the latter inverts the
   finding.

`fix:` and `docs:` are weighed differently rather than counted together: in the
incident, every unreleased day was a user hitting a `ModuleNotFoundError`, and
a portfolio sweep should not drown that in documentation churn.

### Added — the identity probe

Ruff, mypy and pytest all pass on a server that introduces itself to every
upstream as a release it stopped being months ago. The live probe does not see
it either: the responses are fine, it is the *request* that lies. A portfolio
sweep on 2026-07-29 across all 30 servers found the drift is the rule, not the
exception — 12 sent a wrong version (4 a wrong **major**), 20 carried a stale
`__version__`, 17 a stale README badge, 4 a stale `server.json`. The last one
is structurally invisible: `publish.yml` rewrites that field from the tag at
release time, so the committed value never reaches the artifact and nothing
ever contradicts it.

- `scripts/identity_probe.py` — deterministic, `--format text|json`, exit
  `0`/`1`/`2`. Checks `server.json`, README badges and `src/` against
  `pyproject.toml`; `--installed` additionally resolves the version from the
  installed distribution. That flag is the one that counts: metadata is written
  at install time, so an editable install keeps reporting the pre-bump version
  and a repository can be clean while the shipped artifact is wrong.
- `skills/identity-probe/SKILL.md` — in the shape of `python-auditor`, with the
  reasons not to replace the probe with a grep.

Three design decisions, each of them a bug this check made first:

1. **Whole files are scanned for the value pattern, not lines for the
   keyword.** A constant split over two lines escapes a line-oriented search —
   that is how `swiss-electricity-mcp` kept shipping `0.2.0` through three call
   sites *after* a fix had been merged and reported as done.
2. **Comments are stripped with `tokenize`, not `split("#")`.** The first
   version flagged a comment documenting the very incident the check exists to
   prevent. A rule that punishes documentation gets the documentation deleted —
   and a `#` inside a string literal must not truncate the line.
3. **Every category is reported before exiting.** An earlier version aborted on
   the first finding, reported a stale badge and never reached the source scan.

Fallbacks are recognised by their PEP 440 local segment, never by matching a
fixed marker string — the marker-specific variant produced nine false
positives. A bare `"0.0.0"` is reported, correctly: it is indistinguishable
from a real release in a log or a User-Agent.

Verified against real repositories (`swiss-environment-mcp`, `bakom-mcp`,
`srgssr-mcp` — the latter two contain drift comments that must not fire) and
against a fixture reproducing every historical failure case.

### Added — recall floors and the whole-chain canary

The auditor watched upstream for *schema* drift and nothing for *recall*. Both
gaps followed from the same premise, stated in `live_probe.py`: «We compare
structural signatures, NOT values.» Correct for a timestamp, wrong for a hit
count — an endpoint that starts returning one record instead of twenty has an
identical signature, so the diff is empty and the probe stays green.

Prompted by [`termdat-mcp#11`](https://github.com/malkreide/termdat-mcp/issues/11):
a server that searched one of 23 classifications for months because an optional
scope parameter it never sent defaults to a single subject area upstream. The API
was healthy throughout, 33 offline tests were green, and a 68-check audit had
passed.

- **`min_count` floors in `scripts/live_probe.py`** — a probe may declare a
  minimum record count, optionally with `count_path` (dot path; inferred from the
  common CKAN / GeoJSON / `results` / `entries` shapes when omitted). Reported
  separately from schema drift, because the remedy differs: drift means update
  the fixture, a recall drop means find out what shrank. New `count_records()`
  helper; an uncountable payload is an explicit error, never a silent pass.
- **New outputs** `recall_drop` and `alert`. `drift` keeps its original
  schema-only meaning so existing workflows do not change behaviour silently —
  `alert` is the one to gate on now that a probe can fail two independent ways.
- **`scripts/recall_canary.py` + manifest** — calls the target server's **own
  tools** in-process via the FastMCP in-memory client with the network alive.
  `live_probe.py` hits raw upstream URLs and so verifies the endpoint; the canary
  verifies the whole chain (arguments → request construction → parsing). A recall
  bug typically sits between the two, where a raw-URL probe is blind to it by
  construction. Shares `count_records()` so a floor means the same in both jobs.
- **`live-probe.yml.template`** runs both probes and gates the tracking issue on
  `alert` from either. The canary's install step is `continue-on-error`, and a
  missing canary report is called out in the merged report — a silently skipped
  check reads exactly like a passing one, which is the failure mode this job
  exists to prevent.
- **Confabulation guards in promptfoo** — new `datastore_sql_empty` fixture
  (zero records, otherwise structurally identical to the populated one). The
  deterministic profile asserts that a zero-record response stays well-formed and
  does not assert absence on the caller's behalf; the graded profile adds an
  `llm-rubric` that passes a plain empty payload and fails only when the tool
  interprets the emptiness for the reader. In the reported session that
  interpretation — «an empty result usually means the term is out of scope» —
  was exactly what the model took as licence to invent an answer. Corresponds to
  `mcp-audit` check FID-003; the positive half (an empty result should carry a
  concrete next step) is graded rather than asserted, so adopting it stays a
  decision rather than a red pipeline.
- **`tests/test_recall_floors.py`** — 21 stdlib tests covering counting, floor
  reporting, output signalling and canary error isolation.

### Added — gateway-independent Telegram announce
- **`scripts/telegram_notify.py`** — a stdlib-only, one-way notifier that pushes
  an audit report to Telegram over the Bot API **without** an OpenClaw runtime,
  mirroring the self-contained notifier of the sibling `future-skills-evidence-graph`
  repo. OpenClaw stays the interactive control plane (commands, per-finding `OK`,
  cron `--announce`); this is the complement for hosts that produce a report but
  run no gateway (Tier-0 / a keyed operator run / a CI job / the trusted Broker).
  It is **no-op without `TELEGRAM_BOT_TOKEN` + a chat id** (`TELEGRAM_ANNOUNCE_TO`,
  else the first `TELEGRAM_ALLOW_FROM` id) and **best-effort** — every send error
  is a token-redacted warning and the exit code stays 0, so it can never turn a
  green/findings/hard-fail verdict into a crash. Deliberately **outbound only**:
  inbound commands remain OpenClaw's sandboxed job, so no second, less-guarded
  command path is added.
- **`scripts/nightly-audit.sh`** gained an **opt-in** final announce step
  (`TELEGRAM_NOTIFY=1`, default off) that runs *after* `outcome_rc` is captured
  and `|| true`, so the exit-code contract with the cron agent / Broker is
  untouched. No-op on the credential-free Worker, which never holds the token.
- New `tests/test_telegram_notify.py` (17 stdlib `unittest` tests) pinning the
  no-op-without-config, best-effort-exit-0, token-redaction, truncation and
  chat-id-resolution invariants. Docs: `docs/telegram/standalone-notify.md`;
  `.env.example` / README updated with `TELEGRAM_ANNOUNCE_TO` + `TELEGRAM_NOTIFY`.

### Added — gateway-independent Telegram intake (inbound)
- **`scripts/telegram_intake.py`** — the inbound complement to the announce: a
  stdlib-only intake that lets an allow-listed user drive a small set of **safe,
  deterministic** commands from Telegram **without** an OpenClaw runtime, running
  in GitHub Actions. Mirrors the intake of the sibling `future-skills-evidence-graph`
  repo in two modes — scheduled `getUpdates` poll (default) and an optional
  Cloudflare webhook relay (`relay/telegram-webhook-relay.js`) for real-time push.
  - Commands: `/audit [ref]` files an `audit-request` **issue** for `TARGET_REPO`
    (read-only request artifact — the audit and any PR are still produced with a
    human in the loop); `/status` replies with the latest committed audit record
    (`docs/audits/`); `/help`.
  - **Deliberately outbound-of-writes:** it never authorizes a PR — cutting a
    `fix/<slug>` PR stays inside the OpenClaw sandbox, so no second, less-guarded
    write path is introduced (mirrors the "candidates only, nothing live" model).
  - Security: **sender allowlist** (`TELEGRAM_ALLOW_FROM` vs `message.from.id`;
    unknown senders ignored **silently**), the untrusted git ref is
    **charset-validated** (branch/tag/sha only) before it reaches the issue, and
    the poll **acknowledges the offset before processing** so a crash can't re-file
    duplicates. No-op without `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOW_FROM`.
- **`.github/workflows/telegram-intake.yml`** — poll (`*/10`) + `workflow_dispatch`
  (relay push); `permissions: issues: write` only; no-op-cheap when unconfigured.
- New `tests/test_telegram_intake.py` (14 stdlib `unittest` tests) pinning the
  allowlist silent-drop, ref-charset guard, `/audit`→issue, `/status`, and
  acknowledge-first invariants. Docs: `docs/telegram/standalone-intake.md`;
  `.env.example` / README updated with `TELEGRAM_GITHUB_TOKEN`.

### Security / Changed — hardening from the solution review (S1–S3, T2)
- **Broker-side classification (S2)** — the untrusted Worker microVM no longer
  ships a self-declared verdict. `scripts/nightly-audit.sh` now emits a raw
  `nightly-evidence.json` (gate exit codes) and the Worker sends only that + the
  promptfoo JSON over vsock; the **trusted Broker** re-derives the verdict with
  its own classifier (`nightly_audit_report.py --from-evidence`). Missing/garbled
  evidence classifies as **hard-fail, never green**, and an exit-code/promptfoo
  mismatch (forged all-zero exit codes) is caught — a compromised Worker can no
  longer forge a pass. New `tests/test_nightly_audit_report.py` (6 tests) pins the
  forgery-resistance. Also fixed a latent portability bug: the Broker's tar
  extraction used the non-portable `--no-absolute-names` (rejected by GNU tar 1.35);
  the path-traversal guard is the explicit exact-name member list, which is portable.
- **Worker egress interlock (S3)** — `deploy/microvm/run-worker.sh` refuses to boot
  unless the host egress allowlist is loaded, and runs qemu as a dedicated
  unprivileged UID so the ruleset actually binds (override `EGRESS_ALLOWLIST=off`
  for isolated dev hosts, loud warning). New `deploy/microvm/egress-allowlist.nft`
  (+ `apply-egress-allowlist.sh`) ships the nftables ruleset as code: DNS + web to
  the public internet only, host-LAN/link-local dropped, all other ports denied.
  Corrected the misleading `restrict=off` comment (SLIRP isolates the guest from
  the host LAN but does NOT limit which internet hosts it reaches — that is the
  host firewall's job) and the `restrict=on` example in `forkd-isolation.md` (it
  would sever the guest from the network entirely). `00-preflight.sh` now checks
  for `nft` + the loaded table.
- **Cross-family grader (S1)** — the llm-rubric grader now defaults to a genuinely
  DIFFERENT model family than the (Anthropic) writer: `openai:gpt-4o-mini`, or a
  local `ollama:chat:llama3.1` via `GRADER_PROVIDER` (zero cloud key), passed to
  promptfoo with `--grader`. Fixed the previous `anthropic:claude-sonnet-4-6` /
  `claude-haiku-4-5` defaults (same family as the writer — not an independent
  check) in `promptfoo/promptfooconfig.yaml`, `tensorzero/tensorzero.toml`, the CI
  template, `.env.example`, both READMEs, and the cron/Pi docs.
- **Pinned truth-engine (T2)** — CI and `nightly-audit.sh` no longer run
  `promptfoo@latest` (a deterministic gate must not reload a moving target). Pinned
  to `0.121.17` via a single `PROMPTFOO_VERSION` knob; bump deliberately.

### Added
- **Phase 5 rollout kit** (`deploy/`) — runnable scripts to actually deploy the
  hardening layers on a local Linux VM (Broker = host VM with OpenClaw +
  credentials + TensorZero; Worker = throwaway microVM per run, no credentials,
  vsock-only result channel):
  - `deploy/00-preflight.sh`: host readiness (nested KVM, qemu, socat,
    cloud-image-utils, envsubst, docker, vhost-vsock) — fails loud with a
    remediation line per blocker, changes nothing.
  - `deploy/tensorzero/up.sh` + `episode-tokens.sh`: bring up the gateway +
    ClickHouse, healthcheck + smoke an inference, and sum one run's tokens from
    ClickHouse so the per-run cost-cap uses the REAL writer+grader total.
  - `deploy/microvm/{build-worker-image.sh,run-worker.sh,worker-cloud-init.yaml.tmpl}`
    + `channel/broker-listener.sh`: build a Debian-cloud-image Worker + cloud-init
    seed, boot ONE throwaway microVM per run (fresh qcow2 overlay, discarded
    after), run `nightly-audit.sh` read-only inside it, and ship only
    summary.json + report.md back over vsock to the host dropbox. Received data
    is treated as untrusted (fixed filename extraction, no exec).
  - `scripts/nightly-audit.sh`: when `TENSORZERO_GATEWAY` is set, tags each run
    with an episode id and feeds the gateway's real per-run token total to
    `budget_guard` (falls back to the promptfoo count otherwise).
  - `docs/deployment/phase5-rollout.md`: the end-to-end runbook (preflight →
    TensorZero → Worker microVM → egress allowlist → cron cutover) with a
    "fertig wenn" gate per step and a one-line rollback. `.env.example` gained
    `TENSORZERO_GATEWAY` / `CLICKHOUSE_HTTP`; `.gitignore` excludes the generated
    images/overlays/seed.
- **Phase 5 hardening & scaling** (optional — budget guardrails implemented;
  forkd/TensorZero prepared as ops guides + config templates):
  - `scripts/budget_guard.py` + `tests/test_budget_guard.py`: the budget
    leitplanken as runnable, stdlib-only code (15 unit tests, `python3 -m
    unittest tests.test_budget_guard`). A **circuit breaker** (opens after N
    consecutive hard-fails or a budget breach; half-open trial after a cooldown;
    a green/findings run closes it), a **token ceiling** (per-run + rolling
    window, read from the promptfoo `--output` JSON), and validation/surfacing of
    the **max-iterations** knob. State lives atomically in the gitignored
    `.audit/budget-state.json`.
  - `scripts/nightly-audit.sh`: wired the guard in as step `0` (preflight — an
    open breaker writes a hard-fail-shaped report+summary and exits, so a skipped
    run is routed like any other "did not pass", never announced green) and step
    `6` (record — feeds the outcome + measured tokens back for the next run,
    without rewriting today's verdict). Opt out with `BUDGET_GUARD=0`.
  - `docs/budget/guardrails.md`: the three guardrails, the breaker state machine,
    the exit-code contract, and how max-iterations maps to OpenClaw/TensorZero.
  - `docs/deployment/forkd-isolation.md`: the microVM/KVM ops guide — the
    untrusted-reader vs. credential-holder two-VM split, both the x86
    (forkd/Cloud Hypervisor/Firecracker) and ARM64 (QEMU `microvm` on KVM) paths,
    the vsock channel invariant (results cross, never raw code), and a migration
    checklist. Marked optional / "erst wenn stabil".
  - `docs/observability/tensorzero.md` + `tensorzero/tensorzero.toml` +
    `tensorzero/docker-compose.yml`: the LLM gateway between OpenClaw and the
    provider — per-run cost-caps (episode-tagged token totals fed to the guard),
    A/B variants (writer vs. cheaper candidate, judged against the deterministic
    gate, never the model), and a ClickHouse audit-trail. Provider key lives only
    in the gateway env; bound to localhost.
  - `.env.example`: documented the optional `BUDGET_*`, `CLICKHOUSE_*` and
    `ANTHROPIC_BASE_URL` knobs (all with safe defaults / commented out).
- **Phase 4 nightly-audit OpenClaw cron** (daily 03:00 Europe/Zurich):
  - `scripts/nightly-audit.sh`: the deterministic core. Pulls the target
    read-only (git over HTTPS, no push), runs ruff + mypy + pytest, the
    schema-drift gate (`generate_schemas.py --check`) and the promptfoo eval
    (tool-output contract + OWASP red-team), then writes a concise report +
    `summary.json` under the gitignored `.audit/`. Exit code is the contract:
    `0` green / `2` findings / `1` hard-fail.
  - `scripts/nightly_audit_report.py`: classifies the gate exit codes + the
    promptfoo JSON into schema-drift vs red-team vs toolchain failure, and —
    crucially — separates a *finding* (a red eval) from an **unresolvable
    model/provider error**, which HARD-fails (exit 1) instead of being reported
    as a pass ("hart fehlschlagen, nicht still ausweichen").
  - `openclaw/cron/nightly-audit.json`: the version-controlled job spec
    (isolated session, explicit `model` + `fallbacks: []` so OpenClaw fails the
    run on an unresolvable model, `--announce` to Telegram) and the agent prompt
    that opens/updates `schema-drift`/`redteam` issues, gates any draft PR behind
    an explicit Telegram OK (branch `fix/<slug>`, never `main`), and pushes the
    report.
  - `openclaw/cron/install.sh`: idempotent registration via `openclaw cron
    create`. Requires an explicit `OPENCLAW_AUDIT_MODEL` (no default) and passes
    `--fallbacks ""`, so the job is never registered against a silent model.
  - `docs/cron/nightly-audit.md`: flow, the issue-auto/PR-gated-on-OK split, the
    three-layer model hard-fail, install + management.
- **Phase 2 deterministic verification artifacts** (target-repo templates):
  - `schemas/generate_schemas.py`: derives each tool's output JSON-Schema from
    its FastMCP return type via the in-memory client; `--check` mode fails CI if
    a committed schema drifts from the type hints. Plus `schemas/README.md` and
    a representative `schemas/zurich_datastore_sql.json` /
    hand-authored `schemas/geojson_featurecollection.json` (RFC 7946).
  - `promptfoo/providers/call_tool.py`: implemented the FastMCP in-memory
    provider — calls a tool (or reads a resource) with outbound `httpx` patched
    via `AsyncMock` against `promptfoo/fixtures/` (no live network) and returns
    the raw JSON. Server import + fixtures dir are env-configurable.
  - `promptfoo/fixtures/`: recorded upstream responses backing the contract and
    injection tests (datastore SQL, two GeoJSON layers, STRB, an IPI payload).
  - `promptfoo/promptfooconfig.yaml`: `is-json` contract checks for
    `zurich_datastore_sql` and the two GeoJSON layer surfaces, SQL/STRB injection
    negative-tests, an indirect-prompt-injection "data stays data" rubric (graded
    by an independent model family), and the `pii`/`prompt-injection`/
    `sql-injection` red-team block.
  - `.github/workflows/ci.yml.template`: the `promptfoo` job is documented as the
    REQUIRED check and now runs the schema-drift gate
    (`generate_schemas.py --check`) before the eval.
  - `.github/workflows/live-probe.yml.template` + `scripts/live_probe.py`
    (+ `scripts/live_probe.manifest.json`): weekly cron that queries the real
    Zürich endpoints once, compares response *structure* (not values) against the
    recorded fixtures, and opens/updates a single `schema-drift` tracking issue on
    divergence. Stdlib-only, never fails the cron on a flaky endpoint.

### Changed
- `docs/audits/2026-06-27.md`: replaced the *blocked* placeholder with the
  **real, completed** read-only audit of `zurich-opendata-mcp` v0.3.3 (run in a
  session with target access, folded back here as the canonical Phase-1 record).
  All gates green; 24 tools / 5 resources enumerated with `file:line`; P0 SQL
  surface confirmed clean (validators re-run offline); one P1 watch-item — an
  unescaped CQL passthrough in `zurich_geo_features.property_filter` (geo.py:100)
  — plus the broad mypy `ignore_errors` override flagged as a frozen type gate.

### Added
- `scripts/audit-target.sh`: provisioning + run harness that unblocks a real
  read-only audit. Clones/pins the target MCP repo into a gitignored `.audit/`
  work dir (read-only against the target — no writes, no push), runs
  `ruff`+`mypy`+`pytest`, and captures each exit code + log under
  `.audit/logs/`. Honors `TOOLS.md` (git-over-HTTPS only, no `curl | sh`, token
  never inlined). Must run on a host whose egress allowlist permits the target.
- `docs/audits/README.md`: how a real audit is produced (harness provisions +
  runs; the `python-auditor` agent interprets the logs and writes the report)
  and where it must run. `docs/audits/2026-06-27.md` §4 now points at the harness.
- `.gitignore`: ignore the `.audit/` work dir (cloned target + run logs).
- `openclaw/workspace/skills/python-auditor/SKILL.md`: workspace copy of the
  Phase-1 auditor skill (OpenClaw loads skills from the configured `workspace`).
  `requires.bins: [uv, ruff, mypy, pytest]`; runs ruff+mypy+pytest on every
  analysis and quotes the exact `file:line` from stderr on any non-zero exit.
  Report-only in Phase 1.
- `docs/audits/2026-06-27.md`: first read-only audit of `zurich-opendata-mcp`.
  Records the toolchain run as **blocked** (target source not present in the
  control-plane env / out of GitHub scope) — no pass/fail claimed without an
  observed exit code — and lays out the tool/resource priority matrix (P0
  SQL-injection for `zurich_datastore_sql`, P1 schema-validation for GeoJSON
  tools).
- `docs/deployment/raspberry-pi.md`: deployment guide for running the OpenClaw
  orchestrator on a dedicated, network-isolated **Raspberry Pi 5 (8 GB)** — now
  the recommended deployment for security reasons (hardware/network isolation of
  the credential-holding process from the work PC). Keeps both alternatives
  (local Linux VM, cheap VPS) with their trade-offs.

- `.env.example`, `.gitignore` and the CI template
  (`.github/workflows/ci.yml.template`) that 0.1.0 referenced but did not ship.
  The CI template targets the MCP-server repo (ruff + mypy + pytest + promptfoo)
  and is inert in the auditor repo by design.

### Changed
- Architecture (`docs/plans/2026-06-24-continuous-auditor-v2.md`): added a **Host
  layer** to the target architecture — dedicated Pi 5 as the recommended isolated
  host, with hardware isolation as the outermost of three security layers
  (host → Docker sandbox → forkd). Both READMEs (EN/DE) gained a **Deployment**
  section linking the guide.
- `openclaw/openclaw.json`: Telegram allowlist now resolves from the
  `TELEGRAM_ALLOW_FROM` env var via `${VAR}` substitution instead of a hardcoded
  numeric ID — no secrets in the repo. (`allowFrom` takes plain user IDs, not
  SecretRef objects, per the OpenClaw config docs.)

## [0.1.0] - 2026-06-24

### Added
- Initial scaffold: README (EN/DE), LICENSE, CHANGELOG, .gitignore, .env.example
- v2 build plan under docs/plans
- OpenClaw config + policy-as-code (SOUL.md, AGENTS.md, TOOLS.md)
- Skills: python-auditor, fastmcp-testing, promptfoo-eval
- promptfoo config scaffold + Python provider stub
- CI template (ruff + mypy + pytest + promptfoo)
