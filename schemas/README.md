# schemas/ — the drift detector

JSON-Schemas of the MCP tool outputs. The schema **is** the deterministic
drift detector of Plan Phase 2: a shape change — an upstream CKAN/WFS field
rename, or a careless return-type edit — surfaces as a reviewable git diff and a
red CI check, never a silent break.

## What lives here

| File | Source | Maintained by |
|---|---|---|
| `<tool>.json` (in the target repo) | server-derived return-type of each tool | `generate_schemas.py` (regenerate) |
| `geojson_featurecollection.json` | RFC 7946 (GeoJSON) | by hand — resources carry no output schema |

**No tool schema is committed here.** There used to be a `zurich_datastore_sql.json`,
described as "representative" in this file and as "GENERATED from the FastMCP
return type" in its own `$comment`. Neither held: `zurich_datastore_sql` has
returned Markdown with a fenced JSON block of records since the target's first
commit, so its real output schema is `{"result": string}`, and the
`fields`/`records`/`sql` object the file described was never produced. The
contract tests now assert the shape the tool actually returns
(`promptfoo/assertions/contract.js`); the schemas themselves belong in the target
repo, regenerated from its type hints by `generate_schemas.py`.

`geojson_featurecollection.json` declares `"$schema":
"http://json-schema.org/draft-07/schema#"` — **http**, the meta-schema's actual
identifier. Written as https, Ajv (promptfoo's validator) cannot resolve it and
every `is-json` against the file errors out before it validates anything.

## Regenerate (in the target MCP-server repo)

```bash
# server importable as zurich_opendata_mcp.server:mcp (override via MCP_SERVER_IMPORT)
python schemas/generate_schemas.py            # write/update schemas/<tool>.json
python schemas/generate_schemas.py --check    # CI gate: exit 1 if a committed schema drifts
```

## How the three layers fit together

1. **Generated schemas (this dir)** — derived from the tools' type hints by
   whichever SDK the target uses (the official `mcp`, or the separate
   `fastmcp` package — they are different projects; see
   `skills/fastmcp-testing/SKILL.md`).
2. **`schemas/generate_schemas.py --check`** in `ci.yml` — fails a PR if the
   committed schemas no longer match the type hints (drift not regenerated).
3. **`promptfoo` `is-json`** (`promptfoo/promptfooconfig.yaml`) — validates real
   tool output (in-memory client, fixtures, no network) against these schemas.
4. **Weekly live-probe** (`scripts/live_probe.py`) — compares the *structure* of
   the real endpoints against the recorded fixtures and opens an issue on drift.

A schema mismatch is upstream drift or a regression — review it, update the
fixture **and** schema together, and re-check the contract tests. Do not
silently adapt.
