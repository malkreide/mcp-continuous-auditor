#!/usr/bin/env python3
"""Deterministic GitHub issue routing for the nightly audit (Analysis U-C).

The nightly cron used to let the OpenClaw agent decide whether to open a new
findings issue or reuse an existing one — LLM judgment in the OUTPUT path of an
otherwise deterministic pipeline, where a misjudgement means a duplicate issue or
a missed dedup. This script does it in code, mirroring the weekly ``live-probe``
job's ``github-script``: one tracking issue per finding class, deduped by a hidden
HTML marker, updated with a comment when it already exists. The agent now only
runs this script and announces the report — the open/update decision is code.

Contract with the cron flow: run it on ``exit == 2`` (findings). For green /
hard-fail it is a no-op (green opens nothing; a hard-fail is announced, not
ticketed). It never opens a PR — that stays the human-gated path (AGENTS.md).

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

# Finding class (summary boolean) -> (label, title). Order is stable and
# deterministic. schema-drift + redteam mirror the live-probe labels; the newer
# other/toolchain classes (Iteration 1) share a generic label.
_CLASSES: list[tuple[str, str, str]] = [
    ("schema_drift", "schema-drift", "[nightly] Schema drift detected"),
    ("redteam", "redteam", "[nightly] Red-team hit"),
    ("other_findings", "audit-finding", "[nightly] Audit finding (uncategorised)"),
    ("toolchain_fail", "audit-finding", "[nightly] Toolchain failure"),
]

_LABEL_COLORS = {
    "schema-drift": "b60205",
    "redteam": "d93f0b",
    "audit-finding": "fbca04",
}


def _marker(label: str) -> str:
    """Hidden dedup marker embedded in each tracking issue's body."""
    return f"<!-- nightly-audit:{label} -->"


def finding_classes(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Pure: the issue(s) this summary implies. Only on outcome 'findings'; one
    entry per distinct label (so other_findings + toolchain_fail collapse to a
    single 'audit-finding' issue rather than two)."""
    if summary.get("outcome") != "findings":
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, label, title in _CLASSES:
        if summary.get(key) and label not in seen:
            out.append({"key": key, "label": label, "marker": _marker(label), "title": title})
            seen.add(label)
    return out


def decide(open_issues: list[dict[str, Any]], marker: str) -> tuple[str, int | None]:
    """Pure: 'comment' on the first open issue whose body carries the marker, else
    'create'. This is the deterministic dedup the agent used to eyeball."""
    for iss in open_issues:
        if marker in (iss.get("body") or ""):
            return "comment", iss.get("number")
    return "create", None


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
        _req("POST", f"{_API}/repos/{repo}/labels", token, {
            "name": label,
            "color": _LABEL_COLORS.get(label, "ededed"),
            "description": "Opened by the nightly audit (deterministic routing)",
        })


def _open_issues(repo: str, label: str, token: str) -> list[dict[str, Any]]:
    url = f"{_API}/repos/{repo}/issues?state=open&labels={label}&per_page=100"
    result = _req("GET", url, token)
    return result if isinstance(result, list) else []


def sync(repo: str, cls: dict[str, str], body: str, token: str, dry_run: bool) -> str:
    marker = cls["marker"]
    full_body = f"{marker}\n\n{body}"
    if dry_run:
        _ensure = "would ensure"
        existing = []
    else:
        _ensure_label(repo, cls["label"], token)
        existing = _open_issues(repo, cls["label"], token)
    action, number = decide(existing, marker)
    if dry_run:
        return f"[dry-run] {action} issue for label '{cls['label']}'" + (
            f" #{number}" if number else "")
    if action == "comment":
        _req("POST", f"{_API}/repos/{repo}/issues/{number}/comments", token,
             {"body": f"Still present on the latest nightly:\n\n{body}"})
        return f"commented on #{number} ({cls['label']})"
    created = _req("POST", f"{_API}/repos/{repo}/issues", token, {
        "title": cls["title"], "body": full_body, "labels": [cls["label"]],
    })
    return f"opened #{created.get('number')} ({cls['label']})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary", required=True, help="path to nightly-summary.json")
    p.add_argument("--report", required=True, help="path to nightly-report.md (issue body)")
    p.add_argument("--target", default="", help="owner/repo (else taken from the summary)")
    p.add_argument("--token-env", default="GITHUB_TOKEN", help="env var holding the PAT")
    p.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    args = p.parse_args(argv)

    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"FATAL: cannot read summary: {e}", file=sys.stderr)
        return 1

    classes = finding_classes(summary)
    if not classes:
        print(f"no findings to route (outcome={summary.get('outcome')!r}) — nothing to do")
        return 0

    repo = args.target or str(summary.get("target") or "")
    if "/" not in repo or repo == "invalid":
        print(f"FATAL: no valid target repo (got {repo!r})", file=sys.stderr)
        return 1

    body = Path(args.report).read_text(encoding="utf-8") if Path(args.report).exists() else \
        "(nightly report body unavailable)"

    token = os.environ.get(args.token_env, "")
    if not token and not args.dry_run:
        print(f"FATAL: {args.token_env} not set (issues:write PAT required)", file=sys.stderr)
        return 1

    rc = 0
    for cls in classes:
        try:
            print(sync(repo, cls, body, token, args.dry_run))
        except urllib.error.HTTPError as e:
            print(f"ERROR routing '{cls['label']}': HTTP {e.code} {e.reason}", file=sys.stderr)
            rc = 1
        except urllib.error.URLError as e:
            print(f"ERROR routing '{cls['label']}': {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
