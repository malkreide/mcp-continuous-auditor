# Doc-claim probe

> Do the identifiers the documentation cites actually exist in the code?

`scripts/doc_claim_probe.py`

## The case

A written justification for a finding — `ARCH-003` — named ten rubric codes as
the ones it had been graded against. None of the ten was in `GREEN_RUBRICS`. The
prose was confident, internally consistent, and about identifiers that were not
in the code.

Nobody caught it in review, because checking would have meant opening ten files
to look up ten constants. Prose that *sounds* like it cites the source is exactly
the prose reviewers stop checking.

That is a mechanical error and it deserves a mechanical check. An identifier is
the one part of a documentation claim a machine can verify without understanding
the sentence: if `SECURITY.md` says a check is called `NO_TAGS`, either that
string is in the code or the document is describing something that does not
exist.

## What counts as a claim

Only what the author marked as code — a `` `backtick span` `` — and only three
shapes.

**Identifier codes**: `LOCK_DRIFT`, `ARCH-003`, `NO_TAGS`,
`UNYANKED_BROKEN_RELEASE`. All-caps segments joined by `-` or `_`. The shape is
deliberately narrow: single words (`README`, `GET`) are not claims about
identifiers, and neither are `Requires-Dist` or `User-Agent` — a lowercase letter
takes a token out of scope, which is what keeps the HTTP and packaging
vocabulary out of the findings.

**Paths**: `scripts/yank_probe.py`, `.github/workflows/tests.yml`. A path is a
claim that resolves against the filesystem, and a document pointing at a renamed
file is wrong in a way readers only discover at the worst moment.

**Collection membership**: where a document names a collection constant that
exists in the code — `GREEN_RUBRICS` — every code-shaped token in the same
paragraph is checked against that collection's actual members. This is the
incident, caught exactly where it happened. The finding prints the members, and
distinguishes "the identifier exists elsewhere in the code" from "it does not
exist at all", because those are two different corrections.

## Where a claim may resolve

In the code, and not in more prose. A rubric code that appears in the README, in
the German README and in the CHANGELOG and nowhere else has been *repeated*, not
defined. The resolution index is built from the non-Markdown files of the
repository — `.py`, `.yaml`, `.json`, `.toml`, `.sh`, workflow templates.

A flat token index rather than a Python symbol table, on purpose: a promptfoo
rubric lives in YAML, an exit code in Python and a check name in a shell script,
and all three are equally good answers to "does this identifier exist". A
Python-only index would report every YAML-defined rubric as missing — a probe
wrong about the very incident it was written for.

## What it stays silent about

* **Fenced blocks are read for paths only.** A shell example says
  `python scripts/foo.py` and that path is a real claim; the same block's sample
  *output* is illustration, and flagging an identifier there would turn every
  worked example into a finding.
* **Standards namespaces are exempt.** `PEP-658`, `RFC-6749`, `CVE-2024-1` are
  citations, not identifiers. The list is short, named in the source, and
  extensible with `--ignore`.
* **Identifiers cited beside a link to another repository** belong to that
  repository — this README's `OPS-005` is `mcp-audit-skill`'s rubric. They are
  *listed* in the report rather than dropped: an exemption that is not visible is
  indistinguishable from a blind spot.
* **It does not check whether the sentence is true.** `NO_TAGS` existing does not
  make the paragraph around it correct. This probe answers the smaller question
  it can answer without judgement, and says so rather than implying the larger
  one.

## Running it

```bash
python scripts/doc_claim_probe.py --target .
python scripts/doc_claim_probe.py --target . --doc SECURITY.md --doc SECURITY.de.md
python scripts/doc_claim_probe.py --target . --ignore OPS --format json
```

| Exit | Meaning |
|---|---|
| 0 | every cited identifier and path resolves |
| 2 | finding — an unresolved citation, a dead path, a false membership claim |
| 3 | not measured — no documentation files matched |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run — no code files, so every citation would be "unresolved" and none of it would be true |
