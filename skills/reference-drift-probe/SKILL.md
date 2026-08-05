---
name: reference-drift-probe
description: Compare the code a skill repository ships under reference/ against the servers that copied it, in BOTH directions — REFERENCE_STALE when the template is behind the servers, REFERENCE_UNADOPTED when a server is behind the template, UNVERIFIED when the mapping or the retrieval failed. The mapping comes from an explicit reference/adoption.toml and is never guessed. Deterministic and read-only; run it, do not reason about it.
requires:
  bins: [python, git]
---

# Reference-drift probe

Is the template still the best version of itself?

```bash
python scripts/reference_drift_probe.py --target <skill-repo> --repos-root ~/src
python scripts/reference_drift_probe.py --target <skill-repo> --repos-root ~/src --format json
python scripts/reference_drift_probe.py --target <skill-repo> \
    --repo-path malkreide/swiss-efv-mcp=/srv/swiss-efv-mcp
python scripts/reference_drift_probe.py --target <skill-repo> --repos-root ~/src --no-unanimity
```

Exit `0` agree, `2` **findings**, `3` nothing readable (NOT MEASURED), `4` the
checkout moved during the run, `127` the harness could not run.

## Why

`reference/` in a skill repository ships code that gets copied into servers.
After the copy, the two halves drift apart in both directions and neither side
looks wrong: in the server only the copied fragment is visible, and in the skill
repository there is no server. Nobody is ever looking at both.

The dangerous direction is the template being behind. A server that missed a fix
is one repository with one defect; a stale template hands the same defect to
every server built next, at the moment somebody is least likely to read the code
they are copying.

## The mapping is declared, never guessed

`reference/adoption.toml` names which template, which repositories, which file
and symbol, and since when. There is no name-similarity fallback. A guessed
mapping produces a finding nobody can retrace, and a finding nobody can retrace
is how a gate gets switched off.

No manifest beside a shipped template ⇒ `MANIFEST_MISSING`, and the probe stops
there. Format reference: [adoption.example.toml](../../adoption.example.toml).

## What is compared, and what is not

| Compared | Not compared |
|---|---|
| the **properties** the template guarantees | the text — the adopters rename the constants |
| a call to a dotted name (`time.monotonic` = there is a wall-clock budget) | which module alias it was written under |
| a string literal (`retry-after` — a header name is on the wire) | the wording of messages, which is prose per server |
| `min` **wrapping** `random.uniform` — the cap applied *after* the jitter | that both merely appear somewhere |
| exception types raised and caught | the function name, which every adopter changes |

Each declared property carries `expect = "present"` or `"absent"`, because half
of these fixes are removals.

## The second layer, and why it exists

The declared list has one blind spot, and it is the incident itself: whoever
forgets the fix in the template forgets to write the property down too. So the
probe also compares three facts that need no declaration — **called function
names, raised exception types, caught exception types**, last dotted segment
only, because those are what copying does not rename.

It reports one only on **unanimity**: every readable adoption site agrees, there
are at least `--floor` of them (default 3), and the template differs. Eleven
independently maintained repositories agreeing is evidence; two are a
coincidence. This layer produces `REFERENCE_STALE` and nothing else — an
`UNADOPTED` verdict needs a declared property, because "one server lacks
something" without a declaration is exactly the guess the probe refuses.

Silence it per fact with `[unanimity] ignore` in the manifest. The report prints
that list: an exemption that is invisible is a blind spot.

## Not measured is not clean

Nothing is fetched. A repository that is not under `--repos-root` and has no
`--repo-path` is `REPO_NOT_ON_DISK`; a moved symbol is `SITE_UNREADABLE`; an
unparseable template is `REFERENCE_UNREADABLE`. Every run states its coverage —
n of m declared sites read — so a narrow run cannot pass for a clean one.

## Fixing a finding

`REFERENCE_STALE` first, always. It is the one still being copied. Fix the
template, then work through the `REFERENCE_UNADOPTED` rows, which name the
repository, the file, the symbol and the date the mapping was declared.

Full write-up: [docs/probes/reference-drift.md](../../docs/probes/reference-drift.md)
