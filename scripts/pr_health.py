#!/usr/bin/env python3
"""Find open pull requests that are not green — including the ones that look fine.

THE INCIDENT
------------
``mcp-continuous-auditor#50`` and ``#58`` both sat at ``mergeable_state:
dirty``. That is not "there will be conflicts when you merge": GitHub cannot
build a merge ref for such a pull request, so it starts **no workflows at all**.
Both pull requests had zero check runs. Neither showed a red mark, because
nothing had run to be red. A glance at "no failing checks" saw the same picture
it sees on a green pull request.

That is ``OPS-005`` ("skipped is not passed") one level up: not a skipped test,
a skipped pipeline. And it is the same shape as every other finding in this
repo — **absence of a report gets read as absence of a problem.**

WHAT THIS REPORTS
-----------------
``unbuildable``   ``mergeable_state`` is outside the buildable allow-list.
                  GitHub cannot build the merge ref, so CI cannot run.
``no_checks``     No check run at all on the head commit, and that commit is
                  older than ``--grace-minutes``. Below the grace period this
                  is indistinguishable from "the workflows are about to start",
                  and reporting it would make every fresh push a finding.

Every finding carries what was observed — ``mergeable_state``, head SHA, number
of check runs, age of the head commit. Without that a reader has to go and look
anyway, and a report you have to verify by hand is a report nobody reads. That
lesson cost a portfolio sweep a full round: 38 identically worded findings, none
of which said what the server had actually done.

TARGETS
-------
From the coverage manifest (``coverage_manifest.py --format json`` in the
portfolio repo), never from a list in this file. A hand-maintained target list
drifts exactly the way a hand-maintained version number drifts, and for the same
reason: nothing downstream disagrees with it. Archived repositories are skipped
**by name and with a reason** — they are read-only, so an open pull request
there is stuck by definition and a finding about it is noise.

Usage:
    python scripts/pr_health.py --manifest manifest.json
    python scripts/pr_health.py --manifest manifest.json --format json
    python scripts/pr_health.py --manifest manifest.json --allow-skip repo:grund

stdlib only (urllib), matching scripts/sync_findings_issues.py. Reads
GITHUB_TOKEN from the environment; the token is never logged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import coverage  # noqa: E402

_API = "https://api.github.com"

# States in which GitHub CAN build a merge ref, so workflows do run:
#   clean     mergeable, checks green
#   unstable  mergeable, a non-required check failed or is pending
#   blocked   mergeable, waiting on a required review or check
#   behind    mergeable, head is behind base (strict branch protection)
#   draft     documented value for a draft pull request
# Everything else — `dirty` and any value GitHub adds later — means no merge
# ref and therefore no CI. An allow-list rather than a deny-list on purpose: a
# future unbuildable state should surface, not pass silently.
#
# `draft` is in the list because a draft pull request DOES run workflows.
# Measured against live pull requests: swiss-public-data-mcp#31 and #32 both ran
# their full CI while still drafts. Nowadays the API reports their state as
# `clean`/`unstable` rather than `draft` at all — the value is kept here because
# it is documented, and guessing wrong in this direction would make every draft
# a finding, which is how a check stops being read.
BUILDABLE = frozenset({"clean", "unstable", "blocked", "behind", "draft"})

# `mergeable_state` is computed lazily. The first read after a push often
# returns "unknown"; treating that as unbuildable would report a finding for
# every fresh pull request. Ask again instead of guessing.
UNKNOWN = "unknown"


@dataclass
class Finding:
    repo: str
    number: int
    status: str
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        ev = ", ".join(f"{k}={v}" for k, v in self.evidence.items())
        return f"{self.repo}#{self.number} [{self.status}] {self.title[:60]} — {ev}"


ARCHIVED = "archiviert (read-only, PRs dort sind ohnehin fest)"


def read_manifest(
    path: Path,
) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]]]:
    """Split the manifest into a total, targets, and justified omissions.

    The ``repositories``-shaped view of ``scripts/coverage.py``: that module
    owns the validation and the denominator, this function owns what a
    repository entry means. Both used to live here in full — and, separately,
    in ``published_probe.py``. One rule in two implementations is two chances
    to get the denominator wrong, and it had already gone wrong once (an
    all-green run exited 1 because ``2 swept + 1 skipped`` was compared against
    an expected 2).

    Archived repositories are the manifest's own justified omission: they are
    read-only, so an open pull request there is stuck by definition.
    """
    total, targets, omissions = coverage.read_manifest(
        path,
        field="repository",
        section=coverage.REPOSITORIES,
        value_of=coverage.github_slug(path),
        omit_when=lambda raw: ARCHIVED if raw.get("archived") else None,
    )
    return (
        total,
        [(e.value, e.id) for e in targets],
        [(o.name, o.reason) for o in omissions],
    )


def parse_allow_skip(values: list[str]) -> dict[str, str]:
    """``repo:grund`` — the reason is mandatory.

    A skip without a reason is not a skip, it is a gap with an alibi.
    Shared: ``scripts/coverage.py``.
    """
    return coverage.parse_allow_skip(values, noun="repo")


# --- thin GitHub REST layer (urllib) ----------------------------------------


def _get(url: str, token: str) -> Any:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "mcp-continuous-auditor/pr-health")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - fixed api.github.com host
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def open_pulls(repo: str, token: str) -> list[dict[str, Any]]:
    url = f"{_API}/repos/{repo}/pulls?state=open&per_page=100"
    result = _get(url, token)
    return result if isinstance(result, list) else []


def pull_detail(repo: str, number: int, token: str) -> dict[str, Any]:
    """The list endpoint omits ``mergeable_state``; only the single-PR one has it."""
    return _get(f"{_API}/repos/{repo}/pulls/{number}", token)


def check_run_count(repo: str, sha: str, token: str) -> int:
    ref = urllib.parse.quote(sha, safe="")
    data = _get(f"{_API}/repos/{repo}/commits/{ref}/check-runs?per_page=1", token)
    return int(data.get("total_count", 0)) if isinstance(data, dict) else 0


def commit_age_minutes(repo: str, sha: str, token: str, now: dt.datetime) -> float:
    data = _get(
        f"{_API}/repos/{repo}/commits/{urllib.parse.quote(sha, safe='')}", token
    )
    when = data.get("commit", {}).get("committer", {}).get("date")
    if not when:
        # No timestamp is not "brand new". Treating it as fresh would suppress
        # the finding; treating it as old surfaces it for a human to dismiss.
        return float("inf")
    stamp = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    return (now - stamp).total_seconds() / 60.0


def classify(
    pr: dict[str, Any],
    checks: int,
    age_minutes: float,
    grace_minutes: float,
) -> str | None:
    """``unbuildable`` / ``no_checks`` / None. Pure — the tests drive this."""
    state = pr.get("mergeable_state")
    if state is not None and state != UNKNOWN and state not in BUILDABLE:
        return "unbuildable"
    if checks == 0 and age_minutes >= grace_minutes:
        return "no_checks"
    return None


def inspect(repo: str, token: str, grace: float, now: dt.datetime) -> list[Finding]:
    findings: list[Finding] = []
    for stub in open_pulls(repo, token):
        number = stub["number"]
        pr = pull_detail(repo, number, token)
        if pr.get("mergeable_state") == UNKNOWN:
            # Lazily computed; the first read after a push is often "unknown".
            pr = pull_detail(repo, number, token)
        sha = pr.get("head", {}).get("sha", "")
        checks = check_run_count(repo, sha, token) if sha else 0
        age = commit_age_minutes(repo, sha, token, now) if sha else float("inf")
        status = classify(pr, checks, age, grace)
        if status:
            findings.append(
                Finding(
                    repo=repo,
                    number=number,
                    status=status,
                    title=pr.get("title", ""),
                    evidence={
                        "mergeable_state": pr.get("mergeable_state"),
                        "draft": pr.get("draft"),
                        "head": sha[:7],
                        "check_runs": checks,
                        "head_age_min": round(age)
                        if age != float("inf")
                        else "unbekannt",
                    },
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pr_health")
    p.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="coverage_manifest.py --format json",
    )
    p.add_argument(
        "--token-env", default="GITHUB_TOKEN", help="env var holding the PAT"
    )
    p.add_argument(
        "--grace-minutes",
        type=float,
        default=10.0,
        help="unterhalb dieses Alters ist 'keine Checks' nicht von 'startet gleich' zu trennen",
    )
    p.add_argument("--allow-skip", action="append", default=[], metavar="REPO:GRUND")
    p.add_argument("--format", choices=("text", "json"), default="text")
    args = p.parse_args(argv)

    token = os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(f"{args.token_env} ist nicht gesetzt")

    total, entries, omissions = coverage.read_manifest(
        args.manifest,
        field="repository",
        section=coverage.REPOSITORIES,
        value_of=coverage.github_slug(args.manifest),
        omit_when=lambda raw: ARCHIVED if raw.get("archived") else None,
    )
    allowed = parse_allow_skip(args.allow_skip)
    # Ein `--allow-skip` auf einen Namen, den das Manifest nicht kennt, bewirkt
    # nichts und sieht wie eine Entscheidung aus. Der gefaehrliche Fall ist der
    # umbenannte Eintrag: der Operator glaubt, ein Ziel auszulassen, waehrend
    # ein anderes unbemerkt dazukam.
    stray = coverage.unknown_skips(allowed, entries, omissions)
    if stray:
        raise SystemExit(
            "--allow-skip nennt Namen, die im Manifest nicht vorkommen: "
            + ", ".join(stray)
            + ". Ein Skip, der nichts ueberspringt, ist keine Entscheidung"
        )
    kept, by_flag = coverage.split_allowed(entries, allowed)
    skipped = [(o.name, o.reason) for o in (*omissions, *by_flag)]
    swept = [(e.value, e.id) for e in kept]

    now = dt.datetime.now(dt.UTC)
    findings: list[Finding] = []
    errors: list[tuple[str, str]] = []
    for slug, _sid in swept:
        try:
            findings += inspect(slug, token, args.grace_minutes, now)
        except urllib.error.HTTPError as e:
            errors.append((slug, f"HTTP {e.code}"))
        except Exception as e:  # noqa: BLE001 - ein Repo darf den Sweep nicht abbrechen
            # Ohne das endet ein Lauf beim ersten Ausrutscher und berichtet ueber
            # ein Praefix der Zielliste, als waere es die Liste.
            errors.append((slug, f"{type(e).__name__}: {e}"))

    # Ein Repo, das einen Fehler geworfen hat, steht in `swept` UND in `errors`.
    # Beide zu addieren zaehlt es doppelt — und meldet dann "2 von 1", also
    # mehr Deckung als es Ziele gibt. Genau die Sorte Nenner-Fehler, gegen die
    # dieser Block gebaut ist; einmal ausgeliefert habe ich sie schon.
    unreached = {r for r, _ in errors}
    measured = [slug for slug, _ in swept if slug not in unreached]
    # Ein Repo ohne Ergebnis waere ein stiller Ausfall — genau die Sorte, die
    # aussieht wie ein sauberer Lauf. Fehler zaehlen gegen dieselbe Soll-Zahl,
    # sind aber KEINE Deckung: ein HTTP 404 heisst nicht "dort ist nichts".
    cov = coverage.build(
        total,
        len(measured),
        [coverage.Omission(r, g) for r, g in skipped],
        sorted(unreached),
    )
    coverage_ok = cov.complete

    if args.format == "json":
        json.dump(
            {
                "schema": 1,
                "probe": "pr-health",
                "coverage": {
                    **cov.as_dict(),
                    # `measured`/`ok` bleiben als Alias erhalten: sie stehen in
                    # den Berichten, die dieser Lauf schon geschrieben hat.
                    "measured": len(measured),
                    "ok": coverage_ok,
                    "errors": [{"repo": r, "detail": d} for r, d in errors],
                },
                "findings": [f.__dict__ for f in findings],
            },
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
    else:
        for f in findings:
            print(f.line())
        for r, d in errors:
            print(f"{r}: nicht erhoben ({d})", file=sys.stderr)
        print(
            f"\n{len(measured)}/{total} Repos geprueft, {len(skipped)} uebersprungen, "
            f"{len(errors)} nicht erreichbar — {len(findings)} Befunde"
        )
        for r, g in skipped:
            print(f"  uebersprungen: {r} ({g})")
        print(cov.covered())

    if not coverage_ok:
        print(
            f"Deckung unvollstaendig: {cov.covered()} — "
            "'nicht hingesehen' und 'nichts gefunden' sind verschiedene Aussagen",
            file=sys.stderr,
        )
        return coverage.EXIT_INCOMPLETE
    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
