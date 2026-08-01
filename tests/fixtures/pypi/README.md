# PyPI index fixtures

Recorded responses for `scripts/release_gap.py`, so the two index APIs can be
made to disagree in a test without a live call. The default suite never touches
the network; `RELEASE_GAP_LIVE=1` opts into the live re-measurement in
`tests/test_release_gap.py`.

All four describe `zurich-opendata-mcp`, trimmed to the fields the probe reads.

| File | What it is |
|---|---|
| `zurich_simple_converged.json` | **Captured** from `https://pypi.org/simple/zurich-opendata-mcp/` (`Accept: application/vnd.pypi.simple.v1+json`) on 2026-08-01. `0.2.0`–`0.5.1` yanked, `0.6.0`/`0.7.0` live. |
| `zurich_json_converged.json` | **Captured** from `https://pypi.org/pypi/zurich-opendata-mcp/json` in the same minute. Agrees with the Simple API on every version and on every yank flag. |
| `zurich_json_yank_lag.json` | **Derived** from the captured JSON payload by resetting the six yank flags to `false`. |
| `zurich_json_publish_lag.json` | **Derived** from the captured JSON payload by removing `0.7.0` and setting `info.version` back to `0.6.0`. |

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
