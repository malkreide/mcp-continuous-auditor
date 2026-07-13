#!/usr/bin/env python3
"""Acceptance harness for the Phase-6 improve loop — the deterministic judge.

Phase 6 (docs/plans/2026-07-13-phase-6-improve-loop.md) lets a writer agent
propose ONE candidate change to the target's key-less determ suite per
iteration. Whether the candidate is kept is decided HERE, by committed code —
never by an LLM's opinion (Goldene Regel 2). This module implements the 6a
subset of the acceptance rule:

  D1  Reproducibility — the suite with the candidate applied runs twice with
      an identical per-test outcome. A differing pair is discarded as
      ``flaky``: a flaky assert would poison the deterministic ground truth.
  D2  No false positive — every test that is red with the candidate applied
      was already red in the baseline (current target HEAD, no candidate).
      A candidate that turns green tests red, or whose own new asserts fail
      on HEAD, is discarded as ``false-positive``.

(D3, added value via mutation score, is Phase 6b and not implemented here.)

Two further checks are properties of the candidate itself, so they are also
discards, not hard failures: a patch that does not apply (``invalid``) and a
patch touching anything outside ``promptfoo/`` (``out-of-scope`` — the single
editable surface, enforced in the harness and not just in IMPROVE.md).

Everything else — the runner crashing, unparseable results, an empty suite, a
baseline that is itself flaky on HEAD — is an infrastructure HARD failure:
exit 1, never counted as a discard (an aborted run is not a result).

Every judged candidate is appended to the gitignored, append-only journal
``.audit/experiments.jsonl`` (ts, candidate_sha, target_sha, verdict, grund,
dauer_s) — the autoresearch-style experiment log the morning PR embeds.

Exit codes (the contract with improve-loop.sh, Phase 6c — mirrors the repo's
0 green / 2 findings / 1 hard-fail convention):

  judge     -> 0  keep      commit the candidate onto improve/<datum>
            -> 2  discard   revert; the journal carries the grund
            -> 1  hard-fail abort the whole loop iteration
  baseline  -> 0  baseline computed (or cached) and reproducible
            -> 1  the existing suite is not deterministic on HEAD — that is a
                  nightly finding, not an improve problem (Invariante 5)

The suite runner is injected (``--runner``), never hardcoded: the harness
invokes ``<runner> <config> <output.json>`` in the target checkout and reads
promptfoo-style JSON from ``<output.json>``. Phase 6c supplies the pinned
promptfoo wrapper; the tests supply a tiny fake. Stdlib-only, and the only
side effects are the journal, the baseline cache, and the (always reverted)
candidate application — matching scripts/budget_guard.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_KEEP = 0
EXIT_HARD_FAIL = 1
EXIT_DISCARD = 2

# The single editable surface of the improve loop (Phase-6 Invariante 4).
EDITABLE_PREFIX = "promptfoo/"

# Runner exit codes that mean "the suite ran" (results JSON is authoritative):
# 0 = all pass, 1 = generic failure, 100 = promptfoo's default failed-eval code.
_RUNNER_RAN = {0, 1, 100}

_JOURNAL_SCHEMA = 1
_BASELINE_SCHEMA = 1


class Discard(Exception):
    """The candidate is rejected — a verdict about the candidate (exit 2)."""

    def __init__(self, grund: str, detail: str = "") -> None:
        super().__init__(detail or grund)
        self.grund = grund


class HardFail(Exception):
    """The judgement could not be carried out — never a verdict (exit 1)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_short(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _git(target_dir: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(target_dir), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def target_sha(target_dir: Path) -> str:
    cp = _git(target_dir, "rev-parse", "--short", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else "unknown"


# --- candidate patch handling -------------------------------------------------


def candidate_paths(target_dir: Path, patch: Path) -> list[str]:
    """Paths touched by the candidate, via ``git apply --numstat`` (no apply).

    An unparseable patch or an empty diff is a property of the candidate ->
    Discard("invalid"), not a hard failure.
    """
    cp = _git(target_dir, "apply", "--numstat", str(patch))
    if cp.returncode != 0:
        raise Discard("invalid", f"patch does not parse: {cp.stderr.strip()}")
    paths: list[str] = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2]:
            paths.append(parts[2])
    if not paths:
        raise Discard("invalid", "empty diff — nothing to judge")
    return paths


def check_scope(paths: list[str]) -> None:
    outside = [p for p in paths if not p.startswith(EDITABLE_PREFIX)]
    if outside:
        raise Discard(
            "out-of-scope",
            f"candidate touches files outside {EDITABLE_PREFIX}: {', '.join(outside)}",
        )


def apply_candidate(target_dir: Path, patch: Path) -> None:
    cp = _git(target_dir, "apply", "--check", str(patch))
    if cp.returncode != 0:
        raise Discard("invalid", f"patch does not apply: {cp.stderr.strip()}")
    cp = _git(target_dir, "apply", str(patch))
    if cp.returncode != 0:
        raise Discard("invalid", f"patch does not apply: {cp.stderr.strip()}")


def revert_candidate(target_dir: Path, patch: Path) -> None:
    cp = _git(target_dir, "apply", "-R", str(patch))
    if cp.returncode != 0:
        # A checkout left dirty would contaminate every later judgement.
        raise HardFail(
            f"could not revert candidate — target checkout is DIRTY: {cp.stderr.strip()}"
        )


# --- suite execution ------------------------------------------------------------


def _results_map(out_json: Path) -> dict[str, bool]:
    """Per-test outcome map from a promptfoo-style ``--output`` JSON file.

    Tolerates both the wrapped (``{"results": {"results": [...]}}``) and the
    bare (``{"results": [...]}``) shape, like budget_guard.tokens_from_promptfoo.
    """
    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HardFail(f"runner produced no parseable results JSON: {exc}") from exc
    block: Any = data.get("results", data) if isinstance(data, dict) else data
    if isinstance(block, dict):
        block = block.get("results", [])
    if not isinstance(block, list):
        raise HardFail("results JSON has no test list")
    outcomes: dict[str, bool] = {}
    for i, entry in enumerate(block):
        if not isinstance(entry, dict):
            raise HardFail(f"results entry #{i} is not an object")
        key = entry.get("description")
        if key is None:
            key = entry.get("testIdx")
        if key is None:
            key = i
        key = str(key)
        if key in outcomes:  # duplicate labels must not shadow each other
            key = f"{key}#{i}"
        outcomes[key] = bool(entry.get("success"))
    if not outcomes:
        # Fail closed: an empty suite silently "passing twice" is no evidence.
        raise HardFail("suite produced zero test results")
    return outcomes


def run_suite(
    runner: list[str], target_dir: Path, config: str, out_json: Path
) -> dict[str, bool]:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.unlink(missing_ok=True)
    try:
        cp = subprocess.run(
            [*runner, config, str(out_json)],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise HardFail(f"runner could not be executed: {exc}") from exc
    if cp.returncode not in _RUNNER_RAN:
        raise HardFail(
            f"runner failed (exit {cp.returncode}): {cp.stderr.strip()[-500:]}"
        )
    return _results_map(out_json)


def run_suite_twice(
    runner: list[str], target_dir: Path, config: str, work_dir: Path, tag: str
) -> tuple[dict[str, bool], dict[str, bool]]:
    run1 = run_suite(runner, target_dir, config, work_dir / f"{tag}-run1.json")
    run2 = run_suite(runner, target_dir, config, work_dir / f"{tag}-run2.json")
    return run1, run2


# --- baseline (existing suite on target HEAD, no candidate) ---------------------


def baseline_cache_path(journal: Path, sha: str) -> Path:
    return journal.parent / f"improve-baseline-{sha}.json"


def load_baseline(cache: Path, sha: str) -> dict[str, bool] | None:
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("schema") != _BASELINE_SCHEMA
        or data.get("target_sha") != sha
        or not isinstance(data.get("tests"), dict)
    ):
        return None
    return {str(k): bool(v) for k, v in data["tests"].items()}


def compute_baseline(
    runner: list[str], target_dir: Path, config: str, cache: Path, sha: str
) -> dict[str, bool]:
    run1, run2 = run_suite_twice(runner, target_dir, config, cache.parent, "baseline")
    if run1 != run2:
        # The EXISTING suite is not deterministic on HEAD. That is a nightly
        # finding about the target, not a property of any candidate: abort.
        raise HardFail(
            "baseline suite is not reproducible on target HEAD (two runs "
            "differ) — fix the existing suite before judging candidates"
        )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "schema": _BASELINE_SCHEMA,
                "target_sha": sha,
                "computed_at": _now_iso(),
                "tests": run1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return run1


# --- journal ---------------------------------------------------------------------


def journal_append(journal: Path, entry: dict[str, Any]) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


# --- commands ---------------------------------------------------------------------


def cmd_baseline(args: argparse.Namespace) -> int:
    target_dir = Path(args.target_dir)
    journal = Path(args.journal)
    sha = target_sha(target_dir)
    cache = Path(args.cache) if args.cache else baseline_cache_path(journal, sha)
    runner = shlex.split(args.runner)
    cached = load_baseline(cache, sha)
    if cached is not None:
        print(f"IMPROVE: baseline cached for {sha} ({len(cached)} tests)", flush=True)
        return 0
    try:
        tests = compute_baseline(runner, target_dir, args.config, cache, sha)
    except HardFail as exc:
        print(f"IMPROVE: HARD FAIL — {exc}", flush=True)
        return EXIT_HARD_FAIL
    print(f"IMPROVE: baseline computed for {sha} ({len(tests)} tests)", flush=True)
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    target_dir = Path(args.target_dir)
    journal = Path(args.journal)
    patch = Path(args.candidate)
    runner = shlex.split(args.runner)
    sha = target_sha(target_dir)
    cache = Path(args.cache) if args.cache else baseline_cache_path(journal, sha)

    entry: dict[str, Any] = {
        "schema": _JOURNAL_SCHEMA,
        "ts": _now_iso(),
        "candidate": patch.name,
        "candidate_sha": _sha256_short(patch) if patch.exists() else "missing",
        "target_sha": sha,
        "verdict": "hard-fail",
        "grund": None,
        "dauer_s": 0.0,
        "tests": None,
    }

    def finish(verdict: str, grund: str | None, rc: int) -> int:
        entry["verdict"] = verdict
        entry["grund"] = grund
        entry["dauer_s"] = round(time.monotonic() - t0, 3)
        journal_append(journal, entry)
        label = f" ({grund})" if grund else ""
        print(f"IMPROVE: {verdict}{label} — {patch.name} @ {sha}", flush=True)
        return rc

    try:
        if not patch.exists():
            raise HardFail(f"candidate patch not found: {patch}")

        check_scope(candidate_paths(target_dir, patch))

        baseline = load_baseline(cache, sha)
        if baseline is None:
            baseline = compute_baseline(runner, target_dir, args.config, cache, sha)

        apply_candidate(target_dir, patch)
        try:
            run1, run2 = run_suite_twice(
                runner, target_dir, args.config, cache.parent, "candidate"
            )
        finally:
            revert_candidate(target_dir, patch)

        entry["tests"] = {
            "total": len(run1),
            "failed": sum(1 for ok in run1.values() if not ok),
        }

        # D1 — reproducibility: two runs with the candidate must agree exactly.
        if run1 != run2:
            differing = sorted(k for k in run1 if run1[k] != run2.get(k))
            raise Discard(
                "flaky", f"non-reproducible outcome for: {', '.join(differing)}"
            )

        # D2 — no false positive: red-with-candidate is only acceptable where
        # the baseline was already red (a real, pre-existing nightly finding).
        false_pos = sorted(
            k for k, ok in run1.items() if not ok and baseline.get(k, True)
        )
        if false_pos:
            raise Discard(
                "false-positive", f"red on target HEAD: {', '.join(false_pos)}"
            )

    except Discard as exc:
        print(f"IMPROVE: discard detail — {exc}", flush=True)
        return finish("discard", exc.grund, EXIT_DISCARD)
    except HardFail as exc:
        print(f"IMPROVE: HARD FAIL — {exc}", flush=True)
        return finish("hard-fail", str(exc), EXIT_HARD_FAIL)

    return finish("keep", None, EXIT_KEEP)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--journal",
        default=os.environ.get("IMPROVE_JOURNAL", ".audit/experiments.jsonl"),
        help="append-only experiments journal (gitignored)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--target-dir", required=True, help="target checkout to judge against"
    )
    common.add_argument(
        "--config",
        default="promptfoo/promptfooconfig.determ.yaml",
        help="determ suite config, relative to the target checkout",
    )
    common.add_argument(
        "--runner",
        default=os.environ.get("IMPROVE_RUNNER", ""),
        help="suite runner command; invoked as `<runner> <config> <output.json>` "
        "in the target checkout (Phase 6c supplies the pinned promptfoo wrapper)",
    )
    common.add_argument(
        "--cache", default="", help="baseline cache file (default: derived from --journal + target SHA)"
    )

    bl = sub.add_parser(
        "baseline",
        parents=[common],
        help="compute/verify the reproducible baseline of the existing suite on HEAD",
    )
    bl.set_defaults(func=cmd_baseline)

    jd = sub.add_parser(
        "judge", parents=[common], help="judge one candidate diff (D1 + D2)"
    )
    jd.add_argument("--candidate", required=True, help="unified diff of the candidate")
    jd.set_defaults(func=cmd_judge)

    args = p.parse_args(argv)
    if not args.runner.strip():
        p.error("--runner is required (or set IMPROVE_RUNNER)")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
