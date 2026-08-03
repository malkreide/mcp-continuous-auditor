#!/usr/bin/env python3
"""Aggregate the nightly-audit toolchain results into a report + machine summary.

This is the interpretation half of the daily 03:00 OpenClaw cron audit (Plan
Phase 4). ``scripts/nightly-audit.sh`` runs the deterministic gates against a
read-only checkout of the target MCP server and hands their exit codes + the
promptfoo JSON output to this module. Here we:

  * classify the outcome into **schema drift**, **red-team hit**, **transport
    boot failure**, **DNS-rebinding control failure**, plain toolchain failure,
    or all-green — plus one state that is deliberately none of those: an inbound
    host allow-list that is simply **not configured**, which is reported as its
    own visible category rather than folded into a pass or a failure;
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
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Exit-code contract (shared with nightly-audit.sh and the cron agent prompt).
EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_HARD_FAIL = 1

# --- a gate that never finished (Iteration: hung gates) ---------------------
# GNU `timeout` returns 124 when it had to kill the command, and 128+9 = 137 when
# the command ignored SIGTERM and --kill-after had to SIGKILL it. nightly-audit.sh
# wraps every gate in `timeout`, so those two codes carry a meaning no ordinary
# exit code does: the gate produced NO VERDICT at all.
#
# This has to be its own class rather than another flavour of "could not run".
# Twice now a mutation test has surfaced as a HANGING suite rather than a red one
# — in one case because, without the control under test, an SSE GET under a
# foreign Host is admitted and opens an endless event stream the test client then
# waits on at teardown. A timeout that reads as generic infrastructure noise
# swallows exactly that finding. Naming the gate that hung is the whole point:
# "pytest hung" and "promptfoo hung" call for entirely different next steps.
GATE_TIMEOUT_RC = 124
GATE_KILLED_RC = 137
_HUNG_CODES = (GATE_TIMEOUT_RC, GATE_KILLED_RC)

# --- a green run that executed nothing (the silent zero) --------------------
# A test gate that exits 0 having collected no tests is not a pass; it is a gate
# that made no statement while looking exactly like one that did. `unittest
# discover` finding nothing prints "Ran 0 tests" and exits 0 — green, empty, and
# indistinguishable from success in any summary that only reads exit codes.
# The Worker measures the count from the runner's own output and ships it in the
# evidence; -1 means it could not be determined, which is itself not a pass.
TESTS_UNKNOWN = -1


def _hung(rc: int) -> bool:
    return rc in _HUNG_CODES


# promptfoo assertion types that encode a tool-output contract / schema. A
# failure on one of these is schema drift, not a red-team hit.
_CONTRACT_ASSERTIONS = {"is-json", "is-valid-json", "javascript"}

# --- untrusted-string hygiene at the sink (Analysis S-D) --------------------
# The evidence file, its target/target_sha, the header and the promptfoo examples
# are all Worker-controlled and flow into a Markdown report that reaches Telegram,
# GitHub issues, and the credential-holding cron agent's context — an IPI channel.
# We validate the structured fields and strip control chars from everything before
# it is rendered, so a Worker cannot inject terminal escapes or fake Markdown
# structure ("## All green…") into the report.
_TARGET_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
_META_SENTINELS = {"unknown", "skipped", "invalid"}


def _clean_inline(s: Any, max_len: int = 200) -> str:
    """Strip control chars/newlines from an untrusted string and truncate, so it
    cannot inject terminal escapes or structural Markdown into the report."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s))[:max_len]


def _validate_meta(
    value: Any, pattern: re.Pattern[str], kind: str
) -> tuple[str, str | None]:
    """Return (safe_value, error_or_None). A value that is neither a known sentinel
    nor pattern-matching is untrusted/tampered -> 'invalid' + an error string."""
    v = str(value or "").strip()
    if v in _META_SENTINELS or pattern.match(v):
        return v, None
    # Do NOT echo the rejected value: it is attacker-controlled and would put its
    # (sanitised) text back into the report that reaches the cron agent's context.
    return "invalid", f"{kind} failed validation (possible evidence tampering)"


def _load_promptfoo(path: Path) -> dict[str, Any] | None:
    """Parse a promptfoo `--output` JSON file, or None if absent/unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def summarise_shipped_metadata(path: Path | None) -> dict[str, Any]:
    """The shipped gate's fast pre-run, reduced to what belongs in a summary.

    This is EVIDENCE, not a gate — deliberately absent from ``_GATE_NAMES``. That
    list is fail-closed: a name in it that an evidence file does not carry reads
    as 127 and hard-fails the run, which is right for a gate an older Worker
    genuinely did not run, and wrong for a supplementary report. A Worker image
    without the pre-run must classify exactly as it did before.

    So an absent or unparseable file is ``present: False`` and nothing else. It
    never becomes "no findings" — the same refusal as the test count, where an
    unreadable log reads as unknown rather than as zero.
    """
    out: dict[str, Any] = {"present": False}
    if path is None or not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    if not isinstance(data, dict):
        return out
    findings = data.get("findings")
    yanked = data.get("yanked")
    out.update(
        {
            "present": True,
            "exit_code": data.get("exit_code"),
            "publication": data.get("publication"),
            "index_url": data.get("index_url"),
            "index_version": data.get("index_version"),
            "index_status": data.get("index_status"),
            "yank_source": data.get("yank_source"),
            "yanked": sorted(yanked) if isinstance(yanked, dict) else [],
            "findings": [
                {"code": f.get("code"), "severity": f.get("severity", "high")}
                for f in findings
                if isinstance(f, dict)
            ]
            if isinstance(findings, list)
            else [],
        }
    )
    return out


# Gate exit codes carried in a Worker evidence file (see nightly-audit.sh). The
# Worker ships raw evidence; the trusted Broker re-classifies from it, so a
# compromised Worker cannot forge a green verdict (Analysis S2).
#
# ADDING A GATE HERE IS A ROLLOUT STEP, NOT A COSMETIC ONE: an evidence file that
# does not carry a name listed here defaults to 127 (could-not-run) and the run
# HARD-FAILS. That is the intended fail-closed behaviour — a Worker image still
# running the previous nightly-audit.sh genuinely did not run the new gate, and
# must not be classified as green. Roll the Worker image and the Broker together.
_GATE_NAMES = (
    "ruff",
    "mypy",
    "pytest",
    "schema_drift",
    "promptfoo_rc",
    "transport_boot",
    "host_allowlist",
    "shipped_artifact",
    "lockfile",
)

# The DNS-rebinding gate is the one gate whose exit code is not binary. 3 means
# "the target has no inbound host allow-list configured" — the documented
# fail-open state of a non-loopback bind, which is neither a defect (nothing is
# broken) nor a pass (the control is absent). It gets its own field in the
# summary and its own block in the report, and it does NOT turn the run red.
REBIND_NOT_CONFIGURED = 3

# The transport boot gate has the same third state, for the same reason: 3 means
# "the gate never managed to ASK the entrypoint for that transport" (it exited
# cleanly without listening and no transport flag reached it). That is not a
# defect in the target and not a pass either — measured against a target whose
# HTTP transport is healthy but selected with a CLI flag the gate did not send.
BOOT_NOT_MEASURED = 3

# The lockfile gate's third state, and the one that carries the most weight of
# the three: 3 means the target ships no lockfile at all. Across this portfolio
# that is the COMMON answer — 19 of 20 servers commit none — and a library that
# ships no lock has made a defensible choice. Turning it red would mute the gate
# within a day and take the one real finding with it. So it is reported, in its
# own words, and it does not move the outcome.
LOCK_NOT_MEASURED = 3

# 4 is `probe_provenance`'s MOVED_DURING_RUN: HEAD or the working tree changed
# between the probe's first and last read, so the run reached no verdict. That is
# the harness's problem, never the target's, and it is classified with 126/127
# rather than as a finding — charging a defect to a repository on the strength of
# a run that did not read one tree is exactly the error the code exists to avoid.
LOCK_MOVED = 4


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
            "examples": []
            if promptfoo_rc == 0
            else ["promptfoo produced no output and exited non-zero"],
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


# The runner summary lines we can read a test count off. pytest -q ends with
# "217 passed, 3 skipped in 25.56s" (or "no tests ran in 0.01s"); unittest ends
# with "Ran 217 tests in 25.560s". Anything else leaves the count UNKNOWN rather
# than guessed — a wrong count here would either invent a failure or hide one.
_UNITTEST_RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_PYTEST_COLLECTED_RE = re.compile(r"^collected (\d+) items?", re.MULTILINE)
_PYTEST_NO_TESTS_RE = re.compile(r"no tests ran in ", re.MULTILINE)
# Outcomes that mean a test was actually reported on. `deselected`, `warnings`
# and `errors in` (collection errors) are deliberately absent: a suite whose every
# test was deselected executed nothing, which is precisely what we are looking for.
_PYTEST_OUTCOME_RE = re.compile(
    r"(\d+) (passed|failed|xfailed|xpassed|skipped|error|errors)\b"
)
_PYTEST_SUMMARY_RE = re.compile(
    r"^=+ .*\bin \d+\.\d+s.*=+$|^\d+ \w+.* in \d+\.\d+s", re.MULTILINE
)


def count_tests(text: str) -> int:
    """How many tests the runner reported on, or TESTS_UNKNOWN.

    Reads the LAST summary in the log: a gate may legitimately run the suite more
    than once, and it is the final word that describes what the exit code means.
    """
    ran = _UNITTEST_RAN_RE.findall(text or "")
    if ran:
        return int(ran[-1])

    # pytest, quiet mode: the final summary line carries the per-outcome counts.
    summaries = list(_PYTEST_SUMMARY_RE.finditer(text or ""))
    if summaries:
        last = (text or "")[summaries[-1].start() : summaries[-1].end()]
        total = sum(int(n) for n, _ in _PYTEST_OUTCOME_RE.findall(last))
        if total or _PYTEST_NO_TESTS_RE.search(last):
            return total
    if _PYTEST_NO_TESTS_RE.search(text or ""):
        return 0

    collected = _PYTEST_COLLECTED_RE.findall(text or "")
    if collected:
        return int(collected[-1])
    return TESTS_UNKNOWN


def _status(rc: int) -> str:
    if _hung(rc):
        return f"⏱ HUNG — killed by the gate timeout (exit {rc})"
    return "✅ pass" if rc == 0 else f"❌ fail (exit {rc})"


def _pytest_line(s: dict[str, Any]) -> str:
    """The pytest gate's line, with the suite size on it. A bare "✅ pass" cannot
    be told apart from a suite that collected nothing, so the count travels with
    the verdict — and when the count IS zero the tick is withdrawn, because
    "✅ pass — 0 test(s)" is the exact sentence this class exists to prevent."""
    rc = s["gates"]["pytest"]
    n = s.get("tests_collected", TESTS_UNKNOWN)
    if s.get("no_tests_executed"):
        return "🕳 0 tests executed (exit 0) — NOT a pass"
    if not isinstance(n, int) or n < 0:
        return f"{_status(rc)} — ⚠️ test count unknown"
    return f"{_status(rc)} — {n} test(s)"


def _boot_status(rc: int) -> str:
    """The boot gate's three-way rendering. "Not measured" must not wear a tick:
    the transport was never started, so nothing about it was established."""
    if rc == BOOT_NOT_MEASURED:
        return "🟡 transport not selected — not measured (exit 3)"
    return _status(rc)


def _lockfile_status(rc: int) -> str:
    """The lockfile gate's four-way rendering.

    "No lockfile" is the answer for 19 of the 20 servers in this portfolio, and
    it must not wear a tick: nothing was compared, so nothing was established
    about where those bounds are in force.
    """
    if rc == LOCK_NOT_MEASURED:
        return "🟡 no lockfile in the target — not measured (exit 3)"
    if rc == LOCK_MOVED:
        return "⛔ MOVED_DURING_RUN — the checkout changed under the probe (exit 4)"
    return _status(rc)


def _rebind_status(rc: int) -> str:
    """The rebinding gate's own three-way rendering. A control that is not
    configured must not read as "✅ pass" — the whole reason it is reported is
    that the attack is unopposed."""
    if rc == REBIND_NOT_CONFIGURED:
        return "🟡 control not configured (exit 3)"
    return _status(rc)


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    pf = _load_promptfoo(Path(args.promptfoo_json)) if args.promptfoo_json else None
    pfc = classify_promptfoo(pf, args.promptfoo_rc)
    shipped_metadata = summarise_shipped_metadata(
        Path(args.shipped_metadata_json) if args.shipped_metadata_json else None
    )

    # Validate the Worker-controlled metadata (S-D): a target/sha that is neither a
    # known sentinel nor pattern-matching is treated as tampering -> 'invalid' +
    # hard-fail, and never rendered raw into the report.
    target, target_err = _validate_meta(args.target, _TARGET_RE, "target")
    sha, sha_err = _validate_meta(args.sha, _SHA_RE, "target_sha")

    # --- gates that never finished -----------------------------------------
    # Collected FIRST, because a hung gate must not also be counted as a red one.
    # A timeout is not "ruff found problems"; it is "ruff never answered", and
    # folding it into the finding classes would put a defect claim in the report
    # that no gate ever made.
    hung_gates = [
        label
        for label, rc in (
            ("ruff", args.ruff),
            ("mypy", args.mypy),
            ("pytest", args.pytest),
            ("schema-drift gate", args.schema_drift),
            ("transport boot gate", args.transport_boot),
            ("DNS-rebinding gate", args.host_allowlist),
            ("shipped-artifact gate", args.shipped_artifact),
            ("lockfile gate", args.lockfile),
            ("promptfoo", args.promptfoo_rc),
        )
        if _hung(rc)
    ]
    hung = bool(hung_gates)

    # Schema drift = the deterministic schema gate diverged OR a promptfoo
    # is-json/contract assertion failed.
    schema_drift = (args.schema_drift != 0 and not _hung(args.schema_drift)) or pfc[
        "contract_failures"
    ] > 0
    redteam = pfc["redteam_hits"] > 0
    other_findings = pfc.get("other_failures", 0) > 0
    toolchain_fail = any(
        rc != 0 and not _hung(rc) for rc in (args.ruff, args.mypy, args.pytest)
    )
    # Transport boot: the target did not come up, or came up unusable, under at
    # least one configured transport. A server that will not start is a FINDING
    # about the target — only the harness failing to run (126/127, below) is a
    # hard failure. Keeping those two apart is the whole point of the gate.
    transport_boot_unmeasured = args.transport_boot == BOOT_NOT_MEASURED
    transport_boot_fail = args.transport_boot not in (
        0,
        BOOT_NOT_MEASURED,
    ) and not _hung(args.transport_boot)
    # The rebinding gate, three ways. Exit 3 is NOT a failure: a target with no
    # allow-list configured is in the documented fail-open state. It is surfaced
    # as its own flag so the report can say so out loud instead of leaving a
    # missing control to look like a passing one.
    # 126/127 are excluded here on purpose: those mean the harness never ran, and
    # "the control did not hold" is a claim we would then not have earned. They
    # land in hard_fail_reasons below instead.
    # The shipped-artifact gate: what users actually install. Binary — a stale or
    # absent release is a finding about the target, 126/127 is the harness.
    shipped_artifact_fail = args.shipped_artifact not in (0, 126, 127) and not _hung(
        args.shipped_artifact
    )
    # The lockfile gate, four ways. Only 2 is a finding: 3 is "no lockfile"
    # (surfaced on its own, never as a pass), 4 is a checkout that moved under
    # the probe, and 126/127 are the harness. See LOCK_NOT_MEASURED.
    lockfile_unmeasured = args.lockfile == LOCK_NOT_MEASURED
    lockfile_fail = args.lockfile not in (
        0,
        LOCK_NOT_MEASURED,
        LOCK_MOVED,
        126,
        127,
    ) and not _hung(args.lockfile)
    host_allowlist_unconfigured = args.host_allowlist == REBIND_NOT_CONFIGURED
    host_allowlist_fail = args.host_allowlist not in (
        0,
        REBIND_NOT_CONFIGURED,
        126,
        127,
    ) and not _hung(args.host_allowlist)

    # --- the silent zero ----------------------------------------------------
    # A green pytest gate that reported on no tests at all. The count comes from
    # the runner's own output, measured Worker-side (the log never reaches the
    # Broker) and shipped in the evidence.
    tests_collected = int(getattr(args, "tests_collected", TESTS_UNKNOWN))
    pytest_answered = args.pytest == 0 and not _hung(args.pytest)
    no_tests_executed = pytest_answered and tests_collected == 0
    tests_unverified = pytest_answered and tests_collected < 0

    # Hard failure (never silently downgraded to "passed"):
    #   * an audited gate could not run (missing bin / sync failure, rc 127/126);
    #   * promptfoo could not run at all; or
    #   * a model/provider was unresolvable (promptfoo errors > 0);
    #   * the evidence metadata failed validation (possible tampering, S-D).
    infra_codes = {126, 127}
    hard_fail_reasons: list[str] = []
    if target_err:
        hard_fail_reasons.append(target_err)
    if sha_err:
        hard_fail_reasons.append(sha_err)
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
        hard_fail_reasons.append(
            f"schema-drift gate could not run (exit {args.schema_drift})"
        )
    if args.transport_boot in infra_codes:
        hard_fail_reasons.append(
            f"transport boot gate could not run (exit {args.transport_boot}) — the harness "
            "itself failed; this says nothing about whether the target boots"
        )
    if args.host_allowlist in infra_codes:
        hard_fail_reasons.append(
            f"DNS-rebinding gate could not run (exit {args.host_allowlist}) — the harness "
            "itself failed; this says nothing about the target's host allow-list"
        )
    if args.shipped_artifact in infra_codes:
        hard_fail_reasons.append(
            f"shipped-artifact gate could not run (exit {args.shipped_artifact}) — most "
            "often an unreachable index. The published artifact was NOT compared, which "
            "is emphatically not 'in sync'"
        )
    if args.lockfile in infra_codes:
        hard_fail_reasons.append(
            f"lockfile gate could not run (exit {args.lockfile}) — the declared bound "
            "was NOT compared against the lock that installs, which is not the same as "
            "them agreeing"
        )
    if args.lockfile == LOCK_MOVED:
        hard_fail_reasons.append(
            "the lockfile gate reported MOVED_DURING_RUN (exit 4) — the checkout "
            "changed between its first and last read, so it produced no verdict. That "
            "is a harness problem, never a defect in the target"
        )
    if hung:
        # HARD failure, not a finding. A hung gate returned no verdict, and a
        # `findings` outcome would route it to a tracking issue asserting a defect
        # class nothing observed. It is also not merely "could not run": the gate
        # started, did work, and never came back — which is a real signal about
        # the target (an endless SSE stream, a deadlock, a suite waiting on a
        # client teardown) and belongs in front of the operator under its own name.
        hard_fail_reasons.append(
            "gate(s) HUNG and were killed by the timeout: "
            + ", ".join(hung_gates)
            + " — no verdict was produced. A hang is not infrastructure noise: twice "
            "now a real defect has surfaced as a hanging suite rather than a red one. "
            "Read that gate's log for where it stopped before re-running"
        )
    if no_tests_executed:
        hard_fail_reasons.append(
            "the pytest gate exited 0 but reported on 0 tests — an empty suite is not "
            "a pass. Check the test paths / selection in the target before reading "
            "anything else in this run as verified"
        )
    if tests_unverified:
        hard_fail_reasons.append(
            "the pytest gate exited 0 but no test count could be read from its output "
            "— a green result whose suite size is unknown cannot be distinguished from "
            "an empty one, so it is not treated as a pass (same rule as promptfoo's "
            "rc-0-with-no-output)"
        )

    hard_fail = bool(hard_fail_reasons)
    # An unconfigured control does NOT break green: it is the documented
    # fail-open state and the operator's decision, not a defect. It is loud in
    # the report instead — see the block render_report() adds for it.
    green = not (
        schema_drift
        or redteam
        or other_findings
        or toolchain_fail
        or transport_boot_fail
        or host_allowlist_fail
        or shipped_artifact_fail
        or lockfile_fail
        or hard_fail
    )  # transport_boot_unmeasured and lockfile_unmeasured deliberately absent:
    #    neither is a defect, see BOOT_NOT_MEASURED / LOCK_NOT_MEASURED

    if hard_fail:
        outcome, exit_code = "hard-fail", EXIT_HARD_FAIL
    elif green:
        outcome, exit_code = "green", EXIT_GREEN
    else:
        outcome, exit_code = "findings", EXIT_FINDINGS

    # Which promptfoo profile produced this verdict (Analysis T-C). A determ-only
    # run did NOT exercise the model-graded layer (llm-rubric + red-team), so a
    # green determ verdict must never be read as "red-team clear".
    profile = getattr(args, "promptfoo_profile", "") or "unknown"
    graded_layer_ran = profile in ("graded", "full")

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": target,
        "target_sha": sha,
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
        "transport_boot_fail": transport_boot_fail,
        "transport_boot_unmeasured": transport_boot_unmeasured,
        "host_allowlist_fail": host_allowlist_fail,
        "host_allowlist_unconfigured": host_allowlist_unconfigured,
        "shipped_artifact_fail": shipped_artifact_fail,
        "lockfile_fail": lockfile_fail,
        "lockfile_unmeasured": lockfile_unmeasured,
        # Reported, never decisive: `outcome` above is unchanged by this block.
        # The pre-run is a second probe, not the shipped gate's verdict, and
        # letting it move the outcome would be the "a hung gate became a
        # finding" substitution from the other direction.
        "shipped_metadata": shipped_metadata,
        "hung": hung,
        "hung_gates": hung_gates,
        "no_tests_executed": no_tests_executed,
        "tests_unverified": tests_unverified,
        "tests_collected": tests_collected,
        "gates": {
            "ruff": args.ruff,
            "mypy": args.mypy,
            "pytest": args.pytest,
            "schema_drift_gate": args.schema_drift,
            "promptfoo_rc": args.promptfoo_rc,
            "transport_boot_gate": args.transport_boot,
            "host_allowlist_gate": args.host_allowlist,
            "shipped_artifact_gate": args.shipped_artifact,
            "lockfile_gate": args.lockfile,
        },
        "promptfoo": pfc,
    }


def _shipped_metadata_line(s: dict[str, Any]) -> str:
    """One line for the pre-run, and it has to survive the gate above it hanging.

    That is the whole reason the pre-run exists: the full gate is killed before
    it writes anything when it exhausts its budget, leaving `rc=124` and no
    report on the one gate that knows whether users are installing a withdrawn
    release. This line is where that verdict becomes visible to someone reading
    the report rather than the Worker's log directory.
    """
    m = s.get("shipped_metadata") or {}
    if not m.get("present"):
        # Not "clean" — unknown. A Worker image predating the pre-run reaches
        # here, and so does one whose pre-run was itself killed.
        return "no report (not run, or it did not finish) — **unknown**, not clean"

    bits: list[str] = []
    version = m.get("index_version")
    bits.append(f"index serves `{version}`" if version else "no version from the index")
    if m.get("index_status") == "unconfirmed":
        bits.append(
            "index APIs **disagree** (UNCONFIRMED — not a finding, read the log)"
        )
    if m.get("yanked"):
        bits.append(f"{len(m['yanked'])} yanked release(s): {', '.join(m['yanked'])}")

    codes = [f["code"] for f in m.get("findings", []) if f.get("code")]
    if codes:
        bits.append(f"🚨 **{', '.join(codes)}**")
    else:
        bits.append("no metadata findings")

    # When the gate above produced nothing, name precisely which half of the
    # question survived. "The release is fine" and "the artifact runs" are two
    # claims, and only the first one was earned here.
    if s["gates"]["shipped_artifact_gate"] not in (0, 2) or _hung(
        s["gates"]["shipped_artifact_gate"]
    ):
        bits.append(
            "the gate itself returned no verdict, so what is still UNKNOWN is "
            "whether the installed artifact starts and answers"
        )
    return " · ".join(bits)


def render_report(s: dict[str, Any]) -> str:
    icon = {"green": "✅", "findings": "🚨", "hard-fail": "⛔"}[s["outcome"]]
    head = {
        "green": "All gates green — no schema drift, no red-team hit.",
        "findings": "Findings detected — see below.",
        "hard-fail": "HARD FAILURE — the audit could not complete. Do NOT treat as passed.",
    }[s["outcome"]]
    # Both of these are hard failures, but "could not complete" is the wrong
    # sentence for either: one gate ran forever, the other ran nothing. Say which.
    if s.get("hung"):
        head = (
            "HARD FAILURE — "
            + ", ".join(s.get("hung_gates") or ["a gate"])
            + " HUNG and had to be killed. No verdict was produced. Do NOT treat as passed."
        )
    elif s.get("no_tests_executed"):
        head = (
            "HARD FAILURE — the test suite executed 0 tests and still exited 0. "
            "An empty suite is not a pass. Do NOT treat as passed."
        )
    # A green run that ships an unconfigured control must say so in the same
    # breath. "All gates green" on its own is the sentence a reader stops at.
    if s["outcome"] == "green" and s.get("host_allowlist_unconfigured"):
        head = (
            "All gates green — but the inbound host allow-list is NOT configured "
            "(see below). Green here means nothing was found broken, not that "
            "the DNS-rebinding attack is opposed."
        )

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
        f"- pytest: {_pytest_line(s)}",
        f"- schema-drift gate: {_status(s['gates']['schema_drift_gate'])}",
        f"- transport boot gate (initialize + tools/list): "
        f"{_boot_status(s['gates']['transport_boot_gate'])}",
        f"- DNS-rebinding gate (inbound Host/Origin allow-list): "
        f"{_rebind_status(s['gates']['host_allowlist_gate'])}",
        f"- shipped-artifact gate (install from PyPI + run it): "
        f"{_status(s['gates']['shipped_artifact_gate'])}",
        f"- lockfile gate (declared bound vs. the lock that installs): "
        f"{_lockfile_status(s['gates']['lockfile_gate'])}",
        f"  - release metadata pre-run: {_shipped_metadata_line(s)}",
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

    # Hung gates get their own named block ahead of the generic hard-failure list.
    # "Which gate hung" is the entire actionable content — pytest hanging and
    # promptfoo hanging call for different next steps, and a timeout buried in a
    # list of infrastructure reasons is how this class of finding got lost before.
    if s.get("hung"):
        lines += [
            "",
            "## ⏱ Gate(s) HUNG — no verdict produced",
            "",
            "Killed by the gate timeout after producing no result:",
        ]
        lines += [f"- **{_clean_inline(g, 60)}**" for g in (s.get("hung_gates") or [])]
        lines += [
            "",
            "A hang is **not** infrastructure noise and is not the same as a gate that "
            "could not start. The gate ran, did work, and never returned — which has "
            "twice been how a real defect showed itself (a control removed, an SSE "
            "stream left open under a foreign `Host`, the test client then waiting on "
            "teardown). Read that gate's log for where it stopped; do not re-run and "
            "call the second attempt the answer.",
        ]

    if s.get("no_tests_executed") or s.get("tests_unverified"):
        lines += ["", "## 🕳 No tests executed"]
        if s.get("no_tests_executed"):
            lines += [
                "",
                "The pytest gate exited **0** and reported on **0 tests**. That is not a "
                "pass — it is a gate that made no statement while looking exactly like "
                "one that did. Check the target's test paths and selection before "
                "reading anything else in this run as verified.",
            ]
        else:
            lines += [
                "",
                "The pytest gate exited **0** but no test count could be read from its "
                "output, so a real suite cannot be told apart from an empty one. Treated "
                "as unverified rather than as a pass, on the same rule as promptfoo "
                "returning rc 0 with no parseable output.",
            ]

    if s["hard_fail"]:
        lines += ["", "## ⛔ Hard failure"]
        lines += [f"- {r}" for r in s["hard_fail_reasons"]]
        # "Re-run" is the wrong advice for a hang: the second attempt is not the
        # answer, and a run that passes on retry is how an intermittent deadlock
        # gets talked out of the record.
        closing = (
            "The run is **not** a pass. No green claim is made (SOUL.md). "
            "Resolve the model/provider or the broken gate and re-run."
        )
        if s.get("hung"):
            closing = (
                "The run is **not** a pass. No green claim is made (SOUL.md). "
                "Find out *where* the gate stopped before re-running — a hang "
                "that disappears on the second attempt has not been explained."
            )
        elif s.get("no_tests_executed") or s.get("tests_unverified"):
            closing = (
                "The run is **not** a pass. No green claim is made (SOUL.md). "
                "Fix the suite's selection so it actually runs, then re-run."
            )
        lines += ["", closing]

    pf = s["promptfoo"]
    findings: list[str] = []
    if s["schema_drift"]:
        findings.append(
            "**Schema drift** — committed schema / tool-output contract diverged."
        )
    if s["redteam"]:
        findings.append(
            f"**Red-team hit** — {pf['redteam_hits']} adversarial case(s) succeeded against the surface."
        )
    if s.get("other_findings"):
        findings.append(
            f"**Other promptfoo failure(s)** — {pf.get('other_failures', 0)} case(s) failed but "
            "matched neither the schema/contract nor the red-team class (see detail)."
        )
    if s.get("transport_boot_fail"):
        findings.append(
            "**Transport boot failure** — the target did not come up, or came up unusable, "
            "under at least one configured transport (no `initialize` / `tools/list` answer). "
            "Unit tests, ruff and the schema gate are all blind to this: the two known shapes "
            "are a crash at start (a read-only settings object) and an HTTP 421 for every "
            "request under a real hostname (the host was not passed to the app builder). "
            "See the Worker's `transport-boot.log` / `transport-boot.json` for which transport "
            "and which of the two."
        )
    if s.get("host_allowlist_fail"):
        findings.append(
            "**DNS-rebinding control failed** — the target's HTTP transport did not hold "
            "the inbound host allow-list the gate configured. Either a foreign `Host`, the "
            "allowed hostname on a wrong port, or a foreign `Origin` was served; or a VALID "
            "auth token walked past a check that held without one; or the result could not "
            "be attributed to the configured list at all. CORS does not answer this attack "
            "(after the rebind the browser sees same-origin) and neither does a token (the "
            "attacking page holds one). See the Worker's `rebind.log` / `rebind.json` for "
            "which probe and which pass."
        )
    # The pre-run's reason for existing, said where it is read. When the shipped
    # gate produced no verdict at all — hung, or could not run — the metadata
    # half may still have completed, and that half is the one that knows about a
    # withdrawn release. Surfaced as its own line rather than folded into
    # `findings`: the outcome stays whatever the gate's own exit code made it.
    _m = s.get("shipped_metadata") or {}
    _shipped_silent = s["gates"]["shipped_artifact_gate"] not in (0, 2) or _hung(
        s["gates"]["shipped_artifact_gate"]
    )
    _codes = [f["code"] for f in _m.get("findings", []) if f.get("code")]
    # Only a real finding earns a place under "Findings". A pre-run that found
    # nothing still has something worth saying when the gate above it went
    # silent, but saying it here would put "nothing is wrong" under a 🚨 heading
    # — `_shipped_metadata_line` carries that case instead.
    if _shipped_silent and _m.get("present") and _codes:
        findings.append(
            "**The shipped-artifact gate returned no verdict, but its metadata "
            f"pre-run did** — and it found {', '.join(_codes)}. That part is "
            "established: the release comparison completed."
        )

    if s.get("shipped_artifact_fail"):
        findings.append(
            "**Shipped artifact diverges** — the package users actually install is not "
            "the repository. Either it is absent from the index entirely (no publish "
            "process to repair — one to create), or it is behind `main`/the last tag "
            "(the process exists and did not fire; look at the publish WORKFLOW RUN), "
            "or the installed server did not start or answered a tool call with "
            "nothing. Green CI is not shipped software: the case this gate exists for "
            "ran green every night while PyPI served a release with three broken "
            "tools. See the Worker's `shipped.log` / `shipped.json` for which."
        )
    if s.get("lockfile_fail"):
        findings.append(
            "**The declared dependency bound is not in force where the install "
            "happens** — `pyproject.toml` and the lockfile the deployment syncs "
            "from disagree. The lock may record an older specifier (the bound was "
            "merged and the lock never regenerated), pin a version the declaration "
            "does not admit, or be flatly out of date by the resolver's own check. "
            "Every other gate reads `pyproject.toml` and is blind to this: "
            "swiss-procurement-mcp #37 merged the upper bounds and left `uv.lock` "
            "untouched, and for six hours `main` carried the fix in the file "
            "everybody reads and the open range in the file that installs. See the "
            "Worker's `lockfile.log` / `lockfile.json` for which dependency and "
            "which two specifiers."
        )
    if s["toolchain_fail"]:
        findings.append(
            "**Toolchain failure** — ruff/mypy/pytest is red (see gates above)."
        )
    if findings:
        lines += ["", "## 🚨 Findings"]
        lines += [f"- {f}" for f in findings]

    if s.get("transport_boot_unmeasured"):
        lines += [
            "",
            "## 🟡 Transport not selected — the gate never got to ask",
            "",
            "The target's entrypoint exited cleanly without listening, and no "
            "transport flag got it to serve. The gate requests a transport through "
            "env vars (`MCP_TRANSPORT`/`FASTMCP_TRANSPORT`/`PORT`); an entrypoint "
            "that selects it with its own CLI flag simply runs its default and "
            "finishes. **This says nothing about whether that transport works** — "
            "it says the gate could not start it. Declare the exact argv in the "
            "target's `pyproject.toml` under `[tool.mcp_auditor.boot.commands]` "
            "and the check becomes real again. See the Worker's "
            "`transport-boot.json` for which transport.",
        ]

    if s.get("lockfile_unmeasured"):
        lines += [
            "",
            "## 🟡 No lockfile — the gate had nothing to compare",
            "",
            "The target commits neither `uv.lock` nor `poetry.lock`, so the bound in "
            "`pyproject.toml` is the only declaration there is and nothing can have "
            "drifted from it. That is a defensible choice for a library, and it is "
            "why this state does not turn the run red — 19 of the 20 servers in this "
            "portfolio are in it, and a gate that went red on all of them would be "
            "switched off within a day. **It is also not a pass:** this run says "
            "nothing about how that target is actually installed. A lock nobody "
            "syncs from and no lock at all look identical from here.",
        ]

    # Its own block, deliberately outside "Findings" and outside the gate list's
    # tick marks: not a defect, not a pass, and never invisible.
    if s.get("host_allowlist_unconfigured"):
        lines += [
            "",
            "## 🟡 Control not configured — inbound host allow-list",
            "",
            "The target honours no inbound `Host`/`Origin` allow-list, so on a "
            "non-loopback bind it rejects nothing. This is the **documented "
            "fail-open state**, not a bug: guessing an allow-list on `0.0.0.0` "
            "would reject the very deployment it is meant to protect, so these "
            "servers ship the check off until an operator configures it.",
            "",
            "It is reported because the absence is the point. A page in the "
            "operator's network can resolve its own hostname to this server and "
            "talk to it from the browser; CORS sees same-origin after the rebind "
            "and an auth token is already held by the attacking context. Nothing "
            "in this run should be read as evidence that the attack is opposed.",
        ]

    if pf.get("examples"):
        lines += ["", "## promptfoo detail"]
        # Examples embed upstream API payloads (untrusted) — strip control chars /
        # newlines so they cannot inject structure into the report sink (S-D).
        lines += [f"- {_clean_inline(e)}" for e in pf["examples"]]

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
    p.add_argument(
        "--transport-boot",
        type=int,
        dest="transport_boot",
        help="transport boot gate exit code (0 green / 2 target does not "
        "boot / 3 the transport could not be selected, so nothing was "
        "measured / 127 the harness could not run)",
    )
    p.add_argument(
        "--host-allowlist",
        type=int,
        dest="host_allowlist",
        help="DNS-rebinding gate exit code (0 the control is enforced / "
        "2 finding / 3 the control is NOT CONFIGURED — neither a pass "
        "nor a failure / 127 the harness could not run)",
    )
    p.add_argument("--promptfoo-rc", type=int, dest="promptfoo_rc")
    p.add_argument(
        "--shipped-artifact",
        type=int,
        dest="shipped_artifact",
        help="shipped-artifact gate exit code (0 the published package "
        "matches and runs / 2 absent, stale, or it does not run / "
        "127 the harness could not run, e.g. an unreachable index)",
    )
    p.add_argument(
        "--lockfile",
        type=int,
        dest="lockfile",
        help="lockfile gate exit code (0 the lock states what pyproject states / "
        "2 LOCK_DRIFT / LOCK_UNSATISFIED / LOCK_STALE / 3 the target ships no "
        "lockfile — NOT MEASURED, neither a pass nor a finding / 4 the checkout "
        "moved during the run / 127 the harness could not run)",
    )
    p.add_argument(
        "--tests-collected",
        type=int,
        dest="tests_collected",
        default=TESTS_UNKNOWN,
        help="how many tests the pytest gate reported on (-1 = could not be "
        "determined). A green gate with 0 is NOT a pass",
    )
    p.add_argument(
        "--count-tests",
        default="",
        dest="count_tests",
        help="read a runner log and print the test count, then exit. This is "
        "how nightly-audit.sh measures the number it ships in the "
        "evidence — the log itself never reaches the Broker",
    )
    p.add_argument(
        "--from-evidence",
        default="",
        dest="from_evidence",
        help="read gate exit codes (+ target/sha) from a Worker evidence JSON",
    )
    p.add_argument(
        "--shipped-metadata-json",
        default="",
        dest="shipped_metadata_json",
        help="the shipped gate's --metadata-only pre-run report. Evidence, "
        "not a gate: it is reported and never changes the outcome, and "
        "an absent file reads as unknown rather than as clean",
    )
    p.add_argument("--promptfoo-json", default="", dest="promptfoo_json")
    p.add_argument(
        "--promptfoo-profile",
        default="",
        dest="promptfoo_profile",
        help="which promptfoo profile ran (determ|graded|full); stamped into the summary",
    )
    p.add_argument("--target", default="")
    p.add_argument("--sha", default="unknown")
    # Not `required=True`: --count-tests is a measurement mode that writes nothing.
    p.add_argument("--out-report", default="", dest="out_report")
    p.add_argument("--out-summary", default="", dest="out_summary")
    args = p.parse_args()

    if args.count_tests:
        try:
            text = Path(args.count_tests).read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable log -> UNKNOWN, never 0. Reporting "no tests" because we
            # could not open the file would invent the very finding we are hunting.
            print(TESTS_UNKNOWN)
            return 0
        print(count_tests(text))
        return 0

    for flag in ("out_report", "out_summary"):
        if not getattr(args, flag):
            p.error(f"--{flag.replace('_', '-')} is required")

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
        # The runner log stays on the Worker, so the COUNT travels in the evidence.
        # Absent or unparseable reads as UNKNOWN, which is not a pass — a Worker
        # that ships no count cannot have a green pytest verdict believed.
        try:
            args.tests_collected = int(ev["tests_collected"])
        except (KeyError, TypeError, ValueError):
            args.tests_collected = TESTS_UNKNOWN
    else:
        missing = [
            f"--{n.replace('_', '-')}" for n in _GATE_NAMES if getattr(args, n) is None
        ]
        if missing:
            p.error(
                "missing gate flags: "
                + ", ".join(missing)
                + " (or pass --from-evidence)"
            )
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
