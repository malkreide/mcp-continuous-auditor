#!/usr/bin/env python3
"""Published probe — what does the package on PyPI actually send?

``identity_probe.py`` reads a repository. This one reads the artifact: it
installs the distribution from the index into a throwaway venv and measures the
User-Agent the installed code would put on the wire. Those are different
questions, and the gap between them is where this class of bug lives — a source
tree can be perfectly clean for weeks while every user who runs ``pip install``
still gets the old, wrong identity, because the fix was merged and never
released.

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

Exit codes:
  0  every resolved User-Agent matches the installed version, or the package
     sets none at all
  1  drift, a foreign User-Agent, or one that could not be resolved
     (``unverified`` — which is not a pass)
  2  the distribution could not be installed

Usage:
  python scripts/published_probe.py lobbywatch-mcp
  python scripts/published_probe.py --format json bakom-mcp srgssr-mcp
  python scripts/published_probe.py --constraint 'mcp<2' swiss-statistics-mcp
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import tempfile
import tokenize
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Runs inside the target venv: import every submodule and collect any string
# that looks like a product token followed by a version, wherever it sits.
RUNTIME_SCAN = r'''
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

for top in sorted(tops):
    try:
        pkg = importlib.import_module(top)
    except Exception as e:
        errors.append("%s: %s: %s" % (top, type(e).__name__, e)); continue
    scan(pkg)
    for _, modname, _ in pkgutil.walk_packages(getattr(pkg, "__path__", []), top + "."):
        if modname.rsplit(".", 1)[-1] == "__main__":
            continue                      # importing it would start the server
        try:
            scan(importlib.import_module(modname))
        except Exception as e:
            errors.append("%s: %s: %s" % (modname, type(e).__name__, e))

seen_v, uniq = set(), []
for f in found:
    if f["value"] not in seen_v:
        seen_v.add(f["value"]); uniq.append(f)
print(json.dumps({"installed": installed, "runtime": uniq, "errors": errors[:8]}))
'''

# Runs inside the target venv: resolve one f-string's interpolated expression
# against the module that defines it. Measured, not inferred.
EVAL_EXPR = r'''
import importlib, json, sys, warnings
warnings.filterwarnings("ignore")
mod, expr = sys.argv[1], sys.argv[2]
try:
    print(json.dumps({"value": str(eval(expr, vars(importlib.import_module(mod))))}))
except Exception as e:
    print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
'''

# f"token/{expr}" — the inline form that carries no literal digit and is
# therefore invisible to a literal scan.
FSTRING = re.compile(r'f["\']([A-Za-z][A-Za-z0-9_.-]*)/\{([A-Za-z_][A-Za-z0-9_.]*)\}')
# "token/1.2.3" — a hand-maintained literal.
LITERAL = re.compile(r'["\']([A-Za-z][A-Za-z0-9_.+-]*/\d[^"\']*)["\']')
MENTIONS_UA = re.compile(r"user.?agent", re.I)


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
class Result:
    dist: str
    installed: str | None = None
    status: str = "unverified"
    findings: list[Finding] = field(default_factory=list)
    mentions_ua: bool = False
    detail: str = ""
    import_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "no_user_agent")


def _sent_version(value: str) -> str | None:
    m = re.match(r"^[^/]+/([^\s;()]+)", value)
    return m.group(1) if m else None


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


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


def probe(dist: str, constraint: str | None = None, keep: bool = False) -> Result:
    env = Path(tempfile.mkdtemp(prefix=f"probe-{dist}-"))
    try:
        venv.create(env, with_pip=True, clear=True)
        py = env / "bin" / "python"
        spec = [dist] + ([constraint] if constraint else [])
        install = subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "--no-cache-dir", *spec],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if install.returncode != 0:
            tail = (install.stderr or install.stdout).strip().splitlines()[-1:] or ["?"]
            return Result(dist=dist, status="install_failed", detail=tail[0][:300])

        out = _run(py, RUNTIME_SCAN, dist)
        if "installed" not in out:
            return Result(dist=dist, status="install_failed", detail=str(out.get("error"))[:300])

        result = Result(dist=dist, installed=out["installed"], import_errors=out.get("errors", []))
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

            modname = str(rel.with_suffix("")).replace("/", ".").removesuffix(".__init__")
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
                            where=f"{modname}: f\"{token}/{{{expr}}}\"",
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

        foreign = [f for f in result.findings if not f.own]
        graded = [f for f in result.findings if f.own and f.sent_version is not None]
        if graded and not all(f._ok for f in graded):
            result.status = "drift"
        elif foreign:
            # Someone else's identity on our requests. Not version drift —
            # a separate, larger question about what the server tells upstreams.
            result.status = "foreign_user_agent"
            result.detail = "sends a User-Agent that is not this package's identity"
        elif graded:
            result.status = "ok"
        elif result.mentions_ua:
            # Source talks about a User-Agent and no strategy resolved one.
            # That is a gap in this probe, not a clean package.
            result.status = "unverified"
            result.detail = "source mentions a User-Agent but no value could be resolved"
        else:
            result.status = "no_user_agent"
            result.detail = "no custom User-Agent — requests go out under the HTTP client default"
        return result
    finally:
        if not keep:
            shutil.rmtree(env, ignore_errors=True)


def render(r: Result) -> str:
    if r.status == "install_failed":
        return f"INSTALL    {r.dist}: {r.detail}"
    head = f"{r.dist} {r.installed}"
    if r.status == "ok":
        vals = ", ".join(f.value for f in r.findings) or "-"
        return f"OK         {head} sends {vals}"
    if r.status == "no_user_agent":
        return f"NO-UA      {head}: {r.detail}"
    if r.status == "unverified":
        return f"UNVERIFIED {head}: {r.detail}"
    lines = []
    for f in r.findings:
        if not f.own:
            lines.append(f"FOREIGN-UA {head} sends {f.value[:70]!r} (via {f.evidence}, {f.where})")
        elif not f._ok:
            lines.append(
                f"DRIFT      {head} sends {f.sent_version} "
                f"({f.value!r}, via {f.evidence}, {f.where})"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="published_probe")
    ap.add_argument("dists", nargs="+", help="distribution names as published on the index")
    ap.add_argument(
        "--constraint",
        help="extra pip requirement, e.g. 'mcp<2' for a package that no longer imports "
        "against the current release of a dependency",
    )
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--keep-venv", action="store_true", help="leave the venv for inspection")
    args = ap.parse_args()

    results = []
    for dist in args.dists:
        try:
            results.append(probe(dist, args.constraint, args.keep_venv))
        except subprocess.TimeoutExpired:
            results.append(Result(dist=dist, status="install_failed", detail="timeout"))

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "dist": r.dist,
                        "installed": r.installed,
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
                        "import_errors": r.import_errors,
                    }
                    for r in results
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for r in results:
            print(render(r))

    if any(r.status == "install_failed" for r in results):
        return 2
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
