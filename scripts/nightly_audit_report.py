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


# Gate exit codes carried in a Worker evidence file (see nightly-audit.sh). The
# Worker ships raw evidence; the trusted Broker re-classifies from it, so a
# compromised Worker cannot forge a green verdict (Analysis S2).
_GATE_NAMES = ("ruff", "mypy", "pytest", "schema_drift", "promptfoo_rc")


def _load_evidence(path: Path) -> dict[str, Any]:
    """Parse a Worker-produced evidence file. UNTRUSTED and best-effort: an absent
    or garbled file yields {} so every gate later defaults to 'could-not-run' and
    the run classifies as HARD-FAIL, never green."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _gate_from_evidence(gates: dict[str, Any], name: str) -> int:
    """A gate's exit code from evidence, defaulting to 127 (could-not-run) when
    absent/unparseable — a garbled evidence file must never read as green."""
    try:
        return int(gates[name])
    except (KeyError, TypeError, ValueError):
        return 127


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
            "other_failures": 0,
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
    other_failures = 0
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
            # A failure we can attribute to neither the schema/contract nor the
            # red-team. Do NOT fold it into contract_failures — that would falsely
            # report "schema drift". It is still a finding (exit 2), just its own
            # class (Analysis T-F).
            other_failures += 1
            label = "other"
        if len(examples) < 12:
            examples.append(f"{label}: {str(desc)[:160]}")

    # `stats.errors` and per-result `.error` describe the same provider/model
    # failures from two angles; take the larger rather than summing them.
    return {
        "ran": True,
        "errors": max(stats_errors, result_errors),
        "redteam_hits": redteam_hits,
        "contract_failures": contract_failures,
        "other_failures": other_failures,
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
    other_findings = pfc.get("other_failures", 0) > 0
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
    if not pfc["ran"]:
        # promptfoo produced no parseable output. This is ALWAYS a hard failure —
        # the deterministic red-team/contract layer is the auditor's job, so a
        # missing eval is infrastructure failure, never a silent "surface looks
        # safe". Crucially this now also catches promptfoo_rc == 0 with no output
        # (a forged/garbled green from an untrusted Worker) — evidence we cannot
        # verify is never treated as a pass (Analysis S-A).
        if args.promptfoo_rc == 0:
            hard_fail_reasons.append(
                "promptfoo reported success (rc 0) but produced no parseable output — "
                "evidence incomplete; a green verdict cannot be derived"
            )
        else:
            hard_fail_reasons.append("promptfoo did not run (config/binary error)")
    for name, rc in (("ruff", args.ruff), ("mypy", args.mypy), ("pytest", args.pytest)):
        if rc in infra_codes:
            hard_fail_reasons.append(f"{name} could not run (exit {rc})")
    if args.schema_drift in infra_codes:
        hard_fail_reasons.append(f"schema-drift gate could not run (exit {args.schema_drift})")

    hard_fail = bool(hard_fail_reasons)
    green = not (schema_drift or redteam or other_findings or toolchain_fail or hard_fail)

    if hard_fail:
        outcome, exit_code = "hard-fail", EXIT_HARD_FAIL
    elif green:
        outcome, exit_code = "green", EXIT_GREEN
    else:
        outcome, exit_code = "findings", EXIT_FINDINGS

    # Which promptfoo profile produced this verdict (Analysis T-C). A determ-only
    # run did NOT exercise the model-graded layer (llm-rubric + red-team), so a
    # green determ verdict must never be read as "red-team clear".
    profile = (getattr(args, "promptfoo_profile", "") or "unknown")
    graded_layer_ran = profile in ("graded", "full")

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": args.target,
        "target_sha": args.sha,
        "promptfoo_profile": profile,
        "graded_layer_ran": graded_layer_ran,
        "outcome": outcome,
        "exit_code": exit_code,
        "green": green,
        "hard_fail": hard_fail,
        "hard_fail_reasons": hard_fail_reasons,
        "schema_drift": schema_drift,
        "redteam": redteam,
        "other_findings": other_findings,
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
        f"- promptfoo profile: **{s.get('promptfoo_profile', 'unknown')}**",
    ]

    # A determ-only run did not exercise the model-graded layer. Say so loudly so
    # a green determ verdict is never mistaken for a full red-team pass (T-C).
    if not s.get("graded_layer_ran", False):
        lines += [
            "",
            "> **Note — deterministic profile only.** This run evaluated the "
            "key-less contract + injection layer. The model-graded layer "
            "(llm-rubric + red-team) did **not** run here — it runs in "
            "CI-with-secrets / a keyed run. A green result means the deterministic "
            "layer passed, **not** that the red-team is clear.",
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
    if s.get("other_findings"):
        findings.append(
            f"**Other promptfoo failure(s)** — {pf.get('other_failures', 0)} case(s) failed but "
            "matched neither the schema/contract nor the red-team class (see detail)."
        )
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
    # Gate exit codes: pass each flag directly (host run), OR --from-evidence to
    # read them from a Worker evidence JSON (Broker-side classification, S2).
    p.add_argument("--ruff", type=int)
    p.add_argument("--mypy", type=int)
    p.add_argument("--pytest", type=int)
    p.add_argument("--schema-drift", type=int, dest="schema_drift")
    p.add_argument("--promptfoo-rc", type=int, dest="promptfoo_rc")
    p.add_argument("--from-evidence", default="", dest="from_evidence",
                   help="read gate exit codes (+ target/sha) from a Worker evidence JSON")
    p.add_argument("--promptfoo-json", default="", dest="promptfoo_json")
    p.add_argument("--promptfoo-profile", default="", dest="promptfoo_profile",
                   help="which promptfoo profile ran (determ|graded|full); stamped into the summary")
    p.add_argument("--target", default="")
    p.add_argument("--sha", default="unknown")
    p.add_argument("--out-report", required=True, dest="out_report")
    p.add_argument("--out-summary", required=True, dest="out_summary")
    args = p.parse_args()

    if args.from_evidence:
        ev = _load_evidence(Path(args.from_evidence))
        gates = ev.get("gates") if isinstance(ev.get("gates"), dict) else {}
        for name in _GATE_NAMES:
            setattr(args, name, _gate_from_evidence(gates, name))
        if not args.target:
            args.target = str(ev.get("target") or "unknown")
        if not args.sha or args.sha == "unknown":
            args.sha = str(ev.get("target_sha") or "unknown")
        if not args.promptfoo_profile:
            args.promptfoo_profile = str(ev.get("promptfoo_profile") or "")
    else:
        missing = [f"--{n.replace('_', '-')}" for n in _GATE_NAMES if getattr(args, n) is None]
        if missing:
            p.error("missing gate flags: " + ", ".join(missing) + " (or pass --from-evidence)")
        if not args.target:
            p.error("--target is required (or pass --from-evidence carrying it)")

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
