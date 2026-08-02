#!/usr/bin/env python3
"""DNS-rebinding gate — does the target's HTTP transport check the name it is
addressed under?

THE ATTACK
----------
A page in the operator's network resolves its own hostname to this server's
address and then talks to it straight out of the browser. Two controls that look
like they should stop it do not:

  * **CORS does not help.** After the rebind the browser considers the request
    same-origin; there is no preflight to fail.
  * **An auth token does not help.** The attacking page runs in a context that
    already holds one — that is the whole point of running it inside the
    operator's network.

The only control that answers the question is the transport's own check of the
``Host`` (and ``Origin``) header: *under which name may this server be
addressed?* It is the inbound counterpart to an egress allow-list — one decides
where the server may speak *to*, this one decides what it may be spoken *to* as.
Retrofitted in malkreide/bag-health-mcp#51 and malkreide/swiss-transport-mcp#25.

WHERE THIS DOES NOT BELONG
--------------------------
Not in ``live_probe.py`` (that probes the UPSTREAM data endpoints — CKAN, WFS —
against fixtures) and not in the promptfoo red-team (that is the LLM layer,
prompt injection). This is an HTTP-transport property of the target server, so it
belongs to the boot harness: it imports ``transport_boot_probe`` and starts the
target through exactly the same launch plan.

THE FOUR PROBES
---------------
The server is booted on a real (0.0.0.0) bind with an allow-list we configure —
``<allowed name>:<port>`` plus the loopback entries an operator always keeps for
container health checks. Every request then goes to 127.0.0.1 on that port (there
is no DNS for an invented name); only the headers differ:

  1. foreign ``Host``                   -> expected REJECTED
  2. right hostname, WRONG port         -> expected REJECTED
  3. right ``Host``, foreign ``Origin`` -> expected REJECTED
  4. right ``Host`` and port            -> expected ACCEPTED

**Why 2 is the load-bearing one.** A probe against ``evil.example.com`` alone
proves nothing: a server that simply falls back to a loopback default policy
rejects it too, and so does one whose transport is broken in some unrelated way.
What no fallback can imitate is the PAIR (2, 4) — two requests differing in
exactly one thing, the port, one rejected and one accepted. Only an allow-list
that is actually in force and compares entries port-exactly produces it. A
loopback fallback rejects both; a list compared on hostname only accepts both.
That is why probe 4 is not decoration: without it, probes 1-3 measure "something
said no", not "the configured allow-list said no".

**Why the token pass is the second load-bearing one.** Everything above runs
twice: once with no auth configured at all, once with ``MCP_AUTH_TOKEN`` set and
a *valid* ``Authorization: Bearer`` on every request. A server that lets a
foreign ``Host`` through as soon as a valid token is presented has not
implemented this control — it has implemented authentication, which the attack
already defeats by construction. The token pass also carries its own control: one
request with a deliberately WRONG token. If that is served too, the target never
enforced auth here, and the pass is recorded as weaker evidence rather than
claimed as proof of independence.

WHAT THIS GATE DOES NOT COVER
-----------------------------
Both passes configure an Origin list (probe 3 is meaningless without one). Some
targets switch app builders when auth OR CORS is configured — bag-health-mcp#51
serves through ``mcp.run()`` otherwise — so on those, both passes exercise the
*built* app and neither exercises the SDK-served path. That split is a target-side
unit test's job; this gate measures the behaviour a client can observe, and would
only see the difference if the two paths disagreed in a deployment we can reach.

THE THREE OUTCOMES (this gate does not have two)
------------------------------------------------
  **enforced** — 1-3 rejected and 4 accepted, in both passes. The control works.

  **not configured** — the target does not honour any inbound host allow-list
  knob, so nothing rejects anything. On a non-loopback bind this is the
  DOCUMENTED fail-open state, not a bug: the servers above ship it off by default
  because guessing a list on ``0.0.0.0`` would reject the very deployment it is
  meant to protect. Reported as its own visible category — never as a pass (the
  control is absent) and never as a finding (nothing is broken). Exit 3.

  **finding** — the target advertises an allow-list knob and a probe still walked
  through, or a valid token walked through, or the result could not be
  attributed. Exit 2.

Two things separate the last two, in this order. First, what the probes show: a
target that refuses SOME hostile probes and serves others took the allow-list and
applied it wrongly — nothing that is merely switched off can produce that mix, so
it is a finding whatever its documentation says. The same goes for a check that
holds until a valid token appears. Only when all three hostile probes are served
alike is there nothing observable to go on, and then the target's own tree
decides: if its source, README, ``.env.example`` or compose file names one of the
allow-list variables, the knob is there and not honouring it is a defect;
otherwise the target never shipped the control and this is a deployment state.

EXIT CODE — the gate contract
-----------------------------
  0    the control is enforced (or no network transport is configured at all,
       in which case there is no rebinding surface to check)
  2    FINDING, including the fail-closed cases: the control could not be
       attributed, or the target never came up. A boot failure is diagnosed by
       the transport boot gate; this gate only records that it could not measure.
  3    the control is NOT CONFIGURED — a category of its own, see above
  127  the HARNESS could not run. Only this is a hard failure.

Set REBIND_GATE=off to skip the gate entirely.

Stdlib only, like the boot probe it builds on: it runs inside the TARGET's
environment, where we must not add dependencies.

Env:
  REBIND_ALLOWED_HOST   hostname configured into the allow-list and used by
                        probes 2-4 (default "mcp-audit-allowed.probe.invalid").
                        Point it at a name the target genuinely allows when the
                        target pins its list in code instead of reading a var.
  REBIND_FOREIGN_HOST   the attacker's name, probe 1 (default
                        "rebind.attacker.probe.invalid")
  REBIND_AUTH_TOKEN     the token configured for the second pass (default: a
                        fresh random one)
  REBIND_TIMEOUT        hard per-boot deadline in seconds (default 30)
  REBIND_BIND_HOST      what the server is told to bind (default "0.0.0.0")
  REBIND_HTTP_PATHS     endpoint paths to try for streamable-http
                        (default "/mcp/,/mcp,/")
  REBIND_SSE_PATHS      ditto for sse (default "/sse/,/sse")
  REBIND_REPORT         write the machine-readable per-probe detail here
  BOOT_TARGET_ROOT / MCP_SERVER_IMPORT / BOOT_TRANSPORTS
                        shared with the boot gate — same target, same launch plan
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transport_boot_probe as tbp  # noqa: E402

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_CONFIGURED = 3
EXIT_CANNOT_RUN = 127

DEFAULT_TIMEOUT = 30
DEFAULT_ALLOWED_HOST = "mcp-audit-allowed.probe.invalid"
DEFAULT_FOREIGN_HOST = "rebind.attacker.probe.invalid"

# The variables a target may read for its inbound allow-list. We set ALL of them:
# one that the target does not read is inert, and the alternative — reading the
# target's mind about which spelling it chose — is exactly the guessing this
# repository refuses everywhere else.
ALLOWLIST_ENV = ("MCP_ALLOWED_HOSTS", "FASTMCP_ALLOWED_HOSTS", "ALLOWED_HOSTS")
ORIGIN_ENV = ("MCP_CORS_ORIGINS", "MCP_ALLOWED_ORIGINS", "FASTMCP_ALLOWED_ORIGINS")
AUTH_ENV = ("MCP_AUTH_TOKEN", "FASTMCP_AUTH_TOKEN", "AUTH_TOKEN")

# Only HTTP transports carry a Host header, so only they have a rebinding surface.
NETWORK_TRANSPORTS = (tbp.STREAMABLE_HTTP, tbp.SSE)

PASS_NO_TOKEN = "no-token"
PASS_VALID_TOKEN = "valid-token"

# Per-probe outcomes.
LET_THROUGH = "let-through"  # the transport processed the request
REJECTED = "rejected"  # the transport refused it (4xx/5xx)
REJECTED_AUTH = "rejected-auth"  # 401 — auth refused, so not attributable here
ERRORED = "errored"  # the request itself failed

# Per-pass / per-transport verdicts.
ENFORCED = "enforced"
NOT_ENFORCED = "not-enforced"
INCONCLUSIVE = "inconclusive"

# Overall gate outcomes.
OUT_ENFORCED = "enforced"
OUT_NOT_CONFIGURED = "not-configured"
OUT_FINDINGS = "findings"
OUT_NOT_APPLICABLE = "not-applicable"


# --------------------------------------------------------------------------
# does the target advertise a knob at all?
# --------------------------------------------------------------------------


@dataclass
class Knob:
    """Whether the target's own tree names an inbound allow-list variable.

    This is the discriminator between the two ways probes 1-3 can pass. A target
    that never heard of the control is in the documented fail-open state; a target
    that ships the variable and still lets a foreign Host through has a control
    that does not work. Same observation, different verdict — so the difference
    must come from the target, not from us."""

    advertised: bool = False
    names: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "advertised": self.advertised,
            "names": self.names,
            "sources": self.sources[:8],
        }


def detect_knob(root: Path, names: tuple[str, ...] = ALLOWLIST_ENV) -> Knob:
    """Look for an allow-list variable name anywhere in the target's own files.

    Matched on identifier boundaries, not as a substring: ``ALLOWED_HOSTS`` sits
    inside ``MCP_ALLOWED_HOSTS``, so a plain ``in`` would report a target as
    naming two knobs when it names one — and the report's job is to say which
    variable the operator has to set.
    """
    knob = Knob()
    patterns = {
        name: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        for name in names
    }
    for path in tbp._iter_scan_files(root):
        text = tbp._read_capped(path)
        if not text:
            continue
        rel = path.relative_to(root).as_posix()
        for name in names:
            if patterns[name].search(text):
                knob.advertised = True
                if name not in knob.names:
                    knob.names.append(name)
                entry = f"{name}: {rel}"
                if entry not in knob.sources:
                    knob.sources.append(entry)
    return knob


# --------------------------------------------------------------------------
# the probe matrix
# --------------------------------------------------------------------------


@dataclass
class ProbeCase:
    name: str
    host: str
    expect: str  # "reject" | "accept"
    why: str
    origin: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "origin": self.origin,
            "expect": self.expect,
            "why": self.why,
        }


def build_cases(allowed_host: str, foreign_host: str, port: int) -> list[ProbeCase]:
    """The four probes, in the order they are reported.

    ``wrong-port`` and ``allowed`` are deliberately adjacent: they differ in
    exactly one thing, the port, and the pair (wrong-port REJECTED, allowed
    ACCEPTED) is the only signature a fallback policy cannot imitate. See the
    module docstring — this is why probing ``evil.example.com`` alone is not a
    check but a coin toss.
    """
    wrong_port = port + 1 if port < 65535 else port - 1
    return [
        ProbeCase(
            name="foreign-host",
            host=f"{foreign_host}:{port}",
            expect="reject",
            why="the rebinding attack itself: a name we never allowed",
        ),
        ProbeCase(
            name="wrong-port",
            host=f"{allowed_host}:{wrong_port}",
            expect="reject",
            why="the allowed hostname on a port that is in no allow-list entry — "
            "rejected only by a list that is in force and compares port-exactly",
        ),
        ProbeCase(
            name="foreign-origin",
            host=f"{allowed_host}:{port}",
            origin=f"https://{foreign_host}",
            expect="reject",
            why="an allowed Host carrying an Origin from the attacking page",
        ),
        ProbeCase(
            name="allowed",
            host=f"{allowed_host}:{port}",
            expect="accept",
            why="the control probe: without it a rejection above proves only that "
            "something said no, not that the configured allow-list said it",
        ),
    ]


@dataclass
class CaseResult:
    case: ProbeCase
    status: int
    outcome: str
    matched: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.case.as_dict(),
            "status": self.status,
            "outcome": self.outcome,
            "matched": self.matched,
            "detail": self.detail,
        }


@dataclass
class PassResult:
    label: str
    token_configured: bool
    auth_enforced: str  # "yes" | "no" | "unknown"
    cases: list[CaseResult] = field(default_factory=list)
    verdict: str = INCONCLUSIVE
    detail: str = ""
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pass": self.label,
            "token_configured": self.token_configured,
            "auth_enforced": self.auth_enforced,
            "verdict": self.verdict,
            "detail": self.detail,
            "path": self.path,
            "cases": [c.as_dict() for c in self.cases],
        }


@dataclass
class TransportResult:
    transport: str
    mode: str
    verdict: str
    detail: str
    passes: list[PassResult] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "mode": self.mode,
            "verdict": self.verdict,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 3),
            "evidence": self.evidence,
            "passes": [p.as_dict() for p in self.passes],
        }


# --------------------------------------------------------------------------
# one request
# --------------------------------------------------------------------------


def _classify(status: int, let_through: bool) -> str:
    if status == 401:
        # Auth refused before we learn anything about the host check. Recorded as
        # its own outcome so it is never counted as "the allow-list rejected it".
        return REJECTED_AUTH
    return LET_THROUGH if let_through else REJECTED


def attempt(
    transport: str,
    port: int,
    path: str,
    case: ProbeCase,
    timeout: float,
    token: str = "",
) -> tuple[int, bool, bool, str]:
    """Issue one probe. Returns (status, let_through, usable, detail).

    ``let_through`` — the transport processed the request at all. That is what
    probes 1-3 must NOT achieve.
    ``usable`` — the server actually answered with a JSON-RPC result. That is
    what probe 4 must achieve; a 200 carrying a JSON-RPC error means the request
    got past the transport but the server is not usable, and the two must not be
    conflated in either direction.
    """
    headers: dict[str, str] = {}
    if case.origin:
        headers["Origin"] = case.origin
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if transport == tbp.SSE:
        reply = tbp.http_get_sse(port, path, case.host, timeout, extra_headers=headers)
        let_through = reply.status == 200
        return reply.status, let_through, let_through, tbp._tail(reply.body, 160)

    reply = tbp.http_post(
        port,
        path,
        case.host,
        tbp._rpc("initialize", 1, tbp._initialize_params()),
        timeout,
        extra_headers=headers,
    )
    let_through = reply.status in (200, 202)
    usable = (
        reply.status == 200
        and isinstance(reply.payload, dict)
        and "result" in reply.payload
    )
    return reply.status, let_through, usable, tbp._tail(reply.body, 160)


# --------------------------------------------------------------------------
# one pass (one boot)
# --------------------------------------------------------------------------


def allowlist_value(allowed_host: str, port: int) -> str:
    """The allow-list we configure. The loopback entries are not padding: an
    operator always keeps them for container health checks (both retrofit PRs say
    so), and the harness itself needs one to discover the endpoint path before it
    can probe anything. They are port-exact like the real entry, so a target that
    compares entries loosely still fails probe 2."""
    return f"127.0.0.1:{port},localhost:{port},{allowed_host}:{port}"


def pass_env(
    base: dict[str, str], allowed_host: str, port: int, token: str
) -> dict[str, str]:
    """The environment one pass boots the target with: the allow-list under every
    spelling a target might read, an Origin list, and the auth token — or, for the
    token-less pass, the auth variables explicitly REMOVED. Inheriting a token
    from the surrounding environment there would silently turn the two passes into
    the same pass."""
    env = dict(base)
    allow = allowlist_value(allowed_host, port)
    for name in ALLOWLIST_ENV:
        env[name] = allow
    # Origins are compared literally, so the configured one names the allowed host
    # and nothing else — probe 3's foreign Origin then has to be refused by the
    # target rather than by a default that happens to be narrow.
    for name in ORIGIN_ENV:
        env[name] = f"https://{allowed_host}"
    for name in AUTH_ENV:
        if token:
            env[name] = token
        else:
            env.pop(name, None)
    return env


def run_pass(
    plan: tbp.LaunchPlan,
    label: str,
    token: str,
    timeout: float,
    cwd: Path,
    bind_host: str,
    allowed_host: str,
    foreign_host: str,
    paths: list[str],
    base_env: dict[str, str] | None = None,
    port: int | None = None,
    path: str = "",
) -> tuple[PassResult, str]:
    """Boot the target once and run the four probes against it.

    ``path`` short-circuits endpoint discovery with a path already found. The
    caller uses it to hand the token pass the path the token-less pass resolved:
    the endpoint is a property of the app, not of the boot, and discovering it
    again under an ``Authorization``-less request would only earn a 401 that says
    nothing about where the endpoint is.

    Returns (result, hard_error). ``hard_error`` is non-empty only when the probe
    could not be carried out at all — the caller turns that into a fail-closed
    finding rather than a silent skip.
    """
    port = tbp.free_port() if port is None else port
    base = dict(os.environ if base_env is None else base_env)
    env = pass_env(base, allowed_host, port, token)
    run_env = tbp.launch_env(plan, bind_host, port, env)
    argv = tbp.substitute(plan.argv, bind_host, port)

    result = PassResult(
        label=label, token_configured=bool(token), auth_enforced="unknown"
    )

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
        return result, f"could not spawn the target: {type(exc).__name__}: {exc}"

    out_q: "Queue[str | None]" = Queue()
    tbp._reader_thread(proc.stdout, out_q)
    captured: list[str] = []
    started = time.monotonic()
    deadline = started + timeout

    try:
        reason = tbp.wait_for_port(port, deadline, proc)
        if reason:
            return result, (
                f"the server never came up: {reason}; output: "
                f"{tbp._tail(tbp._drain(out_q, captured))}"
            )

        # Discover the endpoint path under a loopback Host, which our configured
        # allow-list contains. Doing it once means every probe below hits the same
        # path and a redirect can never be mistaken for a rejection.
        if not path:
            remaining = max(deadline - time.monotonic(), 1.0)
            loop_host = f"127.0.0.1:{port}"
            if plan.transport == tbp.SSE:
                path, opening = _resolve_sse_path(
                    port, paths, loop_host, min(remaining, 10.0), token
                )
                if opening is None:
                    return result, f"no SSE endpoint answered on {', '.join(paths)}"
            else:
                path, opening = tbp._resolve_path(
                    port, paths, loop_host, min(remaining, 10.0)
                )
                if opening is None:
                    return result, f"no endpoint answered on {', '.join(paths)}"
        result.path = path

        for case in build_cases(allowed_host, foreign_host, port):
            remaining = max(deadline - time.monotonic(), 1.0)
            try:
                status, let_through, usable, body = attempt(
                    plan.transport, port, path, case, min(remaining, 10.0), token
                )
            except (OSError, http.client.HTTPException) as exc:
                result.cases.append(
                    CaseResult(case, 0, ERRORED, False, f"{type(exc).__name__}: {exc}")
                )
                continue
            outcome = _classify(status, let_through)
            if case.expect == "accept":
                matched = usable
            else:
                matched = outcome in (REJECTED, REJECTED_AUTH)
            result.cases.append(CaseResult(case, status, outcome, matched, body))

        # The token pass's own control: a WRONG token against the allowed host. If
        # that is served, the target never checked the token here, and "the host
        # check survived a valid token" is a weaker claim than it looks.
        if token:
            remaining = max(deadline - time.monotonic(), 1.0)
            bogus = ProbeCase(
                name="auth-control",
                host=f"{allowed_host}:{port}",
                expect="reject",
                why="wrong token against an allowed Host",
            )
            try:
                _, let_through, _, _ = attempt(
                    plan.transport,
                    port,
                    path,
                    bogus,
                    min(remaining, 10.0),
                    token + "-invalid",
                )
                result.auth_enforced = "no" if let_through else "yes"
            except (OSError, http.client.HTTPException):
                result.auth_enforced = "unknown"

        result.verdict, result.detail = _pass_verdict(result)
        return result, ""
    finally:
        tbp._terminate(proc)
        tbp._close_streams(proc)


def _resolve_sse_path(
    port: int, candidates: list[str], host_header: str, timeout: float, token: str
) -> tuple[str, tbp.HttpReply | None]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    last: tbp.HttpReply | None = None
    for path in candidates:
        try:
            reply = tbp.http_get_sse(
                port, path, host_header, timeout, extra_headers=headers
            )
        except (OSError, http.client.HTTPException):
            continue
        last = reply
        if reply.status not in (404, 405):
            return reply.final_path or path, reply
    return (candidates[0] if candidates else "/"), last


def _pass_verdict(result: PassResult) -> tuple[str, str]:
    """Reduce one pass's four probes to enforced / not-enforced / inconclusive.

    The control probe decides which of the other two is even available: if the
    allowed Host was not served, no rejection above can be attributed to the
    allow-list we configured, and calling that "enforced" would be the exact
    mistake this gate was built to avoid.
    """
    by_name = {c.case.name: c for c in result.cases}
    control = by_name.get("allowed")
    if control is None or control.outcome == ERRORED:
        return INCONCLUSIVE, "the control probe could not be carried out"
    if not control.matched:
        return INCONCLUSIVE, (
            f"the allowed Host '{control.case.host}' was itself refused "
            f"(HTTP {control.status}) — the target did not take the allow-list we "
            "configured, so nothing it rejected can be credited to that list. Point "
            "REBIND_ALLOWED_HOST at a name the target really allows, or make its "
            "list configurable"
        )

    hostile = [
        by_name[n]
        for n in ("foreign-host", "wrong-port", "foreign-origin")
        if n in by_name
    ]
    if any(c.outcome == ERRORED for c in hostile):
        return INCONCLUSIVE, "at least one probe could not be carried out"

    leaked = [c for c in hostile if not c.matched]
    if leaked:
        return NOT_ENFORCED, "served under " + ", ".join(
            f"{c.case.name} (HTTP {c.status})" for c in leaked
        )

    auth_only = [c for c in hostile if c.outcome == REJECTED_AUTH]
    if auth_only:
        return INCONCLUSIVE, (
            "every hostile probe was refused, but "
            + ", ".join(c.case.name for c in auth_only)
            + " came back 401 — that is authentication refusing us, not the host check"
        )
    return ENFORCED, "every hostile probe refused, the allowed Host served"


# --------------------------------------------------------------------------
# one transport (both passes)
# --------------------------------------------------------------------------


def _partially_enforced(passes: list[PassResult]) -> bool:
    """Did the target refuse at least one hostile probe while serving another?

    That combination can only come from an allow-list that IS in force: a target
    honouring nothing serves all three, and a target honouring the list correctly
    refuses all three. Anything in between is our configuration being read and
    then misapplied."""
    for p in passes:
        if p.verdict != NOT_ENFORCED:
            continue
        hostile = [c for c in p.cases if c.case.expect == "reject"]
        if any(c.matched for c in hostile) and any(not c.matched for c in hostile):
            return True
    return False


def probe_transport(
    plan: tbp.LaunchPlan,
    timeout: float,
    cwd: Path,
    bind_host: str,
    allowed_host: str,
    foreign_host: str,
    paths: list[str],
    token: str,
    env: dict[str, str] | None = None,
) -> TransportResult:
    started = time.monotonic()
    passes: list[PassResult] = []
    errors: list[str] = []
    known_path = ""
    for label, tok in ((PASS_NO_TOKEN, ""), (PASS_VALID_TOKEN, token)):
        result, hard = run_pass(
            plan,
            label,
            tok,
            timeout,
            cwd,
            bind_host,
            allowed_host,
            foreign_host,
            paths,
            env,
            path=known_path,
        )
        if hard:
            result.verdict, result.detail = INCONCLUSIVE, hard
            errors.append(f"{label}: {hard}")
        known_path = known_path or result.path
        passes.append(result)

    verdicts = {p.label: p.verdict for p in passes}
    elapsed = time.monotonic() - started

    if errors:
        return TransportResult(
            plan.transport,
            plan.mode,
            INCONCLUSIVE,
            "the control could not be measured — " + "; ".join(errors),
            passes,
            {"errors": errors},
            elapsed,
        )

    # The token case, stated on its own because it is the one a reader will look
    # for: the host check held without auth and folded the moment a valid token
    # was presented. That server has authentication, not a rebinding control.
    if (
        verdicts.get(PASS_NO_TOKEN) == ENFORCED
        and verdicts.get(PASS_VALID_TOKEN) == NOT_ENFORCED
    ):
        return TransportResult(
            plan.transport,
            plan.mode,
            NOT_ENFORCED,
            "a VALID auth token walked past the host check that held without one — "
            "the control is not independent of authentication, and the attacking "
            "page holds a token by construction",
            passes,
            {"case": "token-bypasses-host-check"},
            elapsed,
        )

    if any(v == NOT_ENFORCED for v in verdicts.values()):
        detail = "; ".join(
            f"{p.label}: {p.detail}" for p in passes if p.verdict == NOT_ENFORCED
        )
        # Absent, or present and wrong? Directly observable, so do not leave it to
        # the text scan: a target that refused SOME hostile probes and served
        # others took our allow-list and then applied it badly (compared without
        # the port, checked the Host but not the Origin). That is a defect, not a
        # deployment that never switched the control on.
        if _partially_enforced(passes):
            return TransportResult(
                plan.transport,
                plan.mode,
                NOT_ENFORCED,
                detail + " — the allow-list IS in force (other probes were refused), "
                "so this is a control that is applied wrongly, not one that "
                "was never configured",
                passes,
                {"case": "partial-enforcement"},
                elapsed,
            )
        return TransportResult(
            plan.transport,
            plan.mode,
            NOT_ENFORCED,
            detail,
            passes,
            {"case": "host-check-absent"},
            elapsed,
        )

    if any(v == INCONCLUSIVE for v in verdicts.values()):
        detail = "; ".join(
            f"{p.label}: {p.detail}" for p in passes if p.verdict == INCONCLUSIVE
        )
        return TransportResult(
            plan.transport,
            plan.mode,
            INCONCLUSIVE,
            detail,
            passes,
            {"case": "not-attributable"},
            elapsed,
        )

    weak = [p.label for p in passes if p.token_configured and p.auth_enforced == "no"]
    detail = (
        "the allow-list held against a foreign Host, the allowed hostname on a "
        "wrong port, and a foreign Origin — under both passes"
    )
    if weak:
        detail += (
            " (note: the target served a deliberately WRONG token, so it does not "
            "enforce auth here — the token pass shows the host check is not "
            "weakened by an Authorization header, not that it outranks a real "
            "auth layer)"
        )
    return TransportResult(
        plan.transport,
        plan.mode,
        ENFORCED,
        detail,
        passes,
        {"case": "enforced"},
        elapsed,
    )


# --------------------------------------------------------------------------
# the gate's verdict
# --------------------------------------------------------------------------


def classify(results: list[TransportResult], knob: Knob) -> tuple[str, int, list[str]]:
    """Fold the per-transport verdicts, plus what the target advertises, into the
    gate's three outcomes."""
    reasons: list[str] = []
    if not results:
        return (
            OUT_NOT_APPLICABLE,
            EXIT_GREEN,
            [
                "no network transport is configured — a Host header, and therefore a "
                "rebinding surface, only exists over HTTP"
            ],
        )

    inconclusive = [r for r in results if r.verdict == INCONCLUSIVE]
    not_enforced = [r for r in results if r.verdict == NOT_ENFORCED]
    # Two shapes that are never "not configured", because both prove the target
    # HAS the control and got it wrong: one that works until a token shows up, and
    # one that refuses some hostile probes while serving others.
    present_and_broken = [
        r
        for r in not_enforced
        if r.evidence.get("case")
        in ("token-bypasses-host-check", "partial-enforcement")
    ]

    if inconclusive:
        for r in inconclusive:
            reasons.append(f"{r.transport}: {r.detail}")
        return OUT_FINDINGS, EXIT_FINDINGS, reasons

    if present_and_broken:
        for r in present_and_broken:
            reasons.append(f"{r.transport}: {r.detail}")
        return OUT_FINDINGS, EXIT_FINDINGS, reasons

    if not_enforced:
        for r in not_enforced:
            reasons.append(f"{r.transport}: {r.detail}")
        if knob.advertised:
            reasons.append(
                "the target's own tree names "
                + ", ".join(knob.names)
                + " — the knob is there and the transport does not honour it"
            )
            return OUT_FINDINGS, EXIT_FINDINGS, reasons
        reasons.append(
            "the target names no inbound allow-list variable anywhere, so nothing "
            "was configured to enforce. On a non-loopback bind this is the "
            "documented fail-open state, not a defect — and not a pass either"
        )
        return OUT_NOT_CONFIGURED, EXIT_NOT_CONFIGURED, reasons

    return OUT_ENFORCED, EXIT_GREEN, [f"{r.transport}: {r.detail}" for r in results]


_ICON = {ENFORCED: "✅", NOT_ENFORCED: "❌", INCONCLUSIVE: "⚠️"}


def render(
    results: list[TransportResult], knob: Knob, outcome: str, reasons: list[str]
) -> str:
    head = {
        OUT_ENFORCED: "✅ The inbound host allow-list is enforced.",
        OUT_NOT_CONFIGURED: "🟡 Control NOT CONFIGURED — neither a pass nor a finding.",
        OUT_FINDINGS: "🚨 Finding — the inbound host check did not hold.",
        OUT_NOT_APPLICABLE: "➖ No network transport — nothing to check.",
    }[outcome]
    lines = ["# DNS-rebinding gate (inbound Host/Origin allow-list)", "", head, ""]

    if knob.advertised:
        lines.append(f"Target advertises an allow-list knob: {', '.join(knob.names)}")
    else:
        lines.append("Target advertises no allow-list knob.")
    lines.append("")

    for r in results:
        lines.append(
            f"{_ICON.get(r.verdict, '?')} {r.transport} [{r.mode}] — {r.detail}"
        )
        for p in r.passes:
            probes = ", ".join(
                f"{c.case.name}={'ok' if c.matched else 'MISS'}({c.status})"
                for c in p.cases
            )
            lines.append(
                f"    · {p.label} (auth enforced: {p.auth_enforced}): {probes}"
            )
    if reasons:
        lines += ["", "## Why"] + [f"- {x}" for x in reasons]

    if outcome == OUT_NOT_CONFIGURED:
        lines += [
            "",
            "> A server without a configured allow-list does not reject anything on a "
            "non-loopback bind — that is the documented behaviour, because guessing a "
            "list on `0.0.0.0` would reject the deployment it is meant to protect. It "
            "is reported here so the absence is *visible*: the rebinding attack is "
            "unopposed, and no probe above can be read as evidence that it is not.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        root = Path(env.get("BOOT_TARGET_ROOT") or ".").resolve()
        timeout = float(env.get("REBIND_TIMEOUT") or DEFAULT_TIMEOUT)
        bind_host = env.get("REBIND_BIND_HOST") or tbp.DEFAULT_BIND_HOST
        allowed_host = env.get("REBIND_ALLOWED_HOST") or DEFAULT_ALLOWED_HOST
        foreign_host = env.get("REBIND_FOREIGN_HOST") or DEFAULT_FOREIGN_HOST
        token = env.get("REBIND_AUTH_TOKEN") or secrets.token_urlsafe(24)
        http_paths = [
            p for p in (env.get("REBIND_HTTP_PATHS") or "/mcp/,/mcp,/").split(",") if p
        ]
        sse_paths = [
            p for p in (env.get("REBIND_SSE_PATHS") or "/sse/,/sse").split(",") if p
        ]
        derivation = tbp.derive(root)
        knob = detect_knob(root)
    except Exception as exc:  # noqa: BLE001 - the harness itself failed
        print(
            f"rebind: harness could not run: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    transports = [t for t in derivation.transports if t in NETWORK_TRANSPORTS]
    results: list[TransportResult] = []
    for transport in transports:
        plan = tbp.build_launch_plan(transport, derivation)
        paths = sse_paths if transport == tbp.SSE else http_paths
        print(f"==> rebind {transport} [{plan.mode}]", file=sys.stderr)
        try:
            results.append(
                probe_transport(
                    plan,
                    timeout,
                    root,
                    bind_host,
                    allowed_host,
                    foreign_host,
                    paths,
                    token,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a probe blowing up is a harness bug
            print(
                f"rebind: probe for {transport} raised: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN

    outcome, exit_code, reasons = classify(results, knob)
    print(render(results, knob, outcome, reasons))

    report_path = env.get("REBIND_REPORT")
    if report_path:
        try:
            Path(report_path).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "outcome": outcome,
                        "exit_code": exit_code,
                        "reasons": reasons,
                        "knob": knob.as_dict(),
                        "allowed_host": allowed_host,
                        "foreign_host": foreign_host,
                        "transports": [r.as_dict() for r in results],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"rebind: could not write {report_path}: {exc}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
