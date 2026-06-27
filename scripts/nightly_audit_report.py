#!/usr/bin/env python3
"""Aggregate the nightly-audit toolchain results into a report + machine summary.

This is the interpretation half of the daily 03:00 OpenClaw cron audit (Plan
Phase 4). ``scripts/nightly-audit.sh`` runs the deterministic gates against a
read-only checkout of the target MCP server and hands their exit codes + the
promptfoo JSON output to this module. Here we:

  * classify the outcome into **schema drift**, **red-team hit**, plain
    toolchain failure, or all-green;
  * separate a genuine finding (a red eval) from an *infrastructure* failure —
    most importantly an **unresolvable model / provider error** in promptfoo,
    which must HARD-FAIL the run rather than be silently reported as "passed"
    (Plan Phase 4: "Bei nicht aufloesbarem Modell: hart fehlschlagen, nicht
    still ausweichen");
  * write a concise Markdown report (used as the Telegram announce body and the
    GitHub issue body) and a ``summary.json`` the cron agent routes on.

The exit code is the contract with the orchestrator and the cron agent:

  0  all gates green
  2  finding(s): schema drift and/or red-team hit and/or toolchain failure
  1  hard failure: a gate could not run, or a model/provider was unresolvable

Ground truth is the exit code, never an opinion (SOUL.md). promptfoo output is
UNTRUSTED data (it embeds upstream API payloads) — we read it as JSON files and
never interpolate it into a shell (AGENTS.md / TOOLS.md).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Exit-code contract (shared with nightly-audit.sh and the cron agent prompt).
EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_HARD_FAIL = 1

# promptfoo assertion types that encode a tool-output contract / schema. A
# failure on one of these is schema drift, not a red-team hit.
_CONTRACT_ASSERTIONS = {"is-json", "is-valid-json", "javascript"}


def _load_promptfoo(path: Path) -> dict[str, Any] | None:
    """Parse a promptfoo `--output` JSON file, or None if absent/unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _results_block(pf: dict[str, Any]) -> dict[str, Any]:
    """promptfoo nests results under `results` (object) across versions; tolerate
    both the wrapped object and a bare list."""
    block = pf.get("results", pf)
    if isinstance(block, list):
        return {"results": block, "stats": pf.get("stats", {})}
    return block if isinstance(block, dict) else {"results": [], "stats": {}}


def _is_redteam(test_case: dict[str, Any]) -> bool:
    """A result belongs to the OWASP red-team block if it carries plugin/strategy
    metadata or a synthesised adversarial prompt."""
    meta = test_case.get("metadata") or {}
    if any(k in meta for k in ("pluginId", "strategyId", "redteamFinalPrompt")):
        return True
    # Older promptfoo stamps the plugin onto the assertion metric name.
    return str(meta.get("purpose", "")).strip().lower().startswith("redteam")


def _failed_assertion_types(result: dict[str, Any]) -> set[str]:
    grading = result.get("gradingResult") or {}
    types: set[str] = set()
    for comp in grading.get("componentResults") or []:
        if comp.get("pass") is False:
            assertion = comp.get("assertion") or {}
            atype = assertion.get("type")
            if atype:
                types.add(str(atype))
    return types


def classify_promptfoo(pf: dict[str, Any] | None, promptfoo_rc: int) -> dict[str, Any]:
    """Reduce promptfoo output to {ran, errors, redteam_hits, contract_failures,
    failures, examples}. `errors` > 0 (provider/model could not be resolved or
    the call itself failed) is a HARD failure, distinct from an assertion
    `failure` (a real, reportable finding)."""
    if pf is None:
        # No JSON at all: the eval could not even produce output. If promptfoo
        # also exited non-zero, treat it as an infrastructure failure.
        return {
            "ran": False,
            "errors": 0 if promptfoo_rc == 0 else 1,
            "redteam_hits": 0,
            "contract_failures": 0,
            "failures": 0,
            "examples": [] if promptfoo_rc == 0 else ["promptfoo produced no output and exited non-zero"],
        }

    block = _results_block(pf)
    stats = block.get("stats") or {}
    results = block.get("results") or []

    stats_errors = int(stats.get("errors") or 0)
    result_errors = 0
    redteam_hits = 0
    contract_failures = 0
    failures = 0
    examples: list[str] = []

    for r in results:
        if r.get("error"):
            result_errors += 1
            examples.append(f"error: {str(r.get('error'))[:160]}")
            continue
        if r.get("success") is not False:
            continue
        failures += 1
        tc = r.get("testCase") or {}
        desc = tc.get("description") or (r.get("description") or "unnamed test")
        if _is_redteam(tc):
            redteam_hits += 1
            label = "red-team"
        elif _failed_assertion_types(r) & _CONTRACT_ASSERTIONS:
            contract_failures += 1
            label = "contract/schema"
        else:
            contract_failures += 1  # default an unclassified failure to contract
            label = "contract"
        if len(examples) < 12:
            examples.append(f"{label}: {str(desc)[:160]}")

    # `stats.errors` and per-result `.error` describe the same provider/model
    # failures from two angles; take the larger rather than summing them.
    return {
        "ran": True,
        "errors": max(stats_errors, result_errors),
        "redteam_hits": redteam_hits,
        "contract_failures": contract_failures,
        "failures": failures,
        "examples": examples,
    }


def _status(rc: int) -> str:
    return "✅ pass" if rc == 0 else f"❌ fail (exit {rc})"


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    pf = _load_promptfoo(Path(args.promptfoo_json)) if args.promptfoo_json else None
    pfc = classify_promptfoo(pf, args.promptfoo_rc)

    # Schema drift = the deterministic schema gate diverged OR a promptfoo
    # is-json/contract assertion failed.
    schema_drift = args.schema_drift != 0 or pfc["contract_failures"] > 0
    redteam = pfc["redteam_hits"] > 0
    toolchain_fail = args.ruff != 0 or args.mypy != 0 or args.pytest != 0

    # Hard failure (never silently downgraded to "passed"):
    #   * an audited gate could not run (missing bin / sync failure, rc 127/126);
    #   * promptfoo could not run at all; or
    #   * a model/provider was unresolvable (promptfoo errors > 0).
    infra_codes = {126, 127}
    hard_fail_reasons: list[str] = []
    if pfc["errors"] > 0:
        hard_fail_reasons.append(
            f"promptfoo reported {pfc['errors']} provider/model error(s) — "
            "an unresolvable or unauthorised model is a HARD failure, not a pass"
        )
    if not pfc["ran"] and args.promptfoo_rc not in (0,):
        hard_fail_reasons.append("promptfoo did not run (config/binary error)")
    for name, rc in (("ruff", args.ruff), ("mypy", args.mypy), ("pytest", args.pytest)):
        if rc in infra_codes:
            hard_fail_reasons.append(f"{name} could not run (exit {rc})")
    if args.schema_drift in infra_codes:
        hard_fail_reasons.append(f"schema-drift gate could not run (exit {args.schema_drift})")

    hard_fail = bool(hard_fail_reasons)
    green = not (schema_drift or redteam or toolchain_fail or hard_fail)

    if hard_fail:
        outcome, exit_code = "hard-fail", EXIT_HARD_FAIL
    elif green:
        outcome, exit_code = "green", EXIT_GREEN
    else:
        outcome, exit_code = "findings", EXIT_FINDINGS

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": args.target,
        "target_sha": args.sha,
        "outcome": outcome,
        "exit_code": exit_code,
        "green": green,
        "hard_fail": hard_fail,
        "hard_fail_reasons": hard_fail_reasons,
        "schema_drift": schema_drift,
        "redteam": redteam,
        "toolchain_fail": toolchain_fail,
        "gates": {
            "ruff": args.ruff,
            "mypy": args.mypy,
            "pytest": args.pytest,
            "schema_drift_gate": args.schema_drift,
            "promptfoo_rc": args.promptfoo_rc,
        },
        "promptfoo": pfc,
    }


def render_report(s: dict[str, Any]) -> str:
    icon = {"green": "✅", "findings": "🚨", "hard-fail": "⛔"}[s["outcome"]]
    head = {
        "green": "All gates green — no schema drift, no red-team hit.",
        "findings": "Findings detected — see below.",
        "hard-fail": "HARD FAILURE — the audit could not complete. Do NOT treat as passed.",
    }[s["outcome"]]

    lines = [
        f"# {icon} Nightly audit — `{s['target']}`",
        "",
        f"- Target: `{s['target']}` @ `{s['target_sha']}`",
        f"- Run (UTC): {s['generated_at']}",
        f"- Outcome: **{s['outcome']}**",
        "",
        f"**{head}**",
        "",
        "## Gates",
        f"- ruff: {_status(s['gates']['ruff'])}",
        f"- mypy: {_status(s['gates']['mypy'])}",
        f"- pytest: {_status(s['gates']['pytest'])}",
        f"- schema-drift gate: {_status(s['gates']['schema_drift_gate'])}",
        f"- promptfoo (contract + red-team): {_status(s['gates']['promptfoo_rc'])}",
    ]

    if s["hard_fail"]:
        lines += ["", "## ⛔ Hard failure"]
        lines += [f"- {r}" for r in s["hard_fail_reasons"]]
        lines += [
            "",
            "The run is **not** a pass. No green claim is made (SOUL.md). "
            "Resolve the model/provider or the broken gate and re-run.",
        ]

    pf = s["promptfoo"]
    findings: list[str] = []
    if s["schema_drift"]:
        findings.append("**Schema drift** — committed schema / tool-output contract diverged.")
    if s["redteam"]:
        findings.append(f"**Red-team hit** — {pf['redteam_hits']} adversarial case(s) succeeded against the surface.")
    if s["toolchain_fail"]:
        findings.append("**Toolchain failure** — ruff/mypy/pytest is red (see gates above).")
    if findings:
        lines += ["", "## 🚨 Findings"]
        lines += [f"- {f}" for f in findings]

    if pf.get("examples"):
        lines += ["", "## promptfoo detail"]
        lines += [f"- {e}" for e in pf["examples"]]

    if s["outcome"] == "findings":
        lines += [
            "",
            "---",
            "Per AGENTS.md the writer never pushes to `main`. A draft PR will be "
            "opened **only after an explicit Telegram OK** — reply `OK` to authorise "
            "a `fix/<slug>` draft PR for the finding(s) above.",
        ]

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ruff", type=int, required=True)
    p.add_argument("--mypy", type=int, required=True)
    p.add_argument("--pytest", type=int, required=True)
    p.add_argument("--schema-drift", type=int, required=True, dest="schema_drift")
    p.add_argument("--promptfoo-rc", type=int, required=True, dest="promptfoo_rc")
    p.add_argument("--promptfoo-json", default="", dest="promptfoo_json")
    p.add_argument("--target", required=True)
    p.add_argument("--sha", default="unknown")
    p.add_argument("--out-report", required=True, dest="out_report")
    p.add_argument("--out-summary", required=True, dest="out_summary")
    args = p.parse_args()

    summary = build_summary(args)
    report = render_report(summary)

    Path(args.out_report).write_text(report, encoding="utf-8")
    Path(args.out_summary).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(report)
    print(f"OUTCOME={summary['outcome']} exit={summary['exit_code']}")
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
