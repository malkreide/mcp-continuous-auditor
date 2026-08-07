#!/usr/bin/env python3
"""Live-schedule probe — do the live tests RUN anywhere, or are they only marked?

THE DOCTRINE THIS CHECKS, AND THE HOLE IN IT
--------------------------------------------
The portfolio's test doctrine says: talk to the real upstream in tests marked
``@pytest.mark.live``, and keep them out of the pull-request run with
``pytest -m "not live"``. That half is right. A foreign API returning 503 must
not turn somebody's unrelated pull request red, because a suite that does that
gets switched off, and a switched-off suite catches nothing.

The exclusion then produces exactly the blindness the doctrine was written to
prevent. A test that runs nowhere is documentation, not a guard, and it rots
silently — nothing goes red when it breaks, because nothing runs it. These are
also the only tests in the repository that can contradict a wrong assumption
about the upstream: every other test asserts against a fixture that was written
from the same assumption.

``-m "not live"`` is not a place where tests run. It is the absence of one.

THE INCIDENTS
-------------
``meteoswiss-mcp`` (2026-07-30, the case behind catalogue item ``DRIFT-005``):
the first execution of the live suite in months put **three of six** tests on
the floor. They had not broken recently — the upstream endpoint had been
retired two days earlier, and before that nobody had started the suite either.
The marker was set correctly, the doctrine was followed, and two tools were
dead without anyone knowing.

``zh-education-mcp`` (2026-08-03) is the same mechanism reaching further. Four
of six datasets were read under field names the source had stopped using, so
eight tools answered every query with an empty result list and the sentence
«Schulgemeinde nicht gefunden» — a failure wearing the costume of an answer.
Every unit test stayed green, because the fixtures pin the old header. Only a
live run could have contradicted it, and no live run was scheduled.

A sweep of ten servers on 2026-08-03 found five with a scheduled live run
(``srgssr``, ``lindas``, ``termdat``, ``swisstopo``, ``parlament``) and five
without (``zh-education``, ``swiss-transport``, ``register``, ``fedlex``,
``swiss-snb``). ``DRIFT-005`` has been ``enforced`` in the catalogue the whole
time; nothing measured it. This script is what measures it.

WHAT IS MEASURED
----------------
One question, in three parts, all of it from the checkout and none of it from
the network:

1. Does the target HAVE live tests — a ``live`` marker actually applied to a
   test, not merely declared in the config?
2. Does a workflow under ``.github/workflows/`` run them ON A SCHEDULE — a
   ``schedule:`` trigger with a ``cron:`` entry, in a job whose pytest
   invocation SELECTS the live marker rather than excluding it?
3. Would a failure of that run be SEEN — a step or job that reacts to
   ``failure()``, or a known notifier action. A scheduled run whose red result
   only lands in the Actions tab is a more expensive way of not running: red
   crons stop being looked at in the second week.

THE MARKER EXPRESSION IS EVALUATED, NOT PATTERN-MATCHED
-------------------------------------------------------
``-m "not live"`` excludes; ``-m live`` selects; ``-m "live and not slow"``
selects; ``-m "not slow"`` selects live tests too, because a live test that is
not slow satisfies it. Grepping for the substring ``live`` gets the first two
right and the last two wrong in both directions.

So the expression is parsed with ``ast`` and asked one question: **is there any
assignment of the other markers under which a test carrying ``live`` is
selected?** That is satisfiability with ``live`` pinned to True, brute-forced
over the remaining names. Above ``_MAX_FREE_MARKERS`` names the answer is
``UNVERIFIED`` rather than a guess — an expression nobody can decide is not
evidence of a schedule and not evidence against one.

A pytest call with no ``-m`` at all admits live tests. That is not an oversight
in the target: ``pytest tests/`` runs the live suite, and the question here is
whether the live suite runs.

A TEST FILE RUN AS A SCRIPT IS RESOLVED, NOT DISMISSED
-------------------------------------------------------
``swiss-snb-mcp`` runs its live suite nightly with two steps that read
``python tests/test_live_scenarios.py`` — no pytest on the line at all. The
files carry ``pytestmark = pytest.mark.live`` and an ``if __name__ ==
"__main__":`` block that runs every scenario and exits non-zero on failure.

Read as «not a pytest call», that repository came back ``LIVE_UNSCHEDULED``: a
false finding against a server that runs its live tests every single night.

So ``python <file>.py`` is recorded and then resolved against the checkout,
which is the only place the answer is:

* the file carries the marker **and** has a ``__main__`` block ⇒ it runs, and
  the schedule counts;
* the file carries the marker and has **no** ``__main__`` block ⇒ a finding with
  its own sentence. Run as a script it imports and exits 0 without executing a
  single test — a green cron that answers the question falsely, which is worse
  than no cron at all;
* anything else ⇒ opaque, and therefore ``UNVERIFIED``.

WHAT A FINDING IS NOT ALLOWED TO CLAIM
--------------------------------------
The dangerous direction for this probe is a false ``LIVE_UNSCHEDULED``: the
workflow does run the suite, through a wrapper this file cannot read. So a
scheduled workflow whose test step is ``make live``, ``tox``, ``nox``, ``just``
or a shell script is recorded as OPAQUE, and an opaque command with no
recognised live run anywhere turns the verdict into ``UNVERIFIED`` — never into
a finding. The report names the command it could not read.

The same rule covers the escape hatch ``DRIFT-005`` allows: a server may be
covered by an external auditor running the live suite against it instead of
shipping its own cron. Where the documentation claims that, the claim is
reported as ``CLAIMS_EXTERNAL_COVERAGE`` and the run is NOT MEASURED. The claim
is in prose; whether the auditor actually runs is not in this checkout, and a
probe that reads «covered by mcp-continuous-auditor» as a pass has believed a
sentence instead of measuring a fact.

STATUSES
  LIVE_SCHEDULED           a cron job selects the live marker, and a failure is visible
  LIVE_UNSCHEDULED         FINDING — live tests exist, no scheduled run selects them
  LIVE_SCHEDULED_SILENT    FINDING — the scheduled run exists, its failure reaches nobody
  CLAIMS_EXTERNAL_COVERAGE NOT MEASURED — documented external coverage, unverifiable here
  NO_LIVE_TESTS            NOT MEASURED — nothing carries the marker; no suite to schedule
  UNVERIFIED               NOT MEASURED — no workflows, unreadable YAML, opaque command,
                           undecidable marker expression, or PyYAML not importable

Notes are printed and never decide the exit code: ``NO_MANUAL_TRIGGER`` (no
``workflow_dispatch`` beside the cron) and ``SPARSE_CADENCE`` (the cron fires
less often than weekly). Both are ``DRIFT-005`` pass criteria and neither is
worth a red gate on its own; a scheduled monthly run is a different animal from
no run at all, and collapsing the two would cost the finding its meaning.

EXIT CODES
  0    LIVE_SCHEDULED — the live suite runs, on a schedule, visibly
  2    FINDING — LIVE_UNSCHEDULED or LIVE_SCHEDULED_SILENT
  3    NOT MEASURED — NO_LIVE_TESTS, CLAIMS_EXTERNAL_COVERAGE, UNVERIFIED
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run (the target path does not exist)

Usage:
  python scripts/live_schedule_probe.py --target ../zh-education-mcp
  python scripts/live_schedule_probe.py --target . --format json
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

SCHEDULED = "LIVE_SCHEDULED"
UNSCHEDULED = "LIVE_UNSCHEDULED"
SILENT = "LIVE_SCHEDULED_SILENT"
EXTERNAL = "CLAIMS_EXTERNAL_COVERAGE"
NO_LIVE_TESTS = "NO_LIVE_TESTS"
UNVERIFIED = "UNVERIFIED"

NOTE_NO_DISPATCH = "NO_MANUAL_TRIGGER"
NOTE_SPARSE = "SPARSE_CADENCE"

_FINDINGS = frozenset({UNSCHEDULED, SILENT})
_NOT_MEASURED = frozenset({EXTERNAL, NO_LIVE_TESTS, UNVERIFIED})

# Beyond this many *other* marker names in one expression the satisfiability
# sweep stops being instant. It is not a correctness bound — it is the line at
# which the probe says so instead of thinking about it for a while.
_MAX_FREE_MARKERS = 12

# The marker name is a portfolio convention, not a pytest builtin, so it is
# spelled once here rather than inlined at six call sites.
LIVE_MARKER = "live"

# `@pytest.mark.live`, `@mark.live`, `pytestmark = pytest.mark.live`, and
# `pytest.param(..., marks=pytest.mark.live)` — the fallback for a file that
# does not parse. The parsing path below is the one that normally decides,
# because this pattern also matches the marker inside a docstring or a string
# literal, and this very file's test suite carries `@pytest.mark.live` in a
# fixture string. A probe that read that as a live suite would report its own
# repository as one that needs a scheduled live run.
_MARK_APPLIED = re.compile(rf"\bmark\.{LIVE_MARKER}\b")
# The declaration in the config — `markers = ["live: talks to the real API"]`.
# Its presence proves intent and nothing about execution, which is why it is
# read separately from the applications above.
_MARK_DECLARED = re.compile(rf"^\s*[\"']?{LIVE_MARKER}\s*:", re.M)

# A command that plausibly runs a test suite but hides which one. Matched
# against the first token of a run line, or the whole line for `make`-style
# targets. `pip install` is deliberately not in here: an opaque command must be
# one that could BE the test run, or every workflow is opaque and the probe
# never concludes anything.
_OPAQUE_COMMANDS = ("make", "tox", "nox", "just", "hatch", "pdm", "invoke")

# Actions whose whole purpose is to make a red run visible. The `if: failure()`
# check below catches the general case; these are the ones that carry their own
# condition and would otherwise read as silent.
_NOTIFIER_ACTIONS = (
    "actions/github-script",
    "peter-evans/create-issue-from-file",
    "jasonetco/create-an-issue",
    "slackapi/slack-github-action",
    "8398a7/action-slack",
    "ravsamhq/notify-slack-action",
    "appleboy/telegram-action",
    "dawidd6/action-send-mail",
    "rtcamp/action-slack-notify",
)

# How a checkout says «somebody else runs my live suite». Read as a CLAIM, not
# as coverage — see the module docstring.
_EXTERNAL_COVERAGE = re.compile(
    r"mcp-continuous-auditor|continuous auditor|externe[rn]? auditor|external auditor",
    re.I,
)

_DOC_FILES = ("README.md", "README.de.md", "CONTRIBUTING.md")


class MarkerExprError(Exception):
    """The ``-m`` expression could not be decided — never silently a pass."""


def admits_live(expr: str | None) -> bool:
    """Can a test carrying the ``live`` marker be selected by this ``-m``?

    ``None`` (no ``-m`` at all) admits: ``pytest tests/`` runs everything.

    Everything else is satisfiability with ``live`` pinned True over the other
    marker names, because the question is not what the expression says about
    ``live`` but whether any live test comes out of it. ``not slow`` mentions
    no marker this probe cares about and still selects the live suite.
    """
    if expr is None:
        return True
    expr = expr.strip()
    if not expr:
        return True
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise MarkerExprError(f"not a marker expression: {expr!r} ({exc.msg})") from exc

    names: list[str] = []

    def collect(node: ast.AST) -> None:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
            for value in node.values:
                collect(value)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            collect(node.operand)
        elif isinstance(node, ast.Name):
            if node.id not in names:
                names.append(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            pass
        else:
            raise MarkerExprError(
                f"unsupported construct in marker expression {expr!r}: "
                f"{type(node).__name__}"
            )

    collect(tree.body)

    free = [n for n in names if n != LIVE_MARKER]
    if len(free) > _MAX_FREE_MARKERS:
        raise MarkerExprError(
            f"{len(free)} free marker names in {expr!r} — over the "
            f"{_MAX_FREE_MARKERS} this probe decides by enumeration"
        )

    code = compile(tree, "<marker>", "eval")
    for combo in itertools.product((True, False), repeat=len(free)):
        env = dict(zip(free, combo, strict=True))
        env[LIVE_MARKER] = True
        if eval(code, {"__builtins__": {}}, env):  # noqa: S307 - AST validated above
            return True
    return False


@dataclass
class PytestCall:
    """One recognised pytest invocation inside a workflow step."""

    command: str
    marker_expr: str | None = None
    explicit_live_flag: bool = False
    admits: bool | None = None  # None = the expression could not be decided
    undecidable: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "marker_expr": self.marker_expr,
            "explicit_live_flag": self.explicit_live_flag,
            "admits_live": self.admits,
            "undecidable": self.undecidable,
        }


@dataclass
class ScriptCall:
    """A workflow step that runs a Python FILE, not pytest.

    ``python tests/test_live_scenarios.py``. Whether that executes any test
    depends on the file, so this is recorded and resolved later against the
    checkout rather than judged here.
    """

    path: str
    command: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "command": self.command}


def parse_pytest_calls(
    script: str,
) -> tuple[list[PytestCall], list[str], list[ScriptCall]]:
    """Every pytest invocation in a ``run:`` block, plus the opaque commands.

    Returns ``(calls, opaque)``. ``opaque`` names commands that could be a test
    run this file cannot read into — a wrapper, a Makefile target, a shell
    script. They never produce a finding; they produce ``UNVERIFIED``.
    """
    calls: list[PytestCall] = []
    opaque: list[str] = []
    scripts: list[ScriptCall] = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # `&&` and `;` chain independent commands in one line; each half may be
        # the test run. Splitting keeps `uv sync && pytest -m live` readable.
        for part in re.split(r"&&|\|\||;", line):
            part = part.strip()
            if not part:
                continue
            try:
                tokens = shlex.split(part)
            except ValueError:
                # An unbalanced quote — a heredoc body, a continued string, a
                # line of an inline Python block. Splitting on whitespace is
                # enough to answer the only two questions asked below, and it
                # keeps such a line from being called opaque merely because it
                # did not lex: a run step full of `echo "…` continuations would
                # otherwise bury every verdict under commands nobody claimed
                # were tests.
                tokens = part.split()
            if not tokens:
                continue
            call = _pytest_call(tokens, part)
            if call is not None:
                calls.append(call)
                continue
            module = _script_call(tokens, part)
            if module is not None:
                scripts.append(module)
            elif _is_opaque(tokens):
                opaque.append(part)
    return calls, opaque, scripts


def _pytest_call(tokens: list[str], original: str) -> PytestCall | None:
    """Recognise `pytest`, `python -m pytest`, `uv run pytest`, `poetry run pytest`.

    The token has to be the COMMAND, reached by walking the prefixes that can
    legitimately stand in front of it. Scanning the line for the first token
    that happens to read ``pytest`` makes ``pip install pytest`` a live run —
    a scheduled workflow that merely installs the tool would then satisfy the
    check it exists to enforce.
    """
    index = _command_index(tokens)
    if index is None:
        return None
    args = tokens[index + 1 :]

    call = PytestCall(command=original)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m" and i + 1 < len(args):
            call.marker_expr = args[i + 1]
            i += 2
            continue
        if arg.startswith("-m") and len(arg) > 2 and not arg.startswith("--"):
            call.marker_expr = arg[2:]
            i += 1
            continue
        if arg.startswith("--live") or arg.startswith("--run-live"):
            call.explicit_live_flag = True
        i += 1

    try:
        call.admits = admits_live(call.marker_expr)
    except MarkerExprError as exc:
        call.undecidable = str(exc)
    if call.explicit_live_flag and call.admits is not False:
        call.admits = True
    return call


# Everything that may stand between the start of a command and `pytest`
# without changing which program is being run.
_RUNNERS = ("uv", "uvx", "poetry", "pipenv", "hatch", "pdm", "rye", "nix-shell")
_RUNNER_VERBS = ("run", "exec")


def _command_index(tokens: list[str]) -> int | None:
    """Index of the ``pytest`` token when it is the command, else ``None``."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        base = token.rsplit("/", 1)[-1]
        if base == "pytest" or base.startswith("pytest-"):
            return i
        if "=" in token and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            i += 1  # a leading environment assignment: `LIVE=1 pytest ...`
            continue
        if base in ("sudo", "env", "xvfb-run", "time", "timeout"):
            i += 1
            continue
        if base in ("python", "python3", "py") or re.fullmatch(r"python3\.\d+", base):
            # Only `python -m pytest` is a pytest run; `python script.py` is not.
            if i + 2 < len(tokens) and tokens[i + 1] == "-m":
                i += 2
                continue
            return None
        if base in _RUNNERS:
            j = i + 1
            while j < len(tokens) and (
                tokens[j].startswith("-") or tokens[j] in _RUNNER_VERBS
            ):
                j += 1
            i = j
            continue
        return None
    return None


def _script_call(tokens: list[str], original: str) -> ScriptCall | None:
    """``python <file>.py`` — an interpreter invoked on a file, not on pytest.

    ``swiss-snb-mcp`` runs its live suite this way: two nightly steps calling
    ``python tests/test_live_scenarios.py``, where the file carries
    ``pytestmark = pytest.mark.live`` AND an ``if __name__ == "__main__":``
    block that runs every scenario and exits non-zero on failure. Read as
    «not a pytest call», that repository came back LIVE_UNSCHEDULED — a false
    finding against a server that runs its live tests every single night.

    Recording it here lets the verdict resolve the file against the checkout,
    which is the only place the answer actually is.
    """
    i = 0
    while i < len(tokens):
        token = tokens[i]
        base = token.rsplit("/", 1)[-1]
        if "=" in token and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            i += 1
            continue
        if base in ("sudo", "env", "xvfb-run", "time", "timeout"):
            i += 1
            continue
        if base in ("python", "python3", "py") or re.fullmatch(r"python3\.\d+", base):
            for arg in tokens[i + 1 :]:
                if arg.startswith("-"):
                    return None  # `-m`, `-c`: not a file invocation
                if arg.endswith(".py"):
                    return ScriptCall(path=arg, command=original)
                return None
            return None
        return None
    return None


def has_main_guard(text: str) -> bool:
    """Does this module execute anything when run as a script?

    The whole question for a ``python tests/test_x.py`` step. A live test file
    without the guard runs its imports and exits 0 — a scheduled job that
    reports success while executing no test, which is the DRIFT-005 failure
    one level further in and worth its own sentence.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and any(
                isinstance(c, ast.Constant) and c.value == "__main__"
                for c in test.comparators
            )
        ):
            return True
    return False


def _is_opaque(tokens: list[str]) -> bool:
    head = tokens[0].rsplit("/", 1)[-1]
    if head in _OPAQUE_COMMANDS:
        return True
    if head in ("bash", "sh", "zsh") and len(tokens) > 1:
        return True
    return tokens[0].endswith(".sh")


def fires_at_least_weekly(expr: str) -> bool | None:
    """Does this 5-field cron fire at least once every seven days?

    ``None`` when the expression is not the five fields cron takes. The reading
    that matters is cron's OR between day-of-month and day-of-week: with both
    restricted the job fires on either, so a restricted weekday alone already
    guarantees a weekly run.
    """
    fields = expr.split()
    if len(fields) != 5:
        return None
    _minute, _hour, dom, month, dow = fields
    if month != "*":
        step = _step_of(month)
        if step is None or step > 1:
            return False
    if dow != "*":
        return True  # any non-empty weekday set fires at least weekly
    if dom == "*":
        return True  # every day the month/weekday filters allow
    step = _step_of(dom)
    if step is not None:
        return step <= 7
    days = _listed_values(dom)
    # A day-of-month LIST can still be weekly (`1,8,15,22,29`), but only if no
    # gap in it — including the wrap into the next month — exceeds seven days.
    if days is None or not days:
        return None
    gaps = [b - a for a, b in itertools.pairwise(days)]
    gaps.append(days[0] + 28 - days[-1])  # the shortest month is the honest bound
    return max(gaps) <= 7


def _step_of(field_text: str) -> int | None:
    match = re.fullmatch(r"\*/(\d+)", field_text)
    return int(match.group(1)) if match else None


def _listed_values(field_text: str) -> list[int] | None:
    values: set[int] = set()
    for part in field_text.split(","):
        if part.isdigit():
            values.add(int(part))
            continue
        span = re.fullmatch(r"(\d+)-(\d+)", part)
        if span is None:
            return None
        values.update(range(int(span.group(1)), int(span.group(2)) + 1))
    return sorted(values)


@dataclass
class ScheduledRun:
    """A cron-triggered job that this probe read into."""

    workflow: str
    job: str
    crons: list[str]
    calls: list[PytestCall] = field(default_factory=list)
    opaque: list[str] = field(default_factory=list)
    scripts: list[ScriptCall] = field(default_factory=list)
    # Filled in by the verdict, which is where the checkout is available:
    # script calls that resolve to a live test file with a `__main__` block.
    resolved_scripts: list[str] = field(default_factory=list)
    # …and those that resolve to a live test file WITHOUT one.
    hollow_scripts: list[str] = field(default_factory=list)
    has_dispatch: bool = False
    failure_visible: bool = False
    visibility_evidence: str = ""

    @property
    def runs_live(self) -> bool:
        return any(c.admits is True for c in self.calls) or bool(self.resolved_scripts)

    @property
    def undecidable(self) -> list[str]:
        return [c.undecidable for c in self.calls if c.undecidable]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "job": self.job,
            "crons": self.crons,
            "calls": [c.as_dict() for c in self.calls],
            "scripts": [s.as_dict() for s in self.scripts],
            "resolved_scripts": self.resolved_scripts,
            "hollow_scripts": self.hollow_scripts,
            "opaque": self.opaque,
            "workflow_dispatch": self.has_dispatch,
            "failure_visible": self.failure_visible,
            "visibility_evidence": self.visibility_evidence,
        }


@dataclass
class Report:
    target: str
    status: str = UNVERIFIED
    reason: str = ""
    live_test_files: list[str] = field(default_factory=list)
    marker_declared_in: list[str] = field(default_factory=list)
    scheduled: list[ScheduledRun] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    external_claim: str = ""
    provenance: probe_provenance.Provenance | None = None

    @property
    def finding(self) -> bool:
        return self.status in _FINDINGS

    @property
    def measured(self) -> bool:
        return self.status not in _NOT_MEASURED

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "status": self.status,
            "reason": self.reason,
            "live_test_files": self.live_test_files,
            "marker_declared_in": self.marker_declared_in,
            "scheduled_runs": [s.as_dict() for s in self.scheduled],
            "unreadable": self.unreadable,
            "notes": self.notes,
            "external_claim": self.external_claim,
        }


def applies_live_marker(text: str) -> bool:
    """Does this module APPLY ``mark.live`` — in code, not in a string?

    Decided on the syntax tree: any ``<anything>.mark.live`` attribute access
    is the marker being applied, whether as a decorator, as ``pytestmark``, or
    inside ``pytest.param(..., marks=...)``. A string literal that contains the
    same characters is not, and the distinction is not academic: the test suite
    for this probe carries ``@pytest.mark.live`` inside a fixture string, and a
    textual match makes this repository look like one with an unscheduled live
    suite. A file that does not parse falls back to the pattern — reporting it
    is better than dropping it, and pytest would not run it either.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return bool(_MARK_APPLIED.search(text))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == LIVE_MARKER
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            return True
    return False


def find_live_tests(root: Path) -> tuple[list[str], list[str]]:
    """(files applying the marker, files declaring it).

    The two are kept apart because only the first is a suite. A ``markers =``
    line in ``pyproject.toml`` with no test behind it is a plan, and reporting a
    plan as a suite would make ``LIVE_UNSCHEDULED`` fire on repositories that
    have nothing to schedule.
    """
    applied: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in (".git", ".venv", "venv", "node_modules") for part in rel.parts):
            continue
        # Only test files. `mark.live` in a docstring under `src/` is prose;
        # counting it would invent a suite and then report it as unscheduled.
        if not (
            path.name.startswith("test_")
            or path.name.endswith("_test.py")
            or path.name == "conftest.py"
            or "tests" in rel.parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if applies_live_marker(text):
            applied.append(str(rel))

    declared: list[str] = []
    for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"):
        config = root / name
        if not config.exists():
            continue
        try:
            text = config.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _MARK_DECLARED.search(text):
            declared.append(name)
    return applied, declared


def _triggers(data: dict[str, Any]) -> dict[str, Any]:
    """The ``on:`` block.

    PyYAML follows YAML 1.1, where a bare ``on`` key is the boolean ``True``.
    Reading only the string key finds no triggers in any real workflow file and
    reports every repository as unscheduled — a false finding produced entirely
    inside the probe.
    """
    for key in ("on", True, "true", "On", "ON"):
        if key in data:
            block = data[key]
            if isinstance(block, dict):
                return block
            if isinstance(block, list):
                return dict.fromkeys(block, None)
            if isinstance(block, str):
                return {block: None}
    return {}


def _crons(triggers: dict[str, Any]) -> list[str]:
    schedule = triggers.get("schedule")
    if not isinstance(schedule, list):
        return []
    out: list[str] = []
    for entry in schedule:
        if isinstance(entry, dict) and isinstance(entry.get("cron"), str):
            out.append(entry["cron"].strip())
    return out


def _visible_on_failure(
    job: dict[str, Any], workflow: dict[str, Any]
) -> tuple[bool, str]:
    """Does a red run reach anybody outside the Actions tab?

    ``failure()`` / ``always()`` in a condition is the general shape. A separate
    job with ``needs:`` and such a condition counts too — that is how the
    notification is usually split out — so the whole workflow is considered,
    not just the job that ran the tests.
    """

    def _condition_hits(node: Any) -> str | None:
        if isinstance(node, dict):
            cond = node.get("if")
            if isinstance(cond, str) and re.search(r"\b(failure|always)\s*\(", cond):
                return cond.strip()
        return None

    for step in job.get("steps", []) or []:
        if isinstance(step, dict):
            hit = _condition_hits(step)
            if hit:
                return True, f"step condition `{hit}`"
            uses = step.get("uses")
            if isinstance(uses, str) and any(
                uses.lower().startswith(a) for a in _NOTIFIER_ACTIONS
            ):
                return True, f"notifier action `{uses}`"

    for name, other in (workflow.get("jobs") or {}).items():
        if not isinstance(other, dict):
            continue
        hit = _condition_hits(other)
        if hit:
            return True, f"job `{name}` condition `{hit}`"
    return False, ""


def read_workflows(root: Path) -> tuple[list[ScheduledRun], list[str]]:
    """Every cron-triggered job under ``.github/workflows/``, and what failed to read."""
    workflows_dir = root / ".github" / "workflows"
    unreadable: list[str] = []
    runs: list[ScheduledRun] = []
    if not workflows_dir.is_dir():
        return runs, unreadable

    try:
        import yaml  # noqa: PLC0415
    except ModuleNotFoundError:
        return runs, ["PyYAML is not importable — no workflow file was parsed"]

    paths = sorted(
        p
        for p in workflows_dir.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    for path in paths:
        rel = str(path.relative_to(root))
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            unreadable.append(f"{rel}: {exc}")
            continue
        if not isinstance(data, dict):
            unreadable.append(f"{rel}: not a mapping at the top level")
            continue
        triggers = _triggers(data)
        crons = _crons(triggers)
        if not crons:
            continue
        has_dispatch = "workflow_dispatch" in triggers
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            unreadable.append(f"{rel}: a scheduled workflow with no readable `jobs:`")
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            run = ScheduledRun(
                workflow=rel, job=str(job_name), crons=crons, has_dispatch=has_dispatch
            )
            for step in job.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                script = step.get("run")
                if not isinstance(script, str):
                    continue
                calls, opaque, scripts = parse_pytest_calls(script)
                run.calls.extend(calls)
                run.opaque.extend(opaque)
                run.scripts.extend(scripts)
            run.failure_visible, run.visibility_evidence = _visible_on_failure(
                job, data
            )
            runs.append(run)
    return runs, unreadable


def external_coverage_claim(root: Path) -> str:
    """A documented claim that somebody else runs the live suite. A claim, not coverage."""
    for name in _DOC_FILES:
        path = root / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _EXTERNAL_COVERAGE.search(line) and re.search(r"\blive\b", line, re.I):
                return f"{name}:{lineno}: {line.strip()}"
    return ""


def probe(target: Path) -> Report:
    report = Report(target=str(target))
    applied, declared = find_live_tests(target)
    report.live_test_files = applied
    report.marker_declared_in = declared

    if not applied:
        report.status = NO_LIVE_TESTS
        report.reason = (
            "no test applies the `live` marker"
            + (
                f" (declared in {', '.join(declared)}, applied nowhere)"
                if declared
                else ""
            )
            + " — there is no live suite to schedule, and this probe measured nothing"
            " about whether one is needed"
        )
        return report

    runs, unreadable = read_workflows(target)
    report.scheduled = runs
    report.unreadable = unreadable

    if not (target / ".github" / "workflows").is_dir():
        report.status = UNVERIFIED
        report.reason = (
            f"{len(applied)} file(s) carry the `live` marker, but the target has no "
            ".github/workflows/ — this probe reads GitHub Actions and cannot see a "
            "CI system it was not shown"
        )
        return report

    # Resolve every `python <file>.py` step against the checkout. This is the
    # step that turns «not a pytest call» into an answer instead of a guess.
    applied_set = set(applied)
    for run in runs:
        for call in run.scripts:
            rel = call.path.lstrip("./")
            if rel not in applied_set:
                # Some other script. What it runs is not readable from here,
                # so it is opaque — never evidence of absence.
                run.opaque.append(call.command)
                continue
            try:
                text = (target / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                run.opaque.append(call.command)
                continue
            if has_main_guard(text):
                run.resolved_scripts.append(rel)
            else:
                run.hollow_scripts.append(rel)

    live_runs = [r for r in runs if r.runs_live]

    hollow = [(r.workflow, f) for r in runs for f in r.hollow_scripts]
    if hollow and not live_runs:
        workflow, rel = hollow[0]
        report.status = UNSCHEDULED
        report.reason = (
            f"{workflow} runs `python {rel}` on a schedule, but that file carries the "
            '`live` marker and has no `if __name__ == "__main__":` block — run as a '
            "script it imports and exits 0 without executing a single test. A green "
            "cron that runs nothing is worse than no cron: it answers the question"
        )
        return report

    if live_runs:
        visible = [r for r in live_runs if r.failure_visible]
        for run in live_runs:
            if not run.has_dispatch:
                report.notes.append(
                    f"{NOTE_NO_DISPATCH}: {run.workflow} → {run.job} has a cron but no "
                    "`workflow_dispatch` — the suite cannot be started by hand after an "
                    "upstream hint"
                )
            for cron in run.crons:
                weekly = fires_at_least_weekly(cron)
                if weekly is False:
                    report.notes.append(
                        f"{NOTE_SPARSE}: {run.workflow} → `{cron}` fires less often "
                        "than weekly"
                    )
                elif weekly is None:
                    report.notes.append(
                        f"{NOTE_SPARSE}: {run.workflow} → `{cron}` is not five cron "
                        "fields; its cadence was not determined"
                    )
        if visible:
            report.status = SCHEDULED
            first = visible[0]
            report.reason = (
                f"{first.workflow} → {first.job} runs the live suite on "
                f"{', '.join(f'`{c}`' for c in first.crons)}; a failure is visible via "
                f"{first.visibility_evidence}"
            )
        else:
            report.status = SILENT
            first = live_runs[0]
            report.reason = (
                f"{first.workflow} → {first.job} runs the live suite on "
                f"{', '.join(f'`{c}`' for c in first.crons)}, but no step or job reacts "
                "to `failure()` and no notifier action is used — a red cron reaches "
                "nobody, and stops being looked at in the second week"
            )
        return report

    # Nothing recognised as a live run on a schedule. Before that becomes a
    # finding, every reason the probe might simply not have SEEN one is spent.
    undecidable = [u for r in runs for u in r.undecidable]
    opaque = [(r.workflow, cmd) for r in runs for cmd in r.opaque]
    if undecidable:
        report.status = UNVERIFIED
        report.reason = (
            "a scheduled workflow runs pytest under a marker expression this probe "
            f"does not decide: {undecidable[0]}"
        )
        return report
    if opaque:
        workflow, cmd = opaque[0]
        report.status = UNVERIFIED
        report.reason = (
            f"a scheduled workflow ({workflow}) runs `{cmd}` — a wrapper this probe "
            "cannot read into. Whether it runs the live suite was not measured"
        )
        return report
    if unreadable:
        report.status = UNVERIFIED
        report.reason = (
            f"{len(unreadable)} workflow file(s) could not be read: {unreadable[0]}"
        )
        return report

    claim = external_coverage_claim(target)
    if claim:
        report.external_claim = claim
        report.status = EXTERNAL
        report.reason = (
            "no scheduled workflow runs the live suite, and the documentation claims "
            f"external coverage — {claim}. Whether the auditor actually runs it is not "
            "in this checkout; the claim is recorded, not believed"
        )
        return report

    report.status = UNSCHEDULED
    where = ", ".join(applied[:3]) + (
        f", +{len(applied) - 3} more" if len(applied) > 3 else ""
    )
    report.reason = (
        f"{len(applied)} file(s) carry the `live` marker ({where}) and no scheduled "
        f"workflow selects it. {len(runs)} cron-triggered job(s) were read. "
        '`-m "not live"` is not a place where tests run'
    )
    return report


def render(report: Report) -> str:
    head = [report.provenance.render()] if report.provenance is not None else []
    if report.provenance is not None and report.provenance.blocking:
        return "\n".join([*head, report.provenance.moved_detail()])

    out = [f"{report.status:<24} {report.reason}"]
    for run in report.scheduled:
        marks = ", ".join(
            [f"-m {c.marker_expr!r}" if c.marker_expr else "no -m" for c in run.calls]
            + [f"python {s} (runs as a script)" for s in run.resolved_scripts]
            + [f"python {s} (NO __main__ block)" for s in run.hollow_scripts]
        )
        out.append(
            f"  cron  {run.workflow} → {run.job}: "
            f"{', '.join(run.crons)}"
            + (f" [{marks}]" if marks else " [no pytest call read]")
            + (f" [opaque: {'; '.join(run.opaque)}]" if run.opaque else "")
        )
    for line in report.unreadable:
        out.append(f"  UNREAD {line}")
    for note in report.notes:
        out.append(f"  note   {note}")
    return "\n".join([*head, *out])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="live_schedule_probe")
    ap.add_argument("--target", default=".", help="path to the MCP server repo")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"{target}: not a directory", file=sys.stderr)
        return 127

    prov = probe_provenance.capture(target)
    report = probe(target)
    report.provenance = prov.recheck()

    if args.format == "json":
        payload = report.as_dict()
        payload["provenance"] = prov.as_dict()
        # A run that read two different trees reports no verdict, so `finding`
        # would be a claim it is not entitled to make.
        payload["finding"] = None if prov.blocking else report.finding
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render(report))

    if prov.blocking:
        return probe_provenance.EXIT_MOVED
    if report.status in _FINDINGS:
        return 2
    if report.status in _NOT_MEASURED:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
