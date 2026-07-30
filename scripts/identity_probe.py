#!/usr/bin/env python3
"""Identity probe — does the server report the version it actually is?

Every outbound request carries a User-Agent, and that string is the only thing
an upstream sees about us. When the version in it is a hand-maintained literal,
it drifts: nothing breaks, no test fails, and the server keeps introducing
itself as a release it stopped being months ago.

A sweep across the 30 servers of the Swiss Public Data portfolio (2026-07-29)
found the problem is the rule, not the exception:

  * 12 servers sent a wrong version, 4 of them a wrong MAJOR version
    (register-mcp announced 1.0 while the package sat at 0.5.0)
  * 20 carried a stale ``__version__``
  * 17 had a README badge behind the package, one by 16 minor versions
  *  4 had a stale ``server.json`` — invisible, because publish.yml rewrites
     that field from the tag at release time, so the committed value never
     reaches the artifact and nothing ever contradicts it

None of this is caught by ruff, mypy or pytest, and none of it is a schema
drift the live probe would see. It is a distinct class, and it needs its own
deterministic check — which is what this script is.

WHY THE DETECTION LOOKS THE WAY IT DOES
---------------------------------------
Three things in here are deliberate, and each is a bug this probe made first:

1. **Scan whole files for the value pattern, not lines for the keyword.**
   ``grep -i user-agent | grep <version>`` misses a constant split over two
   lines — which is exactly how swiss-electricity-mcp kept shipping 0.2.0
   through three call sites *after* a fix had already been merged and
   reported. The identifier and its value need not share a line. (It also
   misses ``USER_AGENT``: an underscore is not a hyphen.)

2. **Comments are not findings.** The first version of this check flagged
   ``# the User-Agent in server.py carried "bakom-mcp/1.0"`` — a comment
   documenting the very incident the check exists to prevent. A rule that
   turns CI red on good documentation teaches people to delete the
   documentation. Comments are stripped with ``tokenize``, not ``split("#")``,
   because a ``#`` inside a string literal must not truncate the line.

3. **Report every category, then exit.** An earlier version aborted on the
   first finding. It reported a stale badge and never reached the source scan —
   for eight of nine repositories the serious question went unanswered while
   the report looked complete.

Fallbacks are recognised by their PEP 440 local segment (``0.0.0+source``),
never by matching a fixed marker string: a portfolio that spells it
``0+unknown`` in one repo and ``0.0.0+source`` in another produced nine false
positives that way. A fallback of plain ``"0.0.0"`` is correctly reported — it
is indistinguishable from a real release, which is the whole objection to it.

ARTIFACT-LEVEL EVIDENCE
-----------------------
``--installed`` resolves the User-Agent from the *installed distribution*
rather than the source tree. That is the only check that proves what ships:
metadata is written at install time, so an editable install keeps reporting
the pre-bump version until it is reinstalled. Source can be perfect and the
artifact still wrong.

Exit codes:
  0  no findings
  1  findings (all categories reported before exiting)
  2  the target is not shaped as expected (no pyproject.toml)

Usage:
  python scripts/identity_probe.py --target ../swiss-environment-mcp
  python scripts/identity_probe.py --target . --installed --format json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomllib landed in 3.11
    tomllib = None  # type: ignore[assignment]

BADGE = re.compile(r"img\.shields\.io/badge/[Vv]ersion-([^-\s)]+)-")


@dataclass
class Report:
    dist: str
    version: str
    declared: list[dict[str, str]] = field(default_factory=list)
    hardcoded: list[dict[str, Any]] = field(default_factory=list)
    runtime: dict[str, str] | None = None

    @property
    def drift(self) -> list[dict[str, str]]:
        return [d for d in self.declared if d["value"] != self.version]

    @property
    def ok(self) -> bool:
        runtime_bad = bool(self.runtime and self.runtime.get("status") == "mismatch")
        return not self.drift and not self.hardcoded and not runtime_bad


def read_project(root: Path) -> dict[str, Any]:
    """The ``[project]`` table. Minimal parser when tomllib is unavailable.

    Only ``name`` and ``version`` are needed; pulling in ``tomli`` just so a
    check can run on 3.10 would be out of proportion.
    """
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)["project"]
    section = re.search(r"^\[project\]\s*$(.*?)(?=^\[)", text, re.MULTILINE | re.DOTALL)
    body = section.group(1) if section else text
    out: dict[str, Any] = {}
    for key in ("name", "version"):
        m = re.search(rf'^{key}\s*=\s*"([^"]+)"', body, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def code_lines(text: str) -> list[str]:
    """Lines with comments blanked out (see docstring, point 2)."""
    lines = text.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                lines[row - 1] = lines[row - 1][:col]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text.splitlines()  # unparseable: check it whole rather than skip it
    return lines


def find_hardcoded(root: Path, dist: str) -> list[dict[str, Any]]:
    """Hand-maintained versions under src/, fallback markers excluded."""
    src = root / "src"
    if not src.is_dir():
        return []
    ua = re.compile(rf"{re.escape(dist)}/(\d+\.\d[^\s\"']*)")
    dunder = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
    hits: list[dict[str, Any]] = []
    for path in sorted(src.rglob("*.py")):
        for lineno, line in enumerate(code_lines(path.read_text(encoding="utf-8")), 1):
            values = [m.group(1) for m in ua.finditer(line)]
            values += [
                m.group(1) for m in dunder.finditer(line) if re.match(r"\d+\.\d", m.group(1))
            ]
            if any("+" not in v for v in values):
                hits.append(
                    {"file": str(path.relative_to(root)), "line": lineno, "code": line.strip()}
                )
    return hits


def collect_declared(root: Path) -> list[dict[str, str]]:
    """Every place that repeats the version: manifest fields and doc badges."""
    found: list[dict[str, str]] = []
    manifest = root / "server.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        found.append({"where": "server.json → version", "value": data.get("version", "")})
        for i, pkg in enumerate(data.get("packages", [])):
            found.append(
                {"where": f"server.json → packages[{i}].version", "value": pkg.get("version", "")}
            )
    for readme in sorted(root.glob("README*.md")):
        for m in BADGE.finditer(readme.read_text(encoding="utf-8")):
            found.append({"where": f"{readme.name} → version badge", "value": m.group(1)})
    return found


def resolve_runtime(dist: str, expected: str) -> dict[str, str]:
    """Ask the installed distribution what it is. The only artifact-level proof."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover
        return {"status": "unavailable", "detail": "importlib.metadata missing"}
    try:
        installed = version(dist)
    except PackageNotFoundError:
        return {
            "status": "not_installed",
            "detail": f"{dist} is not installed in this interpreter — source checked, artifact not",
        }
    if installed != expected:
        return {
            "status": "mismatch",
            "installed": installed,
            "expected": expected,
            "detail": (
                "Installed metadata disagrees with pyproject.toml. After a version bump in an "
                "editable install, re-run `pip install -e .` — metadata is written at install "
                "time, not at import time."
            ),
        }
    return {"status": "match", "installed": installed}


def probe(target: Path, check_installed: bool) -> Report:
    project = read_project(target)
    dist = project["name"]
    version = project.get("version")
    if version is None:
        # dynamic = ["version"]: the literal in src/ IS the source there.
        return Report(dist=dist, version="(dynamic)")
    report = Report(dist=dist, version=version)
    report.declared = collect_declared(target)
    report.hardcoded = find_hardcoded(target, dist)
    if check_installed:
        report.runtime = resolve_runtime(dist, version)
    return report


def render(report: Report) -> str:
    out: list[str] = []
    if report.version == "(dynamic)":
        return f"{report.dist}: dynamic version — identity probe skipped."
    for d in report.drift:
        out.append(f"DRIFT      {d['where']} = {d['value']!r} (pyproject {report.version!r})")
    for h in report.hardcoded:
        out.append(f"HARDCODED  {h['file']}:{h['line']}: {h['code'][:100]}")
    if report.runtime and report.runtime["status"] == "mismatch":
        out.append(
            f"ARTIFACT   installed {report.runtime['installed']!r} != "
            f"pyproject {report.runtime['expected']!r} — {report.runtime['detail']}"
        )
    if report.runtime and report.runtime["status"] == "not_installed":
        out.append(f"NOTE       {report.runtime['detail']}")
    if not out:
        checked = ", ".join(d["where"] for d in report.declared) or "no further places"
        return f"identity OK ({report.dist} {report.version}; checked: {checked}; src/ clean)"
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(prog="identity_probe")
    ap.add_argument("--target", default=".", help="path to the MCP server repo")
    ap.add_argument(
        "--installed",
        action="store_true",
        help="also resolve the version from the installed distribution (artifact-level)",
    )
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not (target / "pyproject.toml").exists():
        print(f"{target}: no pyproject.toml — not a Python MCP server repo", file=sys.stderr)
        return 2

    report = probe(target, args.installed)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "dist": report.dist,
                    "version": report.version,
                    "drift": report.drift,
                    "hardcoded": report.hardcoded,
                    "runtime": report.runtime,
                    "ok": report.ok,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(render(report))

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
