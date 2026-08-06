#!/usr/bin/env python3
"""Tests for scripts/published_probe.py.

Stdlib-only. The probe's slow half installs a distribution from an index; that
is not tested here, because a test that needs PyPI is a test that goes red when
PyPI has a bad afternoon. What is tested is the half that decides what a
measurement *means* — the pattern recognition and the classification — since
that is where every bug in this probe's history actually sat.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import published_probe as pp  # noqa: E402

DIST = "demo-mcp"


def measured(
    findings: list[pp.Finding] | None = None,
    installed: str = "1.2.3",
    mentions_ua: bool = True,
    **over,
) -> pp.Result:
    """A Result as `probe()` would leave it, without a venv or an index."""
    result = pp.Result(
        dist=DIST,
        installed=installed,
        findings=findings or [],
        mentions_ua=mentions_ua,
        **over,
    )
    for f in result.findings:
        f.own = pp._is_own(f.value, DIST)
        f._ok = f.own and f.sent_version == installed
    return result


def classify(findings: list[pp.Finding], installed: str, mentions_ua: bool) -> str:
    """The status rules of probe(), applied to an already-taken measurement.

    Calls the real `decide_status` rather than restating it. The helper used to
    be a copy of the rules, which meant a change to the probe's precedence could
    not fail a test — the copy moved with nothing.
    """
    return pp.decide_status(measured(findings, installed, mentions_ua))[0]


def finding(value: str, evidence: str = "runtime") -> pp.Finding:
    return pp.Finding(
        value=value,
        sent_version=pp._sent_version(value),
        evidence=evidence,
        where="test",
    )


class VersionExtractionTest(unittest.TestCase):
    def test_reads_the_version_after_the_slash(self) -> None:
        self.assertEqual(pp._sent_version("demo-mcp/1.2.3"), "1.2.3")

    def test_stops_at_the_comment_that_follows(self) -> None:
        self.assertEqual(
            pp._sent_version("demo-mcp/0.3.5 (+https://example.invalid)"), "0.3.5"
        )

    def test_a_user_agent_without_a_version_yields_none(self) -> None:
        """sbb-opendata-mcp sends a bare product token.

        There is nothing to compare, so it must not count as a match — it has to
        fall through to `unverified` rather than quietly pass.
        """
        self.assertIsNone(pp._sent_version("sbb-opendata-mcp"))


class PatternTest(unittest.TestCase):
    def test_fstring_pattern_finds_the_inline_form(self) -> None:
        """The shape a literal scan cannot see: no digit after the slash."""
        found = pp.FSTRING.findall(
            'headers={"User-Agent": f"news-monitor-mcp/{__version__}"}'
        )
        self.assertEqual(found, [("news-monitor-mcp", "__version__")])

    def test_fstring_pattern_accepts_any_variable_name(self) -> None:
        """lobbywatch-mcp calls it PACKAGE_VERSION; matching `__version__` missed it."""
        found = pp.FSTRING.findall('UA = f"lobbywatch-mcp/{PACKAGE_VERSION} (+url)"')
        self.assertEqual(found, [("lobbywatch-mcp", "PACKAGE_VERSION")])

    def test_literal_pattern_finds_the_hand_maintained_form(self) -> None:
        self.assertIn(
            "swiss-transport-mcp/1.0",
            pp.LITERAL.findall('headers={"User-Agent": "swiss-transport-mcp/1.0"}'),
        )


class CommentStrippingTest(unittest.TestCase):
    """`bakom-mcp` 2.0.4 sends the right version and was reported as DRIFT.

    Its `__init__.py` documents the old incident in a comment, and the literal
    scan read the comment as evidence. Going red on a comment that records the
    very bug the probe exists to catch teaches people to delete the record.
    """

    def test_a_comment_recording_old_drift_is_not_evidence(self) -> None:
        src = (
            "from importlib.metadata import version\n"
            '# in server.py carried "bakom-mcp/1.0" to the BAKOM endpoints all the while.\n'
            '__version__ = version("bakom-mcp")\n'
        )
        self.assertNotIn("bakom-mcp/1.0", pp.LITERAL.findall(pp.code_only(src)))

    def test_a_real_literal_still_counts(self) -> None:
        src = 'HEADERS = {"User-Agent": "bakom-mcp/1.0"}  # legacy\n'
        self.assertIn("bakom-mcp/1.0", pp.LITERAL.findall(pp.code_only(src)))

    def test_a_hash_inside_a_string_does_not_truncate_the_line(self) -> None:
        """Why tokenize and not split("#")."""
        src = 'UA = "demo-mcp/1.2.3"  # note\nURL = "https://x.invalid/#frag"\n'
        out = pp.code_only(src)
        self.assertIn("demo-mcp/1.2.3", out)
        self.assertIn("https://x.invalid/#frag", out)
        self.assertNotIn("# note", out)

    def test_unparseable_source_is_checked_whole_not_skipped(self) -> None:
        src = 'def broken(:\n    UA = "demo-mcp/1.2.3"\n'
        self.assertIn("demo-mcp/1.2.3", pp.code_only(src))


class ClassificationTest(unittest.TestCase):
    def test_matching_version_is_ok(self) -> None:
        self.assertEqual(classify([finding("demo-mcp/1.2.3")], "1.2.3", True), "ok")

    def test_stale_version_is_drift(self) -> None:
        self.assertEqual(classify([finding("demo-mcp/1.0.0")], "1.2.3", True), "drift")

    def test_nothing_found_and_nothing_mentioned_is_no_user_agent(self) -> None:
        self.assertEqual(classify([], "1.2.3", False), "no_user_agent")

    def test_nothing_found_but_mentioned_is_unverified(self) -> None:
        """The load-bearing rule.

        Reporting this as clean is how a first pass called 24 packages
        unremarkable, 16 of which were drifting.
        """
        self.assertEqual(classify([], "1.2.3", True), "unverified")

    def test_versionless_user_agent_does_not_pass_as_ok(self) -> None:
        """sbb-opendata-mcp sends a bare product token — nothing to compare.

        It must fall through to `unverified` rather than counting as a match.
        """
        self.assertEqual(classify([finding(DIST)], "0.3.3", True), "unverified")

    def test_one_stale_value_among_correct_ones_still_drifts(self) -> None:
        """swiss-electricity-mcp shipped a stale second constant after a merged fix."""
        self.assertEqual(
            classify(
                [finding("demo-mcp/1.2.3"), finding("demo-mcp/0.2.0")], "1.2.3", True
            ),
            "drift",
        )

    def test_a_spoofed_browser_is_not_read_as_a_version(self) -> None:
        """swiss-efv-mcp sends `Mozilla/5.0 … Chrome/124.0`.

        Parsed as product-token/version it yields "5.0" and reports as drift
        against 0.3.0 — wrong twice: the package is not announcing a stale
        version of itself, and impersonating a browser goes unnamed.
        """
        chrome = finding(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0"
        )
        self.assertEqual(classify([chrome], "0.3.0", True), "foreign_user_agent")

    def test_a_foreign_token_does_not_mask_real_drift(self) -> None:
        """Drift outranks it: a stale own identity stays the headline."""
        self.assertEqual(
            classify(
                [finding("Mozilla/5.0 (X11)"), finding("demo-mcp/0.1.0")], "1.2.3", True
            ),
            "drift",
        )

    def test_own_token_is_matched_case_and_separator_insensitively(self) -> None:
        """swisstopo-mcp sends `SwisstopoMCP/…`; the token need not be the dist name."""
        self.assertTrue(pp._is_own("DemoMCP/1.2.3", "demo-mcp"))
        self.assertFalse(pp._is_own("Mozilla/5.0", "demo-mcp"))


class ExitStatusTest(unittest.TestCase):
    def test_only_ok_and_no_user_agent_count_as_clean(self) -> None:
        for status, expected in (
            ("ok", True),
            ("no_user_agent", True),
            ("drift", False),
            ("unverified", False),
            ("foreign_user_agent", False),
            ("install_failed", False),
            ("broken_import", False),
            ("smoke_failed", False),
            ("smoke_unverified", False),
            ("unbounded_dependency", False),
        ):
            with self.subTest(status=status):
                self.assertEqual(pp.Result(dist="x", status=status).ok, expected)


def check(module: str = "demo_mcp.private", kind: str = "submodule") -> pp.ImportCheck:
    return pp.ImportCheck(
        module=module,
        root=module.split(".")[0],
        kind=kind,
        error="ImportError: cannot import name 'x'",
    )


class ImportVerdictTest(unittest.TestCase):
    """`bag-health-mcp` was reported as having a circular import. It has none.

    What failed was importing the private submodule as the very FIRST import of
    the process — an artefact of the order this probe walked the modules in.
    `import bag_health_mcp.server` runs cleanly in a fresh venv, which is what a
    user's code does, and that is the measurement that decides.
    """

    def test_cold_fails_and_warm_succeeds_is_an_import_order_artefact(self) -> None:
        got = pp.classify_import(
            check(),
            cold={"ok": False, "error": "ImportError: x"},
            warm={"ok": True, "error": ""},
        )
        self.assertEqual(got.verdict, "order-artifact")
        self.assertFalse(got.real)

    def test_warm_failing_is_a_real_broken_import(self) -> None:
        """The rule: import the root first, and whatever still fails is real."""
        got = pp.classify_import(
            check(),
            cold={"ok": False, "error": "boom"},
            warm={"ok": False, "error": "ImportError: no mcp.server.fastmcp"},
        )
        self.assertEqual(got.verdict, "real")
        self.assertTrue(got.real)
        self.assertIn("fastmcp", got.warm_error)

    def test_neither_reproduces_is_named_apart_from_an_order_artefact(self) -> None:
        """Both fresh imports work: the bulk scan's own state produced it.

        Reported as `not-reproduced` rather than `order-artifact`, because
        claiming it was the ORDER would be claiming something not measured.
        """
        got = pp.classify_import(
            check(), cold={"ok": True, "error": ""}, warm={"ok": True, "error": ""}
        )
        self.assertEqual(got.verdict, "not-reproduced")
        self.assertFalse(got.real)

    def test_a_missing_extras_only_dependency_is_not_the_server_being_broken(
        self,
    ) -> None:
        """A shipped test module importing `pytest` fails for every user, and
        says nothing about the server. `pip install <dist>` installs no extras."""
        conditional = pp.conditional_names(['pytest>=8; extra == "dev"'])
        got = pp.classify_import(
            check("demo_mcp.tests.test_api"),
            cold={
                "ok": False,
                "error": "ModuleNotFoundError: No module named 'pytest'",
            },
            warm={
                "ok": False,
                "error": "ModuleNotFoundError: No module named 'pytest'",
            },
            conditional=conditional,
        )
        self.assertEqual(got.verdict, "optional-dep")
        self.assertFalse(got.real)

    def test_a_missing_module_nothing_declares_stays_real(self) -> None:
        """Measured against `cowsay` 6.1: its shipped `cowsay.tests.*` import
        `pytest` with no requirement declaring it anywhere. That genuinely does
        not import for anyone who ran `pip install cowsay`."""
        got = pp.classify_import(
            check("demo_mcp.tests.test_api"),
            cold={
                "ok": False,
                "error": "ModuleNotFoundError: No module named 'pytest'",
            },
            warm={
                "ok": False,
                "error": "ModuleNotFoundError: No module named 'pytest'",
            },
            conditional=set(),
        )
        self.assertEqual(got.verdict, "real")

    def test_an_unconditional_dependency_is_never_excused(self) -> None:
        """`mcp` missing is the incident, not an extra."""
        conditional = pp.conditional_names(["mcp>=1.20.0", 'ruff; extra == "dev"'])
        self.assertEqual(conditional, {"ruff"})
        got = pp.classify_import(
            check(),
            cold={"ok": False, "error": "x"},
            warm={"ok": False, "error": "ModuleNotFoundError: No module named 'mcp'"},
            conditional=conditional,
        )
        self.assertEqual(got.verdict, "real")

    def test_a_verification_that_could_not_run_counts_as_real(self) -> None:
        """Fail closed. An unverified failure is not a pass — see `unverified`."""
        got = pp.classify_import(
            check(),
            cold={"error": "no parseable output"},
            warm={"error": "no parseable output"},
        )
        self.assertEqual(got.verdict, "unconfirmed")
        self.assertTrue(got.real)

    def test_a_real_broken_import_decides_the_status(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.imports = [
            pp.classify_import(
                check(), {"ok": False, "error": "x"}, {"ok": False, "error": "x"}
            )
        ]
        status, detail = pp.decide_status(result)
        self.assertEqual(status, "broken_import")
        self.assertIn("demo_mcp.private", detail)

    def test_an_artefact_does_not_decide_the_status(self) -> None:
        """The regression this whole distinction exists to prevent."""
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.imports = [
            pp.classify_import(
                check(), {"ok": False, "error": "x"}, {"ok": True, "error": ""}
            )
        ]
        self.assertEqual(pp.decide_status(result)[0], "ok")

    def test_the_artefact_is_still_rendered_rather_than_swallowed(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.imports = [
            pp.classify_import(
                check(), {"ok": False, "error": "x"}, {"ok": True, "error": ""}
            )
        ]
        result.status, result.detail = pp.decide_status(result)
        self.assertIn("IMPORT-ORD", pp.render(result))


class StartEventTest(unittest.TestCase):
    def test_a_structured_log_line_counts(self) -> None:
        self.assertTrue(
            pp.has_start_event('{"event": "server.start", "transport": "stdio"}')
        )

    def test_a_plain_text_line_counts(self) -> None:
        self.assertTrue(
            pp.has_start_event("2026-08-01 INFO server.start transport=stdio")
        )

    def test_a_structured_line_with_another_event_does_not(self) -> None:
        """Read by field, not by substring — a JSON line is not grepped."""
        self.assertFalse(
            pp.has_start_event('{"event": "config.load", "note": "server.start"}')
        )

    def test_silence_is_not_a_start(self) -> None:
        self.assertFalse(pp.has_start_event("Loading config...\nReady.\n"))

    def test_the_event_name_is_configurable(self) -> None:
        self.assertTrue(pp.has_start_event('{"event": "boot"}', event="boot"))


class SmokeTest(unittest.TestCase):
    """Import success is not start success — the whole reason this stage exists."""

    def test_the_event_and_a_clean_exit_is_a_pass(self) -> None:
        """stdin is CLOSED, so a stdio server announcing and then exiting 0 on
        EOF is the healthy shape. The exit code is not the signal."""
        got = pp.classify_smoke('{"event": "server.start"}\n', 0, "demo-mcp", 6.0)
        self.assertEqual(got.status, "ok")

    def test_still_running_after_the_window_with_the_event_is_a_pass(self) -> None:
        got = pp.classify_smoke("server.start listening\n", None, "demo-mcp", 6.0)
        self.assertEqual(got.status, "ok")

    def test_a_nonzero_exit_is_a_crash(self) -> None:
        got = pp.classify_smoke(
            "ValueError: settings are read-only\n", 1, "demo-mcp", 6.0
        )
        self.assertEqual(got.status, "crashed")

    def test_a_traceback_with_a_zero_exit_is_still_a_crash(self) -> None:
        """A server that swallows its own exception still did not come up."""
        text = 'Traceback (most recent call last)\n  File "x"\nValueError: boom\n'
        self.assertEqual(pp.classify_smoke(text, 0, "demo-mcp", 6.0).status, "crashed")

    def test_no_event_and_no_crash_is_not_a_pass(self) -> None:
        """Same rule as `unverified`: not seeing it is not evidence it happened."""
        got = pp.classify_smoke("nothing to say\n", 0, "demo-mcp", 6.0)
        self.assertEqual(got.status, "no_event")
        self.assertIn("not a pass", got.detail)

    def test_a_crash_decides_the_status_over_a_clean_user_agent(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.smoke = pp.classify_smoke(
            "Traceback (most recent call last)\n", 1, "demo-mcp", 6.0
        )
        self.assertEqual(pp.decide_status(result)[0], "smoke_failed")

    def test_drift_outranks_an_unannounced_start(self) -> None:
        """Positive evidence about the wire beats the absence of evidence."""
        result = measured([finding("demo-mcp/0.1.0")], "1.2.3", True)
        result.smoke = pp.classify_smoke("quiet\n", 0, "demo-mcp", 6.0)
        self.assertEqual(pp.decide_status(result)[0], "drift")

    def test_a_missing_console_script_is_a_failed_smoke(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.smoke = pp.Smoke(status="no_entrypoint", detail="no console script")
        self.assertEqual(pp.decide_status(result)[0], "smoke_failed")

    def test_a_skipped_smoke_changes_nothing(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        self.assertEqual(pp.decide_status(result)[0], "ok")


class EntrypointListingTest(unittest.TestCase):
    """Jeder negative Status traegt seine Beobachtung — auch dieser.

    Der Befund: `_run` meldet einen gescheiterten Aufruf als `{"error": …}`,
    und `.get("scripts") or []` machte daraus eine leere Liste. Der Lauf sagte
    dann «die Distribution deklariert kein Console-Script» — ein FUND gegen das
    Ziel (`no_entrypoint` → `smoke_failed`), erzeugt aus der eigenen Blindheit
    der Sonde. Die schlimmere Richtung von «nicht hingesehen = nichts da».
    """

    def _smoke(self, payload: dict) -> object:
        orig = pp._run
        pp._run = lambda *a, **k: payload  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(pp, "_run", orig))
        env = Path(tempfile.mkdtemp(prefix="entrypoints-"))
        (env / "bin").mkdir()
        return pp.smoke_start(env / "bin" / "python", "demo-mcp", env, 1.0)

    def test_a_failed_listing_is_not_measured_and_carries_the_error(self) -> None:
        got = self._smoke({"error": "AttributeError: no dist-info"})
        self.assertEqual(got.status, "error")
        self.assertIn("AttributeError", got.evidence)
        self.assertIn("NOT established", got.detail)

    def test_a_failed_listing_is_never_a_finding_against_the_target(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.smoke = self._smoke({"error": "boom"})
        self.assertEqual(pp.decide_status(result)[0], "smoke_unverified")

    def test_an_empty_listing_stays_a_finding_and_names_the_observation(self) -> None:
        """Gemessen und leer: das IST ein Mangel — und sagt jetzt, was es sah."""
        got = self._smoke({"scripts": []})
        self.assertEqual(got.status, "no_entrypoint")
        self.assertIn("console_scripts", got.evidence)

    def test_declared_but_not_installed_is_its_own_sentence(self) -> None:
        """Vorher las sich das als 'deklariert kein Console-Script', waehrend
        `pyproject.toml` zwei nennt."""
        got = self._smoke({"scripts": ["demo-mcp", "demo-mcp-admin"]})
        self.assertEqual(got.status, "no_entrypoint")
        self.assertIn("demo-mcp-admin", got.evidence)
        self.assertIn("did not place", got.detail)


class WatchTest(unittest.TestCase):
    """The process half of the smoke stage, against a real subprocess.

    `classify_smoke` above decides what an outcome MEANS; this asserts the
    outcomes are produced at all — stdin really closed, output really drained,
    a server that outlives the window really terminated.
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "published_smoke_server.py"

    def _watch(self, mode: str, seconds: float = 3.0):
        text, code, error = pp.watch(
            [sys.executable, str(self.FIXTURE), mode],
            Path(self.FIXTURE).parent,
            seconds,
        )
        self.assertEqual(error, "")
        return text, code

    def test_a_server_that_outlives_the_window_is_still_running(self) -> None:
        text, code = self._watch("announce", seconds=1.0)
        self.assertIsNone(code)
        self.assertEqual(pp.classify_smoke(text, code, "x", 1.0).status, "ok")

    def test_a_stdio_server_exits_zero_on_the_closed_stdin(self) -> None:
        """stdin is CLOSED by the probe; surviving EOF is the correct behaviour
        and must not be read as the server falling over."""
        text, code = self._watch("eof")
        self.assertEqual(code, 0)
        self.assertEqual(pp.classify_smoke(text, code, "x", 3.0).status, "ok")

    def test_a_crash_at_start_is_seen(self) -> None:
        text, code = self._watch("crash")
        self.assertNotEqual(code, 0)
        self.assertEqual(pp.classify_smoke(text, code, "x", 3.0).status, "crashed")

    def test_a_silent_server_is_not_a_pass(self) -> None:
        text, code = self._watch("quiet")
        self.assertEqual(pp.classify_smoke(text, code, "x", 3.0).status, "no_event")

    def test_a_server_that_floods_stdout_does_not_deadlock_the_probe(self) -> None:
        """More output than a pipe buffer holds. A probe that waits for exit
        without draining hangs here until its own timeout and then reports a
        failure that never happened."""
        text, code = self._watch("flood", seconds=3.0)
        self.assertIsNone(code)
        self.assertEqual(pp.classify_smoke(text, code, "x", 3.0).status, "ok")

    def test_a_missing_entrypoint_reports_rather_than_raises(self) -> None:
        text, code, error = pp.watch(["/nonexistent/entrypoint"], Path("."), 2.0)
        self.assertTrue(error)
        self.assertEqual((text, code), ("", None))


def caps(
    requires: list[str], imported: set[str], versions: dict[str, list[str] | None]
):
    return pp.dependency_caps(requires, imported, DIST, lambda n: versions.get(n))


class DependencyCapTest(unittest.TestCase):
    """`swiss-energy-mcp` 0.3.3 shipped `mcp[cli]>=1.20.0` with no upper bound.

    The artifact never changed. `mcp` 2.0.0 was published, and every fresh
    install of that release died on import. Nothing in the repository was wrong
    on the day it was written; the published metadata met a new major.
    """

    def test_an_open_range_with_a_published_major_past_it_is_a_finding(self) -> None:
        got = caps(
            ["mcp[cli]>=1.20.0"], {"mcp"}, {"mcp": ["1.20.0", "1.28.1", "2.0.0"]}
        )
        self.assertEqual([c.verdict for c in got], ["major-available"])
        self.assertIn("2.0.0", got[0].detail)
        self.assertTrue(got[0].finding)

    def test_an_open_range_with_no_higher_major_yet_is_armed_not_a_finding(
        self,
    ) -> None:
        """The day before. Reported, because the trap is real; not red, because
        nothing has happened yet."""
        got = caps(["mcp[cli]>=1.20.0"], {"mcp"}, {"mcp": ["1.20.0", "1.28.1"]})
        self.assertEqual([c.verdict for c in got], ["armed"])
        self.assertFalse(got[0].finding)

    def test_an_upper_bound_is_recognised_in_all_its_spellings(self) -> None:
        """`~=` and `==2.*` bound the major without ever spelling `<`."""
        for spec in ("mcp>=1.20,<2", "mcp~=1.20.0", "mcp==1.28.1"):
            with self.subTest(spec=spec):
                got = caps([spec], {"mcp"}, {"mcp": ["1.20.0", "2.0.0"]})
                self.assertEqual([c.verdict for c in got], ["capped"])

    def test_a_declared_but_never_imported_dependency_is_not_reported(self) -> None:
        """Import-critical is MEASURED, from sys.modules, not assumed from the
        requirement list. A dependency that is never imported cannot break an
        import, and a finding about it would be a finding about nothing."""
        got = caps(["rich>=13"], set(), {"rich": ["13.0", "14.0"]})
        self.assertEqual(got, [])

    def test_an_environment_gated_requirement_is_skipped(self) -> None:
        """`extra == 'dev'` is not what `pip install` gives a user."""
        got = caps(
            ['pytest>=8; extra == "dev"'], {"pytest"}, {"pytest": ["8.0", "9.0"]}
        )
        self.assertEqual(got, [])

    def test_an_unreadable_index_is_unknown_and_never_capped(self) -> None:
        got = caps(["mcp>=1.20.0"], {"mcp"}, {"mcp": None})
        self.assertEqual([c.verdict for c in got], ["unknown"])
        self.assertFalse(got[0].finding)

    def test_a_prerelease_major_does_not_count_as_published(self) -> None:
        """`2.0.0rc1` is not what a default resolver takes."""
        got = caps(["mcp>=1.20.0"], {"mcp"}, {"mcp": ["1.28.1", "2.0.0rc1"]})
        self.assertEqual([c.verdict for c in got], ["armed"])

    def test_an_open_range_decides_the_status_when_nothing_sharper_did(self) -> None:
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.caps = caps(["mcp>=1.20.0"], {"mcp"}, {"mcp": ["1.20.0", "2.0.0"]})
        status, detail = pp.decide_status(result)
        self.assertEqual(status, "unbounded_dependency")
        self.assertIn("mcp", detail)

    def test_a_broken_import_outranks_it(self) -> None:
        """It already happened; the warning about it happening is not the news."""
        result = measured([finding("demo-mcp/1.2.3")], "1.2.3", True)
        result.caps = caps(["mcp>=1.20.0"], {"mcp"}, {"mcp": ["1.20.0", "2.0.0"]})
        result.imports = [
            pp.classify_import(
                check(),
                {"ok": False, "error": "x"},
                {"ok": False, "error": "no mcp.server.fastmcp"},
            )
        ]
        self.assertEqual(pp.decide_status(result)[0], "broken_import")


class RenderTest(unittest.TestCase):
    def test_every_layer_gets_a_line_not_just_the_headline(self) -> None:
        """A package can drift AND fail to import AND carry an open range.

        Only one of those can be the status. Printing only that one would hide
        two true facts behind a precedence rule.
        """
        result = measured([finding("demo-mcp/0.1.0")], "1.2.3", True)
        result.imports = [
            pp.classify_import(
                check(), {"ok": False, "error": "x"}, {"ok": False, "error": "x"}
            )
        ]
        result.caps = caps(["mcp>=1.20.0"], {"mcp"}, {"mcp": ["1.20.0", "2.0.0"]})
        result.smoke = pp.classify_smoke("quiet\n", 0, "demo-mcp", 6.0)
        result.status, result.detail = pp.decide_status(result)
        text = pp.render(result)
        for marker in ("BROKEN-IMP", "SMOKE-?", "UNCAPPED", "DRIFT"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


class ManifestTest(unittest.TestCase):
    """Die Abdeckung gehoert ins Ergebnis, nicht als Kommentar daneben."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _manifest(self, servers: list[dict]) -> Path:
        p = Path(self.tmp.name) / "manifest.json"
        p.write_text(json.dumps({"servers": servers}), encoding="utf-8")
        return p

    def test_entries_without_a_package_are_a_named_omission(self) -> None:
        """``pypi_dist: null`` heisst: es gibt kein Artefakt zu messen.

        Ein begruendeter Verzicht — begruendet, weil das Manifest es sagt, und
        nicht, weil irgendeine Liste den Eintrag nicht erwaehnt hat.
        """
        path = self._manifest(
            [
                {"id": "a-mcp", "pypi_dist": "a-mcp"},
                {"id": "nur-github", "pypi_dist": None},
            ]
        )
        expected, targets, skipped = pp.read_manifest(path)
        self.assertEqual(targets, [("a-mcp", None)])
        self.assertEqual([n for n, _ in skipped], ["nur-github"])
        self.assertTrue(skipped[0][1], "ein Verzicht ohne Begruendung ist keiner")
        self.assertEqual(expected, 2)

    def test_the_total_counts_every_entry_not_just_the_probeable_ones(self) -> None:
        """Regression: die Soll-Zahl zaehlte anfangs nur Eintraege mit Paket.

        Damit kam ein vollstaendiger Lauf auf `geprueft + uebersprungen` = 3
        gegen eine Soll-Zahl von 2 und meldete die Abdeckung als unvollstaendig
        — Exit 1 bei lauter gruenen Ergebnissen. Die Soll-Zahl darf nicht von
        derselben Einschaetzung abhaengen, die der Abdeckungscheck pruefen soll.
        """
        path = self._manifest(
            [
                {"id": "a-mcp", "pypi_dist": "a-mcp"},
                {"id": "b-mcp", "pypi_dist": None},
                {"id": "c-mcp", "pypi_dist": None},
            ]
        )
        expected, targets, skipped = pp.read_manifest(path)
        self.assertEqual(expected, 3)
        self.assertEqual(len(targets) + len(skipped), expected)

    def test_an_empty_manifest_is_refused(self) -> None:
        """`0/0 geprueft` mit Exit 0 waere von einem gepruefen Portfolio nicht
        zu unterscheiden — genau die Verwechslung, gegen die es hier geht."""
        with self.assertRaises(SystemExit):
            pp.read_manifest(self._manifest([]))

    def test_a_missing_pypi_dist_key_is_not_read_as_null(self) -> None:
        """Fehlend und null sind verschiedene Aussagen.

        Benennt der Erzeuger das Feld um, wuerde `.get()` jeden Eintrag zu
        einem begruendeten Verzicht machen: nichts gemessen, Abdeckung
        vollstaendig, Exit 0. Also der falsche gruene Lauf, den dieser
        Mechanismus verhindern soll — eine Schicht weiter aussen.
        """
        with self.assertRaises(SystemExit):
            pp.read_manifest(self._manifest([{"id": "a-mcp", "dist_name": "a-mcp"}]))

    def test_a_pypi_dist_that_is_neither_name_nor_null_is_refused(self) -> None:
        for bad in ("", "   ", 42, []):
            with self.subTest(value=bad), self.assertRaises(SystemExit):
                pp.read_manifest(self._manifest([{"id": "a-mcp", "pypi_dist": bad}]))

    def test_a_manifest_without_a_servers_list_is_refused(self) -> None:
        p = Path(self.tmp.name) / "broken.json"
        p.write_text(json.dumps({"eintraege": []}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            pp.read_manifest(p)

    def test_a_declared_start_event_reaches_the_target(self) -> None:
        """Der Marker, an dem ein Server sein Bedienen ankuendigt, gehoert ihm.

        Eine Vorgabe fuer alle produzierte im Portfolio 38 von 42 Mal
        `smoke_unverified` — nicht weil die Server schwiegen, sondern weil sie
        es anders formulieren. Eine Pruefung, die fast ueberall unbestaetigt
        meldet, wird weggeklickt, und dann uebersieht sie den einen echten Fall
        (zh-education-mcp 0.2.4 startete gar nicht).
        """
        path = self._manifest(
            [{"id": "a-mcp", "pypi_dist": "a-mcp", "start_event": "a_mcp.startup"}]
        )
        _, targets, _ = pp.read_manifest(path)
        self.assertEqual(targets, [("a-mcp", "a_mcp.startup")])

    def test_a_missing_start_event_is_none_not_an_error(self) -> None:
        """`start_event` ist optional — anders als `pypi_dist`.

        Fehlend heisst hier "noch nicht erhoben". Das ist ein anderer Zustand
        als bei `pypi_dist`, wo Fehlen bedeutet, dass das Manifest nicht zu
        diesem Werkzeug passt: dort kann Stillschweigen alles gruen faerben,
        hier faellt der Aufrufer nur auf seine Vorgabe zurueck.
        """
        path = self._manifest([{"id": "a-mcp", "pypi_dist": "a-mcp"}])
        _, targets, _ = pp.read_manifest(path)
        self.assertEqual(targets, [("a-mcp", None)])

    def test_an_empty_start_event_does_not_silently_pass_as_a_marker(self) -> None:
        """`""` faellt auf die Vorgabe zurueck statt auf "jede Zeile passt"."""
        path = self._manifest(
            [{"id": "a-mcp", "pypi_dist": "a-mcp", "start_event": ""}]
        )
        _, targets, _ = pp.read_manifest(path)
        self.assertFalse(targets[0][1] or None)

    def test_a_skip_without_a_reason_is_rejected(self) -> None:
        for bad in ("meteoswiss-mcp", "meteoswiss-mcp:   "):
            with self.subTest(arg=bad), self.assertRaises(SystemExit):
                pp.parse_allow_skip([bad])

    def test_a_skip_with_a_reason_is_kept_verbatim(self) -> None:
        self.assertEqual(
            pp.parse_allow_skip(["meteoswiss-mcp:upstream down, Ticket #12"]),
            {"meteoswiss-mcp": "upstream down, Ticket #12"},
        )

    def test_a_colon_in_the_reason_survives(self) -> None:
        """``--allow-skip x:siehe https://…`` darf nicht an der URL zerbrechen."""
        self.assertEqual(
            pp.parse_allow_skip(["x-mcp:siehe https://example.org/a"]),
            {"x-mcp": "siehe https://example.org/a"},
        )


if __name__ == "__main__":
    unittest.main()
