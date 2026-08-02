#!/usr/bin/env python3
"""The quality-chain table in both READMEs, against the five members it claims.

This repository is the last link in a chain of five — probe before the build,
data fidelity and transport hardening in the build, audit after it, and this
one in operation. Until now it was also the only link that said so nowhere: the
four skills each carried a table naming their siblings, and this repository had
no such section at all. Where it was mentioned, in the probe skill's README, it
was a trailing sentence *after* the table, which reads as an afterthought
rather than as the fifth link.

WHAT THIS TEST CAN AND CANNOT REACH
-----------------------------------
It checks the table on disk. The thing that actually makes the five findable —
the shared GitHub topic `mcp-quality-chain` — lives outside every working copy
and is therefore unreachable from here; that is checked by
`tools/check_quality_chain.py` in `mcp-audit-skill`, the one repository that
carries the manifest. What is left for this test is the half it can measure:
that the table has not lost a member, and that it links the topic page.

The link matters more than it looks. Without it the table is a list that only
helps someone who already has one of the five open — which is exactly the state
the chain was in before, when the intersection of the five repositories' GitHub
topics was empty.

Stdlib-only, no network, no git.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MEMBERS = (
    "mcp-data-source-probe-skill",
    "mcp-data-fidelity-skill",
    "mcp-transport-hardening-skill",
    "mcp-audit-skill",
    "mcp-continuous-auditor",
)

TOPIC_URL = "https://github.com/topics/mcp-quality-chain"

# Both language versions, with the heading each one uses.
READMES = (
    ("README.md", "The MCP quality chain"),
    ("README.de.md", "Die MCP-Qualitätskette"),
)


def _section(readme: str, heading: str) -> str:
    """The body of the chain section, up to the next heading."""
    text = (ROOT / readme).read_text(encoding="utf-8")
    match = re.search(
        rf"^### {re.escape(heading)}\n(.*?)(?=^#{{2,3}} |\Z)", text, re.M | re.S
    )
    if match is None:
        raise AssertionError(
            f"{readme}: section '### {heading}' not found — the anchor was "
            "removed or reworded, so this test would otherwise silently stop "
            "checking anything"
        )
    return match.group(1)


class QualityChainTable(unittest.TestCase):
    def test_section_exists_in_both_languages(self) -> None:
        for readme, heading in READMES:
            with self.subTest(readme=readme):
                self.assertTrue(_section(readme, heading).strip())

    def test_every_member_is_named(self) -> None:
        for readme, heading in READMES:
            body = _section(readme, heading)
            for member in MEMBERS:
                with self.subTest(readme=readme, member=member):
                    self.assertIn(
                        member,
                        body,
                        f"{readme}: the chain table does not name {member}",
                    )

    def test_this_repository_is_in_its_own_table(self) -> None:
        """A table that lists the siblings and omits itself is the state this
        repository was already in — mentioned by others, silent about itself."""
        for readme, heading in READMES:
            with self.subTest(readme=readme):
                self.assertIn("mcp-continuous-auditor", _section(readme, heading))

    def test_topic_page_is_linked(self) -> None:
        for readme, heading in READMES:
            with self.subTest(readme=readme):
                self.assertIn(
                    TOPIC_URL,
                    _section(readme, heading),
                    f"{readme}: the shared topic page is not linked — without it "
                    "the table only helps someone who already found one of the five",
                )


if __name__ == "__main__":
    unittest.main()
