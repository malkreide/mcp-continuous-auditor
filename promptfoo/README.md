# promptfoo — the deterministic truth engine (two profiles)

promptfoo evaluates the target MCP server's tools in-process (via the FastMCP
in-memory client, `providers/call_tool.py`, httpx mocked against `fixtures/` — no
live network) and asserts their output against the committed schemas + injection
and red-team cases. It is split at the **credential boundary** (Analysis T-C).

## Profiles

| File | Needs a key? | What it runs | Who runs it |
|---|---|---|---|
| `promptfooconfig.determ.yaml` | **no** | contract (`is-json` vs `schemas/`), the schema-drift detector's partner, deterministic injection negatives, the spec-2026-07-28 list-response contract (`ttlMs`/`cacheScope`, deterministic order) | the credential-free Worker microVM; the CI `determ` job (runs on forks too) |
| `promptfooconfig.yaml` | **yes** (grader) | the determ tests **plus** the model-graded layer: `llm-rubric` + committed red-team | CI `graded` job (secrets); a keyed operator run |

`nightly-audit.sh` picks the profile via `PROMPTFOO_PROFILE` (`determ` default,
`graded`, or `full`); an explicit `PROMPTFOO_CONFIG` overrides it. The verdict is
stamped with the profile, so a green **determ-only** run is never read as
"red-team clear" (`graded_layer_ran: false` + a report caveat).

Why the split: the Worker holds **no API keys** by design. The monolithic config's
`llm-rubric` grader needs one, so the Worker would hard-fail on it every night. It
now runs only the key-less `determ` profile; the graded layer runs where a scoped
grader key exists (CI / a keyed run).

## What the determ profile cannot say about the stateless core

The spec-2026-07-28 tests here cover the **payload** half — list responses must
carry `ttlMs`/`cacheScope` and come back in a deterministic order — and stop
there. The stateless core is a **wire** property, and this profile has no wire:
`providers/call_tool.py` drives the in-memory client in-process. An assert that
*«a call without `initialize` succeeds»* would be a statement about the SDK and
would pass for every server, migrated or not — the false green this repository
exists to prevent. That question is measured in `scripts/spec_probe.py --url`,
on a real connection, or it is not measured
([docs/probes/spec.md](../docs/probes/spec.md)).

The `ttlMs`/`cacheScope` assert is baseline-gated for the mirror-image reason.
Whether the installed SDK surfaces the fields at all is a fact about the SDK, so
the assert fails only on a value that is present and **malformed**, and the
payload's `raw_shape` keeps «the server omitted it» apart from «this client
cannot see it». Tighten it to a hard requirement once the portfolio baseline is
on an SDK that surfaces them.

## Red-team actually runs now (T-A)

`promptfoo eval` **ignores** a generative `redteam:` block — only
`promptfoo redteam generate|run` consumes it. Previously the advertised OWASP
red-team therefore never executed. Now:

- **Committed baseline** — `promptfooconfig.yaml` carries real red-team `tests`
  (tagged `metadata.pluginId: pii | prompt-injection | sql-injection`) that
  `eval` runs every time. A failure lights up the classifier's `redteam` branch.
- **Generative expansion** — `redteam/redteam.config.yaml` holds the generative
  spec; `redteam/generate.sh` runs `promptfoo redteam generate` with a **pinned**
  attacker model and writes `redteam/redteam.generated.yaml`. The weekly
  `redteam-regen` workflow regenerates and opens a **PR** when cases change — so
  the gate always evaluates *committed* cases, never a moving target.

```bash
# key-less deterministic profile (no model key needed):
npx -y promptfoo@0.121.17 eval -c promptfoo/promptfooconfig.determ.yaml

# graded profile (needs a grader key; writer != checker):
npx -y promptfoo@0.121.17 eval -c promptfoo/promptfooconfig.yaml

# expand the red-team set (keyed; opens a PR in CI):
promptfoo/redteam/generate.sh
```

Pin the version (`PROMPTFOO_VERSION`, never `@latest`) so the deterministic gate
does not silently reload a new promptfoo. Keep the grader a **different model
family** than the Anthropic writer (`GRADER_PROVIDER`); an Anthropic grader is
refused unless `ALLOW_SAME_FAMILY_GRADER=1` (Analysis S-F).
