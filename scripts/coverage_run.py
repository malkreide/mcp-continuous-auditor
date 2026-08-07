#!/usr/bin/env python3
"""Der Treiber: eine Sonde ueber das ganze Portfolio, gegen das Manifest gezaehlt.

WARUM EIN TREIBER UND NICHT ELF SCHLEIFEN
-----------------------------------------
Die meisten Sonden hier messen EIN Ziel: ``--target <checkout>``, oder
``BOOT_TARGET_ROOT=<checkout>``. Damit sie ein Portfolio abdecken, gibt es zwei
Wege — jede Sonde bekommt ihre eigene Schleife, oder eine Stelle iteriert fuer
alle. Diese Datei ist der zweite Weg. Die Begruendung steht ausfuehrlich in
``docs/probes/README.md``; kurz:

* Die Rechnung, um die es geht — Nenner, begruendete Auslassung, «ohne
  Ergebnis» — stuende sonst elfmal im Baum. Sie war schon zweimal da und ist
  in einer der beiden Kopien falsch gewesen. Elf Kopien sind elf Chancen.
* Die Sonden benennen ihr Ziel nicht gleich: ``--target``, ``--dist``, eine
  Umgebungsvariable. Eine Schleife je Sonde erbt diesen Unterschied; ein
  Treiber uebersetzt ihn einmal, in :data:`PROBES`.
* Jede Sonde hat ihren eigenen Exit-Code-Vertrag (0/1/2/3/4/127, je nach
  Sonde verschieden belegt). Eine Schleife *innerhalb* der Sonde muesste
  daraus trotzdem ein Gesamtergebnis falten — also genau das, was hier steht,
  nur ohne dass es jemand testen kann.
* Sonden, die einen Server starten, binden Ports und lassen Prozesse zurueck.
  Das gehoert in einen Prozess je Ziel, nicht in eine Schleife im selben.

Was der Treiber NICHT tut: er ersetzt keine Sonde, die das Manifest schon
selbst liest. ``published_probe.py --manifest`` und ``pr_health.py --manifest``
messen mehrere Ziele in EINEM Lauf (ein venv, ein Token, eine HTTP-Sitzung) und
behalten ihren eingebauten Modus. Beide rechnen mit demselben
``scripts/coverage.py``.

DIE DREI ANTWORTEN, JE ZIEL
---------------------------
Ein Ziel endet in genau einer Kategorie, und die vierte ist der Grund fuer die
Datei:

* ``green`` — gemessen, nichts gefunden
* ``findings`` — gemessen, etwas gefunden
* ``not_measured`` — die Sonde kam zu keinem Urteil (Exit 3 / 4 / 127)
* ``missing`` — es gab gar keinen Lauf: kein Checkout, kein Prozess, nichts.
  Zaehlt gegen den Nenner und ist KEINE Deckung.
* ``skipped`` — begruendet ausgelassen, per Manifest oder ``--allow-skip``

Beispiele::

  python scripts/coverage_run.py --probe identity \\
      --manifest manifest.json --repos-root ~/portfolio
  python scripts/coverage_run.py --probe shipped --manifest manifest.json \\
      --allow-skip meteoswiss-mcp:"upstream down, Ticket #12" --format json
  python scripts/coverage_run.py --probe boot --manifest manifest.json \\
      --repos-root ~/portfolio -- --some-extra-flag

Exit-Codes:

  0   jedes Manifest-Ziel gemessen oder begruendet ausgelassen, nichts gefunden
  2   vollstaendig abgedeckt, Befunde vorhanden
  3   vollstaendig abgedeckt, keine Befunde, aber mindestens ein Ziel ohne Urteil
  1   Deckung UNVOLLSTAENDIG — der Lauf hat nicht ueberall hingesehen
  127 der Treiber selbst konnte nicht laufen

``1`` steht vor ``2``: ein Lauf, der nicht ueberall hingesehen hat, hat keinen
Befund erhoben, sondern gar nichts. Dieselbe Reihenfolge wie in
``published_probe.py`` und ``pr_health.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import coverage  # noqa: E402
import probe_provenance  # noqa: E402

HERE = Path(__file__).resolve().parent

GREEN = "green"
FINDINGS = "findings"
NOT_MEASURED = "not_measured"
MISSING = "missing"
SKIPPED = "skipped"

EXIT_GREEN = 0
EXIT_FINDINGS = 2
EXIT_NOT_MEASURED = 3
EXIT_CANNOT_RUN = 127

# Der Vertrag, den fast jede Sonde hier fuehrt (siehe docs/probes/*.md):
# 0 gruen, 2 Befund, 3 nicht gemessen, 4 der Baum hat sich waehrend des Laufs
# bewegt (probe_provenance), 127 die Sonde konnte nicht laufen.
_STD_GREEN = frozenset({0})
_STD_FINDINGS = frozenset({2})
_STD_NOT_MEASURED = frozenset({3, probe_provenance.EXIT_MOVED, 127})


def _no_env(target: str) -> dict[str, str]:
    """Die meisten Sonden nehmen ihr Ziel als Argument, nicht aus der Umgebung."""
    return {}


@dataclass(frozen=True)
class ProbeSpec:
    """Wie ein Ziel fuer EINE Sonde aussieht — und was ihre Exit-Codes heissen.

    :param shape: ``checkout`` (das Ziel ist ein Arbeitsverzeichnis, aufgeloest
        ueber ``--repos-root``) oder ``dist`` (das Ziel ist ein Paketname auf
        dem Index).
    """

    name: str
    script: str
    shape: str
    section: str
    field: str
    argv_of: Callable[[str], list[str]]
    env_of: Callable[[str], dict[str, str]] = _no_env
    green: frozenset[int] = _STD_GREEN
    findings: frozenset[int] = _STD_FINDINGS
    not_measured: frozenset[int] = _STD_NOT_MEASURED
    null_reason: str | None = None
    note: str = ""

    def classify(self, rc: int) -> str:
        if rc in self.green:
            return GREEN
        if rc in self.findings:
            return FINDINGS
        if rc in self.not_measured:
            return NOT_MEASURED
        # Ein Code, den der Vertrag nicht kennt, ist kein Befund. Ihn als
        # solchen zu buchen hiesse, dem Ziel einen Mangel anzuhaengen, den
        # niemand gemessen hat — die Sonde hat sich verschluckt, nicht der
        # Server. 124/137 (Timeout/SIGKILL) landen genau hier.
        return NOT_MEASURED


def _dist_argv(script: str, *extra: str) -> Callable[[str], list[str]]:
    return lambda target: [sys.executable, str(HERE / script), "--dist", target, *extra]


def _target_argv(script: str, *extra: str) -> Callable[[str], list[str]]:
    return lambda target: [
        sys.executable,
        str(HERE / script),
        "--target",
        target,
        *extra,
    ]


PROBES: dict[str, ProbeSpec] = {
    "identity": ProbeSpec(
        name="identity",
        script="identity_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=_target_argv("identity_probe.py"),
        # identity_probe fuehrt einen eigenen Vertrag: 1 ist der Befund, und 2
        # heisst "kein pyproject.toml" — also ein Ziel, das diese Sonde gar
        # nicht messen kann, und kein Mangel des Servers.
        green=frozenset({0}),
        findings=frozenset({1}),
        not_measured=frozenset({2, probe_provenance.EXIT_MOVED, 127}),
    ),
    "spec": ProbeSpec(
        name="spec",
        script="spec_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=_target_argv("spec_probe.py"),
    ),
    "lockfile": ProbeSpec(
        name="lockfile",
        script="lockfile_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=_target_argv("lockfile_probe.py"),
    ),
    "doc-claim": ProbeSpec(
        name="doc-claim",
        script="doc_claim_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=_target_argv("doc_claim_probe.py"),
    ),
    "parity": ProbeSpec(
        name="parity",
        script="parity_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=_target_argv("parity_probe.py"),
    ),
    "boot": ProbeSpec(
        name="boot",
        script="transport_boot_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        # Diese Sonde nimmt kein --target: ihr Ziel steht in der Umgebung.
        # Genau der Unterschied, den ein Treiber einmal uebersetzt und elf
        # Schleifen elfmal geerbt haetten.
        argv_of=lambda target: [sys.executable, str(HERE / "transport_boot_probe.py")],
        env_of=lambda target: {"BOOT_TARGET_ROOT": target},
    ),
    "rebind": ProbeSpec(
        name="rebind",
        script="rebind_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=lambda target: [sys.executable, str(HERE / "rebind_probe.py")],
        env_of=lambda target: {"BOOT_TARGET_ROOT": target},
        # 3 heisst hier "die Kontrolle ist nicht konfiguriert" — weder Pass
        # noch Befund, also nicht gemessen. Siehe docs/probes/README.md.
        note="Exit 3 = inbound allow-list nicht konfiguriert",
    ),
    "shipped": ProbeSpec(
        name="shipped",
        script="shipped_probe.py",
        shape="dist",
        section=coverage.SERVERS,
        field="pypi_dist",
        argv_of=_dist_argv("shipped_probe.py"),
        not_measured=frozenset({3, probe_provenance.EXIT_MOVED, 127}),
        null_reason="kein Paket auf dem Index",
    ),
    "yank": ProbeSpec(
        name="yank",
        script="yank_probe.py",
        shape="dist",
        section=coverage.SERVERS,
        field="pypi_dist",
        argv_of=_dist_argv("yank_probe.py"),
        null_reason="kein Paket auf dem Index",
    ),
    "live-schedule": ProbeSpec(
        name="live-schedule",
        script="live_schedule_probe.py",
        shape="checkout",
        section=coverage.REPOSITORIES,
        field="repository",
        argv_of=_target_argv("live_schedule_probe.py"),
        # Der Standardvertrag. Erwaehnenswert ist nur, wie oft die 3 hier
        # legitim ist: ein Repo ohne `live`-Marke hat keine Live-Suite, die
        # geplant laufen koennte. Das ist NICHT MESSBAR und nicht gruen — sonst
        # zaehlt jeder Server ohne Live-Tests als abgedeckt, und die Frage,
        # ob er welche braucht, faellt aus der Bilanz.
        note="3 = keine Live-Suite, dokumentierte Fremdabdeckung oder ein "
        "nicht lesbarer Workflow — gemessen wurde nichts",
    ),
}


@dataclass
class Outcome:
    """Was aus einem Manifest-Eintrag geworden ist."""

    id: str
    target: str
    status: str
    exit_code: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "status": self.status,
            "exit_code": self.exit_code,
            "detail": self.detail,
        }

    def line(self) -> str:
        rc = "—" if self.exit_code is None else str(self.exit_code)
        tail = f" — {self.detail}" if self.detail else ""
        return f"[{self.status:<12}] {self.id:<28} exit {rc:<4}{tail}"


def resolve_checkout(slug: str, repos_root: Path) -> Path:
    """``owner/name`` → ``<repos-root>/name``."""
    return repos_root / slug.rsplit("/", 1)[-1]


def run_probe(
    spec: ProbeSpec, target: str, extra: list[str], timeout: float
) -> tuple[int, str]:
    """Eine Sonde gegen ein Ziel. Rueckgabe ``(exit_code, detail)``.

    Als eigene Funktion, damit die Tests sie ersetzen koennen, ohne echte
    Sonden zu starten — dieselbe Naht wie ``pr_health._get``.
    """
    argv = [*spec.argv_of(target), *extra]
    env = {**os.environ, **spec.env_of(target)}
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built here, never a shell
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # 124 ist der Code, den `timeout(1)` im naechtlichen Lauf setzt. Ein
        # Hang ist "nicht gemessen", nie ein Befund — siehe nightly-audit.sh.
        return 124, f"Zeitueberschreitung nach {timeout:.0f}s"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode, tail[-1][:200] if tail else ""


@dataclass
class Sweep:
    spec: ProbeSpec
    total: int
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[coverage.Omission] = field(default_factory=list)

    def coverage(self) -> coverage.Coverage:
        # `missing` ist die einzige Kategorie, die keine Messung ist. `green`,
        # `findings` und `not_measured` sind alle drei ein Ergebnis: die Sonde
        # lief und hat gesagt, was sie sagen konnte.
        missing = [o.id for o in self.outcomes if o.status == MISSING]
        measured = [o for o in self.outcomes if o.status != MISSING]
        return coverage.build(self.total, len(measured), self.skipped, missing)

    def exit_code(self) -> int:
        cov = self.coverage()
        if not cov.complete:
            return coverage.EXIT_INCOMPLETE
        if any(o.status == FINDINGS for o in self.outcomes):
            return EXIT_FINDINGS
        if any(o.status == NOT_MEASURED for o in self.outcomes):
            return EXIT_NOT_MEASURED
        return EXIT_GREEN

    def render(self) -> str:
        cov = self.coverage()
        lines = [f"==> {self.spec.name} ueber das Manifest ({self.total} Eintraege)"]
        lines += [o.line() for o in self.outcomes]
        lines += [f"[{SKIPPED:<12}] {o.name:<28} {o.reason}" for o in self.skipped]
        lines += ["", cov.render(verb="gemessen"), cov.covered()]
        if self.spec.note:
            lines.append(f"({self.spec.note})")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "probe": self.spec.name,
            "coverage": self.coverage().as_dict(),
            "targets": [o.as_dict() for o in self.outcomes]
            + [
                {
                    "id": o.name,
                    "target": o.name,
                    "status": SKIPPED,
                    "exit_code": None,
                    "detail": o.reason,
                }
                for o in self.skipped
            ],
            "exit_code": self.exit_code(),
        }


def sweep(
    spec: ProbeSpec,
    manifest: Path,
    *,
    repos_root: Path | None,
    allow_skip: dict[str, str],
    extra: list[str],
    timeout: float,
    # Zur Laufzeit aufgeloest, nicht als Default gebunden: sonst haelt die
    # Signatur die Funktion fest, die beim Import galt, und ein Test, der
    # `run_probe` ersetzt, startet trotzdem echte Sonden.
    runner: Callable[[ProbeSpec, str, list[str], float], tuple[int, str]] | None = None,
) -> Sweep:
    runner = runner if runner is not None else run_probe
    total, entries, omissions = coverage.read_manifest(
        manifest,
        field=spec.field,
        section=spec.section,
        null_reason=spec.null_reason,
        value_of=(
            coverage.github_slug(manifest) if spec.field == "repository" else None
        ),
        omit_when=lambda raw: (
            "archiviert (read-only)"
            if spec.section == coverage.REPOSITORIES and raw.get("archived")
            else None
        ),
    )

    stray = coverage.unknown_skips(allow_skip, entries, omissions)
    if stray:
        raise coverage.ManifestError(
            "--allow-skip nennt Namen, die im Manifest nicht vorkommen: "
            + ", ".join(stray)
            + ". Ein Skip, der nichts ueberspringt, ist keine Entscheidung"
        )

    kept, by_flag = coverage.split_allowed(entries, allow_skip)
    run = Sweep(spec=spec, total=total, skipped=[*omissions, *by_flag])

    for entry in kept:
        if spec.shape == "checkout":
            if repos_root is None:  # in main() geprueft; hier fail-closed
                raise coverage.ManifestError(
                    f"coverage_run: {spec.name} braucht --repos-root"
                )
            path = resolve_checkout(entry.value, repos_root)
            if not path.is_dir():
                # NICHT stillschweigend weglassen und NICHT als Befund buchen.
                # Ein fehlender Checkout heisst, dass niemand hingesehen hat.
                run.outcomes.append(
                    Outcome(
                        id=entry.id,
                        target=str(path),
                        status=MISSING,
                        detail=f"kein Checkout unter {path} — nichts gemessen",
                    )
                )
                continue
            target = str(path)
        else:
            target = entry.value

        rc, detail = runner(spec, target, extra, timeout)
        run.outcomes.append(
            Outcome(
                id=entry.id,
                target=target,
                status=spec.classify(rc),
                exit_code=rc,
                detail=detail,
            )
        )

    return run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="coverage_run",
        description="Eine Sonde ueber jedes Manifest-Ziel, gegen den Nenner gezaehlt",
    )
    p.add_argument(
        "--probe",
        required=True,
        choices=sorted(PROBES),
        help="welche Sonde ueber das Portfolio laeuft",
    )
    p.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="coverage_manifest.py --format json aus dem Portfolio-Repo",
    )
    p.add_argument(
        "--repos-root",
        type=Path,
        help="Verzeichnis mit den Checkouts (Pflicht fuer die Sonden, die eines "
        "brauchen); ein Ziel ohne Checkout gilt als NICHT gemessen",
    )
    p.add_argument(
        "--allow-skip",
        action="append",
        default=[],
        metavar="NAME:GRUND",
        help="einen Manifest-Eintrag begruenden statt pruefen; wiederholbar",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Obergrenze je Ziel in Sekunden (default: 900). Eine Ueberschreitung "
        "ist 'nicht gemessen', nie ein Befund",
    )
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--report", type=Path, help="JSON zusaetzlich hierhin schreiben")
    p.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="nach `--`: Argumente, die unveraendert an die Sonde gehen",
    )
    args = p.parse_args(argv)

    spec = PROBES[args.probe]
    extra = [a for a in args.rest if a != "--"]

    if spec.shape == "checkout" and args.repos_root is None:
        print(
            f"coverage_run: --probe {spec.name} misst einen Checkout und braucht "
            "--repos-root",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    allow_skip = coverage.parse_allow_skip(args.allow_skip)
    run = sweep(
        spec,
        args.manifest,
        repos_root=args.repos_root,
        allow_skip=allow_skip,
        extra=extra,
        timeout=args.timeout,
    )

    payload = run.as_dict()
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(run.render())

    if args.report:
        try:
            args.report.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(
                f"coverage_run: {args.report} nicht schreibbar: {exc}", file=sys.stderr
            )

    rc = run.exit_code()
    if rc == coverage.EXIT_INCOMPLETE:
        print(
            f"coverage_run: Deckung unvollstaendig ({run.coverage().covered()}) — "
            "'nicht hingesehen' und 'nichts gefunden' sind verschiedene Aussagen",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
