# TOOLS — Execution policy

## Banned shell patterns
- No `curl ... | sh` / `wget ... | bash` (never pipe network data into a shell).
- No `rm -rf`, no recursive force-deletes outside the sandbox workspace.
- No `git push --force`, no pushing to `main`.
- No writing to paths outside the project workspace.

## Credential handling
- The GitHub token is PR-scoped. Never echo it, never write it to a file,
  never pass it to a tool that logs its arguments.
- Never read or print environment variables that contain secrets.

## Sandbox
All execution runs inside the agent's Docker sandbox (scope: agent).
Network egress is limited to: the target GitHub repo, the configured LLM
provider, and the Zurich municipal endpoints used to record fixtures.
