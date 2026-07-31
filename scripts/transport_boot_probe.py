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
  127  the HARNESS could not run (an internal error in this script). Only this is
       a HARD failure — a target that will not start is a finding about the
       target, never about the infrastructure. Do not blur those two.

Stdlib only (subprocess/socket/http.client) — it runs inside the TARGET's
environment, where we must not add dependencies.

Env:
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

# Gate contract — deliberately the same numbers as the auditor's own classifier.
EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_CANNOT_RUN = 127

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

_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "mcp-continuous-auditor transport-boot-probe", "version": "1"}

# Directories that are never part of a target's own source.
_SKIP_DIRS = {
    ".git", ".venv", "venv", ".tox", "node_modules", "__pycache__", "dist",
    "build", ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages",
    ".audit", ".eggs",
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
    (re.compile(r"""transport\s*=\s*["']([A-Za-z_-]+)["']"""), ""),   # captured
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
    "**/*.py", "**/Dockerfile*", "**/docker-compose*.yml", "**/docker-compose*.yaml",
    "**/fly.toml", "**/Procfile", "**/*.env.example", "**/.env.example",
    "**/pyproject.toml", "**/README*",
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


def _console_script(root: Path, pyproject: dict[str, Any]) -> tuple[list[str] | None, str]:
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
    for main_py in sorted(root.glob("*/__main__.py")) + sorted(root.glob("src/*/__main__.py")):
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
_GENERIC_LAUNCHER = r'''
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
'''


@dataclass
class LaunchPlan:
    transport: str
    mode: str                       # declared | entrypoint | generic
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""


def build_launch_plan(transport: str, derivation: Derivation) -> LaunchPlan:
    """Pick the most faithful way to start the target for one transport."""
    if transport in derivation.declared:
        return LaunchPlan(
            transport=transport, mode="declared",
            argv=list(derivation.declared[transport]),
            note="argv declared by the target ([tool.mcp_auditor.boot.commands])",
        )
    if derivation.entrypoint:
        return LaunchPlan(
            transport=transport, mode="entrypoint",
            argv=list(derivation.entrypoint),
            note=f"target entrypoint ({derivation.entrypoint_source})",
        )
    return LaunchPlan(
        transport=transport, mode="generic",
        argv=[sys.executable, "-c", _GENERIC_LAUNCHER],
        note="imported server object — case-2 coverage is partial in this mode",
    )


# The env var names targets actually read for transport/host/port. We set all of
# them: an entrypoint that reads only one still gets it, and the extras are inert.
_TRANSPORT_ENV = ("MCP_TRANSPORT", "FASTMCP_TRANSPORT", "TRANSPORT")
_HOST_ENV = ("MCP_HOST", "FASTMCP_HOST", "FASTMCP_SERVER_HOST", "HOST")
_PORT_ENV = ("MCP_PORT", "FASTMCP_PORT", "FASTMCP_SERVER_PORT", "PORT")


def launch_env(plan: LaunchPlan, host: str, port: int, base: dict[str, str] | None = None) -> dict[str, str]:
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport, "mode": self.mode, "ok": self.ok,
            "detail": self.detail, "tools": self.tools,
            "elapsed_s": round(self.elapsed_s, 3), "evidence": self.evidence,
        }


def _rpc(method: str, ident: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
    msg["params"] = params if params is not None else {}
    return msg


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": _CLIENT_INFO,
    }


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
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass


def _close_streams(proc: subprocess.Popen) -> None:
    """Release the pipes once the child is gone. Called only after the last read —
    the gate probes several transports in a row and must not leak a descriptor per
    attempt."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _drain(queue: "Queue[str | None]", sink: list[str], wait: float = 1.0) -> str:
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

def _reader_thread(stream: Any, queue: "Queue[str | None]") -> threading.Thread:
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
            plan.argv, cwd=str(cwd), env=run_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(plan.transport, plan.mode, False,
                           f"could not spawn the target: {type(exc).__name__}: {exc}")

    out_q: "Queue[str | None]" = Queue()
    err_lines: list[str] = []
    _reader_thread(proc.stdout, out_q)
    err_q: "Queue[str | None]" = Queue()
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
                    return None, f"the process exited (rc {proc.returncode}) before responding"
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
                plan.transport, plan.mode, False,
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
                plan.transport, plan.mode, False,
                f"initialize failed: {err}; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )
        if "error" in reply:
            _terminate(proc)
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"initialize returned a JSON-RPC error: {_tail(json.dumps(reply['error']), 200)}",
                elapsed_s=time.monotonic() - started,
            )

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        if not send(_rpc("tools/list", 2)):
            _terminate(proc)
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"the target went away before tools/list; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )

        listing, err = await_response(2)
        if listing is None:
            _terminate(proc)
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"tools/list failed: {err}; stderr: {_tail(drain_stderr())}",
                elapsed_s=time.monotonic() - started,
            )
        if "error" in listing:
            _terminate(proc)
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"tools/list returned a JSON-RPC error: {_tail(json.dumps(listing['error']), 200)}",
                elapsed_s=time.monotonic() - started,
            )

        count = _tool_count(listing.get("result"))
        return ProbeResult(
            plan.transport, plan.mode, True,
            f"initialize + tools/list OK ({count if count is not None else '?'} tool(s))",
            tools=count, elapsed_s=time.monotonic() - started,
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


def wait_for_port(port: int, deadline: float, proc: subprocess.Popen | None = None) -> str:
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
    chunks = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
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


def http_post(port: int, path: str, host_header: str, payload: dict[str, Any],
              timeout: float, session_id: str = "",
              extra_headers: dict[str, str] | None = None) -> HttpReply:
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


def http_get_sse(port: int, path: str, host_header: str, timeout: float,
                 extra_headers: dict[str, str] | None = None) -> HttpReply:
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


def _resolve_path(port: int, candidates: list[str], host_header: str,
                  timeout: float) -> tuple[str, HttpReply | None]:
    """Find the endpoint path by trying the candidates with a real initialize. A
    404/405 means wrong path; anything else (including 421) is the server's real
    answer and ends the search."""
    last: HttpReply | None = None
    for path in candidates:
        try:
            reply = http_post(port, path, host_header, _rpc("initialize", 1, _initialize_params()), timeout)
        except (OSError, http.client.HTTPException):
            continue
        last = reply
        if reply.status not in (404, 405):
            # Report the path the server actually settled on, so every later call
            # goes straight there instead of re-walking the redirect each time.
            return reply.final_path or path, reply
    return (candidates[0] if candidates else "/"), last


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
    argv = substitute(plan.argv, bind_host, port)
    run_env = launch_env(plan, bind_host, port, env)

    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=run_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(plan.transport, plan.mode, False,
                           f"could not spawn the target: {type(exc).__name__}: {exc}")

    out_q: "Queue[str | None]" = Queue()
    _reader_thread(proc.stdout, out_q)
    captured: list[str] = []

    def logs() -> str:
        return _drain(out_q, captured)

    deadline = started + timeout
    try:
        reason = wait_for_port(port, deadline, proc)
        if reason:
            # Case 1 lands here: the process raised at start and never listened.
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"the server never came up: {reason}; output: {_tail(logs())}",
                elapsed_s=time.monotonic() - started,
                evidence={"bind_host": bind_host, "port": port},
            )

        remaining = max(deadline - time.monotonic(), 1.0)
        loop_host = f"127.0.0.1:{port}"
        path, reply = _resolve_path(port, paths, loop_host, min(remaining, 10.0))
        if reply is None:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"the port was open but no endpoint answered on {', '.join(paths)}; output: {_tail(logs())}",
                elapsed_s=time.monotonic() - started,
            )
        if reply.status != 200:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"initialize on {path} returned HTTP {reply.status} even for a loopback Host — "
                f"the transport is broken, not merely host-restricted; body: {_tail(reply.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "loopback_status": reply.status},
            )
        if not isinstance(reply.payload, dict) or "result" not in reply.payload:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"initialize on {path} did not return a JSON-RPC result; body: {_tail(reply.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path},
            )

        session_id = reply.headers.get("mcp-session-id", "")

        # --- CASE 2: the same request, only the Host header differs -------------
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            hostile = http_post(port, path, probe_host,
                                _rpc("initialize", 3, _initialize_params()), min(remaining, 10.0))
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"the non-loopback Host probe could not complete: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "probe_host": probe_host},
            )
        if hostile.status == 421:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"HTTP 421 for Host '{probe_host}' while the loopback Host passed — the inbound "
                "host allow-list is derived from a 127.0.0.1 default, so a 0.0.0.0 deployment "
                "rejects every request made under its real hostname. Pass the configured host "
                "through to the app builder.",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "probe_host": probe_host, "status": 421,
                          "loopback_status": 200, "case": "host-allowlist-421"},
            )
        if hostile.status != 200:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"initialize under Host '{probe_host}' returned HTTP {hostile.status} "
                f"(loopback returned 200); body: {_tail(hostile.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "probe_host": probe_host, "status": hostile.status},
            )

        # --- tools/list on the working session ---------------------------------
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            http_post(port, path, loop_host,
                      {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                      min(remaining, 10.0), session_id)
            listing = http_post(port, path, loop_host, _rpc("tools/list", 2),
                                min(remaining, 10.0), session_id)
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"tools/list could not complete: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )
        if listing.status != 200 or not isinstance(listing.payload, dict) or "result" not in listing.payload:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"tools/list on {path} returned HTTP {listing.status}; body: {_tail(listing.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": path, "status": listing.status},
            )

        count = _tool_count(listing.payload.get("result"))
        return ProbeResult(
            plan.transport, plan.mode, True,
            f"bound {bind_host}:{port}{path} — initialize + tools/list OK under both a loopback "
            f"and a non-loopback Host ({count if count is not None else '?'} tool(s))",
            tools=count, elapsed_s=time.monotonic() - started,
            evidence={"path": path, "probe_host": probe_host, "bind_host": bind_host},
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
    argv = substitute(plan.argv, bind_host, port)
    run_env = launch_env(plan, bind_host, port, env)

    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=run_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(plan.transport, plan.mode, False,
                           f"could not spawn the target: {type(exc).__name__}: {exc}")

    out_q: "Queue[str | None]" = Queue()
    _reader_thread(proc.stdout, out_q)
    captured: list[str] = []

    def logs() -> str:
        return _drain(out_q, captured)

    deadline = started + timeout
    try:
        reason = wait_for_port(port, deadline, proc)
        if reason:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"the server never came up: {reason}; output: {_tail(logs())}",
                elapsed_s=time.monotonic() - started,
            )

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
                plan.transport, plan.mode, False,
                f"no SSE endpoint answered on {', '.join(paths)}; output: {_tail(logs())}",
                elapsed_s=time.monotonic() - started,
            )
        if stream.status != 200:
            return ProbeResult(
                plan.transport, plan.mode, False,
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
                plan.transport, plan.mode, False,
                f"the SSE stream on {used} never announced a message endpoint; body: {_tail(stream.body, 200)}",
                elapsed_s=time.monotonic() - started,
                evidence={"path": used},
            )
        if endpoint.startswith("http"):
            endpoint = "/" + endpoint.split("/", 3)[-1] if endpoint.count("/") >= 3 else "/"

        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            posted = http_post(port, endpoint, loop_host,
                               _rpc("initialize", 1, _initialize_params()), min(remaining, 10.0))
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"posting initialize to the SSE message endpoint failed: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )
        # SSE delivers the reply on the stream, so 200/202 both mean "accepted".
        if posted.status not in (200, 202):
            return ProbeResult(
                plan.transport, plan.mode, False,
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
                plan.transport, plan.mode, False,
                f"the non-loopback Host probe could not complete: {type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
            )
        if hostile.status == 421:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"HTTP 421 for Host '{probe_host}' while the loopback Host passed — the inbound "
                "host allow-list is derived from a 127.0.0.1 default, so a 0.0.0.0 deployment "
                "rejects every request made under its real hostname.",
                elapsed_s=time.monotonic() - started,
                evidence={"path": used, "probe_host": probe_host, "status": 421,
                          "case": "host-allowlist-421"},
            )
        if hostile.status != 200:
            return ProbeResult(
                plan.transport, plan.mode, False,
                f"GET {used} under Host '{probe_host}' returned HTTP {hostile.status} "
                "(loopback returned 200)",
                elapsed_s=time.monotonic() - started,
                evidence={"path": used, "status": hostile.status},
            )

        return ProbeResult(
            plan.transport, plan.mode, True,
            f"bound {bind_host}:{port}{used} — SSE handshake + initialize OK under both a "
            "loopback and a non-loopback Host",
            elapsed_s=time.monotonic() - started,
            evidence={"path": used, "endpoint": endpoint},
        )
    finally:
        _terminate(proc)
        _close_streams(proc)


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

def probe_transport(plan: LaunchPlan, timeout: float, cwd: Path,
                    bind_host: str, probe_host: str,
                    http_paths: list[str], sse_paths: list[str]) -> ProbeResult:
    if plan.transport == STDIO:
        return probe_stdio(plan, timeout, cwd)
    if plan.transport == SSE:
        return probe_sse(plan, timeout, cwd, bind_host, probe_host, sse_paths)
    return probe_streamable_http(plan, timeout, cwd, bind_host, probe_host, http_paths)


def render(results: list[ProbeResult], derivation: Derivation) -> str:
    lines = ["# Transport boot gate", ""]
    lines.append(f"Probed {len(results)} transport(s): "
                 + ", ".join(r.transport for r in results))
    if derivation.floor_added:
        lines.append(f"Added by the floor (always probed): {', '.join(derivation.floor_added)}")
    lines.append("")
    for r in results:
        icon = "✅" if r.ok else "❌"
        lines.append(f"{icon} {r.transport} [{r.mode}] — {r.detail}")
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
        http_paths = [p for p in (env.get("BOOT_HTTP_PATHS") or "/mcp/,/mcp,/").split(",") if p]
        sse_paths = [p for p in (env.get("BOOT_SSE_PATHS") or "/sse/,/sse").split(",") if p]
        derivation = derive(root)
    except Exception as exc:  # noqa: BLE001 - the harness itself failed
        print(f"transport-boot: harness could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    if not derivation.transports:
        # Fail closed, exactly like the schema gate: a target we cannot work out how
        # to boot is a FINDING, not a pass and not an infrastructure failure.
        print("transport-boot: no transport could be derived from the target — treating as a "
              "finding (set BOOT_GATE=off if this target genuinely cannot be booted here)",
              file=sys.stderr)
        return EXIT_FINDINGS

    results: list[ProbeResult] = []
    for transport in derivation.transports:
        plan = build_launch_plan(transport, derivation)
        print(f"==> boot {transport} [{plan.mode}]: {' '.join(plan.argv[:2])}", file=sys.stderr)
        try:
            results.append(probe_transport(plan, timeout, root, bind_host, probe_host,
                                           http_paths, sse_paths))
        except Exception as exc:  # noqa: BLE001 - one probe blowing up is a harness bug
            print(f"transport-boot: probe for {transport} raised: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_CANNOT_RUN

    report = render(results, derivation)
    print(report)

    failed = [r for r in results if not r.ok]
    outcome = "findings" if failed else "green"
    exit_code = EXIT_FINDINGS if failed else EXIT_GREEN

    report_path = env.get("BOOT_REPORT")
    if report_path:
        try:
            Path(report_path).write_text(json.dumps({
                "schema": 1,
                "outcome": outcome,
                "exit_code": exit_code,
                "derivation": derivation.as_dict(),
                "transports": [r.as_dict() for r in results],
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"transport-boot: could not write {report_path}: {exc}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
