#!/usr/bin/env python3
"""Published probe — what does the package on PyPI actually send, import and start?

``identity_probe.py`` reads a repository. This one reads the artifact: it
installs the distribution from the index into a throwaway venv and measures what
the installed code would do — the User-Agent it puts on the wire, whether it
imports at all, whether its console script comes up, and whether the version
ranges it declares will still resolve to something that imports tomorrow.

Those are different questions from anything a source tree can answer, and the gap
between them is where this class of bug lives — a source tree can be perfectly
clean for weeks while every user who runs ``pip install`` still gets the old,
wrong identity, because the fix was merged and never released.

That is not hypothetical. A portfolio sweep on 2026-07-30 measured 33 published
packages: 16 sent a version that disagreed with the version they were installed
as. All 16 had the fix merged. None had it released. A 17th sent a spoofed
browser User-Agent, which is a different finding and is reported as one.

WHY THIS DOES NOT GREP
----------------------
Three detection strategies were tried against the same 33 packages. Each one
reported a clean result for packages that were in fact drifting, and each one
was blind in a *different* place:

1. **Regex for ``f"…{__version__}…"``** missed ``lobbywatch-mcp``, which spells
   the variable ``PACKAGE_VERSION``. A pattern only ever knows the spellings its
   author thought of.
2. **Reading the module namespace at runtime** missed ``seco-labor-mcp``, whose
   User-Agent sits nested in ``_HTTP_KWARGS["headers"]["User-Agent"]``, and
   ``swiss-transport-mcp``, which passes the literal inline to the ``httpx``
   constructor *inside a function*, so it exists in no module attribute at all.
3. **Scanning source for literals** misses every f-string User-Agent, because
   there is no digit after the slash to anchor on.

So all three run, and each finding records which one produced it (``evidence``).
Neither alone covers the portfolio.

THE PART THAT MATTERS MOST
--------------------------
A probe that cannot find a User-Agent must not report that there is none. Those
are different claims: "this server sends no custom UA" is a finding, "I did not
recognise the shape" is a failure of the probe. Conflating them is how the first
version of this check pronounced 24 packages unremarkable, 16 of which were
drifting.

Hence ``unverified``: when the installed source mentions a User-Agent but no
strategy could resolve a value, that is reported as such and exits non-zero.
Silence is never taken for a clean bill of health.

IMPORT ERRORS DECIDE THE STATUS — AND ARE MEASURED TWICE
--------------------------------------------------------
Every import failure used to land in ``import_errors`` and change nothing: a
package that could not be imported at all could still be reported
``no_user_agent``, which is a statement about code this probe never executed.
An import error is now a finding of its own (``broken_import``).

The catch is that a naive version of that check is wrong, and the portfolio
already proved it. ``bag-health-mcp`` was reported as having a circular import.
It has none: ``import bag_health_mcp.server`` runs cleanly in a fresh venv. What
fails is importing the private submodule *as the very first import of the
process*, before its own package root has initialised — an artefact of the order
this probe happened to walk the modules in, not a defect in the package.

So the rule this probe follows is the one that evidence supports:

  **import the package ROOT first, then the submodules — and whatever still
  fails after that is real.**

Every failure the bulk scan records is then re-measured in two FRESH
interpreters, because only a fresh process can tell the two apart:

  ``cold``  ``import pkg.sub`` as the very first import of the process
  ``warm``  ``import pkg`` first, then ``import pkg.sub``

``warm`` failing is a real broken import — it is what a user gets. ``cold``
failing while ``warm`` succeeds is this probe's own import order and is reported
as ``import-order artefact``, never as a finding. A verification that could not
be run at all is treated as real: absence of proof is not a pass.

START IS NOT IMPORT
-------------------
A package can import perfectly and still not start. The smoke stage therefore
runs the installed CONSOLE SCRIPT — the thing a user actually types — with stdin
closed, for a few seconds, and expects two things: a ``server.start`` event, and
no crash. Neither alone is enough. A clean exit with nothing announced is
``smoke_unverified`` rather than a pass, for the same reason ``unverified``
exists above: this probe did not see the server reach serving, and not seeing it
is not evidence that it did.

(stdin closed is deliberate and is what a stdio server is supposed to survive:
it reads EOF and shuts down cleanly. The exit code is therefore not the signal —
the announcement before it is. Contrast ``transport_boot_probe.py``, which HOLDS
stdin open because it is mid-conversation; here there is no conversation.)

UPPER BOUNDS ARE PART OF THE PUBLISHED METADATA
-----------------------------------------------
``swiss-energy-mcp`` 0.3.3 shipped ``mcp[cli]>=1.20.0`` with no upper bound. The
day ``mcp`` 2.0.0 was published, every fresh install of that release died on
import — the artifact did not change, the resolver's answer did. Nothing in the
repository was wrong on the day it was written; what went wrong was published
metadata meeting a new major.

So ``requires_dist`` of the installed artifact is read, and a missing upper bound
is reported for the dependencies that are IMPORT-CRITICAL — measured, not
guessed: the ones whose modules actually appear in ``sys.modules`` after
importing the package. Two tiers, because they are two different days:

  ``UNCAPPED_MAJOR_AVAILABLE``  the index ALREADY serves a higher major than the
                               declared floor. The next fresh install can pick it
                               up. This is a finding, and it is the one that
                               arrives before the break rather than after it.
  ``UNCAPPED``                  no higher major is published yet. The trap is
                               armed and has not sprung; reported, not a finding.

COVERAGE IS PART OF THE RESULT
------------------------------
Everything above is about not mistaking "I did not see it" for "it is not
there". One level up, the same mistake is available to whoever assembles the
argument list. On 2026-07-31 a portfolio-wide run of this script reported "33 of
33 ok". Both numbers were true and the set was wrong: ``portfolio.json`` listed
43 active servers, and ten had never been on the command line — seven of them
``core``, including ``meteoswiss-mcp``, whose broken release is the incident
``shipped_probe.py`` was written for.

A hand-assembled argument list is a hand-maintained value, and it drifts for the
reason every hand-maintained value in this portfolio has drifted: nothing
downstream disagrees with it.

So ``--manifest`` takes the target list from the portfolio's own source of truth
(``coverage_manifest.py --format json`` in ``swiss-public-data-mcp``), every such
run ends with a coverage line, and an incomplete run exits non-zero unless each
omission was named together with a reason.

Exit codes:
  0  every resolved User-Agent matches the installed version (or the package sets
     none), everything imports, the entrypoint announced its start, and no
     import-critical range is already open across a published major
  1  drift, a foreign User-Agent, a broken import, a failed or unconfirmed start,
     an open range across a published major, a User-Agent that could not be
     resolved (``unverified`` — which is not a pass), or incomplete coverage
  2  the distribution could not be installed, or ``--version`` was pinned and the
     venv came back holding something else

Usage:
  python scripts/published_probe.py lobbywatch-mcp
  python scripts/published_probe.py --format json bakom-mcp srgssr-mcp
  python scripts/published_probe.py --constraint 'mcp<2' swiss-statistics-mcp
  python scripts/published_probe.py --version 0.3.4 swiss-energy-mcp   # after a release
  python scripts/published_probe.py --manifest manifest.json           # whole portfolio
  python scripts/published_probe.py --manifest manifest.json \
      --allow-skip meteoswiss-mcp:"upstream down, ticket #12"
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# `Requires-Dist` parsing, PEP 440 bound semantics and the index lookup already
# exist in the yank gate and are exactly the semantics needed here — `~=2.1` and
# `==2.*` are upper bounds even though neither spells `<`, and a second
# implementation of that would be a second place for it to be subtly wrong.
import yank_probe as yp

DEFAULT_INDEX = "https://pypi.org/simple"
DEFAULT_START_EVENT = "server.start"
DEFAULT_SMOKE_SECONDS = 6.0

# Runs inside the target venv: import the package ROOT first, then every
# submodule, and collect any string that looks like a product token followed by
# a version, wherever it sits. The order is load-bearing — see the module
# docstring on `bag-health-mcp`.
RUNTIME_SCAN = r"""
import importlib, importlib.metadata as md, json, pkgutil, re, sys, warnings
warnings.filterwarnings("ignore")

dist = sys.argv[1]
try:
    installed = md.version(dist)
except md.PackageNotFoundError:
    print(json.dumps({"error": "not_installed"})); raise SystemExit

tops = set()
for f in md.files(dist) or []:
    parts = f.parts
    if len(parts) > 1 and not parts[0].endswith((".dist-info", ".data")) and parts[0] != "..":
        tops.add(parts[0])

UA = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*/\d[^\s]*")
found, errors = [], []
# Everything already imported before we touch the target, so the dependency
# accounting below reports what the TARGET pulled in and not what CPython
# starts up with.
baseline = set(sys.modules)

def walk(val, path, out, depth=0, seen=None):
    # Containers must be entered. seco-labor-mcp keeps its User-Agent in
    # _HTTP_KWARGS["headers"]["User-Agent"]; a scan over vars(module) alone
    # sees nothing there and would wrongly report no User-Agent at all.
    if depth > 6:
        return
    seen = set() if seen is None else seen
    if isinstance(val, str):
        if UA.match(val):
            out.append((path, val))
    elif isinstance(val, dict):
        if id(val) in seen: return
        seen.add(id(val))
        for k, v in val.items():
            walk(v, "%s[%r]" % (path, k), out, depth + 1, seen)
    elif isinstance(val, (list, tuple, set, frozenset)):
        if id(val) in seen: return
        seen.add(id(val))
        for i, v in enumerate(val):
            walk(v, "%s[%d]" % (path, i), out, depth + 1, seen)
    elif isinstance(val, type):
        if id(val) in seen: return
        seen.add(id(val))
        for k, v in vars(val).items():
            if not k.startswith("__"):
                walk(v, "%s.%s" % (path, k), out, depth + 1, seen)

def scan(mod):
    for name, val in list(vars(mod).items()):
        if name.startswith("__"):
            continue
        hits = []
        try:
            walk(val, name, hits)
        except Exception:
            continue
        for path, value in hits:
            found.append({"module": mod.__name__, "attr": path, "value": value})

def note(module, top, kind, exc):
    errors.append({"module": module, "root": top, "kind": kind,
                   "error": "%s: %s" % (type(exc).__name__, exc)})

for top in sorted(tops):
    # STAGE 1 — the package root, always before any of its submodules. A
    # submodule imported first has to initialise its own parent on the way in,
    # and a package whose root imports the submodule back then looks circular
    # when nothing about it is.
    try:
        pkg = importlib.import_module(top)
    except Exception as e:
        note(top, top, "root", e)
        continue
    scan(pkg)
    # STAGE 2 — the submodules, parents before children (walk_packages yields
    # them in that order), with the root already in sys.modules.
    for _, modname, _ in pkgutil.walk_packages(getattr(pkg, "__path__", []), top + "."):
        if modname.rsplit(".", 1)[-1] == "__main__":
            continue                      # importing it would start the server
        try:
            scan(importlib.import_module(modname))
        except Exception as e:
            note(modname, top, "submodule", e)

# Which DISTRIBUTIONS the target actually pulled in, measured from sys.modules
# rather than assumed from the requirement list. A dependency that is declared
# and never imported cannot break an import, and saying so about it would be a
# finding about nothing.
imported = set()
try:
    mapping = md.packages_distributions()
except Exception:
    mapping = {}
for name in set(sys.modules) - baseline:
    for owner in mapping.get(name.split(".")[0], []):
        imported.add(re.sub(r"[-_.]+", "-", str(owner)).lower())

try:
    requires = list(md.requires(dist) or [])
except Exception:
    requires = []

seen_v, uniq = set(), []
for f in found:
    if f["value"] not in seen_v:
        seen_v.add(f["value"]); uniq.append(f)
print(json.dumps({"installed": installed, "runtime": uniq, "errors": errors[:24],
                  "tops": sorted(tops), "imported_dists": sorted(imported),
                  "requires": requires}))
"""

# Runs inside the target venv, in a FRESH interpreter, to tell a real broken
# import apart from this probe's own import order. `cold` is the very first
# import of the process; `warm` imports the package root first, which is what
# any real user's code does.
VERIFY_IMPORT = r"""
import importlib, json, sys, warnings
warnings.filterwarnings("ignore")
mode, root, module = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    if mode == "warm" and root != module:
        importlib.import_module(root)
    importlib.import_module(module)
except BaseException as e:
    print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}))
else:
    print(json.dumps({"ok": True, "error": ""}))
"""

# Runs inside the target venv: resolve one f-string's interpolated expression
# against the module that defines it. Measured, not inferred.
EVAL_EXPR = r"""
import importlib, json, sys, warnings
warnings.filterwarnings("ignore")
mod, expr = sys.argv[1], sys.argv[2]
try:
    print(json.dumps({"value": str(eval(expr, vars(importlib.import_module(mod))))}))
except Exception as e:
    print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
"""

# Runs inside the target venv: the console scripts the distribution installed.
CONSOLE_SCRIPTS = r"""
import json, sys
from importlib import metadata as md
try:
    eps = [ep.name for ep in md.distribution(sys.argv[1]).entry_points
           if ep.group == "console_scripts"]
except Exception as e:
    print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)})); raise SystemExit
print(json.dumps({"scripts": eps}))
"""

# f"token/{expr}" — the inline form that carries no literal digit and is
# therefore invisible to a literal scan.
FSTRING = re.compile(r'f["\']([A-Za-z][A-Za-z0-9_.-]*)/\{([A-Za-z_][A-Za-z0-9_.]*)\}')
# "token/1.2.3" — a hand-maintained literal.
LITERAL = re.compile(r'["\']([A-Za-z][A-Za-z0-9_.+-]*/\d[^"\']*)["\']')
MENTIONS_UA = re.compile(r"user.?agent", re.I)

TRACEBACK = "Traceback (most recent call last)"


def code_only(text: str) -> str:
    """The source with comments blanked out.

    `bakom-mcp` 2.0.4 sends the correct `bakom-mcp/2.0.4` and was reported as
    DRIFT anyway, because `__init__.py` carries a comment recording the old
    incident:

        # in server.py carried "bakom-mcp/1.0" to the BAKOM endpoints ...

    A probe that goes red on a comment documenting the very bug it exists to
    catch teaches people to delete the documentation. `identity_probe.py`
    learned this and strips comments; this probe was written without that
    lesson and had to relearn it against a real package.

    `tokenize`, not `split("#")` — a `#` inside a string literal must not
    truncate the line. Unparseable source is returned whole rather than
    skipped: checking it noisily beats not checking it.
    """
    lines = text.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                lines[row - 1] = lines[row - 1][:col]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    return "\n".join(lines)


@dataclass
class Finding:
    value: str
    sent_version: str | None
    evidence: str
    where: str
    own: bool = True

    _ok: bool = field(default=False, repr=False)


@dataclass
class ImportCheck:
    """One import failure the bulk scan saw, re-measured in fresh interpreters.

    ``verdict`` is the whole point of this dataclass:

      ``real``            the warm import failed too — a user gets this
      ``order-artifact``  the cold import failed and the warm one succeeded:
                          the package is fine and the ORDER produced the error
      ``not-reproduced``  both fresh imports succeeded, so the failure came from
                          state the bulk scan had accumulated, not from the
                          module. Also not a finding, and named apart from the
                          case above so the report does not claim to know it was
                          the order when it does not
      ``optional-dep``    the module is missing something the distribution
                          declares only behind an EXTRA or a marker. A shipped
                          test module importing `pytest` is not the server being
                          broken, and a gate that says it is gets muted
      ``unconfirmed``     the re-measurement itself could not run. Counted as
                          real, because an unverified failure is not a pass.

    Note what is deliberately NOT excused: a module missing something the
    distribution declares NOWHERE. Measured against `cowsay` 6.1, whose shipped
    `cowsay.tests.*` import `pytest` with no dependency declaring it — that
    genuinely does not import for anyone who ran `pip install cowsay`, and the
    only reason it hurts nobody is that nobody imports it. It stays real.
    """

    module: str
    root: str
    kind: str  # root | submodule
    error: str  # what the bulk scan saw
    verdict: str = "unconfirmed"
    cold_error: str = ""
    warm_error: str = ""

    @property
    def real(self) -> bool:
        return self.verdict in ("real", "unconfirmed")

    @property
    def excused(self) -> bool:
        return self.verdict in ("order-artifact", "not-reproduced", "optional-dep")

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "root": self.root,
            "kind": self.kind,
            "error": self.error,
            "verdict": self.verdict,
            "cold_error": self.cold_error,
            "warm_error": self.warm_error,
        }


@dataclass
class Smoke:
    """What happened when the installed console script was started."""

    status: str = "skipped"  # ok | crashed | no_event | no_entrypoint | error | skipped
    entrypoint: str = ""
    detail: str = ""
    exit_code: int | None = None
    evidence: str = ""  # the line that carried the start event, if any

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "entrypoint": self.entrypoint,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "evidence": self.evidence,
        }


@dataclass
class Cap:
    """One import-critical dependency's upper bound, or the absence of one."""

    name: str
    requirement: str
    floor: str | None = None
    verdict: str = "capped"  # capped | major-available | armed | unknown
    detail: str = ""

    @property
    def finding(self) -> bool:
        return self.verdict == "major-available"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requirement": self.requirement,
            "floor": self.floor,
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass
class Result:
    dist: str
    installed: str | None = None
    status: str = "unverified"
    findings: list[Finding] = field(default_factory=list)
    mentions_ua: bool = False
    detail: str = ""
    import_errors: list[str] = field(default_factory=list)
    imports: list[ImportCheck] = field(default_factory=list)
    smoke: Smoke = field(default_factory=Smoke)
    caps: list[Cap] = field(default_factory=list)
    pinned: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "no_user_agent")

    @property
    def broken_imports(self) -> list[ImportCheck]:
        return [c for c in self.imports if c.real]

    @property
    def artifacts(self) -> list[ImportCheck]:
        return [c for c in self.imports if c.excused]

    @property
    def uncapped(self) -> list[Cap]:
        return [c for c in self.caps if c.finding]


def _sent_version(value: str) -> str | None:
    m = re.match(r"^[^/]+/([^\s;()]+)", value)
    return m.group(1) if m else None


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _canon(name: str) -> str:
    """PEP 503 normalised distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_own(value: str, dist: str) -> bool:
    """Is this the package's own identity, or somebody else's?

    ``swiss-efv-mcp`` sends ``Mozilla/5.0 … Chrome/124.0`` — it impersonates a
    browser. Read as "product token / version" that parses to version ``5.0``
    and reports as drift against ``0.3.0``, which is wrong twice over: the
    package is not announcing a stale version of itself, and the thing it *is*
    doing (pretending to be Chrome to an upstream) goes unnamed.
    """
    return _norm(value.split("/", 1)[0]) == _norm(dist)


def _run(py: Path, code: str, *args: str, timeout: int = 300) -> dict[str, Any]:
    proc = subprocess.run(
        [str(py), "-c", code, *args], capture_output=True, text=True, timeout=timeout
    )
    # The target may log to stdout on import; the payload is the last JSON line.
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"error": (proc.stderr or "no parseable output").strip()[-300:]}


def _site_packages(env: Path) -> Path | None:
    return next(iter(sorted(env.glob("lib/python*/site-packages"))), None)


# --------------------------------------------------------------------------
# PURE LOGIC — no venv, no network. This is the part the tests own.
# --------------------------------------------------------------------------


def identity_status(r: Result) -> tuple[str, str]:
    """The User-Agent verdict alone: drift | foreign | ok | unverified | none.

    Unchanged from the version that measured 33 packages, and deliberately kept
    as its own function so the statuses layered on top of it (a broken import, a
    failed start, an open dependency range) cannot quietly alter what the
    identity evidence says.
    """
    foreign = [f for f in r.findings if not f.own]
    graded = [f for f in r.findings if f.own and f.sent_version is not None]
    if graded and not all(f._ok for f in graded):
        return "drift", ""
    if foreign:
        # Someone else's identity on our requests. Not version drift — a
        # separate, larger question about what the server tells upstreams.
        return (
            "foreign_user_agent",
            "sends a User-Agent that is not this package's identity",
        )
    if graded:
        return "ok", ""
    if r.mentions_ua:
        # Source talks about a User-Agent and no strategy resolved one. That is
        # a gap in this probe, not a clean package.
        return (
            "unverified",
            "source mentions a User-Agent but no value could be resolved",
        )
    return "no_user_agent", (
        "no custom User-Agent — requests go out under the HTTP client default"
    )


def decide_status(r: Result) -> tuple[str, str]:
    """(status, detail) for an already-taken measurement. Pure.

    The ordering is by how much of the rest of the report the failure
    invalidates, not by how alarming it reads:

      1. ``broken_import``       nothing below it was measured on code that runs
      2. ``smoke_failed``        it imports and does not start
      3. ``drift`` / ``foreign_user_agent``   positive evidence about the wire
      4. ``unbounded_dependency``  it works today and the range is already open
                                   across a published major
      5. ``smoke_unverified``    started, announced nothing, did not crash
      6. ``unverified``          a User-Agent this probe could not resolve
      7. ``ok`` / ``no_user_agent``

    Nothing is dropped by losing the tie: every layer keeps its own field in the
    report and its own line in the rendering, so a package with both drift and a
    broken import shows both.
    """
    ident, ident_detail = identity_status(r)

    broken = r.broken_imports
    if broken:
        shown = ", ".join(c.module for c in broken[:4])
        more = f" (+{len(broken) - 4} more)" if len(broken) > 4 else ""
        unconfirmed = [c for c in broken if c.verdict == "unconfirmed"]
        note = (
            " — including "
            + ", ".join(c.module for c in unconfirmed[:3])
            + ", where the re-measurement itself could not run and the failure "
            "is therefore taken at face value"
            if unconfirmed
            else ""
        )
        return "broken_import", (
            f"{len(broken)} module(s) do not import from the installed "
            f"distribution: {shown}{more}{note}. Measured with the package root "
            "imported first, so this is not an import-order artefact"
        )

    if r.smoke.status == "crashed":
        return "smoke_failed", r.smoke.detail
    if r.smoke.status == "no_entrypoint":
        return "smoke_failed", r.smoke.detail

    if ident in ("drift", "foreign_user_agent"):
        return ident, ident_detail

    open_ranges = r.uncapped
    if open_ranges:
        return "unbounded_dependency", (
            "; ".join(f"{c.requirement} — {c.detail}" for c in open_ranges[:4])
        )

    if r.smoke.status in ("no_event", "error"):
        return "smoke_unverified", r.smoke.detail

    return ident, ident_detail


_MODULE_NOT_FOUND = re.compile(
    r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]"
)


def conditional_names(requires: list[str]) -> set[str]:
    """Distributions this package declares only behind an extra or a marker.

    ``pip install <dist>`` installs none of them, so a module that needs one is
    not reachable in the environment this probe measures — and was never meant
    to be. Read off the published metadata rather than off a list of names
    somebody thought looked test-shaped.
    """
    out: set[str] = set()
    for line in requires:
        req = yp.parse_requirement(line)
        if req is not None and req.conditional:
            out.add(req.key)
    return out


def blames_optional(warm_error: str, conditional: set[str]) -> str:
    """The extras-only distribution a ``ModuleNotFoundError`` blames, or "".

    Matches the missing module's top-level name against the requirement name.
    That is exact for the usual case (``pytest`` the requirement provides
    ``pytest`` the module) and misses where a distribution's import name differs
    from its published name — in which case the failure stays ``real``, which is
    the safe direction to be wrong in.
    """
    m = _MODULE_NOT_FOUND.search(warm_error or "")
    if not m:
        return ""
    top = _canon(m.group(1).split(".")[0])
    return top if top in conditional else ""


def classify_import(
    check: ImportCheck,
    cold: dict[str, Any],
    warm: dict[str, Any],
    conditional: set[str] | None = None,
) -> ImportCheck:
    """Fold the two fresh-interpreter measurements into a verdict. Pure.

    ``warm`` is the one that decides, because it is what a user's code does:
    the package root exists before its submodules are touched. ``cold`` is kept
    only to be able to SAY that the difference is an import order — a claim this
    probe used to make wrongly about ``bag-health-mcp``.
    """
    check.cold_error = "" if cold.get("ok") else str(cold.get("error") or "")
    check.warm_error = "" if warm.get("ok") else str(warm.get("error") or "")

    if "ok" not in warm:
        # The verification subprocess itself did not answer. Fail closed.
        check.verdict = "unconfirmed"
        check.warm_error = str(
            warm.get("error") or "the verification produced no answer"
        )
        return check
    if warm.get("ok"):
        check.verdict = "not-reproduced" if cold.get("ok") else "order-artifact"
        return check
    blamed = blames_optional(check.warm_error, conditional or set())
    if blamed:
        check.verdict = "optional-dep"
        check.error = f"{check.error} (needs {blamed}, declared only behind an extra)"
        return check
    check.verdict = "real"
    return check


def has_start_event(text: str, event: str = DEFAULT_START_EVENT) -> str:
    """The line announcing the server's start, or "".

    Both shapes count, because both are shipped in this portfolio: a structured
    log line whose event field carries the name, and a plain line that merely
    contains it. Parsing only the first would call every text-logging server
    silent; accepting only the second would match a stack frame that happens to
    mention it, so JSON is tried first and its fields are read by name.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("event", "msg", "message", "name", "logger", "event_name"):
                if str(payload.get(key) or "") == event:
                    return line[:300]
            continue
        if event in line:
            return line[:300]
    return ""


def classify_smoke(
    text: str,
    exit_code: int | None,
    entrypoint: str,
    seconds: float,
    event: str = DEFAULT_START_EVENT,
) -> Smoke:
    """What the start attempt proved. Pure — the subprocess is the caller's.

    ``exit_code is None`` means the process was still running when the window
    closed, which for a long-lived transport is the healthy shape. It is NOT the
    verdict on its own: a server that is still running and has announced nothing
    has not been shown to serve.
    """
    evidence = has_start_event(text, event)
    crashed = (exit_code is not None and exit_code != 0) or TRACEBACK in (text or "")
    if crashed:
        tail = "\n".join((text or "").strip().splitlines()[-6:])[:400]
        return Smoke(
            status="crashed",
            entrypoint=entrypoint,
            exit_code=exit_code,
            evidence=evidence,
            detail=(
                "the installed console script crashed"
                + (f" (exit {exit_code})" if exit_code is not None else "")
                + f" within {seconds:.0f}s of starting with stdin closed. "
                "Importing the package is not the same as starting it, and "
                f"this is the difference: {tail!r}"
            ),
        )
    if evidence:
        return Smoke(
            status="ok",
            entrypoint=entrypoint,
            exit_code=exit_code,
            evidence=evidence,
            detail="announced its start and did not crash",
        )
    return Smoke(
        status="no_event",
        entrypoint=entrypoint,
        exit_code=exit_code,
        detail=(
            f"the console script ran for {seconds:.0f}s with stdin closed and did "
            f"not crash, but never announced {event!r}. That is not a pass: this "
            "probe did not see the server reach serving, and not seeing it is not "
            "evidence that it did"
        ),
    )


def dependency_caps(
    requires: list[str], imported: set[str], own: str, versions_of: Any = None
) -> list[Cap]:
    """Upper bounds on the dependencies the package actually imports. Pure-ish.

    ``versions_of(name) -> list[str] | None`` is the only impure part and is
    injected, so the whole rule below is testable without an index. ``None``
    from it means the index could not be read, which is reported as ``unknown``
    and never as "capped" — a bound this probe failed to check is not a bound.

    Environment-gated requirements are skipped wholesale, the same rule
    ``yank_probe`` applies: ``extra == 'dev'`` is not what ``pip install`` gives
    a user, and a broken dev dependency breaks nobody's server.
    """
    out: list[Cap] = []
    seen: set[str] = set()
    for line in requires:
        req = yp.parse_requirement(line)
        if req is None or req.conditional:
            continue
        key = req.key
        if key == _canon(own) or key in seen or key not in imported:
            continue
        seen.add(key)
        if req.bounded_above():
            out.append(
                Cap(
                    name=key,
                    requirement=req.raw,
                    floor=req.floor(),
                    verdict="capped",
                    detail="the range names an upper bound",
                )
            )
            continue
        floor = req.floor()
        floor_key = yp._release(floor.rstrip(".*")) if floor else None
        if floor_key is None:
            out.append(
                Cap(
                    name=key,
                    requirement=req.raw,
                    floor=floor,
                    verdict="armed",
                    detail="no upper bound, and no floor this probe can order either — "
                    "the resolver is free to take any release ever published",
                )
            )
            continue
        published = versions_of(key) if versions_of else None
        if published is None:
            out.append(
                Cap(
                    name=key,
                    requirement=req.raw,
                    floor=floor,
                    verdict="unknown",
                    detail="no upper bound, and the index could not be read to say "
                    "whether a higher major is already available",
                )
            )
            continue
        higher = sorted(
            {
                v
                for v in published
                if not yp.sp.is_prerelease(v)
                and (yp._release(v) or (0,))[0] > floor_key[0]
            },
            key=lambda v: yp._release(v) or (),
        )
        if higher:
            out.append(
                Cap(
                    name=key,
                    requirement=req.raw,
                    floor=floor,
                    verdict="major-available",
                    detail=(
                        f"no upper bound, floor {floor}, and the index already serves "
                        f"{higher[-1]} — a major past it. The artifact will not change; "
                        "the resolver's answer to the next fresh install will. This is "
                        "the shape swiss-energy-mcp 0.3.3 shipped in before mcp 2.0.0 "
                        "killed every reinstall of it"
                    ),
                )
            )
        else:
            out.append(
                Cap(
                    name=key,
                    requirement=req.raw,
                    floor=floor,
                    verdict="armed",
                    detail=(
                        f"no upper bound above floor {floor}; no higher major is "
                        "published yet, so the trap is armed and has not sprung"
                    ),
                )
            )
    return out


# --------------------------------------------------------------------------
# IMPURE — the venv, the network, the subprocess.
# --------------------------------------------------------------------------


def _verify_imports(
    py: Path, raw_errors: list[dict[str, Any]], conditional: set[str] | None = None
) -> list[ImportCheck]:
    """Re-measure every failure from the bulk scan in two fresh interpreters."""
    checks: list[ImportCheck] = []
    for entry in raw_errors:
        module = str(entry.get("module") or "")
        root = str(entry.get("root") or module.split(".")[0])
        check = ImportCheck(
            module=module,
            root=root,
            kind=str(entry.get("kind") or "submodule"),
            error=str(entry.get("error") or ""),
        )
        try:
            cold = _run(py, VERIFY_IMPORT, "cold", root, module, timeout=180)
            warm = _run(py, VERIFY_IMPORT, "warm", root, module, timeout=180)
        except subprocess.TimeoutExpired:
            check.verdict = "unconfirmed"
            check.warm_error = "the verification import did not finish"
            checks.append(check)
            continue
        checks.append(classify_import(check, cold, warm, conditional))
    return checks


def _terminate(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), 15)
        else:  # pragma: no cover - not exercised on Linux CI
            proc.terminate()
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass


def watch(argv: list[str], cwd: Path, seconds: float) -> tuple[str, int | None, str]:
    """Run ``argv`` with stdin CLOSED for ``seconds``. (output, exit_code, error).

    ``exit_code is None`` means it was still running when the window closed,
    which is the healthy shape for a long-lived transport rather than a verdict.

    ``communicate`` and not a read loop: the output is drained while the process
    runs, so a server that writes more than a pipe buffer cannot deadlock
    against the probe that is waiting for it to exit. The whole process GROUP is
    signalled at the end, because a server that forked a worker leaves it behind
    otherwise and the next target inherits the port.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=(os.name != "nt"),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except OSError as exc:
        return "", None, f"could not start {argv[0]}: {type(exc).__name__}: {exc}"

    try:
        text, _ = proc.communicate(timeout=seconds)
        return text or "", proc.returncode, ""
    except subprocess.TimeoutExpired:
        _terminate(proc)
        try:
            text, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            text, _ = proc.communicate()
        return text or "", None, ""


def smoke_start(
    py: Path, dist: str, env: Path, seconds: float, event: str = DEFAULT_START_EVENT
) -> Smoke:
    """Start the installed console script with stdin closed, and watch.

    The entrypoint, not ``python -c "import …"``: the incident this stage exists
    for is a package that imports and does not start, and only the thing a user
    types can show that. stdin is closed on purpose — a stdio server must
    survive EOF, and holding it open here would just make the probe wait out its
    own timeout on every healthy target.
    """
    got = _run(py, CONSOLE_SCRIPTS, dist, timeout=120)
    scripts = got.get("scripts") or []
    bindir = env / ("Scripts" if os.name == "nt" else "bin")
    entry = next((bindir / n for n in scripts if (bindir / n).exists()), None)
    if entry is None:
        return Smoke(
            status="no_entrypoint",
            detail=(
                "the installed distribution declares no console script — there "
                "is nothing for a user to start, whatever the source tree does"
            ),
        )

    text, exit_code, error = watch([str(entry)], env, seconds)
    if error:
        return Smoke(status="error", entrypoint=entry.name, detail=error)
    return classify_smoke(text, exit_code, entry.name, seconds, event)


def probe(
    dist: str,
    constraint: str | None = None,
    keep: bool = False,
    *,
    version: str = "",
    smoke: bool = True,
    smoke_seconds: float = DEFAULT_SMOKE_SECONDS,
    start_event: str = DEFAULT_START_EVENT,
    index_url: str = DEFAULT_INDEX,
    check_caps: bool = True,
    index_timeout: float = 20.0,
) -> Result:
    env = Path(tempfile.mkdtemp(prefix=f"probe-{dist}-"))
    try:
        venv.create(env, with_pip=True, clear=True)
        py = env / "bin" / "python"
        # `==version` when asked, and the reason is measured rather than
        # theoretical: `pip install <dist>` was observed serving the PREVIOUS
        # artifact for minutes after a release had landed on the index, even
        # with --no-cache-dir, because the index's own cache had not caught up.
        # A re-check after a release that does not pin the version is a re-check
        # of the release before it. `release_gap`'s index reconciliation treats
        # that window explicitly; so does this.
        target = f"{dist}=={version}" if version else dist
        spec = [target] + ([constraint] if constraint else [])
        install = subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "--no-cache-dir", *spec],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if install.returncode != 0:
            tail = (install.stderr or install.stdout).strip().splitlines()[-1:] or ["?"]
            return Result(
                dist=dist, status="install_failed", detail=tail[0][:300], pinned=version
            )

        out = _run(py, RUNTIME_SCAN, dist)
        if "installed" not in out:
            return Result(
                dist=dist,
                status="install_failed",
                detail=str(out.get("error"))[:300],
                pinned=version,
            )

        result = Result(dist=dist, installed=out["installed"], pinned=version)
        if version and out["installed"] != version:
            # The pin is the whole point; a venv that came back with something
            # else has not verified the release that was asked about.
            result.status = "install_failed"
            result.detail = (
                f"pinned to =={version} and the venv reports {out['installed']} — "
                "the artifact under test is not the one named"
            )
            return result

        requires = [str(r) for r in out.get("requires", [])]
        raw_errors = [e for e in out.get("errors", []) if isinstance(e, dict)]
        result.imports = _verify_imports(py, raw_errors, conditional_names(requires))
        # The old flat list is kept so consumers that read it keep working; it
        # now carries the VERDICT, not just the message.
        result.import_errors = [
            f"{c.module}: {c.error} [{c.verdict}]" for c in result.imports
        ]

        seen: set[str] = set()
        for hit in out["runtime"]:
            seen.add(hit["value"])
            result.findings.append(
                Finding(
                    value=hit["value"],
                    sent_version=_sent_version(hit["value"]),
                    evidence="runtime",
                    where=f"{hit['module']}.{hit['attr']}",
                )
            )

        site = _site_packages(env)
        top = dist.replace("-", "_")
        for path in sorted(site.rglob("*.py")) if site else []:
            rel = path.relative_to(site)
            if not str(rel).startswith(top):
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # Comments are documentation, not evidence — see code_only().
            text = code_only(raw)
            if MENTIONS_UA.search(text):
                result.mentions_ua = True

            modname = (
                str(rel.with_suffix("")).replace("/", ".").removesuffix(".__init__")
            )
            for token, expr in FSTRING.findall(text):
                got = _run(py, EVAL_EXPR, modname, expr, timeout=120)
                if "value" not in got:
                    continue
                value = f"{token}/{got['value']}"
                if value not in seen:
                    seen.add(value)
                    result.findings.append(
                        Finding(
                            value=value,
                            sent_version=got["value"],
                            evidence="f-string",
                            where=f'{modname}: f"{token}/{{{expr}}}"',
                        )
                    )
            # Only literals in files that talk about User-Agents, so that a
            # version string in an unrelated constant is not mistaken for one.
            if MENTIONS_UA.search(text):
                for value in LITERAL.findall(text):
                    if value not in seen:
                        seen.add(value)
                        result.findings.append(
                            Finding(
                                value=value,
                                sent_version=_sent_version(value),
                                evidence="literal",
                                where=str(rel),
                            )
                        )

        for f in result.findings:
            f.own = _is_own(f.value, dist)
            f._ok = f.own and f.sent_version == result.installed

        if check_caps:
            cache: dict[str, list[str] | None] = {}

            def versions_of(name: str) -> list[str] | None:
                if name not in cache:
                    cache[name] = yp.fetch_dependency_versions(
                        name, index_url, index_timeout
                    )
                return cache[name]

            result.caps = dependency_caps(
                requires,
                {str(d) for d in out.get("imported_dists", [])},
                dist,
                versions_of,
            )

        if smoke:
            result.smoke = smoke_start(py, dist, env, smoke_seconds, start_event)

        result.status, result.detail = decide_status(result)
        return result
    finally:
        if not keep:
            shutil.rmtree(env, ignore_errors=True)


def render(r: Result) -> str:
    """Every layer gets a line. The status is the headline, not the whole report.

    A package can be drifting AND have a broken import AND an open dependency
    range; only one of those can be the status, and printing only that one would
    hide two real facts behind a precedence rule.
    """
    if r.status == "install_failed":
        return f"INSTALL    {r.dist}: {r.detail}"
    head = f"{r.dist} {r.installed}"
    lines: list[str] = []

    for check in r.broken_imports:
        marker = "BROKEN-IMP" if check.verdict == "real" else "IMPORT-?  "
        lines.append(
            f"{marker} {head} {check.module} does not import: "
            f"{(check.warm_error or check.error)[:140]}"
        )
    for check in r.artifacts:
        if check.verdict == "order-artifact":
            lines.append(
                f"IMPORT-ORD {head} {check.module} fails only as the FIRST "
                f"import of a process and imports fine after {check.root} — "
                "an artefact of import order, not a finding"
            )
        elif check.verdict == "optional-dep":
            lines.append(
                f"IMPORT-OPT {head} {check.module} needs a dependency this "
                "package declares only behind an extra — not installed by "
                "`pip install`, and not meant to be"
            )
        else:
            lines.append(
                f"IMPORT-ORD {head} {check.module} imports cleanly in a fresh "
                "interpreter both cold and warm — the bulk scan's own state "
                "produced that error, not the package"
            )

    if r.smoke.status == "crashed":
        lines.append(f"SMOKE-FAIL {head} {r.smoke.entrypoint}: {r.smoke.detail}")
    elif r.smoke.status == "no_entrypoint":
        lines.append(f"SMOKE-NONE {head}: {r.smoke.detail}")
    elif r.smoke.status == "no_event":
        lines.append(f"SMOKE-?    {head} {r.smoke.entrypoint}: {r.smoke.detail}")
    elif r.smoke.status == "error":
        lines.append(f"SMOKE-ERR  {head}: {r.smoke.detail}")

    for cap in r.caps:
        if cap.verdict == "major-available":
            lines.append(f"UNCAPPED   {head} {cap.requirement}: {cap.detail}")
        elif cap.verdict == "unknown":
            lines.append(f"UNCAPPED-? {head} {cap.requirement}: {cap.detail}")

    for f in r.findings:
        if not f.own:
            lines.append(
                f"FOREIGN-UA {head} sends {f.value[:70]!r} (via {f.evidence}, {f.where})"
            )
        elif not f._ok:
            lines.append(
                f"DRIFT      {head} sends {f.sent_version} "
                f"({f.value!r}, via {f.evidence}, {f.where})"
            )

    if r.status == "ok" and not lines:
        vals = ", ".join(f.value for f in r.findings) or "-"
        return f"OK         {head} sends {vals}"
    if r.status == "no_user_agent" and not lines:
        return f"NO-UA      {head}: {r.detail}"
    if r.status == "unverified":
        lines.append(f"UNVERIFIED {head}: {r.detail}")
    return "\n".join(lines) if lines else f"OK         {head}"


def _as_dict(r: Result) -> dict[str, Any]:
    return {
        "dist": r.dist,
        "installed": r.installed,
        "pinned": r.pinned,
        "status": r.status,
        "detail": r.detail,
        "findings": [
            {
                "value": f.value,
                "sent_version": f.sent_version,
                "matches_installed": f._ok,
                "evidence": f.evidence,
                "where": f.where,
            }
            for f in r.findings
        ],
        "imports": [c.as_dict() for c in r.imports],
        "smoke": r.smoke.as_dict(),
        "dependency_caps": [c.as_dict() for c in r.caps],
        "import_errors": r.import_errors,
    }


def read_manifest(path: Path) -> tuple[int, list[str], list[tuple[str, str]]]:
    """Split a coverage manifest into a total, targets, and justified omissions.

    Produced by ``coverage_manifest.py --format json`` in the portfolio repo. An
    entry with ``pypi_dist: null`` publishes no package, so there is no artefact
    for this probe to measure — that omission is justified, and it is justified
    *because the manifest says so*, not because a list somewhere happened not to
    mention it. That difference is the whole point.

    The total counts EVERY manifest entry, probeable or not. Counting only the
    probeable ones would make the denominator depend on the same judgement the
    coverage check exists to audit.

    The manifest is validated rather than read optimistically, because the two
    ways it can be wrong both end in a false green:

    * an **empty** ``servers`` list would report ``0/0 geprueft`` and exit 0 —
      indistinguishable from an audited portfolio;
    * a **missing** ``pypi_dist`` key would be read like an explicit ``null``,
      so if the producer ever renames the field, every entry silently becomes a
      justified omission and nothing is measured at all.

    Both would recreate exactly the failure this whole mechanism exists to
    prevent, one layer further out. An absent key and a deliberate ``null`` are
    different claims and must not share a code path.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise SystemExit(f"{path}: kein 'servers'-Feld mit einer Liste")
    if not servers:
        raise SystemExit(
            f"{path}: leere Zielliste. Ein Lauf ohne Ziele meldet sonst '0/0 geprueft' "
            "und Exit 0 — nicht unterscheidbar von einem geprueften Portfolio"
        )

    targets, unpublished = [], []
    for i, s in enumerate(servers):
        if not isinstance(s, dict) or "id" not in s:
            raise SystemExit(f"{path}: Eintrag {i} hat kein 'id'-Feld")
        if "pypi_dist" not in s:
            raise SystemExit(
                f"{path}: Eintrag {s['id']} hat kein Feld 'pypi_dist'. Fehlend und "
                "null sind verschiedene Aussagen: null heisst 'kein Paket', "
                "fehlend heisst 'das Manifest passt nicht zu diesem Werkzeug'"
            )
        dist = s["pypi_dist"]
        if dist is None:
            unpublished.append((s["id"], "kein Paket auf dem Index (laut Manifest)"))
        elif isinstance(dist, str) and dist.strip():
            targets.append(dist)
        else:
            raise SystemExit(
                f"{path}: Eintrag {s['id']}: 'pypi_dist' ist weder Name noch null"
            )

    return len(servers), targets, unpublished


def parse_allow_skip(items: list[str]) -> dict[str, str]:
    """``name:reason`` pairs. The reason is mandatory — that is the mechanism.

    Skipping is allowed; skipping silently is not. Requiring the reason on the
    command line puts the omission into the run's own output instead of leaving
    it with whoever typed the command.
    """
    out: dict[str, str] = {}
    for item in items:
        name, sep, reason = item.partition(":")
        if not sep or not reason.strip():
            raise SystemExit(f"--allow-skip braucht 'name:grund', bekommen: {item!r}")
        out[name.strip()] = reason.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="published_probe")
    ap.add_argument(
        "dists",
        nargs="*",
        help="distribution names as published on the index (or use --manifest)",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        help="Zielliste aus coverage_manifest.py --format json; erzwingt die Abdeckung",
    )
    ap.add_argument(
        "--allow-skip",
        action="append",
        default=[],
        metavar="NAME:GRUND",
        help="einen Eintrag der Zielliste begruenden statt pruefen; wiederholbar",
    )
    ap.add_argument(
        "--constraint",
        help="extra pip requirement, e.g. 'mcp<2' for a package that no longer imports "
        "against the current release of a dependency",
    )
    ap.add_argument(
        "--version",
        default="",
        help="pin the install to this exact version (`dist==VERSION`). Use it for "
        "every re-check after a release: an unpinned install was measured serving "
        "the previous artifact for minutes after the new one was on the index, "
        "even with --no-cache-dir",
    )
    ap.add_argument(
        "--index-url",
        default=DEFAULT_INDEX,
        help="PEP 503 index used for the dependency upper-bound check "
        f"(default: {DEFAULT_INDEX})",
    )
    ap.add_argument(
        "--no-smoke",
        action="store_true",
        help="skip starting the installed console script",
    )
    ap.add_argument(
        "--no-cap-check",
        action="store_true",
        help="skip the requires_dist upper-bound check (no index requests)",
    )
    ap.add_argument(
        "--smoke-seconds",
        type=float,
        default=DEFAULT_SMOKE_SECONDS,
        help=f"how long to watch the entrypoint (default: {DEFAULT_SMOKE_SECONDS:g})",
    )
    ap.add_argument(
        "--start-event",
        default=DEFAULT_START_EVENT,
        help=f"the event the entrypoint must announce (default: {DEFAULT_START_EVENT})",
    )
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument(
        "--keep-venv", action="store_true", help="leave the venv for inspection"
    )
    args = ap.parse_args()

    allowed = parse_allow_skip(args.allow_skip)
    expected = 0
    skipped: list[tuple[str, str]] = []

    if args.manifest:
        if args.dists:
            raise SystemExit("--manifest und eine Dist-Liste schliessen sich aus")
        expected, probeable, skipped = read_manifest(args.manifest)
        targets = [d for d in probeable if d not in allowed]
        skipped += [(d, allowed[d]) for d in probeable if d in allowed]
    elif args.dists:
        targets = args.dists
    else:
        raise SystemExit("weder Dist-Namen noch --manifest angegeben")

    if args.version and len(targets) > 1:
        print(
            "published: --version pins ONE distribution; pass one name with it",
            file=sys.stderr,
        )
        return 2

    results = []
    for dist in targets:
        try:
            results.append(
                probe(
                    dist,
                    args.constraint,
                    args.keep_venv,
                    version=args.version,
                    smoke=not args.no_smoke,
                    smoke_seconds=args.smoke_seconds,
                    start_event=args.start_event,
                    index_url=args.index_url,
                    check_caps=not args.no_cap_check,
                )
            )
        except subprocess.TimeoutExpired:
            results.append(Result(dist=dist, status="install_failed", detail="timeout"))
        except Exception as e:  # noqa: BLE001 - eine Dist darf den Sweep nicht abbrechen
            # Ohne das endet ein Portfolio-Lauf beim ersten Ausrutscher und
            # berichtet ueber ein Praefix der Zielliste, als waere es die Liste.
            results.append(
                Result(
                    dist=dist,
                    status="install_failed",
                    detail=f"{type(e).__name__}: {e}",
                )
            )

    # Ein Ziel ohne Ergebnis waere ein stiller Ausfall — genau die Sorte, die
    # aussieht wie ein sauberer Lauf. Beides zaehlt gegen dieselbe Soll-Zahl.
    missing = [d for d in targets if d not in {r.dist for r in results}]
    coverage_ok = not args.manifest or (
        len(results) + len(skipped) == expected and not missing
    )

    if args.format == "json":
        # Ohne --manifest bleibt die Ausgabe die blanke Liste wie bisher; mit
        # --manifest wird sie ein Objekt, weil die Abdeckung Teil des Ergebnisses
        # ist und nicht als Kommentar danebenstehen darf.
        payload = [_as_dict(r) for r in results]
        out: Any = payload
        if args.manifest:
            out = {
                "coverage": {
                    "expected": expected,
                    "probed": len(results),
                    "skipped": [{"name": n, "reason": why} for n, why in skipped],
                    "missing": missing,
                    "complete": coverage_ok,
                },
                "results": payload,
            }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(render(r))
        if args.manifest:
            line = f"{len(results)}/{expected} geprueft"
            if skipped:
                line += " — uebersprungen: " + ", ".join(
                    f"{n} ({why})" for n, why in skipped
                )
            if missing:
                line += " — OHNE ERGEBNIS: " + ", ".join(missing)
            print(line)

    if not coverage_ok:
        return 1
    if any(r.status == "install_failed" for r in results):
        return 2
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
