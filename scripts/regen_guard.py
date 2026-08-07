#!/usr/bin/env python3
"""Classify one scheduled red-team regeneration run — three states, not two.

`redteam-regen.yml.template` runs on `schedule` and nothing else. That gives it
the same missing addressee `live-probe.yml.template` had before
`scripts/drift_issue.py`: a red weekly run appears in no pull request, and the
job summary is seen by whoever opens the run, which for a cron is nobody. The
observed cost is not hypothetical — a scheduled guard in this portfolio was red
across six consecutive merges and was never answered.

This script does not route the issue; `drift_issue.py` does that, and it is
already tested. What this file owns is the judgement that has to happen first:
did this run's regeneration actually take place, and what did it produce? It
emits the `alert=` verdict and the Markdown report that `drift_issue.py`
consumes as one probe.

THE THIRD STATE, AGAIN
----------------------
The states are the portfolio's (docs/probes/README.md), applied to a delivery
pipeline instead of a data source:

    finding   the regeneration was attempted and did not deliver  -> issue
    clear     it ran and the committed set is accounted for       -> close
    unknown   it never got far enough to say either               -> touch nothing

`unknown` is the state a template needs most. `redteam-regen` cannot run
without an attacker-model key, and most repositories that copy this template
will not have configured one on day one. Folding that into `finding` opens a
ticket in every such repository for a workflow nobody switched on yet; folding
it into `clear` closes a real, open ticket on a run that generated nothing. Both
end the same way — the guard gets switched off — so the missing key is its own
state and moves nothing in either direction.

The same applies to a cancelled run and to a step that never started. What
separates them from a genuine finding is not severity, it is whether an attempt
was made at all.

WHY A FAILED REGENERATION IS A FINDING AND NOT UNKNOWN
-----------------------------------------------------
The question this guard asks is "is the committed adversarial set still being
refreshed?". A `generate.sh` that ran and exited non-zero has answered it: no.
That the cause may be an upstream outage does not make the answer less true —
the set is going stale either way, and going stale silently is precisely the
failure the guard exists to surface. `unknown` is reserved for runs where the
question was never put, not for runs whose answer is unwelcome.

The mirror case is the reason `artifact_ok` exists at all. A `generate.sh` that
exits 0 and writes nothing has claimed success without producing the artifact
the graded CI job evaluates. Trusting the exit code alone would book that as
`clear` and close the issue — a green regeneration with no output is the exact
shape of a silent failure, so the artifact is checked rather than assumed.

WHY THIS IS A SCRIPT AND NOT A HEREDOC
--------------------------------------
Catalogued as `OPS-008` in the audit skill: `echo` and a tool call may live in
YAML, an `if` that decides red or green may not. Inline logic runs only inside a
workflow, so it cannot be unit-tested, and what cannot be unit-tested cannot be
mutation-tested. That is not a style preference here — the two-state collapse in
`live-probe.yml.template` sat unexamined in inline YAML for the life of the
file, and became visible on the day it moved into a script.

Stdlib only, matching drift_issue.py. No network, no token: this file decides,
and `drift_issue.py` acts.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NamedTuple

# The three states, named rather than stringly-typed at the call sites so that a
# typo is a NameError instead of a branch that is silently never taken.
FINDING = "finding"
CLEAR = "clear"
UNKNOWN = "unknown"

# GitHub step outcomes meaning the step executed and completed. Everything else
# — `skipped`, `cancelled`, and the empty string a step that never started
# reports — means no attempt was made. `failure` is deliberately absent: it is
# an attempt, and attempts are judged, not excused.
_COMPLETED = "success"
_ATTEMPTED = frozenset({"success", "failure"})


class Run(NamedTuple):
    """One regeneration run, as the workflow observed it.

    `have_key` is whether the attacker-model credential was configured at all;
    it is a fact the workflow reads from its own secrets, not a judgement.
    `generate` and `pull_request` are GitHub step outcomes. `artifact_ok` is
    whether the committed red-team file exists with content after the run — the
    one signal an exit code cannot forge.
    """

    have_key: bool
    generate: str
    pull_request: str
    artifact_ok: bool


def attempted(outcome: str) -> bool:
    """Pure: did this step actually run to a verdict of its own?

    `skipped` is the one worth stating out loud. A skipped step and a passing
    step are indistinguishable in a job's green checkmark, and every state this
    guard gets wrong gets wrong there first.
    """
    return outcome in _ATTEMPTED


def classify(run: Run) -> str:
    """Pure: FINDING / CLEAR / UNKNOWN for one run.

    The credential check is written first because without a key nothing
    downstream carries information: `generate` will be `skipped`, and the
    artifact will be whatever was committed upstream. That is a reason for the
    reader, not a guarantee — swapping it with the `attempted(generate)` test
    below changes no output, since both return UNKNOWN. The mutation survives
    the suite and is recorded as equivalent in `tests/test_regen_guard.py`
    rather than covered by a test that would only look like it covered it.
    """
    if not run.have_key:
        return UNKNOWN
    if not attempted(run.generate):
        return UNKNOWN
    if run.generate != _COMPLETED:
        return FINDING
    if not run.artifact_ok:
        # Exit 0 with nothing on disk. The graded CI job evaluates this file; if
        # it is missing, the regeneration delivered nothing whatever the shell
        # reported.
        return FINDING
    if not attempted(run.pull_request):
        # Cancelled, or never started. The regeneration itself succeeded and the
        # artifact is on disk; what nobody observed is whether it reached
        # review. Returning FINDING here would be the two-state reflex applied
        # one level down — an unwatched half of the run reported as a defect.
        #
        # This branch was dead when first written: it returned FINDING, which
        # the `!= _COMPLETED` test below already covers, so removing it changed
        # nothing and the mutation survived. The survivor was the signal that
        # the state was wrong, not that the test was missing.
        return UNKNOWN
    if run.pull_request != _COMPLETED:
        # It ran and failed: the regenerated cases exist and reach no human.
        return FINDING
    return CLEAR


def render(run: Run, state: str, *, artifact: str) -> str:
    """Markdown body for the tracking issue, or the closing comment.

    Every branch names what was observed, not only the verdict. A guard that
    reports a state without its evidence gets clicked away at portfolio scale —
    `published_probe` lost 38 of 42 reports to exactly that before its negative
    statuses started carrying their observation.
    """
    lines = [
        "The weekly red-team regeneration is the only thing keeping the "
        "committed adversarial set from going stale. It runs on `schedule`, so "
        "this issue is its addressee — a red cron reaches nobody otherwise.",
        "",
        "| signal | observed |",
        "| --- | --- |",
        f"| attacker key configured | `{'yes' if run.have_key else 'no'}` |",
        f"| `generate.sh` step | `{run.generate or '(never started)'}` |",
        f"| pull-request step | `{run.pull_request or '(never started)'}` |",
        f"| `{artifact}` | `{'present, non-empty' if run.artifact_ok else 'missing or empty'}` |",
        "",
    ]
    if state == FINDING:
        lines += [
            "**The regeneration did not deliver.** The graded CI job keeps "
            "evaluating whatever cases are currently committed, so nothing has "
            "turned red — the set has simply stopped growing, which is the "
            "failure mode this guard exists to make visible.",
            "",
            "Re-run the workflow from the Actions tab once the cause is fixed; "
            "this issue closes itself on the next run that completes.",
        ]
    elif state == CLEAR:
        lines += [
            "The regeneration ran and the committed set is accounted for.",
        ]
    else:
        lines += [
            "**Not measured.** This run never got far enough to say whether the "
            "set is being refreshed, so it changes nothing in either direction "
            "— no issue opened, none closed.",
        ]
    return "\n".join(lines) + "\n"


def artifact_present(path: Path) -> bool:
    """Pure enough: does the generated file exist with content?

    An unreadable file counts as absent. The alternative is to let an OSError
    propagate out of a guard whose entire job is to keep running when things
    around it break.
    """
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _emit(state: str) -> None:
    """Publish `alert=` and `state=` for the routing step.

    `alert` is what `drift_issue.py` reads as one probe's verdict, and it is
    written ONLY for the two states that made an observation. On `unknown` it is
    deliberately absent: `drift_issue.probe_ran()` requires an explicit
    `true`/`false`, so an absent verdict is classified as "did not run" by the
    router's own tested rule rather than by a second copy of it here.
    """
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    try:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"state={state}\n")
            if state == FINDING:
                fh.write("alert=true\n")
            elif state == CLEAR:
                fh.write("alert=false\n")
    except OSError as exc:  # pragma: no cover - CI filesystem only
        print(f"warning: could not write GITHUB_OUTPUT: {exc}", file=sys.stderr)


def _truthy(value: str) -> bool:
    """GitHub renders a boolean expression as the string `true`.

    Anything else — `false`, the empty string a missing secret produces, or a
    value some future workflow edit passes through unquoted — is not a key.
    """
    return value.strip().lower() == "true"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Classify a red-team regeneration run (finding/clear/unknown)."
    )
    p.add_argument(
        "--have-key",
        default="",
        help="`true` if the attacker-model credential is configured",
    )
    p.add_argument("--generate-outcome", default="", help="step outcome of generate.sh")
    p.add_argument(
        "--pr-outcome", default="", help="step outcome of the pull-request step"
    )
    p.add_argument(
        "--artifact",
        default="promptfoo/redteam/redteam.generated.yaml",
        help="the committed red-team set the graded job evaluates",
    )
    p.add_argument("--out", required=True, help="write the Markdown report here")
    args = p.parse_args(argv)

    run = Run(
        have_key=_truthy(args.have_key),
        generate=args.generate_outcome.strip(),
        pull_request=args.pr_outcome.strip(),
        artifact_ok=artifact_present(Path(args.artifact)),
    )
    state = classify(run)
    body = render(run, state, artifact=args.artifact)

    # UNKNOWN writes no report on purpose. `drift_issue.py` treats a missing or
    # empty report as one more reason the probe did not run, so the two halves
    # agree without either trusting the other.
    if state == UNKNOWN:
        print(
            "state: unknown — the regeneration was never attempted, so this run "
            "compared nothing. Leaving any tracking issue exactly as it is."
        )
    else:
        try:
            Path(args.out).write_text(body, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot write {args.out}: {exc}", file=sys.stderr)
            return 1
        print(f"state: {state}")
    print(body)
    _emit(state)
    # Never fails the cron. A red scheduled run with nobody watching is the
    # problem this file exists to solve, so it must not become one.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
