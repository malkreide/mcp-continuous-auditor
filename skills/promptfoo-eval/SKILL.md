---
name: promptfoo-eval
description: Run the deterministic verification suite (promptfoo) for the MCP tools — contract asserts, JSON-schema drift checks, and OWASP red-teaming.
requires:
  bins: [npx]
---

# promptfoo Eval

Run from the repo root:

```
promptfoo eval -c promptfoo/promptfooconfig.yaml
```

- Contract + schema asserts are deterministic; they are the source of truth.
- LLM-graded asserts MUST use a grader model of a different family than the
  writer agent (configured via `defaultTest.options.provider`).
- Red-team plugins (`pii`, `prompt-injection`, `sql-injection`) scan the MCP
  surface for OWASP LLM Top 10 weaknesses.

A failing eval blocks the PR. Do not override it — fix the cause.
