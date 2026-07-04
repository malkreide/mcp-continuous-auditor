# Phase-5-Rollout auf einer lokalen Linux-VM (beide Schichten)

> Ziel: TensorZero **und** die microVM-Isolation real ausrollen — auf **einer
> lokalen Linux-VM** (eigenes Subnetz, nested KVM). Kit: [`deploy/`](../../deploy).
>
> Was wo läuft:
> - **Broker = die Host-VM selbst**: OpenClaw + die Credentials (GitHub-PAT,
>   Anthropic-Key, Telegram) + das TensorZero-Gateway + ClickHouse, alles an
>   `127.0.0.1` gebunden.
> - **Worker = throwaway microVM pro Lauf**: `nightly-audit.sh` read-only,
>   **keine** Credentials, Egress nur GitHub-anon + Zürich, spricht mit dem Broker
>   **nur** über vsock.
>
> Das ist die pragmatische Minimaltopologie mit vollem Isolations-Gewinn: die VM,
> die untrusted Daten verarbeitet, hält nie Credentials. Die volle Zwei-microVM-
> Variante (auch der Broker in einer eigenen VM) steht in
> [forkd-isolation.md](forkd-isolation.md) als nächster Härtungsschritt.

Alle Befehle laufen **auf der lokalen Linux-VM**, nicht in der Cloud-Session.

---

## 0. Preflight

```bash
git clone https://github.com/malkreide/mcp-continuous-auditor.git
cd mcp-continuous-auditor
cp .env.example .env        # Tokens + CLICKHOUSE_PASSWORD eintragen (siehe unten)

bash deploy/00-preflight.sh
```

Behebe jede `MISS`-Zeile (nested KVM, qemu, socat, cloud-image-utils, docker,
`vhost_vsock`-Modul). **Fertig wenn:** `PREFLIGHT: ready`.

`.env` braucht für diesen Rollout zusätzlich:

```bash
ANTHROPIC_API_KEY=...          # liegt NUR auf der Broker-Seite (Host-VM)
GITHUB_TOKEN=...               # PR-scoped, NUR Broker
TARGET_REPO=malkreide/zurich-opendata-mcp
CLICKHOUSE_PASSWORD=...        # frei wählbar, von docker-compose verlangt
# CLICKHOUSE_USER=tensorzero   # optional
```

---

## 1. TensorZero (Broker-Seite)

```bash
# Bilder vorher in tensorzero/docker-compose.yml auf getestete Tags pinnen!
bash deploy/tensorzero/up.sh
```

Das Script zieht Gateway + ClickHouse hoch, wartet auf Health und macht einen
Smoke-Inference-Call. **Fertig wenn:** `smoke OK` und
`curl -s http://127.0.0.1:3000/openai/v1/models` antwortet.

OpenClaw aufs Gateway zeigen (Details:
[../observability/tensorzero.md](../observability/tensorzero.md)):

```bash
# In der .env / OpenClaw-Provider-Config:
ANTHROPIC_BASE_URL=http://127.0.0.1:3000     # bzw. provider baseURL = .../openai/v1
TENSORZERO_GATEWAY=http://127.0.0.1:3000     # aktiviert das Episode-Tagging in nightly-audit.sh
```

Damit trägt jeder nächtliche Lauf eine Episode-ID; sein **echter** Token-Verbrauch
(Writer + Grader) wird aus ClickHouse summiert und ist das harte Pro-Lauf-Ceiling
für `budget_guard` — der Cost-Cap ist damit end-to-end real, nicht mehr nur die
promptfoo-Zählung.

**Fertig wenn:** nach einem Lauf liefert
`bash deploy/tensorzero/episode-tokens.sh <episode-id>` eine Zahl > 0.

---

## 2. microVM-Worker

### 2a. Base-Image + Seed bauen (einmalig, re-runnable)

```bash
TARGET_REPO=malkreide/zurich-opendata-mcp bash deploy/microvm/build-worker-image.sh
```

Lädt das Debian-Cloud-Image (read-only Base) und rendert den cloud-init-Seed.
**Fertig wenn:** `Worker base image ready` mit Base + Seed-Pfad.

### 2b. Broker-Listener starten (vor jedem Worker-Lauf)

```bash
# Empfängt das Worker-Ergebnis über vsock und legt es in die Dropbox.
bash deploy/microvm/channel/broker-listener.sh 9000 ./.audit/incoming
# (Im echten Betrieb als systemd-Unit dauerhaft laufen lassen.)
```

### 2c. Throwaway-Worker-Lauf (über den Breaker-Orchestrator)

In einem zweiten Terminal — nutze `run-audit-cycle.sh` als Broker-Entrypoint
(er verdrahtet den Budget-Breaker, Analysis T-B), nicht `run-worker.sh` direkt:

```bash
DROPBOX=./.audit/incoming bash deploy/microvm/run-audit-cycle.sh
```

`run-audit-cycle.sh` (1) ruft `budget_guard preflight` — bei **offenem** Breaker
wird der Lauf übersprungen; (2) startet `run-worker.sh`: das erzeugt ein frisches
qcow2-Overlay, bootet die Worker-VM (eigener Gast-Kernel via KVM), die VM klont
den Auditor **an einem gepinnten, verifizierten SHA** (Analysis S-B), läuft
`nightly-audit.sh` **read-only** (`PROMPTFOO_PROFILE=determ`) gegen das Ziel,
schickt die **rohe Evidenz** über vsock zum Broker und **schaltet sich ab** (das
Overlay wird verworfen); (3) füttert das klassifizierte Ergebnis in
`budget_guard record` — ein Lauf **ohne** eingegangene Evidenz zählt als
Hard-Fail. (`run-worker.sh` bootet nur mit geladener Egress-Allowlist — siehe §3.)

**Fertig wenn:** der Listener loggt `received -> …/<ts>-<pid>/ (…; broker-classified:
<outcome>)` und dort liegen `nightly-summary.json` + `nightly-report.md` — **vom
Broker** aus der Evidenz erzeugt, nicht vom Worker. Der Worker hat dabei **nie**
einen Credential gesehen, es überquerte **kein** roher Ziel-Code den Kanal, und das
Verdikt stammt nicht aus der untrusted VM (fehlende/verfälschte Evidenz → hard-fail).

---

## 3. Egress-Allowlist (Host-Firewall)

Die VM-User-Mode-Netzwerke (SLIRP) sind absichtlich **nicht** der Durchsetzungspunkt:
SLIRP isoliert den Gast zwar vom Host-LAN, begrenzt aber **nicht**, welche
Internet-Hosts er erreicht. Pinne den Egress am Host (nftables), **asymmetrisch**:

- **Worker-VM** → DNS + Web (80/443) ins öffentliche Internet, **kein** Host-LAN,
  keine sonstigen Ports (kein SMTP-Exfil, kein C2 auf Random-Ports).
- **Broker** (Host-VM) → `api.anthropic.com`, `api.openai.com` (Grader),
  `api.github.com`, Telegram.

Die Worker-Regeln sind als Code mitgeliefert und werden UID-scoped durchgesetzt
(SLIRP-Egress lässt sich nur über die UID des qemu-Prozesses greifen):

```bash
sudo deploy/microvm/apply-egress-allowlist.sh   # legt User 'mcpworker' an + lädt die nft-Tabelle
```

`run-worker.sh` **verweigert den Start**, solange die Tabelle `inet
mcp_worker_egress` nicht geladen ist, und startet qemu als `mcpworker`, damit die
Regeln greifen (Override nur für isolierte Dev-Hosts: `EGRESS_ALLOWLIST=off`). Die
Ruleset-Datei: `deploy/microvm/egress-allowlist.nft`. Eine strengere
**Domain-**Allowlist (nur github/openai/… statt „jedes 443") braucht einen
Filter-Proxy — siehe `docs/deployment/forkd-isolation.md`.

---

## 4. Zusammenführen: nightly-audit im Worker, Routing im Broker

Im Zielbild ruft der OpenClaw-Cron (`openclaw/cron/nightly-audit.json`) nicht mehr
`nightly-audit.sh` direkt auf, sondern:

1. `deploy/microvm/run-audit-cycle.sh` (Broker-Orchestrator, Analysis T-B):
   `budget_guard preflight` → `run-worker.sh` (Worker macht den read-only Audit,
   schickt rohe Evidenz; der `broker-listener` **klassifiziert sie auf der
   Broker-Seite**) → `budget_guard record` (fehlende Evidenz = Hard-Fail),
2. liest die **Broker-erzeugte** `./.audit/incoming/<neueste>/nightly-summary.json`,
3. routet nach dem **Exit-Code** exakt wie heute (grün → Announce; Findings →
   Issue + nach Telegram-OK ein `fix/<slug>`-Draft-PR; hard-fail → stoppen).

Der Modellaufruf für Schritt 2/3 läuft über das TensorZero-Gateway (Broker),
der Cost-Cap greift über die Episode-Tokens. Der Breaker-State (`budget_guard`)
lebt auf der **Broker-Seite** und wird von `run-audit-cycle.sh` gefüttert (nicht
mehr funktionslos, Analysis T-B) — der Worker läuft mit `BUDGET_GUARD=0`, weil
seine VM throwaway ist und keine Historie über Läufe hält.

Der Worker fährt zudem `PROMPTFOO_PROFILE=determ` (Analysis T-C): er hält **keine**
Keys, also läuft dort nur das key-lose deterministische Profil (Contract +
Injection). Die modell-gegradete Ebene (`llm-rubric` + Red-Team) läuft in
CI-mit-Secrets bzw. einem keyed Lauf — nie in der Wegwerf-VM, wo ein fehlender Key
sonst jede Nacht hard-failen würde. Das Verdikt trägt das Profil, damit ein
determ-Grün nie als „Red-Team bestanden" gelesen wird.

**Fertig wenn:** ein nächtlicher Lauf produziert ungefragt einen Report auf
Telegram, dessen deterministisches Ergebnis in einer Wegwerf-VM ohne Credentials
entstand, und dessen Kosten im ClickHouse-Audit-Trail stehen.

---

## Reihenfolge & Rückzug

1. Preflight grün.
2. TensorZero hoch + OpenClaw verdrahtet → ein Lauf, Episode-Tokens > 0.
3. Worker-Image gebaut, ein manueller `run-worker.sh` liefert ein Ergebnis.
4. Egress-Allowlist am Host gepinnt.
5. Cron auf den Worker-Pfad umgestellt.

**Rückzug** jederzeit: `TENSORZERO_GATEWAY` / `ANTHROPIC_BASE_URL` aus der `.env`
nehmen (zurück zu Direkt-Anthropic) und den Cron wieder direkt `nightly-audit.sh`
aufrufen lassen (Docker-Sandbox als Fallback). Die Budget-Leitplanken laufen in
beiden Modi weiter.

## Goldene Regeln (unverändert)

- Die Credential-Seite verarbeitet nie rohe untrusted Daten; der Kanal trägt nur
  Ergebnisse (Exit-Code + Report), nie rohen Code.
- Ein übersprungener/abgebrochener Lauf ist nie ein Bestehen.
- Mensch ist das Merge-Gate; CI ist die Wahrheit.
