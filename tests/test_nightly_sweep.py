#!/usr/bin/env python3
"""Der Sweep in ``scripts/nightly-audit.sh`` — die committete Funktion, gefahren.

Bis 2026-08-06 stand der Zielname als Konstante in Zeile 168 des Skripts. Der
naechtliche Lauf berichtete damit ueber EINEN Server von 44, und nichts in der
Ausgabe sagte das. Diese Datei haelt die Ablösung fest:

* die Zielliste kommt aus dem Manifest, nicht aus einer Konstante;
* ein Kind, das HART fehlschlaegt (Exit 1), ist KEINE Deckung — die Gates sind
  dort nicht gelaufen;
* ein begruendeter Skip ist Deckung und nennt seinen Grund;
* das Skript weigert sich zu laufen, wenn weder Manifest noch Ziel gesetzt ist.

Wie ``test_gate_timeouts.py`` wird die echte Funktion aus dem committeten
Skript herausgeloest und in bash getrieben — keine Kopie dessen, was sie tun
soll. Das Kind ist dieselbe Datei; im Harness ist das ein Stub, der einen
vorgegebenen Exit-Code liefert.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "nightly-audit.sh"


def _extract_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


def _manifest(entries: list[dict]) -> Path:
    p = Path(tempfile.mkdtemp(prefix="nightly-sweep-")) / "manifest.json"
    p.write_text(json.dumps({"repositories": entries}), encoding="utf-8")
    return p


def _repo(name: str, *, archived: bool = False) -> dict:
    return {
        "id": name,
        "repository": f"https://github.com/o/{name}",
        "archived": archived,
    }


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("timeout"), "bash or timeout missing"
)
class SweepTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _extract_function(
            SCRIPT.read_text(encoding="utf-8"), "sweep_over_manifest"
        )

    def _run(
        self,
        manifest: Path,
        rc_map: dict[str, int],
        allow_skip: str = "",
    ) -> tuple[int, str]:
        """Fahre die echte Funktion; das Kind ist ein Stub mit festem Exit-Code."""
        work = Path(tempfile.mkdtemp(prefix="nightly-sweep-work-"))
        rc_file = work / "rc.map"
        rc_file.write_text(
            "".join(f"{slug} {rc}\n" for slug, rc in rc_map.items()), encoding="utf-8"
        )
        harness = work / "harness.sh"
        harness.write_text(
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            f'HERE="{REPO / "scripts"}"\n'
            f'AUDIT_DIR="{work / "audit"}"\n'
            'AUDIT_ALLOW_SKIP="${AUDIT_ALLOW_SKIP:-}"\n'
            'SWEEP_TIMEOUT_TARGET="${SWEEP_TIMEOUT_TARGET:-30}"\n'
            f"{self.body}\n"
            'if [ -n "${NIGHTLY_SWEEP_CHILD:-}" ]; then\n'
            f'  rc="$(awk -v k="${{TARGET_REPO}}" \'$1==k{{print $2}}\' "{rc_file}")"\n'
            '  exit "${rc:-0}"\n'
            "fi\n"
            'sweep_over_manifest "$1" main\n',
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["bash", str(harness), str(manifest)],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "AUDIT_ALLOW_SKIP": allow_skip,
            },
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_the_target_list_comes_from_the_manifest(self) -> None:
        rc, out = self._run(
            _manifest([_repo("a-mcp"), _repo("b-mcp")]),
            {"o/a-mcp": 0, "o/b-mcp": 0},
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("a-mcp", out)
        self.assertIn("b-mcp", out)
        self.assertIn("2/2 abgedeckt — vollstaendig", out)

    def test_a_hard_failing_child_is_not_coverage(self) -> None:
        """Gegenprobe 1 auf der Shell-Seite: Exit != 0 UND der Name im Bericht.

        Exit 1 heisst hier «ein Gate konnte nicht laufen». Das ist weder ein
        sauberes Ziel noch ein Befund — es ist ein ungemessenes, und es muss den
        Sweep seine Vollstaendigkeit kosten statt zu verschwinden.
        """
        rc, out = self._run(
            _manifest([_repo("a-mcp"), _repo("kaputt-mcp")]),
            {"o/a-mcp": 0, "o/kaputt-mcp": 1},
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("OHNE ERGEBNIS: kaputt-mcp", out)
        self.assertIn("1/2 abgedeckt — UNVOLLSTAENDIG", out)

    def test_a_hanging_child_is_unmeasured_and_not_a_finding(self) -> None:
        rc, out = self._run(
            _manifest([_repo("haengt-mcp")]),
            {"o/haengt-mcp": 124},
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("OHNE ERGEBNIS: haengt-mcp (exit 124)", out)

    def test_findings_keep_the_sweep_complete_and_exit_two(self) -> None:
        rc, out = self._run(
            _manifest([_repo("a-mcp"), _repo("b-mcp")]),
            {"o/a-mcp": 0, "o/b-mcp": 2},
        )
        self.assertEqual(rc, 2, out)
        self.assertIn("2/2 abgedeckt — vollstaendig", out)

    def test_an_archived_repo_is_a_named_skip_and_still_counts(self) -> None:
        rc, out = self._run(
            _manifest([_repo("a-mcp"), _repo("alt-mcp", archived=True)]),
            {"o/a-mcp": 0},
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("uebersprungen: o/alt-mcp (archiviert", out)
        self.assertIn("2/2 abgedeckt — vollstaendig", out)

    def test_a_reasoned_allow_skip_is_green_and_prints_the_reason(self) -> None:
        """Gegenprobe 2 auf der Shell-Seite."""
        rc, out = self._run(
            _manifest([_repo("a-mcp"), _repo("b-mcp")]),
            {"o/a-mcp": 0},
            allow_skip="b-mcp:upstream down, Ticket #12",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("upstream down, Ticket #12", out)
        self.assertIn("2/2 abgedeckt — vollstaendig", out)

    def test_an_empty_manifest_aborts_rather_than_reporting_zero_of_zero(self) -> None:
        """Gegenprobe 3 auf der Shell-Seite."""
        rc, out = self._run(_manifest([]), {})
        self.assertNotEqual(rc, 0)
        self.assertNotIn("0/0 abgedeckt — vollstaendig", out)


@unittest.skipUnless(shutil.which("bash"), "bash missing")
class TargetSelectionTest(unittest.TestCase):
    """Die entfernte Konstante — und was jetzt an ihrer Stelle passiert."""

    def test_the_hardcoded_default_target_is_gone(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(
            'TARGET_REPO="${TARGET_REPO:-malkreide/zurich-opendata-mcp}"',
            text,
            "a nightly run that picks its own target reports on one server of 44 "
            "and says nothing about the other 43",
        )

    def _run(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", **env},
        )

    def test_no_target_and_no_manifest_refuses_to_run(self) -> None:
        proc = self._run({})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("weder TARGET_REPO noch AUDIT_MANIFEST", proc.stderr)

    def test_a_manifest_and_a_target_together_are_refused(self) -> None:
        """Beides gesetzt heisst: unklar, worueber der Lauf berichtet."""
        proc = self._run({"AUDIT_MANIFEST": "/nonexistent.json", "TARGET_REPO": "o/a"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("schliessen sich aus", proc.stderr)


if __name__ == "__main__":
    unittest.main()
