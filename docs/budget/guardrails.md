# Budget-Leitplanken (Phase 5)

> Status: implementiert · Code: [`scripts/budget_guard.py`](../../scripts/budget_guard.py)
> · Tests: [`tests/test_budget_guard.py`](../../tests/test_budget_guard.py)

Der nächtliche Cron-Audit (Plan Phase 4) ruft bei **jedem** Lauf ein Cloud-Modell
auf. Ohne Grenzen kosten zwei Fehlermodi unkontrolliert Geld bzw. hämmern auf ein
kaputtes Ziel ein:

1. ein **durchdrehender Agent** (Token-Explosion in *einem* Lauf), und
2. eine **verklemmte Umgebung** (unauflösbares Modell, kaputtes Gate), die jede
   Nacht rot fehlschlägt und dabei weiter Tokens verbrennt.

Phase 5 legt drei deterministische Leitplanken um den Lauf. Sie werden vom
Wrapper-Script erzwungen, **nicht** der Beurteilung des Agenten überlassen.

| Leitplanke | Wo erzwungen | Knopf |
|---|---|---|
| **Token-Ceiling** | `budget_guard.py` (pro Lauf + rollierendes Fenster) | `BUDGET_TOKENS_PER_RUN`, `BUDGET_TOKENS_WINDOW`, `BUDGET_WINDOW_SECONDS` |
| **Circuit Breaker** | `budget_guard.py` (Preflight-Gate) | `BUDGET_BREAKER_THRESHOLD`, `BUDGET_BREAKER_COOLDOWN_SECONDS` |
| **Max-Iterationen** | OpenClaw-Payload + TensorZero-Gateway (upstream) | `BUDGET_MAX_ITERATIONS` (Wert wird durchgereicht) |

## Wie es in den Audit eingehängt ist

`scripts/nightly-audit.sh` ruft den Guard an zwei Stellen:

```text
0) preflight   vor dem Provisioning:
   - Breaker zu / half-open?  -> exit 0  -> Audit läuft.
   - Breaker offen (Cooldown) -> exit 75 -> Audit wird ÜBERSPRUNGEN.
     budget_guard schreibt dann einen hard-fail-förmigen Report + summary.json,
     das Script gibt exit 1 zurück. Ein übersprungener Audit wird damit GENAU
     wie jedes andere "nicht bestanden" geroutet — nie als grün gemeldet.

6) record      nach dem Report:
   - bekommt den Outcome (via exit-code) + die gemessenen Tokens (aus der
     promptfoo-JSON) und aktualisiert den Breaker für den NÄCHSTEN Lauf.
   - läuft ohne --strict: ein Budget-Verstoss kippt den Breaker für *morgen*,
     schreibt aber nie das grüne/Findings-Urteil von *heute* um.
```

Der Exit-Code-Vertrag des Audits bleibt unverändert (`0` grün / `2` Findings /
`1` hard-fail). Der Breaker-Skip wird bewusst auf `1` abgebildet, weil ein nicht
gelaufener Audit sicher **kein** Bestehen ist (SOUL.md: nie „bestanden" ohne
gesehenen grünen Exit-Code).

## Circuit Breaker — Zustände

```
        N aufeinanderfolgende hard-fails  ODER  Budget-Verstoss
closed ───────────────────────────────────────────────────────▶ open
  ▲                                                               │
  │ grüner/Findings-Lauf (Erfolg)                  Cooldown abgelaufen
  │                                                               ▼
  └──────────────────── half_open ◀──────────────────────────────┘
            Erfolg  ▲        │  erneuter hard-fail
                    └────────┘ (zurück zu open)
```

- **closed** — Normalbetrieb. Jeder hard-fail erhöht den Zähler; ein grüner oder
  Findings-Lauf setzt ihn auf 0.
- **open** — Läufe werden übersprungen, bis der Cooldown abgelaufen ist. Schützt
  das Budget vor einer verklemmten Umgebung.
- **half_open** — nach dem Cooldown wird **ein** Probelauf erlaubt. Erfolg →
  `closed`; erneuter hard-fail → zurück zu `open` (neuer Cooldown).

Manuell schliessen (z. B. nachdem du das auslösende Problem behoben hast):

```bash
python3 scripts/budget_guard.py reset --reason "model quota restored"
# optional auch das Token-Fenster zurücksetzen:
python3 scripts/budget_guard.py reset --reason "new billing cycle" --clear-window
```

## Token-Ceiling

Zwei Grenzen, beide verstossbar → Breaker kippt:

- **pro Lauf** (`BUDGET_TOKENS_PER_RUN`, Default 200 000): fängt einen einzelnen
  durchdrehenden Lauf ab — selbst wenn er sonst grün wäre.
- **rollierendes Fenster** (`BUDGET_TOKENS_WINDOW` über `BUDGET_WINDOW_SECONDS`,
  Default 2 000 000 / 24 h): fängt das langsame Auflaufen vieler Läufe ab. Das
  Fenster rollt automatisch weiter, sobald es abgelaufen ist.

Die Tokens kommen aus der promptfoo-`--output`-JSON (`stats.tokenUsage.total`).
Wo ein vollständiger Cost-Trail über **alle** Modellaufrufe (Agent + Grader)
gewünscht ist, liefert ihn TensorZero — siehe
[../observability/tensorzero.md](../observability/tensorzero.md); dessen
gemessene Tokens lassen sich identisch per `--tokens` an `record` übergeben.

## Max-Iterationen

Eine Obergrenze auf die Denk-/Tool-Schleife des Agenten gehört dorthin, wo die
Schleife **läuft** — nicht in dieses Script. `budget_guard.py` validiert nur,
dass der Knopf gesetzt ist, und gibt den effektiven Wert aus
(`BUDGET_MAX_ITERATIONS=…`), damit der Wrapper ihn nach oben durchreichen kann.
Erzwungen wird er an zwei Stellen:

- **OpenClaw-Payload** — Wall-Clock-Schranke über `timeoutSeconds` (bereits in
  `openclaw/cron/nightly-audit.json` gesetzt) plus, falls deine OpenClaw-Version
  es unterstützt, ein expliziter Schritt-/Iterations-Cap.
- **TensorZero-Gateway** — harter Cap auf Variant-Ebene + Cost-Cap pro
  Inferenz-Episode (siehe TensorZero-Doku). Das ist die zuverlässigste Schranke,
  weil sie **provider-seitig** greift, bevor Tokens entstehen.

## Konfiguration

Alle Knöpfe sind Env-Variablen mit sicheren Defaults (in `.env.example`
dokumentiert):

| Variable | Default | Bedeutung |
|---|---|---|
| `BUDGET_GUARD` | `1` | `0`/`off` deaktiviert die Leitplanken (z. B. Erstlauf) |
| `BUDGET_TOKENS_PER_RUN` | `200000` | Token-Obergrenze für einen einzelnen Lauf |
| `BUDGET_TOKENS_WINDOW` | `2000000` | Token-Obergrenze über das rollierende Fenster |
| `BUDGET_WINDOW_SECONDS` | `86400` | Länge des rollierenden Fensters (Sekunden) |
| `BUDGET_BREAKER_THRESHOLD` | `3` | hard-fails in Folge bis der Breaker öffnet |
| `BUDGET_BREAKER_COOLDOWN_SECONDS` | `21600` | Cooldown (Sekunden) bevor ein Probelauf erlaubt wird |
| `BUDGET_MAX_ITERATIONS` | `25` | Agent-Iterations-Cap (upstream durchgereicht) |
| `BUDGET_STATE` | `.audit/budget-state.json` | Pfad zur State-Datei (gitignored) |

## Status & Betrieb

```bash
# effektive Limits + aktueller Zustand (Breaker, Token-Fenster, letzte Läufe):
python3 scripts/budget_guard.py status

# Tests (stdlib-only, kein uv/pytest nötig):
python3 -m unittest tests.test_budget_guard -v
```

Der State liegt unter `.audit/budget-state.json` (Teil des bereits gitignorierten
`.audit/`-Arbeitsverzeichnisses) und wird atomar geschrieben — eine korrupte
State-Datei startet sauber neu mit **geschlossenem** Breaker, damit ein FS-Glitch
den Audit nie dauerhaft blockiert.

## Goldene Regeln (unverändert)

- Mensch ist das Merge-Gate; CI ist die Wahrheit. Die Leitplanken **stoppen**
  Läufe, sie fällen keine Audit-Urteile.
- Ein übersprungener Lauf ist nie ein Bestehen.
- Alle externen Daten (promptfoo-JSON, Env) sind untrusted: nur als Ints gelesen,
  nie in eine Shell interpoliert (AGENTS.md / TOOLS.md).
