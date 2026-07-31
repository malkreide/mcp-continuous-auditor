#!/usr/bin/env python3
"""The in-memory-client dispatch: which "FastMCP" is this server?

TWO DIFFERENT PROJECTS carry the name and cannot share an environment:

  (a) ``mcp`` — the official SDK. Server class ``mcp.server.mcpserver.MCPServer``,
      renamed from ``mcp.server.fastmcp.FastMCP`` in the 2.0 break (old module
      removed, no shim). Client ``mcp.client.client.Client``.
  (b) ``fastmcp`` — a separate PyPI project on its own major line. Server class
      ``fastmcp.FastMCP``, client ``fastmcp.Client``. NOT renamed, still current.

``fastmcp`` still requires ``mcp`` 1.x, so alongside ``mcp`` 2.x even
``import fastmcp`` raises. That is why the auditor cannot pin either one: three
of its scripts run INSIDE the target's environment, and the portfolio contains
targets on both sides. Before this dispatch existed, a hard ``from fastmcp import
Client`` made the schema gate, the promptfoo provider and the recall canary
unrunnable against every target that had migrated to the 2.x SDK.

These tests pin the choosing, not the clients themselves — the decision is what
silently regresses, and it is decidable with fake objects and no SDK installed
at all. The real client construction is covered by ``test_smoke_target.py``,
which drives the (b) path end to end.
"""
from __future__ import annotations

import importlib.util
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_private(path: Path, name: str) -> types.ModuleType:
    """Load a module WITHOUT claiming its normal name in ``sys.modules``.

    ``generate_schemas`` reads ``MCP_SERVER_IMPORT`` at import time, and
    ``test_smoke_target`` sets that variable in ``setUpClass`` and then imports
    the module. Importing it here under its real name would leave the
    import-time default sitting in ``sys.modules``, so the smoke test's import
    would be a no-op and it would go looking for the real target package. That
    is not hypothetical — this file caused exactly that failure once.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # deliberately not registered
    return module


gs = _load_private(REPO / "schemas" / "generate_schemas.py",
                   "_generate_schemas_under_test")


def _server_from(module_name: str, class_name: str) -> object:
    """A stand-in server object whose __module__ is what the dispatch reads."""
    mod = types.ModuleType(module_name)
    cls = type(class_name, (), {})
    cls.__module__ = module_name
    mod.__dict__[class_name] = cls
    return cls()


class DispatchOrderTest(unittest.TestCase):
    """The order is chosen from the SERVER OBJECT's module, never from whatever
    happens to be importable — that is the one signal that cannot be wrong."""

    def _order(self, server: object) -> list[str]:
        seen: list[str] = []

        def make_sdk(_s: object) -> str:
            seen.append("sdk")
            raise ImportError("no mcp here")

        def make_fastmcp(_s: object) -> str:
            seen.append("fastmcp")
            raise ImportError("no fastmcp here")

        saved = (gs._sdk_client, gs._fastmcp_client)
        gs._sdk_client, gs._fastmcp_client = make_sdk, make_fastmcp
        try:
            with self.assertRaises(RuntimeError):
                gs.in_memory_client(server)
        finally:
            gs._sdk_client, gs._fastmcp_client = saved
        return seen

    def test_a_standalone_fastmcp_server_tries_fastmcp_first(self) -> None:
        self.assertEqual(self._order(_server_from("fastmcp.server.server", "FastMCP")),
                         ["fastmcp", "sdk"])

    def test_an_sdk_server_tries_the_sdk_first(self) -> None:
        self.assertEqual(self._order(_server_from("mcp.server.mcpserver", "MCPServer")),
                         ["sdk", "fastmcp"])

    def test_the_old_sdk_class_is_not_mistaken_for_the_standalone_package(self) -> None:
        # `mcp.server.fastmcp.FastMCP` is variant (a) under its OLD name. Its
        # module starts with `mcp`, not `fastmcp` — matching on the class name
        # would send it down the wrong branch, which is exactly the confusion
        # this whole task is about.
        self.assertEqual(self._order(_server_from("mcp.server.fastmcp", "FastMCP")),
                         ["sdk", "fastmcp"])

    def test_an_unknown_server_still_tries_both(self) -> None:
        self.assertEqual(self._order(_server_from("something.else", "Server")),
                         ["sdk", "fastmcp"])

    def test_the_failure_names_both_projects_and_why_they_conflict(self) -> None:
        def boom(_s: object) -> str:
            raise ImportError("nope")

        saved = (gs._sdk_client, gs._fastmcp_client)
        gs._sdk_client, gs._fastmcp_client = boom, boom
        try:
            with self.assertRaises(RuntimeError) as cm:
                gs.in_memory_client(_server_from("mcp.server.mcpserver", "MCPServer"))
        finally:
            gs._sdk_client, gs._fastmcp_client = saved
        msg = str(cm.exception)
        self.assertIn("mcp>=2", msg)
        self.assertIn("fastmcp", msg)
        self.assertIn("cannot be installed together", msg)


class ResultShapeTest(unittest.TestCase):
    """Two shapes genuinely differ between the projects. Both are read
    defensively, so neither SDK's spelling is assumed."""

    def test_list_tools_is_unwrapped_for_either_client(self) -> None:
        # mcp 2.x returns a ListToolsResult; fastmcp returns a plain list.
        wrapped = types.SimpleNamespace(tools=["a", "b"])
        self.assertEqual(gs._tools_of(wrapped), ["a", "b"])
        self.assertEqual(gs._tools_of(["a", "b"]), ["a", "b"])

    def test_the_output_schema_is_read_under_both_spellings(self) -> None:
        # mcp 2.x: output_schema. fastmcp: outputSchema.
        self.assertEqual(
            gs.output_schema_of(types.SimpleNamespace(output_schema={"t": 1})), {"t": 1})
        self.assertEqual(
            gs.output_schema_of(types.SimpleNamespace(outputSchema={"t": 2})), {"t": 2})
        self.assertIsNone(gs.output_schema_of(types.SimpleNamespace()))

    def test_a_pydantic_style_getattr_raiser_does_not_blow_up(self) -> None:
        # `mcp` 2.x tools are pydantic models: attribute access for a missing
        # field raises AttributeError rather than returning None. Reading with a
        # default is what keeps that from crashing the schema gate.
        class Strict:
            output_schema = {"ok": True}

            def __getattr__(self, item: str):
                raise AttributeError(item)

        self.assertEqual(gs.output_schema_of(Strict()), {"ok": True})


class EveryCallSiteDispatchesTest(unittest.TestCase):
    """All three scripts that drive a target server must go through the
    dispatch. A hard `from fastmcp import Client` anywhere is the regression —
    it is what made these three unrunnable against a 2.x target."""

    SITES = ("schemas/generate_schemas.py",
             "promptfoo/providers/call_tool.py",
             "scripts/recall_canary.py")

    def test_no_call_site_hard_imports_a_client(self) -> None:
        for rel in self.SITES:
            with self.subTest(site=rel):
                text = (REPO / rel).read_text(encoding="utf-8")
                self.assertIn("in_memory_client", text,
                              f"{rel} must dispatch, not pin an SDK")
                self.assertIn("def _fastmcp_client", text)
                self.assertIn("def _sdk_client", text)
                # Each client import must appear exactly once, and inside its
                # own branch helper — a second one anywhere is a hard pin
                # sneaking back in.
                lines = text.splitlines()
                for needle, owner in (("from fastmcp import Client", "def _fastmcp_client"),
                                      ("from mcp.client.client import Client", "def _sdk_client")):
                    idx = [i for i, ln in enumerate(lines) if ln.strip().startswith(needle)]
                    self.assertEqual(len(idx), 1,
                                     f"{rel}: expected exactly one `{needle}`, got {len(idx)}")
                    preceding = "\n".join(lines[max(0, idx[0] - 4):idx[0]])
                    self.assertIn(owner, preceding,
                                  f"{rel}: `{needle}` must sit inside {owner}")

    def test_the_smoke_fixture_is_still_the_standalone_package(self) -> None:
        # Deliberately variant (b): it exercises that branch of the dispatch.
        # Rewriting it to MCPServer would be the damaging search-and-replace.
        text = (REPO / "tests" / "fixtures" / "smoke_server.py").read_text(encoding="utf-8")
        self.assertIn("from fastmcp import FastMCP", text)
        self.assertIn("NOT the renamed SDK class", text)


if __name__ == "__main__":
    unittest.main()
