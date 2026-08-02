# Tests

Two tiers, so the default run needs nothing but the standard library.

## Default (stdlib only)

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Covers:

- `test_budget_guard.py` — the Phase-5 budget guardrails.
- `test_nightly_audit_report.py` — the audit classifier (unit level), including
  the two ways a gate can say nothing while looking like it said yes: a gate
  that **hung** (exit 124/137 from the gate timeout, named in the report and
  deliberately kept out of the finding classes — a timeout is not "ruff found
  problems") and a test gate that returned green having executed **0 tests**.
  `CountTestsTest` pins the parser both classes rest on against the literal
  shapes pytest and unittest emit, above all that unreadable output reads as
  *unknown* and never as zero.
  `ShippedMetadataPreRunTest` covers the shipped gate's `--metadata-only`
  pre-run as it reaches the summary. It is evidence, not a gate — deliberately
  absent from the fail-closed `_GATE_NAMES`, so a Worker image predating it
  classifies exactly as before — and the tests pin both halves of that: the
  verdict is reported, and it never moves the outcome. A pre-run finding does
  not turn a green gate run into `findings`, and a hung gate stays a hang while
  the report gains what the pre-run *did* establish. An absent or unparseable
  report reads as **unknown**, never as clean — the same refusal as the test
  count.
- `test_sdk_dispatch.py` — which "FastMCP" a given server belongs to. Two
  different projects carry the name and cannot share an environment (`fastmcp`
  still requires `mcp` 1.x), so the three scripts that run inside a target's
  environment — the schema gate, the promptfoo provider, the recall canary —
  cannot pin either and dispatch on the server object's own module instead.
  These tests pin the *choosing*, with fake objects and no SDK installed at all,
  including that an old-named `mcp.server.fastmcp.FastMCP` is sent down the SDK
  branch and not the standalone one. A structural test asserts each call site
  imports a client exactly once and inside its own branch helper, so a hard
  `from fastmcp import Client` sneaking back in turns CI red — that pin is what
  made all three unrunnable against a migrated target.
- `test_shipped_probe.py` — the shipped-artifact gate
  (`scripts/shipped_probe.py`), which installs the target's package from PyPI and
  makes *that* prove it runs. The network half is not deterministically testable,
  so the module keeps it in three named seams — `_get` (the one network door),
  the install and the subprocess — and everything that decides anything lives
  outside them: these
  tests own the version comparison, the publication states (absent ≠ stale ≠ index
  ahead), the tool-result classification and the finding set, with the seams
  injected. The one thing **not** faked is the stdio conversation: it runs against
  a real subprocess, because the stdin trap is a *timing* property no fake can
  reproduce, and the fixture delays its `tools/call` answer precisely so closing
  stdin early fabricates a failure. Two tests guard the muting risk from both
  sides — an error that reads like the sandbox's egress raises nothing, while an
  empty content list (the incident's own shape) is never excused as one. A last
  pair asserts the Worker's proxy allowlist still permits the index and that the
  credential-holding Broker's has *not* been widened to match. `ReadIndexTest`
  owns the existence check, which used to consult the JSON API while the install
  resolved against the Simple one — two caches of the same index, for a question
  whose wrong answer (`NOT_ON_INDEX`) tells a maintainer they have no release
  process. It pins that the check asks the index `--index-url` points at (not
  pypi.org regardless, which for a private index is a different host and can
  answer about a package the probe was never looking at), that the JSON API is
  consulted only on PyPI because only PyPI has one, and that a 404 there is
  corroborated before being believed. One test records a deliberate change from
  the pre-merge behaviour: both APIs are now read on PyPI rather than the JSON
  one being a pure fallback, because the release-gap cross-check came with the
  merge and needs the second opinion every time.
- `test_yank_probe.py` — the yank probe (`scripts/yank_probe.py`), which asks the
  inverse of the shipped gate's yank question: not *is the version users install
  withdrawn*, but *is a known-broken release still installable*. Four network
  seams are injected — the project page, a release's PEP 658 core metadata, the
  JSON fallback and a dependency's version list. Stubbing the **fallback** is not
  optional and has its own comment: the index under test is pypi.org, so an
  unreadable release falls through to it, and the two "unreadable metadata" tests
  reached the real network and passed for the wrong reason until it was stubbed.
  The whole suite is asserted offline by re-running it with `socket.connect`
  raising. What is injected is captured, not invented: the real Simple API file
  list, the real `Requires-Dist` header block of all eight `zurich-opendata-mcp`
  releases and the real dependency version lists, with exactly one derived
  scenario — the six predecessors before they were yanked, six flags flipped and
  nothing else. `TheIncidentTest` pins the property the probe exists for, that
  **all six** are named and not just `latest-1`. `ConservatismTest` removes each
  of the four conditions in turn and asserts silence, because a gate that fires
  on every uncapped dependency range gets turned off — `httpx`, `pydantic`,
  `sqlparse`, `uvicorn` and `defusedxml` are uncapped in the same six releases
  and must stay quiet. `MetadataParserTest` owns a bug that is invisible when it
  happens: PyPI inlines the MIT licence as a *folded* `License:` header whose
  blank lines arrive as whitespace-only continuations, so testing blankness
  before continuation ends the header block inside the licence and reads six
  dependencies as **zero** — which the probe then reports as a clean catalogue.
  Only the real bytes catch it. `ReadOnlyTest` pins the boundary that keeps this
  a probe and not a credential holder: no option performs a yank (asserted
  against the argparse surface, not a source grep), no `Authorization` header, no
  `getenv`, and every request a GET. No network, no `git`.
- `test_lockfile_probe.py` — the lockfile probe (`scripts/lockfile_probe.py`),
  which asks the neighbouring question to the yank gate's: `pyproject.toml`
  states the bound, does the lock the deployment installs from state it too?
  Real `pyproject.toml` / `uv.lock` / `poetry.lock` files are written into a temp
  dir and the whole comparison runs over them; only the tool subprocess is
  injected. The central scenario is the incident — bounds merged, the lock not
  regenerated — and it asserts that BOTH specifiers reach the finding, since
  "the lock is out of date" is a sentence somebody has to act on. Roughly half
  the cases are about silence: clause order and trailing zeros are not drift,
  marker-gated requirements are skipped, and a missing lockfile is exit 3 rather
  than a finding. `ToolCheckTest` pins the argv itself — `uv lock` **without**
  `--check` regenerates the file under audit, and the difference between this
  probe and one that overwrites its own evidence is a single flag.
- `test_doc_claim_probe.py` — the doc-claim probe
  (`scripts/doc_claim_probe.py`): every identifier the documentation cites must
  resolve in the code. Half these tests are about what must NOT be reported —
  `Requires-Dist`, `PEP-658`, prose outside backticks, sample output inside a
  fenced block, an identifier belonging to a linked repository — because the
  check is only useful if a red run means something. `MembershipTest` covers the
  incident directly: a code cited beside `GREEN_RUBRICS` that the collection does
  not contain, with the members printed in the finding. `OwnDocumentationTest`
  holds this repository's own READMEs to the check it ships.
- `test_parity_probe.py` — the bilingual parity probe
  (`scripts/parity_probe.py`). The correct-translation cases carry as much weight
  as the drift ones: translated headings, translated comments inside a command,
  an untagged fence containing a directory tree and the cross-language link must
  all stay silent, or the red run becomes something everybody learns to ignore.
  `LagTest` builds a real git repository rather than stubbing the log, because
  "how far has the base moved since the translation was last touched" is a
  question about git's actual behaviour. `OwnDocumentationTest` runs the probe
  against this repository's own README pair.
- `test_probe_provenance.py` — the SHA every report carries
  (`scripts/probe_provenance.py`). Real repositories are built in a temp dir and
  moved underneath a captured provenance: a commit lands, a file is edited
  without committing, a `__pycache__` appears. The last one is the interesting
  test — the probe's own footprints must not count as a move, or every
  full-depth run reports MOVED_DURING_RUN about itself. Also pins the
  `decisive=False` path the index probes use, where the move is reported and the
  verdict stands.
- `test_portfolio_scan.py` — the portfolio fan-out (`scripts/portfolio_scan.py`).
  Built around the three properties that would have caught the nested server
  left on the old SDK: `nested_manifests` flags an unclaimed manifest below the
  root **fail-closed** (a heuristic that only flagged server-shaped ones would
  let through the one that does not match the heuristic — the same bet that lost
  the first time); the outlier pass finds the target that disagrees with the
  majority **with no expectation configured**, because mid-migration nobody
  knows which version is right until they see fourteen agree and one not; and an
  unreachable target yields a row of "could not run" cells while the sweep
  continues, with `incomplete` outranking `findings` so a partial run can never
  read as a clean bill. One test hides PyYAML and asserts the stdlib subset
  reader parses the committed `targets.example.yaml` identically — the Worker has
  no PyYAML, and a targets file that parses differently there drops a server
  from the sweep, which is this module's own failure mode turned on itself.
  `CoverageTest` and the `--only` end-to-end cases own the property added after
  three servers dropped out of the tracking for half a day: a run compares what
  the targets file declares against what it scanned, names every gap **by name**
  (a count is what that campaign already had — 26 and 4, neither of which said
  which three were missing), and gives **no overall verdict at all** when the
  two disagree, which outranks both `findings` and a clean matrix. `--partial`
  is the acknowledgement for a deliberately narrow run and keeps the verdict
  while still naming the gap. `DefaultBranchTest` builds real `master`- and
  `main`-headed repositories and asserts the branch is resolved with
  `git ls-remote --symref` rather than assumed: three of this portfolio's repos
  are on `master`, and a `--branch main` clone against them fails, which reads
  as the target's fault. One test proves an explicit `ref:` is never resolved
  away, and one that an unresolvable default clones the remote HEAD with no
  `--branch` at all rather than falling back to `main`.
  Offline: targets carry a local `path:` so nothing is cloned.
- `test_gate_timeouts.py` — the time bounds in the **real**
  `scripts/nightly-audit.sh`. The committed `run_bounded` helper is lifted out of
  the script and driven in bash (124 on a hang, 137 when `SIGTERM` is ignored,
  and the whole process group dying — a gate is `uv run pytest`, so the process
  that hangs is a grandchild). The rest is structural: every gate invocation,
  the promptfoo eval and the provisioning `git` calls must go through a bounded
  launcher. That is the regression it mostly exists for — the natural way to
  lose a time bound is not to break the helper but to add a gate next year and
  call it directly. Needs `bash` + `timeout`.
- `test_sync_findings_issues.py` — deterministic findings→issue routing: which
  issues a summary implies + open-vs-comment dedup, no network (Analysis U-C).
- `test_broker_pipeline.py` — the **real** Broker handler
  (`deploy/microvm/channel/_receive-one.sh`) driven end-to-end: verdict
  re-derivation + the tar path-traversal guard (Analysis S2). Needs `bash` + `tar`.
- `test_egress_interlock.py` — the **real** egress interlock
  (`deploy/microvm/_egress-interlock.sh`): fail-closed without the nft allowlist
  (Analysis S3). Needs `bash` + `nft`.
- `test_audit_cycle.py` — the **real** Broker orchestrator
  (`deploy/microvm/run-audit-cycle.sh`) with a fake worker: the budget breaker is
  fed the worker's outcome, and a missing result is recorded as hard-fail
  (Analysis T-B). Needs `bash`.
- `test_promptfoo_profiles.py` — the split promptfoo profiles are structurally
  correct: determ is key-less, graded carries the model layer + committed
  red-team, the generative spec is isolated (Analysis T-C / T-A). Needs `PyYAML`;
  self-skips without it.
- `test_improve_loop.py` — the **real** Phase-6c orchestrator
  (`scripts/improve-loop.sh`) end-to-end with a fake writer queue, the fake
  suite runner and a local git target: keeps are committed (only under
  `promptfoo/`), discards journaled, per-iteration budget records land in the
  improve-own state, writer crash / flaky baseline hard-fail the run, and the
  keeps ceiling stops early. Needs `bash` + `git`.
- `test_improve_writer.py` — the Phase-6c writer (`scripts/improve_writer.py`)
  with an injected API transport: exit contract 0/10/1, fence unwrapping,
  refusal → graceful stop, token accounting, journal tail in the prompt. No
  network, no real key.
- `test_improve_loop_support.py` — report aggregation (keep/discard tallies,
  skip-lines run isolation, hard-fail outcome) and idempotent draft-PR
  publishing with an injected GitHub opener.
- `test_improve_acceptance.py` — the Phase-6 acceptance harness
  (`scripts/improve_acceptance.py`): a valid candidate is kept, a flaky one
  (D1) and a red-on-HEAD one (D2) are discarded, out-of-scope/invalid patches
  are discards, a flaky baseline or crashing runner is a HARD failure, the
  candidate is always reverted, and the journal is append-only. Phase 6b adds
  D3: in `schema-path` (lite) mode a duplicate/no-new-schema-ref candidate is
  discarded as `redundant`; in `mutation` mode a candidate is kept only if it
  kills a mutant surviving the existing suite (kill map cached per target SHA;
  empty or fully-killed pools HARD-fail). Needs `git`; the suite runner is a
  local fake and mutants are plain diffs — no promptfoo, no mutmut, no network.
- `test_transport_boot_probe.py` — the transport boot gate
  (`scripts/transport_boot_probe.py`), the only gate that observes the *running*
  process. Built around the two bugs no other gate can see: a crash at start
  under the new SDK (`boot_stdio_crash.py`, the read-only settings object) and an
  HTTP 421 for every request under a real hostname (`boot_http_server.py` in
  `host421` mode). One test deliberately proves that a loopback-only probe calls
  the 421 server healthy — that is why the gate varies the `Host` header. Another
  pins the stdin trap: the *same* healthy stdio server is measured as broken the
  moment stdin is closed after the write, so a regression there shows up as a
  test failure rather than as false findings against slow targets. The fixtures
  speak enough JSON-RPC on their own, so all of this is stdlib-only and offline;
  `FastMCPBootTest` is the one class needing `fastmcp` and self-skips without it.
- `test_rebind_probe.py` — the DNS-rebinding gate (`scripts/rebind_probe.py`),
  which boots the target with an inbound `Host`/`Origin` allow-list configured
  and then tries to walk past it. The fixture
  (`tests/fixtures/rebind_http_server.py`) ships six servers that are hard to
  tell apart from outside, and the tests exist to keep the gate from collapsing
  any two of them into one answer. Two carry the design: `loopback_only` refuses
  the attacker's hostname exactly as convincingly as a correct server does, so a
  probe against `evil.example.com` alone would have called it protected — and
  against `hostname_only` probes 1, 3 and 4 answer *identically* to the healthy
  server, so the wrong-port probe is the only thing separating a loose allow-list
  from a strict one. A third pins the token case: `auth_first` holds the host
  check for anonymous requests and folds the moment a valid token appears, which
  is authentication wearing the control's name. The gate's third outcome —
  *control not configured*, exit 3 — is asserted to be neither a pass nor a
  finding, and `FastMCPRebindTest` proves a vanilla FastMCP server lands there
  rather than producing a false alarm.
  `TransportSelectionTest` covers the third outcome: the gate asks for a
  transport through env vars, and a target that selects it with a CLI flag
  (`boot_flag_transport_server.py`, `zurich-opendata-mcp` in miniature) runs its
  default and exits **cleanly**. That is "we never got to ask", not "it does not
  come up" — the gate used to conflate them and report a healthy server as dead.
  The discriminator is the exit code of the target's *own* invocation: non-zero
  means it tried and died and stays a finding. One test pins that a guessed flag
  which makes argparse exit non-zero cannot vote on the verdict — otherwise the
  fix would swap one false finding for another.
- `test_release_metadata.py` — the metadata depth of the shipped probe
  (`scripts/shipped_probe.py --metadata-only`), formerly `release_gap.py`.
  Hardest on the three properties whose failure would turn it into noise or
  into a lie: an unreachable PyPI must not read as "in sync"; a missing tag set
  (a `--depth 1` clone fetches none) must not read as "never released"; and two
  disagreeing PyPI index APIs must produce neither a finding nor a clean bill.
  The git-backed cases build a real repository in a temp dir rather than mocking
  `git log`, which would only assert that the mock matches the assumption.
  Index responses are recorded, in `tests/fixtures/pypi/` — two captured from
  the live index and two reconstructed from them, with that README explaining
  which is which and why the divergence could not be captured directly. They
  are injected at `shipped_probe._get`, the one point that touches the network,
  so the parsing, the yank attribution and the reconciliation all still run.
  Two classes pin the divergences measured on 2026-07-31 against
  `zurich-opendata-mcp`: `YankLagRegressionTest` (the JSON API reporting six
  yanked releases as healthy) and `PublishLagRegressionTest` (the JSON API
  still serving `0.6.0` some 90 s after `0.7.0` was published, which the probe
  used to report as a high-severity `PUBLISH_GAP` against a release that had
  just succeeded). `SimpleHtmlTest` owns the PEP 503 HTML flavour, which is the
  only format an arbitrary index is required to serve and therefore what
  honouring `--index-url` rests on: that `data-yanked` is a yank by its
  *presence* (an empty value is still a yank), that a missing PEP 700 `versions`
  key means deriving versions from filenames rather than reporting none, and
  that a body which is neither JSON nor a project page stays `unreachable`
  instead of becoming an empty success. `LiveDivergenceTest` re-runs the
  measurement against the real index and is skipped unless `RELEASE_GAP_LIVE=1`
  — it asserts that both APIs answered and prints what they said, and fetches
  PyPI's own project page in both flavours to check the HTML parser against real
  markup. `CustomIndexTest` owns `--index-url`, and its sharpest assertion is a
  negative one: pypi.org must appear **nowhere** in the requests made for a
  private-index target, since it would be answering about a different package
  that happens to share the name. A tool that must not be called cannot be
  tested for by its answer, so the stub records which APIs were reached and the
  test asserts on that list. The rest pins that the missing cross-check is
  stated rather than silently skipped, that `UNCONFIRMED` is unreachable with
  only one opinion available, and one end-to-end case proving the pieces
  compose: PEP 503 HTML, no `versions` key, no JSON API, through to a
  `RELEASE_YANKED` naming the release installs fall back to.
  Three classes own the anchors added after the campaign measurement.
  `NoTagsTest` pins all three tag states apart: a full checkout with no tags is
  the `NO_TAGS` finding (half these checks measure against the last tag and
  therefore measured nothing, so a green run there is a statement about how
  little was compared), a tagged repository raises nothing, and a **real**
  `--depth 1` clone — built by cloning a real repo, because `git tag --list`
  succeeds with empty output there and is otherwise indistinguishable — stays
  *unknown* and raises nothing. `CommitKindBeatsAgeTest` pins that kind beats
  age: a `fix:` half a day old is reported where a `docs:` of the same age is
  not, a breaking change is reported at any age in either spelling (`feat!:` and
  a subject `BREAKING CHANGE`), and `--max-age-days-user-facing` buys back a
  grace period so the policy stays a choice. `StaleArtifactTest` owns the
  content comparison — the one gap a version number cannot see — including that
  line endings alone are not a divergence, that a generated `_version.py`
  present only in the wheel is not one either, and that a comparison which could
  not be made is recorded as not made rather than as clean. `PinnedVersionTest`
  covers `--pin-version`: the pin reaches the installer, a venv that comes back
  holding another version makes **no claim at all** (127, not 0 and not 2), and
  the default stays unpinned because a gate is asking a different question.
  Needs `git`; no network in the default run.
- `test_published_probe.py` — the published-artifact probe
  (`scripts/published_probe.py`). Everything here is the half that decides what
  a measurement *means*; the half that installs a distribution is not tested,
  because a test that needs PyPI is a test that goes red when PyPI has a bad
  afternoon. `ImportVerdictTest` pins the distinction the probe got wrong about
  `bag-health-mcp`: a failure that reproduces with the package root imported
  first is real, one that only appears when the submodule is the very first
  import of a process is an import-order artefact and not a finding, one that
  reproduces in neither fresh interpreter is named apart from it rather than
  claimed to be the order, a missing **extras-only** dependency is not the
  server being broken, and a verification that could not run at all counts as
  real — absence of proof is not a pass. `WatchTest` drives a real subprocess
  (`fixtures/published_smoke_server.py`) through the four shapes the smoke stage
  has to tell apart, plus one that writes far more than a pipe buffer holds: a
  probe that waits for exit without draining deadlocks there and reports a
  failure that never happened. `DependencyCapTest` owns the `requires_dist`
  upper-bound rule, including that `~=` and `==2.*` are bounds although neither
  spells `<`, that a declared-but-never-imported dependency is not reported at
  all, and that an unreadable index is `unknown` and never `capped`.
  `RenderTest` asserts every layer still gets its own line when only one of them
  can be the status. No venv, no network, no `git`.

`test_smoke_target.py`, `test_transport_boot_probe.FastMCPBootTest` and
`test_rebind_probe.FastMCPRebindTest` self-**skip** here — all three need
`fastmcp`.

## With fastmcp (the smoke target, finding U-B)

`test_smoke_target.py` runs `schemas/generate_schemas.py` and
`promptfoo/providers/call_tool.py` against `tests/fixtures/smoke_server.py`, a
tiny local FastMCP server — the two code paths that otherwise only run in the
external target repo. Provide fastmcp, e.g. with uv:

```bash
uv run --with fastmcp python -m unittest tests.test_smoke_target
```

No network is used: the provider's httpx call is mocked against
`tests/smoke_fixtures/`.

`test_rebind_probe.FastMCPRebindTest` joins them: a vanilla FastMCP server on a
non-loopback bind is the fail-open case, and the gate must report it as *control
not configured* rather than as a finding. If that ever flips, the SDK changed its
default transport-security posture — and we want to hear it here, not from a real
target at 03:00.

`test_transport_boot_probe.FastMCPBootTest` joins it there: it boots the same
`smoke_server.py` for real, over stdio and over streamable-http, and speaks
`initialize` + `tools/list` to it. The stdlib fixtures prove the probe's logic;
this proves it against the actual SDK, so a change in FastMCP's startup or
transport handling (the 307 from `/mcp/` to `/mcp` was found exactly this way)
surfaces here instead of in a target repo at 03:00. Still no network — only
`tools/list` is called, never a tool.
