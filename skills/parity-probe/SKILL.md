---
name: parity-probe
description: Compare a bilingual documentation pair (README.md/README.de.md, SECURITY.md/SECURITY.de.md) for structural parity — heading skeleton, per-section bullet counts, tagged code blocks, link targets, and how far the translation lags in git. Never compares prose, which is supposed to differ. Deterministic; run it, do not reason about it.
requires:
  bins: [python, git]
---

# Parity probe

Does the translated documentation still say the same thing?

```bash
python scripts/parity_probe.py --target <path>
python scripts/parity_probe.py --target <path> --lang fr --format json
python scripts/parity_probe.py --target <path> --pair README.md:README.de.md
```

Exit `0` parallel, `2` **findings**, `3` no translated documents (NOT MEASURED),
`4` the checkout moved during the run, `127` the harness could not run.

## Why

The portfolio is bilingual. The English side is where features get written up,
so it moves first; the German side is what a Swiss reader opens first. Both files
render, so a section that exists in one and not the other is invisible without
reading both and counting.

## What is compared, and what is not

| Compared | Not compared |
|---|---|
| the sequence of heading **levels** | heading **text** — `Overview` / `Übersicht` is a correct translation |
| top-level list items per section | nested items — a translator may split a sub-point |
| fenced blocks that **declare a language** | untagged fences — a directory tree is prose in a monospace font |
| commands inside those blocks | comments inside them, whole-line and trailing |
| link targets | the cross-language link itself, in both directions |

`TRANSLATION_LAG` counts commits that touched the base file after the last
commit that touched the translation. It is the only check that fires when every
structural check is green — which is what a half-translated paragraph looks like.
Updating both files in one commit leaves it at zero on its own.

## Fixing a finding

Only the **first** heading divergence is reported: after one missing section
every later position is shifted. Fix that one and re-run rather than working
through a shifted diff.

Full write-up: [docs/probes/parity.md](../../docs/probes/parity.md)
