---
name: identity-probe
description: Verify that the target server reports the version it actually is — User-Agent, __version__, server.json and README badge against pyproject.toml, plus the installed artifact. Deterministic; run it, do not reason about it.
requires:
  bins: [python]
---

# Identity Probe

Ruff, mypy and pytest all pass on a server that introduces itself to every
upstream as a release it stopped being months ago. Schema drift detection does
not see it either — the responses are fine, it is the *request* that lies.

Run:

```bash
python scripts/identity_probe.py --target <path-to-server-repo>
python scripts/identity_probe.py --target <path> --installed   # artifact-level
```

Exit `0` clean, `1` findings, `2` not a Python MCP repo. Report every line of
the output; the categories are independent and one clean category says nothing
about the others.

## What `--installed` adds, and why it is the one that counts

Without it, only the source tree is checked. With it, the version is resolved
from the **installed distribution** — the only evidence about what actually
ships. Metadata is written at install time, so an editable install keeps
reporting the pre-bump version until someone reinstalls. A repository can be
perfectly clean and the running artifact still wrong.

If the probe says `not_installed`, say so in the report. "Source checked,
artifact not" is a different claim from "verified", and the difference is
where this class of bug lives.

## Do not replace this with a grep

Three shortcuts fail here, each of them a bug this probe made first:

1. **`grep -i user-agent | grep <version>`** misses a constant split over two
   lines. That is how one portfolio server kept shipping `0.2.0` through three
   call sites *after* a fix had been merged and reported as done. It also
   misses `USER_AGENT` — an underscore is not a hyphen.
2. **`split("#")` to drop comments** truncates a line at a `#` inside a string
   literal. And flagging comments at all is worse than useless: the first
   version of this check went red on a comment documenting the very incident it
   exists to prevent, which teaches people to delete the documentation.
3. **Exiting on the first finding** hides the rest. An earlier version reported
   a stale badge and never reached the source scan; for eight of nine
   repositories the serious question went unanswered while the report looked
   complete.

## Reading the output

| Line | Means |
|---|---|
| `DRIFT` | A file repeats the version and disagrees with `pyproject.toml`. Cosmetic for `server.json` (publish rewrites it from the tag) — which is exactly why nothing else catches it. |
| `HARDCODED` | A hand-maintained version under `src/`. This is the one that reaches upstreams. |
| `ARTIFACT` | The installed distribution disagrees with the source. Usually a stale editable install; re-run `pip install -e .`. |
| `NOTE` | Informational — most often "not installed", i.e. artifact not verified. |

A fallback carrying a PEP 440 local segment (`0.0.0+source`) is not a finding.
A bare `"0.0.0"` is, and correctly so: it is indistinguishable from a real
release in a log or a User-Agent.

## Phase discipline

Same as `python-auditor`. **Phase 1: report only.** In write phases the fix is
mechanical — read the version from `importlib.metadata` and build the
User-Agent from it — but the merge gate stays human.
