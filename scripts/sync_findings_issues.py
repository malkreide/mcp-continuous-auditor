#!/usr/bin/env python3
"""Deterministic GitHub issue routing for the nightly audit (Analysis U-C).

The nightly cron used to let the OpenClaw agent decide whether to open a new
findings issue or reuse an existing one — LLM judgment in the OUTPUT path of an
otherwise deterministic pipeline, where a misjudgement means a duplicate issue or
a missed dedup. This script does it in code, mirroring the weekly ``live-probe``
job's ``github-script``: one tracking issue per finding class, deduped by a hidden
HTML marker, updated with a comment when it already exists. The agent now only
runs this script and announces the report — the open/update decision is code.

THREE STATES, PER LABEL
-----------------------
It used to run on ``exit == 2`` only, and it only ever opened or commented. A
tracking issue that nothing closes grows until it reads as noise, and the answer
to noise is to switch the guard off — so the missing close decided whether this
routing survived, not how tidy it was.

Closing on ``outcome == "green"``, though, would be wrong here, and the summary
says so itself. ``green`` is deliberately computed WITHOUT the not-measured
flags (``nightly_audit_report.py``: "transport_boot_unmeasured and
lockfile_unmeasured deliberately absent"), so a green run can carry a gate that
never produced a verdict. Two concrete ones:

  * ``graded_layer_ran`` false — the determ-only promptfoo profile ran, so the
    model-graded red-team layer was never exercised. The report already refuses
    to read that as "red-team clear"; closing the ``redteam`` issue on it would
    be the same claim by another route, and a closed issue reads as *fixed*.
  * ``host_allowlist_unconfigured`` — the inbound allow-list is fail-open. A
    documented deployment state, not a defect, and not a measurement either.

So each label gets the three answers this repository already applies to every
probe report (``docs/probes/README.md``), rather than the two the delivery had:

    finding   the class's boolean is set                → open or update
    clear     the run concluded AND this class's
              evidence was actually gathered            → close
    unknown   hard-fail, or this class was not measured → touch nothing

Contract with the cron flow: run it on ``exit == 0`` (green, so findings that
went away are closed) and on ``exit == 2`` (findings). A hard-fail classifies
every label ``unknown``, so wiring it there too is safe but pointless — the run
produced no verdict to route. It never opens a PR — that stays the human-gated
path (AGENTS.md).

stdlib only (urllib) — matches scripts/live_probe.py. Reads GITHUB_TOKEN (a
fine-grained PAT with issues:write) from the environment; the token is never
logged. All inputs are untrusted; the report body was already control-char
stripped at the sink (nightly_audit_report.py, Analysis S-D).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_API = "https://api.github.com"

# The three answers, one per label. Named so a typo is a NameError rather than a
# branch that is silently never taken.
FINDING = "finding"
CLEAR = "clear"
UNKNOWN = "unknown"

# Finding class (summary boolean) -> (label, title). Order is stable and
# deterministic. schema-drift + redteam mirror the live-probe labels; the newer
# other/toolchain classes (Iteration 1) share a generic label.
_CLASSES: list[tuple[str, str, str]] = [
    ("schema_drift", "schema-drift", "[nightly] Schema drift detected"),
    ("redteam", "redteam", "[nightly] Red-team hit"),
    ("other_findings", "audit-finding", "[nightly] Audit finding (uncategorised)"),
    ("toolchain_fail", "audit-finding", "[nightly] Toolchain failure"),
    # The process-level gates. Without an entry here a run whose ONLY finding is
    # one of these classifies as `findings` and then routes to no issue at all —
    # the finding exists in the summary and nothing is ever opened for it.
    ("transport_boot_fail", "audit-finding", "[nightly] Transport boot failure"),
    ("host_allowlist_fail", "dns-rebinding", "[nightly] DNS-rebinding control failed"),
    # Same defect, found again: both of these move `outcome` to `findings` in
    # nightly_audit_report.py and had no entry here, so a run whose only red gate
    # was the shipped-artifact or lockfile check opened nothing at all. Adding a
    # gate to the summary and adding it here are one change, not two;
    # test_every_finding_key_in_the_summary_is_routed holds them together now.
    ("shipped_artifact_fail", "audit-finding", "[nightly] Shipped-artifact gate red"),
    ("lockfile_fail", "audit-finding", "[nightly] Lockfile disagrees with pyproject"),
]

# Per label: what the summary must say for this class to count as MEASURED, as
# `(key, required truthiness)`. Fail any pair and the label is `unknown` — it can
# still be opened by its own boolean, but it can never be CLOSED, because closing
# asserts that somebody looked.
#
# `green` in the summary is computed without the not-measured flags on purpose,
# so it cannot answer this question; these keys are how the summary answers it.
_EVIDENCE: dict[str, tuple[tuple[str, bool], ...]] = {
    # A determ-only profile never ran the model-graded red-team layer. The report
    # refuses to call that "red-team clear"; a close would say it more loudly.
    # Absent key -> falsy -> unmeasured, which is the safe direction for a
    # summary too old to carry the flag.
    "redteam": (("graded_layer_ran", True),),
    "audit-finding": (
        ("transport_boot_unmeasured", False),
        ("lockfile_unmeasured", False),
    ),
    # Fail-open is a deployment state, not a measurement of the control.
    "dns-rebinding": (("host_allowlist_unconfigured", False),),
}

_LABEL_COLORS = {
    "schema-drift": "b60205",
    "redteam": "d93f0b",
    "audit-finding": "fbca04",
    "dns-rebinding": "5319e7",
}


def _marker(label: str) -> str:
    """Hidden dedup marker embedded in each tracking issue's body."""
    return f"<!-- nightly-audit:{label} -->"


def label_measured(label: str, summary: dict[str, Any]) -> bool:
    """Pure: did this run actually gather evidence for `label`?

    Only ever consulted to decide whether a label may be CLOSED. A label with no
    entry in `_EVIDENCE` is measured whenever the run concluded — there is no
    partial state for it to be in.
    """
    return all(bool(summary.get(key)) == want for key, want in _EVIDENCE.get(label, ()))


def label_states(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Pure: one entry per distinct label, each carrying its state.

    A hard-fail — or any outcome this script does not recognise — makes every
    label `unknown`: the audit did not complete, so nothing it did not say can be
    read as an all-clear. On a `findings` run the labels that are NOT among the
    findings are ordinary clears; that is the case that closes a schema-drift
    issue in the same run that a red-team hit stays open.
    """
    outcome = summary.get("outcome")
    concluded = outcome in ("green", "findings")

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, label, title in _CLASSES:
        is_finding = concluded and bool(summary.get(key))
        if label in seen:
            # Several classes share a label (audit-finding covers five). One
            # finding among them keeps the whole label a finding: promote the
            # entry that is already there rather than adding a second issue.
            if is_finding:
                for entry in out:
                    if entry["label"] == label:
                        entry["state"] = FINDING
            continue
        seen.add(label)
        if is_finding:
            state = FINDING
        elif concluded and label_measured(label, summary):
            state = CLEAR
        else:
            state = UNKNOWN
        out.append(
            {
                "key": key,
                "label": label,
                "marker": _marker(label),
                "title": title,
                "state": state,
            }
        )
    return out


def finding_classes(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Pure: the labels this summary reports a FINDING for.

    Kept as the narrow question it always answered. `label_states()` is the wider
    one, and the one `main()` drives — this returns its findings subset.
    """
    return [c for c in label_states(summary) if c["state"] == FINDING]


def decide(open_issues: list[dict[str, Any]], marker: str) -> tuple[str, int | None]:
    """Pure: 'comment' on the first open issue whose body carries the marker, else
    'create'. This is the deterministic dedup the agent used to eyeball."""
    for iss in open_issues:
        if marker in (iss.get("body") or ""):
            return "comment", iss.get("number")
    return "create", None


def plan(
    state: str, open_issues: list[dict[str, Any]], marker: str
) -> tuple[str, int | None]:
    """Pure: `(action, issue_number)`. Actions: create / comment / close / noop.

    UNKNOWN returns before the issue list is consulted at all, so no later edit
    can pick up a number it was never entitled to act on.
    """
    if state == UNKNOWN:
        return "noop", None
    if state == FINDING:
        return decide(open_issues, marker)
    if state == CLEAR:
        for iss in open_issues:
            if marker in (iss.get("body") or ""):
                return "close", iss.get("number")
        return "noop", None
    raise ValueError(f"unknown state {state!r}")


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


def _ensure_label(repo: str, label: str, token: str) -> None:
    try:
        _req("GET", f"{_API}/repos/{repo}/labels/{label}", token)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        _req(
            "POST",
            f"{_API}/repos/{repo}/labels",
            token,
            {
                "name": label,
                "color": _LABEL_COLORS.get(label, "ededed"),
                "description": "Opened by the nightly audit (deterministic routing)",
            },
        )


def _open_issues(repo: str, label: str, token: str) -> list[dict[str, Any]]:
    url = f"{_API}/repos/{repo}/issues?state=open&labels={label}&per_page=100"
    result = _req("GET", url, token)
    return result if isinstance(result, list) else []


def sync(repo: str, cls: dict[str, str], body: str, token: str, dry_run: bool) -> str:
    marker = cls["marker"]
    label = cls["label"]
    state = cls["state"]

    # `unknown` short-circuits before any call, read-only ones included. There is
    # nothing to look up: whatever is open stays exactly as it is.
    if state == UNKNOWN:
        return f"[{label}] not measured this run — left untouched"

    if dry_run:
        # Still LOOK: listing open issues is read-only, and `close` and `comment`
        # only ever appear in a plan that saw the list. A dry run that skipped the
        # read would report `create` for a run that will comment.
        existing = _open_issues(repo, label, token) if token else []
    else:
        _ensure_label(repo, label, token)
        existing = _open_issues(repo, label, token)

    action, number = plan(state, existing, marker)

    if dry_run:
        return f"[dry-run] {action} issue for label '{label}'" + (
            f" #{number}" if number else ""
        )
    if action == "noop":
        return f"[{label}] {state}, nothing open to act on"
    if action == "comment":
        _req(
            "POST",
            f"{_API}/repos/{repo}/issues/{number}/comments",
            token,
            {"body": f"Still present on the latest nightly:\n\n{body}"},
        )
        return f"commented on #{number} ({label})"
    if action == "close":
        # Comment first, then close. A close whose comment failed would leave a
        # closed issue with no reason in it; the other order cannot lose the
        # explanation.
        _req(
            "POST",
            f"{_API}/repos/{repo}/issues/{number}/comments",
            token,
            {
                "body": (
                    "The latest nightly ran this gate and it is clean — closing.\n\n"
                    "Reopened automatically if the finding returns.\n\n"
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
    created = _req(
        "POST",
        f"{_API}/repos/{repo}/issues",
        token,
        {
            "title": cls["title"],
            "body": f"{marker}\n\n{body}",
            "labels": [label],
        },
    )
    return f"opened #{created.get('number')} ({label})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary", required=True, help="path to nightly-summary.json")
    p.add_argument(
        "--report", required=True, help="path to nightly-report.md (issue body)"
    )
    p.add_argument(
        "--target", default="", help="owner/repo (else taken from the summary)"
    )
    p.add_argument(
        "--token-env", default="GITHUB_TOKEN", help="env var holding the PAT"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the plan, touch nothing"
    )
    args = p.parse_args(argv)

    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # A summary that cannot be read is not a green run. Refusing here is what
        # keeps a truncated or absent file from closing every open issue.
        print(f"FATAL: cannot read summary: {e}", file=sys.stderr)
        return 1
    if not isinstance(summary, dict):
        print(
            f"FATAL: summary is not an object: {type(summary).__name__}",
            file=sys.stderr,
        )
        return 1

    classes = label_states(summary)
    outcome = summary.get("outcome")
    for cls in classes:
        print(f"  {cls['label']}: {cls['state']}")
    if all(c["state"] == UNKNOWN for c in classes):
        # Hard-fail, or an outcome this script does not recognise. Nothing was
        # concluded, so nothing is opened and — the part that matters — nothing is
        # closed. Announced rather than silent: a quiet no-op reads like a pass.
        print(
            f"nothing was measured (outcome={outcome!r}) — no issue opened, "
            f"none closed, none touched"
        )
        return 0
    if not any(c["state"] == FINDING for c in classes):
        print(f"no findings (outcome={outcome!r}) — closing what the run cleared")

    repo = args.target or str(summary.get("target") or "")
    if "/" not in repo or repo == "invalid":
        print(f"FATAL: no valid target repo (got {repo!r})", file=sys.stderr)
        return 1

    body = (
        Path(args.report).read_text(encoding="utf-8")
        if Path(args.report).exists()
        else "(nightly report body unavailable)"
    )

    token = os.environ.get(args.token_env, "")
    if not token and not args.dry_run:
        print(
            f"FATAL: {args.token_env} not set (issues:write PAT required)",
            file=sys.stderr,
        )
        return 1

    rc = 0
    for cls in classes:
        try:
            print(sync(repo, cls, body, token, args.dry_run))
        except urllib.error.HTTPError as e:
            print(
                f"ERROR routing '{cls['label']}': HTTP {e.code} {e.reason}",
                file=sys.stderr,
            )
            rc = 1
        except urllib.error.URLError as e:
            print(f"ERROR routing '{cls['label']}': {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
