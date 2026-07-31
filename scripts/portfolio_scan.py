#!/usr/bin/env python3
"""Portfolio fan-out — one cheap predicate across every server, as a MATRIX.

WHY THIS EXISTS
---------------
``nightly-audit.sh`` takes ONE target (``TARGET_REPO``) and goes deep. That is
the right shape for a nightly, and the wrong shape for the question this auditor
is actually good at: *does this failure class exist anywhere in the portfolio?*

The occasion was an SDK major migration across a dozen-odd servers. One nested
server fell out of every enumeration — it was not the root package of its
repository, so every list of "the servers" quietly omitted it — and was still
running the old API long after everything else had moved. No per-target report
would ever have shown that, because the finding is not in any single report. It
is in the ROW THAT BREAKS THE PATTERN, which only exists once the results are a
matrix.

So: N targets × M predicates -> a grid, plus an explicit outlier pass. The
outlier pass needs no configured expectation; a majority is enough. That matters,
because the migration's real question was never "is this target on version X",
it was "which one is not on the same version as the others".

WHAT A PREDICATE IS
-------------------
Deliberately small: greppable, or checkable in seconds against a shallow
checkout. It is NOT the nightly. ``boot`` — actually starting the server and
speaking MCP to it (scripts/transport_boot_probe.py) — is the one expensive
predicate, and it is opt-in per target rather than on by default.

Every cell carries one of five statuses, and the middle one is load-bearing:

  ok     the predicate held
  flag   a finding about that target
  note   observed, deliberately NOT a finding (e.g. a control that is simply not
         configured — see scripts/rebind_probe.py for why that is its own thing)
  na     the predicate does not apply here
  error  it could not be evaluated — the cell says so and the sweep CONTINUES

PARTIAL RESULTS ARE THE POINT
-----------------------------
A target that cannot be cloned must not take the fan-out with it. It becomes a
row of ``error`` cells with the reason attached, and the other fifteen targets
still produce their matrix. What an incomplete sweep must never do is report
"no findings": exit 1 covers that case, because "we did not look" and "we looked
and found nothing" are different claims.

EXIT CODES
----------
  0  every cell ok/note/na — the sweep completed and found nothing
  2  at least one flag: a real finding about at least one target
  1  the sweep is INCOMPLETE (any error cell) or the harness itself failed. An
     incomplete sweep cannot be summarised as "no findings", so this outranks 2
     — the report still lists every flag it did find.

COSTS — read scripts/budget_guard.py and docs/budget/guardrails.md
------------------------------------------------------------------
A fan-out multiplies. Today's predicates are deterministic and call no model, so
what they multiply is wall-clock, disk and network — N shallow clones, and with
``boot`` N server starts. The guard therefore gates the fan-out on its WIDTH
before it runs (``budget_guard.py preflight --fanout N``), rather than measuring
the damage afterwards. ``--budget-state`` wires that in; ``--no-budget`` opts out.

EGRESS — read deploy/microvm/forward-proxy/README.md
-----------------------------------------------------
N target repositories do NOT need N new proxy entries: they are all on GitHub,
which is already allowed. What DOES need adding is each target's own upstream
data origin — the allowlist currently names only Zürich's. See
``--print-egress`` for the list this targets file implies.

Stdlib only: it runs on the credential-free Worker, where a dependency is a
liability. PyYAML is used when present and a strict subset reader stands in when
it is not — the reader refuses anything outside the documented shape rather than
guessing at it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_INCOMPLETE = 1

OK, FLAG, NOTE, NA, ERROR = "ok", "flag", "note", "na", "error"
_ICON = {OK: "✅", FLAG: "🚨", NOTE: "🟡", NA: "–", ERROR: "⛔"}

DEFAULT_CLONE_TIMEOUT = 180
DEFAULT_PREDICATE_TIMEOUT = 120
DEFAULT_BOOT_TIMEOUT = 300

# Directories that are never a target's own source (mirrors transport_boot_probe).
_SKIP_DIRS = {
    ".git", ".venv", "venv", ".tox", "node_modules", "__pycache__", "dist",
    "build", ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages",
    ".audit", ".eggs",
}


# --------------------------------------------------------------------------
# the targets file
# --------------------------------------------------------------------------

class TargetsError(Exception):
    """The targets file could not be understood. Never guessed around."""


_FLOW_LIST_RE = re.compile(r"^\[(.*)\]$", re.DOTALL)


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside quotes."""
    out, quote = [], ""
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(raw: str) -> Any:
    v = raw.strip()
    if not v:
        return ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    flow = _FLOW_LIST_RE.match(v)
    if flow:
        inner = flow.group(1).strip()
        return [_scalar(p) for p in inner.split(",") if p.strip()] if inner else []
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _join_flow(lines: list[tuple[int, int, str]], i: int) -> tuple[str, int]:
    """A ``[...]`` flow list may wrap across lines; glue it back into one."""
    _, _, text = lines[i]
    if text.count("[") <= text.count("]"):
        return text, i
    buf = text
    j = i
    while j + 1 < len(lines) and buf.count("[") > buf.count("]"):
        j += 1
        buf += " " + lines[j][2].strip()
    return buf, j


def parse_targets_yaml(text: str) -> dict[str, Any]:
    """Parse the documented subset of YAML, or raise.

    PyYAML is used when it is importable; this reader is the stdlib stand-in for
    the Worker, where a dependency is a liability. It is deliberately STRICT: a
    construct outside the documented shape raises with the offending line rather
    than being quietly reinterpreted. A targets file that is silently
    mis-parsed drops servers from the sweep, which is the exact failure this
    whole module exists to prevent.
    """
    try:
        import yaml  # noqa: PLC0415
    except ModuleNotFoundError:
        pass
    else:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - surfaced as a targets error
            raise TargetsError(f"YAML could not be parsed: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise TargetsError("the targets file must be a mapping at the top level")
        return data

    lines: list[tuple[int, int, str]] = []
    for no, raw in enumerate(text.splitlines(), 1):
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise TargetsError(f"line {no}: tabs are not valid YAML indentation")
        lines.append((no, len(stripped) - len(stripped.lstrip()), stripped.strip()))

    def block(i: int, indent: int) -> tuple[Any, int]:
        if i >= len(lines):
            return {}, i
        if lines[i][2].startswith("- "):
            items: list[Any] = []
            while i < len(lines) and lines[i][1] == indent and lines[i][2].startswith("- "):
                no, _, content = lines[i]
                rest = content[2:].strip()
                if ":" in rest and not rest.startswith(("[", '"', "'")):
                    # `- key: value` opens a mapping whose further keys are the
                    # following lines indented past the dash.
                    item: dict[str, Any] = {}
                    joined, i = _join_flow(lines, i)
                    key, _, val = joined[2:].strip().partition(":")
                    item[key.strip()] = _scalar(val)
                    i += 1
                    inner = indent + 2
                    while i < len(lines) and lines[i][1] >= inner and not lines[i][2].startswith("- "):
                        sub, i = mapping(i, lines[i][1], stop_indent=indent)
                        item.update(sub)
                    items.append(item)
                    continue
                items.append(_scalar(rest))
                i += 1
            return items, i
        return mapping(i, indent, stop_indent=indent - 1)

    def mapping(i: int, indent: int, stop_indent: int = -1) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        while i < len(lines) and lines[i][1] > stop_indent:
            no, ind, content = lines[i]
            if ind < indent:
                break
            if ind > indent:
                raise TargetsError(f"line {no}: unexpected indentation")
            if content.startswith("- "):
                break
            if ":" not in content:
                raise TargetsError(f"line {no}: expected 'key: value', got {content!r}")
            joined, i = _join_flow(lines, i)
            key, _, val = joined.strip().partition(":")
            key = key.strip()
            if val.strip():
                out[key] = _scalar(val)
                i += 1
                continue
            i += 1
            if i < len(lines) and lines[i][1] > ind:
                out[key], i = block(i, lines[i][1])
            elif i < len(lines) and lines[i][1] == ind and lines[i][2].startswith("- "):
                out[key], i = block(i, ind)
            else:
                out[key] = None
        return out, i

    data, _ = mapping(0, 0)
    return data


@dataclass
class Target:
    repo: str
    ref: str = "main"
    path: str = ""                       # local checkout instead of a clone
    server_import: str = ""
    predicates: list[str] = field(default_factory=list)
    known_manifests: list[str] = field(default_factory=list)
    sdk_major_expect: str = ""

    @property
    def name(self) -> str:
        return self.repo or self.path


def _as_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TargetsError(f"{where}: expected a list, got {type(value).__name__}")


def load_targets(path: Path) -> tuple[list[Target], dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TargetsError(f"could not read {path}: {exc}") from exc

    data = parse_targets_yaml(text)
    if not isinstance(data, dict):
        raise TargetsError("the targets file must be a mapping at the top level")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise TargetsError("`defaults` must be a mapping")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise TargetsError("`targets` must be a non-empty list")

    out: list[Target] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_targets, 1):
        if not isinstance(entry, dict):
            raise TargetsError(f"targets[{i}]: expected a mapping")
        repo = str(entry.get("repo") or "").strip()
        local = str(entry.get("path") or "").strip()
        if not repo and not local:
            raise TargetsError(f"targets[{i}]: needs `repo` or `path`")
        if repo and not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            raise TargetsError(f"targets[{i}]: `repo` must be owner/name, got {repo!r}")
        key = repo or local
        if key in seen:
            # A duplicate silently halves the sweep's coverage of one target while
            # the matrix still shows a full-looking row.
            raise TargetsError(f"targets[{i}]: {key} is listed twice")
        seen.add(key)
        out.append(Target(
            repo=repo,
            path=local,
            ref=str(entry.get("ref") or defaults.get("ref") or "main"),
            server_import=str(entry.get("server_import")
                              or defaults.get("server_import") or ""),
            predicates=_as_list(entry.get("predicates") or defaults.get("predicates"),
                                f"targets[{i}].predicates") or list(DEFAULT_PREDICATES),
            known_manifests=_as_list(entry.get("known_manifests"),
                                     f"targets[{i}].known_manifests"),
            sdk_major_expect=str(entry.get("sdk_major_expect")
                                 or defaults.get("sdk_major_expect") or ""),
        ))
    return out, defaults


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------

@dataclass
class Cell:
    status: str
    value: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "value": self.value, "detail": self.detail}


@dataclass
class Ctx:
    target: Target
    root: Path
    timeout: float


def _iter_source(root: Path, patterns: tuple[str, ...], cap: int = 600) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        for p in root.glob(pattern):
            if len(found) >= cap:
                return found
            if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
                found.append(p)
    return found


def _read_pyproject(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def pred_manifest(ctx: Ctx) -> Cell:
    """Does the target have a readable root manifest naming itself?"""
    py = _read_pyproject(ctx.root)
    project = py.get("project") if isinstance(py.get("project"), dict) else {}
    name = str(project.get("name") or "")
    version = str(project.get("version") or "")
    if name and version:
        return Cell(OK, f"{name} {version}")
    if (ctx.root / "package.json").is_file():
        try:
            pkg = json.loads((ctx.root / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Cell(FLAG, "unreadable", "package.json is present but unparseable")
        return Cell(OK, f"{pkg.get('name', '?')} {pkg.get('version', '?')}")
    if not py:
        return Cell(FLAG, "none", "no readable pyproject.toml or package.json at the root")
    return Cell(FLAG, "incomplete", "the root manifest names no project name/version")


_DEP_RE = re.compile(r"^(mcp|fastmcp)\b(?P<spec>.*)$", re.IGNORECASE)
_MAJOR_RE = re.compile(r"(\d+)")


def _sdk_major(spec: str) -> str:
    """The major a constraint pins. `>=2.0,<3` -> 2; `>=1.2` -> 1; `==2.3.1` -> 2."""
    for token in re.split(r",", spec):
        token = token.strip()
        if token.startswith((">=", "==", "~=", "^", ">")):
            m = _MAJOR_RE.search(token)
            if m:
                return m.group(1)
    m = _MAJOR_RE.search(spec)
    return m.group(1) if m else ""


def pred_sdk_major(ctx: Ctx) -> Cell:
    """Which MCP SDK major this target pins.

    The migration predicate. Note it does not need `sdk_major_expect` to be set
    to be useful: the outlier pass finds the odd one out from the majority, which
    is the question actually being asked when a portfolio is mid-migration.
    """
    py = _read_pyproject(ctx.root)
    project = py.get("project") if isinstance(py.get("project"), dict) else {}
    deps: list[str] = []
    for dep in project.get("dependencies") or []:
        deps.append(str(dep))
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            deps.extend(str(d) for d in (group or []))

    for dep in deps:
        m = _DEP_RE.match(dep.strip())
        if not m:
            continue
        major = _sdk_major(m.group("spec"))
        if not major:
            return Cell(NOTE, "unpinned",
                        f"{dep.strip()!r} names no version — the major is whatever "
                        "resolved last, which is how a portfolio drifts apart")
        want = ctx.target.sdk_major_expect
        if want and major != want:
            return Cell(FLAG, major,
                        f"pins SDK major {major}, the portfolio expects {want} "
                        f"({dep.strip()!r})")
        return Cell(OK, major, dep.strip())
    return Cell(NA, "", "no mcp/fastmcp dependency in the root manifest")


_SETTINGS_WRITE_RE = re.compile(r"\.settings\.([A-Za-z_]\w*)\s*=(?!=)")


def pred_settings_write(ctx: Ctx) -> Cell:
    """Assignment to a settings attribute — the crash-at-start from parlament-mcp#29.

    After the SDK major bump the settings object is read-only, so a surviving
    ``mcp.settings.host = ...`` raises at import and the process never comes up.
    Greppable, which is exactly what makes it a good portfolio predicate: the
    boot probe finds it too, but this finds it in a second across fifteen repos.
    """
    hits: list[str] = []
    for path in _iter_source(ctx.root, ("**/*.py",)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            m = _SETTINGS_WRITE_RE.search(line)
            if m:
                hits.append(f"{path.relative_to(ctx.root).as_posix()}:{no} (.{m.group(1)})")
        if len(hits) >= 5:
            break
    if hits:
        return Cell(FLAG, f"{len(hits)} write(s)", "; ".join(hits[:5]))
    return Cell(OK, "none")


def pred_host_allowlist_knob(ctx: Ctx) -> Cell:
    """Does the target ship an inbound Host allow-list knob at all?

    Reuses the rebinding gate's detector. Absence is a NOTE, not a flag — the
    fail-open default is a documented deployment state and not a defect (see
    scripts/rebind_probe.py). In a matrix it is still exactly what you want to
    see: which servers in the portfolio have the control and which do not.
    """
    try:
        import rebind_probe
    except Exception as exc:  # noqa: BLE001 - the predicate, not the sweep, fails
        return Cell(ERROR, "", f"rebind_probe unavailable: {type(exc).__name__}: {exc}")
    knob = rebind_probe.detect_knob(ctx.root)
    if knob.advertised:
        return Cell(OK, ",".join(knob.names))
    return Cell(NOTE, "absent",
                "no inbound allow-list variable named anywhere — the documented "
                "fail-open state, not a defect")


_MANIFEST_NAMES = ("pyproject.toml", "package.json")


def _looks_like_server(path: Path) -> str:
    """Why we think a nested manifest is a server, or "" if we do not."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return ""
    if re.search(r"\b(mcp|fastmcp|@modelcontextprotocol)\b", text, re.IGNORECASE):
        return "its manifest depends on an MCP SDK"
    sibling = path.parent
    for candidate in ("server.py", "src", "main.py", "index.js"):
        if (sibling / candidate).exists() and any(
            re.search(r"\bmcp\b", p.name, re.IGNORECASE) for p in sibling.iterdir()
        ):
            return "a server-shaped module sits beside it"
    return ""


def pred_nested_manifests(ctx: Ctx) -> Cell:
    """Manifests below the root that no target entry claims.

    THIS IS THE ONE THE OCCASION IS ABOUT. A server that is not the root package
    of its repository is invisible to every list written by hand, and that is
    precisely how one was left on the old SDK long after the migration finished.

    Fail-closed on purpose: EVERY undeclared manifest is flagged, whether or not
    it looks like a server. A heuristic that only flagged the server-shaped ones
    would let through exactly the one that does not match the heuristic — which
    is the same bet that lost the first time. Acknowledging a manifest in
    `known_manifests` costs one line and makes the omission deliberate.
    """
    known = {m.strip("/ ") for m in ctx.target.known_manifests}
    found: list[str] = []
    for name in _MANIFEST_NAMES:
        for path in _iter_source(ctx.root, (f"**/{name}",)):
            rel = path.relative_to(ctx.root).as_posix()
            if rel in (name,) or rel in known:
                continue
            why = _looks_like_server(path)
            found.append(f"{rel}{' — ' + why if why else ''}")
    if not found:
        return Cell(OK, "none")
    return Cell(FLAG, f"{len(found)} unclaimed",
                "; ".join(sorted(found)[:6])
                + " — add each as its own target, or acknowledge it in "
                  "`known_manifests` if it is not a server")


def pred_boot(ctx: Ctx) -> Cell:
    """The expensive one: actually start the server and speak MCP to it.

    Opt-in per target. Runs the transport boot gate (scripts/transport_boot_probe.py)
    against the checkout and folds its 0/2/127 contract into a cell — a target
    that will not start is a FLAG about that target, a harness failure is an
    ERROR in that cell only, and neither ends the sweep.
    """
    script = Path(__file__).resolve().parent / "transport_boot_probe.py"
    if not script.is_file():
        return Cell(ERROR, "", "transport_boot_probe.py is missing")
    env = dict(os.environ)
    env["BOOT_TARGET_ROOT"] = str(ctx.root)
    if ctx.target.server_import:
        env["MCP_SERVER_IMPORT"] = ctx.target.server_import
    env.setdefault("BOOT_TIMEOUT", "30")
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], cwd=str(ctx.root), env=env,
            capture_output=True, text=True, timeout=ctx.timeout, check=False)
    except subprocess.TimeoutExpired:
        # Task 3's lesson, one level down: a hang is its own answer, not noise.
        return Cell(ERROR, "hung", f"the boot probe did not finish within {ctx.timeout:.0f}s")
    except OSError as exc:
        return Cell(ERROR, "", f"{type(exc).__name__}: {exc}")
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    detail = tail[-1][:200] if tail else ""
    if proc.returncode == 0:
        return Cell(OK, "boots", detail)
    if proc.returncode == 2:
        return Cell(FLAG, "does not boot", detail)
    return Cell(ERROR, f"rc {proc.returncode}", detail)


PREDICATES: dict[str, Callable[[Ctx], Cell]] = {
    "manifest": pred_manifest,
    "sdk_major": pred_sdk_major,
    "settings_write": pred_settings_write,
    "host_allowlist_knob": pred_host_allowlist_knob,
    "nested_manifests": pred_nested_manifests,
    "boot": pred_boot,
}
DEFAULT_PREDICATES = ("manifest", "sdk_major", "settings_write",
                      "host_allowlist_knob", "nested_manifests")
_EXPENSIVE = {"boot"}


# --------------------------------------------------------------------------
# checkout
# --------------------------------------------------------------------------

def checkout(target: Target, workdir: Path, timeout: float) -> tuple[Path | None, str]:
    """A shallow checkout of one target, or (None, reason).

    Read-only against the target, like every other provisioning path here. The
    reason string is what lands in the row's error cells — a whole row of
    "could not run" is a legitimate result, and the sweep continues.
    """
    if target.path:
        root = Path(target.path).expanduser().resolve()
        if not root.is_dir():
            return None, f"local path {root} does not exist"
        return root, ""
    dest = workdir / target.repo.replace("/", "__")
    url = f"https://github.com/{target.repo}.git"
    cmd = ["git", "clone", "--quiet", "--depth", "1", "--branch", target.ref, url, str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, f"clone timed out after {timeout:.0f}s"
    except OSError as exc:
        return None, f"could not run git: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, f"clone failed (rc {proc.returncode}): {(proc.stderr or '').strip()[:200]}"
    return dest, ""


# --------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------

@dataclass
class Row:
    target: Target
    cells: dict[str, Cell] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target.name, "ref": self.target.ref,
                "error": self.error,
                "cells": {k: v.as_dict() for k, v in self.cells.items()}}


def scan_target(target: Target, workdir: Path, predicates: list[str],
                clone_timeout: float, predicate_timeout: float,
                boot_timeout: float) -> Row:
    row = Row(target=target)
    root, reason = checkout(target, workdir, clone_timeout)
    if root is None:
        # THE PARTIAL-RESULT RULE: one unreachable target is a row of error
        # cells, never the end of the sweep.
        row.error = reason
        for name in predicates:
            row.cells[name] = Cell(ERROR, "no checkout", reason)
        return row

    for name in predicates:
        fn = PREDICATES.get(name)
        if fn is None:
            row.cells[name] = Cell(ERROR, "", f"unknown predicate {name!r}")
            continue
        budget = boot_timeout if name in _EXPENSIVE else predicate_timeout
        try:
            row.cells[name] = fn(Ctx(target=target, root=root, timeout=budget))
        except Exception as exc:  # noqa: BLE001 - one predicate, one cell
            row.cells[name] = Cell(ERROR, "", f"{type(exc).__name__}: {exc}")
    return row


def outliers(rows: list[Row], predicates: list[str]) -> list[dict[str, Any]]:
    """Per predicate: the values a MINORITY of targets carry.

    This is the whole point of a matrix over N reports, and it needs no
    configured expectation — during a migration nobody knows which version is
    "right" until they see that fourteen repos agree and one does not. Only
    considers cells that were actually evaluated; an error cell is not a
    dissenting opinion, it is a missing one.
    """
    out: list[dict[str, Any]] = []
    for name in predicates:
        values = [(r, r.cells[name].value) for r in rows
                  if name in r.cells and r.cells[name].status != ERROR]
        if len(values) < 3:
            continue  # with two targets there is no majority to break
        counts = Counter(v for _, v in values)
        top, top_n = counts.most_common(1)[0]
        if top_n == len(values) or top_n * 2 <= len(values):
            continue  # unanimous, or no clear majority to deviate from
        deviants = [{"target": r.target.name, "value": v} for r, v in values if v != top]
        if deviants:
            out.append({"predicate": name, "majority": top, "majority_count": top_n,
                        "of": len(values), "deviants": deviants})
    return out


def classify(rows: list[Row]) -> tuple[str, int]:
    flags = any(c.status == FLAG for r in rows for c in r.cells.values())
    errors = any(c.status == ERROR for r in rows for c in r.cells.values())
    if errors:
        # Outranks findings on purpose: "we did not look" and "we looked and
        # found nothing" are different claims, and only one of them is a sweep.
        return "incomplete", EXIT_INCOMPLETE
    if flags:
        return "findings", EXIT_FINDINGS
    return "green", EXIT_GREEN


def render(rows: list[Row], predicates: list[str], outs: list[dict[str, Any]],
           outcome: str) -> str:
    head = {
        "green": "✅ No findings across the portfolio.",
        "findings": "🚨 Findings — see the flagged cells.",
        "incomplete": "⛔ The sweep is INCOMPLETE — some cells could not be "
                      "evaluated. Findings below are real; absence of others is not.",
    }[outcome]
    lines = ["# Portfolio scan", "", head, "",
             f"{len(rows)} target(s) × {len(predicates)} predicate(s)", ""]

    lines.append("| target | " + " | ".join(predicates) + " |")
    lines.append("|---" * (len(predicates) + 1) + "|")
    for r in rows:
        cells = []
        for name in predicates:
            c = r.cells.get(name)
            cells.append("–" if c is None
                         else f"{_ICON.get(c.status, '?')} {c.value or c.status}")
        lines.append(f"| `{r.target.name}` | " + " | ".join(cells) + " |")

    if outs:
        lines += ["", "## Out of line", "",
                  "The reason this is a matrix and not N reports — a value only "
                  "one or two targets carry:"]
        for o in outs:
            names = ", ".join(f"`{d['target']}` = {d['value'] or '∅'}" for d in o["deviants"])
            lines.append(f"- **{o['predicate']}**: {o['majority_count']}/{o['of']} targets "
                         f"say `{o['majority'] or '∅'}` — {names}")

    flagged = [(r, n, c) for r in rows for n, c in r.cells.items() if c.status == FLAG]
    if flagged:
        lines += ["", "## 🚨 Flagged"]
        for r, n, c in flagged:
            lines.append(f"- `{r.target.name}` / **{n}** — {c.value}: {c.detail}")

    noted = [(r, n, c) for r in rows for n, c in r.cells.items() if c.status == NOTE]
    if noted:
        lines += ["", "## 🟡 Noted (not findings)"]
        for r, n, c in noted:
            lines.append(f"- `{r.target.name}` / **{n}** — {c.value}: {c.detail}")

    broken = [(r, n, c) for r in rows for n, c in r.cells.items() if c.status == ERROR]
    if broken:
        lines += ["", "## ⛔ Could not run"]
        for r, n, c in broken:
            lines.append(f"- `{r.target.name}` / **{n}** — {c.detail}")
        lines += ["",
                  "These cells are empty, not green. A predicate that could not be "
                  "evaluated says nothing about that target."]
    return "\n".join(lines) + "\n"


def egress_origins(targets: list[Target]) -> list[str]:
    """The proxy entries a targets file implies, for --print-egress."""
    hosts = ["(^|\\.)github\\.com$  # every target repo — ONE entry covers all N"]
    if any(t.repo for t in targets):
        hosts.append("(^|\\.)codeload\\.github\\.com$  # git clone data path")
    return hosts


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------

def budget_preflight(state: str, fanout: int, expensive: int) -> tuple[bool, str]:
    """Ask budget_guard whether a sweep this wide may run. See guardrails.md."""
    guard = Path(__file__).resolve().parent / "budget_guard.py"
    if not guard.is_file():
        return True, "budget_guard.py not present — proceeding unbounded"
    cmd = [sys.executable, str(guard), "--state", state, "preflight",
           "--target", "portfolio", "--fanout", str(fanout),
           "--fanout-expensive", str(expensive)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"budget preflight could not run: {type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""
    return False, (proc.stdout or proc.stderr or "").strip()[:400]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--targets", default="targets.yaml")
    p.add_argument("--predicates", default="",
                   help="comma-separated override for every target")
    p.add_argument("--only", default="", help="comma-separated repo/path substrings")
    p.add_argument("--report", default="", help="write the machine-readable matrix here")
    p.add_argument("--workdir", default="", help="checkout dir (default: a temp dir)")
    p.add_argument("--clone-timeout", type=float, default=DEFAULT_CLONE_TIMEOUT)
    p.add_argument("--predicate-timeout", type=float, default=DEFAULT_PREDICATE_TIMEOUT)
    p.add_argument("--boot-timeout", type=float, default=DEFAULT_BOOT_TIMEOUT)
    p.add_argument("--budget-state", default=os.environ.get("BUDGET_STATE", ""),
                   help="gate the sweep's WIDTH through budget_guard.py preflight")
    p.add_argument("--no-budget", action="store_true",
                   help="skip the budget preflight (know why before you use it)")
    p.add_argument("--list-predicates", action="store_true")
    p.add_argument("--print-egress", action="store_true",
                   help="print the proxy allowlist entries this targets file implies")
    args = p.parse_args(argv)

    if args.list_predicates:
        for name, fn in PREDICATES.items():
            mark = "  (expensive, opt-in)" if name in _EXPENSIVE else ""
            first = (fn.__doc__ or "").strip().splitlines()[0]
            print(f"{name:22s} {first}{mark}")
        return EXIT_GREEN

    try:
        targets, _ = load_targets(Path(args.targets))
    except TargetsError as exc:
        print(f"portfolio: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE

    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        targets = [t for t in targets if any(w in t.name for w in wanted)]
        if not targets:
            print("portfolio: --only matched no target", file=sys.stderr)
            return EXIT_INCOMPLETE

    override = [s.strip() for s in args.predicates.split(",") if s.strip()]
    if override:
        for t in targets:
            t.predicates = list(override)

    if args.print_egress:
        for line in egress_origins(targets):
            print(line)
        print("\n# Each target's own UPSTREAM data origin still needs its own entry —")
        print("# see deploy/microvm/forward-proxy/README.md. GitHub is already allowed;")
        print("# the data endpoints the servers actually call are not.")
        return EXIT_GREEN

    expensive = sum(1 for t in targets for n in t.predicates if n in _EXPENSIVE)
    if args.budget_state and not args.no_budget:
        allowed, note = budget_preflight(args.budget_state, len(targets), expensive)
        print(f"==> budget: {note}", file=sys.stderr)
        if not allowed:
            # A refused sweep is never a green sweep.
            print("portfolio: refused by the budget guard — not run", file=sys.stderr)
            return EXIT_INCOMPLETE

    tmp: tempfile.TemporaryDirectory | None = None
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="portfolio-scan-")
        workdir = Path(tmp.name)

    try:
        rows: list[Row] = []
        for t in targets:
            print(f"==> {t.name} @ {t.ref}", file=sys.stderr)
            rows.append(scan_target(t, workdir, t.predicates, args.clone_timeout,
                                    args.predicate_timeout, args.boot_timeout))
        columns: list[str] = []
        for t in targets:
            for n in t.predicates:
                if n not in columns:
                    columns.append(n)

        outs = outliers(rows, columns)
        outcome, exit_code = classify(rows)
        print(render(rows, columns, outs, outcome))

        if args.report:
            try:
                Path(args.report).write_text(json.dumps({
                    "schema": 1, "outcome": outcome, "exit_code": exit_code,
                    "predicates": columns, "outliers": outs,
                    "targets": [r.as_dict() for r in rows],
                }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"portfolio: could not write {args.report}: {exc}", file=sys.stderr)
        return exit_code
    finally:
        if tmp is not None and shutil.which("git"):
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
