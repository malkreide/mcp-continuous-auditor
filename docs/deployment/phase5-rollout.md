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

### 2c. Throwaway-Worker-Lauf

In einem zweiten Terminal:

```bash
bash deploy/microvm/run-worker.sh
```

Das erzeugt ein frisches qcow2-Overlay, bootet die Worker-VM (eigener
Gast-Kernel via KVM), die VM klont den Auditor, läuft `nightly-audit.sh`
**read-only** gegen das Ziel, schickt `summary.json` + `report.md` über vsock zum
Broker und **schaltet sich ab**. Das Overlay wird verworfen.

**Fertig wenn:** der Listener loggt `received -> ./.audit/incoming/<ts>-<pid>/`
und dort liegen `nightly-summary.json` + `nightly-report.md`. Der Worker hat dabei
**nie** einen Credential gesehen, und es überquerte **kein** roher Ziel-Code den
Kanal — nur das deterministische Ergebnis.

---

## 3. Egress-Allowlist (Host-Firewall)

Die VM-User-Mode-Netzwerke sind absichtlich nicht der Durchsetzungspunkt. Pinne
den Egress am Host (nftables/Router), passend zu `TOOLS.md`, **asymmetrisch**:

- **Worker-VM** → nur `github.com` (anon, read-only) + die Zürcher Endpunkte.
- **Broker** (Host-VM) → `api.anthropic.com`, `api.github.com`, Telegram.

Keine Seite erreicht die Egress-Ziele der anderen. (Konkrete nftables-Regeln je
nach deinem Subnetz-Layout — der Worker bekommt ein eigenes Tap/Bridge-Segment.)

---

## 4. Zusammenführen: nightly-audit im Worker, Routing im Broker

Im Zielbild ruft der OpenClaw-Cron (`openclaw/cron/nightly-audit.json`) nicht mehr
`nightly-audit.sh` direkt auf, sondern:

1. `deploy/microvm/run-worker.sh` (Worker macht den read-only Audit),
2. liest das Ergebnis aus `./.audit/incoming/<neueste>/nightly-summary.json`,
3. routet nach dem **Exit-Code** exakt wie heute (grün → Announce; Findings →
   Issue + nach Telegram-OK ein `fix/<slug>`-Draft-PR; hard-fail → stoppen).

Der Modellaufruf für Schritt 2/3 läuft über das TensorZero-Gateway (Broker),
der Cost-Cap greift über die Episode-Tokens. Der Breaker-State (`budget_guard`)
lebt auf der **Broker-Seite** — der Worker läuft mit `BUDGET_GUARD=0`, weil seine
VM throwaway ist und keine Historie über Läufe hält.

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
