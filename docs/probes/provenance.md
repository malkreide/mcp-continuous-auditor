# Provenance — the SHA every report carries

> Which commit is this report about?

`scripts/probe_provenance.py`

## The case

An identity-probe run on a portfolio repository reported `VERSION = "0.4.0"` as a
hand-maintained literal. The finding was correct when it was measured and false
ten minutes later: `main` had moved, and the report named no commit, so there was
nothing in it that could be re-checked, contradicted or dated.

It was not wrong — it was *unanchored*, which is worse, because a wrong finding
gets argued with and an unanchored one gets filed.

Every probe here makes claims about a checkout: "src/ is clean", "the installed
sources differ from the tree", "no upper bound in the lock". None of those
sentences means anything without the commit it is about. A report that omits the
SHA asserts something about a state it cannot name.

## What it does

Two calls, at the two ends of a probe run:

```python
prov = probe_provenance.capture(target)
...                      # the probe runs
prov.recheck()
```

`capture` records `HEAD`, the branch, whether the checkout is shallow, and a
digest of the uncommitted state. `recheck` reads them again. If anything moved
between the two, the report's status becomes `MOVED_DURING_RUN` and the probe
returns that **instead of a verdict**, with exit code `4`.

Refusing to answer is the point. A probe that reads a tree over thirty seconds
while a rebase lands underneath it has measured two different trees and has no
way to know which finding came from which. "I cannot tell you" is a true
statement about that run; a merged verdict is not.

## The four statuses

| Status | Meaning |
|---|---|
| `PINNED` | clean checkout; this commit reproduces what was read |
| `PINNED_DIRTY` | it did not move, but uncommitted changes mean the SHA does not name it either |
| `MOVED_DURING_RUN` | `HEAD` or the working tree changed between the two reads |
| `UNPINNED` | not a git checkout — an unpacked sdist, or no git on the PATH |

`PINNED_DIRTY` is not pedantry. `PINNED` promises that checking out that commit
reproduces what was read; with uncommitted changes in the tree it does not, and a
status that claimed otherwise would be a promise the reader cannot cash.

`UNPINNED` does not fail the run. A probe whose findings are real is still useful
without provenance — it simply must not claim the anchor it does not have, which
is the rule `shipped_probe`'s `NO_TAGS` and `published_probe`'s `UNVERIFIED`
already follow.

## Why the dirty digest, and not just the SHA

`git checkout`, `git stash pop` and an editor saving a file all change what a
probe reads while `HEAD` stays exactly where it was — and that is the *commoner*
case. So the digest covers `git status --porcelain`: the set of paths that differ
from `HEAD` and how.

It is deliberately not a content hash of the worktree. Probes create venvs,
caches and report files while doing their job; a boot probe leaves a
`__pycache__` behind and an install leaves an egg-info. Those paths are excluded
by name, because a probe crying wolf at its own footprints is the fastest way to
get the whole check switched off.

## Not every probe is talking about the checkout

`identity_probe` reads the tree, so a tree that moves invalidates it.
`yank_probe` and `published_probe` read a package index; the checkout only tells
them which distribution to ask about. Suppressing a catalogue finding because
somebody committed locally would be superstition, not rigour.

So `capture(..., decisive=False)` marks a run whose verdict does not come from
the tree. Such a run still records and prints the move — the report says which
commit it started from and that the tree changed — but it keeps its verdict.
`blocking`, not `moved`, is what a probe branches on, and the distinction is
written into the report so a reader never has to infer which of the two happened.

For the probes with no `--target` at all — `live_probe`, `recall_canary`,
`published_probe` — the tree that decides the run is *this* repository: the
manifest that says which targets to visit, and the floors the answers are held
to. "The canary was green" is a different claim depending on which revision of
the manifest it walked, and until this existed, no report said which.

## Exit code 4

Shared by every probe that adopts the module, and outside the 0/2/3/127
vocabulary the gates already read. `portfolio_scan` maps an unknown return code
to an error cell rather than to green or to a finding, which is the correct
reading of a run that reached no verdict.

```bash
python scripts/probe_provenance.py --target . --format json
```
