# schemas/ — the drift detector

JSON-Schemas of the MCP tool outputs. The schema **is** the deterministic
drift detector of Plan Phase 2: a shape change — an upstream CKAN/WFS field
rename, or a careless return-type edit — surfaces as a reviewable git diff and a
red CI check, never a silent break.

## What lives here

| File | Source | Maintained by |
|---|---|---|
| `zurich_datastore_sql.json` | FastMCP return-type of the tool | `generate_schemas.py` (regenerate) |
| `<other-tool>.json` | FastMCP return-type of each tool | `generate_schemas.py` (regenerate) |
| `geojson_featurecollection.json` | RFC 7946 (GeoJSON) | by hand — resources carry no output schema |

The committed `zurich_datastore_sql.json` here is a **representative** schema for
the auditor repo (the target server is not vendored in). In the target repo,
`generate_schemas.py` regenerates it — and every other tool's schema — from the
live type hints.

## Regenerate (in the target MCP-server repo)

```bash
# server importable as zurich_opendata_mcp.server:mcp (override via MCP_SERVER_IMPORT)
python schemas/generate_schemas.py            # write/update schemas/<tool>.json
python schemas/generate_schemas.py --check    # CI gate: exit 1 if a committed schema drifts
```

## How the three layers fit together

1. **Generated schemas (this dir)** — derived from FastMCP type hints.
2. **`schemas/generate_schemas.py --check`** in `ci.yml` — fails a PR if the
   committed schemas no longer match the type hints (drift not regenerated).
3. **`promptfoo` `is-json`** (`promptfoo/promptfooconfig.yaml`) — validates real
   tool output (in-memory client, fixtures, no network) against these schemas.
4. **Weekly live-probe** (`scripts/live_probe.py`) — compares the *structure* of
   the real endpoints against the recorded fixtures and opens an issue on drift.

A schema mismatch is upstream drift or a regression — review it, update the
fixture **and** schema together, and re-check the contract tests. Do not
silently adapt.
