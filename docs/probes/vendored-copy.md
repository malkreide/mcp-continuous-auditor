# Vendored-copy probe

> Do the files that claim to be identical still match — and does anything say so
> when they stop?

`scripts/vendored_copy_probe.py`

## The case

`sparql_client.py` is held in two repositories, `swiss-environment-mcp` and
`fedlex-mcp`, and both copies carry this header:

```
VENDORED COPY (v1.1.0). Dieses Modul wird **byte-identisch** in mehreren
`*-mcp`-Servern des Portfolios vorgehalten […] Änderungen hier und in den
Schwesterkopien **synchron** halten.
```

On 2026-08-07 they were 250 lines and 140 lines. The retry policy
([`ARCH-014`](https://github.com/malkreide/mcp-audit-skill/blob/main/checks/ARCH-014.md))
had been repaired in one copy and never reached the other: one server honoured
`Retry-After` and jittered its backoff, its twin did neither, and the portfolio
run that found it was looking for something else entirely.

**And the marker read `v1.1.0` on both sides.**

That is the part worth mechanising, and it is a separate thing from the drift.
The drift is ordinary — somebody edits one copy under time pressure and the
second edit never happens. What made it *survive* is that both copies declared
the same version. There was no artefact anywhere that disagreed with anything:
in each repository only one half is visible, and both halves said `v1.1.0`.

A reviewer in `fedlex-mcp` saw a correct file that said v1.1.0. A reviewer in
`swiss-environment-mcp` saw a stale file that said v1.1.0. Neither had any
reason to look further.

## Why this is not `reference-drift`

The two probes look adjacent and compare opposite things.

| | reference-drift | vendored-copy |
|---|---|---|
| Arrangement | one template, N adopters | N copies, no original |
| What adopters may change | constants, function names, wrapping, messages | **nothing** |
| Comparison | AST properties | bytes |
| Finding when text differs | not a finding — renaming is correct adoption | the finding |

`reference-drift` refuses a text diff on purpose: an adopter that renames
`MAX_DELAY_S` to `RETRY_MAX_DELAY` has adopted correctly, and a probe that calls
that drift reports forty things nobody will fix.

A vendored copy is not adopted, it is duplicated, and it declares byte identity
about itself. Where that is the contract, comparing bytes **is** the contract.
Nothing here is a property predicate, because nothing here is allowed to be
renamed.

## The findings

`COPY_DRIFT` — copies that declare byte identity do not match. The detail says
whether they would match with trailing whitespace and blank lines normalised
away, and the severity drops to `medium` when that is all it is. Not because a
stray newline is acceptable — byte identity is still the contract — but because
"fix the newline" and "the retry policy is missing" are different jobs, and a
probe that rates them alike gets ignored. Both digests are printed; which copy
is *right* is a judgement about the code, and the probe does not make it.

`MARKER_STALE` (`high`) — the copies differ and every marker read is the same
string. The incident above. It is a distinct finding rather than a footnote,
because it names the reason the drift survived rather than the drift. Bumping
the marker on the edited side does not fix the drift; it makes it visible.

`MARKER_SPLIT` (`low`) — the copies differ and so do the markers. Deliberately
mild: this is drift that announces itself, and anybody comparing the two headers
learns the truth in one second.

`MARKER_MISSING` (`medium`) — a copy carries no marker at all. Not the same as
stale: there is nothing to have gone stale. But a copy with no marker cannot
tell a reader which version it is, which is how the next `MARKER_STALE` starts.

## What is never a pass

`SITE_MISSING` — the repository is not checked out. Nothing is fetched, by
design: a probe that clones is neither reproducible nor read-only, and it turns
a network error into something indistinguishable from a repository nobody
checked out.

`SITE_UNREADABLE` — the file is not where the manifest says. That is a loud
finding about a stale *mapping*, not a silent skip.

`GROUP_UNMEASURED` — fewer than two copies could be read. A group compared
against one file is not clean, it is unmeasured, and the exit code says `3` and
not `0`.

The manifest itself is checked before anything runs. A group with one site is
rejected outright rather than reported clean — it would always be green and
measure nothing.

## The mapping is declared, never guessed

Which files in which repositories claim to be the same file comes from
`vendored.toml` and nowhere else. There is no name-similarity fallback and there
will not be one, for the reason `reference-drift` gives in the same words: a
guessed mapping produces findings nobody can retrace, and a finding nobody can
retrace is how a gate gets switched off.

See [`vendored.example.toml`](../../vendored.example.toml) for the format.

## Running it

```bash
scripts/vendored_copy_probe.py --manifest vendored.toml --repos-root ~/src
scripts/vendored_copy_probe.py --manifest vendored.toml \
    --repo-path malkreide/fedlex-mcp=/tmp/fedlex --format json
```

Exit codes: `0` green · `2` findings · `3` nothing measured · `127` cannot run ·
`4` the checkout moved during the run (`probe_provenance`).

## The real fix, and why the probe still earns its place

The header names the exit itself: the copies exist *until an installable
`swiss-mcp-commons` package exists*, at which point an import replaces them and
the group goes away. The probe does not compete with that — it holds the
invariant for as long as the interim lasts, and the interim is where the defect
lived.
