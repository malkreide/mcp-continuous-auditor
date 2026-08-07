#!/usr/bin/env python3
"""Reference-drift probe — is the template still the best version of itself?

THE INCIDENT
------------
On 2026-08-03 ``reference/retry_backoff.py`` in ``mcp-data-source-probe-skill``
was found to carry five defects that had already been fixed, one at a time, in
eleven servers. The originating commit of ``swiss-efv-mcp`` contained the same
line, word for word::

    raise RuntimeError(f"Upstream unreachable after retries: {last_error}")

and the CI failure it produced was what started the whole round. None of the
eleven reviews saw it, because in each repository only the copied fragment was
visible. Nobody was ever looking at the template and a server at the same time.

That is the defect this probe mechanises, and it is deliberately asked in BOTH
directions:

* ``REFERENCE_STALE`` — the template is behind the servers. The more dangerous
  of the two: it hands every new server a defect the existing ones have long
  since repaired, and it does so silently, at the moment somebody is least
  likely to read the code they are copying.
* ``REFERENCE_UNADOPTED`` — a server is behind the template. A fix was written
  once and never arrived.
* ``UNVERIFIED`` — the mapping or the retrieval failed. Never silently "clean".

THE MAPPING IS DECLARED, NEVER GUESSED
--------------------------------------
Template → target code comes from an explicit manifest in the skill repository,
``reference/adoption.toml``: which template, which repositories, which file and
symbol, and since when. There is no name-similarity fallback and there will not
be one. A guessed mapping is worse here than no mapping at all, because it
produces a finding nobody can retrace — and a finding nobody can retrace is how
a gate gets switched off.

If the manifest is missing while ``reference/`` ships code, THAT is the first
finding (``MANIFEST_MISSING``) and the probe stops there. A template with no
declared adopters is a template whose blast radius is unknown.

WHAT IS COMPARED — AND WHY NOT THE TEXT
---------------------------------------
A full-text diff is unusable. The adopting repositories rename the constants,
rewrap the lines, reword the messages and rename the function; every one of
those is a correct adoption, and a probe that calls them drift reports forty
things nobody will fix. A bare function-name comparison is the opposite failure:
it is satisfied by a function that shares a name and nothing else.

So the probe compares the PROPERTIES the template guarantees, on two layers.

1. **Declared properties** — ``adoption.toml`` names them, each as a small AST
   predicate over the symbol:

   * ``calls``   — a call to a dotted name (``time.monotonic`` = a wall-clock
     budget exists). Note the limit, because it produced a false positive on
     2026-08-07: ``calls`` cannot tell TIMING something from LIMITING it.
     ``i14y-mcp`` called ``time.perf_counter()`` twice to compute an
     ``elapsed_ms`` for a log line and nothing else, and counted as a server
     with a budget — it was the only one of eleven that appeared to have one,
     and it did not. Declare the bound itself where a manifest can (``asyncio``
     deadline calls alongside the clock read), and read the two lines before
     trusting a lone clock call.
   * ``literal`` — a string literal (``retry-after`` = the server's own hint is
     read; a header name is on the wire and cannot be renamed away)
   * ``wraps``   — a call to ``outer`` that encloses a call to ``inner``
     (``min`` wrapping ``random.uniform`` = the ceiling is applied after the
     jitter, not before it — the ordering, not just the presence). BOTH shapes
     count: the inner call written inside the outer call's arguments, and the
     inner call bound to a local that an argument then names. They are mutually
     exclusive, and reading only the first reported all six correct adopters as
     lagging — see ``_bound_to_inner``.
   * ``raises`` / ``handles`` — an exception type raised, or caught

   each with ``expect = "present"`` or ``"absent"``, because half of these fixes
   are removals.

2. **The unanimity layer** — the declared list has one blind spot, and it is
   exactly the incident above: whoever forgets the fix in the template also
   forgets to write the property down. So the probe also compares three AST
   facts that need no declaration, chosen because they are the ones copying does
   NOT rename — the names of called functions, of raised exception types and of
   caught exception types (last dotted segment only, since import aliasing
   differs per repository and the tail does not).

   A fact is reported ONLY on unanimity: every adoption site that could be read
   agrees, there are at least ``--floor`` of them, and the template differs.
   Eleven independently maintained repositories agreeing is evidence; two are a
   coincidence. This layer can produce ``REFERENCE_STALE`` and nothing else — an
   UNADOPTED verdict requires a declared property, because "one server lacks
   something the others have" without a declaration is precisely the guess this
   probe refuses to make.

TARGETS COME FROM DISK, NOT FROM THE NETWORK
--------------------------------------------
``--repos-root`` points at the checkouts. Nothing is fetched, nothing is cloned:
a probe that reaches the network is neither reproducible nor read-only, and a
network error is indistinguishable from a missing repository. A repository that
is not on disk is ``UNVERIFIED`` — and the report states its COVERAGE, n of m
sites read, so a narrow run cannot read as a clean one.

A declared property is checked against the template whether or not any adoption
site could be read: "does this file do what the manifest says it does" is a claim
about one file, and gating it on the checkouts would leave a fresh manifest
unchecked on the machine it is written on. Only how far the SERVERS have moved
needs them.

EXIT CODES
  0    the template and every declared adoption site agree
  2    FINDING — REFERENCE_STALE, REFERENCE_UNADOPTED, MANIFEST_MISSING,
       MANIFEST_INVALID or TEMPLATE_UNMAPPED
  3    NOT MEASURED — no reference/ directory, or nothing was found to report and
       no adoption site was readable
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run

Usage:
  python scripts/reference_drift_probe.py --target . --repos-root ~/src
  python scripts/reference_drift_probe.py --target . --repos-root ~/src --format json
  python scripts/reference_drift_probe.py --target . \
      --repo-path malkreide/swiss-efv-mcp=/srv/swiss-efv-mcp
  python scripts/reference_drift_probe.py --target . --repos-root ~/src --no-unanimity
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

REFERENCE_DIR = "reference"
MANIFEST_NAME = "adoption.toml"

# Files under reference/ that are not templates and need no mapping.
NOT_A_TEMPLATE = {"__init__.py", "conftest.py"}

# How many adoption sites must be readable and agree before the unanimity layer
# is allowed to speak. Eleven repositories agreeing is evidence; two are a
# coincidence, and a floor of one would turn this layer into a two-file diff.
DEFAULT_FLOOR = 3

# How far a property may follow a call out of the declared symbol and into a
# module-level helper of the same file. Mirrors `HELPER_DEPTH` in the skill's
# own reader (`tools/checks/adoption.py`) — the two read one manifest and have
# to mean the same thing by `symbol`; see the note on `load_symbol`.
HELPER_DEPTH = 3

PROPERTY_KINDS = ("calls", "literal", "wraps", "raises", "handles")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


@dataclass
class Property:
    id: str
    says: str
    kind: str
    expect: str = "present"
    any_of: tuple[str, ...] = ()
    outer: str = ""
    inner: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "says": self.says,
            "kind": self.kind,
            "expect": self.expect,
            "any_of": list(self.any_of),
            "outer": self.outer,
            "inner": list(self.inner),
        }


@dataclass
class Site:
    """One declared adoption of one template, in one repository."""

    repo: str
    file: str
    symbol: str
    since: str
    path: Path | None = None
    unit: Unit | None = None
    unverified: str = ""

    @property
    def name(self) -> str:
        return f"{self.repo}:{self.file}::{self.symbol or '(module)'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "file": self.file,
            "symbol": self.symbol,
            "since": self.since,
            "path": str(self.path) if self.path else "",
            "read": self.unit is not None,
            "unverified": self.unverified,
        }


@dataclass
class Template:
    file: str
    symbol: str
    properties: list[Property] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)
    unit: Unit | None = None
    unverified: str = ""

    @property
    def name(self) -> str:
        return f"{self.file}::{self.symbol or '(module)'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "symbol": self.symbol,
            "read": self.unit is not None,
            "unverified": self.unverified,
            "properties": [p.as_dict() for p in self.properties],
            "sites": [s.as_dict() for s in self.sites],
            "sites_read": sum(1 for s in self.sites if s.unit is not None),
            "sites_declared": len(self.sites),
        }


class ManifestError(Exception):
    """The manifest exists but does not say what it must say."""


def load_manifest(path: Path) -> tuple[list[Template], list[str], list[str]]:
    """(templates, unmapped_ok, unanimity_ignore) — or ``ManifestError``.

    Every field the probe later prints in a finding is required here rather
    than defaulted. A finding says "``malkreide/x`` adopted this on 2026-05-14";
    a manifest that let ``since`` be omitted would produce findings that read as
    if the mapping were older or newer than anybody actually claimed.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{path.name} could not be parsed: {exc}") from exc

    if raw.get("schema") != 1:
        raise ManifestError(
            f"{path.name} must declare `schema = 1`, got {raw.get('schema')!r}"
        )

    unmapped_ok = [str(x) for x in raw.get("unmapped_ok", [])]
    ignore = [str(x) for x in raw.get("unanimity", {}).get("ignore", [])]

    entries = raw.get("template")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"{path.name} declares no `[[template]]`")

    templates = []
    for index, entry in enumerate(entries, 1):
        where = f"{path.name} [[template]] #{index}"
        file = entry.get("file")
        if not isinstance(file, str) or not file:
            raise ManifestError(f"{where}: `file` is required")
        template = Template(file=file, symbol=str(entry.get("symbol", "")))
        for prop in entry.get("property", []):
            template.properties.append(_property(prop, f"{where} `{file}`"))
        sites = entry.get("adoption")
        if not isinstance(sites, list) or not sites:
            raise ManifestError(
                f"{where} `{file}`: no `[[template.adoption]]`. A template with no "
                "declared adopters has an unknown blast radius; declare the "
                "repositories or remove the template"
            )
        for site in sites:
            template.sites.append(_site(site, f"{where} `{file}`"))
        templates.append(template)
    return templates, unmapped_ok, ignore


def _property(raw: object, where: str) -> Property:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: `[[template.property]]` must be a table")
    for key in ("id", "says", "kind"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            raise ManifestError(f"{where}: property is missing `{key}`")
    kind = str(raw["kind"])
    if kind not in PROPERTY_KINDS:
        raise ManifestError(
            f"{where}: property `{raw['id']}` has kind {kind!r}; "
            f"known kinds are {', '.join(PROPERTY_KINDS)}"
        )
    expect = str(raw.get("expect", "present"))
    if expect not in ("present", "absent"):
        raise ManifestError(
            f"{where}: property `{raw['id']}` has expect={expect!r}, not present/absent"
        )
    prop = Property(
        id=str(raw["id"]),
        says=str(raw["says"]),
        kind=kind,
        expect=expect,
        any_of=tuple(str(x) for x in _listify(raw.get("any_of"))),
        outer=str(raw.get("outer", "")),
        inner=tuple(str(x) for x in _listify(raw.get("inner"))),
    )
    if kind == "wraps":
        if not prop.outer or not prop.inner:
            raise ManifestError(
                f"{where}: property `{prop.id}` of kind `wraps` needs `outer` and `inner`"
            )
    elif not prop.any_of:
        raise ManifestError(
            f"{where}: property `{prop.id}` of kind `{kind}` needs `any_of`"
        )
    return prop


def _site(raw: object, where: str) -> Site:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: `[[template.adoption]]` must be a table")
    for key in ("repo", "file", "since"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            raise ManifestError(f"{where}: adoption entry is missing `{key}`")
    since = str(raw["since"])
    if not ISO_DATE.match(since):
        raise ManifestError(
            f"{where}: adoption of `{raw['repo']}` has since={since!r}, not YYYY-MM-DD"
        )
    return Site(
        repo=str(raw["repo"]),
        file=str(raw["file"]),
        symbol=str(raw.get("symbol", "")),
        since=since,
    )


def _listify(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# --------------------------------------------------------------------------
# Reading a symbol out of a file
# --------------------------------------------------------------------------


@dataclass
class Unit:
    """One symbol, plus the import table of the module it came from.

    The imports are carried because they are the difference between two
    identical adoptions. ``import random as rnd`` and ``from random import
    uniform`` are the same call written three ways, and a probe that compared
    the written form would report the naming convention of each repository as
    drift.
    """

    node: ast.AST
    aliases: dict[str, str] = field(default_factory=dict)
    helpers: tuple[ast.AST, ...] = ()

    @property
    def roots(self) -> tuple[ast.AST, ...]:
        """The entry symbol first, then the helpers it reaches.

        Order matters for nothing except readability; what matters is that a
        property may be satisfied in any of them. Sub-expression units — the
        ones built inside `_observed` to look at a single argument — carry no
        helpers and behave exactly as they did before.
        """
        return (self.node, *self.helpers)

    def resolve(self, dotted: str) -> str:
        head, _, rest = dotted.partition(".")
        target = self.aliases.get(head)
        if target is None:
            return dotted
        return f"{target}.{rest}" if rest else target

    def calls(self) -> list[tuple[str, ast.Call]]:
        return [
            (self.resolve(name), call)
            for root in self.roots
            for name, call in _raw_calls(root)
        ]

    def literals(self) -> set[str]:
        seen: set[str] = set()
        for root in self.roots:
            seen |= _literals(root)
        return seen

    def raised(self) -> list[str]:
        return [self.resolve(name) for root in self.roots for name in _raw_raised(root)]

    def caught(self) -> list[str]:
        return [self.resolve(name) for root in self.roots for name in _raw_caught(root)]

    def facts(self) -> frozenset[str]:
        """The rename-stable facts the unanimity layer compares.

        The LAST dotted segment only. Everything before it is an import
        decision that differs between repositories without any behaviour
        differing with it; the tail is the part that copying does not touch.

        ``raise Foo(…)`` is a call as well as a raise, and counting it as both
        would report the same change twice under two labels — once as
        ``call:Foo`` and once as ``raise:Foo``. The construction is dropped from
        the call facts, not from the raise facts, so it is still reported under
        the label that describes it. A ``Foo(…)`` called anywhere else in the
        same symbol keeps its call fact on its own.
        """
        constructed = {
            id(node.exc)
            for root in self.roots
            for node in ast.walk(root)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        }
        out = {
            f"call:{_tail(name)}"
            for name, call in self.calls()
            if id(call) not in constructed
        }
        out |= {f"raise:{_tail(name)}" for name in self.raised()}
        out |= {f"except:{_tail(name)}" for name in self.caught()}
        return frozenset(out)


def _tail(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _named_functions(tree: ast.Module) -> dict[str, ast.AST]:
    """Every `def`/`async def` in the file, by name — methods included.

    `ast.walk` and not `tree.body`, and `setdefault` so the first definition of
    a name wins: this is `tools/checks/adoption.py::_functions` line for line,
    and the two have to agree or the manifest means two things again. Methods
    matter in practice — `swiss-efv-mcp` puts its jitter in
    `EFVClient._delay`, and a module-level-only reading calls that server
    unadopted for a property it plainly holds.
    """
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.setdefault(node.name, node)
    return out


def _reachable_helpers(tree: ast.Module, entry: ast.AST) -> tuple[ast.AST, ...]:
    """The module-level functions ``entry`` calls, followed to ``HELPER_DEPTH``.

    WHY A PROPERTY MAY LEAVE ITS SYMBOL
    -----------------------------------
    This file and the skill's own `tools/checks/adoption.py` read ONE manifest
    from two sides. Until 2026-08-07 they meant different things by `symbol`:
    the skill took the symbol plus the module functions it calls, this took the
    symbol and nothing else. Nobody noticed while the templates were one flat
    function.

    Then the 2026-08-03 repair extracted helpers. In `reference/retry_backoff.py`
    the `retry-after` literal moved into `parse_retry_after` and `random.random`
    into `compute_delay`; `fetch_with_retry` calls them and touches neither. The
    single-symbol reading therefore reported `reads_retry_after`, `jitters` and
    `caps_after_jitter` as satisfied *nowhere* — over a template that satisfies
    all three, and 23 adoption sites that do too, every one of which had made
    the same extraction.

    A finding that indicts everything indicts nothing, and that shape is what
    gave it away: the verdict was about the scope of the measurement, not about
    any code. The skill's Check 17 said all eight properties held on the same
    file, on the same day.

    THE LIMITS, WHICH ARE THE POINT
    -------------------------------
    * CALLED, not merely referenced. A helper passed by name and invoked by
      somebody else is not followed — `swiss-statistics-mcp` hands
      `wait=_retry_wait` to `tenacity`, and no call to it appears anywhere in
      this file. That server therefore still reads as unadopted for the three
      properties that live in the callback, and the report is honest about the
      shape rather than quietly widened to cover it. Following bare name
      references would follow every function ever mentioned.
    * SAME FILE ONLY. Nothing is imported and followed across modules. A
      property that chased imports would eventually measure a library.
    * BOUNDED by `HELPER_DEPTH`, the same constant the skill uses. Three hops is
      what the portfolio's deepest correct adoption needs; unbounded following
      would end at "the property holds somewhere in the file", which is not what
      any of these properties say.
    """
    functions = _named_functions(tree)
    seen: set[int] = {id(entry)}
    frontier: list[ast.AST] = [entry]
    out: list[ast.AST] = []
    for _ in range(HELPER_DEPTH):
        nxt: list[ast.AST] = []
        for node in frontier:
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                helper = functions.get(_dotted(call.func).rsplit(".", 1)[-1])
                if helper is not None and id(helper) not in seen:
                    seen.add(id(helper))
                    nxt.append(helper)
                    out.append(helper)
        frontier = nxt
        if not frontier:
            break
    return tuple(out)


def load_symbol(path: Path, symbol: str) -> tuple[Unit | None, str]:
    """(unit, reason-it-could-not-be-read). An empty ``symbol`` means the module."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path} could not be read: {exc}"
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return None, f"{path} is not parseable Python: {exc}"
    aliases = _aliases(tree)
    if not symbol:
        return Unit(tree, aliases), ""
    node: ast.AST = tree
    for part in symbol.split("."):
        found = _child(node, part)
        if found is None:
            return None, f"{path}: no symbol `{symbol}` (`{part}` not found)"
        node = found
    return Unit(node, aliases, _reachable_helpers(tree, node)), ""


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name → the dotted name it actually refers to.

    Three bindings count, and the third is not an import at all:

    * ``import asyncio`` / ``import random as rnd``
    * ``from random import uniform``
    * ``_sleep = asyncio.sleep`` at MODULE level — a plain assignment of a
      dotted name to a name. It is an alias in every sense that matters here,
      and it is a common idiom rather than an exotic one: four of the servers
      in this portfolio bind the backoff sleep that way so a test can patch the
      module attribute without reaching into every import in the process. Read
      literally, ``await _sleep(delay)`` looked like a symbol that does not
      sleep, and the probe reported four adoptions as lagging when none was.

    Only module level, and only a bare dotted name on the right-hand side: a
    rebinding inside a function is control flow, and a call or a subscript on
    the right is a value, not another name for one.

    Relative imports are skipped: ``from .http import retry`` names something
    inside the repository, whose full path differs per repository by
    construction, and resolving it would manufacture a difference.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                out[local] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Attribute):
            continue
        dotted = _dotted(value)
        # Resolve through what is already known, so `import asyncio as aio`
        # followed by `_sleep = aio.sleep` still lands on `asyncio.sleep`.
        head, _, rest = dotted.partition(".")
        if head in out and rest:
            dotted = f"{out[head]}.{rest}"
        if dotted and target.id not in out:
            out[target.id] = dotted
    return out


def _child(parent: ast.AST, name: str) -> ast.AST | None:
    for node in ast.iter_child_nodes(parent):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.name == name
        ):
            return node
    return None


# --------------------------------------------------------------------------
# The AST facts
# --------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    """The dotted name a callee is written as, as far as it can be named.

    ``random.SystemRandom().uniform`` yields ``uniform``: the part before the
    intervening call cannot be spelled, and inventing a name for it would make
    two identical adoptions look different.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _raw_calls(node: ast.AST) -> list[tuple[str, ast.Call]]:
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _dotted(child.func)
            if name:
                out.append((name, child))
    return out


def _literals(node: ast.AST) -> set[str]:
    return {
        child.value.strip().lower()
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _raw_raised(node: ast.AST) -> list[str]:
    out = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Raise) or child.exc is None:
            continue
        exc = child.exc.func if isinstance(child.exc, ast.Call) else child.exc
        name = _dotted(exc) if isinstance(exc, (ast.Name, ast.Attribute)) else ""
        if name:
            out.append(name)
    return out


def _raw_caught(node: ast.AST) -> list[str]:
    out = []
    for child in ast.walk(node):
        if not isinstance(child, ast.ExceptHandler) or child.type is None:
            continue
        types = child.type.elts if isinstance(child.type, ast.Tuple) else [child.type]
        for item in types:
            name = _dotted(item) if isinstance(item, (ast.Name, ast.Attribute)) else ""
            if name:
                out.append(name)
    return out


def _bound_to_inner(
    root: ast.AST, aliases: dict[str, str], inner: Sequence[str]
) -> set[str]:
    """Locals assigned from an expression that contains one of ``inner``.

    `wraps` asks about ORDER — does the ceiling sit outside the jitter? — and
    order can be written two ways that no single expression satisfies at once:

        return min(base * random.uniform(0.5, 1.5), CAP)   # lexical
        jittered = base * random.uniform(0.5, 1.5)         # name-bound
        return min(jittered, CAP)

    The choice between them IS whether a name is bound, so a lexical-only read
    does not measure the ordering — it measures a coding habit. Measured against
    the eleven-server sweep of 2026-08-03: every one of the six repositories
    that applies the cap after the jitter writes the second form. Read lexically,
    `caps_after_jitter` failed 6 of 6 adoptions that hold exactly the behaviour
    it describes, and the finding pointed at correct code.

    Deliberately shallow, and scoped to ONE root. A binding in one helper must
    not satisfy a `min` in another: the two would be different scopes at
    runtime, and pairing them across would report an ordering nobody wrote.
    Only a direct assignment inside the same root counts —
    no transitive chains, no attributes, no augmented assignment. A wider
    analysis would start reporting a cap where the value merely passed through,
    and a false *positive* here is worse than the false negative it replaces:
    this property exists to catch `min(cap, base) * jitter`, which is not a
    bound at all.
    """
    out: set[str] = set()
    for node in ast.walk(root):
        if not isinstance(node, ast.Assign):
            continue
        value_unit = Unit(node.value, aliases)
        if not any(
            _tail_match(found, want)
            for found, _ in value_unit.calls()
            for want in inner
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.add(target.id)
    return out


def _tail_match(recorded: str, declared: str) -> bool:
    """One dotted name is a tail of the other.

    ``random.uniform`` matches ``uniform`` and vice versa. Import style is a
    per-repository choice — ``import random`` beside ``from random import
    uniform`` — and it is not what this probe is measuring.
    """
    left = recorded.split(".")
    right = declared.split(".")
    width = min(len(left), len(right))
    return left[-width:] == right[-width:]


# --------------------------------------------------------------------------
# Evaluating a declared property
# --------------------------------------------------------------------------


def holds(prop: Property, unit: Unit) -> bool:
    """Does the symbol satisfy the property as declared, ``expect`` included?"""
    return _observed(prop, unit) == (prop.expect == "present")


def _observed(prop: Property, unit: Unit) -> bool:
    if prop.kind == "calls":
        return any(
            _tail_match(name, want) for name, _ in unit.calls() for want in prop.any_of
        )
    if prop.kind == "literal":
        seen = unit.literals()
        return any(want.strip().lower() in seen for want in prop.any_of)
    if prop.kind == "raises":
        return any(
            _tail_match(name, want) for name in unit.raised() for want in prop.any_of
        )
    if prop.kind == "handles":
        return any(
            _tail_match(name, want) for name in unit.caught() for want in prop.any_of
        )
    if prop.kind == "wraps":
        # Two shapes, and they are MUTUALLY EXCLUSIVE — see `_bound_to_inner`.
        # Evaluated PER ROOT: the binding and the outer call have to sit in the
        # same function, which is what they do at runtime.
        for root in unit.roots:
            root_unit = Unit(root, unit.aliases)
            bound = _bound_to_inner(root, unit.aliases, prop.inner)
            for name, call in root_unit.calls():
                if not _tail_match(name, prop.outer):
                    continue
                arguments = [*call.args, *(kw.value for kw in call.keywords)]
                for argument in arguments:
                    # (a) Lexical: the inner call sits inside the outer args.
                    inner = Unit(argument, unit.aliases)
                    for found, _ in inner.calls():
                        if any(_tail_match(found, want) for want in prop.inner):
                            return True
                    # (b) Name-bound: an argument mentions a local assigned from
                    #     an expression containing the inner call.
                    for node in ast.walk(argument):
                        if isinstance(node, ast.Name) and node.id in bound:
                            return True
        return False
    # Unreachable: `_property` rejects every other kind at load time.
    raise ManifestError(f"unknown property kind {prop.kind!r}")


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    severity: str
    template: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "template": self.template,
            "detail": self.detail,
        }


@dataclass
class Unverified:
    code: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "subject": self.subject, "detail": self.detail}


@dataclass
class Report:
    target: str
    templates: list[Template] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    unverified: list[Unverified] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unanimity: bool = True
    floor: int = DEFAULT_FLOOR
    ignored_facts: list[str] = field(default_factory=list)
    measured: bool = False
    harness_error: str = ""
    provenance: probe_provenance.Provenance | None = None

    @property
    def sites_declared(self) -> int:
        return sum(len(t.sites) for t in self.templates)

    @property
    def sites_read(self) -> int:
        return sum(1 for t in self.templates for s in t.sites if s.unit is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "probe": "reference-drift",
            "target": self.target,
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "unanimity": self.unanimity,
            "unanimity_floor": self.floor,
            "unanimity_ignored": list(self.ignored_facts),
            "coverage": {
                "sites_declared": self.sites_declared,
                "sites_read": self.sites_read,
            },
            "templates": [t.as_dict() for t in self.templates],
            "notes": list(self.notes),
            "findings": [f.as_dict() for f in self.findings],
            "unverified": [u.as_dict() for u in self.unverified],
            "harness_error": self.harness_error,
            "exit_code": self.exit_code(),
        }

    def exit_code(self) -> int:
        if self.provenance is not None and self.provenance.blocking:
            return probe_provenance.EXIT_MOVED
        if self.harness_error:
            return EXIT_CANNOT_RUN
        if self.findings:
            return EXIT_FINDINGS
        if not self.measured:
            return EXIT_NOT_MEASURED
        return EXIT_GREEN


# --------------------------------------------------------------------------
# Resolving a repository to a checkout on disk
# --------------------------------------------------------------------------


def resolve_repo(
    repo: str, roots: list[Path], explicit: dict[str, Path]
) -> Path | None:
    """Where ``owner/name`` is checked out, or ``None``.

    Nothing is fetched. A probe that clones is neither reproducible nor
    read-only, and it turns a network error into something that looks exactly
    like a repository nobody has checked out.
    """
    if repo in explicit:
        path = explicit[repo]
        return path if path.is_dir() else None
    name = repo.split("/")[-1]
    for root in roots:
        for candidate in (root / repo, root / name):
            if candidate.is_dir():
                return candidate
    return None


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run(
    target: Path,
    roots: list[Path] | None = None,
    explicit: dict[str, Path] | None = None,
    unanimity: bool = True,
    floor: int = DEFAULT_FLOOR,
) -> Report:
    report = Report(target=str(target), unanimity=unanimity, floor=floor)
    roots = list(roots or [])
    explicit = dict(explicit or {})

    if not target.is_dir():
        report.harness_error = f"{target} is not a directory"
        return report

    reference = target / REFERENCE_DIR
    if not reference.is_dir():
        report.notes.append(
            f"no {REFERENCE_DIR}/ directory in {target} — this repository ships no "
            "template, so nothing was measured. The run is not evidence that any "
            "shared code here is current"
        )
        return report

    shipped = sorted(p for p in reference.rglob("*.py") if p.name not in NOT_A_TEMPLATE)
    manifest = reference / MANIFEST_NAME
    if not manifest.is_file():
        if not shipped:
            report.notes.append(
                f"{REFERENCE_DIR}/ has no Python template and no {MANIFEST_NAME} — "
                "nothing was measured"
            )
            return report
        report.findings.append(
            Finding(
                code="MANIFEST_MISSING",
                severity="high",
                template=f"{REFERENCE_DIR}/",
                detail=(
                    f"{len(shipped)} template file(s) under {REFERENCE_DIR}/ and no "
                    f"{REFERENCE_DIR}/{MANIFEST_NAME}: "
                    + ", ".join(p.relative_to(target).as_posix() for p in shipped[:5])
                    + (" …" if len(shipped) > 5 else "")
                    + ". Without a declared mapping the blast radius of this template "
                    "is unknown, and no drift in either direction can be checked. "
                    "The probe stops here rather than guessing which repositories "
                    "copied it"
                ),
            )
        )
        return report

    try:
        templates, unmapped_ok, ignore = load_manifest(manifest)
    except ManifestError as exc:
        report.findings.append(
            Finding(
                code="MANIFEST_INVALID",
                severity="high",
                template=f"{REFERENCE_DIR}/{MANIFEST_NAME}",
                detail=str(exc),
            )
        )
        return report

    report.templates = templates
    report.ignored_facts = sorted(ignore)

    mapped = {t.file for t in templates}
    for path in shipped:
        rel = path.relative_to(target).as_posix()
        if rel in mapped or rel in unmapped_ok:
            continue
        report.findings.append(
            Finding(
                code="TEMPLATE_UNMAPPED",
                severity="high",
                template=rel,
                detail=(
                    f"`{rel}` is shipped as a template and no [[template]] in "
                    f"{MANIFEST_NAME} names it. It is copied by an unknown set of "
                    "repositories and drifts in both directions unobserved. Declare "
                    "its adopters, or list it under `unmapped_ok` with a reason"
                ),
            )
        )

    for template in templates:
        _resolve_template(report, target, template, roots, explicit)
        if template.unit is None:
            continue
        _compare_declared(report, template)
        if unanimity:
            _compare_unanimity(report, template, floor, set(ignore))

    report.measured = report.sites_read > 0
    if not report.measured and templates:
        # First, not last: the per-template notes below it are about checks that
        # did not run, and the reason they did not run has to be read before them.
        report.notes.insert(
            0,
            f"0 of {report.sites_declared} declared adoption site(s) could be read — "
            "no comparison happened. This is NOT a clean run; see the UNVERIFIED "
            "entries and point --repos-root at the checkouts",
        )
    elif report.sites_read < report.sites_declared:
        report.notes.append(
            f"coverage: {report.sites_read} of {report.sites_declared} declared "
            "adoption site(s) were read. The findings below stand for those; the "
            "sites listed as UNVERIFIED were not compared in either direction"
        )
    return report


def _resolve_template(
    report: Report,
    target: Path,
    template: Template,
    roots: list[Path],
    explicit: dict[str, Path],
) -> None:
    unit, reason = load_symbol(target / template.file, template.symbol)
    template.unit, template.unverified = unit, reason
    if unit is None:
        report.unverified.append(
            Unverified(
                code="REFERENCE_UNREADABLE",
                subject=template.name,
                detail=(
                    f"{reason}. The template itself could not be read, so neither "
                    "direction was compared for it — this is not evidence that its "
                    "adopters are in sync"
                ),
            )
        )
        return

    for site in template.sites:
        root = resolve_repo(site.repo, roots, explicit)
        if root is None:
            site.unverified = "no checkout on disk"
            report.unverified.append(
                Unverified(
                    code="REPO_NOT_ON_DISK",
                    subject=site.name,
                    detail=(
                        f"`{site.repo}` was not found under any --repos-root and has no "
                        "--repo-path. Nothing is fetched by design; this site was not "
                        "compared and must not be read as agreeing with the template"
                    ),
                )
            )
            continue
        site.path = root / site.file
        site.unit, site.unverified = load_symbol(site.path, site.symbol)
        if site.unit is None:
            report.unverified.append(
                Unverified(
                    code="SITE_UNREADABLE",
                    subject=site.name,
                    detail=(
                        f"{site.unverified}. The manifest maps this site explicitly, so "
                        f"the mapping itself may be stale — it was declared on "
                        f"{site.since} and the file or symbol has moved since"
                    ),
                )
            )


def _compare_declared(report: Report, template: Template) -> None:
    """The declared properties, against the template and against each site.

    The two halves have different prerequisites, and conflating them cost the
    probe its most important finding on a first run. Whether a SITE lags needs
    that site to have been read. Whether the TEMPLATE satisfies a property it
    itself declares needs nothing but the template — it is a claim the manifest
    makes about one file. Gating it on the checkouts meant that a run without
    them reported no declared finding at all, which is exactly the machine a new
    manifest is written on.
    """
    read = [s for s in template.sites if s.unit is not None]
    for prop in template.properties:
        # strict: `template.unit` is not None — `run` skips the template otherwise.
        reference_holds = holds(prop, template.unit)
        satisfying = [s for s in read if holds(prop, s.unit)]
        if not reference_holds:
            if not read:
                standing = (
                    "; no adoption site could be read, so how far the servers are "
                    "ahead of it was not measured"
                )
            elif satisfying:
                standing = (
                    f"; {len(satisfying)} of {len(read)} adoption site(s) read do: "
                    + ", ".join(s.repo for s in satisfying[:5])
                    + (" …" if len(satisfying) > 5 else "")
                )
            else:
                standing = (
                    f"; none of the {len(read)} adoption site(s) read does either — "
                    "the property is declared and implemented nowhere"
                )
            report.findings.append(
                Finding(
                    code="REFERENCE_STALE",
                    severity="high",
                    template=template.name,
                    detail=(
                        f"the template does not satisfy its own declared property "
                        f"`{prop.id}` ({prop.says})"
                        + standing
                        + ". Every repository that copies this template next "
                        "inherits the gap"
                    ),
                )
            )
            continue
        satisfied = {id(s) for s in satisfying}
        for site in read:
            if id(site) in satisfied:
                continue
            report.findings.append(
                Finding(
                    code="REFERENCE_UNADOPTED",
                    severity="medium",
                    template=template.name,
                    detail=(
                        f"`{site.repo}` — {site.file}::{site.symbol or '(module)'}, "
                        f"adopted {site.since} — does not satisfy `{prop.id}` "
                        f"({prop.says}), which the template does. The fix exists and "
                        "never arrived here"
                    ),
                )
            )


def _compare_unanimity(
    report: Report, template: Template, floor: int, ignore: set[str]
) -> None:
    """Facts every readable adoption site agrees on, where the template differs.

    Only ``REFERENCE_STALE`` comes out of here. A fact that some sites have and
    others do not is ordinary variation between repositories, and calling it a
    finding would be the guess this probe exists to avoid.
    """
    read = [s for s in template.sites if s.unit is not None]
    if len(read) < floor:
        report.notes.append(
            f"{template.name}: the unanimity layer needs {floor} readable adoption "
            f"site(s) and had {len(read)} — it did not run. Declared properties were "
            "still compared"
        )
        return

    # strict: `template.unit` is not None — `run` skips the template otherwise.
    reference = template.unit.facts()
    site_facts = [s.unit.facts() for s in read]
    everywhere = frozenset.intersection(*site_facts)
    anywhere = frozenset().union(*site_facts)

    for fact in sorted(everywhere - reference - ignore):
        report.findings.append(
            Finding(
                code="REFERENCE_STALE",
                severity="high",
                template=template.name,
                detail=(
                    f"convergent addition: all {len(read)} adoption site(s) read have "
                    f"`{fact}` and the template does not. Independently maintained "
                    "repositories do not agree by accident — this is a fix that was "
                    "made downstream and never came back. No property in "
                    f"{MANIFEST_NAME} describes it"
                ),
            )
        )
    for fact in sorted((reference - anywhere) - ignore):
        report.findings.append(
            Finding(
                code="REFERENCE_STALE",
                severity="high",
                template=template.name,
                detail=(
                    f"convergent removal: the template has `{fact}` and none of the "
                    f"{len(read)} adoption site(s) read still does. Every repository "
                    "that copied this removed it; the template hands it to the next "
                    f"one. No property in {MANIFEST_NAME} describes it"
                ),
            )
        )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def render(report: Report) -> str:
    lines = [f"reference-drift probe — {report.target}"]
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
        if report.provenance.blocking:
            lines.append(f"  {report.provenance.moved_detail()}")
            return "\n".join(lines)
    if report.harness_error:
        lines.append(f"  HARNESS: {report.harness_error}")
        return "\n".join(lines)
    if not report.templates and not report.findings:
        why = report.notes[0] if report.notes else "nothing to compare"
        lines.append(f"  NOT MEASURED: {why}")
        return "\n".join(lines)
    for template in report.templates:
        read = sum(1 for s in template.sites if s.unit is not None)
        lines.append(
            f"  {template.name}: {len(template.properties)} declared propert(ies), "
            f"{read}/{len(template.sites)} adoption site(s) read"
        )
    if report.templates:
        lines.append(
            f"  coverage: {report.sites_read}/{report.sites_declared} site(s); "
            f"unanimity {'off' if not report.unanimity else f'floor {report.floor}'}"
            + (
                f", ignoring {', '.join(report.ignored_facts)}"
                if report.ignored_facts
                else ""
            )
        )
    if report.templates and not report.measured:
        lines.append(
            "  NOT MEASURED: no declared adoption site could be read, so nothing was "
            "compared in either direction"
        )
    for note in report.notes:
        lines.append(f"  note: {note}")
    for item in report.unverified:
        lines.append(f"  UNVERIFIED {item.code} {item.subject}: {item.detail}")
    if not report.findings and report.measured:
        lines.append("  the template and every readable adoption site agree")
    for finding in report.findings:
        lines.append(
            f"  {finding.code} [{finding.severity}] {finding.template}: {finding.detail}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=".", help="path to the SKILL checkout")
    parser.add_argument(
        "--repos-root",
        action="append",
        default=[],
        metavar="DIR",
        help="directory holding the server checkouts, repeatable. Looked up as "
        "DIR/<owner>/<name> and DIR/<name>. Nothing is ever fetched",
    )
    parser.add_argument(
        "--repo-path",
        action="append",
        default=[],
        metavar="OWNER/NAME=PATH",
        help="an explicit checkout for one repository, repeatable",
    )
    parser.add_argument(
        "--floor",
        type=int,
        default=DEFAULT_FLOOR,
        help=f"readable adoption sites the unanimity layer needs (default: {DEFAULT_FLOOR})",
    )
    parser.add_argument(
        "--no-unanimity",
        action="store_true",
        help="compare only the properties declared in the manifest",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", default="", help="also write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()

    explicit: dict[str, Path] = {}
    for spec in args.repo_path:
        if "=" not in spec:
            print(
                f"reference-drift: --repo-path takes OWNER/NAME=PATH, got {spec!r}",
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN
        repo, path = spec.split("=", 1)
        explicit[repo.strip()] = Path(path).expanduser().resolve()
    if args.floor < 1:
        print("reference-drift: --floor must be at least 1", file=sys.stderr)
        return EXIT_CANNOT_RUN

    roots = [Path(root).expanduser().resolve() for root in args.repos_root]

    prov = probe_provenance.capture(target)
    report = run(
        target,
        roots=roots,
        explicit=explicit,
        unanimity=not args.no_unanimity,
        floor=args.floor,
    )
    report.provenance = prov.recheck()

    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
