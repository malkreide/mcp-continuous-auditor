# Audits

Read-only audit reports for the target MCP server, one file per run:
`docs/audits/<YYYY-MM-DD>.md`.

## How a real audit run is produced

The auditor repo is the **control plane** — it does not vendor the target.
A run has two halves:

1. **Provision + run (harness):** `scripts/audit-target.sh` clones/pins the
   target and runs `ruff` + `mypy` + `pytest`, capturing each exit code and log
   under `.audit/` (gitignored). It writes no report and changes no target code.

   ```bash
   # Public target, default ref (main):
   TARGET_REPO=malkreide/zurich-opendata-mcp scripts/audit-target.sh

   # Pin a ref for a reproducible audit:
   TARGET_REPO=malkreide/zurich-opendata-mcp scripts/audit-target.sh v0.3.1
   ```

2. **Interpret + report (agent):** the `python-auditor` skill reads
   `.audit/logs/{ruff,mypy,pytest}.log`, and for every non-zero exit quotes the
   exact `path/to/file.py:LINE — message` from stderr into `docs/audits/<date>.md`.
   It enumerates the tools/resources from the cloned `src/`
   (`@mcp.tool` / `@mcp.resource` → `file:line`) and folds them into the
   P0/P1/P2 priority matrix. No pass/fail is recorded without an observed exit
   code (`SOUL.md` / `AGENTS.md`).

## Where it runs

`audit-target.sh` must run on a host whose egress allowlist permits the target
origin — the deployed OpenClaw host (see
[`docs/deployment/raspberry-pi.md`](../deployment/raspberry-pi.md) and
`openclaw/workspace/TOOLS.md`). It will **not** run from the restricted
control-plane session, whose network scope excludes the target repo; that is by
design (least privilege), and the harness exists so the run happens where the
policy allows it.

## Credentials

For a public target, no token is needed. For a private target, configure a git
credential helper out-of-band — never inline the PAT into the clone URL
(`TOOLS.md`: the token is never echoed, written to a file, or placed on a
command line).
