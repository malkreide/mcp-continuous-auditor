#!/usr/bin/env python3
"""A server that dies at start — case 1 (malkreide/parlament-mcp#29).

After the SDK major bump the settings object became read-only, so the old
``mcp.settings.host = ...`` line raises before anything is served. Every existing
gate stays green through this: the module imports, the tools are declared, the
type hints are intact, the committed schemas still match. Only booting it shows
the truth.

Reproduced here with the real exception text so the probe's captured stderr is
recognisable to whoever reads the finding.
"""

from __future__ import annotations

import sys


class _FrozenSettings:
    """Stands in for the SDK's now-immutable settings object."""

    def __setattr__(self, name: str, value: object) -> None:
        raise ValueError(f'"Settings" object has no field "{name}"')


def main() -> int:
    settings = _FrozenSettings()
    settings.host = "0.0.0.0"  # the line that survived the SDK bump unchanged
    return 0  # never reached


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"ValueError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
