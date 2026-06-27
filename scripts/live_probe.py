#!/usr/bin/env python3
"""Weekly live-probe drift detector (Plan Phase 2c).

Fetch the REAL Zürich endpoints once, compare the *structure* of each response
to its recorded fixture, and report drift. This is the ONLY job allowed to talk
to the live municipal APIs — everything else stays offline.

We compare **structural signatures** (the set of JSON paths and their value
types), NOT values. Live sensor readings, dates and ids change constantly; only
an added / removed / re-typed field is real schema drift. That keeps the weekly
diff signal-rich and false-positive-free.

Probes are declared in scripts/live_probe.manifest.json. Each probe names the
fixture (under promptfoo/fixtures/) it must stay structurally compatible with.

Exit code is always 0 (a flaky endpoint must not fail the cron); drift is
signalled out-of-band so the workflow decides whether to open an issue:

  * writes a Markdown report to $DRIFT_REPORT       (default: live-probe-report.md)
  * appends `drift=true|false` to $GITHUB_OUTPUT     (for the workflow step)

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
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    probes = manifest["probes"] if isinstance(manifest, dict) else manifest

    drift_rows: list[str] = []
    error_rows: list[str] = []
    ok_rows: list[str] = []

    for probe in probes:
        name = probe["name"]
        fixture = probe["fixture"]
        try:
            expected = structural_signature(_load_fixture(fixture))
            live = structural_signature(_fetch(probe))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            error_rows.append(f"- ⚠️ `{name}`: probe failed — `{type(exc).__name__}: {exc}`")
            print(f"::warning title=live-probe::{name} failed: {exc}", file=sys.stderr)
            continue

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
    report = ["# Live-probe drift report\n", f"Probed {len(probes)} endpoint(s).\n"]
    if has_drift:
        report.append("## 🚨 Schema drift detected\n")
        report.append(
            "The live endpoint structure no longer matches the recorded fixture. "
            "Either upstream changed (update the fixture **and** the affected "
            "schema, then review the contract tests) or this is a real regression.\n"
        )
        report.extend(drift_rows)
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
            fh.write(f"drift={'true' if has_drift else 'false'}\n")
            fh.write(f"report_path={report_path}\n")

    return 0  # never fail the cron on a flaky endpoint; drift is signalled via output


if __name__ == "__main__":
    raise SystemExit(main())
