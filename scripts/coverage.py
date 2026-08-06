#!/usr/bin/env python3
"""Die Deckungsschicht, die jeder Portfolio-Lauf teilt.

WARUM ES DIESE DATEI GIBT
-------------------------
Am 2026-07-31 meldete ein Portfolio-Lauf «33 von 33 ok». Der Satz war wahr und
die Menge war falsch: ``portfolio.json`` listete 43 aktive Server, zehn davon
waren nie im Lauf. Nichts widersprach der Zahl, weil nichts sie je gegen die
Quelle der Wahrheit hielt — der Nenner kam aus derselben Liste wie der Zaehler.

Der Kernsatz dieses Repositories, eine Ebene weiter aussen:

    «Ich habe nicht hingesehen» und «da war nichts» duerfen sich nicht
    denselben Exit-Code teilen.

``coverage_manifest.py --format json`` im Portfolio-Repo ist die Quelle der
Wahrheit. Diese Datei ist die eine Stelle, an der sie gelesen, validiert und
gegen das Ergebnis eines Laufs gehalten wird. Vorher stand dieselbe Logik
zweimal im Baum — in ``published_probe.py`` und in ``pr_health.py``, leicht
verschieden, mit je einem eigenen Nenner. Eine Rechnung, die an zwei Stellen
steht, ist eine Rechnung, die an zwei Stellen falsch werden kann; der Beleg
dafuer steht unten bei :class:`Coverage`.

WAS HIER VALIDIERT WIRD, UND WARUM JEDES EINZELNE
-------------------------------------------------
Jede der Pruefungen unten stammt aus einem echten Befund. Alle enden ohne sie
im selben Zustand: **falsches Gruen**.

* Der **Abschnitt fehlt** (kein ``servers``/``repositories``-Schluessel).
  Optimistisch gelesen ist das eine leere Liste — der Lauf meldet «0/0
  geprueft» und Exit 0. Fehlend und leer sind verschiedene Aussagen: leer
  heisst «dieses Portfolio hat keine Ziele», fehlend heisst «dieses Manifest
  passt nicht zu diesem Werkzeug».
* Der **Abschnitt ist leer**. Auch das ist «0/0 geprueft» mit Exit 0 und von
  einem vollstaendig gepruefen Portfolio nicht zu unterscheiden.
* Das **Feld fehlt am Eintrag** (``pypi_dist``, ``repository``). Wird ein
  fehlendes Feld wie ein explizites ``null`` gelesen, macht ein umbenanntes
  Feld beim Erzeuger jeden Eintrag zur «begruendeten Auslassung»: nichts
  gemessen, Deckung vollstaendig, Exit 0.
* Der **Wert ist weder Name noch null**. Ein leerer String oder eine Zahl
  wuerde als Ziel durchgehen und erst beim Werkzeug scheitern — dort dann als
  «nicht erhoben» statt als das, was es ist: ein kaputter Manifest-Eintrag.

ZUM NAMEN
---------
``coverage`` ist auch der Name des bekannten Test-Coverage-Pakets. Die Sonden
legen ``scripts/`` an ``sys.path[0]``, also gewinnt diese Datei im Prozess.
Das ist gewollt (das Modul heisst nach dem, was es tut) und harmlos, solange
der Auditor selbst kein ``coverage.py`` benutzt — er misst mit ``unittest``.
Wer hier je ein Coverage-Werkzeug einzieht, benennt entweder das Werkzeug oder
diese Datei um; ein stiller Import-Konflikt ist genau die Sorte Fehler, die
dieses Modul sonst verhindert.

STDLIB-ONLY, KEIN NETZ, KEIN GIT — die Tests besitzen diese Datei ganz.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Deckung unvollstaendig. Bewusst NICHT der Befund-Code der Sonden (2): ein
# Lauf, der nicht ueberall hingesehen hat, hat keinen Befund erhoben — er hat
# gar nichts erhoben. Wer die beiden auf denselben Code legt, hat den Satz
# aufgegeben, um den es hier geht.
EXIT_INCOMPLETE = 1

# Die beiden Bloecke, die `coverage_manifest.py --format json` ausgibt.
SERVERS = "servers"
REPOSITORIES = "repositories"


@dataclass(frozen=True)
class Entry:
    """Ein pruefbares Ziel aus dem Manifest.

    ``raw`` traegt den ganzen Eintrag mit, damit eine Sonde ihre eigenen
    Zusatzfelder lesen kann (``start_event``, ``archived``, …), ohne dass
    dieses Modul sie alle kennen muss.
    """

    id: str
    value: str
    raw: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


@dataclass(frozen=True)
class Omission:
    """Ein Eintrag, der nicht geprueft wurde — mit dem Grund dafuer.

    Der Grund ist kein Kommentar, er ist der Mechanismus. Eine Auslassung ohne
    Grund ist keine Auslassung, sondern eine Luecke mit einem Alibi.
    """

    name: str
    reason: str


class ManifestError(SystemExit):
    """Abbruch mit Erklaerung. ``SystemExit``, damit jede Sonde ohne eigenes
    ``except`` mit Exit 1 und einer lesbaren Zeile stehen bleibt."""


def read_manifest(
    path: Path,
    *,
    field: str,
    section: str = SERVERS,
    null_reason: str | None = None,
    value_of: Callable[[str, Any], str] | None = None,
    omit_when: Callable[[dict[str, Any]], str | None] | None = None,
) -> tuple[int, list[Entry], list[Omission]]:
    """``(total, targets, omissions)`` aus einem Deckungs-Manifest.

    :param field: das Feld, das ein Ziel benennt (``pypi_dist``,
        ``repository``). Fehlt es an einem Eintrag, bricht der Lauf ab —
        fehlend und ``null`` sind verschiedene Aussagen.
    :param section: ``servers`` oder ``repositories``.
    :param null_reason: was ``null`` in diesem Feld bedeutet. Ist es gesetzt,
        wird ``null`` zur begruendeten Auslassung (``pypi_dist: null`` — der
        Server veroeffentlicht kein Paket). Ist es ``None``, ist ``null`` ein
        Fehler: fuer ``repository`` gibt es keine sinnvolle Lesart davon.
    :param value_of: normalisiert/validiert den Rohwert (``(id, raw) -> str``)
        und darf :class:`ManifestError` werfen. Ohne Angabe muss der Wert ein
        nicht-leerer String sein.
    :param omit_when: entscheidet je Eintrag, ob er aus einem im Manifest
        stehenden Grund uebersprungen wird (``archived: true``) — Rueckgabe ist
        der Grund oder ``None``.

    **``total`` zaehlt JEDEN Eintrag des Abschnitts**, pruefbar oder nicht.
    Ein Nenner, der nur die pruefbaren zaehlt, haengt von derselben
    Einschaetzung ab, die der Deckungscheck pruefen soll — siehe
    :class:`Coverage`.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"{path}: nicht lesbar: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: kein gueltiges JSON: {exc}") from exc

    if not isinstance(data, dict) or section not in data:
        raise ManifestError(
            f"{path}: kein Feld {section!r}. Fehlend und leer sind verschiedene "
            f"Aussagen: leer heisst 'dieses Portfolio hat keine Eintraege', "
            f"fehlend heisst 'dieses Manifest passt nicht zu diesem Werkzeug' "
            f"(erzeugt coverage_manifest.py --format json den Block schon?)"
        )

    entries = data[section]
    if not isinstance(entries, list):
        raise ManifestError(f"{path}: {section!r} ist keine Liste")
    if not entries:
        raise ManifestError(
            f"{path}: leere Zielliste in {section!r}. Ein Lauf ohne Ziele meldet "
            "sonst '0/0 geprueft' und Exit 0 — nicht unterscheidbar von einem "
            "vollstaendig gepruefen Portfolio"
        )

    targets: list[Entry] = []
    omissions: list[Omission] = []
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict) or "id" not in raw:
            raise ManifestError(f"{path}: {section}[{i}] hat kein 'id'-Feld")
        ident = str(raw["id"])

        if field not in raw:
            raise ManifestError(
                f"{path}: Eintrag {ident} hat kein Feld {field!r}. Fehlend und "
                f"null sind verschiedene Aussagen: null heisst "
                f"{null_reason or 'kein Wert'!r}, fehlend heisst 'das Manifest "
                f"passt nicht zu diesem Werkzeug'"
            )

        value = raw[field]
        if value is None:
            if null_reason is None:
                raise ManifestError(
                    f"{path}: Eintrag {ident}: {field!r} ist null. Fuer dieses "
                    "Werkzeug gibt es davon keine Lesart"
                )
            omissions.append(Omission(ident, f"{null_reason} (laut Manifest)"))
            continue

        resolved = (
            value_of(ident, value)
            if value_of is not None
            else _plain_name(path, ident, field, value)
        )

        reason = omit_when(raw) if omit_when is not None else None
        if reason:
            omissions.append(Omission(resolved, reason))
            continue

        targets.append(Entry(id=ident, value=resolved, raw=raw))

    return len(entries), targets, omissions


def _plain_name(path: Path, ident: str, field: str, value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ManifestError(
        f"{path}: Eintrag {ident}: {field!r} ist weder Name noch null ({value!r})"
    )


def github_slug(path: Path) -> Callable[[str, Any], str]:
    """``value_of`` fuer ein ``repository``-Feld: ``owner/name``.

    Nicht nur die Schraegstriche zaehlen: ``git@github.com:o/a.git`` hat genau
    einen und kaeme sonst als Slug durch, um dann bei jedem Repo als HTTP-Fehler
    zu enden — also als «nicht erhoben» statt als das, was es ist: ein kaputter
    Eintrag im Manifest.
    """

    def resolve(ident: str, value: Any) -> str:
        url = str(value).rstrip("/")
        slug = url.removeprefix("https://github.com/")
        parts = slug.split("/")
        if not url.startswith("https://github.com/") or len(parts) != 2:
            raise ManifestError(
                f"{path}: {ident}: 'repository' ist keine "
                f"github.com/<owner>/<name>-URL ({url!r})"
            )
        if not all(parts):
            raise ManifestError(
                f"{path}: {ident}: 'repository' ist keine "
                f"github.com/<owner>/<name>-URL ({url!r})"
            )
        return slug

    return resolve


def parse_allow_skip(items: Iterable[str], *, noun: str = "name") -> dict[str, str]:
    """``name:grund``-Paare. Der Grund ist Pflicht — das ist der Mechanismus.

    Auslassen ist erlaubt, stilles Auslassen nicht. Der Grund gehoert auf die
    Kommandozeile, damit er in der Ausgabe des Laufs landet statt bei dem
    Menschen, der den Befehl getippt hat.
    """
    out: dict[str, str] = {}
    for item in items:
        name, sep, reason = item.partition(":")
        if not sep or not reason.strip() or not name.strip():
            raise ManifestError(
                f"--allow-skip {item!r}: erwartet '{noun}:grund'. Ohne Grund ist "
                "ein uebersprungener Eintrag von einem vergessenen nicht zu "
                "unterscheiden"
            )
        out[name.strip()] = reason.strip()
    return out


def split_allowed(
    targets: Sequence[Entry], allowed: dict[str, str]
) -> tuple[list[Entry], list[Omission]]:
    """Zerlegt die Zielliste an ``--allow-skip``.

    Ein Eintrag darf ueber seine ``id`` ODER ueber seinen aufgeloesten Wert
    benannt werden: auf der Kommandozeile steht mal der Dist-Name, mal der
    Repo-Slug, und beides ist derselbe Server.
    """
    kept: list[Entry] = []
    skipped: list[Omission] = []
    for entry in targets:
        reason = allowed.get(entry.id) or allowed.get(entry.value)
        if reason:
            skipped.append(Omission(entry.value, reason))
        else:
            kept.append(entry)
    return kept, skipped


def unknown_skips(
    allowed: dict[str, str], targets: Sequence[Entry], omissions: Sequence[Omission]
) -> list[str]:
    """``--allow-skip`` fuer Namen, die im Manifest gar nicht vorkommen.

    Ein Tippfehler im Skip ist unsichtbar: der Eintrag wird trotzdem geprueft,
    der Skip bewirkt nichts, und der Lauf sieht aus wie beabsichtigt. Der
    gefaehrliche Fall ist der umgekehrte — der Name existierte einmal und wurde
    im Manifest umbenannt; dann glaubt der Operator, ein Ziel bewusst
    auszulassen, waehrend ein anderes unbemerkt hinzukam.
    """
    known = {e.id for e in targets} | {e.value for e in targets}
    known |= {o.name for o in omissions}
    return sorted(name for name in allowed if name not in known)


@dataclass(frozen=True)
class Coverage:
    """Die Rechnung, gegen die ein Lauf seine eigene Zahl haelt.

    DER NENNER
    ----------
    ``expected`` ist die Zahl ALLER Manifest-Eintraege, nicht die der pruefbaren.
    Die erste Fassung zaehlte nur die pruefbaren und kam bei einem vollstaendigen
    Lauf auf ``2 geprueft + 1 uebersprungen = 3`` gegen ein erwartetes ``2``:
    Exit 1 bei lauter gruenen Ergebnissen. Ein Nenner, der von derselben
    Einschaetzung abhaengt, die der Deckungscheck pruefen soll, prueft nichts.
    ``tests/test_coverage.py`` haelt genau diesen Fall fest.

    ``missing`` ist die Kategorie, fuer die es die Datei gibt: ein Ziel, das
    weder ein Ergebnis noch einen Grund hat. Es zaehlt gegen den Nenner und ist
    KEINE Deckung — ein HTTP 404, ein fehlender Checkout oder eine Sonde, die
    unterwegs geplatzt ist, heissen nicht «da war nichts».
    """

    expected: int
    accounted: int
    skipped: tuple[Omission, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return (
            not self.missing
            and self.accounted + len(self.skipped) == self.expected
            and self.expected > 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected": self.expected,
            "probed": self.accounted,
            "skipped": [{"name": o.name, "reason": o.reason} for o in self.skipped],
            "missing": list(self.missing),
            "complete": self.complete,
        }

    def render(self, verb: str = "geprueft") -> str:
        """Eine Zeile, die den Zaehler NIE ohne den Nenner nennt."""
        line = f"{self.accounted}/{self.expected} {verb}"
        if self.skipped:
            line += " — uebersprungen: " + ", ".join(
                f"{o.name} ({o.reason})" for o in self.skipped
            )
        if self.missing:
            line += " — OHNE ERGEBNIS: " + ", ".join(self.missing)
        return line

    def covered(self) -> str:
        """``n/44 abgedeckt`` — Ergebnisse UND begruendete Auslassungen zaehlen.

        Das ist die andere Haelfte von :meth:`render`: dort steht, was gemessen
        wurde, hier, was verantwortet ist. Beide Zahlen zusammen sagen, ob ein
        Lauf das Portfolio abgedeckt hat.
        """
        done = self.accounted + len(self.skipped)
        state = "vollstaendig" if self.complete else "UNVOLLSTAENDIG"
        return f"{done}/{self.expected} abgedeckt — {state}"


def build(
    expected: int,
    accounted: int,
    skipped: Sequence[Omission] = (),
    missing: Sequence[str] = (),
) -> Coverage:
    """Kleiner Konstruktor, damit die Aufrufer Listen uebergeben duerfen."""
    return Coverage(expected, accounted, tuple(skipped), tuple(missing))


# --------------------------------------------------------------------------
# CLI — damit auch `nightly-audit.sh` die Zielliste aus derselben Quelle liest
# --------------------------------------------------------------------------
#
# Ein Shell-Skript, das das Manifest mit `grep`/`jq` selbst zerlegt, ist eine
# zwoelfte Kopie derselben Regel — und die einzige ohne Test. Deshalb liest
# `nightly-audit.sh` die Liste ueber diesen Aufruf, in einem Format, das `read`
# ohne Werkzeug zerlegt.

_DEFAULT_FIELD = {SERVERS: "pypi_dist", REPOSITORIES: "repository"}
_DEFAULT_NULL_REASON = {SERVERS: "kein Paket auf dem Index", REPOSITORIES: None}


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="coverage",
        description="Die validierte Zielliste aus dem Deckungs-Manifest",
    )
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--section", default=SERVERS, choices=(SERVERS, REPOSITORIES))
    ap.add_argument("--field", default="", help="Vorgabe je Abschnitt")
    ap.add_argument(
        "--allow-skip",
        action="append",
        default=[],
        metavar="NAME:GRUND",
        help="begruendet auslassen; wiederholbar. Der Grund ist Pflicht",
    )
    ap.add_argument("--format", choices=("lines", "json"), default="lines")
    args = ap.parse_args(argv)

    field = args.field or _DEFAULT_FIELD[args.section]
    total, entries, omissions = read_manifest(
        args.manifest,
        field=field,
        section=args.section,
        null_reason=_DEFAULT_NULL_REASON[args.section],
        value_of=github_slug(args.manifest) if field == "repository" else None,
        omit_when=lambda raw: (
            "archiviert (read-only)"
            if args.section == REPOSITORIES and raw.get("archived")
            else None
        ),
    )

    allowed = parse_allow_skip(args.allow_skip)
    stray = unknown_skips(allowed, entries, omissions)
    if stray:
        raise ManifestError(
            "--allow-skip nennt Namen, die im Manifest nicht vorkommen: "
            + ", ".join(stray)
        )
    kept, by_flag = split_allowed(entries, allowed)
    skipped = [*omissions, *by_flag]

    if args.format == "json":
        json.dump(
            {
                "total": total,
                "targets": [{"id": e.id, "value": e.value} for e in kept],
                "skipped": [{"name": o.name, "reason": o.reason} for o in skipped],
            },
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0

    # Tab-getrennt, eine Zeile je Datensatz: `while IFS=$'\t' read -r kind a b`
    # zerlegt das ohne jq. TOTAL zuerst, damit der Leser den Nenner hat, bevor
    # die erste Zeile ihn in Versuchung fuehrt, selbst zu zaehlen.
    print(f"TOTAL\t{total}")
    for e in kept:
        print(f"TARGET\t{e.id}\t{e.value}")
    for o in skipped:
        print(f"SKIP\t{o.name}\t{o.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
