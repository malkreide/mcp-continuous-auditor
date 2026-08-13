# MCP Continuous Auditor

![Version](https://img.shields.io/badge/version-0.3.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11+-blue)
![Node](https://img.shields.io/badge/node-20+-green)

> Ein persistenter Multi-Agenten-Auditor, der MCP-Server kontinuierlich testet und härtet — mit promptfoo + CI als deterministische Wahrheitsinstanz, nie mit der Meinung eines LLM.

[🇬🇧 English Version](README.md)

## Übersicht

Dieses Projekt betreibt einen kontinuierlichen Auditor für [MCP](https://modelcontextprotocol.io)-Server, beginnend mit [`zurich-opendata-mcp`](https://github.com/malkreide/zurich-opendata-mcp). Ein [OpenClaw](https://docs.openclaw.ai)-Gateway stellt den Auditor auf Telegram als Kontrollebene bereit. Anders als ein «Vibecoding»-Agent ist die Verifikation ein **versioniertes Artefakt** (pytest + promptfoo in GitHub Actions), und ein Mensch ist immer das Merge-Gate.

## Funktionen

- **Read-only zuerst** — der Agent berichtet, bevor er je schreibt.
- **Deterministische Wahrheitsinstanz** — promptfoo-YAML-Asserts + JSON-Schema-Drift-Checks in der CI.
- **Recall-Untergrenzen, nicht nur Schema** — eine eingebrochene Ergebnismenge behält ihre JSON-Form, ein struktureller Diff sieht sie also nicht. Probes tragen `min_count`-Untergrenzen, und eine zweite wöchentliche Probe ruft die *eigenen Tools* des Servers live auf: die Raw-URL-Probe verifiziert den Endpunkt, der Canary die ganze Kette.
- **Identity-Probe** — meldet der Server die Version, die er tatsächlich *ist*? Eine handgepflegte Version im User-Agent driftet lautlos: nichts bricht, kein Test schlägt fehl.
  `scripts/identity_probe.py` liest die Quelle und, mit `--installed`, das ausgelieferte Artefakt.
  Ein Portfolio-Sweep fand 12 von 30 Servern mit falscher Version, 4 davon mit falschem *Major* — [docs/probes/identity.md](docs/probes/identity.md).
- **Shipped-Probe** — ist der Release, den Nutzer installieren, *aktuell*, und *läuft* er? Die CI testet den Branch, nicht das Artefakt: `meteoswiss-mcp` lieferte drei Tage lang jedem frischen Install einen Import-Fehler aus, während `main` längst korrigiert war.
  `scripts/shipped_probe.py` läuft in zwei Tiefen — `--metadata-only` sind zwei HTTP-Requests gegen die **Simple-API** (die Oberfläche, von der pip installiert); die Standardtiefe installiert in ein frisches venv und spricht echtes MCP mit ihr.
  `NO_TAGS`, `STALE_ARTIFACT` und `UNCONFIRMED` halten «nicht messbar» von «in sync» getrennt — [docs/probes/shipped.md](docs/probes/shipped.md).
- **Yank-Probe** — die umgekehrte Frage, über den ganzen Katalog: existiert ein bekannt kaputter, **nicht** zurückgezogener Release, mit einem gesunden Nachfolger daneben?
  `scripts/yank_probe.py` läuft über die `Requires-Dist` jeder Version via PEP-658-Core-Metadaten und meldet `UNYANKED_BROKEN_RELEASE` nur, wenn vier Bedingungen zusammen gelten. Sie **empfiehlt** einen Yank und führt nie einen aus — jeder Request ist ein GET.
  Alle sechs Vorgänger von `zurich-opendata-mcp` 0.5.1 trugen den ungedeckelten Bereich; eine Probe, die nur `latest-1` liest, hätte den Katalog sauber genannt — [docs/probes/yank.md](docs/probes/yank.md).
- **Published-Probe** — was tut das *installierte* Artefakt auf der Leitung? `swiss-efv-mcp` bestand Identity und Shipped, während das Paket, das jeder Nutzer installiert, sich als `Mozilla/5.0 … Chrome/124.0` ausgab.
  `scripts/published_probe.py` installiert in ein Wegwerf-venv und misst den User-Agent, die Importe, das Start-Ereignis des Konsolen-Skripts und die Obergrenzen der tatsächlich importierten Abhängigkeiten.
  Wo sie einen Wert nicht auflösen kann, meldet sie `UNVERIFIED`, nie «sauber» — [docs/probes/published.md](docs/probes/published.md).
- **Lockfile-Probe** — `pyproject.toml` nennt die Obergrenze; nennt sie auch das Lockfile, aus dem das Deployment installiert? Der Bounds-PR wurde grün gemergt, ohne `uv.lock` neu zu erzeugen: der Fix stand in der Datei, die alle lesen, und fehlte in der Datei, die installiert.
  `scripts/lockfile_probe.py` vergleicht die aufgezeichnete `requires-dist` und die gepinnten Versionen und fragt `uv lock --check` / `poetry check --lock`, wo vorhanden. `--check` ist fest verdrahtet: `uv lock` ohne den Schalter überschreibt das Beweismittel.
  `LOCK_DRIFT` druckt beide divergierenden Specifier; im nächtlichen Gate läuft sie **vor `uv sync`**, denn `uv sync` schreibt das Lock neu, und ein Gate dahinter liest eine Datei, die sein eigener Harness gerade repariert hat — [docs/probes/lockfile.md](docs/probes/lockfile.md).
- **Doc-Claim-Probe** — existieren die Identifikatoren, die die Doku zitiert, im Code? Eine `ARCH-003`-Begründung nannte zehn Rubrikcodes, keinen davon in `GREEN_RUBRICS`, und das Review sah es nicht.
  `scripts/doc_claim_probe.py` löst jeden in Backticks gesetzten Code, Pfad und Zugehörigkeitsanspruch in `README`/`SECURITY` gegen die Nicht-Markdown-Dateien des Repos auf.
  Standard-Zitate und Identifikatoren eines anderen Repos sind ausgenommen — und werden *aufgelistet*, denn eine unsichtbare Ausnahme ist ein blinder Fleck — [docs/probes/doc-claim.md](docs/probes/doc-claim.md).
- **Bilinguale Paritäts-Probe** — das Portfolio ist zweisprachig, und nur die englische Seite bewegt sich zuerst. Beide Dateien rendern, eine fehlende Sektion auf einer Seite ist unsichtbar.
  `scripts/parity_probe.py` vergleicht Überschriften-Skelette, Aufzählungszahlen je Sektion, ausgezeichnete Code-Blöcke und Linkziele — nie die Prosa, die *sich* unterscheiden soll.
  `TRANSLATION_LAG` zählt Commits, die das Original nach der letzten Aktualisierung der Übersetzung berührt haben: der Fall, den jede strukturelle Prüfung besteht — [docs/probes/parity.md](docs/probes/parity.md).
- **Reference-Drift-Probe** — ein Skill-Repo liefert Code aus, der in Server kopiert werden soll, und nach dem Kopieren schaut niemand mehr auf beide Hälften zugleich. Am 3.8.2026 trug eine Vorlage fünf Defekte, die elf Server längst einzeln behoben hatten; kein einziges der elf Reviews konnte es sehen.
  `scripts/reference_drift_probe.py` vergleicht die *Eigenschaften*, die die Vorlage zusichert — nie ihren Text, den jeder Übernehmende umbenennt — über eine im Adoptions-Manifest deklarierte Zuordnung ([adoption.example.toml](adoption.example.toml)), nie über eine geratene; ein fehlendes Manifest ist selbst der Befund.
  `REFERENCE_STALE` steht vor `REFERENCE_UNADOPTED`, denn eine veraltete Vorlage ist ein Defekt in Verteilung, und eine Einstimmigkeitsschicht findet die Fixes, für die niemand eine Eigenschaft aufgeschrieben hat — [docs/probes/reference-drift.md](docs/probes/reference-drift.md).
- **Spec-Probe** — welche MCP-Protokollversion *spricht* der Server tatsächlich? Das Boot-Gate trug ein einziges handgepflegtes Literal (`"2025-06-18"`), schickte es in jedem Request und verwarf die Antwort des Servers — kein Report hier konnte die Spec eines Ziels benennen.
  `scripts/spec_probe.py` vergleicht Quelltext, *installiertes* SDK, `mcp_spec_version` aus `portfolio.json` und den Draht und meldet `SPEC_DRIFT`, `LEGACY_TRANSPORT` mit Frist-Countdown oder `UNVERIFIED` — nie «nicht messbar» als «in sync».
  `SPEC_UNDECLARED` ist eine Notiz und kein Befund, denn unter den aktuellen SDKs gehört die Version dem SDK — [docs/probes/spec.md](docs/probes/spec.md).
- **Live-Zeitplan-Probe** — die Live-Tests tragen `@pytest.mark.live` und sind mit `-m "not live"` aus der CI ausgeschlossen. Das ist die Doktrin, und `-m "not live"` ist kein Ort, an dem Tests laufen — es ist die Abwesenheit eines solchen. Der erste Live-Lauf von `meteoswiss-mcp` seit Monaten legte drei von sechs Tests um; der Endpunkt war zwei Tage zuvor abgeschafft worden, und davor hatte die Suite ebenfalls niemand gestartet.
  `scripts/live_schedule_probe.py` fragt, ob ein `cron:`-Workflow einen pytest-Aufruf fährt, der die Marke *auswählt*, und ob ein Fehlschlag gesehen würde; der `-m`-Ausdruck wird ausgewertet, nicht gegreppt, denn `not live` enthält das Wort und schliesst aus, `not slow` enthält es nicht und wählt aus.
  Fünf von zehn Portfolio-Servern verletzen diesen bereits `enforced` gesetzten Katalog-Check, `zh-education` darunter — der Server, dessen Live-Suite einer monatelang unsichtbaren Schema-Drift widersprochen hätte — [docs/probes/live-schedule.md](docs/probes/live-schedule.md).
- **Schema-Feld-Probe** — der Code las `r["Schulgemeinde"]`, die Quelle lieferte `schulgemeinde`. Das Ergebnis war kein Fehler, sondern eine leere Trefferliste mit der Meldung «Schulgemeinde nicht gefunden» — ein Ausfall, der wie eine Antwort aussieht und den ein Aufrufer nicht von echter Abwesenheit unterscheiden kann. Vier von sechs Datensätzen, acht Tools, alle Unit-Tests grün, denn die Fixtures pinnen die alte Kopfzeile.
  `scripts/schema_field_probe.py` vergleicht die Feldnamen, die der *Code* liest, mit denen, die die Quelle gerade liefert — `live_probe` vergleicht live gegen eine Fixture, und eine Fixture kann mit der Quelle übereinstimmen, während der Code einen Namen liest, den keine von beiden je enthielt.
  Die Zuordnung ist in [schema-fields.example.toml](schema-fields.example.toml) deklariert, nie geraten, und ein Name ist nur dann `FIELD_MISSING`, wenn ein anderer an derselben Fundstelle auflöst — eine Fundstelle ohne jeden Treffer ist eine falsche Zuordnung, kein Befund.
  Gegen `zh-education-mcp` am 7.8.2026 fand sie den Rest des Vorfalls: `r.get("Total_19_Jahre_alt")` gegen eine Zeile, deren Schlüssel der Fix kleingeschrieben hatte — seither still der Default, in jeder Zeile — [docs/probes/schema-field.md](docs/probes/schema-field.md).
- **Wertebereichs-Probe** — der Code rief `int()` auf der Spalte `anzahl` auf, und bei kleinen Fallzahlen liefert die Quelle aus Datenschutzgründen den Text `"1 bis 5"` statt einer Zahl. `int("1 bis 5")` wirft; der Aufrufer sah nur «unerwarteter interner Fehler». Gemessene Anteile: 18.6 % von 13 902 Zeilen, 18.1 % von 62 684, 1.0 % von 35 903 — eine Absturzchance von eins zu fünf ist das Normalverhalten des Endpunkts, und keine Fixture kann es zeigen, denn eine Fixture trägt die Zeilen, die jemand ausgesucht hat.
  `scripts/value_domain_probe.py` nutzt Manifest und Abruf der Schema-Feld-Probe mit, findet die an `int()`/`float()` übergebenen Spalten und ordnet jeden gelieferten Wert einem von fünf Kübeln zu — mit dem gemessenen Anteil im Befund.
  Ein gedeckelter Lauf ohne Fund ist `UNVERIFIED` und nicht sauber — die unterdrückten Zeilen häufen sich genau dort, wo niemand hingesehen hat — und eine Spalte, deren Umwandlungen alle abgesichert sind, ist `VALUE_DOMAIN_HANDLED` mit ausgewiesenem Anteil, denn ein Gate, das bei korrekt behandeltem Code rot wird, ist in einer Woche abgeschaltet.
  Gegen `zh-education-mcp` am 7.8.2026 hat sie alle drei von Hand gezählten Anteile über 122 379 Zeilen reproduziert und einen vierten gefunden, den niemand notiert hatte — [docs/probes/value-domain.md](docs/probes/value-domain.md).
- **Deckung gegen das Manifest** — ein Portfolio-Lauf meldete «33 von 33 ok», während `portfolio.json` 43 aktive Server listete. Der Satz war wahr und die Menge falsch: Zähler und Nenner kamen aus derselben Liste, also konnte nichts ihm widersprechen.
  `scripts/coverage.py` ist der einzige Leser des Deckungs-Manifests des Portfolios, und `scripts/coverage_run.py` führt jede Ein-Ziel-Sonde über jeden Eintrag darin.
  Ein Ziel ohne Ergebnis zählt gegen den Nenner und ist **keine** Deckung, deshalb liefert unvollständige Deckung Exit `1` und steht vor einem Befund — [docs/probes/README.md](docs/probes/README.md).
- **Jeder Report benennt seinen Commit** — ein Identity-Befund war im Moment der Messung korrekt und zehn Minuten später falsch, weil `main` weitergezogen war und der Report keinen SHA nannte.
  `scripts/probe_provenance.py` erfasst `HEAD` plus einen Digest des unversionierten Standes zu Beginn jeder Probe und liest beides am Ende erneut.
  Hat sich der Baum bewegt, lautet der Status `MOVED_DURING_RUN` und der Exit-Code `4` — kein Ergebnis, denn der Lauf hat nicht einen Baum gelesen — [docs/probes/provenance.md](docs/probes/provenance.md).
- **Unabhängiger Grader** — LLM-bewertete Checks nutzen eine echt andere Modell-*Familie* als der Schreiber (Schreiber ist Anthropic → Grader defaultet auf `openai:gpt-4o-mini` oder ein lokales Ollama-Modell), damit ein korrelierter blinder Fleck nicht seinen eigenen Output durchwinkt.
- **Kontinuierliches Red-Teaming** — OWASP LLM Top 10 (Prompt Injection, PII-Leak) gegen die MCP-Oberfläche.
- **Mensch als Merge-Gate** — der Agent öffnet nur PRs, pusht nie auf `main`.
- **Proaktiv** — ein täglicher Cron-Audit postet einen Report nach Telegram.

## Voraussetzungen

- Node.js 20+ (OpenClaw, promptfoo)
- Python 3.11+ und [uv](https://github.com/astral-sh/uv)
- Docker (Agenten-Sandbox)
- Telegram-Bot-Token (via [@BotFather](https://t.me/BotFather)) und deine numerische Telegram-User-ID
- Fine-grained GitHub-PAT, auf das Ziel-Repo beschränkt (contents + pull-requests + **issues**, **keine** Secrets) — `issues: write` braucht der nächtliche Findings-Flow (er legt die Schema-Drift-/Red-Team-Tickets an)
- Anthropic-API-Key (Schreiber / Tool-Provider-Familie) **und** ein unabhängiger Grader *anderer* Familie — ein OpenAI-Key (Default `openai:gpt-4o-mini`) oder ein lokales Ollama-Modell (`GRADER_PROVIDER=ollama:chat:llama3.1`, ohne Cloud-Key)

## Installation

```bash
git clone https://github.com/malkreide/mcp-continuous-auditor.git
cd mcp-continuous-auditor
cp .env.example .env        # Tokens eintragen
npm i -g openclaw promptfoo # oder npx verwenden
```

## Verwendung / Schnellstart

```bash
# 1. Gateway starten (liest openclaw/openclaw.json)
openclaw start --config openclaw/openclaw.json

# 2. Auf Telegram dem Bot schreiben:
#    audit
#    -> liefert einen ruff/mypy/pytest-Report, read-only, ohne Code-Änderung

# 3. Deterministische Verifikation lokal ausführen
#    key-loses Profil (kein Modell-Key nötig):
promptfoo eval -c promptfoo/promptfooconfig.determ.yaml
#    volles graded-Profil (llm-rubric + Red-Team; braucht Grader-Key):
promptfoo eval -c promptfoo/promptfooconfig.yaml
```

> promptfoo ist an der Credential-Grenze in zwei Profile geteilt — ein key-loses
> **determ**-Profil (nur dieses fährt der credential-freie Worker) und ein
> **graded**-Profil (llm-rubric + Red-Team, braucht Grader-Key). Siehe
> [promptfoo/README.md](promptfoo/README.md).

## Konfiguration

| Variable | Zweck |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot-Token von @BotFather |
| `TELEGRAM_ALLOW_FROM` | Deine numerische Telegram-User-ID (Gating) |
| `TELEGRAM_ANNOUNCE_TO` | Optional — Ziel für die gateway-unabhängige Meldung (`scripts/telegram_notify.py`); fällt auf die erste ID aus `TELEGRAM_ALLOW_FROM` zurück |
| `TELEGRAM_NOTIFY` | Optional — `1` setzen, damit `scripts/nightly-audit.sh` den Report ohne OpenClaw nach Telegram schiebt (Default aus; siehe [Doku](docs/telegram/standalone-notify.md)) |
| `TELEGRAM_GITHUB_TOKEN` | Optional — PAT (issues: read/write) für den gateway-freien eingehenden Intake (`scripts/telegram_intake.py`); fällt auf den Workflow-Token zurück (siehe [Doku](docs/telegram/standalone-intake.md)) |
| `ANTHROPIC_API_KEY` | Schreiber / Tool-Provider-Familie |
| `OPENAI_API_KEY` | Unabhängiger Grader (Default `openai:gpt-4o-mini`; andere Familie als der Schreiber) |
| `GRADER_PROVIDER` | Optionaler Grader-Override, z.B. `ollama:chat:llama3.1` (lokal, kein Cloud-Key) |
| `GITHUB_TOKEN` | Fine-grained PAT, Ziel-Repo: contents + pull-requests + issues, **keine** Secrets |
| `TARGET_REPO` | z.B. `malkreide/zurich-opendata-mcp` |

## Deployment

Die LLM-Inferenz läuft in der Cloud (Anthropic API) — lokal läuft nur der
OpenClaw-Orchestrator. Da dieser Prozess das GitHub-PAT + den Anthropic-Key hält
und Shell-Tools startet, ist die **empfohlene Betriebsart** ein dediziertes,
netz-isoliertes Gerät statt deines Arbeits-PCs.

**Fang bei Tier 0 an** — der ganze Auditor auf einer Linux-Kiste mit OpenClaws
Docker-Sandbox + den deterministischen Gates, **ohne** microVM / TensorZero. Der
Sicherheitskern (read-only, nur PRs, Schreiber≠Prüfer, deterministische Wahrheit,
Hard-Fail-Disziplin) ist damit schon da; die schwereren Isolationsstufen sind
optional und werden einzeln übernommen. Die Stufentabelle steht in
**[docs/deployment/tier-0.md](docs/deployment/tier-0.md)**.

**Empfohlener Host: ein dedizierter Raspberry Pi 5 (8 GB)** (weiterhin Tier 0 —
eine *Host*-Entscheidung). Die Last ist leicht (Orchestrierung + API-Calls, kein
lokales Modell), und ein separates Gerät fügt eine echte Hardware-/Netzwerk-
Isolationsschicht über die bestehende Docker-Sandbox und den fein-granularen PAT
hinzu. Vollständige Anleitung (OS-Setup, ARM64-Checks, Egress-Allowlist,
systemd-Härtung): **[docs/deployment/raspberry-pi.md](docs/deployment/raspberry-pi.md)**.
Gleichwertige Alternativen: eine lokale Linux-VM in eigenem Subnetz oder ein
günstiger VPS. Die Trade-offs stehen in derselben Anleitung.

Optionale Härtungsstufen: Host-Egress-Allowlist + Forward-Proxy → microVM-
Broker/Worker-Trennung → TensorZero-Cost-Cap (siehe Stufentabelle).

## Projektstruktur

```
openclaw/         OpenClaw-Gateway-Config + Policy-as-Code (SOUL/AGENTS/TOOLS)
openclaw/cron/    nightly-audit Cron-Job-Spec + Installer (täglich 03:00 → Telegram)
skills/           python-auditor, fastmcp-testing, promptfoo-eval,
                  identity-probe, published-probe, shipped-probe, yank-probe,
                  lockfile-probe, doc-claim-probe, parity-probe,
                  reference-drift-probe, live-schedule-probe,
                  schema-field-probe, value-domain-probe
                  (shipped-probe hat den früheren release-gap-Skill aufgenommen)
schemas/          generierte Tool-Output-JSON-Schemas = der Drift-Detektor
promptfoo/        deterministische Asserts, Schema-Drift, Red-Team + Fixtures
scripts/          Audit-Harness, Live-Probe, nightly-audit-Cron-Kern, Budget-Guard,
                  deterministisches Findings→Issue-Routing, gepinnter
                  promptfoo-Installer, gateway-unabhängige Telegram-Meldung +
                  Intake (telegram_notify.py, telegram_intake.py)
                  portfolio_scan.py = die Fächerung: EIN billiges Prädikat über
                  JEDEN Server als Matrix (targets.example.yaml), für die Frage,
                  die ein Ein-Ziel-Nightly nicht beantworten kann — welches Repo
                  aus der Reihe fällt. Ein Lauf muss seine ABDECKUNG behaupten:
                  er weist jedes deklarierte, nicht geprüfte Ziel namentlich aus
                  und gibt ohne vollständige Abdeckung kein Gesamturteil ab
                  (--partial quittiert einen bewusst schmalen Lauf). Der
                  Default-Branch jedes Ziels wird live via
                  `git ls-remote --symref` aufgelöst statt als `main`
                  angenommen — drei dieser Repos laufen auf `master`, und
                  `git remote show origin` liest ein gecachtes HEAD, das an
                  einem Tag für vier davon falsch antwortete
                  yank_probe.py = die umgekehrte Frage: nicht «ist das, was
                  Nutzer installieren, zurückgezogen», sondern «ist ein bekannt
                  kaputter Release noch installierbar» — read-only, zieht nie
                  selbst zurück
                  lockfile_probe.py = die Obergrenze in pyproject.toml gegen die
                  Obergrenze im Lock, aus dem das Deployment installiert
                  doc_claim_probe.py = jeder von der Doku zitierte Identifikator
                  muss im Code auflösbar sein; parity_probe.py = das EN/DE-Paar
                  muss strukturell parallel bleiben
                  reference_drift_probe.py = der Code, den ein Skill-Repo zum
                  Kopieren ausliefert, gegen die Server, die ihn kopiert haben
                  — in BEIDE Richtungen, über eine im Adoptions-Manifest
                  deklarierte, nie geratene Zuordnung, und im Vergleich der
                  zugesicherten Eigenschaften statt des Texts
                  probe_provenance.py = der HEAD-SHA, den jeder Report trägt,
                  und der Status MOVED_DURING_RUN, wenn der Baum sich bewegt hat
                  coverage.py = der EINE Leser des Deckungs-Manifests und der
                  Nenner, gegen den jeder Portfolio-Lauf gehalten wird. Ein Lauf
                  meldete «33 von 33 ok» bei 43 aktiven Servern; Zähler und
                  Nenner kamen aus derselben Liste
                  coverage_run.py = dieselbe Regel für die Ein-Ziel-Sonden: eine
                  Sonde über jeden Manifest-Eintrag, am Ende `n/44 abgedeckt`.
                  Ein Ziel ohne Ergebnis zählt gegen den Nenner und ist KEINE
                  Deckung — Exit 1 steht vor einem Befund
targets.example.yaml  Formatreferenz für die Ziel-Liste der Fächerung; die echte
                  targets.yaml ist gitignored (Inventar, kein Quellcode)
adoption.example.toml  Formatreferenz für die Zuordnung der Reference-Drift-
                  Probe; die echte liegt im Skill-Repo, neben den Vorlagen
relay/            optionaler Cloudflare-Worker für Telegram-Push-Intake in Echtzeit
tensorzero/       Phase 5: LLM-Gateway-Config + Stack (Cost-Caps, A/B, Audit-Trail)
tests/            stdlib-Unit-Tests (1004 in 40 Dateien) — laufen via
                  .github/workflows/tests.yml
.github/          tests.yml = die eigene Suite des Auditors;
                  *.yml.template = CI für das Ziel-Repo
docs/plans/       der v2-Bauplan
docs/cron/        der tägliche nightly-audit-Cron (Ablauf, Modell-Hard-Fail, Install)
docs/deployment/  Raspberry-Pi (empfohlener Host), Phase-5 forkd/microVM-Isolation,
                  updating.md = was ein Rollout je Tier bedeutet und woran
                  man die eigene Form erkennt (ein Checkout vs. Broker/Worker-Split)
                  worker-broker-rollout.md = nur Tier 2: beide Seiten in der
                  richtigen REIHENFOLGE aktualisieren (Broker zuerst — ein alter
                  Broker meldet die Findings eines neuen Workers als grün)
docs/budget/      Phase-5 Budget-Leitplanken (Token-Ceiling, Circuit Breaker)
docs/observability/ Phase-5 TensorZero-Gateway (Cost-Caps, A/B, Audit-Trail)
```

## Roadmap

Phase 0 Baseline → 1 Read-only-Auditor → 2 promptfoo-CI-Gate → 3 PR-only-Worker → 4 Cron + Red-Team → 5 Härtung (forkd, TensorZero). Siehe [docs/plans](docs/plans).

> **Stand Phase 3 — der Pfad Finding → Fix → PR ist agenten-unterstützt und
> menschlich ausgelöst, keine automatisierte Pipeline.** Er ist end-to-end
> vorgeführt in [`examples/worker-tdd-demo/`](examples/worker-tdd-demo/)
> (RED-Test → Fix → GREEN → PR) und durch die TDD-Invarianten in
> `openclaw/workspace/AGENTS.md` geregelt. Ein Worker schneidet einen
> `fix/<slug>`-PR aber erst nach deinem ausdrücklichen Telegram-OK, pro Finding —
> es gibt keine eingecheckte Automatik, die aus einem Finding von selbst einen PR
> macht.

## Verwandte Repos

### Die MCP-Qualitätskette

Vier Skills, ein Lebenszyklus, dazu dieses Projekt im Betrieb. Jeder beantwortet eine andere Frage, in der Reihenfolge, in der sie aufkommt — dieses kommt zuletzt und ist das einzige, das immer weiterfragt. Das gemeinsame GitHub-Topic ist [`mcp-quality-chain`](https://github.com/topics/mcp-quality-chain).

Die vier Skills waren einmal vier Repos. Seit [`mcp-audit-skill v3.0.0`](https://github.com/malkreide/mcp-audit-skill/releases/tag/v3.0.0) liegen sie in einem Baum unter `skills/`, und die drei ehemaligen Repos sind archiviert. **Die Links unten zeigen absichtlich auf diesen Tag** — jeder von ihnen behauptet etwas darüber, was der Skill *sagt*, und eine Behauptung, die auf `main` zeigt, kann aufhören zu stimmen, ohne dass sich hier irgendetwas ändert. Ein veralteter Pin ist sichtbar; ein still verschobenes Ziel nicht.

Der Tag steht an einer Stelle, in `tests/test_quality_chain_table.py`, und der hält jeden Inhalts-Verweis in der Doku dieses Repos auf diesen einen Wert — und verlangt von jeder Ketten-Tabelle mindestens einen solchen Verweis, damit die Zusage ihren Gegenstand nicht still verlieren kann.

Jener Test sichert **Konsistenz zu, nie Aktualität**: Ob der gepinnte Tag noch das neueste Release ist, braucht Netz — und er sagt das, statt es entdecken zu lassen. `scripts/audit_pin_drift.py` ist die andere Hälfte: wöchentlich über `.github/workflows/audit-pin-drift.yml`, mit zwei Fragen — gibt es den gepinnten Tag oben überhaupt noch, und ist er noch das neueste Release? Ein roter Lauf dort ist kein Defekt hier, sondern die Aufforderung, den Pin bewusst zu heben.

| Phase | Skill | Frage, die er beantwortet |
|---|---|---|
| vor dem Bau | [`mcp-data-source-probe`](https://github.com/malkreide/mcp-audit-skill/tree/v3.0.0/skills/mcp-data-source-probe) | Taugt die Quelle, und was hat sie? Die Recall-Ground-Truth aus Schritt 1.4 ist das, wogegen die `min_count`-Floors hier messen |
| im Bau | [`mcp-data-fidelity`](https://github.com/malkreide/mcp-audit-skill/tree/v3.0.0/skills/mcp-data-fidelity) | Liefert er, was die Quelle hat? Seine Regel 5 — Recall in den Tests, nicht in der Beschreibung — ist der Grund, warum die Probes Floors tragen statt Schema-Assertions |
| im Bau | [`mcp-transport-hardening`](https://github.com/malkreide/mcp-audit-skill/tree/v3.0.0/skills/mcp-transport-hardening) | Kommt er hoch, weist er richtig ab, bleibt er zustandslos? Seine Regeln 1–4 sind das, wofür `transport_boot_probe.py` das Ziel hochfährt — beide Repos zitieren denselben Vorfall, `parlament-mcp#29` — und seine Stateless- und Legacy-SSE-Regeln sind das, was `spec_probe.py` als `SPEC_DRIFT` und `LEGACY_TRANSPORT` meldet |
| nach dem Bau | [`mcp-audit`](https://github.com/malkreide/mcp-audit-skill/tree/v3.0.0) | Hält er gegen den Katalog? Sein `OPS-005` (Pipeline-Ehrlichkeit) stammt aus diesem Repo — [#29](https://github.com/malkreide/mcp-continuous-auditor/pull/29), eine Testsuite, die kein Workflow je ausgeführt hat |
| im Betrieb | **`mcp-continuous-auditor`** | **Dieses Projekt:** hält er morgen noch? |

Daneben, nicht Teil der Kette: [`mcp-builder`](https://github.com/anthropics/skills/tree/main/skills/mcp-builder) — generische Bauanleitung von Anthropic. Fremdes Repo, kann das Topic nicht tragen.

Die vier Skills sagen, wie ein korrekter Server aussieht; dieses Projekt ist der Teil, der weiterprüft, wenn alle aufgehört haben hinzuschauen. Jede Probe hier existiert, weil ein Server gleichzeitig grün und falsch war — genau die Fehlerklasse, für die sie alle geschrieben wurden.

Die geprüften Server sind das [Swiss Public Data MCP](https://github.com/malkreide/swiss-public-data-mcp) Portfolio mit dem eigenen Topic [`swiss-public-data-mcp`](https://github.com/topics/swiss-public-data-mcp).

## Changelog

Siehe [CHANGELOG.md](CHANGELOG.md)

## Lizenz

MIT License — siehe [LICENSE](LICENSE)

## Autor

Hayal Özkan · [malkreide](https://github.com/malkreide)
