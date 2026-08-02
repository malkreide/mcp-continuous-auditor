# Yank probe

> The shipped probe asks whether the version users install **right now** is
> withdrawn. This asks the inverse, across the whole catalogue: does a
> known-broken, **not**-yanked release still exist, with a healthy successor
> beside it?

`scripts/yank_probe.py` · skill: `skills/yank-probe/SKILL.md`

## The case

`zurich-opendata-mcp` 0.5.1 declared `mcp[cli]>=1.28.1` with no upper bound.
`mcp` 2.0.0 removed `mcp.server.fastmcp`, so every fresh install of 0.5.1 died on
import. 0.6.0 fixed it with `mcp[cli]>=2.0.0,<3`.

Superseding was not enough. The broken release stayed selectable for anyone a
resolver had constrained away from 0.6.0 — an old lockfile, a colliding pin, a
`==0.5.1` in somebody's Dockerfile. It had to be yanked, and it was.

## Two details a naive check misses

**All six predecessors were affected.** 0.2.0, 0.3.0, 0.3.3, 0.4.0, 0.5.0 and
0.5.1 each carried an uncapped `mcp` range. A probe reading only `latest-1` would
have found one of six and reported the catalogue clean. So this walks every
version and groups the answer by the dependency boundary rather than by release,
because that is the shape of the fix: the maintainer yanks a *list*.

**A yank is not a deletion.** PEP 592 keeps the release resolvable for an
explicit pin, with a warning, so existing lockfiles do not break. The finding
therefore never says "delete"; it says "yank", and it says what a yank does and
does not do.

## What the finding is allowed to claim

"Known-broken" is a strong word and metadata alone rarely earns it. Four
conditions must **all** hold before `UNYANKED_BROKEN_RELEASE` is raised for a
version V and a dependency D:

1. V is not yanked and is not a pre-release.
2. V's requirement on D has **no** upper bound.
3. The healthy successor R declares D too, and R's requirement **excludes** V's
   own lower bound. This is the corroborating step, and the one that turns a risk
   into a finding: the maintainer's own later release has already declared that
   the series V floors on is not supported.
4. The newest non-pre-release of D that V actually admits is in a **higher major**
   series than V's lower bound. Without this the break is theoretical.

Failing any one of them, the probe stays quiet. A yank is a public,
irreversible-in-practice statement about someone's release, and a probe that
cries wolf about it will be turned off.

A missing yank *reason* is a separate, low finding: `pip` prints
`Reason for being yanked: <none given>` to the one audience a yanked release
still has.

## It never performs a yank

Deliberately, and not as an oversight. Yanking needs a PyPI API token with upload
scope, it changes what every resolver on the internet sees, and "was this release
actually unusable" is a judgement the maintainer owns. There is no flag for it,
every request is a GET, and a test asserts on the CLI surface so that stays true.

## Why it is not part of the shipped probe

`shipped_probe.py --metadata-only` promises two HTTP requests and the nightly
audit depends on that promise. This reads every release's `Requires-Dist` (over
PEP 658 core metadata — no wheel downloads) and every relevant dependency's
version list: O(versions + dependencies) requests. It imports `shipped_probe`'s
index primitives rather than copying them.

## Running it

```bash
python scripts/yank_probe.py --target <path>                    # dist from pyproject.toml
python scripts/yank_probe.py --dist zurich-opendata-mcp --format json
python scripts/yank_probe.py --dist foo-mcp --max-versions 200   # long catalogues
```

| Exit | Meaning |
|---|---|
| 0 | no unyanked known-broken release; every yank carries a reason |
| 2 | findings |
| 127 | the harness could not run — index unreachable, or the healthy successor's own metadata unreadable |

Truncation by `--max-versions` is always reported: a capped run that reads as
"catalogue clean" is the failure this whole probe is about.
