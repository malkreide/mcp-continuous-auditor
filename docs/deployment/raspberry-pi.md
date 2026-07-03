# Deployment auf dediziertem Gerät (Raspberry Pi 5) — empfohlen

> Status: Empfohlene Betriebsart · Ziel: OpenClaw-Orchestrator auf einem
> isolierten, günstigen Gerät statt auf dem Arbeits-PC.

## Warum überhaupt ein eigenes Gerät?

Die schwere Arbeit dieser Lösung — die LLM-Inferenz — läuft **nicht lokal**,
sondern in der Cloud (Anthropic API). Lokal läuft nur der **OpenClaw-Orchestrator**:
ein Agent, der Shell-Tools startet, ein GitHub-PAT und einen Anthropic-Key hält
und über Telegram gesteuert wird. Genau dieser Prozess ist die Komponente mit
dem größten Blast-Radius — und der Grund, ihn vom persönlichen PC zu trennen.

Das Projekt mindert das Risiko bereits in mehreren Schichten (Docker-Sandbox,
fein-granularer PAT ohne Secrets / nur PR, kein Push auf `main`, Telegram-ID-Gating;
siehe `openclaw/workspace/TOOLS.md`). Ein **dediziertes Gerät** ergänzt das um eine
echte Hardware- und Netzwerk-Isolationsschicht:

- Auf dem Gerät liegen **keine privaten Daten** — selbst ein Sandbox-Ausbruch trifft
  nur eine Wegwerf-Maschine, nicht deinen Arbeits-PC.
- Es lässt sich in ein **eigenes VLAN / Gäste-Netz** hängen, getrennt vom PC.
- Der **Egress** lässt sich am Router hart auf die nötigen Ziele begrenzen
  (passt exakt zur Egress-Policy in `TOOLS.md`).

**Empfehlung: ein dedizierter Raspberry Pi 5 (8 GB), netz-isoliert.** Bestes
Verhältnis aus Isolation, Kosten und geringer Wartung. Warum der Pi reicht: Er
orchestriert nur und ruft Cloud-APIs auf — eine leichte Last. Kein lokales Modell.

## Voraussetzungen (Hardware)

- Raspberry Pi 5, **8 GB RAM** (4-GB-Modell wird mit Node + Python + Docker zu eng).
- microSD ≥ 32 GB (besser: NVMe-SSD via M.2-HAT für Docker-Layer und Logs).
- Stabile Stromversorgung (offizielles 27-W-USB-C-Netzteil) und Ethernet.

## 1. Betriebssystem

**64-bit Raspberry Pi OS (Bookworm) ist Pflicht** — 32-bit kann Docker und die
Toolchains nicht sauber betreiben.

```bash
# Architektur prüfen — muss aarch64 liefern:
uname -m            # -> aarch64
```

Nach dem ersten Boot:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git curl ca-certificates
```

## 2. Laufzeiten installieren (alle ARM64-nativ)

### Node.js 20+ (OpenClaw, promptfoo)

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v             # -> v20.x oder neuer
```

> Hinweis: NodeSource liefert offizielle `arm64`-Pakete.

### Python 3.11+ und uv

```bash
python3 --version   # Bookworm liefert 3.11 — passt
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

### Docker (Agenten-Sandbox)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"     # neu einloggen, damit die Gruppe greift
docker run --rm hello-world         # Smoke-Test (zieht ein arm64-Image)
```

## 3. OpenClaw + promptfoo installieren — mit ARM64-Check

Dies ist der **eine Punkt, den du verifizieren musst**: ob OpenClaw saubere
ARM64-Builds bzw. keine x86-nativen Abhängigkeiten hat.

```bash
npm i -g openclaw promptfoo

# Verifizieren, dass beide ohne Architektur-Fehler starten:
openclaw --version
promptfoo --version
```

Falls die Installation an einer **nativen Node-Abhängigkeit** scheitert
(`node-gyp`-Fehler, „prebuilt binary not found for arm64"):

```bash
# Build-Toolchain nachziehen und Neuinstallation erzwingen:
sudo apt install -y build-essential python3-dev
npm i -g openclaw --build-from-source
```

Bleibt eine Abhängigkeit hart x86-only, ist der **Fallback** ein x86-Host
(siehe Alternativen unten) — der Rest der Lösung (CI, promptfoo, Sandbox) ist
davon unberührt.

## 4. Projekt aufsetzen

```bash
git clone https://github.com/malkreide/mcp-continuous-auditor.git
cd mcp-continuous-auditor
cp .env.example .env        # Tokens eintragen — NIE committen (.gitignore deckt das)
```

Trage in `.env` ein: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOW_FROM`,
`ANTHROPIC_API_KEY`, `GITHUB_TOKEN` (fine-grained, nur Ziel-Repo,
contents + pull-requests, **keine** Secrets), `TARGET_REPO`.

## 5. Netzwerk-Isolation (der eigentliche Sicherheitsgewinn)

Lege den Pi in ein **eigenes VLAN / Gäste-Netz** und beschränke den **ausgehenden**
Verkehr am Router auf das Nötige. Die Egress-Allowlist deckt sich mit der Policy
in `TOOLS.md`:

| Ziel | Wofür |
|---|---|
| `api.telegram.org` | Kontrollebene (Befehle, Reports) |
| `api.anthropic.com` | LLM-Provider (Schreiber / Tool-Provider-Familie) |
| `api.openai.com` | unabhängiger Grader (Default `openai:gpt-4o-mini`) — entfällt bei lokalem `GRADER_PROVIDER=ollama:…` |
| `github.com`, `api.github.com`, `*.githubusercontent.com` | Ziel-Repo, PRs |
| Zürcher Open-Data-Endpunkte | nur zum Aufzeichnen der Fixtures |

Alles andere ausgehend blockieren. Eingehend wird **nichts** benötigt —
Telegram-Long-Polling ist rein ausgehend, also keine Portfreigabe nötig.

## 6. OpenClaw als Dienst (systemd)

Damit der Orchestrator Reboots übersteht und sauber geloggt wird:

```ini
# /etc/systemd/system/openclaw.service
[Unit]
Description=OpenClaw gateway (MCP Continuous Auditor)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/mcp-continuous-auditor
EnvironmentFile=/home/pi/mcp-continuous-auditor/.env
ExecStart=/usr/bin/openclaw start --config openclaw/openclaw.json
Restart=on-failure
RestartSec=5
# Härtung des Host-Prozesses:
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/pi/mcp-continuous-auditor
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw
journalctl -u openclaw -f          # Logs verfolgen
```

> Pfade (`User`, `WorkingDirectory`) an deinen tatsächlichen Benutzer anpassen.
> Die `Protect*`-Direktiven härten den **Host**-Prozess; die Agenten-Sandbox
> (Docker, `scope: agent`) bleibt davon getrennt die innere Isolationsschicht.

## 7. Smoke-Test

```bash
# Auf Telegram dem Bot "audit" schreiben -> read-only ruff/mypy/pytest-Report.
# Deterministische Verifikation lokal:
promptfoo eval -c promptfoo/promptfooconfig.yaml
```

## Härtungs-Checkliste

- [ ] 64-bit OS, `uname -m` = `aarch64`
- [ ] Pi in eigenem VLAN/Gäste-Netz
- [ ] Egress-Allowlist am Router aktiv (Tabelle oben), Rest blockiert
- [ ] Keine eingehenden Portfreigaben
- [ ] `.env` mit `chmod 600`, nicht im Git
- [ ] GitHub-PAT fein-granular: nur Ziel-Repo, contents + pull-requests, keine Secrets
- [ ] Docker-Gruppe nur für den Service-Benutzer
- [ ] `unattended-upgrades` für Sicherheits-Patches aktiv
- [ ] systemd-Härtung (`NoNewPrivileges`, `ProtectSystem=strict`) gesetzt

---

## Alternativen (gleichwertige Isolation, falls kein Pi)

Beide Wege bleiben unterstützt — sie isolieren den Orchestrator ebenfalls vom
Arbeits-PC, mit anderen Trade-offs.

### A) Lokale VM auf dem PC
Eine Linux-VM (Proxmox, VirtualBox, KVM) in einem eigenen Subnetz. **Vorteil:**
keine Extra-Hardware. **Nachteil:** läuft auf derselben physischen Maschine wie
deine privaten Daten — schwächere physische Trennung als ein separates Gerät.
Setup identisch zu Schritt 2–7 (x86_64 statt aarch64 — der ARM64-Check entfällt,
native Abhängigkeiten sind hier nie ein Thema).

### B) Günstiger VPS
Eine kleine Cloud-Instanz (z. B. Hetzner CX22, ~4 €/Monat). **Vorteil:**
vollständig getrennt vom PC, mehr Leistung, immer erreichbar. **Nachteil:**
deine Credentials (GitHub-PAT, Anthropic-Key, Telegram-Token) liegen dann auf
einem Cloud-Host — Vertrauen in den Anbieter nötig. Egress-Allowlist via
Cloud-Firewall statt Heim-Router.

> Unabhängig vom Host: Die **Wahrheitsinstanz** (pytest + promptfoo) läuft ohnehin
> in **GitHub Actions**, also außerhalb jedes lokalen Geräts. Nur der OpenClaw-
> Orchestrator braucht ein Zuhause — und dafür ist der dedizierte Pi 5 die
> empfohlene Wahl.
