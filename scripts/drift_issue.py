#!/usr/bin/env python3
"""Open, update and CLOSE a scheduled guard's tracking issue — three states.

A guard that runs only on `schedule` has no natural addressee. Nothing about a
red weekly run reaches a pull request, and the job summary is seen by whoever
opens the run — which, for a cron, is nobody. The remedy the portfolio settled
on is a tracking issue: one per finding class, deduped by a hidden marker.

`live-probe.yml.template` had that half. It opened the issue and commented on
it, and it never closed it. An issue that only ever grows is read as noise
within a few weeks, and the response to noise is to switch the guard off — so
the missing close is not cosmetic, it is what decides whether the guard
survives. Closing is the part that keeps it credible.

THE THIRD STATE IS THE WHOLE POINT
----------------------------------
Adding a close path to a two-valued classification is how you get a guard that
lies. "No finding was produced" and "everything is in order" are not the same
sentence, and only the second may close an issue:

    finding   a probe ran and reported something      → open or update
    clear     every probe ran and reported nothing     → close
    unknown   at least one probe did not run at all    → TOUCH NOTHING

`unknown` covers the probe that crashed, the step that was skipped because its
install failed, the report file that is missing or empty, the run that never
reached the comparison. Fold it into `clear` and the guard closes an open issue
on the strength of a comparison that never happened — the finding is still
there, the ticket is gone, and the close is evidence that it was fixed. That is
strictly worse than the missing close it was meant to repair.

`unknown` is deliberately inert in BOTH directions: it opens nothing either. A
probe that cannot run is a deployment problem, and a tracking issue that
reappears every week because a dependency will not install is the same noise by
another route. It is reported on stdout and in `$GITHUB_OUTPUT`, where the job
summary and the run log can see it.

The predictable consequence — an open issue stays open for as long as the
probes cannot run — is intended. The guard does not claim a finding is gone
until something actually looked.

WHY THIS IS A SCRIPT AND NOT A HEREDOC
--------------------------------------
The logic it replaces was an inline `actions/github-script` block. Inline logic
in YAML runs only inside a workflow, so it cannot be unit-tested, and a
classification that cannot be tested cannot be mutation-tested — which is
exactly where the two-state error sat, unexamined, for the life of the file.
`classify()` and `plan()` here are pure functions over data; `tests/
test_drift_issue.py` drives them without a network, a filesystem or a token.

Reusable by design: the marker, label, title and probe list all come from the
command line, so a second scheduled guard needs no second copy of this.

Stdlib only (urllib), matching scripts/live_probe.py and
scripts/sync_findings_issues.py. Reads the token from the environment and never
logs it. Never fails the cron on a finding: findings are the issue's job, and a
red cron with nobody watching is the problem this file exists to solve.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

_API = "https://api.github.com"

# The three states. Named rather than stringly-typed at the call sites so a typo
# is a NameError instead of a silently-never-taken branch.
FINDING = "finding"
CLEAR = "clear"
UNKNOWN = "unknown"

# GitHub step outcomes that mean the step actually executed and completed. Every
# other value — `failure`, `cancelled`, `skipped`, or the empty string a step
# that never started reports — means no comparison was made.
_RAN_OUTCOMES = frozenset({"success"})


class Probe(NamedTuple):
    """One check's self-report, as the workflow observed it.

    `outcome` is the GitHub step outcome. `alert` is the `alert=true|false` the
    probe appended to `$GITHUB_OUTPUT`; the empty string means it never got that
    far. `report_ok` is whether the probe's Markdown report exists and has
    content — a probe that wrote no report did not finish, whatever its exit
    code claims.
    """

    name: str
    outcome: str
    alert: str
    report_ok: bool


def probe_ran(probe: Probe) -> bool:
    """Pure: did this probe actually perform its comparison?

    All three signals must agree. Any one of them alone is forgeable by a
    failure mode this guard has already seen: a step can exit 0 without writing
    a report, and a crashed step leaves `alert` empty while the workflow still
    evaluates the expression around it.
    """
    return (
        probe.outcome in _RAN_OUTCOMES
        and probe.alert in ("true", "false")
        and probe.report_ok
    )


def classify(probes: list[Probe]) -> str:
    """Pure: FINDING / CLEAR / UNKNOWN over the probes of one run.

    Order of the tests is load-bearing. A probe that alerted decides the run
    even when a sibling never ran: the finding is real and reported, and holding
    it back because the run was also incomplete would lose it. Only the
    all-clear needs every probe present, because only the all-clear closes.

    No probes at all is UNKNOWN, not CLEAR — an empty list is the absence of
    evidence, and the caller that passes one has a bug rather than a green run.
    """
    if not probes:
        return UNKNOWN
    if any(probe_ran(p) and p.alert == "true" for p in probes):
        return FINDING
    if all(probe_ran(p) for p in probes):
        return CLEAR
    return UNKNOWN


def marker_for(token: str) -> str:
    """Hidden HTML marker that identifies this guard's issue in a body."""
    return f"<!-- {token} -->"


def find_tracked(open_issues: list[dict[str, Any]], marker: str) -> int | None:
    """Pure: number of the first open issue carrying the marker, else None.

    Matching on the marker rather than the title is what keeps the guard off
    issues a human opened under the same label.
    """
    for issue in open_issues:
        if marker in (issue.get("body") or ""):
            return issue.get("number")
    return None


def plan(
    state: str, open_issues: list[dict[str, Any]], marker: str
) -> tuple[str, int | None]:
    """Pure: `(action, issue_number)` for a state and the open issues.

    Actions: `create`, `comment`, `close`, `noop`.

    UNKNOWN returns `noop` before anything else is consulted — no lookup, no
    number, nothing that a later edit could accidentally act on.
    """
    if state == UNKNOWN:
        return "noop", None
    tracked = find_tracked(open_issues, marker)
    if state == FINDING:
        return ("comment", tracked) if tracked is not None else ("create", None)
    if state == CLEAR:
        return ("close", tracked) if tracked is not None else ("noop", None)
    raise ValueError(f"unknown state {state!r}")


def parse_probe(spec: str, root: Path) -> Probe:
    """`NAME:OUTCOME:ALERT[:REPORT]` -> Probe. Report path may contain colons.

    A probe declared without a report path is judged on outcome and alert alone;
    pass the path whenever there is one, since it is the signal the other two
    cannot fake.
    """
    parts = spec.split(":", 3)
    if len(parts) < 3:
        raise ValueError(f"--probe needs NAME:OUTCOME:ALERT[:REPORT], got {spec!r}")
    name, outcome, alert = parts[0], parts[1], parts[2]
    report = parts[3] if len(parts) == 4 else ""
    if not report:
        # No report declared: outcome and alert carry the decision on their own.
        return Probe(name, outcome, alert, report_ok=True)
    path = root / report
    try:
        report_ok = bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        report_ok = False
    return Probe(name, outcome, alert, report_ok=report_ok)


# --- thin GitHub REST layer (urllib) ----------------------------------------


def _req(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "mcp-continuous-auditor")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - fixed api.github.com host
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _ensure_label(repo: str, label: str, color: str, token: str) -> None:
    try:
        _req("GET", f"{_API}/repos/{repo}/labels/{label}", token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        _req(
            "POST",
            f"{_API}/repos/{repo}/labels",
            token,
            {
                "name": label,
                "color": color,
                "description": "Opened by a scheduled guard (deterministic routing)",
            },
        )


def _open_issues(repo: str, label: str, token: str) -> list[dict[str, Any]]:
    url = f"{_API}/repos/{repo}/issues?state=open&labels={label}&per_page=100"
    result = _req("GET", url, token)
    return result if isinstance(result, list) else []


def execute(
    action: str,
    number: int | None,
    *,
    repo: str,
    label: str,
    title: str,
    marker: str,
    body: str,
    token: str,
) -> str:
    """Carry out one planned action. Returns a one-line log message."""
    if action == "noop":
        return "nothing to do"
    if action == "create":
        created = _req(
            "POST",
            f"{_API}/repos/{repo}/issues",
            token,
            {"title": title, "body": f"{marker}\n\n{body}", "labels": [label]},
        )
        return f"opened #{created.get('number')} ({label})"
    if action == "comment":
        _req(
            "POST",
            f"{_API}/repos/{repo}/issues/{number}/comments",
            token,
            {"body": f"Still present on the latest run:\n\n{body}"},
        )
        return f"commented on #{number} ({label})"
    if action == "close":
        # Comment first, then close. If the close call fails the issue keeps an
        # explanation of why it was about to be closed; closing first and failing
        # to comment would leave a closed issue with no reason in it.
        _req(
            "POST",
            f"{_API}/repos/{repo}/issues/{number}/comments",
            token,
            {
                "body": (
                    "Every probe ran on the latest run and none reported a "
                    "finding — closing.\n\n"
                    "Reopened automatically if it comes back.\n\n"
                    f"{body}"
                )
            },
        )
        _req(
            "PATCH",
            f"{_API}/repos/{repo}/issues/{number}",
            token,
            {"state": "closed", "state_reason": "completed"},
        )
        return f"closed #{number} ({label})"
    raise ValueError(f"unknown action {action!r}")


def _emit_output(state: str, action: str) -> None:
    """Publish the classification so the job summary and log can see it.

    `unknown` is the state that must not be quiet: it is the one where the guard
    deliberately does nothing, and a silent do-nothing is indistinguishable from
    a pass — the failure mode this whole file is about.
    """
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    try:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"state={state}\n")
            fh.write(f"action={action}\n")
    except OSError as exc:  # pragma: no cover - CI filesystem only
        print(f"warning: could not write GITHUB_OUTPUT: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Three-state tracking-issue routing.")
    p.add_argument("--repo", default="", help="owner/repo (default $GITHUB_REPOSITORY)")
    p.add_argument("--marker", required=True, help="dedup token, e.g. live-probe-drift")
    p.add_argument("--label", required=True, help="issue label")
    p.add_argument("--label-color", default="b60205", help="colour if it must be made")
    p.add_argument("--title", required=True, help="issue title when opening")
    p.add_argument("--body-file", required=True, help="Markdown body / comment")
    p.add_argument(
        "--probe",
        action="append",
        default=[],
        metavar="NAME:OUTCOME:ALERT[:REPORT]",
        help="one per check; repeatable. Every probe must have run to close.",
    )
    p.add_argument("--token-env", default="GITHUB_TOKEN", help="env var with the PAT")
    p.add_argument(
        "--dry-run", action="store_true", help="print the plan, touch nothing"
    )
    args = p.parse_args(argv)

    root = Path.cwd()
    try:
        probes = [parse_probe(spec, root) for spec in args.probe]
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    state = classify(probes)
    for probe in probes:
        ran = "ran" if probe_ran(probe) else "DID NOT RUN"
        print(
            f"  probe {probe.name}: {ran} "
            f"(outcome={probe.outcome or '-'}, alert={probe.alert or '-'}, "
            f"report={'ok' if probe.report_ok else 'missing/empty'})"
        )
    print(f"state: {state}")
    if state == UNKNOWN:
        print(
            "At least one probe did not run, so this run compared nothing. "
            "Leaving any tracking issue exactly as it is — neither opening nor "
            "closing on evidence that was never gathered."
        )

    marker = marker_for(args.marker)
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if state != UNKNOWN and "/" not in repo:
        print(f"FATAL: no valid repo (got {repo!r})", file=sys.stderr)
        return 2

    token = os.environ.get(args.token_env, "")
    if state != UNKNOWN and not token and not args.dry_run:
        print(
            f"FATAL: {args.token_env} not set (issues:write required)", file=sys.stderr
        )
        return 2

    body = ""
    if state != UNKNOWN:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8").strip()
        except OSError:
            body = "(report body unavailable)"

    # A dry run still LOOKS: listing open issues is read-only, and without it the
    # plan is guesswork. `close` and `comment` only ever appear in a plan that saw
    # the issue list, so a dry run that skipped the read would report `create` for
    # a run that will comment and `noop` for one that will close — the two actions
    # anyone dry-runs this to check.
    existing: list[dict[str, Any]] = []
    if state != UNKNOWN:
        if args.dry_run and not token:
            print(
                "[dry-run] no token: planning against an empty issue list, so "
                "`comment` and `close` cannot appear below."
            )
        else:
            try:
                if not args.dry_run:
                    _ensure_label(repo, args.label, args.label_color, token)
                existing = _open_issues(repo, args.label, token)
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                # A real run must stop: planning against an issue list it failed
                # to read would open a duplicate of an issue that is already
                # there. A dry run only wanted the list to print a better plan,
                # so it says what it lost and carries on.
                if not args.dry_run:
                    print(f"ERROR: cannot read open issues: {exc}", file=sys.stderr)
                    return 1
                print(f"[dry-run] could not read open issues ({exc}); ", end="")
                print("planning against an empty list.")

    action, number = plan(state, existing, marker)
    _emit_output(state, action)

    if args.dry_run:
        target = f" #{number}" if number else ""
        print(f"[dry-run] {action}{target} for label {args.label!r}")
        return 0

    try:
        print(
            execute(
                action,
                number,
                repo=repo,
                label=args.label,
                title=args.title,
                marker=marker,
                body=body,
                token=token,
            )
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"ERROR: {action} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
