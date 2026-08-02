# Parity probe

> Does the translated documentation still say the same thing as the original?

`scripts/parity_probe.py`

## The case

The portfolio ships every repository bilingually: `README.md` beside
`README.de.md`, `SECURITY.md` beside `SECURITY.de.md`. Nothing checked the pair.

The English side is where features get written up, so it moves first, and the
German side is the one a Swiss reader opens first. The drift is one-way and it is
invisible: both files are valid Markdown, both render, and the only way to notice
a section that exists in one and not the other is to read both and count.

## Why it does not compare the text

The two files are *supposed* to differ in every word. `Overview` and `Übersicht`
are a correct translation, and a string comparison would call them drift. So the
probe compares only what a translation must preserve.

**The heading skeleton** — the sequence of heading *levels*. A translation may
rename every heading; it may not drop one, add one, or reorder them. When the
sequences diverge the report prints the position and **both** titles, because
"section 7 differs" is not actionable and "`## Roadmap` vs (nothing)" is. Only
the *first* divergence is reported: after one missing section every later
position is shifted, and printing forty consequential mismatches buries the one
that has to be fixed.

**Top-level list items per section** — the shape the observed drift took: a
feature added to one list and not the other. Nested items are not counted; a
translator legitimately splits or joins a sub-point.

**Fenced code blocks** — count first, then content. Two restrictions keep this
honest: comments are stripped, whole-line and trailing alike, because
`# fill in tokens` beside `# Tokens eintragen` is a correct translation; and only
fences that *declare a language* are compared, because an untagged block is as
often a directory tree or a sample report — prose in a monospace font, which a
translation is supposed to translate. A ```` ```bash ```` block is a command, and
a German README that installs a renamed script is wrong in a way no reader can
see from the German alone.

**Link targets** — the set of URLs and relative paths. The cross-language link is
excluded in both directions; it is the one link that is *supposed* to differ, and
reporting it would put a permanent finding on every correctly translated
repository in the portfolio.

**Translation lag** — commits that touched the base file after the last commit
that touched the translation. This is the only check that can say "the German
side is behind" while every structural check above is green, which is exactly
what a partially translated paragraph looks like. Zero lag is what good practice
produces on its own: a change that updates both files in one commit leaves
nothing after it.

## Not measured is not clean

A repository with no translated file has no parity to check and exits `3`.
Without git on the PATH the lag check does not run and the report says so — the
structural findings still stand, but the run must not read as evidence about
freshness it never measured.

Discovery is limited to the checkout root. A translated document deep in `docs/`
is a different editorial commitment from the two files every reader lands on, and
sweeping the whole tree would turn one drifting appendix into a red gate for the
repository.

## Running it

```bash
python scripts/parity_probe.py --target .
python scripts/parity_probe.py --target . --lang fr --format json
python scripts/parity_probe.py --target . --pair README.md:README.de.md
```

| Exit | Meaning |
|---|---|
| 0 | the pairs are structurally parallel |
| 2 | finding — a section, a bullet, a block or a link exists on one side only |
| 3 | not measured — no translated documents found |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |
