# MCP Continuous Auditor

![Version](https://img.shields.io/badge/version-0.1.0-blue)
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
- **Identity-Probe** — ein Server, dessen User-Agent eine handgepflegte Version trägt, driftet lautlos: nichts bricht, kein Test schlägt fehl, und er stellt sich jedem Upstream weiter als ein Release vor, das er nicht mehr ist. Ein Portfolio-Sweep fand 12 von 30 Servern mit falscher Version, 4 davon mit falschem *Major*. `scripts/identity_probe.py` prüft die Quelle und, mit `--installed`, das ausgelieferte Artefakt — der einzige Nachweis, der eine veraltete Editable-Installation überlebt.
- **Shipped-Probe** — die Identity-Probe fragt, ob die gemeldete Version *korrekt* ist; diese fragt, ob sie *aktuell* ist, und dann, ob sie *läuft*. Ein Repository kann grün, auditiert und vollständig repariert sein, während jedes `pip install` weiterhin den kaputten Release ausliefert, denn die CI testet den Branch und nicht das Artefakt. `meteoswiss-mcp` lieferte drei Tage lang jedem frischen Install einen Import-Fehler aus, während `main` längst korrigiert war — aufgefallen ist es erst durch einen externen Bug-Report. `scripts/shipped_probe.py` läuft in zwei Tiefen: `--metadata-only` vergleicht Index-Version, Yank-Status, Release-Tags und unveröffentlichte Commits für zwei HTTP-Requests und gewichtet ein `fix:` anders als ein `docs:` — die Commit-Art schlägt das Alter, ein nutzerrelevanter Commit wird gemeldet, sobald er unveröffentlicht ist, ein brechender in jedem Alter, während Hausarbeit die Sieben-Tage-Uhr behält; die Standardtiefe installiert die Distribution danach in ein frisches venv, vergleicht die installierten Quellen gegen den Checkout und spricht echtes MCP mit ihr. Zwei Anker schliessen die Lücken, die eine Versionsnummer offen lässt: `NO_TAGS`, denn ein Repository ohne Release-Tags lässt die Hälfte dieser Prüfungen ins Leere laufen und muss das sagen, statt «OK» zu melden (ein Shallow-Clone ist *unbekannt*, nicht null), und `STALE_ARTIFACT`, das Inhalt statt Nummern vergleicht — der einzige Weg, den Fall zu sehen, in dem Index und Checkout beide 0.3.3 heissen und verschieden sind. `--pin-version` installiert `dist==VERSION` für die Nachprüfung nach einem Release: ein ungepinnter Install lieferte nachweislich noch Minuten nach der Veröffentlichung das alte Artefakt aus, trotz `--no-cache-dir`. Sie liest die **Simple-API** unter `--index-url` — die Oberfläche, von der `pip` installiert, in beiden Varianten (PEP 691 JSON und PEP 503 HTML, da nur HTML garantiert ist) — weil PyPIs JSON-API nachweislich um Minuten hinterherhinkte, sowohl bei der neuesten Version als auch beim `yanked`-Flag; auf PyPI wird die JSON-API als Zweitmeinung gelesen, und wo beide sich widersprechen, lautet die Antwort `UNCONFIRMED` statt geraten. Das schliesst zugleich einen blinden Fleck, den die Versionsnummer allein nicht sieht: ein *zurückgezogener* Release sieht aus wie ein gesunder — die Version existiert, das Tag passt, die CI ist grün — während jedes `pip install` still auf etwas Älteres auflöst. Ein unerreichbarer Index gilt als Harness-Fehler, nie als «in sync».
- **Yank-Probe** — die Shipped-Probe fragt, ob die Version, die Nutzer *gerade jetzt* installieren, zurückgezogen ist. Diese fragt das Gegenteil, über den ganzen Katalog: existiert ein bekannt kaputter, **nicht** zurückgezogener Release, mit einem gesunden Nachfolger daneben? `zurich-opendata-mcp` 0.5.1 deklarierte `mcp[cli]>=1.28.1` ohne Obergrenze; `mcp` 2.0.0 entfernte `mcp.server.fastmcp`, und jeder frische Install starb beim Import. 0.6.0 hat es behoben — und Nachfolgen allein genügte nicht, denn der kaputte Release blieb wählbar für jeden, den ein Resolver per altem Lockfile oder kollidierendem Pin von 0.6.0 weggezwungen hatte. Zwei Dinge, die eine naive Prüfung übersieht: **alle sechs** Vorgänger trugen einen ungedeckelten `mcp`-Bereich, eine Probe, die nur `latest-1` liest, hätte einen von sechs gefunden und den Katalog als sauber gemeldet; und ein Yank ist *keine* Löschung — PEP 592 hält den Release für einen expliziten Pin auflösbar, mit Warnung, damit bestehende Lockfiles nicht brechen, und der Befund sagt nie «löschen». `scripts/yank_probe.py` läuft über die `Requires-Dist` jeder Version via PEP-658-Core-Metadaten (ohne Wheel-Downloads) und meldet `UNYANKED_BROKEN_RELEASE` nur, wenn vier Bedingungen zusammen gelten — der Release ist nicht zurückgezogen, sein Bereich ist ungedeckelt, der Pin des gesunden Nachfolgers *schliesst* die Untergrenze dieses Bereichs aus, und jenseits der Grenze ist tatsächlich etwas publiziert und wählbar. Die dritte Bedingung rechtfertigt das Wort «kaputt»: der spätere Release des Maintainers hat den Übergang bereits selbst als brechend erklärt. Ein fehlender Yank-*Grund* ist ein eigener, niedriger Befund, denn `pip` gibt `Reason for being yanked: <none given>` an genau das Publikum aus, das ein zurückgezogener Release noch hat. Die Probe **empfiehlt** einen Yank und führt nie einen aus: das bräuchte einen Upload-Token und ist die Entscheidung des Maintainers, also gibt es dafür keinen Schalter und jeder Request ist ein GET.
- **Published-Probe** — die Identity-Probe liest ein Repository und die Metadaten-Tiefe der Shipped-Probe vergleicht Versions*nummern*; keine von beiden öffnet das Artefakt. `swiss-efv-mcp` besteht beide (PyPI 0.3.0, `main` 0.3.0, `src/` sauber), während das Paket, das jeder Nutzer installiert, `Mozilla/5.0 … Chrome/124.0` sendet — es gibt sich Upstreams gegenüber als Browser aus. `scripts/published_probe.py` installiert die Distribution in ein Wegwerf-venv und misst, was der ausgelieferte Code tut: den User-Agent, den er auf die Leitung legt — 16 von 33 Portfolio-Paketen meldeten eine Version, die sie nicht waren; der Fix ist in allen gemergt — plus drei Dinge, die ein User-Agent nicht verrät. **Import-Fehler bestimmen den Status** (`broken_import`), und das Paket-Root wird vor seinen Submodulen importiert, damit ein Artefakt der Import-Reihenfolge nicht als Defekt gilt: `bag-health-mcp` wurde als Zirkelimport gemeldet, den es nicht hat — nur das private Submodul *als allererster Import eines Prozesses* scheitert. **Start ist nicht Import**: das installierte Konsolen-Skript läuft mit geschlossenem stdin ein paar Sekunden und muss ein `server.start`-Ereignis melden, ohne abzustürzen — ein sauberer Exit ohne Meldung ist kein Bestehen. Und **`requires_dist` wird auf fehlende Obergrenzen geprüft**, bei den Abhängigkeiten, die das Paket tatsächlich importiert: `swiss-energy-mcp` 0.3.3 lieferte `mcp[cli]>=1.20.0` ohne Deckel aus, und als `mcp` 2.0.0 erschien, war jede Neuinstallation tot — gemeldet, sobald der Index einen höheren Major führt, also vor dem Bruch statt danach. Wo sie einen Wert nicht auflösen kann, meldet sie `UNVERIFIED`, nie «sauber» — eine frühere Fassung, die «nichts gefunden» mit «da ist nichts» verwechselte, nannte 24 Pakete unauffällig, von denen 16 drifteten.
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
                  identity-probe, published-probe, shipped-probe, yank-probe
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
targets.example.yaml  Formatreferenz für die Ziel-Liste der Fächerung; die echte
                  targets.yaml ist gitignored (Inventar, kein Quellcode)
relay/            optionaler Cloudflare-Worker für Telegram-Push-Intake in Echtzeit
tensorzero/       Phase 5: LLM-Gateway-Config + Stack (Cost-Caps, A/B, Audit-Trail)
tests/            stdlib-Unit-Tests (605 in 26 Dateien) — laufen via
                  .github/workflows/tests.yml
.github/          tests.yml = die eigene Suite des Auditors;
                  *.yml.template = CI für das Ziel-Repo
docs/plans/       der v2-Bauplan
docs/cron/        der tägliche nightly-audit-Cron (Ablauf, Modell-Hard-Fail, Install)
docs/deployment/  Raspberry-Pi (empfohlener Host), Phase-5 forkd/microVM-Isolation,
                  worker-broker-rollout.md = beide Seiten in der richtigen
                  REIHENFOLGE aktualisieren (Broker zuerst — ein alter Broker
                  meldet die Findings eines neuen Workers als grün)
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
