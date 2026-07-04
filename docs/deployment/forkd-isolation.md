# forkd / microVM-Isolation (Phase 5) — Untrusted-Reader vs. Credential-Halter

> Status: **optional, erst wenn stabil** · Linux-only · ersetzt die Docker-Sandbox
> als *innerste* der drei Sicherheitsschichten (Host → Sandbox → forkd).
>
> Reihenfolge: Erst alle Gates grün und der Betrieb auf dem dedizierten Host
> ([raspberry-pi.md](raspberry-pi.md)) stabil — **dann** diese Schicht. forkd
> ist v0.4/0.5 und bewegt sich schnell; betreibe es nicht als kritischen Pfad,
> solange die Docker-Sandbox ihren Job tut.

## Warum überhaupt microVMs?

Heute läuft alles in **einer** Docker-Sandbox (`openclaw.json`:
`sandbox.mode=all`). Container teilen sich den Host-Kernel — ein Kernel-Exploit
oder eine Sandbox-Flucht trifft sofort den Prozess, der den **GitHub-PAT und den
Anthropic-Key hält**. Genau dieser Prozess hat den grössten Blast-Radius.

Eine **microVM** (eigener Gast-Kernel via KVM) hebt die Isolationsgrenze von
Namespaces/cgroups auf eine Hardware-virtualisierte Grenze. Der eigentliche
Gewinn kommt aber erst aus der **Aufteilung in zwei VMs**:

```
                 ┌──────────────────────────────────────┐
   Telegram ───▶ │  VM-A: Credential-Halter ("Broker")  │
   (Steuerung)   │  - hält GitHub-PAT + Anthropic-Key   │
                 │  - KEIN Ziel-Code, KEINE Tool-Outputs│
                 │  - nur: PR öffnen, Modell aufrufen   │
                 └───────────────┬──────────────────────┘
                                 │  schmaler, getypter Kanal
                                 │  (Job rein, Report raus — nie roher Code/Output)
                 ┌───────────────▼──────────────────────┐
   Ziel-Repo ──▶ │  VM-B: Untrusted-Reader ("Worker")   │
   Zürich-APIs   │  - klont/liest Ziel read-only        │
   promptfoo     │  - ruff/mypy/pytest/promptfoo        │
                 │  - sieht NIE die Credentials         │
                 └──────────────────────────────────────┘
```

**Kerninvariante:** Die VM, die untrusted Daten *verarbeitet* (Ziel-Code,
API-Payloads, Logs — siehe AGENTS.md „Untrusted data"), besitzt **niemals** die
Credentials. Die VM, die Credentials besitzt, *verarbeitet* **niemals** rohe
untrusted Daten — sie bekommt nur das deterministische Ergebnis (Exit-Codes +
den knappen Report aus `nightly_audit_report.py`) über einen schmalen Kanal.
Eine Prompt-Injection im Ziel-Repo kann so bestenfalls VM-B verderben — und VM-B
hat nichts, was sich exfiltrieren liesse.

Das ist dieselbe Trennung, die der Plan unter „Sicherheit: … später forkd
(Privilege Separation)" meint — jetzt konkret.

## Host-Voraussetzungen (beide Pfade)

```bash
# 1) KVM muss verfügbar sein (nested virt, falls der Host selbst eine VM ist):
ls -l /dev/kvm                      # muss existieren und für die Gruppe les-/schreibbar sein
# 2) CPU-Virtualisierung an (x86):
grep -Eoc '(vmx|svm)' /proc/cpuinfo # > 0
# 3) ARM64: KVM kommt aus dem Kernel — prüfe:
#    zcat /proc/config.gz | grep CONFIG_KVM   bzw.  dmesg | grep -i kvm
```

Beide microVM-Monitore brauchen `/dev/kvm`. Auf einer geschachtelten VM (lokale
Linux-VM-Variante aus raspberry-pi.md) muss der Hypervisor **nested
virtualization** erlauben.

## Pfad A — x86-64: forkd / Cloud Hypervisor / Firecracker

Auf x86-64-Hosts (eigener Linux-Server oder VPS) ist der microVM-Stack
ausgereift. **forkd** orchestriert kurzlebige microVMs auf Basis eines
KVM-Monitors (Cloud Hypervisor / Firecracker); ideal für „pro Audit eine frische
Wegwerf-VM".

Grobskizze (zwei persistente VMs, der Worker pro Lauf neu):

```bash
# Pseudobefehle — exakte Flags je forkd-Version (v0.4/0.5) prüfen.
# Broker-VM (langlebig, hält die Credentials, minimaler Egress):
forkd vm create broker \
  --kernel ./vmlinux --rootfs ./broker.ext4 \
  --vcpus 1 --mem 512M \
  --egress api.anthropic.com,api.github.com \
  --no-net-to vm:worker

# Worker-VM (read-only Reader; pro Audit frisch geklont, danach verworfen):
forkd vm clone worker-base worker-$(date +%s) \
  --vcpus 2 --mem 2G \
  --egress github.com,<zürich-endpunkte> \
  --no-egress api.anthropic.com,api.github.com   # Worker braucht KEINE Provider-/PR-Credentials
```

- **Egress je VM hart begrenzen** — exakt wie die Allowlist in `TOOLS.md`, aber
  jetzt asymmetrisch: Worker → GitHub (anon, read-only) + Zürich-APIs; Broker →
  Anthropic + GitHub-API (PR). Keine VM erreicht die Egress-Ziele der anderen.
- **Worker pro Lauf wegwerfen** — frische rootfs je Audit; eine Kompromittierung
  überlebt keinen Lauf.

Firecracker direkt (ohne forkd) ist die konservative Alternative, wenn forkd
noch zu jung ist — gleiche Topologie, mehr Handarbeit am vsock-Kanal.

## Pfad B — ARM64 (Raspberry Pi 5, empfohlener Host)

forkd, Cloud Hypervisor und Firecracker sind in der Praxis **x86-zentriert**;
auf ARM64 ist die Unterstützung jünger und teils unvollständig. Auf dem
empfohlenen Pi-5-Host nimmst du daher den nativen KVM-on-ARM-Weg:

- **QEMU `microvm`-Maschine** (`-M microvm`) auf KVM (`-accel kvm`,
  `-cpu host`) — minimaler Maschinentyp, schneller Boot, ohne vollen
  Firmware-Overhead. Das ist der robusteste microVM-Pfad auf aarch64 heute.
- **Cloud Hypervisor auf ARM64** ist möglich, aber Reifegrad und Features pro
  Release prüfen, bevor du es in den kritischen Pfad nimmst.
- Pi-Realität: 8 GB RAM reichen für **zwei kleine VMs** (Broker ~512 MB, Worker
  ~2–3 GB). Lege die rootfs-Images auf die **NVMe-SSD** (M.2-HAT), nicht die
  microSD — sonst wird der Worker-Clone pro Lauf zum Flaschenhals.

Skizze (eine Broker-VM, Worker pro Lauf via Overlay-Image):

```bash
# Worker (read-only Reader) — frisches qcow2-Overlay auf einem Read-only-Base:
qemu-img create -f qcow2 -b worker-base.qcow2 -F qcow2 worker-run.qcow2
qemu-system-aarch64 -M microvm -accel kvm -cpu host -smp 2 -m 2048 \
  -kernel Image -drive file=worker-run.qcow2,if=virtio,format=qcow2 \
  -netdev user,id=n0,restrict=off \
  -device virtio-net-device,netdev=n0 \
  -nographic   # KEIN Mount der Credentials, KEIN Pfad zur Broker-VM
```

> **`restrict=off`, nicht `on`.** QEMU-`restrict=on` würde den Gast **komplett**
> vom Netz trennen — dann scheitert schon der Auditor-/Ziel-Clone. Der Worker
> *muss* raus (GitHub/uv/npm/Zürich); die eigentliche Begrenzung („nur DNS+Web,
> kein Host-LAN, keine Random-Ports") gehört auf die **Host-Firewall**, UID-scoped
> auf den qemu-Prozess. Als Code mitgeliefert: `deploy/microvm/egress-allowlist.nft`
> + `apply-egress-allowlist.sh`; `run-worker.sh` verweigert den Start ohne die
> geladene Tabelle. Eine **Domain-**Allowlist (statt „jedes 443") braucht einen
> Filter-Forward-Proxy vor dem Gast.

Der Kanal zwischen den VMs läuft über **virtio-vsock** (kein TCP/IP nötig): der
Worker schreibt nur **rohe Evidenz** (`nightly-evidence.json` = die Gate-Exit-Codes
+ die promptfoo-JSON) in den vsock — **kein** selbst erklärtes Verdikt. Der
**vertrauenswürdige Broker klassifiziert selbst** (`nightly_audit_report.py
--from-evidence`) und entscheidet anhand *seines* Exit-Codes über Issue/PR. So kann
ein kompromittierter Worker kein „grün" fälschen: fehlende/verfälschte Evidenz wird
zum hard-fail, nie zu grün (Analysis S2). Roher Ziel-Code überquert den Kanal **nie**.
Restrisiko: ein voll kompromittierter Worker kann noch *in sich schlüssige* grüne
Evidenz liefern (alle Exit-Codes 0 + saubere promptfoo-JSON) — dagegen hülfe nur
Ergebnis-Attestierung (out of scope). Der Gewinn: Auslassung, Verfälschung oder ein
Exit-Code/promptfoo-Widerspruch lesen sich nicht mehr als grün.

> Wenn dir der ARM-Pfad zu jung ist: **bleib bei der Docker-Sandbox** auf dem Pi
> (Status quo) und ziehe die microVM-Trennung erst auf einem x86-Host nach. Genau
> das meint „erst wenn stabil".

## Mapping auf die bestehende Architektur

| Schicht | Heute | Mit Phase 5 |
|---|---|---|
| Host | dedizierter Pi 5, eigenes VLAN | unverändert |
| Sandbox | **eine** Docker-Sandbox (`sandbox.mode=all`) | **zwei microVMs** (Broker / Worker) |
| Credentials | im Sandbox-Prozess | **nur** in der Broker-VM |
| Untrusted-Verarbeitung | im selben Prozess | **nur** in der Worker-VM |
| Kanal | in-process | schmaler vsock (Job rein / rohe Evidenz raus, Broker klassifiziert) |

`scripts/nightly-audit.sh` bleibt das deterministische Herzstück — es läuft
**innerhalb der Worker-VM** und produziert dort die rohe Evidenz. Die
**Klassifikation** (`nightly_audit_report.py`) läuft dagegen auf der **Broker-Seite**
über die empfangene Evidenz, damit das Verdikt nicht aus der untrusted VM stammt.
Die Budget-Leitplanken ([../budget/guardrails.md](../budget/guardrails.md)) laufen
ebenfalls auf dem **Broker** (der Worker läuft mit `BUDGET_GUARD=0`, weil seine VM
throwaway ist und keine Historie über Läufe hält).

## Migrations-Checkliste

1. Docker-Sandbox stabil grün, Phase 4 läuft Wochen ohne hard-fail.
2. Host wählen: x86 (Pfad A) oder Pi/ARM (Pfad B). `/dev/kvm` prüfen.
3. **Worker-VM** bauen: Toolchain (uv, ruff, mypy, pytest, node/promptfoo),
   `nightly-audit.sh`, **keine** Credentials. Egress: nft-Port+LAN+DNS-Set
   (`egress-allowlist.nft`); Domain-Allowlist (GitHub/PyPI/npm/Zürich) via
   Forward-Proxy (`deploy/microvm/forward-proxy/`).
4. **Broker-VM** bauen: hält PAT + Anthropic-Key, ruft Modell + öffnet PRs.
   Egress: `broker-egress-allowlist.nft`; Domain-Allowlist (Anthropic/OpenAI/
   GitHub-API/Telegram) via Forward-Proxy (`broker-allow.txt`).
5. vsock-Kanal: Worker → (rohe Evidenz: `nightly-evidence.json` + promptfoo-JSON) →
   Broker; der **Broker klassifiziert** (`--from-evidence`). Inhalt als untrusted
   behandeln (AGENTS.md), nie als Shell interpolieren; fehlende Evidenz = hard-fail.
6. Worker pro Lauf wegwerfen (frische rootfs/Overlay). Broker langlebig.
7. Erst danach `sandbox.mode` in `openclaw.json` entsprechend zurückfahren.

## Goldene Regeln (Phase-5-spezifisch)

- Die Credential-VM verarbeitet **nie** rohe untrusted Daten.
- Die Reader-VM besitzt **nie** Credentials.
- Der Kanal transportiert **Ergebnisse** (Exit-Code + Report), nie rohen Code
  oder rohe API-Payloads.
- forkd/microVM ist optional und additiv — die Docker-Sandbox bleibt der
  Fallback, bis die microVM-Schicht nachweislich stabil ist.
