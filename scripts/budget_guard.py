#!/usr/bin/env python3
"""Budget guardrails for the nightly-audit cron — the Phase-5 cost/safety circuit.

The daily 03:00 OpenClaw cron audit (Plan Phase 4) calls a *cloud* model on
every run. Left unbounded, two failure modes burn money or hammer a broken
target: a runaway agent loop (token blow-up in a single run) and a wedged
environment that keeps red-failing every night. Phase 5 adds three deterministic
leitplanken around the run — implemented here, enforced by the wrapper script,
never left to the agent's judgement:

  1. **Token ceiling** — a per-run and a rolling-window cap on tokens spent. A
     run that measurably exceeds the per-run ceiling, or a window already over
     its cap, is a budget breach.
  2. **Circuit breaker** — after N consecutive HARD failures (an unresolvable
     model, a gate that could not run) *or* a budget breach, the breaker opens
     and the next runs are SKIPPED for a cooldown, instead of re-spending tokens
     on an environment that is clearly wedged. A green/findings run closes it.
  3. **Max iterations** — a cap on the agent's own think/tool loop. This one is
     enforced upstream (the OpenClaw payload and/or the TensorZero gateway, see
     docs/budget/guardrails.md); this module *validates the knob is set* and
     surfaces the effective value so the wrapper can pass it on, rather than
     silently running uncapped.

Contract with ``scripts/nightly-audit.sh`` (and, through it, the cron agent):

  preflight  -> exit 0   the breaker is closed (or in a half-open trial) — RUN.
             -> exit 75  the breaker is OPEN — SKIP this run. (75 = EX_TEMPFAIL.)
                         With --out-report/--out-summary it also writes a
                         hard-fail-shaped report+summary so the cron agent routes
                         the skip exactly like any other "did not pass" outcome:
                         a skipped audit is never announced as green.
  record     -> exit 0   state updated for the next run. Recording never fails
                         the pipeline (the run already happened) unless --strict.

State lives in the gitignored ``.audit/budget-state.json``. Everything here is
stdlib-only and side-effect-free except for that one file (matches
``scripts/live_probe.py``). All inputs (promptfoo JSON, env) are treated as
untrusted: we read tokens as ints and never exec anything (AGENTS.md / TOOLS.md).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# preflight exit codes (the contract with nightly-audit.sh).
PREFLIGHT_RUN = 0
PREFLIGHT_SKIP = 75  # EX_TEMPFAIL: a temporary, self-clearing refusal to run.

# Outcome <- exit-code mapping, shared with the nightly-audit exit contract
# (0 green / 2 findings / 1 hard-fail). Only a HARD failure feeds the breaker.
_OUTCOME_BY_EXIT = {0: "green", 2: "findings", 1: "hard-fail"}

_STATE_SCHEMA = 1
_MAX_RUN_HISTORY = 50  # keep the state file bounded; we only need recent runs.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Limits:
    """Effective guardrail limits, resolved from env with safe defaults."""

    def __init__(self) -> None:
        self.tokens_per_run = _env_int("BUDGET_TOKENS_PER_RUN", 200_000)
        self.tokens_window = _env_int("BUDGET_TOKENS_WINDOW", 2_000_000)
        self.window_seconds = _env_int("BUDGET_WINDOW_SECONDS", 86_400)  # 24h
        self.breaker_threshold = _env_int("BUDGET_BREAKER_THRESHOLD", 3)
        self.cooldown_seconds = _env_int("BUDGET_BREAKER_COOLDOWN_SECONDS", 21_600)  # 6h
        self.max_iterations = _env_int("BUDGET_MAX_ITERATIONS", 25)

    def as_dict(self) -> dict[str, int]:
        return {
            "tokens_per_run": self.tokens_per_run,
            "tokens_window": self.tokens_window,
            "window_seconds": self.window_seconds,
            "breaker_threshold": self.breaker_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "max_iterations": self.max_iterations,
        }


def _default_state() -> dict[str, Any]:
    return {
        "schema": _STATE_SCHEMA,
        "breaker": {
            "state": "closed",  # closed | open | half_open
            "consecutive_hard_fails": 0,
            "opened_at": None,
            "open_reason": None,
        },
        "window": {"tokens_used": 0, "window_started_at": None},
        "runs": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must not wedge the audit forever; start clean but
        # leave the breaker CLOSED so a transient FS glitch never silently skips.
        return _default_state()
    if not isinstance(data, dict) or data.get("schema") != _STATE_SCHEMA:
        return _default_state()
    # Tolerate partially-written state by filling any missing top-level keys.
    base = _default_state()
    base.update({k: data.get(k, base[k]) for k in base})
    return base


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["runs"] = state.get("runs", [])[-_MAX_RUN_HISTORY:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX: never leave a half-written state file.


def _roll_window(state: dict[str, Any], limits: Limits, now: datetime) -> None:
    """Reset the rolling token window if it has expired."""
    win = state["window"]
    started = _parse_iso(win.get("window_started_at"))
    if started is None or (now - started).total_seconds() >= limits.window_seconds:
        win["tokens_used"] = 0
        win["window_started_at"] = _iso(now)


def tokens_from_promptfoo(path: Path | None) -> int:
    """Best-effort total token count from a promptfoo `--output` JSON file.

    promptfoo nests usage under `stats.tokenUsage.total` (sometimes wrapped in a
    top-level `results` object). Absent/unparseable -> 0 (we never guess a cost).
    """
    if path is None or not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    block = data.get("results", data)
    if isinstance(block, list):
        block = data
    stats = (block.get("stats") if isinstance(block, dict) else None) or {}
    usage = stats.get("tokenUsage") or {}
    total = usage.get("total")
    try:
        return max(0, int(total))
    except (TypeError, ValueError):
        return 0


def _trip(state: dict[str, Any], reason: str, now: datetime) -> None:
    br = state["breaker"]
    br["state"] = "open"
    br["opened_at"] = _iso(now)
    br["open_reason"] = reason


# --- commands ---------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace, limits: Limits) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    now = _now()
    _roll_window(state, limits, now)
    br = state["breaker"]

    skip_reason: str | None = None
    if br["state"] == "open":
        opened = _parse_iso(br.get("opened_at"))
        elapsed = (now - opened).total_seconds() if opened else limits.cooldown_seconds
        if elapsed < limits.cooldown_seconds:
            remaining = int(limits.cooldown_seconds - elapsed)
            skip_reason = (
                f"circuit breaker OPEN ({br.get('open_reason') or 'repeated failures'}); "
                f"cooldown {remaining}s remaining — run skipped to avoid spending "
                f"tokens on a wedged environment"
            )
        else:
            # Cooldown elapsed: allow ONE trial run (half-open). record() decides
            # whether to close (success) or re-open (another failure).
            br["state"] = "half_open"

    # Window already over budget is also a skip — a cost circuit, not just a fault one.
    if skip_reason is None and state["window"]["tokens_used"] >= limits.tokens_window:
        skip_reason = (
            f"rolling token window exhausted "
            f"({state['window']['tokens_used']}/{limits.tokens_window} in "
            f"{limits.window_seconds}s) — run skipped until the window rolls over"
        )

    save_state(state_path, state)

    if skip_reason is not None:
        print(f"BUDGET: SKIP — {skip_reason}", flush=True)
        if args.out_summary:
            _write_skip_artifacts(args, skip_reason, limits, now)
        return PREFLIGHT_SKIP

    print(
        f"BUDGET: RUN — breaker {br['state']}, "
        f"window {state['window']['tokens_used']}/{limits.tokens_window} tokens, "
        f"max_iterations={limits.max_iterations}",
        flush=True,
    )
    # Surface max-iterations for the wrapper to pass upstream (OpenClaw/TensorZero).
    print(f"BUDGET_MAX_ITERATIONS={limits.max_iterations}", flush=True)
    return PREFLIGHT_RUN


def _write_skip_artifacts(
    args: argparse.Namespace, reason: str, limits: Limits, now: datetime
) -> None:
    """On a breaker SKIP, emit a hard-fail-shaped report + summary so the cron
    agent routes it like any other "did not pass" outcome (never announced as
    green). Shape matches scripts/nightly_audit_report.py's summary."""
    summary = {
        "generated_at": _iso(now),
        "target": args.target,
        "target_sha": "skipped",
        "outcome": "hard-fail",
        "exit_code": 1,
        "green": False,
        "hard_fail": True,
        "hard_fail_reasons": [reason],
        "schema_drift": False,
        "redteam": False,
        "toolchain_fail": False,
        "skipped_by_budget_guard": True,
        "limits": limits.as_dict(),
    }
    report = (
        f"# ⛔ Nightly audit — `{args.target}`\n\n"
        f"- Outcome: **hard-fail (skipped by budget guard)**\n"
        f"- Run (UTC): {_iso(now)}\n\n"
        f"**HARD FAILURE — the audit was NOT run. Do NOT treat as passed.**\n\n"
        f"## ⛔ Budget guard\n- {reason}\n\n"
        f"The circuit breaker protects against spending tokens on a wedged "
        f"environment. It re-arms automatically after the cooldown; to force a "
        f"run sooner, reset it: `python3 scripts/budget_guard.py reset "
        f'--reason "<why>"`.\n'
    )
    if args.out_report:
        Path(args.out_report).write_text(report, encoding="utf-8")
    Path(args.out_summary).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(report)


def cmd_record(args: argparse.Namespace, limits: Limits) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    now = _now()
    _roll_window(state, limits, now)
    br = state["breaker"]

    outcome = _OUTCOME_BY_EXIT.get(args.exit_code, "hard-fail")
    tokens = args.tokens
    if tokens < 0:  # not given explicitly — try the promptfoo output.
        tokens = tokens_from_promptfoo(Path(args.promptfoo_json)) if args.promptfoo_json else 0

    # Account tokens against the rolling window.
    state["window"]["tokens_used"] = int(state["window"]["tokens_used"]) + int(tokens)

    breaches: list[str] = []
    if limits.tokens_per_run and tokens > limits.tokens_per_run:
        breaches.append(
            f"per-run token ceiling exceeded ({tokens}/{limits.tokens_per_run})"
        )
    if state["window"]["tokens_used"] > limits.tokens_window:
        breaches.append(
            f"rolling token window exceeded "
            f"({state['window']['tokens_used']}/{limits.tokens_window})"
        )

    # Breaker bookkeeping: HARD failures accumulate; a green/findings run is a
    # success that closes the breaker and clears the streak.
    if outcome == "hard-fail":
        br["consecutive_hard_fails"] = int(br["consecutive_hard_fails"]) + 1
        if br["consecutive_hard_fails"] >= limits.breaker_threshold:
            _trip(
                state,
                f"{br['consecutive_hard_fails']} consecutive hard failures",
                now,
            )
    else:
        br["consecutive_hard_fails"] = 0
        if br["state"] in ("half_open", "open"):
            br["state"] = "closed"
            br["opened_at"] = None
            br["open_reason"] = None

    # A budget breach trips the breaker regardless of pass/fail — runaway cost is
    # itself a reason to stop launching runs.
    if breaches:
        _trip(state, "; ".join(breaches), now)

    state["runs"].append(
        {
            "at": _iso(now),
            "outcome": outcome,
            "exit_code": args.exit_code,
            "tokens": tokens,
            "breaches": breaches,
        }
    )
    save_state(state_path, state)

    print(
        f"BUDGET: recorded outcome={outcome} tokens={tokens} "
        f"window={state['window']['tokens_used']}/{limits.tokens_window} "
        f"breaker={br['state']} streak={br['consecutive_hard_fails']}",
        flush=True,
    )
    for b in breaches:
        print(f"BUDGET: breach — {b}", flush=True)

    # Recording reflects what already happened; it must not turn a real outcome
    # into a pipeline failure unless explicitly asked (--strict, for tests/CI).
    if args.strict and (breaches or br["state"] == "open"):
        return 1
    return 0


def cmd_status(args: argparse.Namespace, limits: Limits) -> int:
    state = load_state(Path(args.state))
    out = {"limits": limits.as_dict(), **state}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def cmd_reset(args: argparse.Namespace, limits: Limits) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    state["breaker"] = _default_state()["breaker"]
    if args.clear_window:
        state["window"] = _default_state()["window"]
    state["runs"].append(
        {"at": _iso(_now()), "outcome": "reset", "reason": args.reason or "manual"}
    )
    save_state(state_path, state)
    print(f"BUDGET: breaker reset ({args.reason or 'manual'})", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--state",
        default=os.environ.get("BUDGET_STATE", ".audit/budget-state.json"),
        help="path to the gitignored budget state file",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="gate a run on the breaker / token window")
    pf.add_argument("--target", default=os.environ.get("TARGET_REPO", "unknown"))
    pf.add_argument("--out-report", dest="out_report", default="")
    pf.add_argument("--out-summary", dest="out_summary", default="")
    pf.set_defaults(func=cmd_preflight)

    rec = sub.add_parser("record", help="record a finished run's outcome + tokens")
    rec.add_argument("--exit-code", type=int, required=True, dest="exit_code")
    rec.add_argument("--tokens", type=int, default=-1, help="explicit token total (else from --promptfoo-json)")
    rec.add_argument("--promptfoo-json", dest="promptfoo_json", default="")
    rec.add_argument("--strict", action="store_true", help="exit non-zero on a breach / open breaker")
    rec.set_defaults(func=cmd_record)

    st = sub.add_parser("status", help="print effective limits + current state")
    st.set_defaults(func=cmd_status)

    rs = sub.add_parser("reset", help="manually close the breaker")
    rs.add_argument("--reason", default="")
    rs.add_argument("--clear-window", action="store_true", help="also reset the token window")
    rs.set_defaults(func=cmd_reset)

    args = p.parse_args(argv)
    return int(args.func(args, Limits()))


if __name__ == "__main__":
    raise SystemExit(main())
