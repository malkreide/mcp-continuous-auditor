#!/usr/bin/env python3
"""Structural tests for the split promptfoo profiles (Analysis T-C / T-A).

Asserts the credential boundary is actually encoded in the YAML:
  * determ profile is KEY-LESS — no grader, no llm-rubric, no red-team;
  * graded profile carries the model layer — grader + llm-rubric + committed
    red-team cases tagged with metadata.pluginId, and NO generative `redteam:`
    block that `promptfoo eval` would silently ignore (the T-A trap);
  * the generative `redteam:` spec lives only in redteam/redteam.config.yaml.

Needs PyYAML. The rest of the suite is stdlib-only, so this self-skips when yaml
is absent (run e.g. `uv run --with pyyaml python -m unittest tests.test_promptfoo_profiles`).
"""

from __future__ import annotations

import unittest
from pathlib import Path

PF = Path(__file__).resolve().parents[1] / "promptfoo"

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_YAML = False


def _load(rel: str) -> dict:
    import yaml

    return yaml.safe_load((PF / rel).read_text(encoding="utf-8"))


def _assert_types(cfg: dict) -> set[str]:
    types: set[str] = set()
    for t in cfg.get("tests", []) or []:
        for a in t.get("assert") or []:
            if a.get("type"):
                types.add(str(a["type"]))
    return types


def _provider_ids(cfg: dict) -> list[str]:
    return [str(p.get("id", p)) for p in (cfg.get("providers") or [])]


@unittest.skipUnless(_HAVE_YAML, "PyYAML not installed")
class PromptfooProfilesTest(unittest.TestCase):
    def test_determ_profile_is_key_less(self) -> None:
        c = _load("promptfooconfig.determ.yaml")
        # No grader provider, no generative red-team, no model-graded assertions.
        self.assertNotIn("defaultTest", c, "determ must carry no grader provider")
        self.assertNotIn("redteam", c, "determ must have no generative red-team block")
        self.assertNotIn("llm-rubric", _assert_types(c), "determ must be key-less")
        # Only the in-process MCP provider (no model endpoint).
        self.assertTrue(any("call_tool.py" in pid for pid in _provider_ids(c)))

    def test_graded_profile_has_model_layer(self) -> None:
        c = _load("promptfooconfig.yaml")
        self.assertIn("defaultTest", c, "graded must set the cross-family grader")
        self.assertIn("llm-rubric", _assert_types(c), "graded must exercise llm-rubric")
        # The generative block must NOT be here (eval would ignore it — the T-A bug).
        self.assertNotIn("redteam", c)
        # Committed red-team cases are tagged so the classifier's redteam branch fires.
        plugins = {
            (t.get("metadata") or {}).get("pluginId") for t in (c.get("tests") or [])
        }
        for expected in ("prompt-injection", "pii", "sql-injection"):
            self.assertIn(
                expected, plugins, f"missing committed red-team case: {expected}"
            )

    def test_generative_redteam_spec_is_isolated(self) -> None:
        c = _load("redteam/redteam.config.yaml")
        self.assertIn("redteam", c, "the generative spec must live here")
        self.assertIn("plugins", c["redteam"])
        # It targets the same in-process provider (relative path from redteam/).
        self.assertTrue(any("call_tool.py" in pid for pid in _provider_ids(c)))


EVAL_PROFILES = ("promptfooconfig.determ.yaml", "promptfooconfig.yaml")


def _asserts(cfg: dict):
    for t in cfg.get("tests", []) or []:
        for a in t.get("assert") or []:
            yield t.get("description", "?"), a


@unittest.skipUnless(_HAVE_YAML, "PyYAML not installed")
class AssertionsThatCanRunTest(unittest.TestCase):
    """Three ways both eval profiles reported on something they never checked.

    Each was measured against zurich-opendata-mcp on 2026-10-14 and is held here
    structurally, so it fails in the unit suite rather than in a nightly run
    that nobody reads closely enough to see an ERROR where a PASS was expected.
    """

    def test_multiline_inline_javascript_returns_something(self) -> None:
        """promptfoo runs a multi-line inline value as a FUNCTION BODY. The
        `(() => { … })()` written over several lines evaluated and returned
        nothing — "Custom function must return a boolean", every run."""
        for profile in EVAL_PROFILES:
            for desc, a in _asserts(_load(profile)):
                v = a.get("value")
                if a.get("type") != "javascript" or not isinstance(v, str):
                    continue
                if v.startswith("file://") or "\n" not in v.rstrip():
                    continue
                # A `return` INSIDE the arrow function does not count — the old
                # IIFEs had plenty. It has to sit at the body's own top level,
                # i.e. on a line at the smallest indentation in the value.
                lines = [ln for ln in v.splitlines() if ln.strip()]
                indent = min(len(ln) - len(ln.lstrip()) for ln in lines)
                top = [
                    ln.strip() for ln in lines if len(ln) - len(ln.lstrip()) == indent
                ]
                with self.subTest(profile=profile, test=desc):
                    self.assertTrue(
                        any(ln.startswith("return") for ln in top),
                        "multi-line inline javascript with no top-level `return` "
                        "evaluates to undefined in promptfoo",
                    )

    def test_every_referenced_assertion_function_is_exported(self) -> None:
        import re

        refs = [
            (profile, desc, a["value"])
            for profile in EVAL_PROFILES
            for desc, a in _asserts(_load(profile))
            if isinstance(a.get("value"), str)
            and a["value"].startswith("file://assertions/")
        ]
        for profile, desc, v in refs:
            with self.subTest(profile=profile, test=desc):
                path, _, fn = v[len("file://") :].partition(":")
                self.assertTrue((PF / path).is_file(), v)
                js = (PF / path).read_text(encoding="utf-8")
                exports = re.search(r"module\.exports\s*=\s*\{([^}]*)\}", js)
                self.assertIsNotNone(exports, f"{path} exports nothing")
                exported = {n.strip() for n in exports.group(1).split(",")}
                self.assertIn(fn, exported, f"{v}: not exported")

    def test_every_schema_file_an_is_json_names_exists(self) -> None:
        """A deleted or renamed schema must not leave an assertion pointing at
        nothing."""
        for profile in EVAL_PROFILES:
            for desc, a in _asserts(_load(profile)):
                v = a.get("value")
                if a.get("type") == "is-json" and isinstance(v, str):
                    with self.subTest(profile=profile, test=desc):
                        self.assertTrue((PF / v[len("file://") :]).is_file(), v)

    def test_a_fixture_backed_query_is_the_query_it_was_recorded_with(self) -> None:
        """Each datastore fixture is a recorded response and carries the SQL it
        answered. A test sending other SQL stops being about that response — and
        `FROM "x"` stops reaching the tool at all once the target refuses tables
        that are not DataStore resources."""
        import json

        for profile in EVAL_PROFILES:
            for t in _load(profile).get("tests", []) or []:
                v = t.get("vars") or {}
                if v.get("tool") != "zurich_datastore_sql" or not v.get("fixture"):
                    continue
                fixture = json.loads(
                    (PF / "fixtures" / f"{v['fixture']}.json").read_text(
                        encoding="utf-8"
                    )
                )
                with self.subTest(profile=profile, test=t.get("description")):
                    self.assertEqual(
                        json.loads(v["args"])["params"]["sql"], fixture["result"]["sql"]
                    )

    def test_schema_files_name_a_resolvable_meta_schema(self) -> None:
        """draft-07's identifier is http://. Written as https://, Ajv cannot
        resolve it and every is-json against the file errors out — which hid,
        among other things, a test reading a layer the target never offered."""
        import json

        for f in sorted((PF.parent / "schemas").glob("*.json")):
            with self.subTest(schema=f.name):
                meta = json.loads(f.read_text(encoding="utf-8")).get("$schema", "")
                self.assertFalse(
                    meta.startswith("https://json-schema.org/draft-0"), meta
                )


if __name__ == "__main__":
    unittest.main()
