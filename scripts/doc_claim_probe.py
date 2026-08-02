#!/usr/bin/env python3
"""Doc-claim probe — do the identifiers the documentation cites actually exist?

THE INCIDENT
------------
A written justification for a finding — ``ARCH-003`` — named ten rubric codes
as the ones it had been graded against. None of the ten was in ``GREEN_RUBRICS``.
The prose was confident, internally consistent, and about identifiers that were
not in the code. Nobody caught it in review, because checking would have meant
opening ten files to look up ten constants, and prose that *sounds* like it
cites the source is exactly the prose reviewers stop checking.

That is a mechanical error and it deserves a mechanical check. An identifier is
the one part of a documentation claim that a machine can verify without
understanding the sentence: if ``SECURITY.md`` says a check is called
``NO_TAGS``, either that string is in the code or the document is describing
something that does not exist.

WHAT COUNTS AS A CLAIM
----------------------
Only what the author marked as code — a ```backtick span`` `` — and only three
shapes. Everything else in a document is prose, and prose is not this probe's
business:

1. **Identifier codes**: ``LOCK_DRIFT``, ``ARCH-003``, ``NO_TAGS``,
   ``UNYANKED_BROKEN_RELEASE``. All-caps segments joined by ``-`` or ``_``.
   The shape is deliberately narrow: single words (``README``, ``GET``) are not
   claims about identifiers, and ``Requires-Dist`` and ``User-Agent`` are not
   either — a lowercase letter takes a token out of scope, which is what keeps
   the HTTP and packaging vocabulary out of the findings.
2. **Paths**: ``scripts/yank_probe.py``, ``.github/workflows/tests.yml``. A
   path is a claim that resolves against the filesystem, and a document that
   points at a file that was renamed is wrong in a way readers only find out
   about at the worst moment.
3. **Collection membership**: where a document names a collection constant that
   exists in the code — ``GREEN_RUBRICS`` — every code-shaped token in the same
   paragraph is checked against that collection's actual members. This is the
   incident, caught exactly where it happened.

WHERE A CLAIM MAY RESOLVE
-------------------------
In the code, and not in more prose. A rubric code that appears in the README, in
the German README and in the CHANGELOG and nowhere else has been repeated, not
defined. So the resolution index is built from the non-Markdown files of the
repository: ``.py``, ``.yaml``, ``.json``, ``.toml``, ``.sh``, workflow
templates. A promptfoo rubric lives in YAML, an exit code in Python, and both
are equally good answers to "does this identifier exist".

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* **Fenced blocks are read for paths only.** A shell example says
  ``python scripts/foo.py``, and that path is a real claim. The same block's
  sample OUTPUT is illustration — flagging an identifier there would turn every
  worked example into a finding.
* **Standards namespaces are exempt.** ``PEP-658``, ``RFC-6749``, ``CVE-2024-1``
  are citations, not identifiers, and no amount of grepping the repository will
  resolve them. The list is short, named, and extensible with ``--ignore``.
* **It does not check whether the sentence is TRUE.** ``NO_TAGS`` existing does
  not make the paragraph around it correct. This probe answers the smaller
  question it can answer without judgement, and says so rather than implying the
  larger one.
* **It does not flag an identifier the docs never mention.** Documentation
  completeness is a different check with a different failure mode.

EXIT CODES
  0    every cited identifier and path resolves
  2    FINDING — an unresolved citation, a dead path, a false membership claim
  3    NOT MEASURED — no documentation files matched
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run

Usage:
  python scripts/doc_claim_probe.py --target .
  python scripts/doc_claim_probe.py --target . --format json
  python scripts/doc_claim_probe.py --target . --doc SECURITY.md --doc SECURITY.de.md
  python scripts/doc_claim_probe.py --target . --ignore OPS --ignore FID
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

DEFAULT_DOCS = ("README.md", "README.de.md", "SECURITY.md", "SECURITY.de.md")
DEFAULT_CODE_SUFFIXES = (
    ".py", ".yaml", ".yml", ".json", ".toml", ".sh", ".cfg", ".ini", ".template",
    ".js", ".ts", ".nft", ".txt",
)
SKIP_DIRS = {
    ".git", ".venv", "venv", ".tox", "node_modules", "__pycache__", "dist",
    "build", ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages",
    ".audit", ".eggs",
}

# Citations, not identifiers: no amount of grepping resolves them, and reporting
# them would drown the findings the probe exists for.
STANDARDS = frozenset({
    "PEP", "RFC", "ISO", "CVE", "CWE", "OWASP", "GDPR", "HTTP", "HTTPS", "UTF",
    "SHA", "TLS", "SSL", "DNS", "API", "URL", "URI", "JSON", "YAML", "TOML",
    "MIT", "GPL", "BSD", "SPDX", "MCP", "LLM", "AI",
})

INLINE_SPAN = re.compile(r"`([^`\n]+)`")
FENCE = re.compile(r"^\s*(```|~~~)")
# All-caps segments joined by - or _. `LOCK_DRIFT`, `ARCH-003`, `NO_TAGS`.
CODE_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
PATH_LIKE = re.compile(
    r"^[A-Za-z0-9._][A-Za-z0-9._/-]*\."
    r"(?:py|sh|md|ya?ml|json|toml|txt|cfg|ini|nft|js|ts|template)$")
# Anything a resolution could match: identifiers, dotted paths, hyphenated codes.
WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*")
# Paths may start with a dot — `.github/workflows/tests.yml` is the case that
# taught this its own pattern: WORD skipped the leading dot and reported the
# truncated remainder as a dead path, which is a probe inventing a finding.
PATH_TOKEN = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
# A line that links to another repository is citing that repository's
# identifiers, not this one's. `OPS-005` belongs to `mcp-audit-skill` and no
# amount of grepping here will resolve it. Such tokens are LISTED in the report
# rather than dropped, so the exemption stays visible.
EXTERNAL_LINK = re.compile(r"https?://[^\s)]*github\.com/[^\s)]+")
URL = re.compile(r"https?://\S+")


@dataclass
class Claim:
    """One thing a document asserts exists."""

    kind: str      # code | path
    token: str
    doc: str
    line: int
    context: str = ""


@dataclass
class Collection:
    """A named collection of string constants, as the code defines it."""

    name: str
    members: frozenset[str]
    where: str

    @property
    def shape(self) -> re.Pattern[str] | None:
        """A pattern all members share, or None when they do not share one.

        Membership is only enforced against tokens that LOOK like members. Half
        the value of this check is in not firing when a paragraph mentions a
        collection and, quite legitimately, some unrelated constant beside it.
        """
        if len(self.members) < 2:
            return None
        prefixes = {m.split("-")[0].split("_")[0] for m in self.members}
        seps = {("-" if "-" in m else "_" if "_" in m else "") for m in self.members}
        if len(seps) != 1 or not next(iter(seps)):
            return None
        sep = re.escape(next(iter(seps)))
        if len(prefixes) == 1:
            return re.compile(rf"^{re.escape(next(iter(prefixes)))}{sep}[A-Z0-9]+$")
        return re.compile(rf"^[A-Z][A-Z0-9]*{sep}[A-Z0-9]+$")


@dataclass
class Finding:
    code: str
    severity: str
    doc: str
    line: int
    token: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "doc": self.doc,
                "line": self.line, "token": self.token, "detail": self.detail}


@dataclass
class Report:
    target: str
    docs: list[str] = field(default_factory=list)
    claims: int = 0
    code_files: int = 0
    collections: list[str] = field(default_factory=list)
    # Identifiers cited beside a link to another repository. Listed, not
    # resolved: an exemption that is not visible is indistinguishable from a
    # blind spot.
    external: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    harness_error: str = ""
    provenance: probe_provenance.Provenance | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "probe": "doc_claim",
            "target": self.target,
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "docs": list(self.docs),
            "claims_checked": self.claims,
            "code_files_indexed": self.code_files,
            "collections": list(self.collections),
            "external_citations": list(self.external),
            "notes": list(self.notes),
            "findings": [f.as_dict() for f in self.findings],
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
        if not self.docs:
            return EXIT_NOT_MEASURED
        return EXIT_GREEN


# --------------------------------------------------------------------------
# Reading the documents
# --------------------------------------------------------------------------


def paragraphs(text: str) -> list[tuple[int, str]]:
    """(first line number, text) per blank-line-separated block.

    The paragraph is the unit membership is judged in: a claim and the
    collection it claims to belong to are written next to each other, and
    widening the window to the section would sweep in every unrelated code in
    between.
    """
    out: list[tuple[int, str]] = []
    start, buf = 1, []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not buf:
                start = lineno
            buf.append(line)
        elif buf:
            out.append((start, "\n".join(buf)))
            buf = []
    if buf:
        out.append((start, "\n".join(buf)))
    return out


def extract(doc: Path, name: str) -> tuple[list[Claim], list[Claim]]:
    """(claims, external citations) for one document.

    Identifiers come from inline spans outside fenced blocks; paths come from
    everywhere, fences included. A fenced block's commands cite real files —
    that is a claim — while its sample OUTPUT is illustration, and flagging an
    identifier there would turn every worked example into a finding.

    An identifier on a line that links to another repository is that
    repository's, and is returned separately: reported, never resolved.
    """
    claims: list[Claim] = []
    external: list[Claim] = []
    fenced = False
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        spans = [m.group(1) for m in INLINE_SPAN.finditer(line)] if not fenced else []
        cites_elsewhere = bool(EXTERNAL_LINK.search(line))
        for span in spans:
            for token in WORD.findall(span):
                if CODE_ID.match(token) and not _is_standard(token):
                    claim = Claim("code", token, name, lineno, line.strip()[:160])
                    (external if cites_elsewhere else claims).append(claim)
        # URLs are stripped first: `github.com/o/r/blob/main/scripts/foo.py`
        # matches the path shape exactly and resolves against nothing here.
        haystack = URL.sub(" ", line if fenced else " ".join(spans) or line)
        for token in PATH_TOKEN.findall(haystack):
            if PATH_LIKE.match(token) and "/" in token:
                claim = Claim("path", token, name, lineno, line.strip()[:160])
                (external if cites_elsewhere else claims).append(claim)
    return claims, external


def _is_standard(token: str) -> bool:
    prefix = re.split(r"[-_]", token)[0]
    return prefix in STANDARDS


# --------------------------------------------------------------------------
# Reading the code
# --------------------------------------------------------------------------


def iter_code(target: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in suffixes or "".join(path.suffixes[-2:]) in suffixes:
            out.append(path)
    return out


def build_index(files: list[Path]) -> set[str]:
    """Every token the code contains. The resolution target.

    A flat set rather than a symbol table on purpose: a rubric code lives in
    promptfoo YAML, an exit code in Python and a check name in a shell script,
    and "is this identifier present in the code at all" is the question the
    documentation claim actually raises. A Python-only symbol table would report
    every YAML-defined rubric as missing — a probe that is wrong about the
    incident it was written for.
    """
    index: set[str] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index.update(WORD.findall(text))
    return index


def find_collections(files: list[Path], root: Path) -> dict[str, Collection]:
    """Module-level collections of string constants, by name.

    ``ast`` rather than a regex: a multi-line set literal is the normal way to
    write one of these, and a line-based reader would find the first two members
    and quietly report the rest as non-members.
    """
    found: dict[str, Collection] = {}
    for path in (p for p in files if p.suffix == ".py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            # OSError included on purpose: a file can vanish between the listing
            # and the read, and a probe that dies on that reports nothing at all
            # about the other 119 files.
            continue
        for node in tree.body:
            targets: list[ast.expr]
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            value = node.value
            if value is None:
                continue
            members = _string_members(value)
            if members is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, Collection(
                        name=target.id, members=members,
                        where=f"{path.relative_to(root)}:{node.lineno}"))
    return found


def _string_members(value: ast.expr) -> frozenset[str] | None:
    """The string constants of a literal collection, or None if it is not one.

    ``frozenset({...})`` and ``set([...])`` are unwrapped: they are how a
    constant collection is usually spelled when it must not be mutated, and not
    seeing through the call would mean not seeing the collections most worth
    checking.
    """
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id in ("frozenset", "set", "tuple", "list") and value.args:
            return _string_members(value.args[0])
        return None
    if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return None
    members = set()
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        members.add(element.value)
    return frozenset(members) if members else None


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


def check_membership(doc_name: str, text: str, collections: dict[str, Collection],
                     index: set[str]) -> list[Finding]:
    """The ARCH-003 case: codes cited beside a collection they are not in."""
    findings: list[Finding] = []
    for start, block in paragraphs(text):
        spans = " ".join(m.group(1) for m in INLINE_SPAN.finditer(block))
        named = [collections[t] for t in WORD.findall(spans) if t in collections]
        if not named:
            continue
        for collection in named:
            shape = collection.shape
            if shape is None:
                continue
            for token in dict.fromkeys(WORD.findall(spans)):
                if token == collection.name or not shape.match(token):
                    continue
                if token in collection.members:
                    continue
                findings.append(Finding(
                    code="NOT_A_MEMBER", severity="medium", doc=doc_name,
                    line=start, token=token,
                    detail=(
                        f"cited in the same paragraph as `{collection.name}` and shaped "
                        f"like its members, but {collection.name} "
                        f"({collection.where}) does not contain it. Members: "
                        f"{', '.join(sorted(collection.members)[:8])}"
                        f"{' …' if len(collection.members) > 8 else ''}. "
                        + ("The identifier does exist elsewhere in the code, so this "
                           "reads as a membership claim that is not true — if the "
                           "paragraph means to contrast it, say so in words."
                           if token in index else
                           "The identifier does not exist in the code at all."))))
    return findings


def run(target: Path, docs: tuple[str, ...] = DEFAULT_DOCS,
        ignore: tuple[str, ...] = (),
        suffixes: tuple[str, ...] = DEFAULT_CODE_SUFFIXES) -> Report:
    report = Report(target=str(target))
    if not target.is_dir():
        report.harness_error = f"{target} is not a directory"
        return report

    doc_paths = [target / name for name in docs if (target / name).is_file()]
    if not doc_paths:
        report.notes.append(
            f"none of {', '.join(docs)} exists in {target} — nothing was measured. "
            "This run is not evidence that the documentation cites real identifiers")
        return report
    report.docs = [p.name for p in doc_paths]

    code_files = [p for p in iter_code(target, suffixes) if p.name not in set(docs)]
    report.code_files = len(code_files)
    if not code_files:
        report.harness_error = (
            f"no code files under {target} — every citation would be reported as "
            "unresolved, which would say more about this run than about the docs")
        return report

    index = build_index(code_files)
    collections = find_collections(code_files, target)
    report.collections = sorted(collections)
    ignored = set(ignore)

    for path in doc_paths:
        text = path.read_text(encoding="utf-8")
        claims, external = extract(path, path.name)
        report.claims += len(claims)
        if external:
            cited = sorted({c.token for c in external})
            report.external.extend(f"{path.name}: {t}" for t in cited)
            report.notes.append(
                f"{path.name} cites {len(cited)} identifier(s) on lines that link to "
                f"another repository ({', '.join(cited[:6])}"
                f"{' …' if len(cited) > 6 else ''}) — those belong to that repository "
                "and are not resolved here. Not a finding, and not a claim about them "
                "either")
        seen: set[tuple[str, str]] = set()
        for claim in claims:
            if re.split(r"[-_]", claim.token)[0] in ignored or claim.token in ignored:
                continue
            key = (claim.kind, claim.token)
            if key in seen:
                # One finding per identifier per document: a README that cites a
                # dead constant nine times has one problem, not nine.
                continue
            seen.add(key)
            if claim.kind == "code":
                if claim.token not in index:
                    report.findings.append(Finding(
                        code="UNRESOLVED_CLAIM", severity="high", doc=claim.doc,
                        line=claim.line, token=claim.token,
                        detail=(
                            f"`{claim.token}` is cited as an identifier but appears "
                            f"nowhere in the {len(code_files)} code file(s) of this "
                            f"repository — only in prose. Context: {claim.context}")))
            elif not (target / claim.token).exists():
                report.findings.append(Finding(
                    code="UNRESOLVED_PATH", severity="high", doc=claim.doc,
                    line=claim.line, token=claim.token,
                    detail=(
                        f"`{claim.token}` does not exist in the checkout. A document "
                        "pointing at a renamed file is wrong in the way readers only "
                        f"discover at the worst moment. Context: {claim.context}")))
        report.findings.extend(
            f for f in check_membership(path.name, text, collections, index)
            if re.split(r"[-_]", f.token)[0] not in ignored and f.token not in ignored)

    return report


def render(report: Report) -> str:
    lines = [f"doc-claim probe — {report.target}"]
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
        if report.provenance.blocking:
            lines.append(f"  {report.provenance.moved_detail()}")
            return "\n".join(lines)
    if report.harness_error:
        lines.append(f"  HARNESS: {report.harness_error}")
        return "\n".join(lines)
    if not report.docs:
        lines.append(f"  NOT MEASURED: {report.notes[0]}")
        return "\n".join(lines)
    lines.append(
        f"  {report.claims} citation(s) in {', '.join(report.docs)} against "
        f"{report.code_files} code file(s), {len(report.collections)} collection(s)")
    for note in report.notes:
        lines.append(f"  note: {note}")
    if not report.findings:
        lines.append("  every cited identifier and path resolves")
    for finding in report.findings:
        lines.append(f"  {finding.code} [{finding.severity}] "
                     f"{finding.doc}:{finding.line} `{finding.token}` — {finding.detail}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=".", help="path to the checkout")
    parser.add_argument("--doc", action="append", default=[],
                        help=f"documentation file to check, repeatable "
                             f"(default: {', '.join(DEFAULT_DOCS)})")
    parser.add_argument("--ignore", action="append", default=[],
                        help="an identifier, or the prefix before its separator, to "
                             "treat as a citation rather than a claim. Repeatable")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", default="", help="also write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()

    prov = probe_provenance.capture(target)
    report = run(target, docs=tuple(args.doc) or DEFAULT_DOCS,
                 ignore=tuple(args.ignore))
    report.provenance = prov.recheck()

    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
