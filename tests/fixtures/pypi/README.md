# PyPI index fixtures

Recorded responses for `scripts/shipped_probe.py` and `scripts/yank_probe.py`,
so the two index APIs can be made to disagree — and the catalogue can be put
back into a past state — without a live call. The default suite never touches
the network; `RELEASE_GAP_LIVE=1` opts into the live re-measurement in
`tests/test_release_metadata.py`.

All of them describe `zurich-opendata-mcp`, trimmed to the fields the probes
read.

## For `shipped_probe.py` — the two APIs disagreeing

| File | What it is |
|---|---|
| `zurich_simple_converged.json` | **Captured** from `https://pypi.org/simple/zurich-opendata-mcp/` (`Accept: application/vnd.pypi.simple.v1+json`) on 2026-08-01. `0.2.0`–`0.5.1` yanked, `0.6.0`/`0.7.0` live. |
| `zurich_json_converged.json` | **Captured** from `https://pypi.org/pypi/zurich-opendata-mcp/json` in the same minute. Agrees with the Simple API on every version and on every yank flag. |
| `zurich_json_yank_lag.json` | **Derived** from the captured JSON payload by resetting the six yank flags to `false`. |
| `zurich_json_publish_lag.json` | **Derived** from the captured JSON payload by removing `0.7.0` and setting `info.version` back to `0.6.0`. |

## For `yank_probe.py` — the catalogue before the yank

| File | What it is |
|---|---|
| `zurich_simple_files.json` | **Captured** on 2026-08-01, same request as `zurich_simple_converged.json` but keeping `url` and `core-metadata` — the two keys the yank probe needs to reach PEP 658 metadata. |
| `zurich_core_metadata_headers.json` | **Captured**: the `.metadata` sidecar of one wheel per release, all eight, truncated at the blank line that ends the RFC 822 header block. That block is everything the parser reads; the description below it is tens of KB of README per release. |
| `dependency_versions.json` | **Captured** version lists for the six runtime dependencies (`mcp`, `httpx`, `pydantic`, `sqlparse`, `uvicorn`, `defusedxml`). `mcp` is the one that matters: `1.29.0`, then `2.0.0a1`…`2.0.0rc1`, then `2.0.0`. |

`tests/test_yank_probe.py` derives exactly one scenario from these — the state
on 2026-07-31, before the six predecessors were yanked — by flipping those six
flags and changing nothing else.

The header-block capture is worth keeping honest bytes for rather than
hand-writing: PyPI inlines the whole MIT licence as a **folded** `License:`
header, and the blank lines inside it arrive as whitespace-only continuation
lines. A parser that tests "is this line blank" before "is this line a
continuation" ends the header block on the second line of the licence and reads
six dependencies as zero — which the probe would then report as a clean
catalogue. That was a real bug in the first draft, and it is only catchable
against the real bytes.

## Why two of them are derived and not captured

The two divergences these fixtures encode were measured against the live index
on 2026-07-31 — six releases reading `yanked: false` on the JSON API while the
Simple API had them all as yanked, and the JSON API still serving `0.6.0` some
90 s after `0.7.0` was published. Re-measured on 2026-08-01, both had
converged: the captured pair above agrees on everything.

So the divergence is a propagation window, not a standing property of either
API, and it cannot be captured on demand — by the time you notice it is worth
recording, PyPI has caught up. Reconstructing it from the real converged
payload is the honest option remaining: every field except the ones the lag
actually moved is the index's own bytes, and the diff between each derived file
and its captured original is exactly the divergence being claimed.

What is asserted against them is correspondingly narrow. These fixtures prove
the probe behaves correctly *given* two disagreeing APIs. They do not prove
PyPI diverges — that was a measurement, it is written up in the CHANGELOG, and
it is not something a fixture can stand in for.
