# Identity probe

> Does the server report the version it actually is?

`scripts/identity_probe.py` · skill: `skills/identity-probe/SKILL.md`

## The case

Every outbound request carries a User-Agent, and that string is the only thing
an upstream sees about us. When the version in it is a hand-maintained literal,
it drifts: nothing breaks, no test fails, and the server keeps introducing
itself as a release it stopped being months ago.

A sweep across the 30 servers of the Swiss Public Data portfolio (2026-07-29)
found the problem is the rule, not the exception:

* 12 servers sent a wrong version, 4 of them a wrong **major** version —
  `register-mcp` announced 1.0 while the package sat at 0.5.0
* 20 carried a stale `__version__`
* 17 had a README badge behind the package, one by 16 minor versions
* 4 had a stale `server.json` — invisible, because `publish.yml` rewrites that
  field from the tag at release time, so the committed value never reaches the
  artifact and nothing ever contradicts it

None of this is caught by ruff, mypy or pytest, and none of it is a schema drift
the live probe would see. It is a distinct class, and it needs its own
deterministic check.

## Why the detection looks the way it does

Three things in the implementation are deliberate, and each is a bug this probe
made first.

**Scan whole files for the value pattern, not lines for the keyword.**
`grep -i user-agent | grep <version>` misses a constant split over two lines —
which is exactly how `swiss-electricity-mcp` kept shipping 0.2.0 through three
call sites *after* a fix had been merged and reported. The identifier and its
value need not share a line. (It also misses `USER_AGENT`: an underscore is not
a hyphen.)

**Comments are not findings.** The first version flagged
`# the User-Agent in server.py carried "bakom-mcp/1.0"` — a comment documenting
the very incident the check exists to prevent. A rule that turns CI red on good
documentation teaches people to delete the documentation. Comments are stripped
with `tokenize`, not `split("#")`, because a `#` inside a string literal must not
truncate the line.

**Report every category, then exit.** An earlier version aborted on the first
finding. It reported a stale badge and never reached the source scan — for eight
of nine repositories the serious question went unanswered while the report looked
complete.

Fallbacks are recognised by their PEP 440 local segment (`0.0.0+source`), never
by matching a fixed marker string: a portfolio that spells it `0+unknown` in one
repo and `0.0.0+source` in another produced nine false positives that way. A
fallback of plain `"0.0.0"` is correctly reported — it is indistinguishable from
a real release, which is the whole objection to it.

## Artifact-level evidence

`--installed` resolves the User-Agent from the *installed distribution* rather
than the source tree. That is the only check that proves what ships: metadata is
written at install time, so an editable install keeps reporting the pre-bump
version until it is reinstalled. Source can be perfect and the artifact still
wrong.

## Running it

```bash
python scripts/identity_probe.py --target ../swiss-environment-mcp
python scripts/identity_probe.py --target . --installed --format json
```

| Exit | Meaning |
|---|---|
| 0 | no findings |
| 1 | findings — every category is reported before exiting |
| 2 | the target is not shaped as expected (no `pyproject.toml`) |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |

`UNVERIFIED` is its own outcome: if `src/` mentions a User-Agent but neither a
hand-maintained value nor a runtime-assembled one turns up, the probe says so
instead of reporting clean. An artifact-level sweep of 33 published packages was
first pronounced 24-clean by a check that had simply failed to recognise the
shape. 16 of those were drifting.
