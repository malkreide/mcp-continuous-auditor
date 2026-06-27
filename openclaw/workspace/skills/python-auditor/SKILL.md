---
name: python-auditor
description: Run the local CI loop (ruff, mypy, pytest) on the target MCP repo and report violations with exact file:line cited from stderr. In phase 1, report only — do not patch.
requires:
  bins: [uv, ruff, mypy, pytest]
---

# Python Auditor

The workspace copy of the auditor skill (OpenClaw loads skills from the
configured `workspace`, i.e. `openclaw/workspace/skills/`). Keep it in sync
with the catalog copy at `skills/python-auditor/SKILL.md`.

## On every analysis (and after any edit in a write phase)

Run all three, in order, and capture each exit code:

1. `uv run ruff check .`
2. `uv run mypy .`
3. `uv run pytest -q`

## Reporting violations

For each command with a **non-zero exit code**:

- Read its **stderr** (ruff/mypy/pytest all emit `path:line[:col]`).
- Extract the **exact file and line** and quote it verbatim:
  `path/to/file.py:LINE — message`
- Never paraphrase a location. If stderr gives a column, keep it.
- Group findings per tool (ruff / mypy / pytest) and show the exit code.

## Phase gate

**Phase 1: REPORT ONLY.** Never modify `src/`, never commit, never push.

**Write phases (later):** locate the violation, apply a minimal patch, re-run
the three commands until all exit `0`.

## Hard rule

Never report "passed" without showing the command's exit code. A zero exit code
is the only evidence of success — not your reading of the output.
