# Bauplan Phase 6 — Nightly-Improve-Loop (Keep/Discard nach autoresearch-Muster)

> Status: Exploring · Datum: 2026-07-13 · Baut auf: [Bauplan v2](2026-06-24-continuous-auditor-v2.md), Phasen 0–5

Phasen 0–5 machen den Auditor **beobachtend**: er misst das Target jede Nacht gegen
eine feste, deterministische Suite. Phase 6 macht ihn zusätzlich **selbstverbessernd** —
aber nur an genau einer Stelle: an der Audit-Suite selbst, nie am Target-Code.

Das Muster stammt aus [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
(MIT): ein Agent editiert dort über Nacht eine einzige Datei (`train.py`), jedes
Experiment läuft mit festem Budget, und eine **objektive, nicht-LLM-Metrik** (val_bpb)
entscheidet allein über keep/discard. Genau dieses Prinzip — Metrik statt Agenten-Urteil —
ist bereits die Goldene Regel 2 dieses Projekts; Phase 6 wendet es auf einen
Verbesserungs-Loop an.

## Übersetzung des Musters

| autoresearch | dieser Auditor (Phase 6) |
|---|---|
| objektive Metrik `val_bpb` | deterministische Annahme-Regel D1–D3 (unten) |
| eine editierbare Datei (`train.py`) | eine editierbare Fläche: `promptfoo/` determ-Suite des Targets |
| festes 5-Minuten-Budget pro Experiment | Token-/Zeit-Ceiling pro Iteration (`budget_guard.py`) |
| keep bei besserer Metrik, sonst discard | keep nur wenn D1 ∧ D2 ∧ D3, sonst revert + Journalgrund |
| Journal aller Läufe | append-only `experiments.jsonl` + Morgenreport |
| `program.md` (vom Menschen editierte Instruktion) | `openclaw/workspace/IMPROVE.md` (Policy der Schleife) |

**Bewusst NICHT übernommen:** autonomes Editieren von Anwendungscode (der Loop fasst
`src/` des Targets nie an), Auto-Keep als Endzustand (behaltene Kandidaten landen in
einem PR — Goldene Regel 1 bleibt), und alles GPU-/Trainingsspezifische.

## Zielbild / Fluss

```text
Cron (wöchentlich, So 03:00 — getrennt vom täglichen Nightly-Audit)
  └─> improve-loop.sh: Provision des Targets (read-only, wie nightly-audit.sh §1)
      └─> für i in 1..N (N budgetiert):
          1. Writer (Anthropic) schlägt EINEN Kandidaten vor:
             ein neues deterministisches Assert / eine Injection-Probe —
             ausschliesslich Dateien unter promptfoo/ (determ-Profil).
          2. Annahme-Harness prüft rein deterministisch (kein LLM-Urteil):
             D1 Reproduzierbarkeit — Kandidat läuft 2× mit identischem Ergebnis
                (Flake-Gate; ein flakiges Assert wäre vergiftete Wahrheitsinstanz).
             D2 Kein False-Positive — Kandidat ist GRÜN auf aktuellem Target-HEAD.
             D3 Mehrwert — Kandidat tötet ≥1 Mutante, die kein bestehendes Assert
                tötet (Mutation-Score steigt), oder deckt einen bislang
                ungeprüften Schema-Pfad aus schemas/.
          3. keep  -> Commit auf Branch improve/<datum>
             discard -> Revert; Grund (flaky | false-positive | redundant) ins Journal
          4. Journalzeile nach experiments.jsonl (append-only).
      └─> danach: EIN Draft-PR (improve/<datum>) mit allen Keeps + Journal
          + Telegram-Report. Mensch reviewt und merged.
```

Beispiel-Morgenmeldung: *«7 Kandidaten, 3 behalten, 4 verworfen (2× flaky,
1× false-positive, 1× redundant). Mutation-Score 61 % → 68 %. PR #42.»*

## Invarianten (unverändert aus v2 — der Loop verschärft sie sogar)

1. **Mensch ist Merge-Gate.** Der Loop merged nie; er sammelt auf `improve/<datum>`
   und öffnet einen Draft-PR. Keep/Discard entscheidet nur, was *im PR landet*.
2. **CI ist die Wahrheit.** Die Annahme-Regel D1–D3 ist committeter Code, kein
   LLM-Rubric. Der Writer schlägt vor; die Regel entscheidet.
3. **Writer ≠ Prüfer — hier sogar: gar kein LLM-Prüfer.** Innerhalb des Loops läuft
   ausschliesslich das key-lose determ-Profil; das graded-Profil (llm-rubric) bleibt
   ausserhalb. Damit kann der Loop auf dem credential-freien Worker laufen.
4. **Read-only gegen das Target.** Editiert wird nur die Audit-Suite (`promptfoo/`),
   nie `src/` des Targets — dasselbe Contract wie `audit-target.sh`.
5. **Hard-fail-Disziplin.** Infrastrukturfehler (promptfoo nicht startbar, Netz weg,
   Target-Suite selbst flaky) bricht die Schleife mit exit 1 ab und wird nie als
   «discard» gezählt — ein abgebrochener Lauf ist kein Ergebnis.
6. **Budget.** `budget_guard.py` bekommt zusätzlich ein per-Iteration-Ceiling;
   der Circuit-Breaker gilt auch hier, aber mit **eigenem** `BUDGET_STATE`
   (`.audit/improve-budget-state.json`), damit ein teurer Improve-Lauf nicht den
   täglichen Audit sperrt (und umgekehrt).

## Bausteine und Integrationspunkte

- **`scripts/improve-loop.sh`** — Orchestrierung der Schleife. Wiederverwendet die
  Bausteine aus `nightly-audit.sh`: Provision (§1, read-only), promptfoo-Pinning via
  `install-promptfoo.sh`, `budget_guard.py preflight/record`, Hard-Fail-Helper.
- **`scripts/improve_acceptance.py`** — implementiert D1–D3 und schreibt
  `experiments.jsonl`; stdlib-only und unit-testbar wie `budget_guard.py`
  (→ `tests/test_improve_acceptance.py` mit Fixtures für keep/flaky/false-positive/redundant).
- **Mehrwert-Metrik (D3):** [mutmut](https://github.com/boxed/mutmut) oder cosmic-ray
  gegen das Target als Mutation-Score. Pragmatischer Einstieg, falls Mutation-Testing
  auf dem Pi 5 zu teuer ist: «Kandidat referenziert einen Schema-Pfad aus `schemas/`,
  den kein bestehendes Assert referenziert» — schwächer, aber deterministisch und billig;
  Upgrade auf Mutation-Score, sobald ein Lauf < 30 min bleibt.
- **`openclaw/workspace/IMPROVE.md`** — das `program.md`-Äquivalent: definiert die
  einzige editierbare Fläche, das Kandidaten-Format (ein Assert pro Iteration, YAML-Diff),
  verbotene Muster (kein `llm-rubric`, kein Netzzugriff in Providern, keine Änderung
  bestehender Asserts) und Abbruchkriterien.
- **`openclaw/cron/improve-loop.json`** — zweiter Cron-Spec, wöchentlich statt täglich
  (Kostenkontrolle; der Nightly-Audit bleibt unangetastet), Report via `--announce`.
- **Report:** `improve-summary.json` + Markdown-Journal im PR-Body; die
  Findings→Issues-Routing-Logik (`sync_findings_issues.py`) wird NICHT benutzt —
  der Loop produziert PRs, keine Issues.

## Teilphasen

### 6a — Journal + Annahme-Harness (D1, D2)

Manuell angestossen, ohne Cron, ohne Writer-Automatik: die Regel zuerst, der Agent danach.

> **Status: implementiert.** `scripts/improve_acceptance.py` (stdlib-only;
> Subcommands `baseline` + `judge`, Exit-Contract 0 keep / 2 discard / 1 hard-fail,
> Runner injizierbar via `--runner`/`IMPROVE_RUNNER`) +
> `tests/test_improve_acceptance.py`. Zusätzlich zu D1/D2 werden bereits die
> Kandidaten-Discards `invalid` und `out-of-scope` (Pfad-Check „nur `promptfoo/`“)
> im Harness erzwungen; Baseline wird pro Target-SHA gecacht.

```text
PROMPT 6a — an Claude Code in mcp-continuous-auditor:
Baue scripts/improve_acceptance.py (stdlib-only): nimmt einen Kandidaten-Diff gegen
promptfoo/promptfooconfig.determ.yaml, führt die determ-Suite 2× aus (D1: identisches
Ergebnis beider Läufe, sonst discard "flaky"), prüft D2 (Kandidat grün auf Target-HEAD,
sonst discard "false-positive") und schreibt eine Journalzeile nach
.audit/experiments.jsonl (append-only: ts, kandidat-sha, verdict, grund, dauer).
Dazu tests/test_improve_acceptance.py mit Fixtures: ein gültiger Kandidat wird behalten,
ein absichtlich flakiger (random-abhängig) und ein auf HEAD roter werden verworfen.
Kein Cron, kein LLM-Call — nur Harness + Tests.
```

**Fertig, wenn:** die drei Fixture-Fälle (keep / flaky / false-positive) im Test
deterministisch das erwartete Verdict bekommen.

### 6b — Mehrwert-Metrik (D3)

```text
PROMPT 6b:
Erweitere improve_acceptance.py um D3: baseline-Mutation-Score des Targets ermitteln
(mutmut, gecacht pro Target-SHA in .audit/), dann Kandidat nur behalten, wenn er
mindestens eine Mutante tötet, die die bestehende Suite überlebt. Fallback-Modus
D3-lite (--coverage-mode schema-path) für Hosts ohne Mutation-Budget. Fixture-Test:
ein Duplikat eines bestehenden Asserts wird als "redundant" verworfen.
```

**Fertig, wenn:** ein redundantes Assert (semantisches Duplikat) verworfen wird und
der Journalgrund `redundant` lautet.

> **Status: implementiert.** `improve_acceptance.py` hat jetzt
> `--coverage-mode mutation|schema-path|off` (Default: `schema-path`, via
> `IMPROVE_COVERAGE_MODE` übersteuerbar). Der Mutation-Modus ist Tool-agnostisch:
> `--mutants-dir` nimmt einen Pool von Mutanten-*Diffs* (von mutmut, cosmic-ray
> oder jedem Generator erzeugt — der Harness wendet nur Patches an), Kill-Status
> der bestehenden Suite wird pro Target-SHA gecacht, behalten wird nur ein
> Kandidat, der ≥1 überlebende Mutante tötet. Leerer oder vollständig getöteter
> Pool → Hard-Fail (fail-closed statt Alles-redundant/Alles-keep). D3-lite
> vergleicht `schemas/*.json`-Referenzen der Kandidaten-Diffzeilen gegen alle
> bestehenden Dateien unter `promptfoo/`. Reihenfolge bleibt D1 → D2 → D3
> (ein flakiges Duplikat journaliert `flaky`, nicht `redundant`).

### 6c — Writer-Loop + Cron + PR

```text
PROMPT 6c:
Baue scripts/improve-loop.sh nach dem Muster von nightly-audit.sh (set -uo pipefail,
hard_fail-Helper, budget_guard mit eigenem BUDGET_STATE und per-Iteration-Ceiling):
N Iterationen (default 10), pro Iteration ein Writer-Vorschlag (Anthropic) strikt
innerhalb der IMPROVE.md-Policy, dann improve_acceptance.py als Richter, keeps auf
Branch improve/<datum> committen. Am Ende: Draft-PR mit Journal im Body + Telegram-
Announce. Dazu openclaw/cron/improve-loop.json (wöchentlich So 03:00) und
openclaw/workspace/IMPROVE.md. Der Loop darf ausschliesslich unter promptfoo/
schreiben — erzwungen im Harness (Pfad-Check vor jedem Commit), nicht nur per Policy.
```

**Fertig, wenn:** Montagmorgen ungefragt ein Draft-PR mit ≥1 behaltenen Assert und
vollständigem Journal vorliegt, kein Commit ausserhalb `promptfoo/` existiert und
der Nightly-Audit der Nacht davor unbeeinflusst gelaufen ist.

## Kostenrahmen

Pro Iteration: 1 Writer-Call + 2–3 determ-Evals. Das determ-Profil ist key-los —
die Modellkosten sind also fast ausschliesslich die N Writer-Calls (bei N=10 und
kleinen Diffs im niedrigen Cent-/Rappen-Bereich pro Lauf). Mutation-Testing kostet
CPU-Zeit, keine Tokens; auf dem Pi 5 ist deshalb D3-lite der Default. Ceilings über
die bestehenden `BUDGET_*`-Variablen, plus neu `IMPROVE_MAX_ITER` und
`IMPROVE_ITER_TOKEN_CEILING`.

## Risiken

- **Goodhart / Metrik-Gaming:** Der Writer könnte triviale Mutanten-Killer erzeugen,
  die den Score heben, ohne echte Prüfschärfe. Gegenmittel: D3 verlangt Kills, die die
  *bestehende* Suite nicht schafft, und der Mensch sieht im PR jeden Kandidaten samt
  Journal — der Loop filtert nur vor, er beweist keinen Wert.
- **Suiten-Aufblähung:** Jedes Keep verlängert die Nightly-Laufzeit. Gegenmittel:
  Obergrenze Keeps/Lauf (z. B. 5) und optional D4: Gesamtlaufzeit der determ-Suite
  darf pro Lauf um max. x % wachsen.
- **Flakiges Target:** D1 misst den Kandidaten, setzt aber ein stabiles Target voraus.
  Wenn die *bestehende* Suite auf HEAD nicht 2× identisch läuft, bricht der Lauf hart
  ab (Invariante 5) — das ist dann ein Nightly-Finding, kein Improve-Problem.
- **Scope-Creep des Writers:** Policy allein genügt nicht (v2, Phase 3 lässt grüssen) —
  der Pfad-Check `nur promptfoo/` ist deshalb im Harness erzwungen, nicht nur in
  IMPROVE.md dokumentiert.
