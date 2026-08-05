#!/usr/bin/env python3
"""Spec probe — which MCP protocol version does this server actually speak?

THE OCCASION
------------
MCP spec ``2026-07-28`` removes the handshake. ``initialize``/``initialized``
and ``Mcp-Session-Id`` are gone; every request carries its protocol version,
client info and capabilities in ``params._meta`` under
``io.modelcontextprotocol/*`` keys, and servers MUST implement ``server/discover``.

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
``LEGACY_TRANSPORT`` the wire is demonstrably on a pre-2026-07-28 form — a
                     reachable ``/sse`` endpoint, an issued ``Mcp-Session-Id``,
                     or a correctly formed stateless call the server refuses.
                     Each signal carries ITS OWN footing (see ``DEPRECATIONS``)
``UNVERIFIED``       a source could not be read. NEVER rendered as "in sync"
``SPEC_UNDECLARED``  the source declares no protocol version at all

The last one is a NOTE, not a finding, and that is deliberate. Under the current
SDKs the protocol version belongs to the SDK, not to the server: 39 of the 42
servers in this portfolio declare nothing, and they are right not to. A
predicate that turned red on all 39 would be switched off within a day, and a
switched-off check catches nothing. What ``SPEC_UNDECLARED`` buys is that the
report says *why* it has no code-level value, instead of leaving a blank that
reads like agreement.

WHAT THE FIRST VERSION GOT WRONG
--------------------------------
This probe was first written against a written SUMMARY of the spec rather than
the document. All three of its stated assumptions turned out to be wrong, and
two of them would have produced exactly the false finding the probe exists to
prevent. Reading the spec (``SPEC_SOURCE``, on ``SPEC_VERIFIED_ON``) corrected:

1. **``_meta`` was in the wrong place, with the wrong keys.** It belongs in
   ``params``, not at the JSON-RPC message root, and its keys are namespaced:
   ``io.modelcontextprotocol/protocolVersion`` / ``clientInfo`` /
   ``clientCapabilities``. Since the ``MCP-Protocol-Version`` header MUST match
   the ``_meta`` value and a mismatch is a mandatory ``400`` with
   ``-32020 HeaderMismatch``, a COMPLIANT server would have rejected this
   probe's stateless call — and the probe would have called that
   ``LEGACY_TRANSPORT``.

2. **``Mcp-Name`` was sent on every call, empty.** It mirrors ``params.name`` or
   ``params.uri`` and is required only for ``tools/call``, ``resources/read``
   and ``prompts/get``. An empty header with no body field to match is a
   ``HeaderMismatch`` by the same rule. Same false finding, second route.

3. **The deprecation clock is not one clock.** The twelve-month window governs
   Roots, Sampling, Logging and DCR (deprecated in ``2026-07-28``, eligible
   ``2027-07-28``). The HTTP+SSE transport was deprecated in ``2025-03-26`` and
   its earliest removal is "three months after SEP-2596 reaches Final", a date
   the registry does not give — so it is NOT COMPUTABLE, and the ``2027-07-28``
   this probe used to print for a ``/sse`` endpoint was an invented number.
   ``Mcp-Session-Id`` is not deprecated at all: it was REMOVED outright, with no
   window. And "earliest removal" is ELIGIBILITY, never a deadline.

The lesson is the module's own: a probe that reads a summary and reports a
measurement has laundered an assumption into evidence. Every rule applied here
now names the page it came from.

Both header variants are still sent, now that the rule is confirmed rather than
assumed — the difference between a strict server and a lax one is itself worth
measuring, and no single request can make it.

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

TARGET_SPEC = "2026-07-28"

# Read from the specification document on 2026-08-05, not from a summary. The
# first version of this probe was written against a migration brief and got
# three of its three assumptions wrong; every rule below now names the page it
# came from so the next reader can re-check it instead of trusting this file.
SPEC_SOURCE = "https://modelcontextprotocol.io/specification/2026-07-28"
SPEC_VERIFIED_ON = "2026-08-05"

# THE DEPRECATION CLOCK IS NOT ONE CLOCK, which is the correction that matters
# most here. The policy (/community/feature-lifecycle) sets a TWELVE-MONTH
# MINIMUM measured from the release of the revision that first marks a feature
# Deprecated — but the registry (/specification/2026-07-28/deprecated) shows the
# features this probe cares about are on three different footings:
#
#   * Roots, Sampling, Logging, DCR — Deprecated in 2026-07-28, earliest removal
#     "first revision released on or after 2027-07-28". The 12-month clock.
#   * HTTP+SSE (the /sse transport) — Deprecated since 2025-03-26, earliest
#     removal "three months after SEP-2596 reaches Final". A DIFFERENT clock, and
#     the registry does not state when SEP-2596 went Final, so the date is NOT
#     COMPUTABLE from the published spec. An earlier version of this probe
#     printed 2027-07-28 for a /sse endpoint. That number was invented.
#   * Mcp-Session-Id — not deprecated at all. REMOVED outright in 2026-07-28
#     (changelog, Major change 1). There is no window and no countdown; a server
#     issuing one is speaking an older revision.
#
# And "earliest removal" is ELIGIBILITY, not a deadline: "Features may remain
# Deprecated, without removal, for much longer than the minimum deprecation
# window." So this probe reports a date where the spec gives one, says so where
# it does not, and never calls either a deadline.
DEPRECATION_WINDOW_MONTHS = 12
TWELVE_MONTH_FEATURES_DEPRECATED_IN = "2026-07-28"


@dataclass(frozen=True)
class Deprecation:
    """One deprecated thing, with the footing the registry actually gives it."""

    what: str
    deprecated_in: str
    # None when the spec states no computable date. NOT a stand-in for "none".
    earliest_removal: date | None
    basis: str

    def phrase(self, today: date) -> str:
        if self.earliest_removal is None:
            return (
                f"{self.what} — Deprecated since {self.deprecated_in}; "
                f"earliest removal is {self.basis}, which the registry does not "
                "date, so no countdown can be given. Not a deadline either way"
            )
        left = (self.earliest_removal - today).days
        when = self.earliest_removal.isoformat()
        if left < 0:
            return (
                f"{self.what} — eligible for removal since {when} "
                f"({abs(left)} day(s) ago). Eligibility, not a deadline: a "
                "Deprecated feature may remain for much longer"
            )
        return (
            f"{self.what} — Deprecated in {self.deprecated_in}; eligible for "
            f"removal in the first revision released on or after {when} "
            f"({left} day(s) away). Eligibility, not a deadline"
        )


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


def deadline(
    announced: str = TWELVE_MONTH_FEATURES_DEPRECATED_IN, months: int = 12
) -> date:
    """The day a 12-month-window feature becomes ELIGIBLE for removal.

    Not "the day it stops being allowed" — that is what an earlier version of
    this docstring said, and it is wrong twice over. The policy defines a
    minimum window after which the feature "becomes eligible for removal in the
    first specification revision released as Current on or after the window
    elapses", and adds that "Features may remain Deprecated, without removal,
    for much longer than the minimum deprecation window."

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


# The registry, transcribed. Each entry keeps the footing the spec gives it,
# because they are not the same and reporting them as one produced a number that
# appears nowhere in the specification.
DEPRECATIONS: dict[str, Deprecation] = {
    "http_sse": Deprecation(
        what="the HTTP+SSE transport (2024-11-05)",
        deprecated_in="2025-03-26",
        # NOT the 12-month clock, and NOT computable: the registry gives
        # "Three months after SEP-2596 reaches Final" and states no Final date.
        earliest_removal=None,
        basis="three months after SEP-2596 reaches Final",
    ),
    "roots_sampling_logging_dcr": Deprecation(
        what="Roots, Sampling, Logging and Dynamic Client Registration",
        deprecated_in="2026-07-28",
        earliest_removal=deadline(TWELVE_MONTH_FEATURES_DEPRECATED_IN, 12),
        basis="the twelve-month minimum window",
    ),
}


def countdown(today: date, until: date) -> str:
    """The sentence a 12-month-window finding carries.

    Days and not a boolean, because "deprecated" is not actionable and "357
    days" is — but "eligible" and not "allowed until", because that is what the
    policy says. Past the date it says so plainly rather than printing a
    negative number, which reads as a bug and gets ignored.
    """
    left = days_left(today, until)
    if left < 0:
        return (
            f"eligible for removal since {until.isoformat()} "
            f"({abs(left)} day(s) ago) — eligibility, not a deadline"
        )
    return (
        f"{left} day(s) until it is eligible for removal ({until.isoformat()}) — "
        "eligibility, not a deadline"
    )


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


# Verbatim from the transport page's worked examples. The keys are NAMESPACED
# and `_meta` sits inside `params`, not at the JSON-RPC message root. The first
# version of this probe got both wrong, and the consequence was not cosmetic: the
# `MCP-Protocol-Version` header MUST match `_meta`'s
# `io.modelcontextprotocol/protocolVersion`, and a mismatch is a mandatory
# `400 Bad Request` with `-32020 HeaderMismatch`. A compliant, fully migrated
# server would therefore have rejected this probe's stateless call — which the
# probe read as "the server still requires initialize" and reported as
# LEGACY_TRANSPORT. A false finding against exactly the servers that had done
# the work, which is the failure this whole probe family exists to prevent.
_META_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"

_CLIENT_INFO = {"name": "mcp-continuous-auditor spec-probe", "version": "1"}

# `Mcp-Name` mirrors `params.name` or `params.uri` and is REQUIRED only for these
# three methods (transport page, "Standard Request Headers"). Sending it on
# `tools/list` — as the first version did, with an empty value — gives a
# validating server a header with no body field to match, which is a
# `HeaderMismatch` rejection by the same rule. An empty header is not a neutral
# one.
_NAMED_METHODS = ("tools/call", "resources/read", "prompts/get")


def _stateless_params(
    spec: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`params` carrying the per-request metadata that replaced the handshake."""
    return {
        **(params or {}),
        "_meta": {
            _META_VERSION: spec,
            _META_CLIENT_INFO: _CLIENT_INFO,
            _META_CLIENT_CAPS: {},
        },
    }


def _headers(method: str, spec: str, name: str = "") -> dict[str, str]:
    """The headers the transport requires for one request, and no others."""
    out = {"MCP-Protocol-Version": spec, "Mcp-Method": method}
    if method in _NAMED_METHODS and name:
        out["Mcp-Name"] = name
    return out


def _stateless_call(
    method: str, ident: int, spec: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _rpc(method, ident, _stateless_params(spec, params))


def _result_type(payload: Any) -> str:
    """``result.resultType`` — required on every result from 2026-07-28 on.

    Changelog, Major change 8: results carry ``"complete"`` or
    ``"input_required"``, and "Clients MUST treat results from earlier-protocol
    servers that omit the field as `complete`". That makes its PRESENCE a clean
    positive signal — an older server cannot accidentally produce it — while its
    absence proves nothing on its own.
    """
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    return str(result.get("resultType") or "") if isinstance(result, dict) else ""


@dataclass
class WireResult:
    url: str
    reachable: bool = False
    negotiated: str = ""
    advertised: list[str] = field(default_factory=list)
    stateless_ok: bool | None = None
    stateless_with_headers: int = 0
    stateless_without_headers: int = 0
    handshake_ok: bool | None = None
    session_id: str = ""
    sse_endpoint: str = ""
    discover_ok: bool | None = None
    result_type: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "reachable": self.reachable,
            "negotiated": self.negotiated,
            "advertised_versions": list(self.advertised),
            "stateless_ok": self.stateless_ok,
            "stateless_status_with_required_headers": self.stateless_with_headers,
            "stateless_status_without_required_headers": self.stateless_without_headers,
            "handshake_ok": self.handshake_ok,
            "session_id_issued": bool(self.session_id),
            "sse_endpoint": self.sse_endpoint,
            "discover_ok": self.discover_ok,
            "result_type": self.result_type,
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

    Both header variants are still sent, now that the rule is confirmed rather
    than assumed: `Mcp-Method` and `Mcp-Name` are REQUIRED for compliance
    (transport page, "Standard Request Headers"), and a server that validates
    them answers `400` with `-32020 HeaderMismatch`. Sending the call each way
    keeps the difference measured, which is what tells a strict server apart
    from a lax one — a distinction no single request can make.
    """
    out = WireResult(url=url)

    # 1) a real call with NO handshake in front of it, correctly formed.
    #    `Mcp-Name` is deliberately ABSENT: it mirrors `params.name`/`params.uri`
    #    and is required only for tools/call, resources/read and prompts/get. An
    #    empty one here is a header with no body field to match, which a
    #    validating server must reject.
    listed = request(
        url,
        payload=_stateless_call("tools/list", 1, spec),
        headers=_headers("tools/list", spec),
        timeout=timeout,
    )
    out.reachable = listed.reached
    out.stateless_with_headers = listed.status
    if listed.error:
        out.notes.append(f"the endpoint could not be reached: {listed.error}")
        return out
    out.stateless_ok = listed.rpc_ok
    out.result_type = _result_type(listed.payload)

    # 2) the same call WITHOUT the required headers.
    bare = request(
        url,
        payload=_stateless_call("tools/list", 2, spec),
        headers={"MCP-Protocol-Version": spec},
        timeout=timeout,
    )
    out.stateless_without_headers = bare.status
    if listed.rpc_ok and not bare.rpc_ok:
        out.notes.append(
            "the stateless call succeeds WITH `Mcp-Method` and fails without it "
            f"(HTTP {bare.status}) — the server enforces the required request "
            "headers, as the transport requires"
        )
    elif listed.rpc_ok and bare.rpc_ok:
        out.notes.append(
            "the stateless call succeeds WITHOUT `Mcp-Method` too — the headers "
            "are REQUIRED for compliance, so this server does not enforce them. "
            "Not a protocol-version finding; worth knowing before an "
            "intermediary starts routing on them"
        )
    elif not listed.rpc_ok and bare.rpc_ok:
        out.notes.append(
            "the stateless call succeeds WITHOUT the required headers and fails "
            f"with them (HTTP {listed.status}) — the server rejects headers it "
            "does not know, which places it before 2026-07-28"
        )
        out.stateless_ok = True

    # 3) the handshake. Removed in 2026-07-28; present or absent, both are data.
    #    A modern server answers an unimplemented method with 404 + -32601.
    hand = request(
        url,
        payload=_rpc(
            "initialize",
            3,
            {
                "protocolVersion": spec,
                "capabilities": {},
                "clientInfo": dict(_CLIENT_INFO),
            },
        ),
        headers=_headers("initialize", spec),
        timeout=timeout,
    )
    out.handshake_ok = hand.rpc_ok
    if hand.rpc_ok and isinstance(hand.payload, dict):
        result = hand.payload.get("result")
        if isinstance(result, dict):
            out.negotiated = str(result.get("protocolVersion") or "")
    # Removed outright in 2026-07-28, not deprecated: a server that mints one is
    # speaking an older revision. There is no window and no countdown for it.
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

    # 5) server/discover. NOT optional for the server: "servers MUST implement
    #    this RPC to advertise their supported protocol versions, capabilities,
    #    and identity" (changelog, Major change 3). It is the CLIENT that MAY
    #    call it. An earlier comment here called it optional in the spec, which
    #    inverted the obligation.
    #
    #    Its answer is also the cleanest positive evidence available: nothing
    #    else distinguishes a migrated server from one that merely tolerates a
    #    handshake-free call.
    disc = request(
        url,
        payload=_stateless_call("server/discover", 4, spec),
        headers=_headers("server/discover", spec),
        timeout=timeout,
    )
    out.discover_ok = disc.rpc_ok if disc.reached else None
    if disc.rpc_ok and isinstance(disc.payload, dict):
        result = disc.payload.get("result")
        if isinstance(result, dict):
            versions = result.get("supportedProtocolVersions") or result.get(
                "protocolVersions"
            )
            if isinstance(versions, list) and versions:
                out.advertised = [str(v) for v in versions]
                # What the server SAYS it speaks, which is a stronger statement
                # than what it tolerated above.
                if not out.negotiated:
                    out.negotiated = str(versions[0])
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
            "spec_source": SPEC_SOURCE,
            "spec_rules_verified_on": SPEC_VERIFIED_ON,
            # The 12-month clock, and ONLY the features it actually governs
            # (Roots, Sampling, Logging, DCR). The /sse transport is on a
            # different footing and carries no computable date — see
            # `DEPRECATIONS`. A single `deprecation_deadline` field applied to
            # everything was how the invented number got into the report.
            "twelve_month_eligibility": self.until.isoformat(),
            "days_to_eligibility": days_left(self.today, self.until),
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

    # --- LEGACY_TRANSPORT: the wire is demonstrably on a pre-2026-07-28 form --
    #
    # Each signal carries ITS OWN footing. Lumping them under one countdown was
    # the bug: `/sse` and `Mcp-Session-Id` are not on the same clock, and one of
    # them is on no clock at all.
    wire = report.wire
    if wire is not None and wire.reachable:
        legacy: list[str] = []
        if wire.sse_endpoint:
            legacy.append(
                f"a legacy HTTP+SSE endpoint answers at {wire.sse_endpoint} — "
                + DEPRECATIONS["http_sse"].phrase(report.today)
            )
        if wire.session_id:
            legacy.append(
                f"the server issues an `Mcp-Session-Id` ({wire.session_id[:12]}…). "
                "That header was REMOVED in 2026-07-28, not deprecated — there is "
                "no window and no countdown; the server is speaking an earlier "
                "revision"
            )
        if wire.stateless_ok is False:
            legacy.append(
                "a correctly formed call without a preceding handshake was "
                f"refused (HTTP {wire.stateless_with_headers}) — the server still "
                "requires `initialize`, which 2026-07-28 removed"
            )
        if legacy:
            report.findings.append(
                Finding(
                    LEGACY_TRANSPORT,
                    "medium",
                    "; ".join(legacy)
                    + ". A RECOMMENDATION, not a gate: every form named here is "
                    "still valid, and where a removal date exists it marks "
                    "ELIGIBILITY rather than a deadline — the policy is explicit "
                    "that a Deprecated feature may remain for much longer",
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
) -> Report:
    report = Report(
        target=str(target),
        spec=spec,
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

    lines.append(f"  target spec {report.spec} · rules read from {SPEC_SOURCE}")
    lines.append(f"  spec rules verified on {SPEC_VERIFIED_ON}")
    lines.append(
        "  deprecation clocks (they are NOT one clock):\n"
        + "\n".join(f"    {d.phrase(report.today)}" for d in DEPRECATIONS.values())
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
            f"discover={_tri(wire.discover_ok)} "
            f"resultType={wire.result_type or '(absent)'}"
        )
        if wire.advertised:
            lines.append(f"    advertised versions: {', '.join(wire.advertised)}")
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
        default=TWELVE_MONTH_FEATURES_DEPRECATED_IN,
        help="the revision that opened the twelve-month window for Roots, "
        "Sampling, Logging and DCR (default %(default)s). It does NOT govern "
        "the HTTP+SSE transport, which is on its own footing",
    )
    p.add_argument(
        "--window-months",
        type=int,
        default=DEPRECATION_WINDOW_MONTHS,
        help="the policy's minimum deprecation window (default %(default)s)",
    )
    p.add_argument(
        "--now",
        default="",
        metavar="YYYY-MM-DD",
        help="pin today's date. Any day count is time-dependent, and a report "
        "that cannot be reproduced does not satisfy the provenance rule",
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
