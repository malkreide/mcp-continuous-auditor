"""Every check this repository runs on itself, as an importable package.

    bash scripts/validate.sh          # the documented way
    python -m scripts.checks          # the same, without the wrapper
    python -m scripts.checks 1 2      # only these

WHY THIS REGISTRY EXISTS HERE, given the checks were already testable. The
gain differs from the sister repos:

* ONE run names ALL findings. These were seven workflow steps across two
  jobs; the first red one aborted its job and the rest never ran. Every
  failure cost a round.
* The ORDER lives in the number, not in the order of `run:` lines. Check 1
  (pin) and 2 (running Ruff) before the gates 3, 4 and 5 — a lint finding
  produced by an unpinned Ruff is worthless, and that was previously assured
  only by line order.
* A CRASH in a check is reported as a defect in `scripts/checks`, not as a
  finding about the repository.

Under `scripts/` rather than `tools/`: this repo keeps its tooling there. The
chain shares the construction, not the directory names.

The imports below exist for REGISTRATION: `@register` runs at import time.
Miss a line and that module's checks vanish from every run without anything
turning red — which is why `test_registry_covers_every_module` compares two
TEXTS rather than asking the registry at runtime.
"""

from . import hygiene, ruff_gate, toolchain, workflows
from ._core import (
    Check,
    CheckFailed,
    Result,
    all_checks,
    pycache_to_temp,
    register,
    run,
    run_all,
)

__all__ = [
    "Check",
    "CheckFailed",
    "Result",
    "all_checks",
    "hygiene",
    "pycache_to_temp",
    "register",
    "ruff_gate",
    "run",
    "run_all",
    "toolchain",
    "workflows",
]
