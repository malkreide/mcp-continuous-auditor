#!/usr/bin/env python3
"""The quality-chain table in both READMEs, and the tag its links are pinned to.

This repository is the last link in a chain that used to be five repositories —
probe before the build, data fidelity and transport hardening in the build,
audit after it, and this one in operation. Until this test existed it was also
the only link that said so nowhere: the four skills each carried a table naming
their siblings, and this repository had no such section at all.

WHAT CHANGED WITH `mcp-audit-skill v3.0.0`
------------------------------------------
The chain now counts SKILLS, not repositories. The four skills live in one tree
under `skills/` in `mcp-audit-skill`; `mcp-data-source-probe-skill`,
`mcp-data-fidelity-skill` and `mcp-transport-hardening-skill` are archived. The
membership itself is declared once, in that repository's
`docs/quality-chain.json`, and this test holds the local half against it.

`mcp-continuous-auditor` is deliberately NOT a member and is in the table
anyway: it is not a skill but the runtime that drives the chain. It answers no
question in a server's lifecycle — it keeps asking them.

WHY THE LINKS CARRY A TAG AND NOT `main`
----------------------------------------
Every link in that table makes a claim about what the skill SAYS — "its rule 5",
"its rules 1-4", "its step 1.4 recall ground truth". A claim pointed at `main`
can stop being true without a single byte changing here, and nothing would say
so. Pointed at a tag it can only go STALE, which is a state somebody can see.
That trade was made deliberately: a visible stale pin beats a silently moved
target.

So `PIN` is the one place the tag is written down, and the tests below hold
every content link in the documentation to it. Bumping the pin is one edit here
plus the links themselves — and the failure names each file that disagrees.

WHAT THIS TEST CAN AND CANNOT REACH
-----------------------------------
It reads files on disk. Three things are therefore out of reach, and none of
them is silently treated as passing:

* Whether `PIN` is still the LATEST release of `mcp-audit-skill`. That needs the
  network. This test asserts CONSISTENCY, never currency.
* Whether the pinned tag exists at all, and whether the paths behind those links
  resolve. Same reason.
* The shared GitHub topic `mcp-quality-chain`, which lives outside every working
  copy — checked by `tools/check_quality_chain.py` in `mcp-audit-skill`, the one
  repository that carries the manifest.

The link scan covers both READMEs and `docs/**/*.md` — the documentation. It
does not walk `tests/` or `scripts/`; a URL in a fixture is not a claim anyone
reads.

Stdlib-only, no network, no git.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Der Tag, gegen den dieses Repo die Kette zitiert. EINE Stelle, absichtlich.
PIN = "v3.0.0"

#: Die vier Skills — in Backticks, wie sie in der Tabelle stehen. Die Begrenzer
#: gehoeren zur Zusage: ohne sie wuerde `mcp-data-source-probe` auch in
#: `mcp-data-source-probe-skill` treffen, also im Namen des archivierten Repos,
#: und die Tabelle bestuende weiter, waehrend sie auf ein Grab zeigt.
MEMBERS = (
    "`mcp-data-source-probe`",
    "`mcp-data-fidelity`",
    "`mcp-transport-hardening`",
    "`mcp-audit`",
)

#: Kein Mitglied der Kette, steht aber in der Tabelle — siehe Modul-Docstring.
THIS_REPO = "`mcp-continuous-auditor`"

TOPIC_URL = "https://github.com/topics/mcp-quality-chain"

# Both language versions, with the heading each one uses.
READMES = (
    ("README.md", "The MCP quality chain"),
    ("README.de.md", "Die MCP-Qualitätskette"),
)

#: Ein Verweis in `mcp-audit-skill`, der auf einen Ref zeigt — also einer, der
#: etwas ueber INHALT behauptet. Der blosse Repo-Link ohne Pfad ist bewusst
#: nicht erfasst: «hier liegt das Repo» veraltet nicht.
PINNED_LINK = re.compile(
    r"github\.com/malkreide/mcp-audit-skill/(?:tree|blob|releases/tag)/([^/\s)]+)"
)


def _dokumente() -> list[Path]:
    """Beide READMEs und alles unter `docs/` — die Prosa, die jemand liest."""
    return [ROOT / name for name, _ in READMES] + sorted((ROOT / "docs").rglob("*.md"))


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
                    # assertIn statt assertFalse waere hier lesbarer, kippt aber
                    # das ganze README in die Fehlermeldung — und ein Befund,
                    # den niemand bis zum Ende liest, ist keiner.
                    self.assertTrue(
                        member in body,
                        f"{readme}: the chain table does not name {member}",
                    )

    def test_this_repository_is_in_its_own_table(self) -> None:
        """A table that lists the siblings and omits itself is the state this
        repository was already in — mentioned by others, silent about itself."""
        for readme, heading in READMES:
            with self.subTest(readme=readme):
                self.assertTrue(
                    THIS_REPO in _section(readme, heading),
                    f"{readme}: the chain table does not name {THIS_REPO} — the "
                    "runtime that drives the chain is missing from its own table",
                )

    def test_the_archived_repositories_are_not_linked(self) -> None:
        """Die drei Herkunftsrepos sind archiviert. Ein Link darauf fuehrt zwar
        noch irgendwohin — und genau das ist das Problem: Er sieht heil aus und
        zeigt auf einen Stand, der nicht mehr gepflegt wird."""
        tot = (
            "malkreide/mcp-data-source-probe-skill",
            "malkreide/mcp-data-fidelity-skill",
            "malkreide/mcp-transport-hardening-skill",
        )
        for pfad in _dokumente():
            text = pfad.read_text(encoding="utf-8")
            for name in tot:
                with self.subTest(datei=pfad.name, repo=name):
                    self.assertFalse(
                        f"github.com/{name}" in text,
                        f"{pfad.relative_to(ROOT)}: links {name}, which is "
                        "archived — the skill moved into mcp-audit-skill "
                        f"under skills/ as of {PIN}",
                    )

    def test_topic_page_is_linked(self) -> None:
        for readme, heading in READMES:
            with self.subTest(readme=readme):
                self.assertIn(
                    TOPIC_URL,
                    _section(readme, heading),
                    f"{readme}: the shared topic page is not linked — without it "
                    "the table only helps someone who already found one of them",
                )


class DerPin(unittest.TestCase):
    """Jeder Inhalts-Verweis nach `mcp-audit-skill` zeigt auf DENSELBEN Tag."""

    def test_es_gibt_ueberhaupt_verweise(self) -> None:
        """Leeres Ergebnis ist ein Befund, kein Bestehen.

        Faende der Scanner nichts, waeren alle Zusagen darunter erfuellt, ohne
        dass irgendetwas geprueft worden waere — der Fall, in dem ein Gate
        gruen meldet, weil es seinen Gegenstand verloren hat.
        """
        treffer = [
            (pfad, ref)
            for pfad in _dokumente()
            for ref in PINNED_LINK.findall(pfad.read_text(encoding="utf-8"))
        ]
        self.assertTrue(
            treffer,
            "Kein einziger Verweis auf mcp-audit-skill mit Ref gefunden — "
            "entweder ist die Ketten-Tabelle verschwunden oder das Suchmuster "
            "passt nicht mehr auf die URL-Form.",
        )

    def test_ANKER_die_kettentabelle_selbst_traegt_gepinnte_links(self) -> None:
        """Der repo-weite Scanner reicht hier NICHT, und das ist gemessen.

        Beim Bau dieser Datei wurde geprobt, die URLs im README in eine Form zu
        schreiben, die `PINNED_LINK` nicht trifft (`…mcp-audit-skill@v3.0.0`).
        Alle Zusagen darueber blieben gruen: Der Scanner fand anderswo noch
        Treffer, also war die Liste nicht leer, und fuer das README pruefte
        niemand mehr irgendetwas. Die Zusage waere still verschwunden — was
        genau die Bauart ist, gegen die dieses Portfolio sonst antritt.

        Deshalb hat jede Ketten-Tabelle ihren eigenen Anker: mindestens ein
        gepinnter Verweis IN IHR, nicht bloss irgendwo im Repo.
        """
        for readme, heading in READMES:
            with self.subTest(readme=readme):
                refs = PINNED_LINK.findall(_section(readme, heading))
                self.assertTrue(
                    refs,
                    f"{readme}: die Ketten-Tabelle enthaelt keinen einzigen "
                    "Verweis in der gepinnten URL-Form. Entweder zeigt sie "
                    "nicht mehr auf die Skills, oder ihre Links sind so "
                    "umgeschrieben, dass der Pin-Test sie nicht mehr sieht.",
                )

    def test_kein_verweis_zeigt_auf_einen_branch(self) -> None:
        for pfad in _dokumente():
            for ref in PINNED_LINK.findall(pfad.read_text(encoding="utf-8")):
                with self.subTest(datei=str(pfad.relative_to(ROOT)), ref=ref):
                    self.assertEqual(
                        ref,
                        PIN,
                        f"{pfad.relative_to(ROOT)}: verweist auf '{ref}' statt "
                        f"auf {PIN}. Zeigt ein Verweis auf 'main', kann seine "
                        "Aussage still falsch werden; zeigt er auf einen "
                        "anderen Tag, sagt diese Doku zwei Staende gleichzeitig.",
                    )

    def test_der_pin_ist_ein_tag(self) -> None:
        self.assertRegex(
            PIN,
            r"^v\d+\.\d+\.\d+$",
            "PIN muss ein Release-Tag sein — ein Branch- oder Commit-Name hier "
            "wuerde die Zusage dieses Moduls aushebeln, ohne einen Test rot zu "
            "machen.",
        )


if __name__ == "__main__":
    unittest.main()
