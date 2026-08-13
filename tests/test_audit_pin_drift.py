#!/usr/bin/env python3
"""The pure half of `scripts/audit_pin_drift.py`.

The network half is not mocked, and that is the same decision
`test_quality_chain_table.py` records for its own subject: a mock of GitHub's
answer would only restate this repository's assumption about that answer, and
could never contradict it. What can be tested here is everything the script
decides ONCE IT HAS an answer — including the three states it must keep apart:

    tag exists + latest matches  -> in sync
    tag does not exist           -> BROKEN, and broken NOW
    could not be measured        -> UNKNOWN, and explicitly not a pass

That third one is the whole point. `None` folded into "differs" would make an
unreachable API indistinguishable from a stale pin, and the fix for those two
is not the same.

`read_pin` is tested against the real file as well as against synthetic ones:
the real one proves the parser matches the thing it parses today, the
synthetic ones prove it fails loudly on the shapes that would otherwise pass
silently.

Stdlib-only, no network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_pin_drift import (  # noqa: E402
    PIN_FILE,
    compare,
    read_pin,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# read_pin() — the anchor
# --------------------------------------------------------------------------


def test_the_real_pin_file_is_readable():
    """Against the file as it actually is — not a fixture of it.

    A parser proven only against synthetic input is proven against its own
    author's idea of the format.
    """
    pin = read_pin()
    assert pin.startswith("v"), f"pin does not look like a tag: {pin!r}"


def test_the_pin_matches_the_one_the_documentation_uses():
    """The connection to the other guard, spelled out.

    Without it, `PIN` could be renamed in the test module and this script
    would keep reading some other constant that happens to still be there.
    """
    pin = read_pin()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-audit-skill/tree/{pin}" in readme, (
        f"README.md carries no link pinned to {pin} — the pin this script "
        "reads and the pin the documentation uses have come apart."
    )


def _pin_file(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "pinfile.py"
    p.write_text(body, encoding="utf-8")
    return p


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(LookupError, match="missing"):
        read_pin(tmp_path / "nope.py")


def test_a_missing_constant_is_an_error(tmp_path):
    """The anchor case: renamed away, and nothing computes a default."""
    path = _pin_file(tmp_path, 'OTHER = "v1.2.3"\n')
    with pytest.raises(LookupError, match=r"no module-level"):
        read_pin(path)


def test_the_constant_inside_a_docstring_does_not_count(tmp_path):
    """Why this parses instead of greps.

    The real file mentions `PIN` in prose and in an error message. A regex
    would happily read one of those.
    """
    path = _pin_file(
        tmp_path,
        '"""A module that talks about PIN = "v9.9.9" without setting it."""\n'
        '# PIN = "v8.8.8"\n'
        'PIN = "v3.0.0"\n',
    )
    assert read_pin(path) == "v3.0.0"


def test_a_computed_pin_is_refused(tmp_path):
    path = _pin_file(tmp_path, 'MAJOR = 3\nPIN = f"v{MAJOR}.0.0"\n')
    with pytest.raises(LookupError, match="not a plain string literal"):
        read_pin(path)


def test_an_annotated_assignment_is_read(tmp_path):
    path = _pin_file(tmp_path, 'PIN: str = "v4.1.0"\n')
    assert read_pin(path) == "v4.1.0"


# --------------------------------------------------------------------------
# compare() — the three states
# --------------------------------------------------------------------------


def test_in_sync():
    ok, message = compare("v3.0.0", "v3.0.0", True)
    assert ok
    assert "In sync" in message


def test_drift_names_both_versions():
    ok, message = compare("v3.0.0", "v3.2.0", True)
    assert not ok
    assert "v3.0.0" in message and "v3.2.0" in message
    # The message must say what it is NOT: a blocker.
    assert "block" in message


def test_a_pin_that_points_at_nothing_is_reported_as_broken():
    ok, message = compare("v9.9.9", "v3.0.0", False)
    assert not ok
    assert message.startswith("BROKEN:")


def test_broken_outranks_drift():
    """A tag that does not exist is a fact; being behind is a decision.

    Reported as drift, somebody raises the pin to the latest release and the
    broken link is gone by accident rather than by diagnosis — and the same
    typo in the next pin repeats it.
    """
    _, message = compare("v9.9.9", "v3.0.0", False)
    assert "DRIFT" not in message


@pytest.mark.parametrize(
    ("latest", "exists"),
    [(None, True), ("v3.0.0", None), (None, None)],
)
def test_unmeasured_is_unknown_and_never_a_pass(latest, exists):
    ok, message = compare("v3.0.0", latest, exists)
    assert not ok
    assert message.startswith("UNKNOWN:")
    assert "NOT a pass" in message


def test_unknown_names_what_was_not_measured():
    _, message = compare("v3.0.0", None, True)
    assert "the latest release" in message
    _, message = compare("v3.0.0", "v3.0.0", None)
    assert "whether the pinned tag exists" in message


# --------------------------------------------------------------------------
# The script as a whole
# --------------------------------------------------------------------------


def test_ANKER_an_unreadable_pin_exits_two_without_touching_the_network():
    """Usage error and finding are different exits, and the first comes first.

    Running with a pin file that cannot be read must fail on THAT, not on a
    network call it should never have reached.
    """
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pathlib;"
            "sys.path.insert(0, str(pathlib.Path('.').resolve()));"
            "import scripts.audit_pin_drift as m;"
            "m.PIN_FILE = pathlib.Path('does-not-exist.py');"
            "sys.exit(m.main([]))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2, done.stderr
    assert "has no home" in done.stderr


def test_the_pin_file_constant_points_at_the_real_module():
    assert PIN_FILE.is_file(), f"{PIN_FILE} is missing"
    assert PIN_FILE.name == "test_quality_chain_table.py"


# --------------------------------------------------------------------------
# The summary step of `audit-pin-drift.yml`
# --------------------------------------------------------------------------
#
# BUILT IN FROM THE START, because the same construction was measured failing
# next door: `quality-chain.yml` in `mcp-audit-skill` read a report schema that
# had moved on, and its summary step died on a `KeyError` before writing a
# line — every run, for a week, while the guard itself worked fine.
#
# The Python of a workflow lives in a heredoc that nobody but the runner ever
# executes. So this pulls it out of the YAML and runs it against a report
# `main()` really produced, rather than one hand-built from an assumption about
# the schema.

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "audit-pin-drift.yml"


def _summary_script() -> str:
    """The Python out of the heredoc.

    The heredoc word `PY` is the anchor. If it disappears — somebody moves the
    step to a file — that is an ERROR, not a silent skip: this test would
    otherwise have nothing left to check.
    """
    import re
    import textwrap

    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"python - <<'PY'[^\n]*\n(.*?)\n[ \t]*PY\n", text, re.S)
    if match is None:
        raise AssertionError(
            f"{WORKFLOW.name}: no `python - <<'PY' … PY` heredoc found — the "
            "anchor is gone, and this test checked nothing."
        )
    return textwrap.dedent(match.group(1))


def _report(monkeypatch, *, latest, exists) -> str:
    """A real report out of `main()`, not one written by hand here.

    Hand-written it would carry this test's assumption about the schema, and
    the schema is precisely what is in question.
    """
    import io

    import scripts.audit_pin_drift as m

    monkeypatch.setattr(m, "fetch_latest", lambda *a, **k: (latest, "ok"))
    monkeypatch.setattr(m, "fetch_tag_exists", lambda *a, **k: (exists, "ok"))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = m.main(["--format", "json"])
    monkeypatch.undo()
    assert code in (0, 1), f"unexpected exit {code}"
    return buf.getvalue()


def _run_summary(tmp_path: Path, raw: str) -> str:
    (tmp_path / "pin.json").write_text(raw, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-c", _summary_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (
        "the summary step died — exactly the failure that ran unnoticed for a "
        f"week in mcp-audit-skill:\n{done.stderr}"
    )
    return done.stdout


def test_ANKER_the_in_sync_report_renders(tmp_path, monkeypatch):
    pin = read_pin()
    out = _run_summary(tmp_path, _report(monkeypatch, latest=pin, exists=True))
    assert "Audit pin" in out
    assert "action needed" not in out
    assert pin in out


def test_ANKER_the_drift_report_renders(tmp_path, monkeypatch):
    out = _run_summary(tmp_path, _report(monkeypatch, latest="v99.0.0", exists=True))
    assert "action needed" in out
    assert "v99.0.0" in out
    # The three steps of raising the pin must be in the summary — that is the
    # part somebody acts on.
    assert "PIN" in out and "reading what changed" in out


def test_ANKER_the_unknown_report_renders(tmp_path, monkeypatch):
    """Not measured must survive rendering too — it is the state that hides."""
    out = _run_summary(tmp_path, _report(monkeypatch, latest=None, exists=None))
    assert "action needed" in out
    assert "not measured" in out


def test_a_missing_result_does_not_kill_the_summary(tmp_path):
    """If the step before died, this one says WHY nothing is there —
    it does not write a second error over the first."""
    out = _run_summary(tmp_path, "")
    assert "No result" in out
