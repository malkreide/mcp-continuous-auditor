# Worker + Broker zusammen ausrollen

> Gilt für jede Änderung, die dem Evidence-File ein **Pflichtfeld** hinzufügt.
> Aktuell betroffen: `transport_boot` (#31), `host_allowlist` (#33),
> `tests_collected` (#34), `shipped_artifact` (#36), `lockfile`.
>
> **Nur für Tier 2** — den microVM-Split mit zwei getrennten Checkouts. Wer den
> Auditor als *einen* Checkout fährt (Tier 0/1, der Cron ruft
> `scripts/nightly-audit.sh` direkt), hat dieses Problem nicht: dort kommen
> beide Hälften aus demselben Baum. Der Update-Pfad dafür steht in
> [updating.md](updating.md) und ist zwei Befehle lang.

Der Auditor läuft an zwei Stellen mit **zwei getrennten Kopien desselben Repos**:

| Seite | Was dort läuft | Woher der Code kommt |
|---|---|---|
| **Worker** (microVM, credential-frei) | `scripts/nightly-audit.sh` — erzeugt `nightly-evidence.json` | `git clone` beim Boot, gepinnt auf `AUDITOR_EXPECTED_SHA` aus dem Cloud-Init-Seed |
| **Broker** (Host, hält die Credentials) | `scripts/nightly_audit_report.py --from-evidence` — leitet das Urteil neu ab | der Checkout des Hosts, aufgelöst von `broker-listener.sh` als `${REPO_ROOT}/scripts/nightly_audit_report.py` |

Beide müssen dieselbe Vorstellung davon haben, welche Gates es gibt. Wenn nicht,
gibt es **eine gefährliche und eine harmlose Richtung** — und sie sind nicht
symmetrisch.

## Warum der Broker zuerst muss

Gemessen mit dem echten Klassifizierer von vor PR #31 (`git show 161ec6b:scripts/nightly_audit_report.py`)
gegen Evidenz, die ein neuer Worker schickt — alte Gates grün, drei **neue** Gates
auf Exit 2:

```
$ python3 old_classifier.py --from-evidence ev_new.json --promptfoo-json pf.json …
exit=0
outcome: green | green: True
```

**Ein alter Broker meldet grün.** `_gate_from_evidence()` liest ausschliesslich
Namen aus seiner eigenen `_GATE_NAMES`-Liste; unbekannte Schlüssel im
Evidence-File werden schlicht nicht gelesen. Drei Gates mit Befunden
verschwinden spurlos.

Die Gegenrichtung:

```
NEW Broker + OLD Worker evidence -> exit 1, outcome 'hard-fail'
   reasons: transport boot gate could not run (exit 127) …
            DNS-rebinding gate could not run (exit 127) …
            shipped-artifact gate could not run (exit 127) …
```

**Ein neuer Broker mit alter Evidenz hard-failt laut.** Das ist das beabsichtigte
Fail-closed-Verhalten: ein Worker-Image, das die neuen Gates gar nicht gefahren
hat, darf nicht grün klassifizieren.

> **Reihenfolge: Broker zuerst, Worker danach.**
> Der schlimmste Fall bei dieser Reihenfolge ist ein hard-failender Zyklus, bis
> der Worker nachzieht. Bei der umgekehrten Reihenfolge ist der schlimmste Fall
> ein **stilles falsches Grün** — und das ist genau der Zustand, den dieser
> Auditor bekämpft.

## Die Schritte

### 0. Vorher: was ausgerollt wird festnageln

```bash
cd <broker-checkout>
git fetch origin main
git log --oneline HEAD..origin/main          # was kommt dazu
ROLLOUT_SHA="$(git rev-parse origin/main)"; echo "${ROLLOUT_SHA}"
```

Diesen einen SHA benutzen beide Seiten. Nicht „main" auf beiden — zwischen den
zwei Schritten kann `main` sich bewegen, und dann laufen sie doch auseinander.

### 1. Broker aktualisieren

```bash
cd <broker-checkout>
git checkout "${ROLLOUT_SHA}"
python3 -m unittest discover -s tests -p 'test_*.py'   # muss grün sein
```

Der Klassifizierer wird pro Lieferung neu ausgeführt (`socat … EXEC:_receive-one.sh`),
liest also die Datei bei jedem Empfang frisch. Ein Neustart des Listeners ist für
den Klassifizierer **nicht** nötig — wohl aber, wenn sich `broker-listener.sh`
oder `_receive-one.sh` selbst geändert haben, denn deren Pfade und die
`DROPBOX`/`REPORT_PY`-Umgebung werden beim Start gesetzt:

```bash
git diff --name-only HEAD@{1} HEAD -- deploy/microvm/channel/ | grep . && echo "-> Listener neu starten"
```

Neustart (bzw. `systemctl restart` der Unit):

```bash
pkill -f 'socat VSOCK-LISTEN' || true
bash deploy/microvm/channel/broker-listener.sh 9000 ./.audit/incoming
```

### 2. Prüfen, dass der Broker die neuen Felder wirklich verlangt

Ein Rollout ohne diese Kontrolle ist geraten. Alte Evidenz einspeisen und
sicherstellen, dass es **hard-failt**:

```bash
cd <broker-checkout>
cat > /tmp/ev-old.json <<'EOF'
{"target":"o/r","target_sha":"abc1234",
 "gates":{"ruff":0,"mypy":0,"pytest":0,"schema_drift":0,"promptfoo_rc":0}}
EOF
echo '{"results":{"stats":{"errors":0},"results":[]}}' > /tmp/pf.json
python3 scripts/nightly_audit_report.py --from-evidence /tmp/ev-old.json \
  --promptfoo-json /tmp/pf.json --out-report /tmp/r.md --out-summary /tmp/s.json
echo "exit=$?   # 1 erwartet"
```

Exit **1** und `outcome: hard-fail` — der Broker ist neu. Exit 0 heisst: der
Checkout ist noch alt, **nicht weitermachen**.

### 3. Worker-Seed auf denselben SHA neu bauen

Das „Worker-Image" ist ein **stock Debian-Cloud-Image plus Seed-ISO**; der
Auditor-Code wird beim Boot geklont. Ausgerollt wird also der **Pin im Seed**,
nicht ein neu gebackenes Image:

```bash
cd <broker-checkout>
TARGET_REPO=malkreide/zurich-opendata-mcp \
AUDITOR_REF="${ROLLOUT_SHA}" \
  bash deploy/microvm/build-worker-image.sh
```

Achte in der Ausgabe auf:

```
==> auditor pinned: <ROLLOUT_SHA> -> <ROLLOUT_SHA>
```

**Nicht akzeptieren:** `WARNING: could not resolve … — Worker will run UNVERIFIED`.
Dann steht `AUDITOR_EXPECTED_SHA=SKIP` im Seed, und der Worker fährt ungeprüften
Code — die Supply-Chain-Kontrolle ist damit aus. Ursache ist fast immer, dass der
SHA noch nicht auf dem Remote ist (`git push` vergessen).

### 4. Einen Zyklus fahren und die Naht ansehen

```bash
DROPBOX=./.audit/incoming TARGET_REPO=malkreide/zurich-opendata-mcp \
  bash deploy/microvm/run-audit-cycle.sh
```

In der Worker-Konsole muss stehen:

```
[worker] auditor SHA verified: <ROLLOUT_SHA>
```

Danach im frischesten Dropbox-Verzeichnis prüfen, dass die neuen Gates
tatsächlich **gelaufen** sind — nicht nur, dass der Lauf grün ist:

```bash
run="$(ls -1dt ./.audit/incoming/*/ | head -1)"; echo "${run}"
python3 - "$run" <<'PY'
import json, sys, pathlib
run = pathlib.Path(sys.argv[1])
ev = json.loads((run/"nightly-evidence.json").read_text())
need = ["transport_boot","host_allowlist","shipped_artifact","lockfile"]
missing = [k for k in need if k not in ev.get("gates",{})]
print("gates :", json.dumps(ev.get("gates",{}), sort_keys=True))
print("tests_collected:", ev.get("tests_collected","ABSENT"))
print("FEHLT:", missing or "nichts")
s = json.loads((run/"nightly-summary.json").read_text())
print("outcome:", s["outcome"], "| exit:", s["exit_code"])
PY
```

Ein `127` bei einem der neuen Gates heisst: der Worker fährt noch alten Code
oder das Gate konnte nicht starten — **kein** Grund, ihn abzuschalten, sondern
Anlass, in `.audit/logs/` nachzusehen.

### 5. Erst danach den Cron wieder scharf schalten

Solange Schritt 4 nicht sauber durchgelaufen ist, bleibt der Cron aus. Ein
hard-failender Zyklus ist erwartbar und harmlos; ein Cron, der nachts auf einen
halb ausgerollten Stand trifft, produziert nur Rauschen.

## Wenn die Reihenfolge doch verletzt wurde

Symptom **Worker neu, Broker alt**: alles grün, verdächtig ruhig. Zu erkennen an
einer Evidenz, die Gate-Namen enthält, die im `summary.json` unter `gates` **nicht**
auftauchen:

```bash
run="$(ls -1dt ./.audit/incoming/*/ | head -1)"
python3 -c '
import json,sys,pathlib
r=pathlib.Path(sys.argv[1])
ev=set(json.load(open(r/"nightly-evidence.json"))["gates"])
su=set(json.load(open(r/"nightly-summary.json"))["gates"])
ig={g for g in ev if not any(g in s for s in su)}
print("vom Broker IGNORIERTE Gates:", ig or "keine")
' "$run"
```

Ist die Menge nicht leer, war das Urteil dieses Laufs unvollständig. Broker
aktualisieren (Schritt 1–2) und den Zyklus **wiederholen** — das alte Grün nicht
als Ergebnis stehen lassen.

## Rückzug

Beide Seiten auf den vorherigen SHA zurücksetzen, in derselben Logik, nur
umgekehrt: **Worker zuerst** (Seed auf den alten SHA neu bauen), **Broker danach**.
Auch hier gilt die Asymmetrie — ein neuer Broker mit altem Worker ist laut, ein
alter Broker mit neuem Worker ist still.

## Warum das nicht automatisiert ist

Weil der gefährliche Zustand still ist. Ein Skript, das beide Seiten anfasst und
in der Mitte scheitert, hinterlässt genau die Kombination, die grün meldet, ohne
zu prüfen. Schritt 2 und Schritt 4 sind die Kontrollen, die ein solches Skript
sich selbst nicht abnehmen kann — sie messen den Zustand, statt ihn anzunehmen.
