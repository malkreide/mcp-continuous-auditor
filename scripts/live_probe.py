#!/usr/bin/env python3
"""Weekly live-probe drift detector (Plan Phase 2c).

Fetch the REAL Zürich endpoints once, compare the *structure* of each response
to its recorded fixture, and report drift. This is the ONLY job allowed to talk
to the live municipal APIs — everything else stays offline.

We compare **structural signatures** (the set of JSON paths and their value
types), NOT values. Live sensor readings, dates and ids change constantly; only
an added / removed / re-typed field is real schema drift. That keeps the weekly
diff signal-rich and false-positive-free.

RECALL FLOORS — the one place values do matter. Ignoring values is right for a
timestamp and wrong for a hit count: an endpoint that silently starts returning
one record instead of twenty has identical structure, so the signature diff is
empty and the probe stays green. That is exactly how termdat-mcp#11 went
unnoticed — an omitted filter parameter restricted the upstream search to one of
23 classifications, and every response was still a well-formed list.

A probe may therefore declare ``min_count`` (and optionally ``count_path``, a
dot path to the countable collection; inferred when omitted). Falling below the
floor is reported separately from schema drift, because the remedy differs:
drift means update the fixture, a recall drop means find out what shrank.

Set floors generously below the observed value — roughly half. The floor exists
to catch a collapse, not to track normal corpus churn; a check that cries wolf
gets muted, and a muted check catches nothing.

Probes are declared in scripts/live_probe.manifest.json. Each probe names the
fixture (under promptfoo/fixtures/) it must stay structurally compatible with.

Exit code is always 0 (a flaky endpoint must not fail the cron); findings are
signalled out-of-band so the workflow decides whether to open an issue:

  * writes a Markdown report to $DRIFT_REPORT        (default: live-probe-report.md)
  * appends `drift=true|false` to $GITHUB_OUTPUT      (schema drift only)
  * appends `recall_drop=true|false`                 (a floor was breached)
  * appends `alert=true|false`                       (either of the two — use this)

Stdlib only (urllib) — no third-party deps, so the probe runs anywhere.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_provenance  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = Path(__file__).resolve().parent / "live_probe.manifest.json"
_FIXTURES = Path(os.environ.get("MCP_FIXTURES_DIR", _ROOT / "promptfoo" / "fixtures"))
_TIMEOUT = int(os.environ.get("LIVE_PROBE_TIMEOUT", "30"))
_USER_AGENT = "mcp-continuous-auditor live-probe (+https://github.com/malkreide/mcp-continuous-auditor)"


def structural_signature(obj: Any, path: str = "$") -> set[str]:
    """Collapse a JSON value to a set of `path:type` markers (array-index agnostic)."""
    sig: set[str] = set()
    if isinstance(obj, dict):
        for key in obj:
            sig |= structural_signature(obj[key], f"{path}.{key}")
    elif isinstance(obj, list):
        sig.add(f"{path}[]:array")
        for item in obj:  # merge every element under one index-agnostic path
            sig |= structural_signature(item, f"{path}[]")
    elif isinstance(obj, bool):
        sig.add(f"{path}:bool")
    elif isinstance(obj, (int, float)):
        sig.add(f"{path}:number")
    elif obj is None:
        sig.add(f"{path}:null")
    else:
        sig.add(f"{path}:string")
    return sig


# Collection keys worth counting when a probe declares `min_count` without a
# `count_path`. Ordered: the first one that resolves to a list wins.
_COUNT_KEYS: tuple[tuple[str, ...], ...] = (
    ("result", "records"),   # CKAN datastore_search / _sql
    ("features",),           # GeoJSON FeatureCollection
    ("results",),
    ("records",),
    ("entries",),
    ("data",),
    ("items",),
)


def _resolve_path(payload: Any, path: tuple[str, ...]) -> Any:
    """Walk a dot path through nested dicts. Returns None if any segment is missing."""
    node = payload
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node


def count_records(payload: Any, count_path: str | None = None) -> int | None:
    """Number of records in a response, or None when it cannot be determined.

    With `count_path` the answer is explicit and a miss is an error the caller
    should surface — a silently unresolvable path would read as "no floor set".
    Without it we try the well-known collection keys, then fall back to a
    top-level list.
    """
    if count_path:
        node = _resolve_path(payload, tuple(count_path.split(".")))
        return len(node) if isinstance(node, (list, dict)) else None

    if isinstance(payload, list):
        return len(payload)
    for keys in _COUNT_KEYS:
        node = _resolve_path(payload, keys)
        if isinstance(node, list):
            return len(node)
    return None


def _fetch(probe: dict) -> Any:
    url = probe["url"]
    method = probe.get("method", "GET").upper()
    body = probe.get("body")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (fixed manifest URLs)
        return json.loads(resp.read().decode("utf-8"))


def _load_fixture(name: str) -> Any:
    fname = name if name.endswith(".json") else f"{name}.json"
    payload = json.loads((_FIXTURES / fname).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload.pop("_comment", None)  # housekeeping key, not part of the contract
    return payload


def main() -> int:
    # "The live probe was green" is a different claim depending on which
    # revision of the manifest and which fixtures it walked. Both live in this
    # repository, so this is the state the report is about.
    prov = probe_provenance.capture_auditor()
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    probes = manifest["probes"] if isinstance(manifest, dict) else manifest

    drift_rows: list[str] = []
    recall_rows: list[str] = []
    error_rows: list[str] = []
    ok_rows: list[str] = []

    for probe in probes:
        name = probe["name"]
        fixture = probe["fixture"]
        try:
            expected = structural_signature(_load_fixture(fixture))
            payload = _fetch(probe)
            live = structural_signature(payload)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            error_rows.append(f"- ⚠️ `{name}`: probe failed — `{type(exc).__name__}: {exc}`")
            print(f"::warning title=live-probe::{name} failed: {exc}", file=sys.stderr)
            continue

        # Recall floor — independent of the structural diff. A collapsed result
        # set keeps the same JSON shape, so the signature comparison cannot see it.
        floor = probe.get("min_count")
        if floor is not None:
            count = count_records(payload, probe.get("count_path"))
            if count is None:
                where = probe.get("count_path") or "inferred collection key"
                error_rows.append(
                    f"- ⚠️ `{name}`: `min_count` set but no countable collection "
                    f"found ({where}) — fix `count_path` in the manifest."
                )
                print(f"::warning title=live-probe::{name}: uncountable payload", file=sys.stderr)
            elif count < floor:
                recall_rows.append(
                    f"- 📉 `{name}`: **{count}** record(s), floor is **{floor}**. "
                    "The response shape is unchanged, so this is not schema drift — "
                    "something narrowed the result set. Check whether an optional "
                    "filter/scope parameter changed its upstream default."
                )
            else:
                ok_rows.append(f"- ✅ `{name}` — recall {count} ≥ floor {floor}")

        added = sorted(live - expected)
        removed = sorted(expected - live)
        if added or removed:
            lines = [f"### `{name}` (fixture: `{fixture}`)"]
            if removed:
                lines.append("**Missing in live (fixture has, endpoint dropped):**")
                lines += [f"- `{p}`" for p in removed]
            if added:
                lines.append("**New in live (endpoint added, fixture lacks):**")
                lines += [f"- `{p}`" for p in added]
            drift_rows.append("\n".join(lines))
        else:
            ok_rows.append(f"- ✅ `{name}` — structurally in sync ({len(live)} paths)")

    has_drift = bool(drift_rows)
    has_recall_drop = bool(recall_rows)
    prov.recheck()
    report = [
        "# Live-probe drift report\n",
        f"Probed {len(probes)} endpoint(s).\n",
        f"_{prov.render()}_\n",
    ]
    if has_drift:
        report.append("## 🚨 Schema drift detected\n")
        report.append(
            "The live endpoint structure no longer matches the recorded fixture. "
            "Either upstream changed (update the fixture **and** the affected "
            "schema, then review the contract tests) or this is a real regression.\n"
        )
        report.extend(drift_rows)
        report.append("")
    if has_recall_drop:
        report.append("## 📉 Recall below floor\n")
        report.append(
            "An endpoint returned fewer records than its declared floor while its "
            "structure stayed identical — the failure mode a signature diff cannot "
            "see. Before assuming the corpus shrank, re-run each optional filter "
            "parameter omitted-vs-explicitly-maximal and compare the counts; a "
            "changed upstream default restricts the result set without changing "
            "the response shape.\n"
        )
        report.extend(recall_rows)
        report.append("")
    if error_rows:
        report.append("## ⚠️ Probe errors (transient or endpoint moved)\n")
        report.extend(error_rows)
        report.append("")
    if ok_rows:
        report.append("## In sync\n")
        report.extend(ok_rows)
        report.append("")

    report_text = "\n".join(report)
    report_path = Path(os.environ.get("DRIFT_REPORT", _ROOT / "live-probe-report.md"))
    report_path.write_text(report_text, encoding="utf-8")
    print(report_text)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            # `drift` keeps its original meaning (schema only) so existing
            # workflows do not silently change behaviour; `alert` is the one to
            # gate on now that a probe can fail two independent ways.
            fh.write(f"drift={'true' if has_drift else 'false'}\n")
            fh.write(f"recall_drop={'true' if has_recall_drop else 'false'}\n")
            fh.write(f"alert={'true' if (has_drift or has_recall_drop) else 'false'}\n")
            fh.write(f"report_path={report_path}\n")
            # So an issue opened from this run names the manifest revision it
            # walked, not just the day it ran.
            fh.write(f"auditor_sha={prov.head or ''}\n")

    return 0  # never fail the cron on a flaky endpoint; findings go via the outputs


if __name__ == "__main__":
    raise SystemExit(main())
