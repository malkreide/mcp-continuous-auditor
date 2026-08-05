# Reference-drift probe

> Is the template still the best version of itself — and did its fixes ever
> arrive?

`scripts/reference_drift_probe.py`

## The case

On 2026-08-03 `reference/retry_backoff.py` in `mcp-data-source-probe-skill` was
found to carry five defects. All five had already been fixed — one at a time, in
eleven different servers, over about three months.

The originating commit of `swiss-efv-mcp` contained the same line, word for
word:

```python
raise RuntimeError(f"Upstream unreachable after retries: {last_error}")
```

and the CI failure it produced was what started the whole round.

None of the eleven reviews saw it. Not because the reviewers were careless — in
each repository, only the copied fragment was visible. The template was in
another repository, and nobody had both open at once. Eleven people each fixed
their copy, eleven pull requests merged green, and the file everybody would copy
next was untouched by all of them.

That is the shape of the defect. It is not "somebody made a mistake"; it is that
the artefact nobody is looking at is the one with the largest blast radius, and
that no review can be expected to catch it, because the evidence is not in the
diff.

## Both directions, and which one is worse

| Status | Meaning |
|---|---|
| `REFERENCE_STALE` | the template is behind the servers |
| `REFERENCE_UNADOPTED` | a server is behind the template |
| `UNVERIFIED` | the mapping or the retrieval failed — never silently clean |

`REFERENCE_STALE` is the dangerous one and the report puts it first. A server
that missed a fix is one repository carrying one defect, and it is bounded. A
stale template is a defect being *distributed*: it is handed to every server
built next, at the exact moment somebody is least likely to read the code they
are copying — that is what copying a reference implementation is for.

The probe still reports `REFERENCE_UNADOPTED`, because a fix written once and
never delivered is the failure the whole `reference/` directory exists to
prevent.

## The mapping is declared, never guessed

Template → target code comes from `reference/adoption.toml` in the skill
repository: which template, which repositories, which file and symbol, and since
when. There is no name-similarity fallback and there will not be one.

A guessed mapping is worse here than no mapping at all. A finding produced by
heuristic resemblance says "this function looks like the template and differs
from it", and nobody reading it can tell whether the resemblance was real. It is
unretraceable, and unretraceable findings are how a gate gets switched off — the
same failure the rest of this directory is built to avoid, arriving through a
different door.

So: **no manifest beside a shipped template is itself the first finding**
(`MANIFEST_MISSING`) and the probe stops there. Not "not measured" — a finding. A
template published for copying whose blast radius nobody wrote down is a defect
in its own right, and it is the one that has to be fixed before any comparison
means anything. `TEMPLATE_UNMAPPED` is the same rule per file, for the second
template added to a `reference/` directory that already had a manifest.

Format reference: [`adoption.example.toml`](../../adoption.example.toml), which
reproduces this case.

## What is compared — the decision, and why

Neither of the obvious answers works.

**A full-text diff is unusable.** The adopting repositories rename the constants,
rewrap the lines, reword the error messages and rename the function — every one
of those is a *correct* adoption. A diff reports forty differences, thirty-nine
of which are supposed to be there, and the report is discarded on first reading.

**A function-name comparison is too weak.** It is satisfied by a function that
shares a name and nothing else, which is precisely the state the eleven servers
were in.

So the probe compares the **properties the template guarantees**. For
`retry_backoff.py` those are the five questions somebody would actually ask of a
retry helper:

* does it read `Retry-After`, or does it guess?
* does it jitter, or do all clients retry on the same tick?
* does it cap **after** jittering, so the jitter cannot exceed the ceiling?
* does it bound wall-clock time, or only the number of attempts?
* does it fail with a typed error a caller can branch on?

Each is a small AST predicate declared in the manifest — `calls`, `literal`,
`wraps`, `raises`, `handles` — with `expect = "present"` or `"absent"`, because
half of these fixes are removals.

Two of those deserve their reasoning spelled out.

`literal` matches a **string literal**, and it is used for the header name.
`retry-after` is on the wire: it is the one token in this file that no adopting
repository can rename, which is exactly what makes it worth matching on. The
comparison is case-insensitive, because `Retry-After` and `retry-after` are the
same header.

`wraps` asks whether a call to `outer` has a call to `inner` **inside its
arguments**. That is the difference between `min(cap, base * jitter)` and
`min(cap, base) * jitter`: both contain a cap and a jitter, and only the first
one is bounded. Presence alone cannot tell them apart; containment can, without
any dataflow analysis and without any judgement.

Import aliasing is resolved before matching. `import random as rnd`,
`from random import uniform` and `random.uniform(…)` are the same call written
three ways, and a probe that reported a repository's import convention as drift
would be back to the full-text diff by another route.

## The second layer: unanimity

The declared-property model has one blind spot, and it is exactly this case:
whoever forgets the fix in the template also forgets to write the property down.
A probe with only that layer would have called `retry_backoff.py` clean on
2026-08-03.

So the probe also compares three facts that need no declaration, chosen because
they are the ones copying does **not** rename:

* the names of called functions,
* the types raised,
* the types caught,

each reduced to its last dotted segment, since the part before it is an import
decision that differs per repository without any behaviour differing with it.

A fact is reported only on **unanimity**: every adoption site that could be read
agrees, there are at least `--floor` of them (default 3), and the template
differs. Two repositories agreeing is a coincidence; eleven independently
maintained repositories agreeing is evidence. On the 2026-08-03 tree this layer
reports both halves of the incident without a line of prior declaration —

* *convergent removal*: the template raises `RuntimeError` and none of the
  eleven still does,
* *convergent addition*: all eleven raise a typed upstream error and the
  template does not.

This layer produces `REFERENCE_STALE` and nothing else. An `UNADOPTED` verdict
requires a declared property, because "one server lacks something the others
have" with nothing declared about it is precisely the guess this probe refuses
to make — one repository legitimately differing from ten is ordinary variation,
not drift.

`[unanimity] ignore` in the manifest silences a fact, and every report prints
that list. An exemption that is invisible is a blind spot, which is the same
rule [doc-claim.md](doc-claim.md) applies to its own exemptions.

A `raise Foo(…)` is a call as well as a raise. It is counted only as the raise,
so a changed exception is reported once under the label that describes it rather
than twice under two.

## What it says on that tree

Against a reconstruction of the 2026-08-03 state — the pre-fix template, three
adopting servers, and a manifest declaring exactly one property:

```text
reference/retry_backoff.py::request_with_retry: 1 declared propert(ies), 3/3 adoption site(s) read
coverage: 3/3 site(s); unanimity floor 3
REFERENCE_STALE [high] the template does not satisfy its own declared property
  `reads_retry_after` (honours the server's own Retry-After hint); 3 of 3
  adoption site(s) read do: malkreide/swiss-efv-mcp, malkreide/zurich-opendata-mcp,
  malkreide/swiss-parliament-mcp. Every repository that copies this template next
  inherits the gap
REFERENCE_STALE [high] convergent removal: the template has `raise:RuntimeError`
  and none of the 3 adoption site(s) read still does. Every repository that copied
  this removed it; the template hands it to the next one. No property in
  adoption.toml describes it
REFERENCE_STALE [high] convergent addition: all 3 adoption site(s) read have
  `call:monotonic` and the template does not …
```

One finding came from the declared property. The rest — the `RuntimeError` line,
the wall-clock budget, the jitter — came from the layer that was told nothing
about them.

## Not measured is not clean

Nothing is fetched. `--repos-root` points at checkouts already on disk, and
`--repo-path` names one explicitly. A probe that clones is not reproducible, and
worse, it makes a network failure indistinguishable from a repository nobody has
checked out — the two would report the same thing and mean opposite things.

Every failure to read is a named `UNVERIFIED` entry, never an absence:

| Code | What happened |
|---|---|
| `REPO_NOT_ON_DISK` | the repository is under no `--repos-root` and has no `--repo-path` |
| `SITE_UNREADABLE` | the file or symbol the manifest maps is not there — the mapping itself may be stale |
| `REFERENCE_UNREADABLE` | the template did not parse; nothing was compared in either direction |

Every run states its **coverage** — n of m declared sites read. Findings from
the sites that *were* read still stand; the run simply may not be read as
evidence about the ones that were not.

The two halves of the declared check have different prerequisites, and it is
worth being precise about which. Whether a *server* lags needs that server to
have been read. Whether the *template* satisfies a property the manifest itself
declares needs nothing but the template — it is a claim about one file. So that
half runs with no checkouts at all, and says in the finding that how far the
servers have moved was not measured. The first version gated both on the
checkouts, which meant a fresh manifest produced no declared finding on the
machine it was written on: the run where it matters most. A `SITE_UNREADABLE` entry prints the
`since` date from the manifest, because a mapping declared in May and pointing at
a symbol that has since moved is a finding about the manifest, not about the
code.

`since` is otherwise only printed. Inferring from it — "adopted before the fix
landed, therefore stale" — would be a judgement, and this probe answers with
status codes.

## Running it

```bash
python scripts/reference_drift_probe.py --target <skill-repo> --repos-root ~/src
python scripts/reference_drift_probe.py --target <skill-repo> --repos-root ~/src --format json
python scripts/reference_drift_probe.py --target <skill-repo> \
    --repo-path malkreide/swiss-efv-mcp=/srv/swiss-efv-mcp
python scripts/reference_drift_probe.py --target <skill-repo> --repos-root ~/src --no-unanimity
```

| Exit | Meaning |
|---|---|
| 0 | the template and every readable adoption site agree |
| 2 | finding — `REFERENCE_STALE`, `REFERENCE_UNADOPTED`, `MANIFEST_MISSING`, `MANIFEST_INVALID`, `TEMPLATE_UNMAPPED` |
| 3 | not measured — no `reference/` directory, or nothing to report and no adoption site was readable |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |

## What it deliberately does not do

* **It does not read a repository it was not pointed at.** The manifest names the
  adopters; the probe does not go looking for more.
* **It does not judge whether a property is the right one.** It reports whether
  the template and the servers agree about it. Choosing the guarantees is the
  manifest author's work, and it is reviewable because it is written down.
* **It does not compare Python it cannot parse**, and it does not compare
  anything that is not Python. A template in another language is out of scope
  and says so rather than passing.
* **It does not fix anything.** Read-only, like every probe here: it never
  writes into a target checkout and never edits a template.
