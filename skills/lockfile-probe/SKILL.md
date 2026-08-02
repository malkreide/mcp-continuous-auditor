---
name: lockfile-probe
description: Check whether the dependency bound declared in pyproject.toml is also in force in the lockfile the deployment installs from. Reports LOCK_DRIFT with both diverging specifiers, LOCK_UNSATISFIED when the pinned version violates the declaration, LOCK_STALE from the resolver's own check. Read-only — `uv lock` is only ever run with `--check`. Deterministic; run it, do not reason about it.
requires:
  bins: [python]
---

# Lockfile probe

`pyproject.toml` states the bound. Does the lockfile — the file the deployment
actually installs from — state it too?

```bash
python scripts/lockfile_probe.py --target <path>
python scripts/lockfile_probe.py --target <path> --format json
python scripts/lockfile_probe.py --target <path> --no-tools   # offline, files only
```

Exit `0` in sync, `2` **findings**, `3` no lockfile (NOT MEASURED — never read as
a pass), `4` the checkout moved during the run, `127` the harness could not run.

## The incident

The upper bounds from a portfolio PR were merged, reviewed and green — in
`pyproject.toml`. `uv.lock` was not regenerated, so its recorded `requires-dist`
still carried the uncapped range and its pins came from the pre-bound
resolution. On `main` the fix was in the file everybody reads and absent from
the file that installs.

## The findings

| Code | Claim |
|---|---|
| `LOCK_DRIFT` | pyproject's specifier ≠ the one the lock recorded. Both printed. Where the difference is a missing cap, the detail says the bound is not in force where the install happens |
| `LOCK_UNSATISFIED` | the pinned version is not admitted by pyproject's specifier — what installs violates what is declared |
| `LOCK_STALE` | `uv lock --check` / `poetry check --lock` says the lock is out of date |
| `LOCK_MISSING_DEP` | a declared dependency has no entry in the lock at all |

Specifiers are compared as parsed clause sets: `>=2.0.0,<3` and `<3,>=2.0.0` are
one requirement, `<3` and `<3.0` one bound. Marker-gated requirements are
skipped — deciding a marker without an environment is a guess.

## It also runs in the nightly gate

Step **1b** of `scripts/nightly-audit.sh`, before `uv sync` — because `uv sync`
re-locks, so a gate placed after it reads a file its own harness just repaired.
`LOCKFILE_GATE=off` to skip it. Exit 3 does not turn the run red.

## Do not "fix" it by re-locking blindly

The finding says which two declarations disagree. Re-locking makes the *lock*
match `pyproject.toml`, which is usually right — but if the pin it produces
crosses a major the project has not tested, that is a separate decision for the
maintainer, not a side effect of silencing a probe.

Full write-up: [docs/probes/lockfile.md](../../docs/probes/lockfile.md)
