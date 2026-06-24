---
name: python-auditor
description: Run the local CI loop (ruff, mypy, pytest) on the target MCP repo and report violations with exact file:line. In phase 1, report only — do not patch.
requires:
  bins: [uv, ruff, mypy, pytest]
---

# Python Auditor

When auditing, or after any edit in a write phase:

1. `uv run ruff check .`
2. `uv run mypy .`
3. `uv run pytest -q`

For each non-zero exit code:
- Read stderr, extract the exact file and line number.
- Quote it in the report as `path/to/file.py:LINE — message`.

**Phase 1: REPORT ONLY.** Do not modify `src/`.
**Write phases:** locate the violation, apply a minimal patch, re-run until green.

Never report success without showing the command's exit code.
