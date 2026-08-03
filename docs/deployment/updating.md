# Den Auditor aktualisieren — und was «Rollout» je nach Tier bedeutet

> Kurzform: **`main` zu aktualisieren ändert am laufenden System nichts.** Der
> Nachtlauf fährt aus einem *Checkout*, nicht aus `main`. Solange der nicht
> nachgezogen wird, läuft weiter der alte Code — ein neu gebautes Gate schweigt
> dann nicht, weil es nichts findet, sondern weil es gar nicht existiert.

Diese Seite beantwortet zwei Fragen, die bisher zwischen den Deployment-Docs
lagen:

1. Ich habe **noch gar nichts deployed** — was brauche ich überhaupt?
2. Ich habe deployed — was heisst «Rollout», wenn eine Änderung ein neues Gate
   mitbringt?

## Noch nichts deployed?

Dann ist [tier-0.md](tier-0.md) der Einstieg, nicht diese Seite. Der Auditor
läuft ab Tier 0 vollständig: ein Checkout, die deterministischen Gates, der
OpenClaw-Cron. Kein microVM, kein KVM, kein TensorZero — das sind Härtungsstufen,
keine Voraussetzungen.

Für den ersten Lauf brauchst du **kein** Deployment: `bash scripts/nightly-audit.sh`
läuft auch auf einer Wegwerf-Maschine gegen ein `TARGET_REPO` und schreibt Report
und Summary lokal. Erst der *proaktive* Teil — der 03:00-Cron, der nach Telegram
meldet und Issues anlegt — braucht einen Host, der dauerhaft läuft und die
Credentials hält.

## Welche Form läuft bei mir?

| Woran erkennbar | Form | Update-Pfad |
|---|---|---|
| Der Cron ruft `bash scripts/nightly-audit.sh` | **Tier 0/1 — ein Checkout** | unten, zwei Befehle |
| Der Cron ruft `deploy/microvm/run-audit-cycle.sh`; es gibt ein Seed-ISO und ein `.audit/incoming/` | **Tier 2 — Broker/Worker-Split** | [worker-broker-rollout.md](worker-broker-rollout.md) |

Der versionierte Cron-Spec (`openclaw/cron/nightly-audit.json`) beschreibt die
erste Form. Wer den Split fährt, weiss es, weil er ihn nach
[phase5-rollout.md](phase5-rollout.md) aufgesetzt hat.

## Tier 0/1 — ein Checkout

```bash
cd <auditor-checkout>
git fetch origin main
git log --oneline HEAD..origin/main            # was kommt dazu
git checkout "$(git rev-parse origin/main)"    # oder ein bewusst gewählter SHA
python3 -m unittest discover -s tests -p 'test_*.py'
```

Läuft die Suite grün, ist der Rollout fertig. Der Cron-Job liest das Skript bei
jedem Lauf frisch; ein Neustart ist nur nötig, wenn sich der Cron-*Spec* selbst
geändert hat (dann `openclaw/cron/install.sh` erneut).

**Warum hier keine Reihenfolge zu beachten ist:** `nightly-audit.sh` ruft den
Klassifizierer als `"${HERE}/nightly_audit_report.py"` auf — also aus seinem
eigenen Verzeichnis. Beide Hälften kommen aus demselben Baum und können gar nicht
auseinanderlaufen. Genau diese Eigenschaft fehlt dem Split, und daraus entsteht
dort die ganze Sorgfalt.

## Tier 2 — Broker/Worker-Split

Hier laufen **zwei getrennte Kopien** desselben Repos: der Worker (microVM,
credential-frei) erzeugt `nightly-evidence.json`, der Broker (Host mit den
Credentials) leitet das Urteil daraus neu ab und löst den Klassifizierer über
seinen *eigenen* `REPO_ROOT` auf. Die beiden können unterschiedliche Vorstellungen
davon haben, welche Gates es gibt — und die zwei Richtungen sind nicht
gleich schlimm:

| Zustand | Verhalten | Bewertung |
|---|---|---|
| Broker neu, Worker alt | lautes `hard-fail`, bis der Worker nachzieht | harmlos, sichtbar |
| **Worker neu, Broker alt** | **`green` — der Befund verschwindet spurlos** | genau der Fehlermodus, den dieser Auditor bekämpft |

Gemessen für das zuletzt hinzugekommene Feld, mit dem Klassifizierer von vor dem
Lockfile-Gate gegen Evidenz, die `lockfile: 2` (also einen echten `LOCK_DRIFT`)
trägt:

```
exit=0 | outcome: green | green: True
Gates, die der alte Broker gelesen hat: [ruff, mypy, pytest, schema_drift,
  promptfoo_rc, transport_boot, host_allowlist, shipped_artifact]
-> lockfile im Urteil enthalten? False
```

`_gate_from_evidence()` liest ausschliesslich Namen aus seiner eigenen
`_GATE_NAMES`-Liste; unbekannte Schlüssel im Evidence-File werden nicht gelesen.
Deshalb: **Broker zuerst, Worker danach** — der vollständige Ablauf mit allen
Kontrollen steht in [worker-broker-rollout.md](worker-broker-rollout.md).

## Warum ein neues Gate überhaupt ein Rollout ist

Die Gate-Liste im Klassifizierer ist **fail-closed**: ein Name in `_GATE_NAMES`,
den ein Evidence-File nicht trägt, liest als `127` und lässt den Lauf hart
fehlschlagen. Das ist für ein Worker-Image, das das Gate wirklich nicht gefahren
hat, die richtige Antwort — und es ist der Grund, warum das Hinzufügen eines
Gates ein Betriebsvorgang ist und keine reine Code-Änderung.

Ein `hard-fail` direkt nach einem Rollout ist deshalb erwartbar und kein Grund,
etwas abzuschalten. Ein *grüner* Lauf, in dem ein neues Gate fehlt, ist das
Problem.

## Was ein Rollout nicht mitbringt

Der Nachtlauf prüft **ein** `TARGET_REPO`. Portfolioweite Aussagen — «19 von 20
Servern committen kein Lockfile» — kommen aus `scripts/portfolio_scan.py` bzw.
aus einem manuellen Sweep über die Checkouts, nicht aus dem Cron. Ein grüner
Nachtlauf sagt etwas über einen Server, nicht über das Portfolio.
