#!/usr/bin/env python3
"""Shipped probe — install what users install, and make it prove it runs.

WHY THIS EXISTS
---------------
Green CI is not shipped software. The concrete case, from this portfolio:
``main`` stood at 0.6.0, the GitHub release was never cut, so the publish
workflow never fired, and PyPI served 0.5.0 for an entire release cycle — with
three tools that were demonstrably broken in it. Every nightly run was green.
The auditor was reading the source and never once the thing users install.

Nothing else in this repository closes that. ``release_gap.py`` compares the
repository against the index *metadata* — it asks whether a version exists.
``published_probe.py`` installs the artifact and reads its User-Agent out of the
code. Neither one runs the installed server. This does:

  1. install the distribution from PyPI into a FRESH venv (never the checkout);
  2. hold the installed version against the repository's version AND the last
     git tag, reporting every divergence;
  3. start the installed entrypoint and speak a real ``initialize`` plus a real
     ``tools/call`` to it;
  4. keep "not on PyPI at all" apart from "on PyPI but stale". Both are
     findings. They are not the same finding and they do not have the same fix.

WHY (4) IS ITS OWN DISTINCTION
------------------------------
"Never published" means the release process has never run for this package —
the fix is to publish it. "Published but behind" means the process exists and
did not fire this time — the fix is to look at the workflow run, which usually
failed on an approval or an OIDC trust that nobody was watching. Reporting both
as "PyPI is out of date" sends the maintainer to the wrong place.

Because that distinction carries an accusation, the check behind it reads the
SIMPLE API at ``--index-url`` — the exact surface pip will resolve against, and
therefore the one this probe's entire claim is about. It used to ask pypi.org's
JSON API and then install from wherever the target publishes: two caches of one
index in the best case, and two different hosts for anyone on a private index.
Reading an arbitrary index means reading PEP 503 HTML, since the JSON flavour is
optional and HTML is the only format required; both are parsed into one shape in
``release_gap._get``. ``lookup_index`` below documents why the JSON fallback
exists only for PyPI and why a 404 is corroborated rather than believed.

THE STDIN TRAP, AGAIN — AND WORSE HERE
--------------------------------------
``transport_boot_probe.py`` documents it: close stdin after writing and the
server shuts down before network-bound work finishes, and you record a failure
that does not exist. This probe makes a real TOOL CALL, which is the most
network-bound thing a server does, so the trap has more room to bite. stdin is
held open until every answer is in, and ``_close_stdin_early`` exists purely so
the test suite can demonstrate that closing it fabricates a failure.

WHAT A FAILING TOOL CALL DOES AND DOES NOT PROVE
------------------------------------------------
A tool that answers ``isError`` is reported as a finding — that is the shape the
incident took. But this probe runs inside a Worker with a default-deny egress
allowlist, and a tool whose upstream origin is not on that list fails in exactly
the same way. The finding says so in its own text rather than letting the reader
assume the artifact is broken: check the allowlist before filing a bug against
the target. (``deploy/microvm/forward-proxy/README.md``.)

READ-ONLY, like every other path here: the target checkout is only read. The
venv is built in a temp dir and removed.

EXIT CODES
  0    the published artifact matches the repository and ran
  2    FINDING — absent from the index, stale, version divergence, or the
       installed server did not answer
  127  the HARNESS could not run (no network to the index, venv creation failed).
       An unreachable index is NOT reported as "in sync": a comparison that did
       not happen is never a pass.

Usage:
  python scripts/shipped_probe.py --dist zurich-opendata-mcp --target ../zurich-opendata-mcp
  python scripts/shipped_probe.py --dist foo-mcp --target . --tool health --format json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_gap as rg
import transport_boot_probe as tbp

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_CANNOT_RUN = 127

DEFAULT_INDEX = "https://pypi.org/simple"
DEFAULT_INSTALL_TIMEOUT = 600
DEFAULT_RUN_TIMEOUT = 120

# Publication states — (4) in the docstring. Kept apart because they send the
# maintainer to different places.
NOT_PUBLISHED = "not-published"
PUBLISHED = "published"
INDEX_UNREACHABLE = "index-unreachable"
INSTALL_FAILED = "install-failed"


@dataclass
class Finding:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass
class Versions:
    installed: str = ""
    repo: str = ""
    tag: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"installed": self.installed, "repo": self.repo, "tag": self.tag}


@dataclass
class ToolCall:
    ran: bool = False
    name: str = ""
    status: str = "skipped"     # ok | error | empty | no-answer | skipped
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "name": self.name, "status": self.status,
                "detail": self.detail}


@dataclass
class Report:
    dist: str
    publication: str = PUBLISHED
    versions: Versions = field(default_factory=Versions)
    findings: list[Finding] = field(default_factory=list)
    entrypoint: str = ""
    tools: int | None = None
    tool_call: ToolCall = field(default_factory=ToolCall)
    harness_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1, "dist": self.dist, "publication": self.publication,
            "versions": self.versions.as_dict(), "entrypoint": self.entrypoint,
            "tools": self.tools, "tool_call": self.tool_call.as_dict(),
            "harness_error": self.harness_error,
            "findings": [f.as_dict() for f in self.findings],
            "exit_code": self.exit_code(),
        }

    def exit_code(self) -> int:
        if self.harness_error:
            return EXIT_CANNOT_RUN
        return EXIT_FINDINGS if self.findings else EXIT_GREEN


# --------------------------------------------------------------------------
# PURE LOGIC — no network, no subprocess. This is the part the tests own.
# --------------------------------------------------------------------------

def compare_versions(v: Versions) -> list[Finding]:
    """Every divergence between what is installed, what the repo says, and what
    the last tag says.

    Ordering uses release_gap's narrow release-segment comparison; anything it
    cannot order is reported as a plain difference rather than silently ranked
    the wrong way round. The direction matters: PyPI *behind* the repo is the
    incident, PyPI *ahead* of it means something was published from a tree this
    checkout does not have, which is a different and rarer problem.
    """
    out: list[Finding] = []
    inst_key = rg.release_key(v.installed) if v.installed else None
    repo_key = rg.release_key(v.repo) if v.repo else None

    if v.installed and v.repo and v.installed != v.repo:
        if inst_key and repo_key and inst_key < repo_key:
            out.append(Finding(
                "STALE_ON_INDEX",
                f"PyPI serves {v.installed}, the repository is at {v.repo} — users "
                f"install {v.installed}. This is the shape of the incident: the "
                "release was never cut, so the publish workflow never ran"))
        elif inst_key and repo_key and inst_key > repo_key:
            out.append(Finding(
                "INDEX_AHEAD",
                f"PyPI serves {v.installed}, ahead of the repository's {v.repo} — "
                "something was published from a tree this checkout does not have, "
                "or the checkout is not the branch that releases"))
        else:
            out.append(Finding(
                "VERSION_DIFFERS",
                f"PyPI serves {v.installed}, the repository says {v.repo}; neither "
                "could be ordered against the other, so the direction is unknown"))

    if v.installed and v.tag:
        tag_version = rg.normalise_tag(v.tag)
        tag_key = rg.release_key(tag_version) if tag_version else None
        if tag_version and tag_version != v.installed:
            if tag_key and inst_key and tag_key > inst_key:
                out.append(Finding(
                    "TAG_NOT_ON_INDEX",
                    f"the last tag is {v.tag} but PyPI serves {v.installed} — a tag "
                    "exists that the index does not. Somebody cut the release and "
                    "it did not land, so the publish WORKFLOW RUN is where to look, "
                    "not the release process"))
            else:
                out.append(Finding(
                    "TAG_DIFFERS",
                    f"the last tag is {v.tag} while PyPI serves {v.installed} — the "
                    "index is not behind the tag, so the two were cut from "
                    "different places"))
    return out


# Failures whose text says "the sandbox stopped me", not "the artifact is broken".
# This probe runs behind a default-deny egress allowlist, and a tool whose
# upstream origin is not on that list fails in the same place a genuinely broken
# tool does. Reporting those as findings would make the gate fire on every target
# whose upstream nobody has allowlisted yet — and a gate that cries wolf gets
# muted, which is the same reasoning that keeps recall floors at half the
# observed count. So they are recorded as UNATTRIBUTABLE and do not raise a
# finding. Note what is deliberately NOT in this list: an empty content list.
# That is the incident's own shape, it looks nothing like a blocked socket, and
# it stays a finding.
_EGRESS_MARKERS = (
    "connection refused", "connection reset", "connection aborted",
    "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "getaddrinfo", "dns", "proxy",
    "timed out", "timeout", "network is unreachable", "no route to host",
    "ssl", "certificate verify failed", "403 forbidden", "tunnel connection failed",
)


def looks_like_egress(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _EGRESS_MARKERS)


def classify_tool_result(payload: Any) -> ToolCall:
    """Turn a ``tools/call`` reply into a verdict.

    Deliberately more than two-way. "The transport answered", "the tool worked"
    and "the sandbox let it reach its upstream" are three different claims, and
    the incident was tools that answered perfectly well with nothing in them.
    """
    if not isinstance(payload, dict):
        return ToolCall(ran=True, status="no-answer",
                        detail="the server returned no JSON-RPC object")
    if "error" in payload:
        err = payload.get("error") or {}
        message = str(err.get("message"))
        if looks_like_egress(message):
            return ToolCall(ran=True, status="blocked",
                            detail=f"unattributable — the error reads like this "
                                   f"Worker's egress allowlist, not the artifact: "
                                   f"{message[:160]}")
        return ToolCall(ran=True, status="error",
                        detail=f"JSON-RPC error {err.get('code')}: {message[:160]}")
    result = payload.get("result")
    if not isinstance(result, dict):
        return ToolCall(ran=True, status="no-answer",
                        detail="the reply carried no result object")
    if result.get("isError"):
        text = " ".join(
            str(c.get("text", "")) for c in (result.get("content") or [])
            if isinstance(c, dict))
        if looks_like_egress(text):
            return ToolCall(ran=True, status="blocked",
                            detail=f"unattributable — the tool's own error reads "
                                   f"like this Worker's egress allowlist rather "
                                   f"than a defect: {text[:160]}")
        return ToolCall(
            ran=True, status="error",
            detail=f"the tool reported isError: {text[:160]}. If the target's "
                   "upstream origin is not on this Worker's allowlist, add it "
                   "before filing this against the target "
                   "(deploy/microvm/forward-proxy/README.md)")
    content = result.get("content")
    if isinstance(content, list) and not content:
        return ToolCall(ran=True, status="empty",
                        detail="the tool returned an empty content list — it "
                               "answered, and it answered with nothing")
    return ToolCall(ran=True, status="ok", detail="returned content")


def pick_tool(tools: list[dict[str, Any]], preferred: str = "") -> tuple[str, str]:
    """Which tool to call, and why not, if none.

    A tool is only callable blind when it needs no arguments — inventing values
    for a required parameter would test our guess, not the artifact.
    """
    by_name = {str(t.get("name")): t for t in tools if isinstance(t, dict)}
    if preferred:
        if preferred in by_name:
            return preferred, ""
        return "", f"the requested tool {preferred!r} is not in tools/list"
    for name, spec in by_name.items():
        schema = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
        if not (schema.get("required") or []):
            return name, ""
    if by_name:
        return "", ("every tool requires arguments; pass --tool/--tool-args to "
                    "exercise one rather than have the probe guess values")
    return "", "the server listed no tools"


# One definition, used by both probes: "does this index have a JSON API to
# corroborate with" is the same question here as in release_gap, and two copies
# of a host check are two chances to answer it differently.
is_pypi = rg.is_pypi


def lookup_index(
    dist: str, timeout: float, index_url: str = DEFAULT_INDEX
) -> tuple[str | None, str, str]:
    """Does this distribution exist on the index, and at which version?

    Asked against the SAME index the install resolves against — ``--index-url``,
    whatever it points at. This probe's whole claim is about what ``pip`` does,
    and it used to ask pypi.org's JSON API and then install from wherever the
    target actually publishes. For a private index those are different hosts, so
    the check could answer confidently about a package it was never looking at.

    Reading an arbitrary index means reading PEP 503 HTML, which is the only
    format such an index is required to serve — ``release_gap._get`` parses both
    flavours into one shape, so that support lives in one place rather than two.

    The JSON API stays as a fallback ONLY for PyPI, because only PyPI has one. On
    any other index a failed Simple read is the end of the road, and it is
    reported rather than papered over: 127, not a guess.

    404 is corroborated rather than believed — but again only on PyPI, where a
    second opinion exists. "Never published" is the one verdict here that
    accuses a maintainer of having no release process at all, and a first-ever
    publish is exactly when two caches are most likely to be seconds apart. If
    either index has heard of the package this returns "published" and lets the
    INSTALL settle it: unlike ``release_gap``, this probe has a tiebreaker and
    does not have to leave the disagreement unresolved.
    """
    view = rg.fetch_simple(dist, timeout, index_url)
    if view.readable:
        return (view.latest_installable or view.latest), "ok", ""

    if not is_pypi(index_url):
        if view.status == "not_published":
            return None, "not_published", f"{dist} is not on {index_url} (HTTP 404)"
        return None, "unreachable", (
            f"{view.detail} — and {index_url} is not PyPI, so there is no JSON API "
            "to corroborate it with")

    fallback_version, fallback_status, fallback_detail = rg.fetch_pypi_version(dist, timeout)
    if view.status == "not_published":
        if fallback_status == "ok":
            return fallback_version, "ok", ""
        return None, "not_published", f"{dist} is not on PyPI (HTTP 404)"
    return fallback_version, fallback_status, fallback_detail


def build_findings(report: Report) -> list[Finding]:
    """The whole verdict, from an already-populated report. Pure."""
    out: list[Finding] = []
    if report.publication == NOT_PUBLISHED:
        out.append(Finding(
            "NOT_ON_INDEX",
            f"{report.dist} does not exist on the index at all. Distinct from a "
            "stale release: there is no publish process to repair here, there is "
            "one to create — `pip install` has never worked for this package"))
        return out
    if report.publication == INSTALL_FAILED:
        out.append(Finding(
            "INSTALL_FAILED",
            "the distribution exists on the index but could not be installed into "
            "a clean venv — which is what every user's first command does"))
        return out

    out.extend(compare_versions(report.versions))

    if not report.entrypoint:
        out.append(Finding(
            "NO_ENTRYPOINT",
            "the installed distribution declares no console script — nothing to "
            "start, so nobody can run what was published"))
        return out

    if report.tools is None:
        out.append(Finding(
            "DOES_NOT_RUN",
            "the installed entrypoint did not answer initialize + tools/list. The "
            "artifact on the index does not start, whatever the branch does"))
        return out

    tc = report.tool_call
    if tc.status == "error":
        out.append(Finding("TOOL_ERROR", f"{tc.name}: {tc.detail}"))
    elif tc.status == "no-answer":
        out.append(Finding("TOOL_NO_ANSWER", f"{tc.name}: {tc.detail}"))
    elif tc.status == "empty":
        # The incident's own shape: it answered, and it answered with nothing.
        out.append(Finding("TOOL_EMPTY", f"{tc.name}: {tc.detail}"))
    # status == "blocked" raises nothing on purpose — see _EGRESS_MARKERS. It is
    # still in the report, so it is visible rather than swallowed.
    return out


# --------------------------------------------------------------------------
# IMPURE — the network and the subprocess. Injected, so the logic above can be
# tested without either.
# --------------------------------------------------------------------------

@dataclass
class Installed:
    ok: bool
    version: str = ""
    entrypoint: str = ""
    python: str = ""
    detail: str = ""


def install_from_index(dist: str, workdir: Path, index_url: str,
                       timeout: float) -> Installed:
    """A fresh venv and one ``pip install`` from the index.

    ``--no-cache-dir`` is not optional: with a warm wheel cache this measures
    what pip kept on disk last time, which is precisely the stale artifact we
    are trying to catch — the check would then confirm the bug as healthy.
    ``--index-url`` is pinned so a pip.conf mirror cannot quietly answer for
    PyPI either.
    """
    env_dir = workdir / "venv"
    try:
        venv.create(env_dir, with_pip=True, clear=True)
    except Exception as exc:  # noqa: BLE001 - harness failure, reported as such
        return Installed(False, detail=f"venv creation failed: {type(exc).__name__}: {exc}")
    py = env_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")
    try:
        proc = subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "--no-cache-dir",
             "--index-url", index_url, dist],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Installed(False, detail=f"pip install exceeded {timeout:.0f}s")
    except OSError as exc:
        return Installed(False, detail=f"could not run pip: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        return Installed(False, detail=(proc.stderr or proc.stdout or "").strip()[-400:])

    probe_src = (
        "import json,sys\n"
        "from importlib import metadata as m\n"
        "d=sys.argv[1]\n"
        "try:\n"
        "    v=m.version(d)\n"
        "except Exception as e:\n"
        "    print(json.dumps({'error':str(e)})); raise SystemExit(0)\n"
        "eps=[]\n"
        "try:\n"
        "    for ep in m.distribution(d).entry_points:\n"
        "        if ep.group=='console_scripts': eps.append(ep.name)\n"
        "except Exception: pass\n"
        "print(json.dumps({'version':v,'scripts':eps}))\n"
    )
    try:
        meta = subprocess.run([str(py), "-c", probe_src, dist],
                              capture_output=True, text=True, timeout=60, check=False)
        info = json.loads((meta.stdout or "{}").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        return Installed(False, detail=f"installed metadata unreadable: {exc}")
    if info.get("error"):
        return Installed(False, detail=f"installed metadata unreadable: {info['error']}")

    scripts = info.get("scripts") or []
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    entry = ""
    for name in scripts:
        candidate = bindir / name
        if candidate.exists():
            entry = str(candidate)
            break
    return Installed(True, version=str(info.get("version") or ""),
                     entrypoint=entry, python=str(py))


def speak_mcp(argv: list[str], timeout: float, cwd: Path,
              tool: str = "", tool_args: dict[str, Any] | None = None,
              env: dict[str, str] | None = None,
              _close_stdin_early: bool = False) -> dict[str, Any]:
    """initialize -> tools/list -> tools/call, over stdio, against the INSTALLED
    entrypoint.

    stdin stays open until the last answer is read. ``_close_stdin_early``
    exists only so the tests can show that closing it fabricates a failure — a
    tool call is the most network-bound thing a server does, so this is where
    the trap bites hardest.
    """
    run_env = dict(os.environ if env is None else env)
    run_env["PYTHONUNBUFFERED"] = "1"
    out: dict[str, Any] = {"tools": None, "listing": None, "call": None, "error": ""}
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=run_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True)
    except (OSError, ValueError) as exc:
        out["error"] = f"could not start the installed entrypoint: {type(exc).__name__}: {exc}"
        return out

    q: "Queue[str | None]" = Queue()
    tbp._reader_thread(proc.stdout, q)
    err_q: "Queue[str | None]" = Queue()
    tbp._reader_thread(proc.stderr, err_q)
    err_lines: list[str] = []
    deadline = time.monotonic() + timeout

    def send(msg: dict[str, Any]) -> bool:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def await_id(ident: int) -> dict[str, Any] | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = q.get(timeout=min(remaining, 0.5))
            except Empty:
                if proc.poll() is not None:
                    return None
                continue
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == ident:
                return msg

    try:
        if not send(tbp._rpc("initialize", 1, tbp._initialize_params())):
            out["error"] = ("the entrypoint closed stdin before initialize; stderr: "
                            + tbp._tail(tbp._drain(err_q, err_lines)))
            return out
        # THE TRAP. Production callers never set this.
        if _close_stdin_early and proc.stdin is not None:
            proc.stdin.close()

        init = await_id(1)
        if init is None or "error" in init:
            out["error"] = ("initialize did not succeed; stderr: "
                            + tbp._tail(tbp._drain(err_q, err_lines)))
            return out

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        if not send(tbp._rpc("tools/list", 2)):
            out["error"] = "the entrypoint went away before tools/list"
            return out
        listing = await_id(2)
        if listing is None or "error" in listing:
            out["error"] = ("tools/list did not succeed; stderr: "
                            + tbp._tail(tbp._drain(err_q, err_lines)))
            return out
        tools = ((listing.get("result") or {}).get("tools") or []) \
            if isinstance(listing.get("result"), dict) else []
        out["tools"] = tools
        out["listing"] = listing

        if tool:
            send(tbp._rpc("tools/call", 3,
                          {"name": tool, "arguments": tool_args or {}}))
            out["call"] = await_id(3)
        return out
    finally:
        tbp._terminate(proc)
        tbp._close_streams(proc)


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------

def probe(dist: str, target: Path, *, tool: str = "",
          tool_args: dict[str, Any] | None = None,
          index_url: str = DEFAULT_INDEX,
          install_timeout: float = DEFAULT_INSTALL_TIMEOUT,
          run_timeout: float = DEFAULT_RUN_TIMEOUT,
          installer: Callable[..., Installed] | None = None,
          speaker: Callable[..., dict[str, Any]] | None = None,
          index_lookup: Callable[[str, float], tuple[str | None, str, str]] | None = None,
          ) -> Report:
    """Everything, wired. The three injectable seams are the three impure parts:
    the index lookup, the install, and the subprocess."""
    report = Report(dist=dist)
    # The seam stays two-argument so injected fakes stay trivial; the index URL
    # is closed over rather than threaded through it.
    index_lookup = index_lookup or (lambda d, t: lookup_index(d, t, index_url))
    installer = installer or install_from_index
    speaker = speaker or speak_mcp

    # The repository side, read-only. `read_project` returns the [project] TABLE
    # (not the whole document) and raises when there is no pyproject at all — a
    # target we cannot read a version from still deserves the index comparison,
    # so an absent version is left empty rather than made fatal.
    try:
        project = rg.read_project(target)
    except OSError:
        project = {}
    report.versions.repo = str(project.get("version") or "")
    # `release_tags` sorts NEWEST FIRST (`--sort=-v:refname`), so the latest tag
    # is [0]. Taking [-1] would compare the index against the OLDEST release the
    # repository ever cut, which is always behind and always "a finding".
    tags = rg.release_tags(target)
    report.versions.tag = tags[0] if tags else ""

    # Does it exist on the index at all? Asked BEFORE installing, so "never
    # published" is answered by the index rather than inferred from pip's stderr.
    index_version, status, detail = index_lookup(dist, 20.0)
    if status == "unreachable":
        # A comparison that did not happen is not a pass. 127, not 0 and not 2.
        report.harness_error = (
            f"the index could not be reached ({detail}) — the published artifact "
            "was NOT compared. This is not 'in sync'")
        return report
    if status == "not_published":
        report.publication = NOT_PUBLISHED
        report.findings = build_findings(report)
        return report

    with tempfile.TemporaryDirectory(prefix="shipped-probe-") as tmp:
        got = installer(dist, Path(tmp), index_url, install_timeout)
        if not got.ok:
            report.publication = INSTALL_FAILED
            report.findings = build_findings(report)
            report.findings.append(Finding("INSTALL_DETAIL", got.detail))
            return report

        report.versions.installed = got.version or (index_version or "")
        report.entrypoint = got.entrypoint
        if not got.entrypoint:
            report.findings = build_findings(report)
            return report

        spoke = speaker([got.entrypoint], run_timeout, Path(tmp), tool, tool_args)
        if spoke.get("error") or spoke.get("tools") is None:
            report.findings = build_findings(report)
            report.findings.append(
                Finding("RUN_DETAIL", str(spoke.get("error") or "no tools/list answer")))
            return report

        tools = spoke["tools"] or []
        report.tools = len(tools)
        chosen, why = pick_tool(tools, tool)
        if not chosen:
            report.tool_call = ToolCall(ran=False, status="skipped", detail=why)
        elif spoke.get("call") is None and chosen != tool:
            # tools/list came back, but the caller did not name a tool, so the
            # call has to be made now that we know which one is argument-free.
            again = speaker([got.entrypoint], run_timeout, Path(tmp), chosen, tool_args)
            report.tool_call = classify_tool_result(again.get("call"))
            report.tool_call.name = chosen
        else:
            report.tool_call = classify_tool_result(spoke.get("call"))
            report.tool_call.name = chosen

        report.findings = build_findings(report)
    return report


def render(r: Report) -> str:
    lines = [f"# Shipped probe — `{r.dist}`", ""]
    if r.harness_error:
        lines += [f"⛔ {r.harness_error}", ""]
        return "\n".join(lines) + "\n"
    icon = "✅" if not r.findings else "🚨"
    lines += [
        f"{icon} publication: **{r.publication}**",
        "",
        f"- installed from the index: `{r.versions.installed or '—'}`",
        f"- repository version:       `{r.versions.repo or '—'}`",
        f"- last git tag:             `{r.versions.tag or '—'}`",
        f"- entrypoint:               `{Path(r.entrypoint).name if r.entrypoint else '—'}`",
        f"- tools listed:             {r.tools if r.tools is not None else '—'}",
        f"- tool call:                {r.tool_call.name or '—'} → {r.tool_call.status}",
    ]
    if r.tool_call.detail:
        lines.append(f"  ({r.tool_call.detail})")
    if r.findings:
        lines += ["", "## 🚨 Findings"]
        lines += [f"- **{f.code}** — {f.detail}" for f in r.findings]
    else:
        lines += ["", "The artifact users install matches the repository and runs."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dist", required=True, help="distribution name on the index")
    p.add_argument("--target", default=".", help="the target checkout (read-only)")
    p.add_argument("--tool", default="", help="tool to call (default: the first "
                                              "one needing no arguments)")
    p.add_argument("--tool-args", default="{}", help="JSON arguments for --tool")
    p.add_argument("--index-url", default=DEFAULT_INDEX)
    p.add_argument("--install-timeout", type=float, default=DEFAULT_INSTALL_TIMEOUT)
    p.add_argument("--run-timeout", type=float, default=DEFAULT_RUN_TIMEOUT)
    p.add_argument("--report", default="", help="write the machine-readable report here")
    p.add_argument("--format", choices=("text", "json"), default="text")
    args = p.parse_args(argv)

    try:
        tool_args = json.loads(args.tool_args or "{}")
        if not isinstance(tool_args, dict):
            raise ValueError("--tool-args must be a JSON object")
    except ValueError as exc:
        print(f"shipped: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    if not shutil.which("git"):
        print("shipped: git not found — the tag comparison cannot be made",
              file=sys.stderr)

    try:
        report = probe(args.dist, Path(args.target).resolve(), tool=args.tool,
                       tool_args=tool_args, index_url=args.index_url,
                       install_timeout=args.install_timeout,
                       run_timeout=args.run_timeout)
    except Exception as exc:  # noqa: BLE001 - the harness itself failed
        print(f"shipped: harness could not run: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_CANNOT_RUN

    print(json.dumps(report.as_dict(), indent=2, sort_keys=True)
          if args.format == "json" else render(report))
    if args.report:
        try:
            Path(args.report).write_text(
                json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        except OSError as exc:
            print(f"shipped: could not write {args.report}: {exc}", file=sys.stderr)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
