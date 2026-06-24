# Bauplan v2 — Continuous Auditor mit deterministischer Wahrheitsinstanz

> Status: Exploring · Datum: 2026-06-24 · Ziel-Repo: `malkreide/zurich-opendata-mcp`

Weiterentwickelte Variante des «Dobby»-Blueprints. Kernidee: Die Wahrheitsinstanz
ist nicht ein Agent, sondern ein committetes, deterministisches Artefakt
(pytest + promptfoo in GitHub Actions). Der Agent schlägt nur vor und interpretiert;
ein Mensch ist immer das Merge-Gate.

## Zielarchitektur

| Schicht | Komponente | Rolle | Phase |
|---|---|---|---|
| Kontrolle | OpenClaw + Telegram | Befehle, Reports, Approval-Gate | 1 |
| Policy | SOUL.md / AGENTS.md / TOOLS.md | harte Invarianten (read-before-write, PR-only) | 1 |
| Skills | python-auditor, fastmcp-testing, promptfoo-eval | standardisierte Workflows | 1–2 |
| Wahrheitsinstanz | pytest + promptfoo + ruff/mypy in CI | deterministisches Bestanden/Durchgefallen | 2 |
| Sicherheit | Docker-Sandbox -> spaeter forkd | Blast-Radius, Privilege Separation | 1 / 5 |
| Proaktivitaet | OpenClaw Cron | taeglicher Read-only-Audit -> Telegram | 4 |
| Observability/Budget | TensorZero (optional) | Cost-Caps, Audit-Trail | 5 |

**Fluss:** Cron -> Orchestrator (Telegram) -> liest Repo read-only -> meldet Findings
-> *Mensch genehmigt* -> Worker oeffnet PR auf Branch -> GitHub Actions = Gate
-> *Mensch merged*. Niemals Push auf `main`.

---

## Phase 0 — Sicherheits-Baseline

**Ziel:** Repo so absichern, dass selbst ein fehlgeleiteter Agent keinen Schaden anrichtet.

**Schritte:** Branch Protection auf `main` (PRs + required CI), fine-grained PAT
(nur Ziel-Repo, contents+pull-requests, KEINE Secrets), CI gruen.

```text
PROMPT 0 — an Claude Code im Repo zurich-opendata-mcp:
Richte die Sicherheits-Baseline ein, ohne Anwendungscode zu aendern:
1. Lies pyproject.toml und .github/workflows/ — fasse den aktuellen CI-Stand zusammen.
2. Erstelle/aktualisiere .github/workflows/ci.yml so, dass auf jeden PR laufen:
   `uv run ruff check`, `uv run mypy`, `uv run pytest`. Kein Deploy, kein Push.
3. Schreibe docs/SECURITY-BASELINE.md: Branch-Protection-Regeln auf main,
   empfohlene fine-grained-PAT-Scopes (single repo, contents+pull-requests,
   NICHT secrets), Verbot von Force-Push.
Mach KEINE Aenderungen an src/. Zeig mir am Ende den Diff.
```

**Fertig, wenn:** CI auf einem Test-PR gruen ist und `main` ohne gruene CI nicht mergebar.

---

## Phase 1 — Read-only-Auditor (OpenClaw + Telegram)

**Ziel:** Agent darf lesen, analysieren, melden — sonst nichts. Vertrauen verdienen.

**Schritte:** Gateway aufsetzen, Telegram-Bot via @BotFather, ID-Gating, Sandbox an,
Policy-Dateien schreiben (siehe openclaw/ in diesem Repo).

```text
PROMPT 1a — Skill "python-auditor" bauen:
Erstelle in ~/.openclaw/workspace/skills/python-auditor/SKILL.md einen Skill mit
requires.bins [uv, ruff, mypy, pytest]. Bei jeder Analyse ruff+mypy+pytest ausfuehren,
bei Non-Zero-Exit die exakte Datei:Zeile aus stderr zitieren. In Phase 1 NUR berichten.

PROMPT 1b — erster Audit-Lauf (read-only):
Fuehre einen vollstaendigen Read-only-Audit auf zurich-opendata-mcp aus:
ruff + mypy + pytest. Liste die 24 Tools und 5 Resources auf und markiere je Tool die
Auditing-Prioritaet (SQL-Injection bei zurich_datastore_sql, Schema-Validierung bei
GeoJSON-Tools). Schreib einen knappen Markdown-Report nach docs/audits/<datum>.md.
Aendere KEINEN Code.
```

**Fertig, wenn:** «audit» auf Telegram liefert einen zitatgenauen Report, ohne dass
eine Datei in `src/` veraendert wurde.

---

## Phase 2 — Deterministische Wahrheitsinstanz mit promptfoo (Herzstueck)

**Ziel:** Verifikation wird ein versioniertes Artefakt in CI — kein Agent-Urteil.

Zwei Ebenen: **pytest** prueft Tools direkt (Unit/Contract, aufgezeichnete Fixtures
-> erkennt Schema-Drift deterministisch). **promptfoo** prueft Tool-Outputs gegen das
generierte JSON-Schema, scannt auf Injection (OWASP) und bewertet End-to-End mit einem
**unabhaengigen Grader-Modell** anderer Familie. Schreiber != Pruefer.

Artefakte in diesem Repo: promptfoo/promptfooconfig.yaml, promptfoo/providers/call_tool.py,
.github/workflows/ci.yml.

```text
PROMPT 2a — Schema als Drift-Detektor:
Generiere aus den FastMCP-Type-Hints die JSON-Schemas der Tool-Outputs nach schemas/.
Schreibe providers/call_tool.py: ruft ein Tool ueber den FastMCP-In-Memory-Client auf
(httpx via AsyncMock gegen Fixtures, KEIN Live-Netz) und gibt das rohe JSON zurueck.

PROMPT 2b — Contract-Tests + Red-Team:
Erstelle promptfooconfig.yaml mit (1) is-json gegen die Schemas fuer zurich_datastore_sql
und zwei GeoJSON-Tools, (2) Injection-Negativtests, (3) redteam-Block (pii, prompt-injection,
sql-injection). Verdrahte den promptfoo-Job in ci.yml als REQUIRED check.

PROMPT 2c — Live-Probe-Drift-Job:
Erstelle einen separaten GitHub-Actions-Cron-Job, der woechentlich die ECHTEN Zuercher
Endpunkte einmal abfragt, gegen die Fixtures difft und bei Abweichung ein Issue oeffnet.
```

**Fertig, wenn:** Ein PR mit absichtlich gebrochenem Schema von der CI rot gestoppt wird.

---

## Phase 3 — Schreibrechte mit Mensch-Merge-Gate

**Ziel:** Worker darf patchen — nur via PR, CI aus Phase 2 ist das Tor.

```text
PROMPT 3 — Worker mit TDD-Invariante:
Erweitere AGENTS.md: Worker darf src/ aendern, aber (a) nur auf Branch fix/<slug> + PR,
nie auf main; (b) TDD: kein neues Tool/Resource ohne zuvor lokal gruen gelaufenen
async-Test; (c) nach jedem Edit ruff + mypy, Fehler selbst beheben. Demonstriere es an
EINEM kleinen Issue: erst fehlschlagender Test, dann Fix, dann PR. Ich merge.
```

**Fertig, wenn:** Agent oeffnet gruenen PR, du reviewst, du merderst. Push auf main scheitert.

---

## Phase 4 — Proaktivitaet (Cron) + adversariale Ebene richtig

**Ziel:** Taeglicher Read-only-Sweep; «adversarial» kommt aus dem promptfoo-Red-Team
(deterministisch), nicht aus einem zweiten Chat-Agenten.

```text
PROMPT 4 — Cron-Audit:
Lege einen OpenClaw-Cron-Job an (taeglich 03:00), der upstream pullt, ruff/mypy/pytest +
promptfoo eval laeuft, bei Schema-Drift oder Red-Team-Treffern ein Issue + (nur nach
meinem Telegram-OK) einen PR-Entwurf erzeugt, und einen knappen Report mit --announce nach
Telegram pusht. Bei nicht aufloesbarem Modell: hart fehlschlagen, nicht still ausweichen.
```

**Fertig, wenn:** Morgens ungefragt ein Audit-Report auf Telegram, Findings als Issues.

---

## Phase 5 — Haertung & Skalierung (optional)

- **forkd** statt Docker: microVM/KVM-Isolation, getrennte VMs fuer Untrusted-Reader vs.
  Credential-Halter (v0.4/0.5, Linux-only — erst wenn stabil).
- **TensorZero** zwischen OpenClaw und Provider: Cost-Caps pro Cron-Lauf, A/B, Audit-Trail.
- Budget-Leitplanken: Max-Iterationen, Token-Ceiling, Circuit Breaker.

---

## Goldene Regeln

1. Mensch ist immer das Merge-Gate.
2. CI ist die Wahrheit, nie ein Agent.
3. Untrusted-Daten strikt von Tool-Aufrufen trennen.
4. Jede Phase erst abschliessen, wenn das «Fertig-wenn»-Kriterium beobachtet wurde.

## Tool-Referenzen (aus AI-Tools-DB)

- **promptfoo** — deterministische Asserts + OWASP-Red-Teaming (Wahrheitsinstanz).
- **forkd** — microVM-Sandbox fuer Privilege Separation.
- **TensorZero** — LLM-Gateway mit Observability + Budget-Kontrolle.
