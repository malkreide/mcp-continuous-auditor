# Lösungsanalyse — MCP Continuous Auditor (Stand 2026-07-04)

> Zweiter Review-Zyklus. Der erste (PR #12) führte zu S1–S3/T2 (Cross-Family-
> Grader, Broker-Klassifikation, Egress-Interlock, gepinntes promptfoo).
> Dieses Dokument bewertet den Stand **nach** diesen Härtungen: Ist die Lösung
> zielführend? Wo sind Lücken (Technik, UX, Sicherheit)? Was ist zu verbessern?
> Finding-IDs: `A-*` (Architektur/Gesamt), `S-*` (Sicherheit), `T-*` (Technik),
> `U-*` (UX/Betrieb) — Nummerierung neu, überschneidet sich nicht mit PR #12.

---

## 1. Gesamturteil: zielführend? **Ja — mit vier substanziellen Integrationslücken.**

Die Architektur ist ungewöhnlich diszipliniert und trägt das erklärte Ziel
(ein kontinuierlicher, vertrauenswürdiger MCP-Auditor, bei dem nie ein
LLM-Urteil die Wahrheitsinstanz ist):

- **Deterministische Wahrheit** — pytest/ruff/mypy + promptfoo + Schema-Drift-Gate,
  Exit-Codes als Vertrag (`0/2/1`), Klassifikator getestet. Das Kernversprechen
  „CI entscheidet, nie ein Agent" ist konsequent umgesetzt.
- **Vertrauensgrenzen stimmen konzeptionell**: Worker (untrusted, credential-frei,
  throwaway microVM) liefert nur Roh-Evidenz; Broker (Credentials) re-klassifiziert;
  Mensch ist Merge-Gate; Writer ≠ Grader (Cross-Family).
- **Ehrliche Dokumentation**: Restrisiken (selbstkonsistente Fälschung, Port- statt
  Domain-Allowlist) sind explizit benannt statt wegdokumentiert.
- **Fail-closed-Grundhaltung**: unauflösbares Modell → Hard-Fail; Breaker-Skip →
  nie „grün"; fehlende Evidenz → Hard-Fail.

Die Schwächen liegen fast alle dort, wo eine dokumentierte Garantie **nicht
vollständig implementiert** ist. Vier davon sind substanziell (S-A, T-A, T-B,
S-E) — keine kippt die Architektur, aber alle vier schwächen genau die
Eigenschaften, die das Projekt als Alleinstellungsmerkmal beansprucht.

---

## 2. Sicherheitslücken

### S-A (hoch) — Broker klassifiziert „grün" bei fehlendem promptfoo.json + all-zero-Gates

`nightly_audit_report.py::classify_promptfoo`: Ist die promptfoo-JSON **abwesend**
und `promptfoo_rc == 0`, ergibt sich `ran=False, errors=0` — und `build_summary`
erzeugt **keinen** Hard-Fail-Grund (`if not pfc["ran"] and args.promptfoo_rc not in (0,)`).
Ein kompromittierter Worker, der Evidenz mit fünf Null-Exit-Codes schickt und die
promptfoo.json **weglässt**, wird vom Broker **grün** klassifiziert.

Das widerspricht der in `broker-listener.sh` dokumentierten Garantie
(„omission, garbling, or an exit-code/promptfoo mismatch can no longer read as
green") und ist eine Lücke **innerhalb** des erklärten Threat-Models — schwächer
als die (bewusst ausgeklammerte) selbstkonsistente Fälschung, denn hier genügt
Weglassen. Der Test `test_forged_green_exit_codes_are_caught_by_promptfoo_evidence`
deckt nur den Fall *vorhandener* promptfoo-JSON mit Failures ab.

**Fix:** Im `--from-evidence`-Pfad gilt: `promptfoo_rc == 0` **und** keine
parsebare promptfoo-JSON ⇒ Hard-Fail („evidence incomplete"). Test ergänzen.

### S-B (mittel) — Worker-Bootstrap verletzt die eigene Policy: `curl | sh`, ungepinnte Installer, ungepinnter Auditor

`worker-cloud-init.yaml.tmpl` lädt zur Laufzeit `astral.sh/uv/install.sh | sh`
und `deb.nodesource.com/setup_20.x | bash` — exakt das Muster, das `TOOLS.md`
verbietet („never pipe network data into a shell"). Beide mit `|| true`
(Installationsfehler werden verschluckt), beide ungepinnt (Supply-Chain: der
Installer von heute Nacht ist nicht der reviewte von gestern). Zusätzlich wird
der Auditor selbst per `--branch ${AUDITOR_REF}` (Branch, nicht SHA) geklont —
was nachts läuft, ist nicht zwingend das, was reviewt wurde.

Die microVM begrenzt den Blast-Radius, aber der Worker ist genau die Maschine,
die untrusted Ziel-Code ausführt — sein eigener Bootstrap sollte die härteste,
nicht die weichste Stelle sein.

**Fix:** Toolchain in das Base-Image backen (`build-worker-image.sh`), zur
Laufzeit nichts mehr aus dem Netz installieren; `AUDITOR_REF` auf Tag/SHA pinnen
und nach dem Clone verifizieren (`git rev-parse HEAD` gegen erwarteten SHA);
`|| true` entfernen — Bootstrap-Fehler ⇒ Evidenz „could-not-run", nie stiller
Weiterlauf.

### S-C (mittel) — Egress-Allowlist verhindert Lateral Movement, aber keine Exfiltration

Bewusst dokumentiert, dennoch als Lücke festzuhalten: `egress-allowlist.nft`
erlaubt (a) **DNS an beliebige Resolver** — ein klassischer Tunnel-/Exfil-Kanal —
und (b) **443 ins gesamte öffentliche Internet** — Exfil an einen beliebigen
Angreifer-Server ist trivial. Der reale Gewinn (LAN-/Link-Local-Drop, keine
Random-Ports) ist wertvoll, aber „Egress nur GitHub-anon + Zürich"
(phase5-rollout.md, README) verspricht mehr, als die Regeln liefern. Außerdem
ist die im Runbook beschriebene **Broker**-seitige Allowlist (nur
anthropic/openai/github/telegram) nicht als Code mitgeliefert — nur die
Worker-Hälfte existiert.

**Fix:** (1) DNS auf feste Resolver (z. B. 9.9.9.9/1.1.1.1) einschränken;
(2) Filter-Proxy (z. B. squid/tinyproxy mit Domain-Allowlist) als nächste
Ausbaustufe konkret einplanen — die Doku verweist darauf, es fehlt ein
Template; (3) die Broker-Allowlist als zweite nft-Datei ausliefern;
(4) Formulierungen in README/Runbook auf das tatsächlich Erzwungene abschwächen.

### S-D (mittel) — Broker-Listener: fehlende Limits + unsanitisierte Strings im Report

`broker-listener.sh` / `_receive-one.sh`:

- **Kein Größenlimit** auf dem vsock-Stream: ein kompromittierter Worker kann
  die Broker-Disk mit einem endlosen tar füllen (DoS auf der Credential-Seite).
- **Kein Read-Timeout** auf der Header-Zeile: eine offene Verbindung ohne Daten
  hält einen Handler-Prozess unbegrenzt.
- **Symlink-Member**: `tar -x` mit exakter Namensliste verhindert Traversal,
  aber ein Member `nightly-evidence.json`, das ein **Symlink** ist, wird als
  solcher extrahiert; der Klassifikator liest dann eine beliebige Broker-Datei
  als „Evidenz" (Ausgang meist Hard-Fail, aber es ist ein Read-Primitive).
- **Unsanitisierte Strings**: `header` wird roh in Log/Datei geschrieben
  (Terminal-Escape-Injection beim Operator), und `target`/`target_sha` aus der
  untrusted Evidenz fließen ungefiltert in `nightly-report.md` — ein Worker kann
  per eingebetteten Newlines/Markdown den Report strukturell verfälschen
  („## Alles grün…"), der an Telegram, in Issues **und in den Kontext des
  credential-haltenden Cron-Agenten** geht. Das ist ein IPI-Kanal direkt in die
  Broker-Seite.

**Fix:** `head -c`-Limit (z. B. 10 MB) vor tar; `read -t`; nach Extraktion
`[ -f … ] && [ ! -L … ]` erzwingen; `target` gegen `^[A-Za-z0-9._/-]+$` und
`sha` gegen `^[0-9a-f]{4,40}$` validieren (sonst „invalid" + Hard-Fail);
Header auf N Bytes kürzen und nicht-druckbare Zeichen strippen. Dasselbe
Sanitizing gehört in `nightly_audit_report.py` (Verteidigung an der Senke —
auch die promptfoo-`examples` sind untrusted und landen im Report).

### S-E (mittel) — Dokumentierter PAT-Scope kann den Findings-Flow nicht ausführen

README/Plan/`.env.example` schreiben durchgängig „fine-grained PAT: contents +
pull-requests, sonst nichts". Der Cron-Flow (Schritt 5 in
`openclaw/cron/nightly-audit.json`) verlangt aber **Issues öffnen/aktualisieren**
— dafür braucht der PAT `issues: write`. Folge in der Praxis: entweder schlägt
der Findings-Pfad nachts fehl, oder Operatoren über-scopen den Token ad hoc und
undokumentiert. Beides ist schlechter als ein korrekt dokumentierter Minimal-Scope.

**Fix:** Scope-Doku auf `contents + pull-requests + issues` korrigieren (alle
Fundstellen), und im Install-/Preflight-Pfad die effektiven Token-Rechte einmal
gegen die benötigten prüfen (ein `GET /repos/{target}` + Doku-Hinweis genügt).

### S-F (niedrig) — Writer-≠-Grader-Invariante wird nirgends erzwungen

Die Cross-Family-Eigenschaft (S1 aus PR #12) hängt allein an Defaults und
Kommentaren. Setzt ein Operator `GRADER_PROVIDER=anthropic:…`, degradiert die
Unabhängigkeit still — kein Skript prüft die Familie. **Fix:** In
`nightly-audit.sh` (und dem CI-Template) ein Guard: beginnt `GRADER_PROVIDER`
mit `anthropic`, während der Writer Anthropic ist ⇒ Hard-Fail mit klarer Meldung
(Override-Flag für bewusste Ausnahmen).

---

## 3. Technische Lücken

### T-A (hoch) — Der OWASP-Red-Team-Block wird sehr wahrscheinlich nie ausgeführt

`promptfooconfig.yaml` enthält einen `redteam:`-Block (pii, prompt-injection,
sql-injection, jailbreak-Strategien) — aber CI-Template und `nightly-audit.sh`
rufen nur `promptfoo eval -c …` auf. In promptfoo (auch 0.121.x) führt `eval`
den `redteam:`-Block **nicht** aus: adversariale Fälle müssen mit
`promptfoo redteam generate` erzeugt (→ `redteam.yaml`) und dann evaluiert
werden (bzw. `promptfoo redteam run`). Konsequenz: Das beworbene „Continuous
red-teaming (OWASP LLM Top 10)" reduziert sich real auf die ~4 statischen
Injection-Negativtests; der `redteam`-Zweig des Klassifikators
(`_is_redteam`, Issue-Label `redteam`) kann aus dem Plugin-Pfad nie feuern.

**Fix:** (1) Gegen die gepinnte Version 0.121.17 verifizieren; (2) den
Generate-Schritt verdrahten: `redteam generate` mit **gepinntem**
Angreifer-Modell, die generierten Fälle **committen** (Determinismus! sonst ist
das Gate wieder ein Moving Target) und nightly + CI gegen die committeten Fälle
evaluieren; Regeneration als bewusster, review-pflichtiger Schritt (z. B.
wöchentlicher Job, der bei neuen Fällen einen PR öffnet).

### T-B (hoch) — Budget-Guard ist im MicroVM-Zielbild funktionslos

Der Worker läuft bewusst mit `BUDGET_GUARD=0` („der Breaker lebt auf der
Broker-Seite"). Aber: weder `run-worker.sh` noch `broker-listener.sh` rufen
`budget_guard.py preflight/record` auf — auf der Broker-Seite füttert **nichts**
den Breaker. Auch die Behauptung in `run-worker.sh` („the breaker on the Broker
side then counts the missing result as a failure on the next cycle") hat keine
Implementierung: ein ausbleibendes Ergebnis wird nirgends gezählt. Im
Phase-5-Zielbild sind damit Circuit-Breaker, Token-Ceilings und die
Missing-Result-Erkennung komplett inaktiv — genau in der Betriebsart, die als
Zielarchitektur beworben wird. Nur im Direkt-Pfad (`nightly-audit.sh` ohne
microVM) sind die Leitplanken verdrahtet.

**Fix:** Den Breaker auf die Broker-Seite ziehen: `broker-listener.sh` ruft nach
jeder Klassifikation `budget_guard record --exit-code <outcome>` auf, und der
Orchestrator ruft vor `run-worker.sh` `budget_guard preflight`. Ein Worker-Lauf
ohne eingegangene Evidenz innerhalb des Timeouts wird als Hard-Fail
(`record --exit-code 1`) gezählt, damit die Missing-Result-Erkennung real greift.

### T-C (hoch) — Der credential-freie Worker kann den `llm-rubric`-Grader nicht ausführen

Der Worker bootet ohne API-Keys (`worker-cloud-init.yaml.tmpl`: nur `TARGET_REPO`,
`BUDGET_GUARD`), führt aber die **vollständige** `promptfooconfig.yaml` aus. Der
IPI-Test nutzt ein `llm-rubric`-Assert, das zwingend einen Grader-Key braucht.
Ohne Key liefert promptfoo `stats.errors > 0`, was der Klassifikator korrekt als
Hard-Fail wertet — der Worker-Pfad hard-failt damit **jede Nacht** auf der
LLM-gegradeten Ebene. Zusammen mit T-A (Red-Team läuft ohnehin nicht) bedeutet
das: in der microVM-Topologie läuft real nur die key-lose deterministische Hälfte
(`is-json`/`javascript`/`contains`) — die beiden Modell-abhängigen Schichten sind
entweder inaktiv (T-A) oder erzeugen einen Fehlschlag (T-C).

**Fix:** Die Wahrheitsinstanz an der Credential-Grenze spalten — genau wie die
bereits gebaute Evidenz/Klassifikations-Trennung: deterministische Asserts
(key-los) im Worker; `llm-rubric` + generatives Red-Team auf der Broker-Seite
(die die Keys hält). Die promptfoo-Config in zwei Profile teilen (`--filter-*`
oder zwei Config-Dateien), Worker fährt nur das key-lose Profil.

### T-D (mittel) — Schema-Drift-Gate ist bei Abwesenheit im Ziel „grün"

`nightly-audit.sh`: fehlt `schemas/generate_schemas.py` im Ziel ⇒ `rc_schema=0`
(„absence is not drift"), der Lauf kann grün werden. Fehlt dagegen die
promptfoo-Config ⇒ `rc_pf=127` ⇒ Hard-Fail. Diese Asymmetrie heißt: das Entfernen
des Schema-Generators im Ziel **deaktiviert einen Gate lautlos**, ohne rot zu
werden. Für einen Drift-Detektor ist „Gate weg = grün" die falsche
Default-Richtung — es ist derselbe Fehlermodus wie S-A, nur eine Ebene höher.

**Fix:** Abwesenheit des erwarteten Gates ⇒ Hard-Fail (oder mindestens ein
lautes „gate not present"-Finding, `rc=2`), nie stilles Grün. Wenn ein Ziel
legitim keine Schemas hat, per explizitem Opt-out-Flag, nicht per Abwesenheit.

### T-E (mittel) — Per-Lauf-Token-Ceiling ist ohne TensorZero weitgehend wirkungslos

Ohne Gateway zählt `budget_guard record` nur `tokens_from_promptfoo(...)` — die
Eval-Tokens, **nicht** den Verbrauch des OpenClaw-Auditor-Agenten selbst (dessen
Think/Tool-Loop der teure Teil ist). Das „harte Pro-Lauf-Ceiling" bindet im
Default-Deployment nur einen Bruchteil der realen Kosten; die Rolling-Window- und
Breaker-Logik operieren auf einer massiv unterzählten Basis.

**Fix:** Den Agent-Token-Verbrauch einbeziehen (OpenClaw-Usage-Callback), oder
das Ceiling ehrlich als „nur Eval-Tokens" deklarieren und den echten Cost-Cap
klar als TensorZero-only ausweisen (README/guardrails.md).

### T-F (niedrig) — Dünne Vertragsabdeckung; Fallback-Fehletikettierung

Der Plan nennt *24 Tools / 5 Resources*; die promptfoo-Config hat Vertrags-/
Injection-Asserts für ~5 Tools. Das Schema-Gate deckt alle Tools mit Rückgabe-Typ
ab, aber die is-json-/Injection-Ebene ist punktuell — „deterministische Wahrheit"
gilt real für einen Ausschnitt. Zusätzlich fällt in `classify_promptfoo` ein
*unklassifizierter* Failure auf `contract_failures` (→ `schema_drift=true`); ein
generisches Assert-Fail wird damit als „Schema-Drift" gemeldet.

**Fix:** Contract-Tests Richtung aller Tools ausweiten; für den Klassifikator eine
dritte Kategorie „other/unknown finding" statt Default-`schema_drift`, damit das
Issue-Label die Ursache nicht falsch benennt.

### T-G (niedrig) — Laufzeit-Fetch, brüchiges Framing, verschluckter Reset

`npx -y promptfoo@<v>` zieht die Truth-Engine jede Nacht neu (Version gepinnt,
transitive Deps ungepinnt, kein Lockfile). Der vsock-Frame ist zeilen-, nicht
längen-präfixiert (Desync möglich, siehe S-D). `git reset --hard origin/$REF ||
true` schluckt einen fehlgeschlagenen Reset — dann wird still ein veralteter
Checkout auditiert.

**Fix:** promptfoo vendored/`npm ci`-gepinnt; Frame längen-präfixieren; Reset-
Fehler ⇒ Hard-Fail statt `|| true`.

---

## 4. UX- / Betriebslücken

### U-A (mittel) — Sehr hohe Setup-Komplexität für ein Ziel

Nested KVM, nftables, vsock/udev, cloud-init, TensorZero + ClickHouse, OpenClaw,
promptfoo, zwei Modell-Provider — für das Auditieren *eines* MCP-Servers. Die
Runbooks sind gut, aber die operative Oberfläche ist groß und bricht an vielen
Stellen (nested virt, `vhost_vsock`, udev). **Fix:** Einen „Tier-0"-Modus
dokumentieren, der ohne microVM/TensorZero auskommt (nur Docker-Sandbox +
Direkt-Pfad), als real empfohlener Einstieg; microVM/TensorZero als klar
getrennte, optionale Härtungsstufe.

### U-B (mittel) — Kein End-to-End-Selbsttest im Repo

Weil das Ziel extern ist, lässt sich die Gesamt-Pipeline aus diesem Repo nicht
ausführen; Tests sind Unit-Ebene (`budget_guard`, `nightly_audit_report`). Der
Tar-Traversal-/Symlink-Guard (S-D) und der Egress-Interlock sind ungetestet.
**Fix:** Ein Mini-FastMCP-Smoke-Target als Fixture, gegen das `nightly-audit.sh`
lokal end-to-end grün/rot laufen kann; Shell-Integrationstests für Broker-Handler
und Interlock.

### U-C (mittel) — Issue-Routing ist agent-getrieben, nicht deterministisch

Der Cron-Agent soll „ein bestehendes offenes Issue gleichen Labels
wiederverwenden" — LLM-Judgment im Ausgabepfad einer sonst deterministischen
Pipeline (Duplikate/verpasste Dedup möglich). Der `live-probe`-Job macht genau
das deterministisch in JS (`github-script`, Marker-Kommentar). **Fix:** Dieselbe
Mechanik in den Nightly-Findings-Pfad übernehmen; der Agent liefert nur Body +
Label, das Open/Update ist Code.

### U-D (niedrig) — Phase-3-Schreibpfad ist eher Prompt als Code

„Reply OK → `fix/<slug>`-Draft-PR" existiert als Anweisung; die Fähigkeit
*Finding → Fix-PR* ist nur als `examples/worker-tdd-demo` demonstriert, nicht als
committete Automatisierung. **Fix:** Entweder implementieren oder in README/Plan
klar als „aspirativ/manuell" markieren, damit die Roadmap nicht mehr verspricht,
als läuft.

---

## 5. Priorisierung

| ID  | Schwere | Kategorie | Kern | Aufwand |
|-----|---------|-----------|------|---------|
| S-A | hoch    | Sicherheit | Broker klassifiziert grün bei fehlender promptfoo.json + Null-Gates | klein |
| T-A | hoch    | Technik   | OWASP-Red-Team-Block läuft mit `eval` nie | mittel |
| T-B | hoch    | Technik   | Budget-Guard im microVM-Zielbild unverdrahtet | mittel |
| T-C | hoch    | Technik   | Credential-freier Worker kann `llm-rubric` nicht ausführen | mittel |
| S-D | mittel  | Sicherheit | Broker-Listener: Limits/Timeout/Symlink/IPI-in-Report | mittel |
| S-E | mittel  | Sicherheit | Dokumentierter PAT-Scope kann Findings-Flow nicht ausführen | klein |
| S-B | mittel  | Sicherheit | Worker-Bootstrap: `curl\|sh`, ungepinnt | mittel |
| S-C | mittel  | Sicherheit | Egress: DNS-/443-Exfil offen, Broker-Allowlist fehlt als Code | mittel |
| T-D | mittel  | Technik   | Schema-Gate „grün" bei Abwesenheit | klein |
| T-E | mittel  | Technik   | Token-Ceiling ohne TensorZero wirkungslos | klein |
| U-A/B/C | mittel | UX/Betrieb | Setup-Komplexität, kein E2E-Test, Issue-Dedup agent-getrieben | mittel |
| S-F, T-F, T-G, U-D | niedrig | gemischt | Grader-Guard, Coverage, Framing, Schreibpfad | klein |

**Empfohlene erste Iteration (kleiner Aufwand, hohe Wirkung):** S-A + T-D
(beide je ein Klassifikator-/Gate-Guard + Test), S-E (Doku-Korrektur + Preflight-
Check), S-F (Grader-Family-Guard). Danach die drei „hoch/mittel"-Integrations-
themen T-A, T-B, T-C, die alle dieselbe Ursache haben: eine dokumentierte Garantie
ist nicht vollständig verdrahtet.

---

## 6. Fazit

Zielführend: **ja.** Der Determinismus- und Vertrauensgrenzen-Kern ist
überdurchschnittlich sauber und ehrlich dokumentiert. Die Schwächen sind fast
ausnahmslos **Integrationslücken zwischen Anspruch und Implementierung**, nicht
Architekturfehler: S-A/T-D (ein Gate liest Abwesenheit als Grün), T-A (das
beworbene Red-Team läuft nicht), T-B/T-C (das microVM-Zielbild hat Budget-Guard
und LLM-Ebene nicht verdrahtet), S-D/S-E (Broker-Senke und PAT-Scope nicht zu
Ende gedacht). Jede einzelne schwächt genau die Eigenschaft, die das Projekt als
Alleinstellungsmerkmal beansprucht — und jede ist mit überschaubarem Aufwand
zu schließen, weil die richtige Struktur (Evidenz/Klassifikation, Fail-closed,
Writer≠Grader) bereits steht.
