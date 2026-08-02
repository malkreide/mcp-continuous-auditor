---
name: doc-claim-probe
description: Verify that every identifier the documentation cites — rubric codes, check IDs, constants, file paths, collection membership — actually resolves in the code. Catches a README or SECURITY.md that describes checks and constants which do not exist. Deterministic; run it, do not reason about it.
requires:
  bins: [python]
---

# Doc-claim probe

Do the identifiers the documentation cites actually exist?

```bash
python scripts/doc_claim_probe.py --target <path>
python scripts/doc_claim_probe.py --target <path> --doc SECURITY.md --doc SECURITY.de.md
python scripts/doc_claim_probe.py --target <path> --ignore OPS --format json
```

Exit `0` everything resolves, `2` **findings**, `3` no documentation matched
(NOT MEASURED), `4` the checkout moved during the run, `127` the harness could
not run.

## The incident

An `ARCH-003` justification named ten rubric codes as the ones it had been
graded against. None of the ten was in `GREEN_RUBRICS`. Review did not catch it,
because checking meant opening ten files to look up ten constants — and prose
that *sounds* like it cites the source is exactly the prose reviewers stop
checking.

## What is checked

Only what the author marked as code, in three shapes:

* **identifier codes** — `LOCK_DRIFT`, `ARCH-003`, `NO_TAGS`. All-caps segments
  joined by `-`/`_`; a lowercase letter takes a token out of scope, which is what
  keeps `Requires-Dist` and `User-Agent` out of the findings
* **paths** — must exist in the checkout
* **collection membership** — where a document names a collection constant that
  exists in the code, code-shaped tokens in the same paragraph are checked
  against its actual members

Resolution happens against the repository's non-Markdown files, so a rubric
defined in promptfoo YAML resolves. A code that appears only in the README, the
German README and the CHANGELOG has been *repeated*, not defined.

## Exemptions are listed, never silent

Standards citations (`PEP-658`, `RFC-…`, `CVE-…`) and identifiers on lines that
link to another repository are not resolved — and are printed in the report. An
exemption nobody can see is indistinguishable from a blind spot.

Add repo-specific ones with `--ignore <ID or prefix>`.

## What it does not claim

That the sentence is true. `NO_TAGS` existing does not make the paragraph around
it correct. This answers the smaller, mechanical question, and says so.

Full write-up: [docs/probes/doc-claim.md](../../docs/probes/doc-claim.md)
