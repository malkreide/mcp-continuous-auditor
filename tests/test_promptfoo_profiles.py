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
        for a in (t.get("assert") or []):
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
            (t.get("metadata") or {}).get("pluginId")
            for t in (c.get("tests") or [])
        }
        for expected in ("prompt-injection", "pii", "sql-injection"):
            self.assertIn(expected, plugins, f"missing committed red-team case: {expected}")

    def test_generative_redteam_spec_is_isolated(self) -> None:
        c = _load("redteam/redteam.config.yaml")
        self.assertIn("redteam", c, "the generative spec must live here")
        self.assertIn("plugins", c["redteam"])
        # It targets the same in-process provider (relative path from redteam/).
        self.assertTrue(any("call_tool.py" in pid for pid in _provider_ids(c)))


if __name__ == "__main__":
    unittest.main()
