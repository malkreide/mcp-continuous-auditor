#!/usr/bin/env python3
"""Parity probe — does the translated documentation still say the same thing?

WHY
---
The portfolio ships every repository bilingually: ``README.md`` beside
``README.de.md``, ``SECURITY.md`` beside ``SECURITY.de.md``. Nothing checks the
pair. The English side is where features get written up, so it moves first, and
the German side is the one a Swiss reader opens first. The drift is one-way and
it is invisible: both files are valid Markdown, both render, and the only way to
notice a section that exists in one and not the other is to read both and count.

That makes it exactly the kind of defect this repository exists to mechanise —
and the German side of this very README had drifted before this probe was
written.

WHY IT DOES NOT COMPARE THE TEXT
--------------------------------
The two files are *supposed* to differ in every word. ``Overview`` and
``Übersicht`` are a correct translation and a string comparison would call them
drift. So the probe compares only what a translation must preserve:

1. **The heading skeleton** — the sequence of heading levels. A translation may
   rename every heading; it may not drop one, add one, or reorder them. When the
   sequences diverge, the report prints the position and BOTH titles, because
   "section 7 differs" is not actionable and "``## Roadmap`` vs (nothing)" is.
2. **Top-level list items per section** — the shape the observed drift took: a
   feature added to one list and not the other. Nested items are not counted;
   a translator legitimately splits or joins a sub-point.
3. **Fenced code blocks** — count first, then content with comment lines
   removed, since comments inside an example are prose and are meant to be
   translated. Commands are not: a German README that installs a renamed script
   is wrong in a way no reader can see from the German alone.
4. **Link targets** — the set of URLs and relative paths. The cross-language
   link itself is excluded in both directions; it is the one link that is
   *supposed* to differ.
5. **Translation lag** — commits that touched the base file after the last
   commit that touched the translation. This is the only check that can say "the
   German side is behind" while every structural check above is green, which is
   what a partial translation of an existing paragraph looks like.

NOT MEASURED IS NOT CLEAN
-------------------------
A repository with no translated file has no parity to check and exits ``3``.
Without git on the PATH, the lag check does not run and the report says so —
the structural findings still stand, but the run must not read as evidence
about freshness it never measured.

EXIT CODES
  0    the pairs are structurally parallel
  2    FINDING — a section, a bullet, a block or a link exists on one side only
  3    NOT MEASURED — no translated documents found
  4    MOVED_DURING_RUN — the checkout changed under the probe (probe_provenance)
  127  the HARNESS could not run

Usage:
  python scripts/parity_probe.py --target .
  python scripts/parity_probe.py --target . --lang fr --format json
  python scripts/parity_probe.py --target . --pair README.md:README.de.md
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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

DEFAULT_LANG = "de"

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
TOP_LEVEL_ITEM = re.compile(r"^(?:[-*+]|\d+\.)\s+\S")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
COMMENT_LINE = re.compile(r"^\s*(#|//|--)")


@dataclass
class Section:
    level: int
    title: str
    line: int
    items: int = 0  # top-level list items
    # (info string, normalised body). The info string decides whether the body
    # is compared at all — see `normalise_block`.
    blocks: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DocView:
    name: str
    sections: list[Section] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def skeleton(self) -> tuple[int, ...]:
        return tuple(s.level for s in self.sections)


@dataclass
class Finding:
    code: str
    severity: str
    pair: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "pair": self.pair,
            "detail": self.detail,
        }


@dataclass
class PairResult:
    base: str
    translation: str
    base_sections: int = 0
    translation_sections: int = 0
    lag: list[str] = field(default_factory=list)
    lag_measured: bool = False

    @property
    def name(self) -> str:
        return f"{self.base} ↔ {self.translation}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "translation": self.translation,
            "base_sections": self.base_sections,
            "translation_sections": self.translation_sections,
            "lag_measured": self.lag_measured,
            "lag_commits": list(self.lag),
        }


@dataclass
class Report:
    target: str
    pairs: list[PairResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    harness_error: str = ""
    provenance: probe_provenance.Provenance | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "probe": "parity",
            "target": self.target,
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "pairs": [p.as_dict() for p in self.pairs],
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
        if not self.pairs:
            return EXIT_NOT_MEASURED
        return EXIT_GREEN


# --------------------------------------------------------------------------
# Reading one document
# --------------------------------------------------------------------------


def parse(path: Path) -> DocView:
    """The structural skeleton of a Markdown file. No prose survives this."""
    view = DocView(name=path.name)
    current = Section(level=0, title="(preamble)", line=1)
    view.sections.append(current)
    fenced = False
    info = ""
    block: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fence = FENCE.match(line)
        if fence:
            if fenced:
                current.blocks.append((info, normalise_block(block)))
                block, info = [], ""
            else:
                info = line.strip()[len(fence.group(1)) :].strip()
            fenced = not fenced
            continue
        if fenced:
            block.append(line)
            continue
        heading = HEADING.match(line)
        if heading:
            current = Section(
                level=len(heading.group(1)), title=heading.group(2), line=lineno
            )
            view.sections.append(current)
            continue
        if TOP_LEVEL_ITEM.match(line):
            current.items += 1
        view.links.extend(LINK.findall(line))
    if fenced and block:
        # An unclosed fence: keep what was read rather than dropping it. The
        # imbalance shows up as a block-count difference, which is a finding
        # worth having.
        current.blocks.append((info, normalise_block(block)))
    return view


def normalise_block(lines: list[str]) -> str:
    """A code block reduced to what a translation must not change.

    Comments are dropped — whole lines and trailing ones alike. A comment inside
    an example is prose and is *meant* to be translated; ``# fill in tokens``
    beside ``# Tokens eintragen`` is a correct translation and reporting it as
    drift is how a check gets switched off. What must survive is the command.
    """
    kept = []
    for line in lines:
        stripped = _strip_trailing_comment(line).strip()
        if stripped and not COMMENT_LINE.match(stripped):
            kept.append(stripped)
    return "\n".join(kept)


def _strip_trailing_comment(line: str) -> str:
    """Drop a trailing ``#`` comment that is not inside quotes.

    Quote-aware because ``--tool-args '{"q": "#tag"}'`` is a command, not a
    comment, and truncating it would invent a difference between two identical
    examples.
    """
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#" and index and line[index - 1] in " \t":
            return line[:index]
    return line


# --------------------------------------------------------------------------
# Comparing a pair
# --------------------------------------------------------------------------


def compare(base: DocView, other: DocView, pair: str) -> list[Finding]:
    findings: list[Finding] = []

    if base.skeleton != other.skeleton:
        findings.extend(skeleton_findings(base, other, pair))
    else:
        # Only meaningful once the skeletons align: without that, section i on
        # one side and section i on the other are different sections, and every
        # count difference below would be an artefact of the misalignment.
        # strict: the skeletons were compared above and are equal here.
        for left, right in zip(base.sections, other.sections, strict=True):
            if left.items != right.items:
                findings.append(
                    Finding(
                        code="BULLET_DRIFT",
                        severity="medium",
                        pair=pair,
                        detail=(
                            f"`{base.name}` §{left.line} “{left.title}” has {left.items} "
                            f"top-level item(s); `{other.name}` §{right.line} "
                            f"“{right.title}” has {right.items}"
                        ),
                    )
                )
            if len(left.blocks) != len(right.blocks):
                findings.append(
                    Finding(
                        code="CODE_BLOCK_DRIFT",
                        severity="medium",
                        pair=pair,
                        detail=(
                            f"“{left.title}”: {len(left.blocks)} code block(s) in "
                            f"`{base.name}`, {len(right.blocks)} in `{other.name}`"
                        ),
                    )
                )
            else:
                for index, (lb, rb) in enumerate(
                    zip(left.blocks, right.blocks, strict=True), 1
                ):
                    # Only fences that declare a language are compared. An
                    # untagged block is as often a directory tree or a sample
                    # report — prose in a monospace font, which a translation is
                    # supposed to translate. `” ```bash ”` is a command, and a
                    # German README that installs a renamed script is wrong in a
                    # way no reader can see from the German alone.
                    if not lb[0] or not rb[0]:
                        continue
                    if lb[1] != rb[1]:
                        findings.append(
                            Finding(
                                code="CODE_BLOCK_CONTENT_DRIFT",
                                severity="high",
                                pair=pair,
                                detail=(
                                    f"“{left.title}” block {index} (```{lb[0]}): the commands "
                                    f"differ (comments excluded). `{base.name}`: "
                                    f"{_first_diff(lb[1], rb[1])}"
                                ),
                            )
                        )

    findings.extend(link_findings(base, other, pair))
    return findings


def skeleton_findings(base: DocView, other: DocView, pair: str) -> list[Finding]:
    """Where the two heading sequences part company, with both titles.

    A first-divergence report rather than a diff: after one missing section
    every later position is shifted, and printing forty consequential
    mismatches buries the one that has to be fixed.
    """
    findings = [
        Finding(
            code="SECTION_COUNT_DRIFT",
            severity="high",
            pair=pair,
            detail=(
                f"`{base.name}` has {len(base.sections) - 1} heading(s), "
                f"`{other.name}` has {len(other.sections) - 1}"
            ),
        )
    ]
    for index in range(max(len(base.sections), len(other.sections))):
        left = base.sections[index] if index < len(base.sections) else None
        right = other.sections[index] if index < len(other.sections) else None
        if left is not None and right is not None and left.level == right.level:
            continue
        findings.append(
            Finding(
                code="SECTION_MISMATCH",
                severity="high",
                pair=pair,
                detail=(
                    f"the heading sequences diverge at position {index}: "
                    f"`{base.name}` has "
                    f"{_describe(left)}, `{other.name}` has {_describe(right)}. "
                    "Everything after this position is shifted and is not reported "
                    "separately — fix this one and re-run"
                ),
            )
        )
        break
    return findings


def _describe(section: Section | None) -> str:
    if section is None:
        return "nothing (the document ends)"
    return f"“{'#' * section.level} {section.title}” (line {section.line})"


def _first_diff(left: str, right: str) -> str:
    # NOT strict: the two blocks differ, and their lengths may too —
    # that case is what the fallback below reports.
    for a, b in zip(left.splitlines(), right.splitlines(), strict=False):
        if a != b:
            return f"`{a}` vs `{b}`"
    longer = left.splitlines() if len(left) > len(right) else right.splitlines()
    shorter = right.splitlines() if len(left) > len(right) else left.splitlines()
    return f"one side has {len(longer) - len(shorter)} extra line(s): `{longer[-1]}`"


def link_findings(base: DocView, other: DocView, pair: str) -> list[Finding]:
    """Link targets that exist on one side only.

    The cross-language link is excluded in both directions: it is the one link
    that is supposed to differ, and reporting it would put a permanent finding
    on every correctly translated repository in the portfolio.
    """
    cross = {base.name, other.name, f"./{base.name}", f"./{other.name}"}
    left = {ln for ln in base.links if ln not in cross}
    right = {ln for ln in other.links if ln not in cross}
    findings = []
    for missing in sorted(left - right):
        findings.append(
            Finding(
                code="LINK_DRIFT",
                severity="low",
                pair=pair,
                detail=f"`{base.name}` links to {missing}; `{other.name}` does not",
            )
        )
    for extra in sorted(right - left):
        findings.append(
            Finding(
                code="LINK_DRIFT",
                severity="low",
                pair=pair,
                detail=f"`{other.name}` links to {extra}; `{base.name}` does not",
            )
        )
    return findings


# --------------------------------------------------------------------------
# Translation lag, from git
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def translation_lag(root: Path, base: str, translation: str) -> tuple[bool, list[str]]:
    """(measured, commit subjects that touched the base after the translation).

    Zero lag is what good practice produces on its own: a change that updates
    both files in one commit leaves nothing after it. The finding therefore
    fires only where the two genuinely came apart in time.
    """
    last = _git(root, "log", "-1", "--format=%H", "--", translation)
    if last is None:
        return False, []
    if not last:
        # The translation is untracked or brand new — every commit that touched
        # the base predates it in the log, and reporting all of them as lag
        # would be noise, not a finding.
        return True, []
    since = _git(root, "log", f"{last}..HEAD", "--format=%h %s", "--", base)
    if since is None:
        return False, []
    return True, [line for line in since.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Discovery and the run
# --------------------------------------------------------------------------


def discover(target: Path, lang: str) -> list[tuple[Path, Path]]:
    """Every ``X.md`` / ``X.<lang>.md`` pair in the checkout root.

    Root only: a translated document deep in ``docs/`` is a different editorial
    commitment from the two files every reader lands on, and sweeping the whole
    tree would turn one drifting appendix into a red gate for the repository.
    """
    pairs = []
    for translated in sorted(target.glob(f"*.{lang}.md")):
        base = target / (translated.name[: -len(f".{lang}.md")] + ".md")
        if base.is_file():
            pairs.append((base, translated))
    return pairs


def run(
    target: Path, lang: str = DEFAULT_LANG, explicit: tuple[tuple[Path, Path], ...] = ()
) -> Report:
    report = Report(target=str(target))
    if not target.is_dir():
        report.harness_error = f"{target} is not a directory"
        return report

    pairs = list(explicit) or discover(target, lang)
    if not pairs:
        report.notes.append(
            f"no *.{lang}.md beside a *.md in {target} — nothing was measured. This "
            "run is not evidence that the documentation is bilingual, nor that it "
            "is in sync"
        )
        return report

    for base_path, other_path in pairs:
        try:
            base, other = parse(base_path), parse(other_path)
        except OSError as exc:
            report.harness_error = f"{base_path.name}/{other_path.name}: {exc}"
            return report
        result = PairResult(
            base=base_path.name,
            translation=other_path.name,
            base_sections=len(base.sections) - 1,
            translation_sections=len(other.sections) - 1,
        )
        pair_name = result.name
        report.findings.extend(compare(base, other, pair_name))

        measured, lag = translation_lag(target, base_path.name, other_path.name)
        result.lag_measured = measured
        result.lag = lag
        if not measured:
            report.notes.append(
                f"{pair_name}: git could not be read, so the translation-lag check did "
                "not run. The structural findings stand; freshness was not measured"
            )
        elif lag:
            report.findings.append(
                Finding(
                    code="TRANSLATION_LAG",
                    severity="medium",
                    pair=pair_name,
                    detail=(
                        f"{len(lag)} commit(s) touched `{base_path.name}` after the last "
                        f"one that touched `{other_path.name}`: "
                        + "; ".join(lag[:5])
                        + (" …" if len(lag) > 5 else "")
                        + ". The structure can still match while a paragraph says "
                        "something the other no longer does"
                    ),
                )
            )
        report.pairs.append(result)

    return report


def render(report: Report) -> str:
    lines = [f"parity probe — {report.target}"]
    if report.provenance is not None:
        lines.append(f"  {report.provenance.render()}")
        if report.provenance.blocking:
            lines.append(f"  {report.provenance.moved_detail()}")
            return "\n".join(lines)
    if report.harness_error:
        lines.append(f"  HARNESS: {report.harness_error}")
        return "\n".join(lines)
    if not report.pairs:
        lines.append(f"  NOT MEASURED: {report.notes[0]}")
        return "\n".join(lines)
    for pair in report.pairs:
        lines.append(
            f"  {pair.name}: {pair.base_sections} vs {pair.translation_sections} "
            f"heading(s), lag {'not measured' if not pair.lag_measured else len(pair.lag)}"
        )
    for note in report.notes:
        lines.append(f"  note: {note}")
    if not report.findings:
        lines.append("  every pair is structurally parallel")
    for finding in report.findings:
        lines.append(f"  {finding.code} [{finding.severity}] {finding.detail}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=".", help="path to the checkout")
    parser.add_argument(
        "--lang",
        default=DEFAULT_LANG,
        help=f"translation suffix to pair up (default: {DEFAULT_LANG}, "
        f"i.e. README.md ↔ README.{DEFAULT_LANG}.md)",
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="BASE:TRANSLATION",
        help="an explicit pair, repeatable; overrides discovery",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--report", default="", help="also write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()

    explicit: list[tuple[Path, Path]] = []
    for spec in args.pair:
        if ":" not in spec:
            print(
                f"parity: --pair takes BASE:TRANSLATION, got {spec!r}", file=sys.stderr
            )
            return EXIT_CANNOT_RUN
        left, right = spec.split(":", 1)
        explicit.append((target / left, target / right))
    for left, right in explicit:
        if not left.is_file() or not right.is_file():
            print(
                f"parity: {left.name} or {right.name} does not exist", file=sys.stderr
            )
            return EXIT_CANNOT_RUN

    prov = probe_provenance.capture(target)
    report = run(target, lang=args.lang, explicit=tuple(explicit))
    report.provenance = prov.recheck()

    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
