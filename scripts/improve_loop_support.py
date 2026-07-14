#!/usr/bin/env python3
"""Deterministic support steps of the Phase-6c improve loop.

Two subcommands, both stdlib-only and unit-testable (matching budget_guard /
improve_acceptance):

  report   aggregate THIS run's slice of the experiments journal into
           improve-summary.json + improve-report.md — the morning message the
           cron agent announces and the body of the draft PR.
  publish  open (or find) the draft PR for the improve/<datum> branch on the
           TARGET repo via the GitHub REST API. Idempotent: an existing open
           PR for the branch is reused, never duplicated. The loop merges
           nothing — the human is the merge gate (Goldene Regel 1).

The GITHUB_TOKEN is read from the environment and sent only as an
Authorization header — never printed, never interpolated into a shell.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_SUMMARY_SCHEMA = 1

Opener = Callable[[urllib.request.Request], Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- report --------------------------------------------------------------------


def read_run_entries(journal: Path, skip_lines: int) -> list[dict[str, Any]]:
    if not journal.exists():
        return []
    entries = []
    for line in journal.read_text(encoding="utf-8").splitlines()[skip_lines:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def summarize(entries: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    keeps = [e for e in entries if e.get("verdict") == "keep"]
    discards: dict[str, int] = {}
    for e in entries:
        if e.get("verdict") == "discard":
            grund = str(e.get("grund") or "unknown")
            discards[grund] = discards.get(grund, 0) + 1
    hard_fails = [str(e.get("grund")) for e in entries if e.get("verdict") == "hard-fail"]
    return {
        "schema": _SUMMARY_SCHEMA,
        "generated_at": _now_iso(),
        "target": args.target,
        "target_sha": args.sha,
        "branch": args.branch,
        "outcome": "hard-fail" if hard_fails else "completed",
        "iterations": len(entries),
        "keeps": [
            {
                "candidate": e.get("candidate"),
                "candidate_sha": e.get("candidate_sha"),
                "killed_mutant": e.get("killed_mutant"),
                "new_schema_refs": e.get("new_schema_refs"),
            }
            for e in keeps
        ],
        "discards": discards,
        "hard_fail_reasons": hard_fails,
        "dauer_s": round(sum(float(e.get("dauer_s") or 0) for e in entries), 3),
    }


def render_report(summary: dict[str, Any]) -> str:
    keeps = summary["keeps"]
    discards = summary["discards"]
    discard_bits = ", ".join(f"{n}× {g}" for g, n in sorted(discards.items()))
    lines = [
        f"# 🧪 Improve loop — `{summary['target']}` @ {summary['target_sha']}",
        "",
        f"- Run (UTC): {summary['generated_at']}",
        f"- Branch: `{summary['branch']}`",
        f"- Kandidaten: **{summary['iterations']}**, behalten: **{len(keeps)}**, "
        f"verworfen: **{sum(discards.values())}**"
        + (f" ({discard_bits})" if discard_bits else ""),
        "",
    ]
    if summary["outcome"] == "hard-fail":
        lines += [
            "**HARD FAILURE — der Lauf wurde abgebrochen; ein Abbruch ist kein Verdict.**",
            "",
        ]
        lines += [f"- {reason}" for reason in summary["hard_fail_reasons"]]
        lines.append("")
    if keeps:
        lines.append("## Behaltene Kandidaten")
        for k in keeps:
            proof = (
                f"tötet Mutante `{k['killed_mutant']}`"
                if k.get("killed_mutant")
                else f"neue Schema-Refs: {', '.join(k['new_schema_refs'] or [])}"
                if k.get("new_schema_refs")
                else "D1/D2 bestanden"
            )
            lines.append(f"- `{k['candidate']}` ({k['candidate_sha']}) — {proof}")
        lines += [
            "",
            "Jedes Keep hat die deterministische Annahme-Regel D1–D3 bestanden. "
            "Der Loop merged nichts — Review + Merge sind Menschensache.",
        ]
    else:
        lines.append("Keine Keeps in diesem Lauf — kein PR nötig.")
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    summary = summarize(read_run_entries(Path(args.journal), args.skip_lines), args)
    report = render_report(summary)
    Path(args.out_summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_summary).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    Path(args.out_report).write_text(report, encoding="utf-8")
    print(report)
    return 0


# --- publish -------------------------------------------------------------------


def _github(
    method: str, url: str, token: str, opener: Opener, payload: dict[str, Any] | None = None
) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with opener(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_publish(args: argparse.Namespace, opener: Opener | None = None) -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token.strip():
        print("PUBLISH: HARD FAIL — GITHUB_TOKEN is not set", flush=True)
        return 1
    opener = opener or urllib.request.urlopen
    owner = args.repo.split("/")[0]
    api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
    base_url = f"{api}/repos/{args.repo}/pulls"
    try:
        existing = _github(
            "GET",
            f"{base_url}?state=open&head={owner}:{args.branch}",
            token,
            opener,
        )
        if isinstance(existing, list) and existing:
            print(f"PUBLISH: open PR already exists: {existing[0].get('html_url')}", flush=True)
            return 0
        pr = _github(
            "POST",
            base_url,
            token,
            opener,
            {
                "title": args.title,
                "head": args.branch,
                "base": args.base,
                "body": Path(args.body_file).read_text(encoding="utf-8"),
                "draft": True,
            },
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"PUBLISH: HARD FAIL — GitHub API: {exc}", flush=True)
        return 1
    print(f"PUBLISH: draft PR created: {pr.get('html_url')}", flush=True)
    return 0


def main(argv: list[str] | None = None, opener: Opener | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    rep = sub.add_parser("report", help="aggregate this run's journal slice")
    rep.add_argument("--journal", required=True)
    rep.add_argument("--skip-lines", type=int, default=0, dest="skip_lines")
    rep.add_argument("--target", required=True)
    rep.add_argument("--sha", required=True)
    rep.add_argument("--branch", required=True)
    rep.add_argument("--out-report", required=True, dest="out_report")
    rep.add_argument("--out-summary", required=True, dest="out_summary")
    rep.set_defaults(func=lambda a: cmd_report(a))

    pub = sub.add_parser("publish", help="open the draft PR for the improve branch")
    pub.add_argument("--repo", required=True, help="owner/name of the TARGET repo")
    pub.add_argument("--branch", required=True)
    pub.add_argument("--base", required=True)
    pub.add_argument("--title", required=True)
    pub.add_argument("--body-file", required=True, dest="body_file")
    pub.set_defaults(func=lambda a: cmd_publish(a, opener))

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
