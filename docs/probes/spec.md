# Spec probe

> Which MCP protocol version does this server actually speak?

`scripts/spec_probe.py` · related: `scripts/transport_boot_probe.py`,
`scripts/portfolio_scan.py`

## The case

MCP spec `2026-07-28` removes the handshake. `initialize`/`initialized` and
`Mcp-Session-Id` are gone; every request carries its protocol version,
`clientInfo` and capabilities in `_meta`. The legacy HTTP+SSE transport is
deprecated with a twelve-month window.

That turns a question nobody in this repository could ask into the one that
decides a migration: *which spec is this server on?* Before this probe the answer
existed nowhere, and the reason is worth stating precisely, because it is not
that the measurement was hard.

`transport_boot_probe.py` carried a single hand-maintained literal:

```python
_PROTOCOL_VERSION = "2025-06-18"
```

It sent that value in `initialize` and in the `MCP-Protocol-Version` header of
every POST, and it **never read the answer back**. The negotiated version arrived
on `result.protocolVersion` on every single successful boot and was discarded —
only `result.tools` was ever looked at. One literal fanned out by import through
`shipped_probe.py` and `rebind_probe.py` into three gates, and none of the three
could tell you what it had been talking to.

That is the same defect class `identity_probe` exists to catch — a hand-maintained
version that drifts while nothing breaks and no test fails — sitting in the
auditor's own source. This probe is the other half of the fix: the boot gate now
reads the negotiated version, and this compares it against every place the
version is *claimed*.

## The false finding this probe was built alongside

A gate that opens with `initialize` cannot tell a migrated server from a broken
one. The old code path was:

```
send initialize  ->  JSON-RPC error  ->  FAIL  ->  exit 2
```

and exit 2 travels through `nightly_audit_report.py` into
`sync_findings_issues.py` and ends as a GitHub issue. The first server in the
portfolio to finish the migration would have been issued a bug report **for
finishing it** — a false finding with high confidence, the wrong addressee, and
automatic escalation.

So `transport_boot_probe.py` gained the status `STATELESS`. A rejected handshake
now triggers a second question rather than a verdict: can the server serve a real
call with no handshake at all? If it can, the result is a pass with a label. If it
cannot, the original failure stands. Only JSON-RPC `-32601` (or a *method not
found* message) takes that branch — an internal error or a crash keeps failing,
because those are what the gate is for.

This is the same shape as the existing `NOT_SELECTED` outcome, and for the same
reason: ask the second question before concluding from the first.

## Four sources, and they disagree independently

| Source | What it is |
|---|---|
| `code` | what the target's own source declares, if it declares anything |
| `artifact` | what the **installed** SDK will actually put on the wire |
| `portfolio` | `mcp_spec_version` from the index repo's `portfolio.json` |
| `wire` | what a running server negotiates — measured |

Any two readable values that disagree are `SPEC_DRIFT`. The interesting pair is
`portfolio` against `wire`: a migration tracker is a plan, and a plan that has
drifted from the deployment is worse than no plan, because it is the one people
consult.

`artifact` is the level the source cannot reach. During this migration a target's
source does not change at all — the SDK version does — so a source-only check
sees a clean repository and a wrong wire. That is `identity_probe --installed`'s
lesson applied to a second field.

## The status vocabulary

| Status | Meaning |
|---|---|
| `SPEC_DRIFT` | two sources name different versions |
| `LEGACY_TRANSPORT` | the wire is demonstrably on a deprecated form, with the remaining days of the window |
| `UNVERIFIED` | a source could not be read — never rendered as «in sync» |
| `SPEC_UNDECLARED` | the source declares no protocol version at all |

`SPEC_UNDECLARED` is a **note, not a finding**, and that is deliberate. Under the
current SDKs the protocol version belongs to the SDK, not to the server: 39 of the
42 servers in this portfolio declare nothing, and they are right not to. A
predicate that turned red on all 39 would be switched off within a day, and a
switched-off check catches nothing. What the status buys is that the report says
*why* it has no code-level value, instead of leaving a blank that reads like
agreement.

`LEGACY_TRANSPORT` carries **each signal's own footing**, which is the correction
described below: `/sse`, an issued `Mcp-Session-Id` and a refused stateless call
are three different statements on three different clocks — and one of them is on
no clock at all. Where the spec gives a date the finding gives it; where it does
not, the finding says so rather than borrowing one. It is a recommendation and
never a gate: every form named is still valid, and a probe that failed the build
today would be asserting a rule that does not yet apply.

## What the first version got wrong

This probe was first written against a written **summary** of the spec rather
than the document, and it stated three assumptions in its own output. All three
were wrong. Two of them would have produced exactly the false finding the probe
exists to prevent — a `LEGACY_TRANSPORT` against a fully migrated server.

The rules below were read from
[the specification](https://modelcontextprotocol.io/specification/2026-07-28) on
**2026-08-05**. Each names its page so the next reader can re-check it instead of
trusting this file, which is precisely what did not happen the first time.

### 1. `_meta` was in the wrong place, with the wrong keys

It belongs in **`params`**, not at the JSON-RPC message root, and its keys are
namespaced:

```json
"params": {
  "_meta": {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "…", "version": "…"},
    "io.modelcontextprotocol/clientCapabilities": {}
  }
}
```

This is not cosmetic. The `MCP-Protocol-Version` header **MUST** match the
`io.modelcontextprotocol/protocolVersion` value in the body, and a mismatch is a
mandatory `400 Bad Request` with `-32020 HeaderMismatch`. A **compliant** server
would therefore have rejected this probe's stateless call — and the probe would
have read that as «the server still requires `initialize`».

### 2. `Mcp-Name` was sent on every call, empty

It mirrors `params.name` or `params.uri` and is required only for `tools/call`,
`resources/read` and `prompts/get`. An empty header with no body field to match
is a `HeaderMismatch` by the same rule that requires it. Same false finding, by a
second route. An empty header is not a neutral one.

### 3. The deprecation clock is not one clock

| What | Deprecated in | Earliest removal |
|---|---|---|
| Roots, Sampling, Logging, DCR | `2026-07-28` | first revision on or after `2027-07-28` |
| HTTP+SSE transport (`/sse`) | `2025-03-26` | **three months after SEP-2596 reaches Final** — the registry gives no date |
| `Mcp-Session-Id` | — | **not deprecated: removed outright** in `2026-07-28` |

The twelve-month window is real, but it governs the four features in the first
row — none of which this probe measures. The `/sse` transport is on a different
footing entirely and its removal date is **not computable** from the published
spec. The first version printed `2027-07-28` for an answering `/sse` endpoint.
That number appears nowhere in the specification; it came from applying the wrong
clock, and a fabricated date in a report is worse than no date, because it gets
planned against.

And «earliest removal» is **eligibility, never a deadline**. The policy is
explicit: *«Features may remain Deprecated, without removal, for much longer than
the minimum deprecation window.»*

### What was right

`initialize`/`notifications/initialized` are removed; `Mcp-Session-Id` is
removed; `Mcp-Method`/`Mcp-Name` are required for compliance; `ttlMs`/`cacheScope`
are required on list results. `server/discover` exists — and is stronger than
assumed: **servers MUST implement it**; it is the *client* that MAY call it.

The probe still sends the stateless call both with and without the required
headers. Now that the rule is confirmed the point is different: the difference
between the two answers is what tells a server that **enforces** the requirement
from one that merely tolerates it, and no single request can make that
distinction.

## The countdown must be reproducible

Every report in this repository names the commit it is about
([provenance.md](provenance.md)). A countdown is time-dependent, which quietly
breaks the same promise from the other side: the same commit produces a different
report tomorrow. `--now YYYY-MM-DD` pins the date, and the tests use it. Without
it the run stamps today's UTC date into the report so the number can at least be
re-derived.

## Running it

```bash
# source + artifact, inside the target's environment
python scripts/spec_probe.py --target ../zurich-opendata-mcp --installed

# add the migration tracker — its absence is NOT agreement
python scripts/spec_probe.py --target . --portfolio ../swiss-public-data-mcp/portfolio.json

# the only source that is evidence rather than a claim
python scripts/spec_probe.py --url https://example.invalid/mcp --format json
```

| Exit | Meaning |
|---|---|
| 0 | every readable source agrees, and the wire is not on a deprecated form |
| 2 | finding — `SPEC_DRIFT` or `LEGACY_TRANSPORT` |
| 3 | not measured — no source could be read |
| 4 | `MOVED_DURING_RUN` — see [provenance.md](provenance.md) |
| 127 | the harness could not run |

Provenance is captured as **decisive** for a checkout run and **non-decisive**
with `--url`: there the verdict comes from a running server and not from the tree,
so a checkout that moves is reported and does not withdraw the answer. The same
distinction `recall_canary` already makes.

## Read-only

Every request is a GET or a JSON-RPC read (`tools/list`, `initialize`,
`server/discover`). The probe recommends a migration and never performs one — and
the deadline it prints is a date, not a gate.

## The portfolio side

Two predicates in `scripts/portfolio_scan.py` answer the fleet-wide half:

* **`sdk_upper_bound`** — is the resolved SDK major fixed, or is it whatever the
  next install picks? `fastmcp` 4.0 is released and breaking, and `fastmcp` 3.x
  pins `mcp<2.0`, so an unbounded server can be moved onto an incompatible line by
  an unrelated `uv sync` while nothing in its own repository changes.
* **`sdk_flavour`** — the official `mcp` SDK or the standalone `fastmcp` package.
  Two different projects that cannot share an environment, and `sdk_major` reports
  `2` for both, which is not the same statement.

Neither is in the default predicate set: adding one there changes the verdict of
every existing targets file without anybody editing it. Both report **every**
target rather than hunting for a known answer — a predicate built around an
expected outlier confirms it. The outlier pass turns the column into the row that
breaks the pattern, which is the reason this is a matrix and not *N* reports.

## The promptfoo half, and its limit

The determ profile gained asserts for the list-response contract — `tools/list`,
`resources/list` and `prompts/list` **MUST** carry `ttlMs`/`cacheScope` (a
`CacheableResult`) and **SHOULD** come back in a deterministic order. The
difference in strength is why the order assert checks uniqueness rather than a
fixed sequence.

`cacheScope`'s vocabulary is `public` | `private`. The first version of the
assert listed `session|client|global|none`, which was invented — it would have
**failed every compliant server and passed none**, the precise inversion of what
an assert is for. It is fixed, and it is the fourth thing this probe got wrong by
reading a summary instead of the document.

It did **not** gain a stateless assert, and that is the honest outcome rather than
an omission. The provider drives the FastMCP in-memory client in-process; there is
no wire. «A call without `initialize` succeeds» asserted through it would be a
statement about the SDK and would pass for every server, migrated or not — the
exact false green this repository exists to prevent. The handshake question is
measured in `spec_probe.py --url`, on a real connection, or it is not measured.

The `ttlMs`/`cacheScope` asserts are baseline-gated for the mirror-image reason:
whether the installed SDK surfaces the fields at all is a fact about the SDK, and
a red pipeline on an SDK that predates them would report the client's age as the
server's defect. Determinism — which every SDK can honour — is asserted hard; the
field pair fails only on the one shape that is definitely wrong, a value that is
present and malformed. `raw_shape` in the payload keeps «the server omitted it»
apart from «this client cannot see it». Tighten to a hard requirement once the
portfolio baseline is on an SDK that surfaces them.
