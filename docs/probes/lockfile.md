# Lockfile probe

> `pyproject.toml` states the bound. Does the lockfile — the file the deployment
> actually installs from — state it too?

`scripts/lockfile_probe.py`

## The case

The upper bounds from a portfolio PR were merged, reviewed and green. They were
in `pyproject.toml`. `uv.lock` was not regenerated, so its recorded
`requires-dist` still carried the uncapped range and its pinned versions were
whatever the pre-bound resolution had produced.

On `main`, the fix was present in the file everybody reads and absent from the
file that installs. Every review reads the first one; every `uv sync`, every
container build and every CI job uses the second. Two different dependency
declarations in one repository, and nothing in the toolchain said so.

## Why this is not the yank probe

The yank probe reads `Requires-Dist` off the *index* and asks what a resolver is
told when it installs the package fresh. That is a question about published
metadata. This one is about the repository, and it bites earlier: before the
release, on the branch, where the bound was supposed to take effect.

## The four findings

**`LOCK_DRIFT`** — the specifier `pyproject.toml` states for a dependency is not
the specifier the lock recorded for it. Both are printed side by side, because
"the lock is out of date" is a sentence somebody has to act on and the diverging
pair is the whole of the action. Where the divergence is specifically a missing
cap, the finding says so in words: *the upper bound is in pyproject.toml and NOT
in the lock, so it is not in force where the install happens.*

`uv.lock` makes this directly readable — it echoes the project's own
`requires-dist` under `[package.metadata]`, so the comparison needs no resolver
and no network. Clauses are compared as parsed sets, never as strings:
`>=2.0.0,<3` and `<3,>=2.0.0` are one requirement, and `<3` and `<3.0` are one
bound. A probe that reported those as drift would be retired within a week.

**`LOCK_UNSATISFIED`** — the version pinned in the lock is not admitted by the
specifier in `pyproject.toml`. The sharpest of the four: it does not argue about
metadata hygiene, it says what gets installed violates what the project declares.

**`LOCK_STALE`** — `uv lock --check` or `poetry check --lock` says the lock is out
of date. The tools own the full answer, including the parts this file
deliberately does not model: marker evaluation, the extras closure, the
resolver's own hashes. Where a tool is not installed, its absence is *reported*
rather than counted as agreement.

**`LOCK_MISSING_DEP`** — `pyproject.toml` declares a dependency that has no
package entry in the lock at all.

## What it deliberately does not claim

* **Conditional requirements are skipped**, marker and extras alike — the same
  rule the yank probe follows. `extra == 'dev'` is not installed by a plain sync,
  and `python_version < "3.12"` holds for some installs and not others.
* **A missing lockfile is not a finding.** A library that ships no lock has made a
  defensible choice, and a red gate there would teach people to commit a lock
  they never sync from. Exit 3: not measured.
* **Whether the deployment installs from the lock at all** is not checked here. A
  lock nobody syncs from is decoration, and finding that out means reading
  Dockerfiles and workflow files. It is a real gap and it belongs in its own
  probe rather than smuggled into this one's exit code.
* **poetry.lock cannot answer the specifier question.** It does not echo the
  project's own `requires-dist`, so `LOCK_DRIFT` is not available for it. The
  report names the gap instead of letting a poetry repository read as more
  thoroughly checked than it was.

## Read-only, and provably so

`uv lock` **without** `--check` regenerates the very file this probe compares
against. The flag is hard-coded, there is no switch to drop it, and a test
asserts on the exact argv — the difference between this probe and one that
quietly overwrites its own evidence is a single word.

## In the nightly gate

It runs as step **1b** of `scripts/nightly-audit.sh` — immediately after the
target is cloned and **before `uv sync`**. That ordering is not housekeeping, it
is the gate:

`uv sync` re-locks. Measured, not assumed: against a checkout whose `uv.lock`
recorded `mcp[cli]>=1.28.1` while `pyproject.toml` said `>=2.0.0,<3`, a single
`uv sync --offline` rewrote the recorded specifier to the pyproject one before it
failed for want of a cached wheel. Run after the sync, this gate reads a lockfile
its own harness has just repaired and reports every target clean. That is not a
gate, it is a mirror — and nothing else in the script would fail if somebody
moved the block down, which is why `tests/test_gate_timeouts.py` pins the order.

It is launched with `python3`, never `uv run`: the target's venv does not exist
yet at that point, which is the whole reason the probe is stdlib-only.

The classifier gives exit 3 its own line and its own block in the report, and it
does **not** turn the run red. 19 of the 20 servers in this portfolio ship no
lockfile; a gate that went red on all of them would be switched off within a day
and take the one real finding with it. Exit 4 (`MOVED_DURING_RUN`) and 126/127
are hard failures, not findings — a run that read two different trees, or none,
has not established anything about the target.

`LOCKFILE_GATE=off` disables it; `GATE_TIMEOUT_LOCKFILE` bounds it (default 300s,
because `uv lock --check` may consult the index).

Adding this gate is a **rollout step**: an evidence file without a `lockfile`
entry reads as 127 and hard-fails, which is correct for a Worker image that
genuinely did not run it. Roll the Worker and the Broker together — see
[docs/deployment/worker-broker-rollout.md](../deployment/worker-broker-rollout.md).

## Running it

```bash
python scripts/lockfile_probe.py --target ../swiss-electricity-mcp
python scripts/lockfile_probe.py --target . --format json
python scripts/lockfile_probe.py --target . --no-tools    # parsing only, offline
```

| Exit | Meaning |
|---|---|
| 0 | the lock states what `pyproject.toml` states |
| 2 | finding — drift, an unsatisfied pin, a stale lock, a missing entry |
| 3 | not measured — no lockfile in the target |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |
