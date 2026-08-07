#!/usr/bin/env python3
"""Tests for scripts/reference_drift_probe.py — template vs. the servers.

The probe reads a skill checkout and several server checkouts, so these tests
build exactly that: a `reference/` directory with a manifest beside it, and
three throwaway repositories that copied it.

The CORRECT-ADOPTION cases matter as much as the drift ones, and there are more
of them here for that reason. A copied fragment is *supposed* to arrive renamed
— different constants, a different function name, `import random as rnd` instead
of `import random` — and a probe that cannot tell a renamed adoption from a
regression produces a red run that everybody learns to ignore. That is the exact
failure mode a full-text diff has, and avoiding it is the whole design.

Stdlib-only, no network, no git.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reference_drift_probe as rdp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# The template as it is SUPPOSED to be: reads Retry-After, jitters, caps after
# jittering, keeps a wall-clock budget, and fails with a typed error.
GOOD_TEMPLATE = '''\
"""Retry with backoff — copy this into the server, do not reinvent it."""

import random
import time

MAX_DELAY = 30.0
BUDGET = 120.0


def request_with_retry(session, url, attempts=5):
    started = time.monotonic()
    last_error = None
    for attempt in range(attempts):
        try:
            return session.get(url)
        except TimeoutError as exc:
            last_error = exc
        if time.monotonic() - started > BUDGET:
            break
        hinted = session.headers.get("retry-after")
        base = float(hinted) if hinted else 2**attempt
        time.sleep(min(MAX_DELAY, random.uniform(0.0, base)))
    raise UpstreamUnavailable(last_error)
'''

# A correct adoption. Every constant renamed, the function renamed, the header
# spelled with different capitalisation, `random` imported under an alias, the
# attempt count different. Nothing here is drift.
GOOD_SITE = '''\
"""HTTP access for this server."""

import random as rnd
import time

CEILING = 45.0
TIME_BUDGET = 90.0


def _fetch_with_backoff(client, target, tries=4):
    begin = time.monotonic()
    failure = None
    for round_index in range(tries):
        try:
            return client.get(target)
        except TimeoutError as exc:
            failure = exc
        if time.monotonic() - begin > TIME_BUDGET:
            break
        advice = client.headers.get("Retry-After")
        window = float(advice) if advice else 2**round_index
        time.sleep(min(CEILING, rnd.uniform(0.0, window)))
    raise UpstreamUnavailable(failure)
'''

MANIFEST = """\
schema = 1

[[template]]
file = "reference/retry_backoff.py"
symbol = "request_with_retry"

[[template.property]]
id = "reads_retry_after"
says = "honours the server's own Retry-After hint"
kind = "literal"
any_of = ["retry-after"]
expect = "present"

[[template.property]]
id = "jitters"
says = "spreads the retry"
kind = "calls"
any_of = ["random.uniform"]
expect = "present"

[[template.property]]
id = "caps_after_jitter"
says = "applies the ceiling after jittering"
kind = "wraps"
outer = "min"
inner = ["random.uniform"]
expect = "present"

[[template.property]]
id = "wall_clock_budget"
says = "bounds the total time spent"
kind = "calls"
any_of = ["time.monotonic"]
expect = "present"

[[template.adoption]]
repo = "acme/alpha-mcp"
file = "src/http.py"
symbol = "_fetch_with_backoff"
since = "2026-05-14"

[[template.adoption]]
repo = "acme/beta-mcp"
file = "src/http.py"
symbol = "_fetch_with_backoff"
since = "2026-05-21"

[[template.adoption]]
repo = "acme/gamma-mcp"
file = "src/http.py"
symbol = "_fetch_with_backoff"
since = "2026-06-02"
"""


class Bed:
    """A skill checkout, a repos root, and however many server checkouts."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.skill = tmp / "skill"
        self.root = tmp / "src"
        (self.skill / rdp.REFERENCE_DIR).mkdir(parents=True)
        self.root.mkdir()

    def template(self, source: str, name: str = "retry_backoff.py") -> None:
        (self.skill / rdp.REFERENCE_DIR / name).write_text(source, encoding="utf-8")

    def manifest(self, text: str) -> None:
        (self.skill / rdp.REFERENCE_DIR / rdp.MANIFEST_NAME).write_text(
            text, encoding="utf-8"
        )

    def server(self, name: str, source: str, at: str = "src/http.py") -> None:
        path = self.root / name / at
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def servers(self, *sources: str) -> None:
        names = ("alpha-mcp", "beta-mcp", "gamma-mcp")
        for name, source in zip(names, sources, strict=True):
            self.server(name, source)

    def run(self, **kwargs):
        kwargs.setdefault("roots", [self.root])
        return rdp.run(self.skill, **kwargs)


class BedCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bed = Bed(Path(self._tmp.name))

    def codes(self, report) -> list[str]:
        return [f.code for f in report.findings]

    def details(self, report) -> str:
        return " | ".join(f.detail for f in report.findings)


# --------------------------------------------------------------------------
# Nothing to measure, and the manifest
# --------------------------------------------------------------------------


class NotMeasuredTest(BedCase):
    def test_no_reference_directory_is_not_measured(self):
        """A repository that ships no template has nothing to be stale about."""
        with tempfile.TemporaryDirectory() as tmp:
            report = rdp.run(Path(tmp))
        self.assertEqual(report.exit_code(), rdp.EXIT_NOT_MEASURED)
        self.assertEqual(report.findings, [])
        self.assertIn("nothing was measured", report.notes[0])

    def test_reference_without_python_is_not_measured(self):
        (self.bed.skill / rdp.REFERENCE_DIR / "NOTES.md").write_text("x", "utf-8")
        report = self.bed.run()
        self.assertEqual(report.exit_code(), rdp.EXIT_NOT_MEASURED)
        self.assertEqual(report.findings, [])


class ManifestTest(BedCase):
    def test_missing_manifest_is_the_first_finding_and_the_probe_stops(self):
        """A template with no declared adopters has an unknown blast radius."""
        self.bed.template(GOOD_TEMPLATE)
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["MANIFEST_MISSING"])
        self.assertEqual(report.exit_code(), rdp.EXIT_FINDINGS)
        # It stops there: no comparison was attempted against the servers that
        # are sitting right beside it.
        self.assertEqual(report.templates, [])
        self.assertIn("retry_backoff.py", self.details(report))

    def test_dunder_init_alone_is_not_a_template(self):
        self.bed.template("", name="__init__.py")
        report = self.bed.run()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), rdp.EXIT_NOT_MEASURED)

    def test_unmapped_template_is_a_finding(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.template(GOOD_TEMPLATE, name="pagination.py")
        self.bed.manifest(MANIFEST)
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertIn("TEMPLATE_UNMAPPED", self.codes(report))
        self.assertIn("pagination.py", self.details(report))

    def test_unmapped_ok_silences_it_and_the_exemption_is_visible(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.template(GOOD_TEMPLATE, name="pagination.py")
        self.bed.manifest(
            MANIFEST.replace(
                "schema = 1", 'schema = 1\nunmapped_ok = ["reference/pagination.py"]'
            )
        )
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertNotIn("TEMPLATE_UNMAPPED", self.codes(report))

    def test_schema_version_is_required(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST.replace("schema = 1", "schema = 2"))
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["MANIFEST_INVALID"])

    def test_adoption_without_since_is_invalid(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST.replace('since = "2026-05-14"\n', ""))
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["MANIFEST_INVALID"])
        self.assertIn("since", self.details(report))

    def test_since_must_be_a_date(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST.replace('"2026-05-14"', '"last spring"'))
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["MANIFEST_INVALID"])

    def test_template_without_adopters_is_invalid(self):
        self.bed.template(GOOD_TEMPLATE)
        head = MANIFEST.split("[[template.adoption]]")[0]
        self.bed.manifest(head)
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["MANIFEST_INVALID"])
        self.assertIn("blast radius", self.details(report))

    def test_unknown_property_kind_is_invalid(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST.replace('kind = "literal"', 'kind = "vibes"'))
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["MANIFEST_INVALID"])
        self.assertIn("vibes", self.details(report))

    def test_the_committed_example_parses(self):
        """`adoption.example.toml` is the format's reference; it must load."""
        templates, unmapped, ignore = rdp.load_manifest(ROOT / "adoption.example.toml")
        self.assertEqual(len(templates), 1)
        self.assertEqual(
            {p.id for p in templates[0].properties},
            {
                "reads_retry_after",
                "jitters",
                "caps_after_jitter",
                "wall_clock_budget",
                "no_bare_runtime_error",
            },
        )
        self.assertEqual(len(templates[0].sites), 3)
        self.assertEqual((unmapped, ignore), ([], []))


# --------------------------------------------------------------------------
# The renamed-but-correct adoption. The noise the design exists to avoid.
# --------------------------------------------------------------------------


class CorrectAdoptionTest(BedCase):
    def setUp(self):
        super().setUp()
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST)

    def test_renamed_adoption_is_clean(self):
        """Renamed constants, renamed function, aliased import, different
        capitalisation of the header, a different attempt count — all correct."""
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(report.findings, [], self.details(report))
        self.assertEqual(report.exit_code(), rdp.EXIT_GREEN)
        self.assertEqual(report.sites_read, 3)

    def test_reworded_message_is_not_drift(self):
        """Error text is prose. It is meant to be rewritten per server."""
        reworded = GOOD_SITE.replace(
            "raise UpstreamUnavailable(failure)",
            'raise UpstreamUnavailable(f"parliament API gave up: {failure}")',
        )
        self.bed.servers(reworded, reworded, reworded)
        self.assertEqual(self.bed.run().findings, [])

    def test_from_import_style_is_not_drift(self):
        """`from random import uniform` is the same call, written differently."""
        other = GOOD_SITE.replace(
            "import random as rnd", "from random import uniform"
        ).replace("rnd.uniform", "uniform")
        self.bed.servers(other, other, other)
        report = self.bed.run()
        self.assertEqual(report.findings, [], self.details(report))

    def test_one_site_with_an_extra_call_is_not_a_finding(self):
        """Ordinary variation between repositories. Unanimity is required."""
        chatty = GOOD_SITE.replace(
            "        failure = exc", "        failure = exc\n        log.warning(exc)"
        )
        self.bed.servers(chatty, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(report.findings, [], self.details(report))


# --------------------------------------------------------------------------
# REFERENCE_UNADOPTED — the server is behind the template
# --------------------------------------------------------------------------


class UnadoptedTest(BedCase):
    def setUp(self):
        super().setUp()
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST)

    def test_a_site_missing_a_declared_property(self):
        stale = GOOD_SITE.replace('client.headers.get("Retry-After")', "None")
        self.bed.servers(stale, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["REFERENCE_UNADOPTED"])
        detail = self.details(report)
        self.assertIn("acme/alpha-mcp", detail)
        self.assertIn("reads_retry_after", detail)
        # The finding names WHEN the mapping was declared, so a reader can tell
        # a fresh adoption from one that has been drifting for a year.
        self.assertIn("2026-05-14", detail)

    def test_cap_before_jitter_is_unadopted(self):
        """The ordering, not the presence. Both versions cap AND jitter."""
        unbounded = GOOD_SITE.replace(
            "time.sleep(min(CEILING, rnd.uniform(0.0, window)))",
            "time.sleep(min(CEILING, window) * rnd.uniform(0.8, 1.2))",
        )
        self.bed.servers(unbounded, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["REFERENCE_UNADOPTED"])
        self.assertIn("caps_after_jitter", self.details(report))

    def test_every_site_missing_it_yields_one_finding_each(self):
        stale = GOOD_SITE.replace("time.monotonic", "time.time")
        self.bed.servers(stale, stale, stale)
        report = self.bed.run()
        self.assertEqual(self.codes(report).count("REFERENCE_UNADOPTED"), 3)


# --------------------------------------------------------------------------
# REFERENCE_STALE — the template is behind the servers. The dangerous one.
# --------------------------------------------------------------------------


class StaleDeclaredTest(BedCase):
    def test_template_failing_its_own_property(self):
        self.bed.template(GOOD_TEMPLATE.replace("time.monotonic", "time.time"))
        self.bed.manifest(MANIFEST)
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertIn("REFERENCE_STALE", self.codes(report))
        detail = self.details(report)
        self.assertIn("wall_clock_budget", detail)
        self.assertIn("3 of 3 adoption site(s) read do", detail)
        # And no UNADOPTED noise: the sites are ahead, not behind.
        self.assertNotIn("REFERENCE_UNADOPTED", self.codes(report))

    def test_it_fires_with_no_checkouts_at_all(self):
        """The claim is about ONE file; it must not wait for the servers.

        A fresh manifest is written on a machine that usually has no server
        checkouts. Gating this on them meant the most important finding was
        invisible on exactly that run.
        """
        self.bed.template(GOOD_TEMPLATE.replace("time.monotonic", "time.time"))
        self.bed.manifest(MANIFEST)
        report = rdp.run(self.bed.skill)  # no --repos-root
        self.assertIn("REFERENCE_STALE", self.codes(report))
        self.assertEqual(report.exit_code(), rdp.EXIT_FINDINGS)
        detail = self.details(report)
        self.assertIn("wall_clock_budget", detail)
        # …and it says plainly what it did NOT measure.
        self.assertIn("was not measured", detail)
        self.assertEqual({u.code for u in report.unverified}, {"REPO_NOT_ON_DISK"})

    def test_no_unadopted_without_a_readable_site(self):
        """The other half genuinely needs the checkouts, and stays quiet."""
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST)
        report = rdp.run(self.bed.skill)
        self.assertEqual(report.findings, [], self.details(report))
        self.assertEqual(report.exit_code(), rdp.EXIT_NOT_MEASURED)

    def test_declared_and_implemented_nowhere_says_so(self):
        stale = GOOD_TEMPLATE.replace("time.monotonic", "time.time")
        self.bed.template(stale)
        self.bed.manifest(MANIFEST)
        site = GOOD_SITE.replace("time.monotonic", "time.time")
        self.bed.servers(site, site, site)
        report = self.bed.run()
        self.assertIn("REFERENCE_STALE", self.codes(report))
        self.assertIn("implemented nowhere", self.details(report))


class StaleUnanimityTest(BedCase):
    """The 2026-08-03 case, caught with nothing declared about it.

    The manifest here never mentions `RuntimeError`. That is the point: whoever
    forgets the fix in the template forgets to write the property down too, and
    a probe that only checks declared properties would call this clean.
    """

    def setUp(self):
        super().setUp()
        self.bed.template(
            GOOD_TEMPLATE.replace(
                "raise UpstreamUnavailable(last_error)",
                'raise RuntimeError(f"Upstream unreachable after retries: {last_error}")',
            )
        )
        self.bed.manifest(MANIFEST)
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)

    def test_convergent_removal_is_reported(self):
        report = self.bed.run()
        self.assertIn("REFERENCE_STALE", self.codes(report))
        detail = self.details(report)
        self.assertIn("convergent removal", detail)
        self.assertIn("raise:RuntimeError", detail)

    def test_convergent_addition_is_reported(self):
        report = self.bed.run()
        self.assertIn("convergent addition", self.details(report))
        self.assertIn("raise:UpstreamUnavailable", self.details(report))

    def test_it_never_produces_unadopted(self):
        """Unanimity speaks about the template only, by construction."""
        self.assertNotIn("REFERENCE_UNADOPTED", self.codes(self.bed.run()))

    def test_no_unanimity_flag_silences_it(self):
        report = self.bed.run(unanimity=False)
        self.assertEqual(report.findings, [], self.details(report))

    def test_the_floor_holds_it_back(self):
        report = self.bed.run(floor=4)
        self.assertEqual(report.findings, [], self.details(report))
        self.assertTrue(
            any("unanimity layer needs 4" in n for n in report.notes), report.notes
        )

    def test_ignore_list_silences_one_fact(self):
        self.bed.manifest(
            MANIFEST
            + '\n[unanimity]\nignore = ["raise:RuntimeError", "call:RuntimeError"]\n'
        )
        report = self.bed.run()
        self.assertNotIn("raise:RuntimeError", self.details(report))
        # …and the exemption is printed rather than applied invisibly.
        self.assertIn("raise:RuntimeError", report.ignored_facts)

    def test_one_dissenting_site_kills_unanimity(self):
        """Ten servers and one hold-out is not unanimity. It says nothing."""
        self.bed.servers(
            GOOD_SITE.replace(
                "raise UpstreamUnavailable(failure)", "raise RuntimeError(failure)"
            ),
            GOOD_SITE,
            GOOD_SITE,
        )
        report = self.bed.run()
        self.assertNotIn("convergent removal", self.details(report))


# --------------------------------------------------------------------------
# UNVERIFIED — never silently clean
# --------------------------------------------------------------------------


class UnverifiedTest(BedCase):
    def setUp(self):
        super().setUp()
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST)

    def test_no_checkouts_at_all_is_not_measured(self):
        report = self.bed.run()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), rdp.EXIT_NOT_MEASURED)
        self.assertEqual({u.code for u in report.unverified}, {"REPO_NOT_ON_DISK"})
        self.assertIn("NOT a clean run", report.notes[0])

    def test_partial_coverage_is_stated(self):
        self.bed.server("alpha-mcp", GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(report.sites_read, 1)
        self.assertEqual(report.sites_declared, 3)
        self.assertTrue(any("coverage: 1 of 3" in n for n in report.notes))

    def test_findings_from_the_readable_sites_still_stand(self):
        self.bed.server(
            "alpha-mcp", GOOD_SITE.replace('client.headers.get("Retry-After")', "None")
        )
        report = self.bed.run()
        self.assertEqual(self.codes(report), ["REFERENCE_UNADOPTED"])
        self.assertEqual(report.exit_code(), rdp.EXIT_FINDINGS)
        self.assertEqual(len(report.unverified), 2)

    def test_a_moved_symbol_is_unverified_not_clean(self):
        self.bed.servers(
            GOOD_SITE.replace("_fetch_with_backoff", "_fetch"), GOOD_SITE, GOOD_SITE
        )
        report = self.bed.run()
        codes = {u.code for u in report.unverified}
        self.assertEqual(codes, {"SITE_UNREADABLE"})
        # The mapping's own age is what makes it actionable.
        self.assertIn("2026-05-14", report.unverified[0].detail)

    def test_an_unreadable_template_compares_nothing(self):
        self.bed.template("def request_with_retry(:\n")
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        report = self.bed.run()
        self.assertEqual(report.findings, [])
        self.assertEqual([u.code for u in report.unverified], ["REFERENCE_UNREADABLE"])
        self.assertEqual(report.exit_code(), rdp.EXIT_NOT_MEASURED)

    def test_explicit_repo_path_resolves(self):
        elsewhere = self.bed.tmp / "elsewhere"
        (elsewhere / "src").mkdir(parents=True)
        (elsewhere / "src" / "http.py").write_text(GOOD_SITE, encoding="utf-8")
        report = rdp.run(
            self.bed.skill,
            roots=[self.bed.root],
            explicit={"acme/alpha-mcp": elsewhere},
        )
        self.assertEqual(report.sites_read, 1)

    def test_owner_scoped_layout_resolves(self):
        path = self.bed.root / "acme" / "alpha-mcp" / "src"
        path.mkdir(parents=True)
        (path / "http.py").write_text(GOOD_SITE, encoding="utf-8")
        self.assertEqual(self.bed.run().sites_read, 1)


# --------------------------------------------------------------------------
# The unit under the probe
# --------------------------------------------------------------------------


class UnitTest(unittest.TestCase):
    def unit(self, source: str, symbol: str = "") -> rdp.Unit:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.py"
            path.write_text(source, encoding="utf-8")
            unit, reason = rdp.load_symbol(path, symbol)
        self.assertEqual(reason, "")
        assert unit is not None
        return unit

    def test_alias_is_resolved(self):
        unit = self.unit(
            "import random as rnd\n\n\ndef f():\n    return rnd.uniform(0, 1)\n"
        )
        self.assertIn("random.uniform", [name for name, _ in unit.calls()])

    def test_from_import_is_resolved(self):
        unit = self.unit(
            "from random import uniform\n\n\ndef f():\n    return uniform(0, 1)\n"
        )
        self.assertIn("random.uniform", [name for name, _ in unit.calls()])

    def test_module_level_assignment_is_an_alias(self):
        """`_sleep = asyncio.sleep` so a test can patch the module attribute.

        Four servers in the portfolio bind the backoff sleep exactly this way.
        Read literally, `await _sleep(delay)` looks like a symbol that does not
        sleep, and the probe called four correct adoptions lagging.
        """
        unit = self.unit(
            "import asyncio\n\n_sleep = asyncio.sleep\n\n\n"
            "async def f():\n    await _sleep(1)\n"
        )
        self.assertIn("asyncio.sleep", [name for name, _ in unit.calls()])

    def test_assignment_alias_resolves_through_an_import_alias(self):
        unit = self.unit(
            "import asyncio as aio\n\n_sleep = aio.sleep\n\n\n"
            "async def f():\n    await _sleep(1)\n"
        )
        self.assertIn("asyncio.sleep", [name for name, _ in unit.calls()])

    def test_a_call_on_the_right_hand_side_is_not_an_alias(self):
        """`x = mod.factory()` binds a value, not another name for one."""
        unit = self.unit(
            "import mod\n\nclient = mod.factory()\n\n\ndef f():\n    return client.get()\n"
        )
        self.assertNotIn("mod.factory.get", [name for name, _ in unit.calls()])

    def test_a_rebinding_inside_a_function_is_not_an_alias(self):
        unit = self.unit(
            "import time\n\n\ndef f():\n    nap = time.sleep\n    return nap\n"
        )
        self.assertNotIn("nap", rdp._aliases(unit.node))

    def test_relative_import_is_left_alone(self):
        """A repo-internal module path differs per repository by construction."""
        unit = self.unit("from .http import retry\n\n\ndef f():\n    return retry()\n")
        self.assertIn("retry", [name for name, _ in unit.calls()])

    def test_intervening_call_yields_the_tail(self):
        unit = self.unit(
            "import random\n\n\ndef f():\n    return random.SystemRandom().uniform(0, 1)\n"
        )
        self.assertIn("uniform", [name for name, _ in unit.calls()])

    def test_tuple_except_is_read(self):
        unit = self.unit(
            "def f():\n    try:\n        pass\n    except (TimeoutError, OSError):\n        pass\n"
        )
        self.assertEqual(set(unit.caught()), {"TimeoutError", "OSError"})

    def test_bare_reraise_is_not_a_raised_type(self):
        unit = self.unit(
            "def f():\n    try:\n        pass\n    except OSError:\n        raise\n"
        )
        self.assertEqual(unit.raised(), [])

    def test_a_raise_is_not_also_a_call_fact(self):
        """Otherwise every changed exception is reported twice under two labels."""
        unit = self.unit("def f():\n    raise OSError('x')\n")
        self.assertIn("raise:OSError", unit.facts())
        self.assertNotIn("call:OSError", unit.facts())

    def test_the_same_name_called_elsewhere_keeps_its_call_fact(self):
        unit = self.unit("def f():\n    e = OSError('x')\n    raise OSError('y')\n")
        self.assertIn("call:OSError", unit.facts())

    def test_facts_are_last_segment_only(self):
        unit = self.unit(
            "import random as rnd\n\n\ndef f():\n    return rnd.uniform(0, 1)\n"
        )
        self.assertIn("call:uniform", unit.facts())

    def test_dotted_symbol_lookup(self):
        source = "class Upstream:\n    def fetch(self):\n        return 1\n"
        unit = self.unit(source, "Upstream.fetch")
        self.assertIsInstance(unit.node, rdp.ast.FunctionDef)

    def test_tail_match_is_symmetric(self):
        self.assertTrue(rdp._tail_match("random.uniform", "uniform"))
        self.assertTrue(rdp._tail_match("uniform", "random.uniform"))
        self.assertFalse(rdp._tail_match("time.time", "time.monotonic"))


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


class CliTest(BedCase):
    def test_json_report_carries_coverage_and_exit(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST)
        self.bed.servers(GOOD_SITE, GOOD_SITE, GOOD_SITE)
        out = self.bed.tmp / "r.json"
        code = rdp.main(
            [
                "--target",
                str(self.bed.skill),
                "--repos-root",
                str(self.bed.root),
                "--format",
                "json",
                "--report",
                str(out),
            ]
        )
        self.assertEqual(code, rdp.EXIT_GREEN)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["probe"], "reference-drift")
        self.assertEqual(payload["coverage"], {"sites_declared": 3, "sites_read": 3})
        self.assertEqual(payload["exit_code"], rdp.EXIT_GREEN)

    def test_malformed_repo_path_cannot_run(self):
        self.assertEqual(
            rdp.main(["--target", str(self.bed.skill), "--repo-path", "nope"]),
            rdp.EXIT_CANNOT_RUN,
        )

    def test_floor_below_one_cannot_run(self):
        self.assertEqual(
            rdp.main(["--target", str(self.bed.skill), "--floor", "0"]),
            rdp.EXIT_CANNOT_RUN,
        )

    def test_render_never_calls_an_unmeasured_run_clean(self):
        self.bed.template(GOOD_TEMPLATE)
        self.bed.manifest(MANIFEST)
        text = rdp.render(self.bed.run())
        self.assertIn("UNVERIFIED", text)
        self.assertIn("NOT MEASURED", text)
        self.assertNotIn("every readable adoption site agree", text)


class WrapsShapeTest(unittest.TestCase):
    """`wraps` asks about ORDER, and order can be written two ways.

    Measured against the eleven-server sweep of 2026-08-03: every one of the six
    repositories that applies the cap after the jitter binds a local first. Read
    lexically only, `caps_after_jitter` failed 6 of 6 adoptions that hold exactly
    the behaviour it describes — the finding pointed at correct code.
    """

    PROP = rdp.Property(
        id="caps_after_jitter",
        says="applies the ceiling after jittering",
        kind="wraps",
        outer="min",
        inner=("random.uniform",),
        expect="present",
    )

    def holds(self, body: str) -> bool:
        source = f"import random\n\nCAP = 20.0\n\n\ndef f(base):\n{body}"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.py"
            path.write_text(source, encoding="utf-8")
            unit, reason = rdp.load_symbol(path, "f")
        self.assertEqual(reason, "")
        assert unit is not None
        return rdp.holds(self.PROP, unit)

    def test_lexical_shape_holds(self):
        """min(base * random.uniform(...), CAP) — the inner call inside the args."""
        self.assertTrue(
            self.holds("    return min(base * random.uniform(0.5, 1.5), CAP)\n")
        )

    def test_name_bound_shape_holds(self):
        """jittered = ...; min(jittered, CAP) — what every real adopter writes."""
        self.assertTrue(
            self.holds(
                "    jittered = base * random.uniform(0.5, 1.5)\n"
                "    return min(jittered, CAP)\n"
            )
        )

    def test_the_wrong_order_still_fails(self):
        """The whole point of the property: a cap applied BEFORE the jitter.

        `min(base, CAP) * random.uniform(...)` contains a cap and a jitter and
        is not bounded — 20s times 1.5 is 30s. If this ever passes, the property
        has stopped measuring anything.
        """
        self.assertFalse(
            self.holds(
                "    capped = min(base, CAP)\n"
                "    return capped * random.uniform(0.5, 1.5)\n"
            )
        )

    def test_an_unrelated_local_does_not_count(self):
        """A name bound from something else must not satisfy the property."""
        self.assertFalse(
            self.holds("    plain = base * 2\n    return min(plain, CAP)\n")
        )

    def test_the_binding_must_be_inside_the_same_symbol(self):
        """Deliberately shallow: no transitive chains, no cross-function flow.

        A wider analysis would start reporting a cap where the value merely
        passed through, and a false positive is worse here than the false
        negative it replaces.
        """
        self.assertFalse(
            self.holds(
                "    jittered = base * random.uniform(0.5, 1.5)\n"
                "    passed_on = jittered\n"
                "    return min(passed_on, CAP)\n"
            )
        )


if __name__ == "__main__":
    unittest.main()
