---
name: yank-probe
description: Find releases that are known-broken but still installable — an uncapped dependency range whose successor capped it, across every published version, not just the last one. Also flags yanks published without a reason. Recommends a yank; never performs one. Deterministic; run it, do not reason about it.
requires:
  bins: [python]
---

# Yank probe

`shipped-probe` asks whether the version users install **right now** is
withdrawn. This asks the inverse, across the whole catalogue:

> Does a known-unusable, **not**-yanked release still exist, with a healthy
> successor beside it?

Run:

```bash
python scripts/yank_probe.py --target <path>              # dist from pyproject.toml
python scripts/yank_probe.py --dist zurich-opendata-mcp --format json
python scripts/yank_probe.py --dist foo-mcp --index-url https://pypi.example.com/simple
python scripts/yank_probe.py --dist foo-mcp --max-versions 200   # long catalogues
```

Exit `0` clean, `2` **findings**, `127` the harness could not run (index
unreachable, or the healthy successor's own metadata unreadable — a comparison
that did not happen is never a pass).

## Why this is not part of `shipped-probe`

`shipped_probe.py --metadata-only` promises **two HTTP requests**, and
`nightly-audit.sh` depends on that promise: the metadata pre-run exists so the
release verdict survives the full gate hanging, and it only survives because it
is cheap. This probe reads every release's `Requires-Dist` and every relevant
dependency's version list — O(versions + dependencies) requests. Folding it in
would break the exact property the pre-run was added for.

It imports `shipped_probe`'s index primitives rather than copying them, the same
way `shipped_probe` imports `transport_boot_probe`.

## The incident

`zurich-opendata-mcp` 0.5.1 declared `mcp[cli]>=1.28.1` with **no upper bound**.
`mcp` 2.0.0 removed `mcp.server.fastmcp`, so every fresh install died on import.
0.6.0 fixed it with `mcp[cli]>=2.0.0,<3`.

Two things a naive check misses, and both are why this probe is shaped the way
it is:

1. **All six predecessors were affected** — 0.2.0, 0.3.0, 0.3.3, 0.4.0, 0.5.0
   and 0.5.1 each carried an uncapped `mcp` range. A check that looked at
   `latest-1` would have found 0.5.1, reported it, and left five installable
   broken releases behind while reading green. So this walks **every** version
   and groups the answer by the dependency boundary, because that is the shape
   of the fix: the maintainer yanks a *list*.
2. **Superseding is not enough.** The broken releases stayed selectable for any
   resolver constrained away from 0.6.0 — an old lockfile, a colliding pin, a
   `==0.5.1` in somebody's Dockerfile.

## What the finding is allowed to claim

"Known-unusable" is a strong word, and metadata alone rarely earns it. All four
conditions must hold before `UNYANKED_BROKEN_RELEASE` is raised for a version V
and dependency D:

| # | Condition | Without it |
|---|---|---|
| 1 | V is not yanked and not a pre-release | there is nothing to report |
| 2 | V's requirement on D has **no upper bound** | the resolver cannot walk past what V was built against |
| 3 | The healthy successor declares D and its requirement **excludes V's floor** | an uncapped range is a *risk*, not a finding — and a gate that fires on every uncapped range gets muted |
| 4 | The newest non-pre-release of D that V admits is in a **higher major series** than V's floor | the break is theoretical; nothing resolves into it |

Condition 3 is the one that licenses the word "broken": the maintainer's own
later release has already declared that the series V floors on is unsupported.
The finding quotes that pin as its evidence.

## The two findings

| Code | Severity | Means |
|---|---|---|
| `UNYANKED_BROKEN_RELEASE` | high | one or more not-yanked releases resolve across a dependency major boundary their own successor excludes. **Recommends** a yank of the named versions. |
| `YANK_REASON_MISSING` | low | a release is yanked with no PEP 592 reason. `pip` prints `Reason for being yanked: <none given>` to the only audience a yanked release still has — someone an old lockfile dropped onto it, who cannot tell a security withdrawal from a bad build. |

## A yank is not a deletion — and this probe does not perform one

PEP 592 keeps a yanked release resolvable for an **explicit** pin, with a
warning. That is the point: existing lockfiles do not break. So the finding
never says "delete", and neither should you.

The probe **recommends** and never acts. Yanking needs a PyPI API token with
upload scope, it changes what every resolver on the internet sees, and whether a
release was really unusable is the maintainer's judgement. There is no flag that
performs a yank, no credential is read, and every request is a GET —
`tests/test_yank_probe.py::ReadOnlyTest` pins all three. Adding an `--apply`
would be a change of category, not a feature.

## Reading the output

```
yank probe — zurich-opendata-mcp on https://pypi.org/simple
  8 release(s), 6 yanked, healthy successor: 0.7.0
  dependency httpx: index serves 0.28.1
  YANK_REASON_MISSING [low] 6 yanked release(s) carry no reason: …
```

Three lines mean the audit was **narrowed**, and none of them means clean:

- `NOT AUDITED: the N oldest release(s)` — `--max-versions` truncated the walk.
- `metadata unreadable (not audited, not clean)` — a release exposed no PEP 658
  core metadata and the JSON fallback did not apply (non-PyPI index).
- `HARNESS:` — nothing was concluded at all; exit is 127.

## Not wired into the nightly gate

Deliberately. `nightly_audit_report.py::_GATE_NAMES` is fail-closed — a name
added there makes every Worker still running the previous `nightly-audit.sh`
hard-fail, so it is a rollout step that pairs a Worker image with a Broker
image. Run this probe from a skill invocation or by hand until that rollout is
scheduled.
