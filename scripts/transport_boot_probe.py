#!/usr/bin/env python3
"""Transport boot gate — start the target under every configured transport and
actually speak MCP to it.

WHY THIS GATE EXISTS
--------------------
The worst class of bug found in the portfolio so far was invisible to every
existing gate: unit tests green, ruff green, schema gate green — and the server
did not come up at all under HTTP transport. Two real cases:

  1. After the SDK major bump the settings object is read-only. The old line
     ``mcp.settings.host = ...`` raises ``ValueError: "Settings" object has no
     field "host"`` at start. The process never comes up.
     (malkreide/parlament-mcp#29)
  2. When ``host`` is not passed through to the app builder, the SDK derives its
     inbound host allow-list from the default ``127.0.0.1`` and answers every
     request carrying a real hostname on a 0.0.0.0 deployment with HTTP 421.
     The process runs and is completely unusable.

Neither is reachable by importing the module and asserting on it. The only way
to see them is to boot the thing and talk to it. That is all this script does:
for every transport the target actually configures, start it, run a real JSON-RPC
``initialize``, then ``tools/list``, and report what happened.

HOW THE TARGET IS STARTED (this is the part that decides whether case 2 is
visible at all)
----------------------------------------------------------------------------
If we boot the server ourselves via ``mcp.run(host=...)`` we pass ``host``
correctly *on the target's behalf* — and walk straight past the bug, which lives
in the target's own startup code. So we prefer the target's OWN entrypoint, in
this precedence:

  ``declared``    an explicit ``[tool.mcp_auditor.boot]`` table in the target's
                  pyproject.toml gives the argv per transport. Most faithful and
                  fully deterministic — the recommended thing for a target to ship.
  ``entrypoint``  a ``[project.scripts]`` console script, or ``python -m <pkg>``
                  when the package has a ``__main__``. Transport/host/port are
                  handed over through the usual env vars.
  ``generic``     last resort: import the server object (``MCP_SERVER_IMPORT``)
                  and call ``run()`` on it. This still catches case 1 (a crash at
                  start) but only PARTIALLY covers case 2, because here *we* are
                  the caller that passes ``host``. The mode is stamped into the
                  report for exactly this reason — a green ``generic`` HTTP result
                  is weaker evidence than a green ``entrypoint`` one.

HOW HTTP IS PROBED (case 2)
---------------------------
The server is bound to ``0.0.0.0`` — a real deployment, not a loopback-only one —
and then probed TWICE over the same connection target (127.0.0.1, because there is
no DNS for an invented name), varying only the ``Host:`` header:

  * first with a loopback ``Host`` — this must work, otherwise the transport is
    simply broken and we say so;
  * then with a non-loopback name (``BOOT_HTTP_HOST``) — a 421 here, when the
    loopback probe passed, is the exact signature of case 2 and nothing else.

Running both is what keeps the finding diagnostic instead of alarming: a target
that legitimately pins its allowed hosts fails both, and is reported differently.

NOTE — the OTHER polarity lives in scripts/rebind_probe.py. Here a rejection is
the BUG (nobody configured an allow-list, so rejecting a real hostname can only
mean the host never reached the app builder). There a rejection is the CONTROL
WORKING, because that gate configures an allow-list first and then tries to walk
past it. The two never disagree about the same server: this gate boots without
any allow-list variable set, so a target that honours one is untouched by it. A
target with a HARDCODED non-loopback allow-list is the one shape that trips this
gate while the rebinding gate calls it healthy — read both reports together.

THE STDIO TRAP
--------------
stdin is held OPEN until every response has been read. Closing it right after
writing the request makes the server shut down before network-bound calls finish,
and you measure a failure that does not exist. ``probe_stdio`` takes an internal
``_close_stdin_early`` knob for no other reason than to let
``tests/test_transport_boot_probe.py`` demonstrate that exact failure.

EXIT CODE — the gate contract (see nightly-audit.sh / nightly_audit_report.py)
-----------------------------------------------------------------------------
  0    every configured transport came up and answered initialize + tools/list
  2    FINDING: a transport did not come up, crashed, timed out, 421'd, or
       answered wrongly — and also the fail-closed case where the target's server
       could not be located at all (mirrors the schema gate: absence is a finding,
       not a pass). Switch the gate off with BOOT_GATE=off if a target genuinely
       cannot be booted here.
  4    MOVED_DURING_RUN: HEAD or the working tree changed between deriving the
       launch plan and judging the boot, so the plan and the verdict are about
       two different trees. Outranks 2 — a boot judged against a tree it was not
       launched from must not be charged to the target. See `probe_provenance`.
  3    NOT MEASURED: the entrypoint exited cleanly without listening and no
       transport flag got it to serve, so this gate never managed to ASK for that
       transport. Neither a pass nor a finding — the same shape as the rebinding
       gate's "control not configured". Fix it in the target with a
       [tool.mcp_auditor.boot.commands] entry. A real failure outranks this: if
       anything genuinely did not come up, the exit code is 2.
  127  the HARNESS could not run (an internal error in this script). Only this is
       a HARD failure — a target that will not start is a finding about the
       target, never about the infrastructure. Do not blur those two.

HOW A TRANSPORT IS REQUESTED, and why 3 exists
----------------------------------------------
The gate asks for a transport through env vars (MCP_TRANSPORT, FASTMCP_TRANSPORT,
PORT, ...) and, for non-declared launches, then tries the common CLI spellings
(``--http --port N``, ``--transport http --port N``, ...). Only the FIRST attempt
— the target's own argv plus the env — decides the verdict; the flag attempts are
chances to succeed and never chances to fail, because an argparse error from a
guess says something about the guess, not the target.

Measured against zurich-opendata-mcp: it selects HTTP with ``--http`` and reads
none of those env vars, so the entrypoint ran stdio, found stdin closed, and
exited rc 0. The gate used to call that "the server never came up" — a finding
against a target whose HTTP transport is in fact healthy.

Stdlib only (subprocess/socket/http.client) — it runs inside the TARGET's
environment, where we must not add dependencies.

SPEC 2026-07-28 — A REFUSED HANDSHAKE IS NOT A BROKEN SERVER
------------------------------------------------------------
The stateless core removes `initialize`/`initialized` and `Mcp-Session-Id`. A
server that has migrated answers `initialize` with JSON-RPC -32601, and this gate
used to call that "the server never came up" — a FAIL that travels through
`nightly_audit_report.py` into `sync_findings_issues.py` and ends as a GitHub
issue against a healthy target. The first server to finish the migration would
have been issued a bug report for finishing it.

So a rejected handshake now triggers the SECOND question rather than a verdict:
can the server serve a real call with no handshake at all? If it can, the result
is `STATELESS` — a pass with a label. If it cannot, the original failure stands.
Only -32601 (or a "method not found" message) takes this branch; an internal
error or a crash keeps failing, because those are what the gate is for.

The version the server names in its `initialize` result is now READ (see
`negotiated_version`) instead of discarded, and lands in the report. That
measurement was arriving all along; nothing looked at it, which is why no report
here could say which spec a target speaks. `scripts/spec_probe.py` is what
compares it against the other places the version is claimed.

Env:
  MCP_PROTOCOL_VERSION  the protocol version this gate SENDS (default 2025-06-18)
  MCP_SERVER_IMPORT  "package.module:attr" for the generic launcher
                     (default "server:mcp"; nightly-audit.sh always sets it)
  BOOT_TARGET_ROOT   target checkout to derive from (default: cwd)
  BOOT_TRANSPORTS    explicit comma-separated override; suppresses derivation
                     AND the floor, so it is the way to probe exactly one
  BOOT_TIMEOUT       hard per-transport-attempt deadline in seconds (default 30)
  BOOT_HTTP_HOST     the non-loopback Host header used for the case-2 probe
                     (default "mcp-boot-probe.audit.invalid")
  BOOT_BIND_HOST     what the server is told to bind (default "0.0.0.0")
  BOOT_HTTP_PATHS    comma-separated endpoint paths to try for streamable-http
                     (default "/mcp/,/mcp,/")
  BOOT_SSE_PATHS     ditto for sse (default "/sse/,/sse")
  BOOT_REPORT        write the machine-readable per-transport detail here

NOTE ON WHAT REACHES THE BROKER: in the microVM rollout the channel ships exactly
two files by name (nightly-evidence.json, promptfoo.json). BOOT_REPORT is NOT one
of them — it stays in the Worker's logs. What the Broker classifies on is the
integer exit code carried in the evidence's `gates` object. Keep the contract in
the exit code, not in this report.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

# Gate contract — deliberately the same numbers as the auditor's own classifier.
EXIT_GREEN = 0
EXIT_FINDINGS = 2
# The transport could not be SELECTED — see NOT_SELECTED. Its own code, mirroring
# the rebinding gate's "control not configured": neither a pass nor a finding.
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

# Per-transport outcomes.
OK = "ok"
FAIL = "fail"
# "We never managed to ask this entrypoint to serve that transport" — which is a
# different statement from "the server does not come up", and the gate used to
# conflate them. Measured against zurich-opendata-mcp: its entrypoint selects
# HTTP with a `--http` FLAG, not the env vars this probe sets, so it ran stdio,
# found stdin closed, and exited rc 0. The gate reported "the server never came
# up" — a claim about the target that nothing had established. Its HTTP transport
# is in fact healthy.
#
# The discriminator is the exit code of a process that never listened:
#   rc != 0  it tried and died          -> FAIL (case 1, a real finding)
#   rc == 0  it ran something else and finished cleanly -> NOT_SELECTED
# A crash leaves a non-zero status and a traceback; a clean exit after being asked
# for the wrong transport does not.
NOT_SELECTED = "not-selected"
# The server came up and answered MCP, but WITHOUT a handshake — spec 2026-07-28
# removed `initialize`/`initialized` and `Mcp-Session-Id` in favour of a stateless
# core. This is a PASS with a label, not a finding.
#
# Before this existed, a correctly migrated server produced `FAIL` here: the gate
# sent `initialize`, got a rejection, and reported "the server never came up" —
# which travels through nightly_audit_report.py into sync_findings_issues.py and
# ends as a GitHub issue against a healthy target. A migration in which the
# auditor files a bug report against the first server to finish it is worse than
# no auditor. The discriminator is the same shape as NOT_SELECTED above: ask the
# second question before concluding from the first.
STATELESS = "stateless"

STDIO = "stdio"
STREAMABLE_HTTP = "streamable-http"
SSE = "sse"

# The floor: a FastMCP server can always be served over stdio and streamable-http,
# so both are probed even when the target's config only names one. Case 1 and 2
# both hide in a transport nobody thought to exercise — probing only what the
# target advertises would reproduce exactly that blind spot. An explicit
# BOOT_TRANSPORTS suppresses the floor when an operator knows better.
FLOOR_TRANSPORTS = (STDIO, STREAMABLE_HTTP)

DEFAULT_TIMEOUT = 30
DEFAULT_HTTP_HOST = "mcp-boot-probe.audit.invalid"
DEFAULT_BIND_HOST = "0.0.0.0"

# The protocol version this gate SENDS. It used to be a bare literal, which is
# the exact defect class `identity_probe` was written to catch — a hand-maintained
# version that drifts while nothing breaks and no test fails — sitting in the
# auditor's own source, fanned out through shipped_probe and rebind_probe into
# three gates. It is now overridable, and (more to the point) the version the
# server ANSWERS is read back rather than discarded; see `negotiated_version`.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_PROTOCOL_VERSION = os.environ.get("MCP_PROTOCOL_VERSION") or DEFAULT_PROTOCOL_VERSION
_CLIENT_INFO = {"name": "mcp-continuous-auditor transport-boot-probe", "version": "1"}

# Directories that are never part of a target's own source.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "site-packages",
    ".audit",
    ".eggs",
}
_MAX_SCAN_FILES = 400
_MAX_SCAN_BYTES = 512_000


# --------------------------------------------------------------------------
# transport derivation — read the target's config, do not guess
# --------------------------------------------------------------------------


def normalise_transport(raw: str) -> str | None:
    """Map the many spellings of a transport onto our three canonical names."""
    v = str(raw or "").strip().strip("\"'").lower().replace("_", "-")
    if v in ("stdio", "std-io"):
        return STDIO
    if v in ("http", "streamable-http", "streamablehttp", "http-stream"):
        return STREAMABLE_HTTP
    if v == "sse":
        return SSE
    return None


_SOURCE_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"""transport\s*=\s*["']([A-Za-z_-]+)["']"""), ""),  # captured
    # Matches `--transport http`, `--transport=http` and the JSON-array CMD form
    # a Dockerfile uses: ["serve", "--transport", "http"].
    (re.compile(r"""--transport["']?[=,\s]*["']?([A-Za-z_-]+)"""), ""),
    (re.compile(r"""MCP_TRANSPORT\s*[=:]\s*["']?([A-Za-z_-]+)"""), ""),
    (re.compile(r"""FASTMCP_TRANSPORT\s*[=:]\s*["']?([A-Za-z_-]+)"""), ""),
    (re.compile(r"\.streamable_http_app\s*\("), STREAMABLE_HTTP),
    (re.compile(r"\.http_app\s*\("), STREAMABLE_HTTP),
    (re.compile(r"\.sse_app\s*\("), SSE),
    (re.compile(r"run_sse_async\s*\("), SSE),
    (re.compile(r"run_stdio_async\s*\("), STDIO),
)

_SCAN_GLOBS = (
    "**/*.py",
    "**/Dockerfile*",
    "**/docker-compose*.yml",
    "**/docker-compose*.yaml",
    "**/fly.toml",
    "**/Procfile",
    "**/*.env.example",
    "**/.env.example",
    "**/pyproject.toml",
    "**/README*",
)


@dataclass
class Derivation:
    """What the target's own configuration says about how it is served."""

    transports: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    floor_added: list[str] = field(default_factory=list)
    entrypoint: list[str] | None = None
    entrypoint_source: str = ""
    declared: dict[str, list[str]] = field(default_factory=dict)
    env_overrides: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transports": self.transports,
            "sources": self.sources,
            "floor_added": self.floor_added,
            "entrypoint": self.entrypoint,
            "entrypoint_source": self.entrypoint_source,
            "declared": {k: list(v) for k, v in self.declared.items()},
        }


def _iter_scan_files(root: Path) -> list[Path]:
    """Files worth reading, with the vendored/build trees pruned and a hard cap so
    a huge checkout cannot turn derivation into the slow part of the night."""
    seen: list[Path] = []
    for pattern in _SCAN_GLOBS:
        for path in root.glob(pattern):
            if len(seen) >= _MAX_SCAN_FILES:
                return seen
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            seen.append(path)
    return seen


def _read_capped(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_MAX_SCAN_BYTES)
    except OSError:
        return ""


def _load_pyproject(root: Path) -> dict[str, Any]:
    """Parse pyproject.toml. tomllib is stdlib from 3.11; the target may run on an
    older interpreter, so a missing tomllib degrades to "no pyproject data" rather
    than failing the gate."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11 target
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def _boot_table(pyproject: dict[str, Any]) -> dict[str, Any]:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return {}
    for key in ("mcp_auditor", "mcp-auditor", "mcp_continuous_auditor"):
        section = tool.get(key)
        if isinstance(section, dict) and isinstance(section.get("boot"), dict):
            return section["boot"]
    return {}


def _console_script(
    root: Path, pyproject: dict[str, Any]
) -> tuple[list[str] | None, str]:
    """The target's own launch argv, preferring an installed console script.

    A console script lives in the same venv as the interpreter running us (the
    gate is invoked through `uv run` inside the target), so resolving it next to
    sys.executable finds the real thing without a nested `uv run`.
    """
    scripts = pyproject.get("project", {}).get("scripts")
    if isinstance(scripts, dict):
        for name in scripts:
            candidate = Path(sys.executable).parent / str(name)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return [str(candidate)], f"[project.scripts] {name}"

    # `python -m <pkg>` when the package ships a __main__.
    for main_py in sorted(root.glob("*/__main__.py")) + sorted(
        root.glob("src/*/__main__.py")
    ):
        if any(part in _SKIP_DIRS for part in main_py.parts):
            continue
        pkg = main_py.parent.name
        return [sys.executable, "-m", pkg], f"{pkg}/__main__.py"
    return None, ""


def derive(root: Path, env: dict[str, str] | None = None) -> Derivation:
    """Work out which transports this target is configured to serve, and how it
    is normally launched. Explicit BOOT_TRANSPORTS wins and suppresses the floor."""
    env = dict(os.environ if env is None else env)
    d = Derivation()
    pyproject = _load_pyproject(root)
    boot = _boot_table(pyproject)

    # 1) declared argv per transport — the target told us exactly how to boot it.
    declared_cmds = boot.get("commands")
    if isinstance(declared_cmds, dict):
        for name, argv in declared_cmds.items():
            canonical = normalise_transport(name)
            if canonical and isinstance(argv, list) and argv:
                d.declared[canonical] = [str(a) for a in argv]
                d.sources.append(f"{canonical}: [tool.mcp_auditor.boot.commands]")

    override = (env.get("BOOT_TRANSPORTS") or "").strip()
    if override:
        for part in override.split(","):
            canonical = normalise_transport(part)
            if canonical and canonical not in d.transports:
                d.transports.append(canonical)
                d.sources.append(f"{canonical}: BOOT_TRANSPORTS override")
        d.entrypoint, d.entrypoint_source = _console_script(root, pyproject)
        return d

    # 2) an explicit transport list in the boot table.
    declared_list = boot.get("transports")
    if isinstance(declared_list, list):
        for item in declared_list:
            canonical = normalise_transport(str(item))
            if canonical and canonical not in d.transports:
                d.transports.append(canonical)
                d.sources.append(f"{canonical}: [tool.mcp_auditor.boot] transports")

    # 3) whatever the source, Dockerfile, compose file or .env.example reveals.
    for path in _iter_scan_files(root):
        text = _read_capped(path)
        if not text:
            continue
        rel = path.relative_to(root).as_posix()
        for pattern, fixed in _SOURCE_MARKERS:
            for match in pattern.finditer(text):
                canonical = fixed or normalise_transport(match.group(1))
                if not canonical:
                    continue
                if canonical not in d.transports:
                    d.transports.append(canonical)
                    d.sources.append(f"{canonical}: {rel}")

    for canonical in d.declared:
        if canonical not in d.transports:
            d.transports.append(canonical)

    # 4) the floor. Probing only what the target advertises reproduces the blind
    #    spot this gate exists to remove, so stdio + streamable-http are always in.
    for canonical in FLOOR_TRANSPORTS:
        if canonical not in d.transports:
            d.transports.append(canonical)
            d.floor_added.append(canonical)
            d.sources.append(f"{canonical}: floor (always probed)")

    order = {STDIO: 0, STREAMABLE_HTTP: 1, SSE: 2}
    d.transports.sort(key=lambda t: order.get(t, 9))
    d.entrypoint, d.entrypoint_source = _console_script(root, pyproject)
    return d


# --------------------------------------------------------------------------
# launch plans
# --------------------------------------------------------------------------

# Imported, then run. Only reached when the target ships no usable entrypoint —
# see the module docstring on why this mode is weaker evidence for case 2.
_GENERIC_LAUNCHER = r"""
import importlib, os, sys
sys.path.insert(0, os.getcwd())
ref = os.environ.get("MCP_SERVER_IMPORT") or "server:mcp"
mod, _, attr = ref.partition(":")
srv = getattr(importlib.import_module(mod), attr or "mcp")
transport = os.environ["BOOT_GENERIC_TRANSPORT"]
if transport == "stdio":
    srv.run(transport="stdio")
else:
    kwargs = {"host": os.environ["BOOT_GENERIC_HOST"],
              "port": int(os.environ["BOOT_GENERIC_PORT"])}
    try:
        srv.run(transport=transport, **kwargs)
    except (ValueError, TypeError) as exc:
        # Only a rejected transport NAME is retried under its alias; any other
        # startup error (case 1) must propagate and fail the boot.
        if "transport" not in str(exc).lower():
            raise
        srv.run(transport="streamable-http" if transport == "http" else "http", **kwargs)
"""


@dataclass
class LaunchPlan:
    transport: str
    mode: str  # declared | entrypoint | generic
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""


def build_launch_plan(transport: str, derivation: Derivation) -> LaunchPlan:
    """Pick the most faithful way to start the target for one transport."""
    if transport in derivation.declared:
        return LaunchPlan(
            transport=transport,
            mode="declared",
            argv=list(derivation.declared[transport]),
            note="argv declared by the target ([tool.mcp_auditor.boot.commands])",
        )
    if derivation.entrypoint:
        return LaunchPlan(
            transport=transport,
            mode="entrypoint",
            argv=list(derivation.entrypoint),
            note=f"target entrypoint ({derivation.entrypoint_source})",
        )
    return LaunchPlan(
        transport=transport,
        mode="generic",
        argv=[sys.executable, "-c", _GENERIC_LAUNCHER],
        note="imported server object — case-2 coverage is partial in this mode",
    )


# The env var names targets actually read for transport/host/port. We set all of
# them: an entrypoint that reads only one still gets it, and the extras are inert.
_TRANSPORT_ENV = ("MCP_TRANSPORT", "FASTMCP_TRANSPORT", "TRANSPORT")
_HOST_ENV = ("MCP_HOST", "FASTMCP_HOST", "FASTMCP_SERVER_HOST", "HOST")
_PORT_ENV = ("MCP_PORT", "FASTMCP_PORT", "FASTMCP_SERVER_PORT", "PORT")


def launch_env(
    plan: LaunchPlan, host: str, port: int, base: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(plan.env)
    wire = "http" if plan.transport == STREAMABLE_HTTP else plan.transport
    for name in _TRANSPORT_ENV:
        env[name] = wire
    if plan.transport != STDIO:
        for name in _HOST_ENV:
            env[name] = host
        for name in _PORT_ENV:
            env[name] = str(port)
    env["BOOT_GENERIC_TRANSPORT"] = wire
    env["BOOT_GENERIC_HOST"] = host
    env["BOOT_GENERIC_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def substitute(argv: list[str], host: str, port: int) -> list[str]:
    """Fill {host}/{port} placeholders in a declared argv."""
    return [a.replace("{host}", host).replace("{port}", str(port)) for a in argv]


# CLI spellings a target may use to pick a network transport. The env vars in
# `launch_env` cover targets that read the environment; these cover the ones that
# take a flag — zurich-opendata-mcp takes `--http --port N` and ignores the
# environment entirely, which is how the gate came to report a healthy server as
# dead.
#
# This is NOT the same kind of guessing the module docstring refuses. Deriving
# WHICH transports a target serves must never be guessed, because a wrong guess
# there invents a requirement. Guessing how to INVOKE one is self-verifying: an
# attempt only counts if the port then opens and the server answers real MCP. A
# wrong flag just fails and we move on; a right one is proof.
_TRANSPORT_FLAGS: dict[str, tuple[tuple[str, ...], ...]] = {
    STREAMABLE_HTTP: (
        ("--http", "--port", "{port}"),
        ("--transport", "http", "--port", "{port}"),
        ("--transport", "streamable-http", "--port", "{port}"),
    ),
    SSE: (
        ("--sse", "--port", "{port}"),
        ("--transport", "sse", "--port", "{port}"),
    ),
}


def argv_variants(plan: LaunchPlan, host: str, port: int) -> list[list[str]]:
    """Every way worth trying to start `plan` on a network transport, in order.

    The bare argv comes first: a target that reads the environment is already
    served by `launch_env`, and trying flags on it first would risk an argparse
    error on a server that would have worked. A DECLARED argv is never extended —
    the target told us exactly how it wants to be started, and adding flags to
    that would be overriding an explicit instruction with a guess.
    """
    base = substitute(plan.argv, host, port)
    if plan.mode == "declared" or plan.transport == STDIO:
        return [base]
    out = [base]
    for flags in _TRANSPORT_FLAGS.get(plan.transport, ()):
        out.append(
            base
            + [f.replace("{port}", str(port)).replace("{host}", host) for f in flags]
        )
    return out


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    transport: str
    mode: str
    ok: bool
    detail: str
    tools: int | None = None
    elapsed_s: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    # ok | fail | not-selected. The third one is the whole point of this field:
    # see NOT_SELECTED below. `ok` stays the boolean the rest of the code reads,
    # and a not-selected result is NOT ok — it is simply not a finding either.
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = OK if self.ok else FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "mode": self.mode,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "tools": self.tools,
            "elapsed_s": round(self.elapsed_s, 3),
            "evidence": self.evidence,
        }


def _rpc(
    method: str, ident: int, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
    msg["params"] = params if params is not None else {}
    return msg


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": _CLIENT_INFO,
    }


def _stateless_meta() -> dict[str, Any]:
    """The `_meta` block a stateless (2026-07-28) request carries.

    Under the stateless core there is no handshake to negotiate in, so every
    request brings its own protocol version, clientInfo and capabilities. A
    server on the older spec ignores an unknown `_meta` key, so sending it costs
    nothing and is what makes ONE request shape work against both.
    """
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "clientInfo": _CLIENT_INFO,
        "capabilities": {},
    }


def _opening_call(stateless: bool, ident: int = 1) -> dict[str, Any]:
    """The first request of a probe: a handshake, or a real call without one."""
    if stateless:
        return {**_rpc("tools/list", ident), "_meta": _stateless_meta()}
    return _rpc("initialize", ident, _initialize_params())


def _has_result(payload: Any) -> bool:
    return isinstance(payload, dict) and "result" in payload


def _handshake_refused(payload: Any) -> bool:
    """Is this a server that has REMOVED `initialize`, rather than a broken one?

    JSON-RPC -32601 is "method not found", which is what a stateless server
    answers to a method the spec deleted. Anything else — an internal error, a
    validation error, a crash — is a genuine failure and must keep failing, so
    the check is on the code and not on "did initialize succeed".
    """
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    if not isinstance(err, dict):
        return False
    if err.get("code") == -32601:
        return True
    return "method not found" in str(err.get("message", "")).lower()


def negotiated_version(payload: Any) -> str:
    """The protocol version the SERVER named. Previously read by nobody.

    `initialize`'s result carried `protocolVersion` all along and this gate threw
    it away, looking only at `result.tools`. That single omission is why no report
    in this repository could say which spec a target speaks — the measurement was
    arriving and being discarded.
    """
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    return str(result.get("protocolVersion") or "")


def _tool_count(result: Any) -> int | None:
    if isinstance(result, dict):
        tools = result.get("tools")
        if isinstance(tools, list):
            return len(tools)
    return None


def _terminate(proc: subprocess.Popen) -> None:
    """Kill the whole process group — a server that forked workers must not be
    left holding the port for the next transport's probe."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        # pragma: no cover - unkillable child
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def _close_streams(proc: subprocess.Popen) -> None:
    """Release the pipes once the child is gone. Called only after the last read —
    the gate probes several transports in a row and must not leak a descriptor per
    attempt."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _drain(queue: Queue[str | None], sink: list[str], wait: float = 1.0) -> str:
    """Everything the reader thread has produced, waiting briefly for its EOF
    sentinel. Without the wait, a process that just crashed can lose its last words
    to a race — and those last words ARE the finding (the traceback naming the
    read-only settings field)."""
    deadline = time.monotonic() + wait
    while True:
        remaining = deadline - time.monotonic()
        try:
            item = queue.get(timeout=remaining) if remaining > 0 else queue.get_nowait()
        except Empty:
            break
        if item is None:
            break
        sink.append(item)
    return "".join(sink)


def _tail(text: str, limit: int = 400) -> str:
    """Last few lines of a captured stream, control chars stripped — this text ends
    up in a log the operator reads and (as an exit code's explanation) near the
    report sink, so it must not carry terminal escapes."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text or "")
    return cleaned.strip()[-limit:]


# --------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------


def _reader_thread(stream: Any, queue: Queue[str | None]) -> threading.Thread:
    def run() -> None:
        try:
            for line in iter(stream.readline, ""):
                queue.put(line)
        except (ValueError, OSError):  # stream closed under us
            pass
        finally:
            queue.put(None)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def probe_stdio(
    plan: LaunchPlan,
    timeout: float,
    cwd: Path,
    env: dict[str, str] | None = None,
    _close_stdin_early: bool = False,
) -> ProbeResult:
    """Boot over stdio and run initialize + tools/list.

    ``_close_stdin_early`` exists ONLY so the test suite can demonstrate the trap
    this gate was written around: close stdin after writing and the server shuts
    down before network-bound work finishes, and you record a failure that is not
    real. Production callers never set it.
    """
    started = time.monotonic()
    run_env = launch_env(plan, "127.0.0.1", 0, env)
    try:
        proc = subprocess.Popen(
            plan.argv,
            cwd=str(cwd),
            env=run_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(
            plan.transport,
            plan.mode,
            False,
            f"could not spawn the target: {type(exc).__name__}: {exc}",
        )

    out_q: Queue[str | None] = Queue()
    err_lines: list[str] = []
    _reader_thread(proc.stdout, out_q)
    err_q: Queue[str | None] = Queue()
    _reader_thread(proc.stderr, err_q)

    deadline = started + timeout

    def drain_stderr() -> str:
        return _drain(err_q, err_lines)

    def send(msg: dict[str, Any]) -> bool:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def await_response(ident: int) -> tuple[dict[str, Any] | None, str]:
        """Read until the response with this id arrives or the deadline passes.
        Notifications and log lines interleaved on stdout are skipped, not fatal."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "timed out waiting for a response"
            try:
                line = out_q.get(timeout=min(remaining, 0.5))
            except Empty:
                if proc.poll() is not None:
                    return (
                        None,
                        f"the process exited (rc {proc.returncode}) before responding",
                    )
                continue
            if line is None:
                return None, f"stdout closed (process rc {proc.poll()})"
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # a stray log line on stdout is bad manners, not a boot failure
            if isinstance(msg, dict) and msg.get("id") == ident:
                return msg, ""

    try:
        if not send(_rpc("initialize", 1, _initialize_params())):
            _terminate(proc)
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"the target closed stdin before initialize could be written; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )

        # THE TRAP: stdin stays open past the write. Closing it here is what the
        # `_close_stdin_early` switch reproduces for the regression test.
        if _close_stdin_early and proc.stdin is not None:
            proc.stdin.close()

        reply, err = await_response(1)
        if reply is None:
            _terminate(proc)
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"initialize failed: {err}; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )
        # A REJECTED handshake is not a broken server. Spec 2026-07-28 removed
        # `initialize`; a server that has migrated answers -32601 here and is
        # perfectly healthy. Ask the second question — can it serve a real call
        # with no handshake at all — before concluding anything about it.
        stateless = _handshake_refused(reply)
        if "error" in reply and not stateless:
            _terminate(proc)
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"initialize returned a JSON-RPC error: {_tail(json.dumps(reply['error']), 200)}",
                elapsed_s=time.monotonic() - started,
            )

        spec = negotiated_version(reply)
        if not stateless:
            send(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
        if not send(
            _opening_call(stateless, 2) if stateless else _rpc("tools/list", 2)
        ):
            _terminate(proc)
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"the target went away before tools/list; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )

        listing, err = await_response(2)
        if listing is None:
            _terminate(proc)
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"tools/list failed: {err}; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )
        if "error" in listing:
            _terminate(proc)
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"tools/list returned a JSON-RPC error: {_tail(json.dumps(listing['error']), 200)}",
                elapsed_s=time.monotonic() - started,
            )

        count = _tool_count(listing.get("result"))
        opened = "no handshake (stateless core)" if stateless else "initialize"
        return ProbeResult(
            plan.transport,
            plan.mode,
            True,
            f"{opened} + tools/list OK ({count if count is not None else '?'} tool(s))"
            + (f"; server negotiated {spec}" if spec else ""),
            tools=count,
            elapsed_s=time.monotonic() - started,
            status=STATELESS if stateless else OK,
            evidence={"negotiated_protocol_version": spec, "stateless": stateless},
        )
    finally:
        _terminate(proc)
        _close_streams(proc)


# --------------------------------------------------------------------------
# HTTP (streamable-http and sse)
# --------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Launch:
    """The outcome of trying to get a network transport listening."""

    proc: subprocess.Popen | None = None
    logs: str = ""
    argv: list[str] = field(default_factory=list)
    attempt: int = 0  # 0 = the target's own invocation, >0 = a flag guess
    reason: str = ""  # "" once something is listening
    clean_exit: bool = False  # the TARGET'S OWN invocation exited rc 0 unlistening
    drain: Any = None


def start_listening(
    variants: list[list[str]],
    run_env: dict[str, str],
    cwd: Path,
    port: int,
    deadline: float,
) -> Launch:
    """Try each invocation until one listens on `port`.

    Only attempt 0 — the target's own argv plus the transport env vars — decides
    the VERDICT. The flag variants after it are chances to succeed and never
    chances to fail: an argparse error from a guessed `--http` says something
    about our guess, not about the target, and letting it set the verdict would
    swap one false finding for another.
    """
    out = Launch()
    first_rc: int | None = None
    for i, argv in enumerate(variants):
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=run_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            if i == 0:
                out.reason = f"could not spawn the target: {type(exc).__name__}: {exc}"
            continue
        q: Queue[str | None] = Queue()
        _reader_thread(proc.stdout, q)
        cap: list[str] = []
        # A guessed variant gets a short leash; the target's own invocation gets
        # the full budget, and returns early anyway when the process exits.
        sub = deadline if i == 0 else min(deadline, time.monotonic() + 20.0)
        reason = wait_for_port(port, sub, proc)
        if not reason:
            out.proc, out.argv, out.attempt = proc, argv, i
            out.drain = lambda q=q, cap=cap: _drain(q, cap)
            return out
        rc = proc.poll()
        text = _tail(_drain(q, cap))
        if i == 0:
            first_rc = rc
            out.reason = f"{reason}; output: {text}"
            out.argv = argv
        _terminate(proc)
        _close_streams(proc)
        if time.monotonic() >= deadline:
            break
    out.clean_exit = first_rc == 0
    out.drain = lambda: ""
    return out


def wait_for_port(
    port: int, deadline: float, proc: subprocess.Popen | None = None
) -> str:
    """Block until the port accepts, the process dies, or the deadline passes.
    Returns "" on success or a reason string."""
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return f"the process exited (rc {proc.returncode}) before it listened on {port}"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return ""
        except OSError:
            time.sleep(0.1)
    return f"nothing was listening on port {port} within the deadline"


def _parse_sse_payload(body: str) -> Any:
    """Pull the JSON out of an SSE frame; streamable-http may answer either way."""
    chunks = [
        line[5:].strip() for line in body.splitlines() if line.startswith("data:")
    ]
    for chunk in reversed(chunks):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


def _decode_body(headers: dict[str, str], body: str) -> Any:
    ctype = (headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype:
        return _parse_sse_payload(body)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return _parse_sse_payload(body)


@dataclass
class HttpReply:
    status: int
    headers: dict[str, str]
    body: str
    payload: Any
    final_path: str = ""


_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 3


def _redirect_target(location: str, current: str) -> str:
    """The path a Location header points at, absolute form reduced to its path.
    Only the path is followed — the connection stays pinned to 127.0.0.1, so a
    redirect can never move the probe to another host."""
    value = (location or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        rest = value.split("://", 1)[1]
        return "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    if value.startswith("/"):
        return value
    base = current.rsplit("/", 1)[0]
    return f"{base}/{value}"


def http_post(
    port: int,
    path: str,
    host_header: str,
    payload: dict[str, Any],
    timeout: float,
    session_id: str = "",
    extra_headers: dict[str, str] | None = None,
) -> HttpReply:
    """One JSON-RPC POST. The connection always goes to 127.0.0.1 — only the Host
    header varies, which is exactly the knob case 2 turns on.

    Redirects are followed (with the method and body preserved, as 307/308
    require): FastMCP answers ``/mcp/`` with a 307 to ``/mcp``, and treating that
    as "the transport is broken" would make the gate fire on every healthy target.

    ``extra_headers`` is what the rebinding gate (scripts/rebind_probe.py) varies:
    ``Origin`` and ``Authorization``. It is applied last, so a caller can also
    override ``Host`` — the header this whole function exists to control.
    """
    headers = {
        "Host": host_header,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    headers.update(extra_headers or {})

    body = json.dumps(payload)
    current = path
    for _ in range(_MAX_REDIRECTS + 1):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("POST", current, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            got = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()
        if resp.status in _REDIRECT_CODES:
            nxt = _redirect_target(got.get("location", ""), current)
            if nxt and nxt != current:
                current = nxt
                continue
        return HttpReply(resp.status, got, raw, _decode_body(got, raw), current)
    return HttpReply(resp.status, got, raw, _decode_body(got, raw), current)


def http_get_sse(
    port: int,
    path: str,
    host_header: str,
    timeout: float,
    extra_headers: dict[str, str] | None = None,
) -> HttpReply:
    """The legacy SSE handshake's opening GET — we only need the first frames, so
    the read is bounded rather than following the stream to its end. Redirects are
    followed for the same reason as in http_post.

    The bounded read is what makes this safe for the rebinding gate too: a server
    with NO host allow-list answers a foreign-Host GET with an endless event
    stream, and an unbounded reader would hang on the very case it is measuring.
    """
    headers = {"Host": host_header, "Accept": "text/event-stream"}
    headers.update(extra_headers or {})
    current = path
    for _ in range(_MAX_REDIRECTS + 1):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", current, headers=headers)
            resp = conn.getresponse()
            got = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status in _REDIRECT_CODES:
                resp.read(4096)
                nxt = _redirect_target(got.get("location", ""), current)
                if nxt and nxt != current:
                    current = nxt
                    continue
            raw = ""
            if resp.status == 200:
                deadline = time.monotonic() + timeout
                while "data:" not in raw and time.monotonic() < deadline:
                    chunk = resp.read(256)
                    if not chunk:
                        break
                    raw += chunk.decode("utf-8", errors="replace")
            else:
                raw = resp.read(4096).decode("utf-8", errors="replace")
            return HttpReply(resp.status, got, raw, None, current)
        finally:
            conn.close()
    return HttpReply(resp.status, got, "", None, current)


def _resolve_path(
    port: int,
    candidates: list[str],
    host_header: str,
    timeout: float,
    stateless: bool = False,
) -> tuple[str, HttpReply | None]:
    """Find the endpoint path by trying the candidates with a real opening call.

    A 404/405 means wrong path; anything else (including 421) is the server's real
    answer and ends the search.

    NOTE that `initialize` is not merely the assertion here, it is the DISCOVERY
    mechanism. So a stateless server that rejects the method breaks path
    resolution itself, not just the check — the gate would then report "the
    transport is broken" about a path it never reached. `stateless=True` runs the
    same search with a real call instead, which is why the caller tries both.
    """
    last: HttpReply | None = None
    for path in candidates:
        try:
            reply = http_post(
                port, path, host_header, _opening_call(stateless, 1), timeout
            )
        except (OSError, http.client.HTTPException):
            continue
        last = reply
        if reply.status not in (404, 405):
            # Report the path the server actually settled on, so every later call
            # goes straight there instead of re-walking the redirect each time.
            return reply.final_path or path, reply
    return (candidates[0] if candidates else "/"), last


def _unlistening_result(
    plan: LaunchPlan, launch: Launch, bind_host: str, port: int, elapsed: float
) -> ProbeResult:
    """Nothing listened. Decide WHICH of the two statements that supports.

    A clean exit (rc 0) from the target's own invocation means it ran something
    else — almost always stdio, because the transport was requested through env
    vars this entrypoint does not read — and finished. That is not "the server
    does not come up"; it is "we never got to ask". Saying the former is a claim
    about the target that nothing established, and it is the bug this branch
    exists to fix.
    """
    ev = {"bind_host": bind_host, "port": port, "reason": launch.reason}
    if launch.clean_exit:
        return ProbeResult(
            plan.transport,
            plan.mode,
            False,
            "the entrypoint exited cleanly (rc 0) without ever listening, and none "
            "of the usual transport flags got it to serve either. It almost "
            f"certainly does not select {plan.transport} the way this gate asks — "
            "the env vars (MCP_TRANSPORT/FASTMCP_TRANSPORT/PORT) went unread. This "
            "says NOTHING about whether the transport works. Declare the exact "
            "argv in the target's pyproject.toml under "
            f'[tool.mcp_auditor.boot.commands] "{plan.transport}" = [...] and the '
            "gate will start it the way the target expects",
            elapsed_s=elapsed,
            status=NOT_SELECTED,
            evidence={**ev, "case": "transport-not-selected"},
        )
    return ProbeResult(
        plan.transport,
        plan.mode,
        False,
        f"the server never came up: {launch.reason}",
        elapsed_s=elapsed,
        status=FAIL,
        evidence=ev,
    )


def probe_streamable_http(
    plan: LaunchPlan,
    timeout: float,
    cwd: Path,
    bind_host: str = DEFAULT_BIND_HOST,
    probe_host: str = DEFAULT_HTTP_HOST,
    paths: list[str] | None = None,
    env: dict[str, str] | None = None,
    port: int | None = None,
) -> ProbeResult:
    """Boot over streamable-http on a real (0.0.0.0) bind and speak MCP to it.

    Two Host headers, deliberately: loopback first to establish that the transport
    works at all, then a non-loopback name. A 421 on the second when the first
    passed is case 2 and nothing else — that distinction is what keeps this a
    diagnostic finding rather than an alarm.
    """
    started = time.monotonic()
    port = free_port() if port is None else port
    paths = paths or ["/mcp/", "/mcp", "/"]
    run_env = launch_env(plan, bind_host, port, env)
    deadline = started + timeout

    launch = start_listening(
        argv_variants(plan, bind_host, port), run_env, cwd, port, deadline
    )
    proc = launch.proc
    if proc is None:
        return _unlistening_result(
            plan, launch, bind_host, port, time.monotonic() - started
        )

    def logs() -> str:
        return launch.drain()

    try:
        remaining = max(deadline - time.monotonic(), 1.0)
        loop_host = f"127.0.0.1:{port}"
        path, reply = _resolve_path(port, paths, loop_host, min(remaining, 10.0))

        # The handshake did not land. Before calling the transport broken — the
        # claim that turned a migrated server into a GitHub issue — ask whether
        # this is a server that no longer HAS a handshake, by making a real call
        # without one. Only a stateless call that genuinely succeeds may change
        # the verdict; a second failure leaves the original one standing.
        stateless = False
        if reply is None or reply.status != 200 or not _has_result(reply.payload):
            remaining = max(deadline - time.monotonic(), 1.0)
            alt_path, alt = _resolve_path(
                port, paths, loop_host, min(remaining, 10.0), stateless=True
            )
            if alt is not None and alt.status == 200 and _has_result(alt.payload):
                path, reply, stateless = alt_path, alt, True

        if reply is None:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"the port was open but no endpoint answered on {', '.join(paths)}; output: {_tail(logs())}",
                elapsed_s=time.monotonic() - started,
            )
        if reply.status != 200:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"initialize on {path} returned HTTP {reply.status} even for a loopback Host — "
                f"the transport is broken, not merely host-restricted; body: {_tail(reply.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "loopback_status": reply.status},
            )
        if not _has_result(reply.payload):
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"neither initialize nor a handshake-free call on {path} returned a "
                f"JSON-RPC result; body: {_tail(reply.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path},
            )

        spec = negotiated_version(reply.payload)
        session_id = reply.headers.get("mcp-session-id", "")

        # --- CASE 2: the same request, only the Host header differs -------------
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            hostile = http_post(
                port,
                path,
                probe_host,
                # The SAME request shape that worked for the loopback Host. Case 2
                # is a difference of one header and nothing else; varying the body
                # too would make a 421 unattributable.
                _opening_call(stateless, 3),
                min(remaining, 10.0),
            )
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"the non-loopback Host probe could not complete: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "probe_host": probe_host},
            )
        if hostile.status == 421:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"HTTP 421 for Host '{probe_host}' while the loopback Host passed — the inbound "
                "host allow-list is derived from a 127.0.0.1 default, so a 0.0.0.0 deployment "
                "rejects every request made under its real hostname. Pass the configured host "
                "through to the app builder.",
                elapsed_s=time.monotonic() - started,
                evidence={
                    "path": path,
                    "probe_host": probe_host,
                    "status": 421,
                    "loopback_status": 200,
                    "case": "host-allowlist-421",
                },
            )
        if hostile.status != 200:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"initialize under Host '{probe_host}' returned HTTP {hostile.status} "
                f"(loopback returned 200); body: {_tail(hostile.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={
                    "path": path,
                    "probe_host": probe_host,
                    "status": hostile.status,
                },
            )

        # --- tools/list on the working session ---------------------------------
        # In the stateless case the opening call WAS tools/list, so there is
        # nothing left to ask: repeating it would only measure the same thing a
        # second time and give a flaky server a second chance to fail.
        if stateless:
            listing = reply
        else:
            remaining = max(deadline - time.monotonic(), 1.0)
            try:
                http_post(
                    port,
                    path,
                    loop_host,
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    min(remaining, 10.0),
                    session_id,
                )
                listing = http_post(
                    port,
                    path,
                    loop_host,
                    _rpc("tools/list", 2),
                    min(remaining, 10.0),
                    session_id,
                )
            except (OSError, http.client.HTTPException) as exc:
                return ProbeResult(
                    plan.transport,
                    plan.mode,
                    False,
                    f"tools/list could not complete: {type(exc).__name__}: {exc}",
                    elapsed_s=time.monotonic() - started,
                )
        if listing.status != 200 or not _has_result(listing.payload):
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"tools/list on {path} returned HTTP {listing.status}; body: {_tail(listing.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "status": listing.status},
            )

        count = _tool_count(listing.payload.get("result"))
        opened = "no handshake (stateless core)" if stateless else "initialize"
        return ProbeResult(
            plan.transport,
            plan.mode,
            True,
            f"bound {bind_host}:{port}{path} — {opened} + tools/list OK under both a loopback "
            f"and a non-loopback Host ({count if count is not None else '?'} tool(s))"
            + (f"; server negotiated {spec}" if spec else ""),
            tools=count,
            elapsed_s=time.monotonic() - started,
            status=STATELESS if stateless else OK,
            evidence={
                "path": path,
                "probe_host": probe_host,
                "bind_host": bind_host,
                "negotiated_protocol_version": spec,
                "stateless": stateless,
                # A session id under the stateless core is itself a legacy signal;
                # spec_probe.py is what turns that into a LEGACY_TRANSPORT finding.
                "session_id_issued": bool(session_id),
            },
        )
    finally:
        _terminate(proc)
        _close_streams(proc)


def probe_sse(
    plan: LaunchPlan,
    timeout: float,
    cwd: Path,
    bind_host: str = DEFAULT_BIND_HOST,
    probe_host: str = DEFAULT_HTTP_HOST,
    paths: list[str] | None = None,
    env: dict[str, str] | None = None,
    port: int | None = None,
) -> ProbeResult:
    """Boot over the legacy SSE transport: GET the stream, read the `endpoint`
    event, POST initialize to it. Same two-Host logic as streamable-http."""
    started = time.monotonic()
    port = free_port() if port is None else port
    paths = paths or ["/sse/", "/sse"]
    run_env = launch_env(plan, bind_host, port, env)
    deadline = started + timeout

    launch = start_listening(
        argv_variants(plan, bind_host, port), run_env, cwd, port, deadline
    )
    proc = launch.proc
    if proc is None:
        return _unlistening_result(
            plan, launch, bind_host, port, time.monotonic() - started
        )

    def logs() -> str:
        return launch.drain()

    try:
        loop_host = f"127.0.0.1:{port}"
        stream: HttpReply | None = None
        used = paths[0]
        for path in paths:
            remaining = max(deadline - time.monotonic(), 1.0)
            try:
                candidate = http_get_sse(port, path, loop_host, min(remaining, 10.0))
            except (OSError, http.client.HTTPException):
                continue
            if candidate.status in (404, 405):
                continue
            stream, used = candidate, (candidate.final_path or path)
            break

        if stream is None:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"no SSE endpoint answered on {', '.join(paths)}; output: {_tail(logs())}",
                elapsed_s=time.monotonic() - started,
            )
        if stream.status != 200:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"GET {used} returned HTTP {stream.status} for a loopback Host; body: {_tail(stream.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": used, "loopback_status": stream.status},
            )

        endpoint = ""
        for line in stream.body.splitlines():
            if line.startswith("data:"):
                value = line[5:].strip()
                if value.startswith("/") or value.startswith("http"):
                    endpoint = value
                    break
        if not endpoint:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"the SSE stream on {used} never announced a message endpoint; body: {_tail(stream.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": used},
            )
        if endpoint.startswith("http"):
            endpoint = (
                "/" + endpoint.split("/", 3)[-1] if endpoint.count("/") >= 3 else "/"
            )

        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            posted = http_post(
                port,
                endpoint,
                loop_host,
                _rpc("initialize", 1, _initialize_params()),
                min(remaining, 10.0),
            )
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"posting initialize to the SSE message endpoint failed: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )
        # SSE delivers the reply on the stream, so 200/202 both mean "accepted".
        if posted.status not in (200, 202):
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"initialize on the SSE message endpoint returned HTTP {posted.status}; "
                f"body: {_tail(posted.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"endpoint": endpoint, "status": posted.status},
            )

        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            hostile = http_get_sse(port, used, probe_host, min(remaining, 10.0))
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"the non-loopback Host probe could not complete: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )
        if hostile.status == 421:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"HTTP 421 for Host '{probe_host}' while the loopback Host passed — the inbound "
                "host allow-list is derived from a 127.0.0.1 default, so a 0.0.0.0 deployment "
                "rejects every request made under its real hostname.",
                elapsed_s=time.monotonic() - started,
                evidence={
                    "path": used,
                    "probe_host": probe_host,
                    "status": 421,
                    "case": "host-allowlist-421",
                },
            )
        if hostile.status != 200:
            return ProbeResult(
                plan.transport,
                plan.mode,
                False,
                f"GET {used} under Host '{probe_host}' returned HTTP {hostile.status} "
                "(loopback returned 200)",
                elapsed_s=time.monotonic() - started,
                evidence={"path": used, "status": hostile.status},
            )

        return ProbeResult(
            plan.transport,
            plan.mode,
            True,
            f"bound {bind_host}:{port}{used} — SSE handshake + initialize OK under both a "
            "loopback and a non-loopback Host. NOTE: HTTP+SSE is the legacy "
            "transport, deprecated under spec 2026-07-28 — run "
            "`scripts/spec_probe.py --url ...` for the deadline",
            elapsed_s=time.monotonic() - started,
            evidence={"path": used, "endpoint": endpoint, "legacy_transport": True},
        )
    finally:
        _terminate(proc)
        _close_streams(proc)


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


def probe_transport(
    plan: LaunchPlan,
    timeout: float,
    cwd: Path,
    bind_host: str,
    probe_host: str,
    http_paths: list[str],
    sse_paths: list[str],
) -> ProbeResult:
    if plan.transport == STDIO:
        return probe_stdio(plan, timeout, cwd)
    if plan.transport == SSE:
        return probe_sse(plan, timeout, cwd, bind_host, probe_host, sse_paths)
    return probe_streamable_http(plan, timeout, cwd, bind_host, probe_host, http_paths)


def render(results: list[ProbeResult], derivation: Derivation) -> str:
    lines = ["# Transport boot gate", ""]
    lines.append(
        f"Probed {len(results)} transport(s): "
        + ", ".join(r.transport for r in results)
    )
    if derivation.floor_added:
        lines.append(
            f"Added by the floor (always probed): {', '.join(derivation.floor_added)}"
        )
    lines.append("")
    icons = {OK: "✅", FAIL: "❌", NOT_SELECTED: "🟡", STATELESS: "✅"}
    for r in results:
        lines.append(
            f"{icons.get(r.status, '?')} {r.transport} [{r.mode}] — {r.detail}"
        )

    negotiated = sorted(
        {
            str(r.evidence.get("negotiated_protocol_version") or "")
            for r in results
            if r.evidence.get("negotiated_protocol_version")
        }
    )
    if negotiated:
        lines += ["", "Protocol version(s) the server named: " + ", ".join(negotiated)]
    stateless = [r for r in results if r.status == STATELESS]
    if stateless:
        lines += [
            "",
            "## ✅ Stateless core — a PASS, not a finding",
            "",
            "For " + ", ".join(r.transport for r in stateless) + " the server "
            "refused `initialize` (JSON-RPC -32601) and then served a real call "
            "with no handshake in front of it. That is spec 2026-07-28 behaviour, "
            "and it is what this gate used to report as “the server never came "
            "up”. Nothing here is wrong with the target.",
        ]

    unselected = [r for r in results if r.status == NOT_SELECTED]
    if unselected:
        lines += [
            "",
            "## 🟡 Transport not selected — NOT a statement about the target",
            "",
            "For " + ", ".join(r.transport for r in unselected) + " the entrypoint "
            "exited cleanly without listening, and no transport flag got it to "
            "serve. The gate asks for a transport through env vars "
            "(`MCP_TRANSPORT`/`FASTMCP_TRANSPORT`/`PORT`); a target that selects it "
            "with a CLI flag it does not recognise simply runs its default and "
            "finishes. **Nothing here says the transport is broken** — it says the "
            "gate never managed to start it. Fix it once, in the target:",
            "",
            "```toml",
            "[tool.mcp_auditor.boot.commands]",
        ]
        for r in unselected:
            lines.append(
                f'"{r.transport}" = ["<entrypoint>", "--<flag>", "--port", "{{port}}"]'
            )
        lines.append("```")
    weak = [r for r in results if r.ok and r.mode == "generic" and r.transport != STDIO]
    if weak:
        lines += [
            "",
            "> Note — the HTTP transport(s) above booted in `generic` mode: this harness "
            "imported the server object and passed `host` itself. That still proves the "
            "process starts, but it cannot fully exercise a target whose OWN startup code "
            "drops `host` before the app builder. Ship a `[project.scripts]` entrypoint or "
            "a `[tool.mcp_auditor.boot.commands]` table to get the strong check.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        root = Path(env.get("BOOT_TARGET_ROOT") or ".").resolve()
        timeout = float(env.get("BOOT_TIMEOUT") or DEFAULT_TIMEOUT)
        bind_host = env.get("BOOT_BIND_HOST") or DEFAULT_BIND_HOST
        probe_host = env.get("BOOT_HTTP_HOST") or DEFAULT_HTTP_HOST
        http_paths = [
            p for p in (env.get("BOOT_HTTP_PATHS") or "/mcp/,/mcp,/").split(",") if p
        ]
        sse_paths = [
            p for p in (env.get("BOOT_SSE_PATHS") or "/sse/,/sse").split(",") if p
        ]
        # Captured before `derive` reads the first file: the derivation, the
        # launch plan and every boot below are claims about this commit.
        prov = probe_provenance.capture(root)
        derivation = derive(root)
    except Exception as exc:  # noqa: BLE001 - the harness itself failed
        print(
            f"transport-boot: harness could not run: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    if not derivation.transports:
        # Fail closed, exactly like the schema gate: a target we cannot work out how
        # to boot is a FINDING, not a pass and not an infrastructure failure.
        print(
            "transport-boot: no transport could be derived from the target — treating as a "
            "finding (set BOOT_GATE=off if this target genuinely cannot be booted here)",
            file=sys.stderr,
        )
        return EXIT_FINDINGS

    results: list[ProbeResult] = []
    for transport in derivation.transports:
        plan = build_launch_plan(transport, derivation)
        print(
            f"==> boot {transport} [{plan.mode}]: {' '.join(plan.argv[:2])}",
            file=sys.stderr,
        )
        try:
            results.append(
                probe_transport(
                    plan, timeout, root, bind_host, probe_host, http_paths, sse_paths
                )
            )
        except Exception as exc:  # noqa: BLE001 - one probe blowing up is a harness bug
            print(
                f"transport-boot: probe for {transport} raised: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN

    report = render(results, derivation)
    print(report)

    prov.recheck()
    print(prov.render(), file=sys.stderr)

    # A real failure outranks a not-selected: if stdio genuinely did not come up,
    # that is the finding, whatever we could not ask of the HTTP transport.
    failed = [r for r in results if r.status == FAIL]
    unselected = [r for r in results if r.status == NOT_SELECTED]
    if prov.blocking:
        # Ahead of the failure branch on purpose: a boot that was launched from
        # one tree and judged against another has not measured anything, and
        # calling that a finding would put a defect on the target's account.
        print(f"transport-boot: {prov.moved_detail()}", file=sys.stderr)
        outcome, exit_code = "moved", probe_provenance.EXIT_MOVED
    elif failed:
        outcome, exit_code = "findings", EXIT_FINDINGS
    elif unselected:
        outcome, exit_code = "not-measured", EXIT_NOT_MEASURED
    else:
        outcome, exit_code = "green", EXIT_GREEN

    report_path = env.get("BOOT_REPORT")
    if report_path:
        try:
            Path(report_path).write_text(
                json.dumps(
                    {
                        # 3: transports carry `negotiated_protocol_version` and the
                        # `stateless` status; spec_probe.py reads both.
                        "schema": 3,
                        "outcome": outcome,
                        "exit_code": exit_code,
                        "provenance": prov.as_dict(),
                        "derivation": derivation.as_dict(),
                        "transports": [r.as_dict() for r in results],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(
                f"transport-boot: could not write {report_path}: {exc}", file=sys.stderr
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
