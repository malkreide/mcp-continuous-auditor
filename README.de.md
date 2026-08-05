# MCP Continuous Auditor

![Version](https://img.shields.io/badge/version-0.2.0-blue)
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
- **Spec-Probe** — welche MCP-Protokollversion *spricht* der Server tatsächlich? Das Boot-Gate trug ein einziges handgepflegtes Literal (`"2025-06-18"`), schickte es in jedem Request und verwarf die Antwort des Servers — kein Report hier konnte die Spec eines Ziels benennen.
  `scripts/spec_probe.py` vergleicht Quelltext, *installiertes* SDK, `mcp_spec_version` aus `portfolio.json` und den Draht und meldet `SPEC_DRIFT`, `LEGACY_TRANSPORT` mit Frist-Countdown oder `UNVERIFIED` — nie «nicht messbar» als «in sync».
  `SPEC_UNDECLARED` ist eine Notiz und kein Befund, denn unter den aktuellen SDKs gehört die Version dem SDK — [docs/probes/spec.md](docs/probes/spec.md).
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
                  lockfile-probe, doc-claim-probe, parity-probe
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
                  probe_provenance.py = der HEAD-SHA, den jeder Report trägt,
                  und der Status MOVED_DURING_RUN, wenn der Baum sich bewegt hat
targets.example.yaml  Formatreferenz für die Ziel-Liste der Fächerung; die echte
                  targets.yaml ist gitignored (Inventar, kein Quellcode)
relay/            optionaler Cloudflare-Worker für Telegram-Push-Intake in Echtzeit
tensorzero/       Phase 5: LLM-Gateway-Config + Stack (Cost-Caps, A/B, Audit-Trail)
tests/            stdlib-Unit-Tests (687 in 30 Dateien) — laufen via
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

Fünf Repos, ein Lebenszyklus. Jedes beantwortet eine andere Frage, in der Reihenfolge, in der sie aufkommt — dieses kommt zuletzt und ist das einzige, das immer weiterfragt. Das gemeinsame GitHub-Topic ist [`mcp-quality-chain`](https://github.com/topics/mcp-quality-chain) und listet alle fünf auf einer Seite.

| Phase | Repo | Frage, die es beantwortet |
|---|---|---|
| vor dem Bau | [`mcp-data-source-probe-skill`](https://github.com/malkreide/mcp-data-source-probe-skill) | Taugt die Quelle, und was hat sie? Die Recall-Ground-Truth aus Schritt 1.4 ist das, wogegen die `min_count`-Floors hier messen |
| im Bau | [`mcp-data-fidelity-skill`](https://github.com/malkreide/mcp-data-fidelity-skill) | Liefert er, was die Quelle hat? Seine Regel 5 — Recall in den Tests, nicht in der Beschreibung — ist der Grund, warum die Probes Floors tragen statt Schema-Assertions |
| im Bau | [`mcp-transport-hardening-skill`](https://github.com/malkreide/mcp-transport-hardening-skill) | Kommt er hoch, weist er richtig ab? Der Transport-Pfad, den die Canary-Probe live durchläuft |
| nach dem Bau | [`mcp-audit-skill`](https://github.com/malkreide/mcp-audit-skill) | Hält er gegen den Katalog? Sein `OPS-005` (Pipeline-Ehrlichkeit) stammt aus diesem Repo — [#29](https://github.com/malkreide/mcp-continuous-auditor/pull/29), eine Testsuite, die kein Workflow je ausgeführt hat |
| im Betrieb | **`mcp-continuous-auditor`** | **Dieses Projekt:** hält er morgen noch? |

Daneben, nicht Teil der Kette: [`mcp-builder`](https://github.com/anthropics/skills/tree/main/skills/mcp-builder) — generische Bauanleitung von Anthropic. Fremdes Repo, kann das Topic nicht tragen.

Die vier Skills sagen, wie ein korrekter Server aussieht; dieses Projekt ist der Teil, der weiterprüft, wenn alle aufgehört haben hinzuschauen. Jede Probe hier existiert, weil ein Server gleichzeitig grün und falsch war — genau die Fehlerklasse, für die alle fünf geschrieben wurden.

Die geprüften Server sind das [Swiss Public Data MCP](https://github.com/malkreide/swiss-public-data-mcp) Portfolio mit dem eigenen Topic [`swiss-public-data-mcp`](https://github.com/topics/swiss-public-data-mcp).

## Changelog

Siehe [CHANGELOG.md](CHANGELOG.md)

## Lizenz

MIT License — siehe [LICENSE](LICENSE)

## Autor

Hayal Özkan · [malkreide](https://github.com/malkreide)
