"""Tests for the Ruff version guard.

The pin guard next door compares two TEXTS. That the Ruff which then runs the
gates carries that version was never measured — and "both places agree" was
reported anyway. `scripts/check_ruff_version.py` closes that; this file proves
it closes it.

The pure side is what gets tested: `compare()` receives the pin, the raw
`ruff --version` output and its exit code as values. No PATH, no subprocess,
no mocks — a mock would only restate this file's own assumption about the
output shape.

The **ANCHOR** cases weigh more than the individual ones: when an anchor goes,
the check has nothing left to compare, and the obvious implementation reports
"passed" for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_ruff_pin import workflow_pins
from scripts.check_ruff_version import compare, parse_version

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_matching_version_is_green() -> None:
    ok, message = compare("0.16.1", "ruff 0.16.1\n", 0)
    assert ok, message
    assert "0.16.1" in message


def test_wrong_version_is_a_finding() -> None:
    """The measured incident: an older Ruff sits earlier on PATH."""
    ok, message = compare("0.16.1", "ruff 0.15.8\n", 0)
    assert not ok
    assert "0.15.8" in message
    assert "0.16.1" in message
    # The finding must say why the pin sync does NOT catch this, or the next
    # reader looks for the fault in the wrong file.
    assert "two texts" in message


def test_ANCHOR_missing_pin_is_a_finding() -> None:
    """No pin means "not compared", not "passed"."""
    ok, message = compare(None, "ruff 0.16.1\n", 0)
    assert not ok
    assert "anchor gone" in message


@pytest.mark.parametrize(
    "raw",
    ["Ruff, version 0.16.1", "", "0.16.1", "ruff\n"],
    ids=["other-shape", "empty", "no-name", "no-version"],
)
def test_ANCHOR_unreadable_output_shape_is_a_finding(raw: str) -> None:
    """If upstream changes the output, the check must not silently do nothing."""
    ok, message = compare("0.16.1", raw, 0)
    assert not ok
    assert "does not answer in the form" in message


def test_failing_invocation_is_a_finding() -> None:
    ok, message = compare("0.16.1", "boom", 3)
    assert not ok
    assert "exited with 3" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ruff 0.16.1", "0.16.1"), ("ruff 0.16.1+deadbeef", "0.16.1+deadbeef")],
)
def test_parse_version(raw: str, expected: str) -> None:
    assert parse_version(raw) == expected


def test_the_real_pin_is_readable() -> None:
    """The guard must not be green because it cannot find the real pin."""
    text = (REPO_ROOT / ".github/workflows/lint.yml").read_text(encoding="utf-8")
    assert workflow_pins(text), (
        "lint.yml names no `ruff==<version>` — then the version guard checks "
        "nothing when it matters."
    )
