#!/usr/bin/env python3
"""Spec probe — which MCP protocol version does this server actually speak?

THE OCCASION
------------
MCP spec ``2026-07-28`` removes the handshake. ``initialize``/``initialized``
and ``Mcp-Session-Id`` are gone; every request carries its protocol version,
``clientInfo`` and capabilities in ``_meta``. The legacy HTTP+SSE transport is
deprecated with a twelve-month window.

That turns a question nobody in this repository could ask into the one that
decides a migration: *which spec is this server on?* Before this probe the
answer existed nowhere. ``scripts/transport_boot_probe.py`` carried a single
hand-maintained literal (``_PROTOCOL_VERSION = "2025-06-18"``), sent it in
``initialize`` and in the ``MCP-Protocol-Version`` header of every POST, and
**never read the answer back**. The negotiated version came in on
``result.protocolVersion`` and was discarded; only ``result.tools`` was ever
looked at. One literal fanned out through ``shipped_probe`` and ``rebind_probe``
into three gates, and none of the three could tell you what it had been talking
to.

That is the same defect class ``identity_probe`` exists to catch — a
hand-maintained version that drifts while nothing breaks and no test fails —
sitting in the auditor's own source. This probe is the other half of the fix:
``transport_boot_probe`` now reads the negotiated version, and this reads every
place the version is *claimed* and compares them.

FOUR SOURCES, AND THEY DISAGREE INDEPENDENTLY
---------------------------------------------
``code``       what the target's own source declares, if it declares anything
``artifact``   what the INSTALLED SDK will actually put on the wire — the only
               artifact-level evidence, exactly like ``identity_probe --installed``
``portfolio``  ``mcp_spec_version`` from the index repo's ``portfolio.json``
``wire``       what a running server negotiates, measured

Any two known values that disagree are ``SPEC_DRIFT``. The interesting pair is
``portfolio`` against ``wire``: a migration tracker is a plan, and a plan that
has drifted from the deployment is worse than no plan, because it is consulted.

THE STATUS VOCABULARY, AND WHY IT HAS FOUR ENTRIES AND NOT THREE
----------------------------------------------------------------
``SPEC_DRIFT``       two sources name different versions
``LEGACY_TRANSPORT`` the wire is demonstrably on a deprecated form — a reachable
                     ``/sse`` endpoint, an issued ``Mcp-Session-Id``, or a
                     stateless call the server refuses. Carries the remaining
                     days of the deprecation window
``UNVERIFIED``       a source could not be read. NEVER rendered as "in sync"
``SPEC_UNDECLARED``  the source declares no protocol version at all

The last one is a NOTE, not a finding, and that is deliberate. Under the current
SDKs the protocol version belongs to the SDK, not to the server: 39 of the 42
servers in this portfolio declare nothing, and they are right not to. A
predicate that turned red on all 39 would be switched off within a day, and a
switched-off check catches nothing. What ``SPEC_UNDECLARED`` buys is that the
report says *why* it has no code-level value, instead of leaving a blank that
reads like agreement.

WHAT THIS PROBE DOES NOT KNOW
-----------------------------
It was written against a written summary of spec ``2026-07-28``, not against the
spec document. Three rules are therefore ASSUMPTIONS and are marked as such in
the report rather than asserted:

  * ``Mcp-Method`` and ``Mcp-Name`` are mandatory headers on Streamable HTTP
  * ``initialize`` and ``Mcp-Session-Id`` are removed rather than optional
  * the deprecation window is twelve months from ``2026-07-28``

The wire mode MEASURES all three rather than assuming them — it sends a request
with the headers and one without, and reports both answers. Where a measurement
cannot separate two explanations, it says ``UNVERIFIED`` and names both. Pass
``--spec-verified`` once the rules have been checked against the document; that
flag only changes the wording of the report, never a verdict, because a verdict
that moved when somebody set a flag would not be a measurement.

READ-ONLY
---------
Every request is a GET or a JSON-RPC read (``tools/list``, ``initialize``,
``server/discover``). This probe recommends a migration; it never performs one.

EXIT CODES
  0    every readable source agrees and the wire is not on a deprecated form
  2    FINDING — ``SPEC_DRIFT`` or ``LEGACY_TRANSPORT``
  3    NOT MEASURED — no source could be read at all
  4    ``MOVED_DURING_RUN`` — the checkout changed under the probe
  127  the HARNESS could not run

Usage:
  python scripts/spec_probe.py --target ../zurich-opendata-mcp
  python scripts/spec_probe.py --target . --installed
  python scripts/spec_probe.py --url https://example.invalid/mcp --format json
  python scripts/spec_probe.py --target . --portfolio ../swiss-public-data-mcp/portfolio.json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

SPEC_DRIFT = "SPEC_DRIFT"
LEGACY_TRANSPORT = "LEGACY_TRANSPORT"
UNVERIFIED = "UNVERIFIED"
SPEC_UNDECLARED = "SPEC_UNDECLARED"

# The spec this portfolio is migrating to, and the deprecation clock that came
# with it. Both are DATA, not policy: `--spec`/`--deprecated-on` move them, so a
# later spec does not require editing this file. Taken from the migration brief —
# see "WHAT THIS PROBE DOES NOT KNOW".
TARGET_SPEC = "2026-07-28"
DEPRECATION_ANNOUNCED = "2026-07-28"
DEPRECATION_WINDOW_MONTHS = 12

# The versions a server can plausibly be on. Used only to recognise a date-shaped
# literal as a protocol version rather than as a release date; an unknown value
# is still reported, never filtered away.
KNOWN_SPECS = ("2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28")

DEFAULT_TIMEOUT = 15.0

# A date-shaped literal. MCP spec versions are dates, which is what makes them
# greppable — and also what makes a naive grep find every changelog entry, so the
# match is only taken when the surrounding line is about a protocol version.
_SPEC_DATE = re.compile(r"""["'](20\d{2}-[01]\d-[0-3]\d)["']""")
_SPEC_CONTEXT = re.compile(
    r"protocol[_\s]*version|protocolVersion|spec[_\s]*version|LATEST_PROTOCOL|"
    r"MCP[_-]PROTOCOL",
    re.IGNORECASE,
)

# Directories that are never a target's own source (mirrors the other probes).
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


# --------------------------------------------------------------------------
# the deprecation clock
# --------------------------------------------------------------------------


def deadline(announced: str = DEPRECATION_ANNOUNCED, months: int = 12) -> date:
    """The day the deprecated form stops being allowed.

    Month arithmetic without dateutil: add whole months and clamp the day, which
    is exact for the only case that matters here (a 12-month window lands on the
    same day of the same month a year later).
    """
    start = date.fromisoformat(announced)
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    day = min(
        start.day,
        [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
            month - 1
        ],
    )
    return date(year, month, day)


def days_left(today: date, until: date) -> int:
    return (until - today).days


def countdown(today: date, until: date) -> str:
    """The sentence a LEGACY_TRANSPORT finding carries.

    A countdown and not a boolean, because "deprecated" is not actionable and
    "271 days" is. Past the deadline it says so plainly rather than printing a
    negative number, which reads as a bug and gets ignored.
    """
    left = days_left(today, until)
    if left < 0:
        return (
            f"the deprecation window closed {abs(left)} day(s) ago "
            f"({until.isoformat()}) — this is no longer a countdown"
        )
    return f"{left} day(s) left in the deprecation window (until {until.isoformat()})"


# --------------------------------------------------------------------------
# source 1 — the code
# --------------------------------------------------------------------------


def code_lines(text: str) -> list[str]:
    """Lines with comments blanked out.

    Same rule and the same reason as ``identity_probe.code_lines``: a comment
    that documents an old protocol version is documentation, and a check that
    turns red on documentation teaches people to delete it.
    """
    lines = text.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                lines[row - 1] = lines[row - 1][:col]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text.splitlines()  # unparseable: check it whole rather than skip it
    return lines


@dataclass
class Declared:
    """What the target's own source says, and where."""

    versions: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)

    @property
    def value(self) -> str:
        return self.versions[0] if self.versions else ""


def scan_code(root: Path) -> Declared:
    """Protocol-version literals in the target's source.

    Scans ``src/`` when it exists and the repository root otherwise — a src-layout
    is the portfolio convention but not a guarantee, and a probe that finds
    nothing because it looked in the wrong directory would report a flat-layout
    server as ``SPEC_UNDECLARED``, which is a false statement rather than a
    missing one.
    """
    out = Declared()
    base = root / "src" if (root / "src").is_dir() else root
    for path in sorted(base.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(code_lines(text), 1):
            if not _SPEC_CONTEXT.search(line):
                continue
            for m in _SPEC_DATE.finditer(line):
                value = m.group(1)
                if value not in out.versions:
                    out.versions.append(value)
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:  # pragma: no cover - path outside the root
                    rel = path.name
                out.sites.append(f"{rel}:{lineno} → {value}")
    return out


# --------------------------------------------------------------------------
# source 2 — the installed artifact
# --------------------------------------------------------------------------


def scan_artifact() -> dict[str, str]:
    """What the INSTALLED SDK will put on the wire.

    The artifact-level check, and the only one that answers "what actually
    ships". A target's source can be perfectly migrated while the environment it
    runs in resolves an SDK that speaks the old spec — and the reverse, which is
    the case during this migration: nothing in the source changes at all, the SDK
    version does, and no source-level check can see it.

    ``LATEST_PROTOCOL_VERSION`` is read from whichever SDK is importable. Two
    different projects are called FastMCP and cannot share an environment (see
    ``promptfoo/providers/call_tool.py``), so both are tried and neither is
    required.
    """
    out: dict[str, str] = {}
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib
        return {"status": UNVERIFIED, "detail": "importlib.metadata is unavailable"}

    for dist in ("mcp", "fastmcp"):
        try:
            out[f"{dist}_version"] = version(dist)
        except PackageNotFoundError:
            continue

    if not out:
        return {
            "status": UNVERIFIED,
            "detail": (
                "neither `mcp` nor `fastmcp` is installed in this interpreter — "
                "source checked, artifact not. Run this inside the target's "
                "environment (`uv run scripts/spec_probe.py --installed`) to get "
                "the artifact-level answer"
            ),
        }

    for module, attr in (
        ("mcp.types", "LATEST_PROTOCOL_VERSION"),
        ("fastmcp", "LATEST_PROTOCOL_VERSION"),
    ):
        try:
            mod = __import__(module, fromlist=[attr])
        except Exception:  # noqa: BLE001 - an SDK that will not import is not a verdict
            continue
        value = getattr(mod, attr, None)
        if isinstance(value, str) and _SPEC_DATE.fullmatch(f'"{value}"'):
            out["version"] = value
            out["source"] = f"{module}.{attr}"
            out["status"] = "ok"
            return out

    out["status"] = UNVERIFIED
    out["detail"] = (
        "the installed SDK exposes no readable protocol-version constant "
        "(looked for mcp.types.LATEST_PROTOCOL_VERSION and "
        "fastmcp.LATEST_PROTOCOL_VERSION) — the SDK version above is recorded, "
        "but what it puts on the wire was not established"
    )
    return out


# --------------------------------------------------------------------------
# source 3 — portfolio.json
# --------------------------------------------------------------------------


def scan_portfolio(path: Path, dist: str) -> dict[str, str]:
    """``mcp_spec_version`` for one server, out of the index repo's tracker.

    The tracker lives in another repository, so it is opt-in via ``--portfolio``
    and its ABSENCE IS NOT AGREEMENT. A probe that silently skipped it would
    report "every readable source agrees" about two sources while the third — the
    one the migration is actually steered by — was never opened.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": UNVERIFIED,
            "detail": f"could not read {path}: {type(exc).__name__}: {exc}",
        }

    entries = data.get("servers") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return {
            "status": UNVERIFIED,
            "detail": f"{path}: no `servers` list — the tracker's shape is not what this probe reads",
        }

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        names = {
            str(entry.get(key, "")).strip()
            for key in ("name", "repo", "pypi_package", "package")
        }
        names |= {n.rsplit("/", 1)[-1] for n in names if n}
        if dist and (dist in names or dist.replace("_", "-") in names):
            value = str(entry.get("mcp_spec_version") or "").strip()
            if not value:
                return {
                    "status": SPEC_UNDECLARED,
                    "detail": f"{path}: `{dist}` carries no `mcp_spec_version`",
                    "wave": str(entry.get("migration_wave") or ""),
                }
            return {
                "status": "ok",
                "version": value,
                "wave": str(entry.get("migration_wave") or ""),
                "migration_status": str(entry.get("migration_status") or ""),
            }
    return {
        "status": UNVERIFIED,
        "detail": (
            f"{path}: no entry for `{dist}`. The server is either absent from the "
            "tracker or listed under a name this probe did not match — both mean "
            "the tracker was NOT compared, not that it agrees"
        ),
    }


# --------------------------------------------------------------------------
# source 4 — the wire
# --------------------------------------------------------------------------


@dataclass
class WireReply:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    payload: Any = None
    error: str = ""

    @property
    def reached(self) -> bool:
        return self.status > 0 and not self.error

    @property
    def rpc_ok(self) -> bool:
        return (
            self.reached
            and self.status in (200, 202)
            and isinstance(self.payload, dict)
            and "result" in self.payload
        )


def _parse_sse(body: str) -> Any:
    for line in reversed(body.splitlines()):
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    return None


def _decode(headers: dict[str, str], body: str) -> Any:
    if "text/event-stream" in (headers.get("content-type") or "").lower():
        return _parse_sse(body)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _parse_sse(body)


def request(
    url: str,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> WireReply:
    """One HTTP request against a URL. Never raises — an unreachable server is a
    measurement outcome, not a harness failure.

    stdlib ``http.client`` rather than httpx: this probe runs on the
    credential-free Worker, where a dependency is a liability.
    """
    import http.client  # noqa: PLC0415 - kept local so the module imports offline

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return WireReply(0, error=f"not an http(s) URL: {url!r}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    conn_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    body = json.dumps(payload) if payload is not None else None
    send = dict(headers or {})
    if body is not None:
        send.setdefault("Content-Type", "application/json")
    send.setdefault("Accept", "application/json, text/event-stream")

    try:
        conn = conn_cls(parsed.hostname, parsed.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=send)
            resp = conn.getresponse()
            raw = resp.read(64_000).decode("utf-8", errors="replace")
            got = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - every network failure is data here
        return WireReply(0, error=f"{type(exc).__name__}: {exc}")
    return WireReply(resp.status, got, raw, _decode(got, raw))


def _rpc(
    method: str, ident: int, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}


def _stateless_meta(spec: str) -> dict[str, Any]:
    """The ``_meta`` block a stateless request carries instead of a handshake.

    Per the migration brief: protocol version, clientInfo and capabilities travel
    with every request. A server on the old spec ignores an unknown ``_meta`` key,
    so sending it costs nothing and is the only way to ask the question.
    """
    return {
        "protocolVersion": spec,
        "clientInfo": {"name": "mcp-continuous-auditor spec-probe", "version": "1"},
        "capabilities": {},
    }


@dataclass
class WireResult:
    url: str
    reachable: bool = False
    negotiated: str = ""
    stateless_ok: bool | None = None
    stateless_with_headers: int = 0
    stateless_without_headers: int = 0
    handshake_ok: bool | None = None
    session_id: str = ""
    sse_endpoint: str = ""
    discover_ok: bool | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "reachable": self.reachable,
            "negotiated": self.negotiated,
            "stateless_ok": self.stateless_ok,
            "stateless_status_with_required_headers": self.stateless_with_headers,
            "stateless_status_without_required_headers": self.stateless_without_headers,
            "handshake_ok": self.handshake_ok,
            "session_id_issued": bool(self.session_id),
            "sse_endpoint": self.sse_endpoint,
            "discover_ok": self.discover_ok,
            "notes": list(self.notes),
        }


def probe_wire(
    url: str, spec: str = TARGET_SPEC, timeout: float = DEFAULT_TIMEOUT
) -> WireResult:
    """What a running server actually does. Four measurements, in this order.

    The ORDER is the design. A handshake-first probe cannot distinguish "this
    server has migrated" from "this server is broken" — which is precisely the
    false finding ``transport_boot_probe`` used to produce, and the reason a
    migrated server would have been issued a bug report by its own auditor. So
    the stateless call goes FIRST and the handshake is asked afterwards, as a
    second data point rather than as a gate.

    Both header variants are sent because the brief's claim that ``Mcp-Method``
    and ``Mcp-Name`` are mandatory is unverified here. Sending one request each
    way turns an assumption into two measured status codes, and the report prints
    both rather than picking the one that suits a conclusion.
    """
    out = WireResult(url=url)

    # 1) a real call with NO handshake in front of it, carrying the new headers.
    listed = request(
        url,
        payload={**_rpc("tools/list", 1), "_meta": _stateless_meta(spec)},
        headers={
            "Mcp-Method": "tools/list",
            "Mcp-Name": "",
            "MCP-Protocol-Version": spec,
        },
        timeout=timeout,
    )
    out.reachable = listed.reached
    out.stateless_with_headers = listed.status
    if listed.error:
        out.notes.append(f"the endpoint could not be reached: {listed.error}")
        return out
    out.stateless_ok = listed.rpc_ok

    # 2) the same call WITHOUT the headers the new spec is said to require.
    #    Two status codes, one difference, no assumption.
    bare = request(
        url,
        payload={**_rpc("tools/list", 2), "_meta": _stateless_meta(spec)},
        headers={"MCP-Protocol-Version": spec},
        timeout=timeout,
    )
    out.stateless_without_headers = bare.status
    if listed.rpc_ok and not bare.rpc_ok:
        out.notes.append(
            "the stateless call succeeds WITH `Mcp-Method`/`Mcp-Name` and fails "
            f"without them (HTTP {bare.status}) — consistent with the headers "
            "being mandatory, which this probe takes from the migration brief "
            "and has not verified against the spec document"
        )
    elif not listed.rpc_ok and bare.rpc_ok:
        out.notes.append(
            "the stateless call succeeds WITHOUT the new headers and fails with "
            f"them (HTTP {listed.status}) — the server rejects headers it does "
            "not know, which is a finding in its own right"
        )
        out.stateless_ok = True

    # 3) the handshake. Present or absent, both are information.
    hand = request(
        url,
        payload=_rpc(
            "initialize",
            3,
            {
                "protocolVersion": spec,
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp-continuous-auditor spec-probe",
                    "version": "1",
                },
            },
        ),
        headers={
            "Mcp-Method": "initialize",
            "Mcp-Name": "",
            "MCP-Protocol-Version": spec,
        },
        timeout=timeout,
    )
    out.handshake_ok = hand.rpc_ok
    if hand.rpc_ok and isinstance(hand.payload, dict):
        result = hand.payload.get("result")
        if isinstance(result, dict):
            out.negotiated = str(result.get("protocolVersion") or "")
    out.session_id = hand.headers.get("mcp-session-id", "")

    # 4) the legacy SSE endpoint, asked for by name.
    sse_url = (
        re.sub(r"/(mcp|messages)/?$", "/sse", url)
        if re.search(r"/(mcp|messages)/?$", url)
        else url.rstrip("/") + "/sse"
    )
    sse = request(
        sse_url,
        method="GET",
        headers={"Accept": "text/event-stream"},
        timeout=min(timeout, 5.0),
    )
    if (
        sse.reached
        and sse.status == 200
        and "text/event-stream" in (sse.headers.get("content-type") or "").lower()
    ):
        out.sse_endpoint = sse_url

    # 5) the optional stateless discovery RPC. Its absence is not a finding — it
    #    is optional in the spec — but its PRESENCE is positive evidence of a
    #    migrated server, which nothing else here provides.
    disc = request(
        url,
        payload={**_rpc("server/discover", 4), "_meta": _stateless_meta(spec)},
        headers={
            "Mcp-Method": "server/discover",
            "Mcp-Name": "",
            "MCP-Protocol-Version": spec,
        },
        timeout=timeout,
    )
    out.discover_ok = disc.rpc_ok if disc.reached else None
    return out


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    severity: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass
class Report:
    target: str
    dist: str = ""
    spec: str = TARGET_SPEC
    code: Declared = field(default_factory=Declared)
    artifact: dict[str, str] = field(default_factory=dict)
    portfolio: dict[str, str] = field(default_factory=dict)
    wire: WireResult | None = None
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)
    harness_error: str = ""
    spec_verified: bool = False
    today: date = field(default_factory=lambda: datetime.now(UTC).date())
    until: date = field(default_factory=deadline)
    provenance: probe_provenance.Provenance | None = None

    @property
    def measured(self) -> dict[str, str]:
        """Every source that produced an actual version string."""
        out: dict[str, str] = {}
        if self.code.value:
            out["code"] = self.code.value
        if self.artifact.get("version"):
            out["artifact"] = self.artifact["version"]
        if self.portfolio.get("version"):
            out["portfolio"] = self.portfolio["version"]
        if self.wire is not None and self.wire.negotiated:
            out["wire"] = self.wire.negotiated
        return out

    def exit_code(self) -> int:
        if self.provenance is not None and self.provenance.blocking:
            return probe_provenance.EXIT_MOVED
        if self.harness_error:
            return EXIT_CANNOT_RUN
        if self.findings:
            return EXIT_FINDINGS
        if not self.measured and self.wire is None:
            return EXIT_NOT_MEASURED
        return EXIT_GREEN

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "probe": "spec",
            "target": self.target,
            "dist": self.dist,
            "target_spec": self.spec,
            "spec_rules_verified": self.spec_verified,
            "deprecation_deadline": self.until.isoformat(),
            "days_left": days_left(self.today, self.until),
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "sources": {
                "code": {"version": self.code.value, "sites": list(self.code.sites)},
                "artifact": dict(self.artifact),
                "portfolio": dict(self.portfolio),
                "wire": self.wire.as_dict() if self.wire else None,
            },
            "measured": self.measured,
            "notes": list(self.notes),
            "unmeasured": list(self.unmeasured),
            "findings": [f.as_dict() for f in self.findings],
            "harness_error": self.harness_error,
            "exit_code": self.exit_code(),
        }


def classify(report: Report) -> None:
    """Turn the four sources into findings. Pure — this is what the tests own."""
    measured = report.measured

    # --- SPEC_DRIFT: two readable sources that do not agree ------------------
    distinct = sorted(set(measured.values()))
    if len(distinct) > 1:
        where = ", ".join(f"{k}={v}" for k, v in sorted(measured.items()))
        report.findings.append(
            Finding(
                SPEC_DRIFT,
                "high",
                f"the sources name different protocol versions ({where}). The pair "
                "that matters is `portfolio` against `wire`: a migration tracker "
                "that disagrees with the deployment is worse than no tracker, "
                "because it is the one people consult",
            )
        )

    # --- LEGACY_TRANSPORT: the wire is demonstrably on a deprecated form -----
    wire = report.wire
    if wire is not None and wire.reachable:
        legacy: list[str] = []
        if wire.sse_endpoint:
            legacy.append(f"a legacy HTTP+SSE endpoint answers at {wire.sse_endpoint}")
        if wire.session_id:
            legacy.append(
                f"the server issues an `Mcp-Session-Id` ({wire.session_id[:12]}…), "
                "which the stateless core removed"
            )
        if wire.stateless_ok is False:
            legacy.append(
                "a call without a preceding handshake was refused "
                f"(HTTP {wire.stateless_with_headers}) — the server still requires "
                "`initialize`"
            )
        if legacy:
            report.findings.append(
                Finding(
                    LEGACY_TRANSPORT,
                    "medium",
                    "; ".join(legacy)
                    + ". "
                    + countdown(report.today, report.until)
                    + ". This is a RECOMMENDATION with a date, not a gate: the "
                    "deprecated form is valid until then, and a probe that failed "
                    "the build today would be asserting a rule that does not yet "
                    "apply",
                )
            )

    # --- what was NOT measured, named rather than left blank ----------------
    if not report.code.value:
        report.notes.append(
            f"{SPEC_UNDECLARED}: the target's source declares no protocol version. "
            "Under the current SDKs that is CORRECT and not a finding — the "
            "version belongs to the SDK. It is recorded so the blank in the table "
            "is not read as agreement"
        )
    if report.artifact.get("status") == UNVERIFIED:
        report.unmeasured.append(f"artifact: {report.artifact.get('detail', '')}")
    if report.portfolio.get("status") == UNVERIFIED:
        report.unmeasured.append(f"portfolio: {report.portfolio.get('detail', '')}")
    elif not report.portfolio:
        report.unmeasured.append(
            "portfolio: no --portfolio given, so `mcp_spec_version` was not "
            "compared. Absence is not agreement"
        )
    if report.wire is None:
        report.unmeasured.append(
            "wire: no --url given, so nothing was measured against a running "
            "server. Every version above is a CLAIM; only the wire is evidence"
        )
    elif not report.wire.reachable:
        report.unmeasured.append(
            f"wire: {'; '.join(report.wire.notes) or 'the endpoint did not answer'}"
        )


def run(
    target: Path,
    *,
    installed: bool = False,
    portfolio: Path | None = None,
    url: str = "",
    spec: str = TARGET_SPEC,
    today: date | None = None,
    until: date | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    spec_verified: bool = False,
) -> Report:
    report = Report(
        target=str(target),
        spec=spec,
        spec_verified=spec_verified,
        today=today or datetime.now(UTC).date(),
        until=until or deadline(),
    )

    if target.is_dir():
        report.dist = _dist_name(target)
        report.code = scan_code(target)
    elif not url:
        report.harness_error = f"{target} is not a directory and no --url was given"
        return report

    if installed:
        report.artifact = scan_artifact()
    if portfolio is not None:
        report.portfolio = scan_portfolio(portfolio, report.dist)
    if url:
        report.wire = probe_wire(url, spec=spec, timeout=timeout)

    classify(report)
    return report


def _dist_name(root: Path) -> str:
    """``[project].name`` — needed to look the server up in the tracker."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return ""
    try:
        import tomllib  # noqa: PLC0415
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        return ""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return ""
    project = data.get("project")
    return str(project.get("name", "")) if isinstance(project, dict) else ""


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def render(report: Report) -> str:
    lines = [
        f"spec probe — {report.target or report.wire.url if report.wire else report.target}"
    ]
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
        if report.provenance.blocking:
            lines.append(f"  {report.provenance.moved_detail()}")
            return "\n".join(lines)
    if report.harness_error:
        lines.append(f"  HARNESS: {report.harness_error}")
        return "\n".join(lines)

    lines.append(
        f"  target spec {report.spec} · {countdown(report.today, report.until)}"
    )
    if not report.spec_verified:
        lines.append(
            "  note: the spec rules this probe applies come from a migration "
            "brief, not from the spec document. The wire mode MEASURES them "
            "rather than assuming them; pass --spec-verified once checked"
        )

    lines.append("  sources:")
    for name, value in sorted(report.measured.items()):
        lines.append(f"    {name:9s} {value}")
    if not report.measured:
        lines.append("    (none produced a version string)")

    wire = report.wire
    if wire is not None and wire.reachable:
        lines.append("  wire behaviour:")
        lines.append(
            f"    stateless={_tri(wire.stateless_ok)} "
            f"handshake={_tri(wire.handshake_ok)} "
            f"session_id={'yes' if wire.session_id else 'no'} "
            f"sse={'yes' if wire.sse_endpoint else 'no'} "
            f"discover={_tri(wire.discover_ok)}"
        )
        for note in wire.notes:
            lines.append(f"      note: {note}")

    for note in report.notes:
        lines.append(f"  {note}")
    for finding in report.findings:
        lines.append(f"  {finding.code} [{finding.severity}] {finding.detail}")
    if not report.findings:
        lines.append("  no drift among the sources that were readable")
    for gap in report.unmeasured:
        lines.append(f"  UNVERIFIED {gap}")
    if report.unmeasured:
        lines.append(
            "  These are gaps, not green cells. A source that was not read says "
            "nothing about this server."
        )
    return "\n".join(lines)


def _tri(value: bool | None) -> str:
    return "?" if value is None else ("yes" if value else "no")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", default=".", help="path to the MCP server checkout")
    p.add_argument(
        "--installed",
        action="store_true",
        help="also resolve the protocol version from the INSTALLED SDK "
        "(artifact-level; run inside the target's environment)",
    )
    p.add_argument(
        "--portfolio",
        default="",
        help="path to the index repo's portfolio.json, for the `mcp_spec_version` "
        "comparison. Omitted means NOT COMPARED, never `agrees`",
    )
    p.add_argument(
        "--url",
        default="",
        help="a running server's MCP endpoint — the only source that is evidence "
        "rather than a claim",
    )
    p.add_argument("--spec", default=TARGET_SPEC, help="the spec being migrated to")
    p.add_argument(
        "--deprecated-on",
        default=DEPRECATION_ANNOUNCED,
        help="when the deprecation window opened (default %(default)s)",
    )
    p.add_argument(
        "--window-months",
        type=int,
        default=DEPRECATION_WINDOW_MONTHS,
        help="length of the deprecation window (default %(default)s)",
    )
    p.add_argument(
        "--now",
        default="",
        metavar="YYYY-MM-DD",
        help="pin today's date. The countdown is time-dependent, and a report "
        "that cannot be reproduced does not satisfy the provenance rule",
    )
    p.add_argument(
        "--spec-verified",
        action="store_true",
        help="the spec rules have been checked against the document. Changes the "
        "report's wording only — never a verdict",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--report", default="", help="also write the JSON report here")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()

    try:
        today = date.fromisoformat(args.now) if args.now else datetime.now(UTC).date()
        until = deadline(args.deprecated_on, args.window_months)
    except ValueError as exc:
        print(f"spec: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    # Captured before the first file is read and re-read after the last one.
    prov = probe_provenance.capture(target, decisive=not args.url)
    report = run(
        target,
        installed=args.installed,
        portfolio=Path(args.portfolio).resolve() if args.portfolio else None,
        url=args.url,
        spec=args.spec,
        today=today,
        until=until,
        timeout=args.timeout,
        spec_verified=args.spec_verified,
    )
    report.provenance = prov.recheck()

    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report))
    if args.report:
        try:
            Path(args.report).write_text(
                json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"spec: could not write {args.report}: {exc}", file=sys.stderr)

    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
