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
- **Unabhängiger Grader** — LLM-bewertete Checks nutzen eine andere Modell-Familie als der Schreiber.
- **Kontinuierliches Red-Teaming** — OWASP LLM Top 10 (Prompt Injection, PII-Leak) gegen die MCP-Oberfläche.
- **Mensch als Merge-Gate** — der Agent öffnet nur PRs, pusht nie auf `main`.
- **Proaktiv** — ein täglicher Cron-Audit postet einen Report nach Telegram.

## Voraussetzungen

- Node.js 20+ (OpenClaw, promptfoo)
- Python 3.11+ und [uv](https://github.com/astral-sh/uv)
- Docker (Agenten-Sandbox)
- Telegram-Bot-Token (via [@BotFather](https://t.me/BotFather)) und deine numerische Telegram-User-ID
- Fine-grained GitHub-PAT, auf das Ziel-Repo beschränkt (contents + pull-requests, **keine** Secrets)
- Anthropic-API-Key (unabhängiger Grader)

## Installation

```bash
git clone https://github.com/malkreide/mcp-continuous-auditor.git
cd mcp-continuous-auditor
cp .env.example .env        # Tokens eintragen
npm i -g openclaw promptfoo
```

## Verwendung / Schnellstart

```bash
# 1. Gateway starten (liest openclaw/openclaw.json)
openclaw start --config openclaw/openclaw.json

# 2. Auf Telegram dem Bot schreiben:
#    audit
#    -> liefert einen ruff/mypy/pytest-Report, read-only, ohne Code-Aenderung

# 3. Deterministische Verifikation lokal ausfuehren
promptfoo eval -c promptfoo/promptfooconfig.yaml
```

## Konfiguration

| Variable | Zweck |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot-Token von @BotFather |
| `TELEGRAM_ALLOW_FROM` | Deine numerische Telegram-User-ID (Gating) |
| `ANTHROPIC_API_KEY` | Unabhängiges Grader-Modell |
| `GITHUB_TOKEN` | Fine-grained PAT, Ziel-Repo, nur PR |
| `TARGET_REPO` | z.B. `malkreide/zurich-opendata-mcp` |

## Deployment

Die LLM-Inferenz läuft in der Cloud (Anthropic API) — lokal läuft nur der
OpenClaw-Orchestrator. Da dieser Prozess das GitHub-PAT + den Anthropic-Key hält
und Shell-Tools startet, ist die **empfohlene Betriebsart** ein dediziertes,
netz-isoliertes Gerät statt deines Arbeits-PCs.

**Empfohlen: ein dedizierter Raspberry Pi 5 (8 GB).** Die Last ist leicht
(Orchestrierung + API-Calls, kein lokales Modell), und ein separates Gerät fügt
eine echte Hardware-/Netzwerk-Isolationsschicht über die bestehende Docker-Sandbox
und den fein-granularen PAT hinzu. Vollständige Anleitung (OS-Setup, ARM64-Checks,
Egress-Allowlist, systemd-Härtung):
**[docs/deployment/raspberry-pi.md](docs/deployment/raspberry-pi.md)**.

Gleichwertige **Alternativen** bleiben unterstützt: eine lokale Linux-VM in
eigenem Subnetz oder ein günstiger VPS. Die Trade-offs stehen in derselben Anleitung.

## Projektstruktur

```
openclaw/         OpenClaw-Gateway-Config + Policy-as-Code (SOUL/AGENTS/TOOLS)
openclaw/cron/    nightly-audit Cron-Job-Spec + Installer (taeglich 03:00 → Telegram)
skills/           python-auditor, fastmcp-testing, promptfoo-eval
promptfoo/        deterministische Asserts, Schema-Drift, Red-Team
scripts/          Audit-Harness, woechentlicher Live-Probe + nightly-audit-Cron-Kern
.github/          CI = die Wahrheitsinstanz (Template fuer das Ziel-Repo)
docs/plans/       der v2-Bauplan
docs/cron/        der taegliche nightly-audit-Cron (Ablauf, Modell-Hard-Fail, Install)
docs/deployment/  Raspberry-Pi-Anleitung (empfohlener Host) + Alternativen
```

## Roadmap

Phase 0 Baseline → 1 Read-only-Auditor → 2 promptfoo-CI-Gate → 3 PR-only-Worker → 4 Cron + Red-Team → 5 Härtung (forkd, TensorZero). Siehe [docs/plans](docs/plans).

## Changelog

Siehe [CHANGELOG.md](CHANGELOG.md)

## Lizenz

MIT License — siehe [LICENSE](LICENSE)

## Autor

Hayal Özkan · [malkreide](https://github.com/malkreide)
