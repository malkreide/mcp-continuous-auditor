#!/usr/bin/env python3
"""Tests for scripts/shipped_probe.py — the gate that checks what users install.

The network half is not deterministically testable, so the module keeps it in
three named seams — the index lookup, the install, and the subprocess — and
everything that decides anything lives outside them. These tests own that
outside: version comparison, the publication states, tool-result classification
and the finding set. The seams are injected with fakes.

The one thing NOT faked is the stdio conversation. That runs against a real
subprocess (``tests/fixtures/shipped_stdio_server.py``), because the property
being protected there is a timing one — the stdin trap — and a fake cannot
reproduce it. The fixture delays its ``tools/call`` answer specifically so
closing stdin early fabricates a failure, and a test asserts exactly that.

Stdlib-only and offline: no index is contacted, no venv is built.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import shipped_probe as sp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SERVER = FIXTURES / "shipped_stdio_server.py"


def V(installed: str = "", repo: str = "", tag: str = "") -> sp.Versions:
    return sp.Versions(installed=installed, repo=repo, tag=tag)


def codes(findings: list[sp.Finding]) -> list[str]:
    return [f.code for f in findings]


# ---------------------------------------------------------------------------
# version comparison — pure
# ---------------------------------------------------------------------------

class CompareVersionsTest(unittest.TestCase):
    def test_everything_agreeing_is_silent(self) -> None:
        self.assertEqual(sp.compare_versions(V("0.6.0", "0.6.0", "v0.6.0")), [])

    def test_the_incident_shape_is_named_stale(self) -> None:
        # main at 0.6.0, PyPI serving 0.5.0 — the exact case this gate exists for.
        found = sp.compare_versions(V("0.5.0", "0.6.0", ""))
        self.assertEqual(codes(found), ["STALE_ON_INDEX"])
        self.assertIn("users install 0.5.0", found[0].detail)

    def test_the_index_being_ahead_is_a_different_finding(self) -> None:
        # Not the same problem and not the same fix: something was published
        # from a tree this checkout does not have.
        found = sp.compare_versions(V("0.7.0", "0.6.0", ""))
        self.assertEqual(codes(found), ["INDEX_AHEAD"])

    def test_unorderable_versions_are_reported_not_ranked(self) -> None:
        # Guessing a direction we cannot establish would point the maintainer
        # the wrong way with full confidence.
        found = sp.compare_versions(V("2026.07.a", "weird-1", ""))
        self.assertEqual(codes(found), ["VERSION_DIFFERS"])
        self.assertIn("direction is unknown", found[0].detail)

    def test_a_tag_the_index_does_not_have_is_reported(self) -> None:
        found = sp.compare_versions(V("0.5.0", "0.5.0", "v0.6.0"))
        self.assertIn("TAG_NOT_ON_INDEX", codes(found))

    def test_a_tag_matching_the_installed_version_is_silent(self) -> None:
        self.assertEqual(sp.compare_versions(V("0.6.0", "0.6.0", "v0.6.0")), [])

    def test_all_three_diverging_produces_all_three_facts(self) -> None:
        found = sp.compare_versions(V("0.4.0", "0.6.0", "v0.5.0"))
        self.assertIn("STALE_ON_INDEX", codes(found))
        self.assertIn("TAG_NOT_ON_INDEX", codes(found))

    def test_a_missing_side_is_not_invented(self) -> None:
        self.assertEqual(sp.compare_versions(V("0.6.0", "", "")), [])
        self.assertEqual(sp.compare_versions(V("", "0.6.0", "v0.6.0")), [])


# ---------------------------------------------------------------------------
# publication states — absent is not stale
# ---------------------------------------------------------------------------

class PublicationStateTest(unittest.TestCase):
    def test_never_published_says_there_is_no_process_to_repair(self) -> None:
        r = sp.Report(dist="ghost-mcp", publication=sp.NOT_PUBLISHED)
        found = sp.build_findings(r)
        self.assertEqual(codes(found), ["NOT_ON_INDEX"])
        self.assertIn("has never worked", found[0].detail)

    def test_stale_and_absent_are_different_codes(self) -> None:
        absent = sp.build_findings(sp.Report(dist="x", publication=sp.NOT_PUBLISHED))
        stale = sp.compare_versions(V("0.5.0", "0.6.0"))
        self.assertNotEqual(codes(absent), codes(stale))

    def test_an_install_failure_is_its_own_state(self) -> None:
        r = sp.Report(dist="x", publication=sp.INSTALL_FAILED)
        self.assertEqual(codes(sp.build_findings(r)), ["INSTALL_FAILED"])

    def test_a_missing_entrypoint_stops_before_claiming_it_does_not_run(self) -> None:
        r = sp.Report(dist="x", versions=V("1.0.0", "1.0.0"), entrypoint="")
        self.assertEqual(codes(sp.build_findings(r)), ["NO_ENTRYPOINT"])

    def test_no_tools_list_answer_is_does_not_run(self) -> None:
        r = sp.Report(dist="x", versions=V("1.0.0", "1.0.0"),
                      entrypoint="/venv/bin/x", tools=None)
        self.assertEqual(codes(sp.build_findings(r)), ["DOES_NOT_RUN"])


# ---------------------------------------------------------------------------
# tool results — answering and working are different claims
# ---------------------------------------------------------------------------

class ToolResultTest(unittest.TestCase):
    def test_content_is_ok(self) -> None:
        got = sp.classify_tool_result(
            {"result": {"content": [{"type": "text", "text": "42"}]}})
        self.assertEqual(got.status, "ok")

    def test_an_empty_content_list_is_the_incident(self) -> None:
        # "three tools that were demonstrably broken" — they answered, with
        # nothing. A check that only asked "did it reply" would call this fine.
        got = sp.classify_tool_result({"result": {"content": []}})
        self.assertEqual(got.status, "empty")
        self.assertIn("answered with nothing", got.detail)

    def test_is_error_names_the_sandbox_ambiguity(self) -> None:
        # The probe runs behind a default-deny egress allowlist, where a tool
        # with an unlisted upstream fails identically. Say so in the finding
        # rather than let the reader file a bug against the target.
        got = sp.classify_tool_result({"result": {"isError": True, "content": []}})
        self.assertEqual(got.status, "error")
        self.assertIn("allowlist", got.detail)

    def test_an_error_that_reads_like_the_sandbox_is_not_a_finding(self) -> None:
        # A gate that fires on every target whose upstream nobody has allowlisted
        # yet gets muted, and a muted gate catches nothing.
        for text in ("Connection refused", "getaddrinfo failed",
                     "Tunnel connection failed: 403 Forbidden", "read timed out"):
            with self.subTest(text=text):
                got = sp.classify_tool_result(
                    {"result": {"isError": True,
                                "content": [{"type": "text", "text": text}]}})
                self.assertEqual(got.status, "blocked")
                r = sp.Report(dist="x", versions=V("1.0.0", "1.0.0"),
                              entrypoint="/v/bin/x", tools=2, tool_call=got)
                self.assertEqual(sp.build_findings(r), [],
                                 "an unattributable failure must not raise a finding")

    def test_a_blocked_jsonrpc_error_is_also_unattributable(self) -> None:
        got = sp.classify_tool_result(
            {"error": {"code": -32000, "message": "upstream DNS lookup failed"}})
        self.assertEqual(got.status, "blocked")

    def test_an_empty_result_is_never_excused_as_the_sandbox(self) -> None:
        # The incident's own shape. It looks nothing like a blocked socket, and
        # excusing it would delete the entire reason this gate exists.
        got = sp.classify_tool_result({"result": {"content": []}})
        self.assertEqual(got.status, "empty")
        r = sp.Report(dist="x", versions=V("1.0.0", "1.0.0"),
                      entrypoint="/v/bin/x", tools=2, tool_call=got)
        self.assertEqual(codes(sp.build_findings(r)), ["TOOL_EMPTY"])

    def test_a_real_defect_message_is_still_a_finding(self) -> None:
        got = sp.classify_tool_result(
            {"result": {"isError": True,
                        "content": [{"type": "text", "text": "KeyError: 'results'"}]}})
        self.assertEqual(got.status, "error")

    def test_a_jsonrpc_error_is_an_error(self) -> None:
        got = sp.classify_tool_result({"error": {"code": -32602, "message": "bad"}})
        self.assertEqual(got.status, "error")
        self.assertIn("-32602", got.detail)

    def test_no_reply_at_all_is_not_silently_ok(self) -> None:
        self.assertEqual(sp.classify_tool_result(None).status, "no-answer")
        self.assertEqual(sp.classify_tool_result({"result": "nope"}).status, "no-answer")


class PickToolTest(unittest.TestCase):
    def test_the_first_argument_free_tool_is_chosen(self) -> None:
        tools = [{"name": "search", "inputSchema": {"required": ["q"]}},
                 {"name": "health", "inputSchema": {"type": "object"}}]
        self.assertEqual(sp.pick_tool(tools), ("health", ""))

    def test_a_requested_tool_wins(self) -> None:
        tools = [{"name": "health", "inputSchema": {}}, {"name": "count", "inputSchema": {}}]
        self.assertEqual(sp.pick_tool(tools, "count"), ("count", ""))

    def test_a_requested_tool_that_is_absent_says_so(self) -> None:
        name, why = sp.pick_tool([{"name": "health", "inputSchema": {}}], "nope")
        self.assertEqual(name, "")
        self.assertIn("not in tools/list", why)

    def test_arguments_are_never_invented(self) -> None:
        # Guessing a value for a required parameter would test the guess.
        name, why = sp.pick_tool([{"name": "search", "inputSchema": {"required": ["q"]}}])
        self.assertEqual(name, "")
        self.assertIn("--tool", why)


# ---------------------------------------------------------------------------
# the probe, with the three impure seams injected
# ---------------------------------------------------------------------------

class ReadIndexTest(unittest.TestCase):
    """The existence check, which used to ask a different cache than pip resolves.

    Patched at ``_get`` — the single point either fetcher uses to reach the
    network — so the parsing and the precedence run for real. The payload shapes
    are the ones ``tests/test_release_metadata.py`` records from the live index.

    These assertions were written against a ``lookup_index()`` helper that the
    merge removed: it answered the same question phase 1 already answers, so
    keeping it meant two index reads per run. The properties did not change, only
    where they live — ``read_index`` + ``reconcile`` now carry them.
    """

    def _read(self, index_url=sp.DEFAULT_INDEX):
        report = sp.Report(dist="demo-mcp", index_url=index_url)
        sp.read_index(report, Path("."), 5.0, offline=False)
        return report

    def setUp(self) -> None:
        self._orig = sp._get
        self.calls: list[str] = []

    def tearDown(self) -> None:
        sp._get = self._orig  # type: ignore[assignment]

    def _serve(self, simple, json_api) -> None:
        def fake(url: str, timeout: float, accept: str | None = None):
            which = "simple" if "/simple/" in url else "json"
            self.calls.append(which)
            served = simple if which == "simple" else json_api
            if served is None:
                return None, "unreachable", "PyPI unreachable: simulated"
            if served == 404:
                return None, "not_published", "not on PyPI (HTTP 404)"
            return served, "ok", ""

        sp._get = fake  # type: ignore[assignment]

    def _simple(self, versions, yanked=()):
        return {
            "meta": {"api-version": "1.4"},
            "versions": list(versions),
            "files": [
                {"filename": f"demo_mcp-{v}.tar.gz", "yanked": v in yanked} for v in versions
            ],
        }

    def _json(self, versions, latest=None):
        return {
            "info": {"version": latest or list(versions)[-1]},
            "releases": {v: [{"filename": f"demo_mcp-{v}.tar.gz", "yanked": False}]
                         for v in versions},
        }

    def test_both_apis_are_read_on_pypi_and_the_simple_one_decides(self) -> None:
        """A deliberate change from the pre-merge behaviour, not a slip.

        The old `lookup_index` stopped as soon as the Simple API answered — the
        JSON API was purely a fallback. Merging brought the release-gap
        cross-check with it, and that needs the second opinion every time: it is
        what turns a mid-propagation index into UNCONFIRMED instead of a false
        finding. The price is one extra HTTP request against PyPI; the Simple
        API still decides the answer.
        """
        self._serve(self._simple(["0.5.0", "0.6.0"]), self._json(["0.5.0", "0.6.0"]))
        report = self._read()
        self.assertEqual((report.index_version, report.index_status), ("0.6.0", "ok"))
        self.assertEqual(self.calls, ["simple", "json"])
        self.assertEqual(report.yank_source, "simple")

    def test_a_disagreement_between_the_two_reaches_the_shipped_gate_too(self) -> None:
        """The cross-check was release_gap's; after the merge it guards this
        gate as well, which is the point of having one probe."""
        self._serve(self._simple(["0.5.0", "0.6.0"]), self._json(["0.5.0"], latest="0.5.0"))
        report = self._read()
        self.assertEqual(report.index_status, "unconfirmed")

    def test_a_yanked_newest_release_resolves_to_what_pip_would_install(self) -> None:
        """pip skips a yanked release; so must the version this reports."""
        self._serve(self._simple(["0.5.0", "0.6.0"], yanked=["0.6.0"]), None)
        report = self._read()
        self.assertEqual((report.index_version, report.index_status), ("0.5.0", "ok"))

    def test_an_html_only_simple_index_falls_back_instead_of_failing(self) -> None:
        """A Simple index that does not speak PEP 691 JSON is not an index that
        is down — pip installs from it. A bare swap would have made those
        setups exit 127, trading one wrong answer for another."""
        self._serve(None, self._json(["0.6.0"]))
        report = self._read()
        self.assertEqual((report.index_version, report.index_status), ("0.6.0", "ok"))
        self.assertEqual(self.calls, ["simple", "json"])

    def test_both_indexes_unreachable_stays_unreachable(self) -> None:
        self._serve(None, None)
        report = self._read()
        self.assertEqual(report.index_status, "unreachable")
        self.assertIn("unreachable", report.index_detail)

    def test_a_404_is_corroborated_before_accusing_a_maintainer(self) -> None:
        """NOT_ON_INDEX says "you have no release process". A first-ever publish
        is exactly when the two APIs are most likely to be seconds apart, so one
        404 is not enough — and here the install can settle it."""
        self._serve(404, self._json(["0.1.0"]))
        report = self._read()
        self.assertEqual((report.index_version, report.index_status), ("0.1.0", "ok"))
        self.assertEqual(self.calls, ["simple", "json"])

    def test_a_404_both_indexes_agree_on_is_not_published(self) -> None:
        self._serve(404, 404)
        report = sp.Report(dist="ghost-mcp")
        sp.read_index(report, Path("."), 5.0, offline=False)
        self.assertEqual((report.index_version, report.index_status),
                         (None, "not_published"))
        self.assertIn("ghost-mcp", report.index_detail)

    def test_the_index_url_is_the_one_asked(self) -> None:
        """The check must consult the index the install will resolve against."""
        asked: list[str] = []

        def fake(url: str, timeout: float, accept: str | None = None):
            asked.append(url)
            return self._simple(["0.6.0"]), "ok", ""

        sp._get = fake  # type: ignore[assignment]
        self._read("https://pypi.example.com/simple")
        self.assertEqual(len(asked), 1)
        self.assertTrue(
            asked[0].startswith("https://pypi.example.com/simple/demo-mcp/"), asked[0])

    def test_a_private_index_gets_no_pypi_fallback(self) -> None:
        """Falling back to pypi.org would answer about a different package.

        The JSON API exists only on PyPI. Querying it for a distribution that
        lives on a private index either 404s (a false NOT_ON_INDEX) or, worse,
        finds an unrelated public package of the same name and calls it found.
        """
        self._serve(None, self._json(["9.9.9"]))
        report = self._read("https://pypi.example.com/simple")
        self.assertEqual(report.index_status, "unreachable")
        self.assertEqual(self.calls, ["simple"], "pypi.org must not be consulted")
        self.assertIn("no JSON API to fall back to", report.index_detail)

    def test_a_private_index_404_is_believed(self) -> None:
        self._serve(404, None)
        report = self._read("https://pypi.example.com/simple")
        self.assertEqual((report.index_version, report.index_status),
                         (None, "not_published"))
        self.assertIn("pypi.example.com", report.index_detail)

    def test_is_pypi_matches_on_host_not_on_prefix(self) -> None:
        for url in ("https://pypi.org/simple", "https://pypi.org/simple/"):
            self.assertTrue(sp.is_pypi(url), url)
        for url in ("https://pypi.example.com/simple",
                    "https://mirror.local/pypi.org/simple",
                    "http://localhost:8080/simple"):
            self.assertFalse(sp.is_pypi(url), url)


class ProbeWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name)
        (self.target / "pyproject.toml").write_text(textwrap.dedent('''
            [project]
            name = "demo-mcp"
            version = "0.6.0"
        ''').strip() + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _serve_index(self, index):
        """Stub the ONE network door, `_get`, for the duration of a probe.

        Before the merge this injected an `index_lookup` seam. That seam is gone:
        it answered the same question phase 1 already answers, which meant two
        index reads per run and two chances for them to disagree. Stubbing `_get`
        keeps the parsing, the yank attribution and the PyPI-only fallback in
        play rather than mocking past them.
        """
        version, status, _detail = index
        def fake(url: str, timeout: float, accept: str | None = None):
            if status == "unreachable":
                return None, "unreachable", "index unreachable: simulated"
            if status == "not_published":
                return None, "not_published", "not on the index (HTTP 404)"
            if "/simple/" in url:
                return {"meta": {"api-version": "1.4"}, "versions": [version],
                        "files": [{"filename": f"demo_mcp-{version}.tar.gz",
                                   "yanked": False}]}, "ok", ""
            return {"info": {"version": version},
                    "releases": {version: [{"filename": f"demo_mcp-{version}.tar.gz",
                                            "yanked": False}]}}, "ok", ""
        return fake

    def _probe(self, *, index=("0.5.0", "ok", ""), installed=None, spoke=None,
               tool="", dist="demo-mcp") -> sp.Report:
        installed = installed or sp.Installed(True, version="0.5.0",
                                              entrypoint="/venv/bin/demo-mcp")
        spoke = spoke if spoke is not None else {
            "tools": [{"name": "health", "inputSchema": {"type": "object"}}],
            "call": {"result": {"content": [{"type": "text", "text": "ok"}]}},
            "error": "",
        }
        original = sp._get
        sp._get = self._serve_index(index)
        try:
            return sp.probe(
                dist, self.target, tool=tool,
                installer=lambda *a, **k: installed,
                speaker=lambda *a, **k: spoke)
        finally:
            sp._get = original

    def test_a_stale_index_is_a_finding_with_the_versions_carried(self) -> None:
        r = self._probe()
        self.assertEqual(r.exit_code(), sp.EXIT_FINDINGS)
        self.assertIn("STALE_ON_INDEX", codes(r.findings))
        self.assertEqual(r.versions.installed, "0.5.0")
        self.assertEqual(r.versions.repo, "0.6.0")

    def test_everything_in_sync_and_running_is_green(self) -> None:
        r = self._probe(index=("0.6.0", "ok", ""),
                        installed=sp.Installed(True, version="0.6.0",
                                               entrypoint="/venv/bin/demo-mcp"))
        self.assertEqual(r.findings, [])
        self.assertEqual(r.exit_code(), sp.EXIT_GREEN)
        self.assertEqual(r.tool_call.status, "ok")

    def test_an_unreachable_index_is_a_harness_failure_not_in_sync(self) -> None:
        # The lesson the whole family of probes is built on: a comparison that
        # did not happen is never a pass, and must not degrade into one.
        r = self._probe(index=(None, "unreachable", "connection refused"))
        self.assertEqual(r.exit_code(), sp.EXIT_CANNOT_RUN)
        self.assertIn("NOT compared", r.harness_error)
        self.assertEqual(r.findings, [])

    def test_a_package_that_is_not_published_never_reaches_the_installer(self) -> None:
        calls: list[str] = []

        def installer(*a, **k):  # pragma: no cover - must not run
            calls.append("installed")
            return sp.Installed(False)

        r = self._probe(dist="ghost-mcp", index=(None, "not_published", "404"),
                        installed=sp.Installed(False), spoke={})
        self.assertEqual(codes(r.findings), ["NOT_ON_INDEX"])
        self.assertEqual(calls, [], "nothing to install — do not try")

    def test_an_uninjected_probe_reads_the_index_url_it_was_given(self) -> None:
        """Phase 1 must consult the index the install will resolve against.

        Every other test here stubs `_get`, so nothing else would notice the
        default reaching for a different host than `--index-url` names.
        """
        asked: list[str] = []
        original = sp._get

        def fake(url: str, timeout: float, accept: str | None = None):
            asked.append(url)
            return {"meta": {"api-version": "1.4"}, "versions": ["0.6.0"],
                    "files": [{"filename": "demo_mcp-0.6.0.tar.gz",
                               "yanked": False}]}, "ok", ""

        sp._get = fake
        try:
            r = sp.probe(
                "demo-mcp", self.target, index_url="https://pypi.example.com/simple",
                installer=lambda *a, **k: sp.Installed(
                    True, version="0.6.0", entrypoint="/venv/bin/demo-mcp"),
                speaker=lambda *a, **k: {"tools": [], "call": {}, "error": ""})
        finally:
            sp._get = original
        self.assertEqual(len(asked), 1, "a private index has no JSON API to also ask")
        self.assertTrue(asked[0].startswith("https://pypi.example.com/simple/demo-mcp/"),
                        asked[0])
        self.assertEqual(r.versions.installed, "0.6.0")

    def test_an_install_failure_carries_pips_own_words(self) -> None:
        r = self._probe(installed=sp.Installed(False, detail="ResolutionImpossible: x"))
        self.assertIn("INSTALL_FAILED", codes(r.findings))
        self.assertTrue(any("ResolutionImpossible" in f.detail for f in r.findings))

    def test_a_server_that_does_not_start_is_a_finding_about_the_artifact(self) -> None:
        r = self._probe(spoke={"tools": None, "call": None,
                               "error": "could not start the installed entrypoint"})
        self.assertIn("DOES_NOT_RUN", codes(r.findings))

    def test_an_empty_tool_answer_surfaces_as_its_own_code(self) -> None:
        r = self._probe(index=("0.6.0", "ok", ""),
                        installed=sp.Installed(True, version="0.6.0",
                                               entrypoint="/venv/bin/demo-mcp"),
                        spoke={"tools": [{"name": "health", "inputSchema": {}}],
                               "call": {"result": {"content": []}}, "error": ""},
                        tool="health")
        self.assertEqual(codes(r.findings), ["TOOL_EMPTY"])

    def test_a_target_with_no_callable_tool_is_recorded_not_guessed(self) -> None:
        r = self._probe(index=("0.6.0", "ok", ""),
                        installed=sp.Installed(True, version="0.6.0",
                                               entrypoint="/venv/bin/demo-mcp"),
                        spoke={"tools": [{"name": "search",
                                          "inputSchema": {"required": ["q"]}}],
                               "call": None, "error": ""})
        self.assertFalse(r.tool_call.ran)
        self.assertEqual(r.tool_call.status, "skipped")
        self.assertEqual(r.findings, [])   # not a defect, just not exercisable

    @unittest.skipUnless(shutil.which("git"), "git missing")
    def test_the_latest_tag_is_used_not_the_oldest(self) -> None:
        # `release_tags` sorts NEWEST FIRST. Taking [-1] compares the
        # index against the oldest release the repo ever cut, which is always
        # behind and therefore always "a finding" — a gate that is right by
        # accident on every repository. This pins the direction.
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", "-C", str(self.target), *a], capture_output=True, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})
        run("init", "-q")
        run("add", "-A")
        run("commit", "-qm", "init")
        for tag in ("v0.4.0", "v0.5.0", "v0.6.0"):
            run("tag", tag)
        r = self._probe(index=("0.6.0", "ok", ""),
                        installed=sp.Installed(True, version="0.6.0",
                                               entrypoint="/venv/bin/demo-mcp"))
        self.assertEqual(r.versions.tag, "v0.6.0")
        self.assertEqual(r.findings, [], msg=str([f.as_dict() for f in r.findings]))

    def test_the_target_checkout_is_only_read(self) -> None:
        before = sorted(p.name for p in self.target.iterdir())
        self._probe()
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), before)


# ---------------------------------------------------------------------------
# the stdio conversation — real subprocess, because the trap is a timing bug
# ---------------------------------------------------------------------------

class SpeakMcpTest(unittest.TestCase):
    def _speak(self, mode: str, tool: str = "health", **kw) -> dict:
        env = dict(os.environ)
        env["SHIPPED_FIXTURE_MODE"] = mode
        return sp.speak_mcp([sys.executable, str(SERVER)], timeout=25,
                            cwd=FIXTURES, tool=tool, env=env, **kw)

    def test_a_healthy_server_answers_all_three(self) -> None:
        got = self._speak("ok")
        self.assertEqual(got["error"], "")
        self.assertEqual(len(got["tools"]), 2)
        self.assertEqual(sp.classify_tool_result(got["call"]).status, "ok")

    def test_closing_stdin_early_fabricates_a_failure(self) -> None:
        # THE TRAP, pinned on the tool call — the most network-bound step, where
        # it bites hardest. If this ever passes with _close_stdin_early=True, the
        # probe has started closing stdin and every slow tool becomes a false
        # finding about a perfectly good release.
        broken = self._speak("ok", _close_stdin_early=True)
        healthy = self._speak("ok")
        self.assertEqual(healthy["error"], "")
        self.assertEqual(sp.classify_tool_result(healthy["call"]).status, "ok")
        self.assertTrue(
            broken["error"] or sp.classify_tool_result(broken["call"]).status != "ok",
            "closing stdin early must be observable, or the trap is not pinned")

    def test_an_empty_answer_is_carried_through_end_to_end(self) -> None:
        got = self._speak("empty")
        self.assertEqual(sp.classify_tool_result(got["call"]).status, "empty")

    def test_an_is_error_answer_is_carried_through(self) -> None:
        got = self._speak("error")
        self.assertEqual(sp.classify_tool_result(got["call"]).status, "error")

    def test_a_jsonrpc_error_is_carried_through(self) -> None:
        got = self._speak("rpcerror")
        self.assertEqual(sp.classify_tool_result(got["call"]).status, "error")

    def test_a_server_that_cannot_be_started_is_reported_not_raised(self) -> None:
        got = sp.speak_mcp([str(FIXTURES / "does-not-exist")], timeout=5, cwd=FIXTURES)
        self.assertIn("could not start", got["error"])

    def test_a_tool_requiring_arguments_is_left_alone(self) -> None:
        got = self._speak("needsargs", tool="")
        name, why = sp.pick_tool(got["tools"])
        self.assertEqual(name, "")
        self.assertIn("--tool", why)


class CliTest(unittest.TestCase):
    def test_bad_tool_args_are_a_harness_error_not_a_finding(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            rc = sp.main(["--dist", "x", "--tool-args", "[1,2]"])
        self.assertEqual(rc, sp.EXIT_CANNOT_RUN)

    def test_render_names_all_three_versions(self) -> None:
        r = sp.Report(dist="demo-mcp", versions=V("0.5.0", "0.6.0", "v0.6.0"))
        r.findings = sp.compare_versions(r.versions)
        text = sp.render(r)
        self.assertIn("0.5.0", text)
        self.assertIn("0.6.0", text)
        self.assertIn("STALE_ON_INDEX", text)

    def test_the_report_is_machine_readable(self) -> None:
        r = sp.Report(dist="demo-mcp", versions=V("0.5.0", "0.6.0"))
        r.findings = sp.compare_versions(r.versions)
        data = json.loads(json.dumps(r.as_dict()))
        self.assertEqual(data["exit_code"], sp.EXIT_FINDINGS)
        self.assertEqual(data["versions"]["installed"], "0.5.0")


class EgressAllowlistTest(unittest.TestCase):
    """The gate cannot reach the index without these, and it fails CLOSED — so a
    pruned entry does not produce a false green, it produces a gate that never
    runs again. That is quieter and therefore worse to lose silently, which is
    why the allowlist is asserted here rather than only described in a comment."""

    REPO = Path(__file__).resolve().parents[1]

    def test_the_worker_may_reach_the_index(self) -> None:
        text = (self.REPO / "deploy" / "microvm" / "forward-proxy"
                / "worker-allow.txt").read_text(encoding="utf-8")
        self.assertIn(r"(^|\.)pypi\.org$", text)
        self.assertIn(r"(^|\.)files\.pythonhosted\.org$", text)

    def test_the_credential_side_is_not_widened_to_match(self) -> None:
        # The Broker holds the tokens and never installs anything. Mirroring the
        # entries there would widen the one side that matters, to buy nothing.
        text = (self.REPO / "deploy" / "microvm" / "forward-proxy"
                / "broker-allow.txt").read_text(encoding="utf-8")
        entries = [ln for ln in text.splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertFalse([e for e in entries if "pypi" in e or "pythonhosted" in e],
                         "the Broker must not be given index egress it has no use for")

    def test_the_nft_layer_still_permits_443(self) -> None:
        # The port layer cannot express per-domain rules — that is the proxy's
        # job — so all it has to keep doing is not blocking HTTPS.
        text = (self.REPO / "deploy" / "microvm"
                / "egress-allowlist.nft").read_text(encoding="utf-8")
        self.assertIn("tcp dport { 80, 443 } accept", text)


if __name__ == "__main__":
    unittest.main()
